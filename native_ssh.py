"""Persistent system-SSH bridge. No Tailscale CLI, login detection, or account changes."""
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from tailscale_auth import auth_url,AuthCancelled

TAILSCALE_LOGIN='https://login.tailscale.com/login'

class NativeSSH:
    def __init__(self,host,user,port,code,known_files,on_url,cancel,stop):
        import shlex
        executable=shutil.which('ssh')
        if not executable:
            executable=str(Path(os.environ.get('WINDIR',r'C:\Windows'))/'System32'/'OpenSSH'/'ssh.exe')
        if not Path(executable).is_file():raise ValueError('未找到系统 OpenSSH 客户端，请安装 Windows 的 OpenSSH 客户端功能')
        known=' '.join('"'+str(Path(p)).replace('\\','/')+'"' for p in known_files if Path(p).is_file())
        options=['-T','-o','BatchMode=yes','-o','PreferredAuthentications=none','-o','PubkeyAuthentication=no',
            '-o','PasswordAuthentication=no','-o','KbdInteractiveAuthentication=no','-o','StrictHostKeyChecking=yes',
            '-o','ConnectTimeout=8','-o','ConnectionAttempts=1','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=2']
        if known:options+=['-o','UserKnownHostsFile='+known]
        command=[executable,*options,'-p',str(port),'-l',user,'--',host,'python3 -u -c '+shlex.quote(code)+' --bridge']
        flags={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
        self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**flags)
        self.frames=queue.Queue();self.errors=[];self.cancel=cancel;self.stop=stop;self.closed=threading.Event()
        self.auth_seen=False;self.on_url=on_url;self.rpc_lock=threading.Lock();self.ready=None
        self.readers=[threading.Thread(target=self._stdout,daemon=True),threading.Thread(target=self._stderr,daemon=True)]
        for reader in self.readers:reader.start()
        try:
            self.ready=self._receive(12,allow_auth=True)
            if self.ready.get('type')!='ready':raise ValueError('服务器未返回采集程序就绪信号')
        except Exception:
            self.close();raise

    def _stdout(self):
        try:
            for line in iter(self.process.stdout.readline,b''):
                if len(line)>40*1024*1024:
                    self.frames.put({'bridge_error':'服务器响应超过读取限制'});return
                try:self.frames.put(json.loads(line))
                except (ValueError,UnicodeDecodeError):
                    # A login shell may print a banner before executing Python.
                    continue
        finally:
            self.frames.put({'bridge_error':'SSH 连接已结束'})
            self.process.stdout.close()

    def _stderr(self):
        seen=set()
        try:
            for line in iter(self.process.stderr.readline,b''):
                text=line.decode('utf-8','replace').strip()
                url=auth_url(text)
                if url and url not in seen:
                    self.auth_seen=True;seen.add(url);self.on_url(url,'ssh')
                self.errors.append(re.sub(r'https://login\.tailscale\.com/a/[^\s]+','[认证链接]',text)[:1000])
                self.errors=self.errors[-12:]
        finally:self.process.stderr.close()

    def _receive(self,timeout,allow_auth=False):
        started=time.monotonic()
        while True:
            if self.cancel.is_set() or self.stop.is_set():raise AuthCancelled('已取消本次连接')
            limit=300 if allow_auth and self.auth_seen else timeout
            if time.monotonic()-started>limit:
                stage='浏览器认证' if self.auth_seen and allow_auth else '服务器连接 / 采集程序启动' if allow_auth else '服务器记录读取'
                raise TimeoutError(stage+'超时。'+('请完成网页验证后重试。' if self.auth_seen and allow_auth else '已停止本次请求，可重新连接。'))
            try:frame=self.frames.get(timeout=.1)
            except queue.Empty:continue
            if frame.get('bridge_error'):
                error='\n'.join(self.errors[-6:])
                raise ConnectionError(frame['bridge_error']+('：'+error if error else ''))
            return frame

    def request(self,request,timeout=45):
        with self.rpc_lock:
            if not self.is_active():raise ConnectionError('SSH 连接已断开，请重新连接')
            self.process.stdin.write((json.dumps(request,ensure_ascii=True)+'\n').encode('utf-8'))
            self.process.stdin.flush()
            try:result=self._receive(timeout)
            except Exception:
                self.close();raise
            if result.get('error'):raise ValueError(result['error'])
            return result

    def is_active(self):return not self.closed.is_set() and self.process.poll() is None
    def get_transport(self):return self

    def close(self):
        if self.closed.is_set():return
        self.closed.set()
        try:self.process.stdin.close()
        except (OSError,ValueError):pass
        try:self.process.wait(timeout=.4)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:self.process.kill()
        for reader in self.readers:
            if reader is not threading.current_thread():reader.join(timeout=.4)
