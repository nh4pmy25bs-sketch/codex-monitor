import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import shlex
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from urllib.parse import urlparse,parse_qs

from core import Store,VERSION,now,digest
from collector import collect
from tailscale_auth import AuthCancelled,auth_url
from native_ssh import NativeSSH,TAILSCALE_LOGIN

ROOT=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))
BASE=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent

def demo():
    import datetime as dt
    today=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    daily=[]
    for i,n in enumerate([42000,78000,65000,124000,91000,165000,108400]):
        daily.append(dict(date=str(today-dt.timedelta(days=6-i)),input=n-6000,cached=int(n*.64),output=6000,total=n,estimated=0))
    sessions=[]
    for i,(title,cwd,status,tokens,files) in enumerate([
        ('检查登录页并修复表单校验','/srv/projects/atlas','completed',108400,3),
        ('梳理数据接口与测试覆盖','/srv/projects/atlas','unknown',309000,2),
        ('调整文档结构与部署说明','/srv/projects/docs','completed',256000,2)]):
        sessions.append(dict(key='demo-'+str(i),sid='example-session-'+str(i+1),source='演示服务器',title=title,cwd=cwd,status=status,tokens=tokens,files=files,usage_known=True,model='示例模型',created=now(),updated=now(),extra={'cli_version':'演示','log_path':'示例记录'}))
    return dict(sessions=sessions,daily=daily,models={'示例模型':673400},today=str(today),malformed=0,totals=dict(tokens=sum(x['total'] for x in daily),today=108400,sessions=3,files=7,unknown_usage=0))

def demo_events(key):
    return [dict(id='demo1',kind='user',label='你的指令',body='检查登录页的输入校验，修复后运行相关测试。',ts=now(),detail={}),
        dict(id='demo2',kind='assistant',label='Codex',body='我会检查表单与校验逻辑，再运行相关测试。',ts=now(),detail={}),
        dict(id='demo3',kind='command',label='执行命令',body='npm test -- login',ts=now(),detail={'status':'completed'}),
        dict(id='demo4',kind='output',label='命令结果',body='3 tests passed · 示例输出',ts=now(),detail={'status':'applied'}),
        dict(id='demo5',kind='file',label='src/login.ts',body='*** Update File: src/login.ts\n@@\n- return email.length > 0;\n+ return /^[^@]+@[^@]+\\.[^@]+$/.test(email);',ts=now(),detail={'operation':'update','status':'applied'}),
        dict(id='demo6',kind='assistant',label='Codex',body='已更新输入校验，相关测试通过。以上内容均为演示，不来自你的服务器。',ts=now(),detail={})]

class HostTrustRequired(Exception):
    def __init__(self,host,key):
        self.info=dict(host=host,type=key.get_name(),fingerprint='SHA256:'+base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('='))

class Monitor:
    def __init__(self,data):
        self.data=Path(data);self.data.mkdir(parents=True,exist_ok=True)
        self.store=Store(self.data/'monitor.db')
        self.client=None;self.config=None;self.wake=threading.Event();self.stop=threading.Event()
        self.lock=threading.RLock();self.state=dict(mode='idle',status='idle',message='连接服务器后开始监控',last_sync=None,hostkey=None,coverage=None,busy=False,auth=None,tailscale_hint=False,connected=False,phase='idle')
        self.action=None;self.git_result=None
        self.resolved_home=None
        self.cancel_connect=threading.Event();self.connecting_client=None;self.url_opener=webbrowser.open
        self.notifier=None;self.notice_cutoff=None;self.phase_started=time.monotonic();self.first_sync=True
        try:self.settings=json.loads((self.data/'settings.json').read_text('utf-8'))
        except (OSError,ValueError):self.settings={}
        self.settings.setdefault('notifications_enabled',True)
        self._next=time.monotonic()+3600
        threading.Thread(target=self.loop,daemon=True).start()

    def profile(self):
        try:
            return json.loads((self.data/'connection.json').read_text('utf-8'))
        except (OSError,ValueError):
            return {}

    def public(self):
        with self.lock:
            return dict(self.state,version=VERSION,data_dir=str(self.data),profile=self.profile(),git=self.git_result,phase_seconds=round(time.monotonic()-self.phase_started,1),notifications_enabled=self.settings['notifications_enabled'],notifications_ready=bool(self.notifier and self.notifier.added))

    def auth_required(self,url,kind):
        checked=auth_url(url)
        if not checked or self.stop.is_set() or self.cancel_connect.is_set():return
        previous=self.state.get('auth') or {}
        self.phase_started=time.monotonic();self.state.update(phase='auth',status='auth_wait',message='请在浏览器完成 Tailscale '+('服务器验证，完成后自动继续连接' if kind=='ssh' else '登录，然后连接服务器'),
            auth={'url':checked,'kind':kind,'opened':False})
        if previous.get('url')==checked:return
        try:self.state['auth']['opened']=bool(self.url_opener(checked))
        except Exception:pass

    def phase(self,name,message,**updates):
        self.phase_started=time.monotonic();self.state.update(phase=name,message=message,**updates)

    def set_notifications(self,enabled):
        if not isinstance(enabled,bool):raise ValueError('通知设置格式错误')
        self.settings['notifications_enabled']=enabled
        (self.data/'settings.json').write_text(json.dumps(self.settings),'utf-8')

    def test_notification(self):
        if not self.notifier:raise ValueError('请从桌面快捷方式启动软件后测试桌面通知')
        if not self.notifier.notify('Codex 监控台 · 测试通知','连接服务器后，Codex 每轮对话完成时会在本机提醒。'):
            raise ValueError('Windows 未接受通知请求，请检查系统通知设置')

    def dispatch_completions(self,recent=None):
        alerts=[]
        for item in recent or []:
            alert=self.store.queue_completion(self.source(),item['sid'],item['ts'],item.get('cwd',''))
            if alert:alerts.append(alert)
        alerts+=self.store.claim_completions(self.source(),self.notice_cutoff)
        if not alerts or not self.settings['notifications_enabled'] or not self.notifier:return
        if len(alerts)==1:self.notifier.notify(alerts[0]['title'],alerts[0]['body'])
        else:self.notifier.notify('Codex 有 '+str(len(alerts))+' 轮对话已完成','打开监控台的通知记录查看对应项目。')

    def open_auth(self):
        checked=auth_url((self.state.get('auth') or {}).get('url'))
        if not checked:raise ValueError('当前没有待完成的认证链接，请先点击“浏览器认证并连接”')
        self.url_opener(checked)

    def cancel_auth(self):
        self.cancel_connect.set()
        client=self.connecting_client
        if client:client.close()
        self.state.update(auth=None,message='已取消本次认证')
        if not self.state['busy']:self.state['status']='idle'

    def submit(self,action,params):
        with self.lock:
            if self.state['busy'] or self.action:
                raise ValueError('正在同步，请稍后再试')
            self.action=(action,params);self.state['busy']=True
        self.wake.set()

    def connect(self,c):
        import paramiko
        host=str(c.get('host','')).strip();user=str(c.get('user','')).strip()
        if not host or not user or len(host)>253 or any(ch in host for ch in '\r\n\x00'):
            raise ValueError('请填写服务器地址和 SSH 用户名')
        port=int(c.get('port',22))
        if not 1<=port<=65535:
            raise ValueError('端口必须在 1 到 65535 之间')
        tail_mode=c.get('auth_mode')=='tailscale'
        if tail_mode and port!=22:raise ValueError('Tailscale SSH 使用 22 端口，请将 SSH 端口改为 22')
        safe={k:c.get(k) for k in ('host','port','user','key','home','days','interval','auth_mode')}
        safe.update(host=host,user=user,port=port)
        (self.data/'connection.json').write_text(json.dumps(safe,ensure_ascii=False,indent=2),'utf-8')
        self.state.update(auth=None,tailscale_hint=False,status='syncing',connected=False)
        self.phase('connecting','正在连接服务器与核对身份……')
        client=paramiko.SSHClient()
        self.connecting_client=client
        client.load_system_host_keys()
        standard=Path.home()/'.ssh'/'known_hosts'
        if standard.is_file():
            client.load_host_keys(str(standard))
        known=self.data/'known_hosts'
        if known.is_file():
            client.load_host_keys(str(known))
        class Policy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(policy,ssh,hostname,key):
                issue=HostTrustRequired(hostname,key)
                if c.get('trust')!=issue.info['fingerprint']:
                    raise issue
                ssh.get_host_keys().add(hostname,key.get_name(),key)
                ssh.save_host_keys(str(known))
        client.set_missing_host_key_policy(Policy())
        password=str(c.get('password') or '') or None
        key=str(c.get('key') or '').strip()
        try:
            if tail_mode:
                keyname=host if port==22 else '['+host+']:'+str(port)
                if not client.get_host_keys().lookup(keyname):
                    # Unknown hosts still require explicit fingerprint verification.
                    probe=paramiko.Transport(socket.create_connection((host,port),timeout=6))
                    try:probe.start_client(timeout=6);server_key=probe.get_remote_server_key()
                    finally:probe.close()
                    Policy().missing_host_key(client,keyname,server_key)
                client.close()
                client=NativeSSH(host,user,port,(ROOT/'collector.py').read_text('utf-8'),
                    [known,standard],self.auth_required,self.cancel_connect,self.stop)
            else:
                client.connect(host,port=port,username=user,password=password,passphrase=password,
                    key_filename=os.path.expanduser(key) if key else None,look_for_keys=True,allow_agent=True,
                    timeout=8,banner_timeout=8,auth_timeout=15)
                client.get_transport().set_keepalive(25)
        except Exception:
            client.close();raise
        finally:self.connecting_client=None
        if self.client:self.client.close()
        self.client=client;self.config=dict(c,port=port,host=host,user=user)
        self.phase('discovering','SSH 已连接，正在定位 Codex 记录……',connected=True,mode='ssh',hostkey=None,auth=None,tailscale_hint=False)
        info=self.remote({'mode':'info','home':c.get('home')})
        self.resolved_home=info['home']
        if self.notice_cutoff is None:self.notice_cutoff=info['server_time']
        self.first_sync=True
        self.phase('connected','服务器已连接；历史记录将在后台补齐。',status='connected')

    def source(self):
        if self.state['mode']=='local':
            return 'local:'+str(Path(self.config['home']).resolve())
        c=self.config or {}
        return 'ssh:'+str(c.get('user',''))+'@'+str(c.get('host',''))+':'+str(c.get('port',22))+':'+str(self.resolved_home or c.get('home') or 'default')

    def remote(self,request):
        if isinstance(self.client,NativeSSH):return self.client.request(request,timeout=35)
        code=(ROOT/'collector.py').read_text('utf-8')
        stdin,stdout,stderr=self.client.exec_command('python3 -c '+shlex.quote(code),timeout=35)
        stdin.write(json.dumps(request,ensure_ascii=True)+'\n');stdin.flush();stdin.channel.shutdown_write()
        raw=stdout.read(40*1024*1024)
        error=stderr.read(8000).decode('utf-8','replace')
        exitcode=stdout.channel.recv_exit_status()
        if exitcode:
            raise ValueError('服务器读取失败。请确认已安装 Python 3。'+error[:1200])
        try:
            result=json.loads(raw)
        except ValueError:
            raise ValueError('服务器返回的记录无法解析，请确认 Python 3 可用')
        if result.get('error'):
            raise ValueError(result['error'])
        return result

    def sync(self):
        mode=self.state['mode']
        if mode not in ('ssh','local'):
            return
        self.phase('reading','服务器已连接，正在读取最新记录……',status='syncing')
        request=dict(notify_since=self.notice_cutoff if mode=='ssh' else None,max_bytes=512*1024 if self.first_sync else 2*1024*1024,mode='scan',home=self.config.get('home'),days=int(self.config.get('days') or 30),cursors=self.store.cursors(self.source()))
        result=self.remote(request) if mode=='ssh' else collect(request)
        if mode=='ssh':self.resolved_home=result.get('home')
        chunks=result.get('chunks',[])
        self.state['sync_bytes']=result.get('bytes_read',0)
        for index,chunk in enumerate(chunks):
            if self.cancel_connect.is_set():raise AuthCancelled('已取消同步')
            self.phase('importing','正在整理记录 '+str(index+1)+' / '+str(len(chunks))+'；服务器连接正常。')
            self.store.ingest(self.source(),chunk['path'],chunk['offset'],base64.b64decode(chunk['data']),chunk['mtime'])
        self.first_sync=False
        if mode=='ssh':self.dispatch_completions(result.get('recent_completions'))
        coverage={k:result.get(k) for k in ('home','count','limited','remaining','days','exists','errors')}
        message='同步完成' if result.get('count') else ('未找到会话记录，请检查 Codex 数据目录和日期范围' if result.get('exists') else '未找到 sessions 目录，请填写服务器实际的 CODEX_HOME')
        self.phase('ready',message,status='connected',last_sync=now(),coverage=coverage)

    def loop(self):
        while not self.stop.is_set():
            self.wake.wait(.5);self.wake.clear()
            with self.lock:
                action=self.action;self.action=None
            try:
                if action:
                    name,p=action
                    if name=='connect':
                        self.cancel_connect.clear();self.notice_cutoff=None;self.resolved_home=None;self.connect(p);self.sync()
                    elif name=='local':
                        path=Path(str(p.get('home',''))).expanduser()
                        if not (path/'sessions').is_dir():
                            raise ValueError('请选择包含 sessions 子文件夹的 Codex 数据目录')
                        if self.client:
                            self.client.close();self.client=None
                        self.config=dict(p,home=str(path.resolve()))
                        self.state.update(mode='local',status='syncing',hostkey=None);self.sync()
                    elif name=='demo':
                        if self.client:
                            self.client.close();self.client=None
                        self.config=None;self.state.update(mode='demo',status='demo',message='演示模式 · 全部为示例数据',coverage=None,hostkey=None)
                    elif name=='disconnect':
                        if self.client:
                            self.client.close();self.client=None
                        self.config=None;self.state.update(mode='idle',status='idle',message='已断开，已保存的历史仍可查看',hostkey=None,auth=None,tailscale_hint=False,connected=False)
                    elif name=='sync':
                        if self.state['mode']=='ssh' and (not self.client or not self.client.get_transport() or not self.client.get_transport().is_active()):
                            self.connect(self.config)
                        self.sync()
                    elif name=='git':
                        self.git_result={'loading':True}
                        # CWD must be a known session path, not an arbitrary browser-supplied shell command.
                        paths={s['cwd'] for s in self.store.overview(self.source())['sessions']}
                        if p.get('cwd') not in paths:
                            raise ValueError('请选择已采集会话中的项目目录')
                        request={'mode':'git','cwd':p['cwd']}
                        self.git_result=self.remote(request) if self.state['mode']=='ssh' else collect(request)
                    elif name=='import':
                        name=Path(str(p.get('name','导入记录.jsonl'))).name
                        body=str(p.get('content',''))
                        if not body.strip():
                            raise ValueError('记录文件为空')
                        source='import:'+digest(body)
                        count=self.store.ingest(source,name,0,(body.rstrip()+'\n').encode('utf-8'))
                        if self.client:
                            self.client.close();self.client=None
                        self.config=None;self.state.update(mode='idle',status='idle',message='已导入 '+str(count)+' 条记录',hostkey=None)
                    self.state['busy']=False
                    self._next=time.monotonic()+(1 if (self.state.get('coverage') or {}).get('remaining') else max(3,int((self.config or {}).get('interval') or 5)))
                elif self.state['mode'] in ('ssh','local') and time.monotonic()>=getattr(self,'_next',0):
                    self.state['busy']=True
                    if self.state['mode']=='ssh' and (not self.client or not self.client.get_transport() or not self.client.get_transport().is_active()):
                        self.connect(self.config)
                    self.sync();self.state['busy']=False
                    self._next=time.monotonic()+(1 if (self.state.get('coverage') or {}).get('remaining') else max(3,int(self.config.get('interval') or 5)))
            except HostTrustRequired as e:
                self.state.update(busy=False,hostkey=e.info,status='verify',message='首次连接：请核对服务器指纹')
                self._next=time.monotonic()+60
            except AuthCancelled:
                self.state.update(busy=False,status='idle',auth=None,message='已取消本次认证')
                self._next=time.monotonic()+3600
            except Exception as e:
                message=str(e) or type(e).__name__
                if type(e).__name__=='AuthenticationException':
                    if not (action and action[0]=='connect' and action[1].get('auth_mode')=='tailscale'):
                        message='SSH 认证失败，请检查用户名、密码或密钥。官方 Codex 账号和 SSH 登录是两回事。'
                if 'tailscale' in getattr(e,'allowed_types',[]):
                    self.state['tailscale_hint']=True
                    message='服务器使用 Tailscale SSH。请选择 Tailscale 浏览器认证，再点击“浏览器认证并连接”，无需填写 SSH 密码或添加 +password。'
                if type(e).__name__=='BadHostKeyException':
                    message='服务器指纹与已保存记录不同，连接已停止。请核实服务器身份。'
                    self.state['mode']='idle';self.config=None
                if self.cancel_connect.is_set():
                    self.state.update(busy=False,status='idle',auth=None,message='已取消本次认证')
                else:self.state.update(busy=False,status='error',message=message[:2000],connected=bool(self.client and self.client.get_transport() and self.client.get_transport().is_active()))
                if action and action[0]=='git':
                    self.git_result={'error':message[:2000]}
                self._next=time.monotonic()+30

    def overview(self,source=None):
        if self.state['mode']=='demo':
            return demo()
        return self.store.overview(source)

def serve(app,port,token):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def send_headers(self,status,kind='application/json; charset=utf-8'):
            self.send_response(status);self.send_header('Content-Type',kind)
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
        def json(self,value,status=200):
            self.send_headers(status);self.wfile.write(json.dumps(value,ensure_ascii=False).encode('utf-8'))
        def allowed(self,auth=True):
            if self.headers.get('Host') not in ('127.0.0.1:'+str(self.server.server_port),'localhost:'+str(self.server.server_port)):
                self.json({'error':'无效访问来源'},403);return False
            if auth and not secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):
                self.json({'error':'请从桌面软件重新打开界面'},401);return False
            origin=self.headers.get('Origin')
            if origin and origin not in ('http://127.0.0.1:'+str(self.server.server_port),'http://localhost:'+str(self.server.server_port)):
                self.json({'error':'无效访问来源'},403);return False
            return True
        def do_GET(self):
            parsed=urlparse(self.path)
            if parsed.path.startswith('/api/'):
                if not self.allowed():return
                if parsed.path=='/api/state':self.json(app.public())
                elif parsed.path=='/api/notifications':self.json(app.store.alert_history())
                elif parsed.path=='/api/overview':self.json(app.overview(parse_qs(parsed.query).get('source',[None])[0]))
                elif parsed.path=='/api/files':
                    self.json([dict(e,session='demo-0',title='演示会话',cwd='/srv/projects/atlas',source='演示服务器') for e in demo_events('demo-0') if e['kind']=='file'] if app.state['mode']=='demo' else app.store.files(parse_qs(parsed.query).get('source',[None])[0]))
                elif parsed.path=='/api/events':
                    key=parse_qs(parsed.query).get('session',[''])[0]
                    self.json(demo_events(key) if app.state['mode']=='demo' else app.store.detail(key))
                else:self.json({'error':'未找到'},404)
                return
            if not self.allowed(False):return
            files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
            if parsed.path not in files:
                self.json({'error':'未找到'},404);return
            filename,kind=files[parsed.path];self.send_headers(200,kind);self.wfile.write((ROOT/'web'/filename).read_bytes())
        def do_POST(self):
            if not self.allowed():return
            try:
                if not self.headers.get('Content-Type','').startswith('application/json'):
                    raise ValueError('请求格式错误')
                size=int(self.headers.get('Content-Length',0))
                if not 0<size<=48*1024*1024:raise ValueError('文件过大，请选择小于 40 MB 的记录')
                body=json.loads(self.rfile.read(size))
                action=self.path.removeprefix('/api/')
                if action=='quit':
                    app.stop.set();threading.Thread(target=self.server.shutdown,daemon=True).start();self.json({'ok':True});return
                if action=='tailscale-open':
                    app.open_auth();self.json({'ok':True});return
                if action=='tailscale-login':
                    app.url_opener(TAILSCALE_LOGIN);self.json({'ok':True});return
                if action=='disconnect' and app.state['busy']:
                    app.cancel_auth()
                    if app.client:app.client.close()
                    app.client=None;app.config=None;app.state.update(mode='idle',connected=False,status='idle',busy=False,message='已断开，历史记录已保留')
                    self.json({'ok':True});return
                if action=='notifications-settings':
                    app.set_notifications(body.get('enabled'));self.json({'ok':True});return
                if action=='notifications-test':
                    app.test_notification();self.json({'ok':True});return
                if action=='cancel-auth':
                    app.cancel_auth();self.json({'ok':True});return
                if action not in ('connect','local','demo','disconnect','sync','git','import'):raise ValueError('未知操作')
                if not isinstance(body,dict):raise ValueError('请求格式错误')
                app.submit(action,body);self.json({'ok':True})
            except Exception as e:self.json({'error':str(e)},400)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads=True
    return server

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--headless',action='store_true');parser.add_argument('--data-dir');parser.add_argument('--port',type=int,default=0);parser.add_argument('--demo',action='store_true');parser.add_argument('--token-file');parser.add_argument('--self-test',action='store_true')
    args=parser.parse_args()
    if args.self_test:
        import tkinter as tk
        window=tk.Tk();window.withdraw();window.iconbitmap(str(ROOT/'app.ico'));window.update();window.destroy()
        for path in ('web/index.html','web/style.css','web/app.js','collector.py'):
            assert (ROOT/path).is_file(),path
        return
    app=Monitor(args.data_dir or BASE/'data')
    token=secrets.token_urlsafe(32);server=serve(app,args.port,token)
    url='http://127.0.0.1:'+str(server.server_port)+'/#'+token
    if args.token_file:Path(args.token_file).write_text(url,'utf-8')
    if args.demo:app.submit('demo',{})
    threading.Thread(target=server.serve_forever,daemon=True).start()
    def close():
        app.stop.set()
        app.cancel_auth()
        if app.client:app.client.close()
        if app.notifier:app.notifier.close()
        server.shutdown();app.store.checkpoint()
    if args.headless:
        print('READY '+url,flush=True)
        try:
            while not app.stop.wait(1):pass
        except KeyboardInterrupt:pass
        close();return
    import tkinter as tk
    from tkinter import ttk
    window=tk.Tk();window.title('Codex 监控台');window.geometry('440x285');window.resizable(False,False)
    window.configure(bg='#f3f6f5')
    try:window.iconbitmap(str(ROOT/'app.ico'))
    except Exception:pass
    tk.Label(window,text='Codex 监控台',font=('Microsoft YaHei UI',19,'bold'),bg='#f3f6f5',fg='#152c2a').pack(pady=(27,8))
    tk.Label(window,text='本机服务正在运行\n点 × 会最小化，完成通知继续有效。',font=('Microsoft YaHei UI',10),bg='#f3f6f5',fg='#627671',justify='center').pack(pady=6)
    ttk.Button(window,text='打开监控界面',command=lambda:webbrowser.open(url),width=29).pack(pady=(18,7))
    def quit_window():
        if app.notifier:app.notifier.close()
        window.destroy()
    ttk.Button(window,text='退出软件并停止同步',command=quit_window,width=29).pack(pady=3)
    tk.Label(window,text='v'+VERSION+' · 数据仅保存在本机 · 仅监听 127.0.0.1',font=('Microsoft YaHei UI',8),bg='#f3f6f5',fg='#81908b').pack(pady=15)
    def check():
        if app.stop.is_set():window.destroy()
        else:window.after(500,check)
    from notifications import DesktopNotifier
    window.update_idletasks()
    try:app.notifier=DesktopNotifier(window.winfo_id(),ROOT/'app.ico')
    except Exception:app.notifier=None
    window.protocol('WM_DELETE_WINDOW',lambda:window.iconify());window.after(500,check);webbrowser.open(url);window.mainloop()
    close()

if __name__=='__main__':
    try:main()
    except Exception as exc:
        if '--headless' in sys.argv:raise
        import tkinter.messagebox
        tkinter.messagebox.showerror('Codex 监控台','启动失败：'+str(exc))
