import base64
import datetime as dt
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from core import Store
from collector import collect
from main import Monitor,HostTrustRequired,serve

def record(typ,payload,ts='2026-10-23T16:01:00Z'):
    return {'timestamp':ts,'type':typ,'payload':payload}

def usage(total,last=None,ts='2026-10-23T16:01:00Z'):
    return record('event_msg',{'type':'token_count','info':{'total_token_usage':total,'last_token_usage':last or total}},ts)

def encoded(rows):
    return ('\n'.join(json.dumps(r,ensure_ascii=False) for r in rows)+'\n').encode('utf-8')

class ParserTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root/'test.db')
    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()
    def test_incremental_unicode_and_duplicate_usage(self):
        a={'input_tokens':100,'cached_input_tokens':50,'output_tokens':20,'reasoning_output_tokens':5,'total_tokens':120}
        b={'input_tokens':200,'cached_input_tokens':80,'output_tokens':30,'total_tokens':230}
        rows=[record('session_meta',{'id':'s1','cwd':'/srv/项目'}),record('event_msg',{'type':'user_message','message':'请检查中文目录'}),usage(a),usage(a,ts='2026-10-23T16:02:00Z'),usage(b,ts='2026-10-24T16:02:00Z')]
        body=encoded(rows);mid=body.find('中文'.encode())+1
        self.store.ingest('one','a.jsonl',0,body[:mid]);self.store.ingest('one','a.jsonl',mid,body[mid:])
        result=self.store.overview();self.assertEqual(result['totals']['tokens'],230)
        self.assertEqual(result['daily'][0]['date'],'2026-10-24');self.assertEqual(result['daily'][0]['total'],120)
        self.assertEqual(result['daily'][1]['date'],'2026-10-25');self.assertEqual(result['daily'][1]['total'],110)
        self.assertEqual(result['daily'][0]['cached'],50)
        self.store.ingest('one','a.jsonl',0,body)
        self.assertEqual(self.store.overview()['totals']['tokens'],230)
        self.assertEqual(len([e for e in self.store.detail(result['sessions'][0]['key']) if e['kind']=='user']),1)
    def test_counter_reset_and_partial_baseline(self):
        first={'input_tokens':1000,'output_tokens':100,'total_tokens':1100}
        last={'input_tokens':30,'output_tokens':10,'total_tokens':40}
        second={'input_tokens':50,'output_tokens':20,'total_tokens':70}
        body=encoded([record('session_meta',{'id':'partial'}),usage(first,last),usage(second,ts='2026-10-23T16:02:00Z')])
        self.store.ingest('x','a',0,body)
        result=self.store.overview();self.assertEqual(result['totals']['tokens'],110)
        self.assertTrue(result['sessions'][0]['extra']['usage_incomplete'])
    def test_patch_confirmed_only_on_output(self):
        rows=[record('session_meta',{'id':'patch'}),record('response_item',{'type':'custom_tool_call','name':'apply_patch','call_id':'p1','input':'*** Begin Patch\n*** Update File: a.py\n@@\n-a\n+b\n*** End Patch'})]
        body=encoded(rows);self.store.ingest('x','p',0,body)
        self.assertEqual(self.store.overview()['totals']['files'],0)
        more=encoded([record('response_item',{'type':'custom_tool_call_output','call_id':'p1','output':'Success. Updated the following files:\nM a.py'})])
        self.store.ingest('x','p',len(body),more)
        self.assertEqual(self.store.overview()['totals']['files'],1)
        self.assertEqual(self.store.files()[0]['detail']['status'],'applied')
    def test_exec_import_without_timestamp_is_stable(self):
        body=encoded([{'type':'thread.started','thread_id':'exec1'},{'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':4,'output_tokens':2}}])
        self.store.ingest('import','a',0,body);self.store.ingest('import','a',0,body)
        result=self.store.overview();self.assertEqual(result['totals']['tokens'],12)
        self.assertTrue(result['sessions'][0]['extra']['estimated_dates'])
    def test_missing_usage_and_failed_patch(self):
        body=encoded([record('session_meta',{'id':'fail'}),record('response_item',{'type':'custom_tool_call','name':'apply_patch','call_id':'f','input':'*** Update File: no.py\n@@\n-a\n+b'}),record('response_item',{'type':'custom_tool_call_output','call_id':'f','output':'Error: Failed to find expected lines'})])
        self.store.ingest('x','f',0,body);r=self.store.overview()
        self.assertEqual(r['totals']['unknown_usage'],1);self.assertEqual(r['totals']['files'],0)
        self.assertEqual(self.store.files()[0]['detail']['status'],'failed')
    def test_readonly_collector_and_cursor(self):
        folder=self.root/'sessions'/'2026';folder.mkdir(parents=True)
        raw=encoded([record('session_meta',{'id':'collect'})]);f=folder/'r.jsonl';f.write_bytes(raw)
        r=collect({'home':str(self.root),'days':3650});self.assertEqual(r['count'],1)
        self.assertEqual(base64.b64decode(r['chunks'][0]['data']),raw)
        r=collect({'home':str(self.root),'days':3650,'cursors':{str(f):len(raw)}});self.assertEqual(r['chunks'],[])
        self.assertEqual(f.read_bytes(),raw)
    def test_git_changes_include_staged_and_untracked(self):
        repo=self.root/'git project';repo.mkdir()
        def git(*args):return subprocess.run(['git',*args],cwd=repo,check=True,capture_output=True)
        git('init');git('config','user.name','Test');git('config','user.email','test@example.invalid')
        (repo/'x.txt').write_text('before\n');git('add','.');git('commit','-m','baseline')
        (repo/'x.txt').write_text('after\n');git('add','x.txt');(repo/'new.txt').write_text('new\n')
        r=collect({'mode':'git','cwd':str(repo)})
        self.assertIn('+after',r['diff']);self.assertIn('?? new.txt',r['status'])

class SSHTest(unittest.TestCase):
    def test_real_ssh_transport_trust_auth_and_remote_scan(self):
        import paramiko
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'remote'/'sessions').mkdir(parents=True)
            (root/'remote'/'sessions'/'r.jsonl').write_bytes(encoded([record('session_meta',{'id':'remote-test','cwd':str(root)}),record('event_msg',{'type':'user_message','message':'SSH real integration'})]))
            listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(5);listener.settimeout(1)
            port=listener.getsockname()[1];key=paramiko.RSAKey.generate(2048);closed=threading.Event();transports=[]
            class Server(paramiko.ServerInterface):
                def check_auth_password(self,username,password):return paramiko.AUTH_SUCCESSFUL if username=='tester' and password=='test-only' else paramiko.AUTH_FAILED
                def get_allowed_auths(self,username):return 'password'
                def check_channel_request(self,kind,chanid):return paramiko.OPEN_SUCCEEDED if kind=='session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
                def check_channel_exec_request(self,channel,command):
                    def execute():
                        incoming=b''
                        while True:
                            chunk=channel.recv(65536)
                            if not chunk:break
                            incoming+=chunk
                        request=json.loads(incoming)
                        # Exercise the exact collector source transmitted in the SSH command.
                        import shlex
                        args=shlex.split(command.decode());code=args[2]
                        p=subprocess.run([sys.executable,'-c',code],input=json.dumps(request).encode(),capture_output=True)
                        channel.sendall(p.stdout)
                        if p.stderr:channel.sendall_stderr(p.stderr)
                        channel.send_exit_status(p.returncode);channel.close()
                    threading.Thread(target=execute,daemon=True).start();return True
            def accept():
                while not closed.is_set():
                    try:conn,_=listener.accept()
                    except socket.timeout:continue
                    except OSError:break
                    transport=paramiko.Transport(conn);transports.append(transport)
                    try:transport.add_server_key(key);transport.start_server(server=Server())
                    except Exception:transport.close()
            threading.Thread(target=accept,daemon=True).start()
            app=Monitor(root/'local-data')
            config={'host':'127.0.0.1','port':port,'user':'tester','password':'test-only','home':str(root/'remote'),'days':3650}
            try:
                with self.assertRaises(HostTrustRequired) as issue:app.connect(config)
                config['trust']=issue.exception.info['fingerprint']
                app.connect(config);app.sync()
                self.assertEqual(app.store.overview()['totals']['sessions'],1)
                self.assertEqual(app.state['status'],'connected')
                app.sync();self.assertEqual(app.store.overview()['totals']['sessions'],1)
                self.assertNotIn('test-only',(root/'local-data'/'connection.json').read_text())
                self.assertEqual(app.store.overview()['sessions'][0]['title'],'SSH real integration')
            finally:
                app.stop.set()
                if app.client:app.client.close()
                closed.set();listener.close()
                for t in transports:t.close()
                time.sleep(.1);app.store.db.close()

class HTTPTest(unittest.TestCase):
    def test_auth_origin_and_demo_isolation(self):
        import urllib.request
        import urllib.error
        with tempfile.TemporaryDirectory() as tmp:
            app=Monitor(tmp);server=serve(app,0,'test-local-token')
            threading.Thread(target=server.serve_forever,daemon=True).start()
            root='http://127.0.0.1:'+str(server.server_port)
            def request(path,body=None,token='test-local-token',origin=None):
                headers={'Authorization':'Bearer '+token}
                if body is not None:headers['Content-Type']='application/json'
                if origin:headers['Origin']=origin
                req=urllib.request.Request(root+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
                with urllib.request.urlopen(req) as response:return response.status,response.read()
            try:
                self.assertEqual(request('/')[0],200)
                with self.assertRaises(urllib.error.HTTPError) as err:request('/api/overview',token='wrong')
                self.assertEqual(err.exception.code,401)
                with self.assertRaises(urllib.error.HTTPError) as err:request('/api/demo',{},origin='https://example.invalid')
                self.assertEqual(err.exception.code,403)
                request('/api/demo',{})
                limit=time.monotonic()+5
                while app.state['busy'] and time.monotonic()<limit:time.sleep(.03)
                result=json.loads(request('/api/overview')[1])
                self.assertGreater(result['totals']['tokens'],0)
                self.assertEqual(app.store.overview()['totals']['sessions'],0)
                self.assertNotIn('password',request('/api/state')[1].decode())
            finally:
                app.stop.set();server.shutdown();server.server_close();app.store.db.close()

if __name__=='__main__':unittest.main(verbosity=2)
