import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch
from tailscale_auth import auth_url
from main import Monitor,serve
from native_ssh import TAILSCALE_LOGIN

TEST_URL='https://login.tailscale.com/a/TEST_ONLY_12345'

class URLTests(unittest.TestCase):
    def test_only_real_vendor_auth_paths(self):
        self.assertEqual(auth_url('# To authenticate, visit: '+TEST_URL),TEST_URL)
        for value in ['http://login.tailscale.com/a/abcdef','https://login.tailscale.com.evil.invalid/a/abcdef','https://login.tailscale.com@evil.invalid/a/abcdef','https://evil.invalid/a/abcdef','https://login.tailscale.com:999/a/abcdef','https://login.tailscale.com/admin','file:///C:/test']:
            self.assertIsNone(auth_url(value))

class AuthRouteTest(unittest.TestCase):
    def test_direct_login_never_queries_or_signs_in_the_local_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('subprocess.run') as run:
                app=Monitor(tmp)
                self.assertFalse(run.called)
            opened=[];app.url_opener=lambda url:opened.append(url) or True
            server=serve(app,0,'test-token');threading.Thread(target=server.serve_forever,daemon=True).start()
            def post(action,body):
                request=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/api/'+action,data=json.dumps(body).encode(),headers={'Authorization':'Bearer test-token','Content-Type':'application/json'})
                with urllib.request.urlopen(request) as response:return response.status
            try:
                with patch('subprocess.run') as run,patch('subprocess.Popen') as popen:
                    self.assertEqual(post('tailscale-login',{}),200)
                    self.assertEqual(opened,[TAILSCALE_LOGIN]);self.assertFalse(run.called);self.assertFalse(popen.called)
                app.state.update(auth={'url':TEST_URL,'kind':'ssh'},busy=True)
                post('tailscale-open',{'url':'https://evil.invalid'})
                self.assertEqual(opened[-1],TEST_URL)
                post('cancel-auth',{});self.assertTrue(app.cancel_connect.is_set());self.assertIsNone(app.state['auth'])
            finally:
                app.stop.set();server.shutdown();server.server_close();app.store.db.close()

if __name__=='__main__':unittest.main(verbosity=2)
