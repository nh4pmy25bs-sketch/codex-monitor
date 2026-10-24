import datetime as dt
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from collector import collect
from main import Monitor
from native_ssh import NativeSSH
from tailscale_auth import AuthCancelled
from test_monitor import record,encoded

TEST_URL='https://login.tailscale.com/a/TEST_ONLY_12345'

class NativeBridgeTests(unittest.TestCase):
    def test_auth_url_and_multiple_requests_use_one_persistent_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);approved=root/'approved';script=root/'bridge.py'
            script.write_text('import sys,json,time\nfrom pathlib import Path\nprint("To authenticate, visit: '+TEST_URL+'",file=sys.stderr,flush=True)\nwhile not Path('+repr(str(approved))+').exists():time.sleep(.01)\nprint("login banner",flush=True)\nprint(json.dumps({"type":"ready","server_time":"2026-10-24T12:00:00+00:00"}),flush=True)\nfor line in sys.stdin:\n print(json.dumps({"echo":json.loads(line)}),flush=True)\n',encoding='utf-8')
            real_popen=subprocess.Popen;arguments=[];opened=[]
            def popen(args,**kwargs):
                arguments.append(args);return real_popen([sys.executable,'-u',str(script)],**kwargs)
            def opener(url,kind):opened.append((url,kind));approved.write_text('ok')
            with patch('native_ssh.subprocess.Popen',side_effect=popen):
                bridge=NativeSSH('example','user',22,'print(1)',[],opener,threading.Event(),threading.Event())
                try:
                    self.assertEqual(bridge.request({'mode':'one'})['echo']['mode'],'one')
                    self.assertEqual(bridge.request({'mode':'two'})['echo']['mode'],'two')
                    self.assertEqual(len(arguments),1)
                    self.assertIn('StrictHostKeyChecking=yes',arguments[0]);self.assertEqual(opened,[(TEST_URL,'ssh')])
                finally:bridge.close()
                self.assertIsNotNone(bridge.process.poll())

    def test_cancel_while_connecting_stops_the_child_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            script=Path(tmp)/'wait.py';script.write_text('import time\ntime.sleep(20)\n')
            real_popen=subprocess.Popen;children=[];created=threading.Event();cancel=threading.Event();errors=[]
            def popen(args,**kwargs):
                child=real_popen([sys.executable,'-u',str(script)],**kwargs);children.append(child);created.set();return child
            def run():
                try:NativeSSH('example','user',22,'print(1)',[],lambda *a:None,cancel,threading.Event())
                except Exception as error:errors.append(error)
            with patch('native_ssh.subprocess.Popen',side_effect=popen):
                thread=threading.Thread(target=run);thread.start();self.assertTrue(created.wait(2));cancel.set();thread.join(3)
                self.assertFalse(thread.is_alive());self.assertIsInstance(errors[0],AuthCancelled)
                self.assertIsNotNone(children[0].poll())

class CompletionTests(unittest.TestCase):
    def test_current_cli_completed_items_render_messages_and_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=Monitor(tmp)
            rows=[record('session_meta',{'id':'current-cli','cwd':'/srv/p'}),
                record('event_msg',{'type':'item_completed','item':{'type':'UserMessage','id':'u','content':[{'type':'text','text':'请检查表单'}]}}),
                record('event_msg',{'type':'item_completed','item':{'type':'AgentMessage','id':'a','content':[{'type':'text','text':'已完成检查'}]}}),
                record('event_msg',{'type':'item_completed','item':{'type':'FileChange','id':'f','status':'completed','changes':{'src/app.py':{'type':'update','unified_diff':'-a\n+b'}}}})]
            try:
                app.store.ingest('s','log',0,encoded(rows));data=app.overview()
                self.assertEqual(data['sessions'][0]['title'],'请检查表单')
                self.assertEqual(data['totals']['files'],1)
                self.assertIn('+b',app.store.files()[0]['body'])
            finally:app.stop.set();app.store.db.close()

    def test_vscode_terminal_new_completion_is_notified_once_and_history_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=Monitor(tmp);sent=[]
            class Sink:
                added=True
                def notify(self,title,body):sent.append((title,body));return True
            app.notifier=Sink();app.config={'host':'test','user':'u','port':22};app.state['mode']='ssh'
            app.notice_cutoff='2026-10-24T12:00:00+00:00'
            source=app.source()
            old=encoded([record('session_meta',{'id':'vscode-cli','cwd':'/srv/project','source':'cli'}),record('event_msg',{'type':'task_complete'},'2026-10-24T11:59:00Z')])
            try:
                app.store.ingest(source,'log',0,old);app.dispatch_completions();self.assertEqual(sent,[])
                item={'sid':'vscode-cli','ts':'2026-10-24T12:00:05Z','cwd':'/srv/project'}
                app.dispatch_completions([item]);app.dispatch_completions([item]);self.assertEqual(len(sent),1)
                new=encoded([record('event_msg',{'type':'task_complete'},item['ts'])])
                app.store.ingest(source,'log',len(old),new);app.dispatch_completions();self.assertEqual(len(sent),1)
                self.assertEqual(len(app.store.alert_history()),1)
                app.set_notifications(False)
                app.dispatch_completions([dict(item,ts='2026-10-24T12:00:07Z')]);self.assertEqual(len(sent),1)
                self.assertIn('project',sent[0][1])
            finally:app.stop.set();app.store.db.close()

    def test_recent_completion_is_found_even_while_full_history_is_backfilling(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'sessions';folder.mkdir()
            rows=[record('session_meta',{'id':'s','cwd':'/srv/p'}),record('event_msg',{'type':'task_complete'},'2026-10-24T11:59:00Z'),record('response_item',{'type':'message','role':'assistant','content':[{'text':'x'*700000}]}),record('event_msg',{'type':'task_complete'},'2026-10-24T12:00:03Z')]
            (folder/'large.jsonl').write_bytes(encoded(rows))
            result=collect({'home':tmp,'days':3650,'max_bytes':65536,'notify_since':'2026-10-24T12:00:00+00:00'})
            self.assertEqual(result['remaining'],1)
            self.assertEqual([x['ts'] for x in result['recent_completions']],['2026-10-24T12:00:03Z'])

if __name__=='__main__':unittest.main(verbosity=2)
