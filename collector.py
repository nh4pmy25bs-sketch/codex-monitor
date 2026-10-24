"""Read-only remote collector. Python 3 stdlib only; executes through SSH stdin.
No installation, background service, or remote temporary file required.
"""
import base64
import datetime
import json
import os
import subprocess
import sys
import time

def collect(request):
    mode=request.get('mode','scan')
    server_time=datetime.datetime.now(datetime.timezone.utc).isoformat()
    if mode=='info':
        home=request.get('home') or os.environ.get('CODEX_HOME') or '~/.codex'
        return {'home':os.path.abspath(os.path.expanduser(home)),'server_time':server_time}
    if mode=='git':
        cwd=request.get('cwd','')
        if not os.path.isdir(cwd):
            return {'error':'项目目录不存在或无法读取'}
        env=dict(os.environ,GIT_OPTIONAL_LOCKS='0',GIT_PAGER='cat',GIT_TERMINAL_PROMPT='0')
        def run(args):
            p=subprocess.run(['git','--no-optional-locks','-c','core.fsmonitor=false','-c','core.untrackedCache=false','-c','core.quotePath=false']+args,cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
            if p.returncode:
                raise ValueError(p.stderr.decode('utf-8','replace')[:2000])
            return p.stdout
        try:
            status=run(['status','--porcelain=v1','-uall']).decode('utf-8','replace')
            diff=run(['diff','--no-ext-diff','--no-textconv','HEAD','--']).decode('utf-8','replace')
        except ValueError:
            try:
                status=run(['status','--porcelain=v1','-uall']).decode('utf-8','replace')
                diff=run(['diff','--no-ext-diff','--no-textconv','--']).decode('utf-8','replace')+'\n'+run(['diff','--cached','--no-ext-diff','--no-textconv','--']).decode('utf-8','replace')
            except Exception as e:
                return {'error':'无法读取 Git 差异：'+str(e)}
        return {'status':status[:100000],'diff':diff[:1000000],'truncated':len(diff)>1000000,'note':'当前工作目录相对 Git 基线的差异，不代表全部由此会话产生。未跟踪文件只列文件名。'}
    home=request.get('home') or os.environ.get('CODEX_HOME') or '~/.codex'
    home=os.path.abspath(os.path.expanduser(home))
    days=max(1,min(3650,int(request.get('days',30))))
    cutoff=time.time()-days*86400
    found=[]; errors=[]
    for sub in ('sessions','archived_sessions'):
        root=os.path.join(home,sub)
        if not os.path.isdir(root):
            continue
        for folder,dirs,names in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if not os.path.islink(os.path.join(folder,d))]
            for name in names:
                if not name.endswith('.jsonl'):
                    continue
                path=os.path.join(folder,name)
                try:
                    if os.path.islink(path):
                        continue
                    st=os.stat(path)
                    if st.st_mtime>=cutoff:
                        found.append((st.st_mtime,path,st.st_size))
                except OSError:
                    errors.append('部分文件无读取权限')
    found.sort(reverse=True)
    completions=[]
    if request.get('notify_since'):
        since=datetime.datetime.fromisoformat(request['notify_since'].replace('Z','+00:00'))
        for mtime,path,size in found[:2000]:
            if mtime<since.timestamp():continue
            try:
                with open(path,'rb') as f:
                    meta=json.loads(f.readline(1024*1024))
                    payload=meta.get('payload') or {}
                    sid=payload.get('id')
                    if not sid:continue
                    start=max(0,size-512*1024);f.seek(start);lines=f.read().splitlines()
                    if start and lines:lines=lines[1:]
                for line in lines:
                    try:r=json.loads(line)
                    except (ValueError,UnicodeDecodeError):continue
                    p=r.get('payload') or {}
                    complete=(r.get('type')=='event_msg' and p.get('type') in ('task_complete','task_completed','turn_complete')) or r.get('type')=='turn.completed'
                    if not complete or not r.get('timestamp'):continue
                    ts=datetime.datetime.fromisoformat(r['timestamp'].replace('Z','+00:00'))
                    if ts>=since:completions.append({'sid':sid,'ts':r['timestamp'],'cwd':payload.get('cwd','')})
            except (OSError,ValueError,TypeError):continue
    cursors=request.get('cursors',{})
    chunks=[]; budget=max(65536,min(8*1024*1024,int(request.get('max_bytes',2*1024*1024)))); remaining=0
    for mtime,path,size in found[:2000]:
        offset=int(cursors.get(path,0))
        if size<offset:
            offset=0
        if size==offset:
            continue
        if budget<=0:
            remaining+=1;continue
        try:
            with open(path,'rb') as f:
                f.seek(offset);raw=f.read(min(budget,1024*1024))
            budget-=len(raw)
            if offset+len(raw)<size:
                remaining+=1
            chunks.append({'path':path,'offset':offset,'data':base64.b64encode(raw).decode('ascii'),'mtime':datetime.datetime.fromtimestamp(mtime,datetime.timezone.utc).isoformat()})
        except OSError:
            errors.append('部分记录无法读取')
    return {'home':home,'count':len(found),'limited':len(found)>2000,'remaining':remaining,'chunks':chunks,'errors':list(set(errors)),'days':days,'exists':os.path.isdir(os.path.join(home,'sessions')),'server_time':server_time,'bytes_read':sum(len(c['data'])*3//4 for c in chunks),'recent_completions':completions}

if __name__=='__main__':
    if '--bridge' in sys.argv:
        print(json.dumps(dict(type='ready',**collect({'mode':'info'}))),flush=True)
        for line in sys.stdin:
            try:result=collect(json.loads(line))
            except Exception as exc:result={'error':str(exc)}
            print(json.dumps(result,ensure_ascii=True),flush=True)
    else:
        try:
            request=json.loads(sys.stdin.readline())
            print(json.dumps(collect(request),ensure_ascii=True))
        except Exception as exc:
            print(json.dumps({'error':str(exc)},ensure_ascii=True))
