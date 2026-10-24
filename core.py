"""Codex Monitor: incremental transcript ingestion, no model/API dependency."""
import base64
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import threading
from pathlib import Path

VERSION = '1.2.0'
TZ = dt.timezone(dt.timedelta(hours=8))

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def stamp(value):
    try:
        value = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)).isoformat()
    except (ValueError, TypeError):
        return now()

def digest(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()

def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(c.get('text', '[图片]') for c in content if isinstance(c, dict))
    return json.dumps(content, ensure_ascii=False) if content else ''

def counts(value):
    value = value or {}
    def n(*keys):
        for key in keys:
            if isinstance(value.get(key), (int, float)):
                return max(0, int(value[key]))
        return 0
    inp = n('input_tokens', 'inputTokens')
    out = n('output_tokens', 'outputTokens')
    return {'input': inp, 'cached': min(inp, n('cached_input_tokens', 'cachedInputTokens')),
            'output': out, 'reasoning': n('reasoning_output_tokens', 'reasoningOutputTokens'),
            'total': n('total_tokens', 'totalTokens') or inp + out}

class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS files(source TEXT,path TEXT,offset INTEGER DEFAULT 0,partial BLOB,state TEXT,PRIMARY KEY(source,path));
        CREATE TABLE IF NOT EXISTS sessions(key TEXT PRIMARY KEY,source TEXT,sid TEXT,title TEXT,cwd TEXT,model TEXT,status TEXT,created TEXT,updated TEXT,extra TEXT);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,session TEXT,ts TEXT,kind TEXT,label TEXT,body TEXT,detail TEXT);
        CREATE TABLE IF NOT EXISTS usage(id TEXT PRIMARY KEY,session TEXT,ts TEXT,input INTEGER,cached INTEGER,output INTEGER,reasoning INTEGER,total INTEGER,estimated INTEGER);
        CREATE TABLE IF NOT EXISTS usage_seen(id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS alerts(id TEXT PRIMARY KEY,session TEXT,ts TEXT,title TEXT,body TEXT);
        CREATE INDEX IF NOT EXISTS events_session ON events(session,ts);
        CREATE INDEX IF NOT EXISTS usage_time ON usage(ts);
        CREATE INDEX IF NOT EXISTS usage_session ON usage(session);
        CREATE INDEX IF NOT EXISTS events_kind ON events(session,kind,label);
        ''')
        self.db.commit()

    def cursors(self, source):
        with self.lock:
            return {r['path']: r['offset'] for r in self.db.execute('SELECT path,offset FROM files WHERE source=?', (source,))}

    def ingest(self, source, path, offset, raw, file_time=None):
        with self.lock:
            f = self.db.execute('SELECT * FROM files WHERE source=? AND path=?', (source, path)).fetchone()
            if f and offset != f['offset'] and offset != 0:
                raise ValueError('记录游标不一致，请重新同步')
            state = json.loads(f['state']) if f and offset else {}
            if not state.get('fallback_time'):
                previous = json.loads(f['state']) if f else {}
                state['fallback_time'] = previous.get('fallback_time') or stamp(file_time)
            partial = bytes(f['partial'] or b'') if f and offset else b''
            blob = partial + raw
            lines = blob.split(b'\n')
            pending = lines.pop()
            if len(pending) > 16 * 1024 * 1024:
                raise ValueError('单条记录超过 16 MB，已停止读取该文件')
            parsed = 0
            try:
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        state['malformed'] = state.get('malformed', 0) + 1
                        continue
                    if isinstance(record, dict):
                        state['record_index']=state.get('record_index',0)+1
                        self.record(source, path, record, state, state['fallback_time'])
                        parsed += 1
                self.db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)',
                    (source, path, offset + len(raw), pending, json.dumps(state, ensure_ascii=False)))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            return parsed

    def record(self, source, path, r, state, file_time):
        typ = r.get('type', '')
        p = r.get('payload') or {}
        if not isinstance(p, dict):
            p = {}
        if typ=='event_msg' and p.get('type')=='item_completed' and isinstance(p.get('item'),dict):
            item=dict(p['item'])
            item['type']={'UserMessage':'user_message','AgentMessage':'agent_message',
                'CommandExecution':'command_execution','FileChange':'file_change'}.get(item.get('type'),item.get('type'))
            if isinstance(item.get('changes'),dict):
                changes=[]
                for filename,value in item['changes'].items():
                    value=value if isinstance(value,dict) else {}
                    changes.append({'path':filename,'kind':value.get('type',value.get('kind','update')),
                        'diff':value.get('diff') or value.get('unified_diff') or value.get('content') or ''})
                item['changes']=changes
            r=dict(r,type='item.completed',item=item);typ='item.completed'
        if typ == 'session_meta':
            state['sid'] = str(p.get('id') or state.get('sid') or Path(path).stem)
        elif typ == 'thread.started':
            state['sid'] = r.get('thread_id') or state.get('sid') or Path(path).stem
        sid = state.get('sid') or Path(path).stem
        key = digest(source + ':' + sid)
        ts = stamp(r.get('timestamp') or p.get('timestamp') or file_time)
        row = self.db.execute('SELECT * FROM sessions WHERE key=?', (key,)).fetchone()
        if row:
            session = dict(row)
            extra = json.loads(session['extra'])
        else:
            session = dict(key=key, source=source, sid=sid, title='未命名会话', cwd='', model='', status='unknown', created=ts, updated=ts)
            extra = {}
        extra['log_path'] = path
        session['updated'] = max(session['updated'], ts)
        session['created'] = min(session['created'], ts)
        if typ in ('session_meta', 'turn_context'):
            session['cwd'] = p.get('cwd') or session['cwd']
            model = p.get('model')
            session['model'] = model if isinstance(model, str) else session['model']
            if typ == 'session_meta':
                extra['cli_version'] = p.get('cli_version', '')
        def event(kind, label, body='', detail=None, eid=None):
            eventid = digest(key + ':' + (eid or digest(json.dumps([ts,kind,label,body], ensure_ascii=False))))
            self.db.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?)',
                (eventid, key, ts, kind, label, str(body)[:150000], json.dumps(detail or {}, ensure_ascii=False)))
        def chat(role, body):
            body = str(body or '').strip()
            if not body:
                return
            prev = extra.get('last_chat', {})
            marker = digest(role + body)
            try:
                elapsed = abs((dt.datetime.fromisoformat(ts)-dt.datetime.fromisoformat(prev.get('ts',''))).total_seconds())
            except ValueError:
                elapsed = 999
            if prev.get('marker') == marker and elapsed < 5:
                return
            extra['last_chat'] = {'marker':marker,'ts':ts}
            event(role, '你的指令' if role=='user' else 'Codex', body)
            if role == 'user' and session['title']=='未命名会话':
                session['title'] = body.replace('\n', ' ')[:90]
        def add_usage(info, raw_usage=False):
            if not info:
                return
            total = counts(info if raw_usage else info.get('total_token_usage'))
            last = counts(info.get('last_token_usage')) if not raw_usage else None
            uid = digest(key + json.dumps([ts, info, state.get('record_index') if raw_usage else None], sort_keys=True))
            if self.db.execute('SELECT 1 FROM usage_seen WHERE id=?', (uid,)).fetchone():
                return
            self.db.execute('INSERT INTO usage_seen VALUES(?)',(uid,))
            if raw_usage:
                delta = total
            else:
                prev = extra.get('cumulative')
                if not prev:
                    delta = last if last and last['total'] else total
                    if total['total'] > delta['total']:
                        extra['usage_incomplete'] = True
                elif total['total'] < prev['total']:
                    delta = last or total
                    extra['counter_resets'] = extra.get('counter_resets',0)+1
                else:
                    delta = {k:max(0,total[k]-prev.get(k,0)) for k in total}
                extra['cumulative'] = total
            if delta['total']:
                estimated = int(not bool(r.get('timestamp')))
                if estimated:
                    extra['estimated_dates'] = True
                self.db.execute('INSERT OR IGNORE INTO usage VALUES(?,?,?,?,?,?,?,?,?)',
                    (uid,key,ts,delta['input'],delta['cached'],delta['output'],delta['reasoning'],delta['total'],estimated))
                extra['usage_known'] = True
        def call(q):
            name = str(q.get('name',''))
            callid = str(q.get('call_id') or q.get('id') or digest(str(q)))
            arg = q.get('arguments', q.get('input', ''))
            try:
                args = json.loads(arg) if isinstance(arg,str) else arg
            except ValueError:
                args = arg
            body = args.get('cmd') or args.get('command') or args.get('patch') or json.dumps(args,ensure_ascii=False) if isinstance(args,dict) else str(args)
            if 'apply_patch' in name:
                paths = re.findall(r'^\*\*\* (Add|Update|Delete) File: (.+)$', body, re.M)
                for kind, filename in paths:
                    event('file', filename.strip(), body, {'operation':kind.lower(),'status':'proposed','call_id':callid}, 'file:'+callid+filename)
                extra.setdefault('pending',{})[callid] = {'kind':'patch','paths':paths}
                event('command','应用文件补丁',body,{'status':'running','call_id':callid},'call:'+callid)
            else:
                extra.setdefault('pending',{})[callid] = {'kind':'command'}
                event('command', name or '工具调用', body, {'status':'running','call_id':callid}, 'call:'+callid)
        def output(q):
            callid = str(q.get('call_id') or q.get('id') or '')
            out = q.get('output', q.get('result',''))
            body = text_content(out)
            status = 'unknown'
            if isinstance(out,dict):
                code = out.get('exit_code',out.get('exitCode'))
                if code is not None:
                    status = 'applied' if code==0 else 'failed'
            if re.search(r'(?:"exit_code"\s*:\s*0\b|Process exited with code 0\b|Success\. Updated)',body):
                status='applied'
            elif re.search(r'(?:"exit_code"\s*:\s*[1-9]|Process exited with code [1-9]|Failed to|Error:)',body):
                status='failed'
            pending = extra.setdefault('pending',{}).pop(callid,{})
            if pending.get('kind')=='patch':
                for kind, filename in pending.get('paths',[]):
                    eid=digest(key+':file:'+callid+filename)
                    old=self.db.execute('SELECT detail FROM events WHERE id=?',(eid,)).fetchone()
                    if old:
                        detail=json.loads(old[0]);detail['status']=status
                        self.db.execute('UPDATE events SET detail=? WHERE id=?',(json.dumps(detail,ensure_ascii=False),eid))
            event('output','命令 / 工具结果',body,{'status':status,'call_id':callid},'output:'+callid)
            if len(extra.get('pending',{}))>100:
                extra['pending']={}
        if typ=='event_msg':
            sub=p.get('type','')
            if sub=='user_message':
                chat('user',p.get('message'))
            elif sub=='agent_message':
                chat('assistant',p.get('message'))
            elif sub=='token_count':
                add_usage(p.get('info'))
            elif sub in ('task_started','turn_started'):
                session['status']='running';event('status','开始工作')
            elif sub in ('task_complete','turn_complete','task_completed'):
                session['status']='completed';event('status','本轮完成',detail={'completion':True,'recorded_timestamp':bool(r.get('timestamp'))})
            elif sub in ('turn_aborted','task_failed','error'):
                session['status']='failed';event('status','中断 / 错误',p.get('message',p.get('reason','')))
        elif typ=='response_item':
            sub=p.get('type')
            if sub=='message' and p.get('role') in ('user','assistant'):
                chat('user' if p['role']=='user' else 'assistant',text_content(p.get('content')))
            elif sub in ('function_call','custom_tool_call'):
                call(p)
            elif sub in ('function_call_output','custom_tool_call_output'):
                output(p)
        elif typ in ('compacted','context_compaction'):
            event('status','上下文已压缩');session['status']='unknown'
        elif typ=='turn.completed':
            add_usage(r.get('usage'),True);session['status']='completed';event('status','本轮完成',detail={'completion':True,'recorded_timestamp':bool(r.get('timestamp'))})
        elif typ=='turn.started':
            session['status']='running';event('status','开始工作')
        elif typ in ('turn.failed','error'):
            session['status']='failed';event('status','执行错误',text_content(r.get('error') or r.get('message')))
        elif typ in ('item.started','item.completed'):
            item=r.get('item') or {};kind=item.get('type')
            if kind in ('agent_message','user_message') and typ=='item.completed':
                chat('assistant' if kind=='agent_message' else 'user',item.get('text',text_content(item.get('content'))))
            elif kind=='command_execution':
                cid=str(item.get('id',''))
                event('command','执行命令',item.get('command',''),{'status':item.get('status'),'call_id':cid},'call:'+cid)
                if typ=='item.completed':
                    event('output','命令结果',item.get('aggregated_output',''),{'status':item.get('status'),'exit_code':item.get('exit_code')},'output:'+cid)
            elif kind=='file_change':
                for c in item.get('changes',[]):
                    filename=c.get('path','未知文件')
                    event('file',filename,c.get('diff',''),{'operation':c.get('kind'),'status':'applied' if item.get('status')=='completed' else item.get('status','unknown')},'file:'+str(item.get('id'))+filename)
        session['extra']=json.dumps(extra,ensure_ascii=False)
        self.db.execute('INSERT OR REPLACE INTO sessions VALUES(:key,:source,:sid,:title,:cwd,:model,:status,:created,:updated,:extra)',session)

    def overview(self, source=None):
        with self.lock:
            where=' WHERE source=?' if source else ''
            sessions=[]
            for row in self.db.execute('SELECT * FROM sessions'+where+' ORDER BY updated DESC', (source,) if source else ()):
                s=dict(row);s['extra']=json.loads(s['extra'])
                sums=self.db.execute('SELECT SUM(total),SUM(input),SUM(cached),SUM(output) FROM usage WHERE session=?',(s['key'],)).fetchone()
                s['tokens']=sums[0] or 0;s['usage_known']=bool(s['extra'].get('usage_known'))
                s['files']=self.db.execute("SELECT COUNT(DISTINCT label) FROM events WHERE session=? AND kind='file' AND json_extract(detail,'$.status')='applied'",(s['key'],)).fetchone()[0]
                if s['status']=='running':
                    elapsed=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(s['updated'])).total_seconds()
                    if elapsed>180:
                        s['status']='unknown'
                sessions.append(s)
            keys={s['key'] for s in sessions};daily={};models={}
            for u in self.db.execute('SELECT * FROM usage ORDER BY ts'):
                if u['session'] not in keys:
                    continue
                day=dt.datetime.fromisoformat(u['ts']).astimezone(TZ).strftime('%Y-%m-%d')
                bucket=daily.setdefault(day,dict(date=day,input=0,cached=0,output=0,total=0,estimated=0))
                for k in ('input','cached','output','total','estimated'):
                    bucket[k]+=u[k]
            for s in sessions:
                model=s['model'] or '未记录模型';models[model]=models.get(model,0)+s['tokens']
            today=dt.datetime.now(TZ).strftime('%Y-%m-%d')
            malformed=self.db.execute('SELECT SUM(COALESCE(json_extract(state,\'$.malformed\'),0)) FROM files'+(' WHERE source=?' if source else ''),(source,) if source else ()).fetchone()[0] or 0
            return dict(sessions=sessions,daily=list(daily.values()),models=models,today=today,malformed=malformed,
                totals=dict(tokens=sum(x['tokens'] for x in sessions),today=daily.get(today,{}).get('total',0),sessions=len(sessions),files=sum(s['files'] for s in sessions),unknown_usage=sum(not s['usage_known'] for s in sessions)))

    def detail(self,key):
        with self.lock:
            rows=self.db.execute('SELECT * FROM events WHERE session=? ORDER BY ts DESC,rowid DESC LIMIT 800',(key,)).fetchall()
            data=[]
            for row in reversed(rows):
                e=dict(row);e['detail']=json.loads(e['detail']);data.append(e)
            return data

    def files(self,source=None):
        with self.lock:
            query="SELECT events.*,sessions.title,sessions.cwd,sessions.source FROM events JOIN sessions ON events.session=sessions.key WHERE events.kind='file'"
            if source:query+=' AND sessions.source=?'
            rows=self.db.execute(query+' ORDER BY events.ts DESC LIMIT 500',(source,) if source else ()).fetchall()
            result=[]
            for row in rows:
                e=dict(row);e['detail']=json.loads(e['detail']);result.append(e)
            return result

    def checkpoint(self):
        with self.lock:
            self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def claim_completions(self,source,since):
        if not since:return []
        with self.lock:
            rows=self.db.execute("""SELECT e.id,e.session,e.ts,s.cwd FROM events e
                JOIN sessions s ON e.session=s.key LEFT JOIN alerts a ON e.id=a.id
                WHERE s.source=? AND e.kind='status' AND e.label='本轮完成' AND e.ts>=?
                AND json_extract(e.detail,'$.completion')=1 AND json_extract(e.detail,'$.recorded_timestamp')=1
                AND a.id IS NULL ORDER BY e.ts LIMIT 20""",(source,since)).fetchall()
            result=[]
            for row in rows:
                project=Path(row['cwd']).name or '服务器项目'
                alert=dict(id=row['id'],session=row['session'],ts=row['ts'],title='Codex 本轮对话已完成',
                    body='项目：'+project[:70]+'。打开监控台查看结果。')
                self.db.execute('INSERT OR IGNORE INTO alerts VALUES(?,?,?,?,?)',
                    (alert['id'],alert['session'],alert['ts'],alert['title'],alert['body']))
                result.append(alert)
            self.db.commit();return result

    def alert_history(self):
        with self.lock:return [dict(row) for row in self.db.execute('SELECT * FROM alerts ORDER BY ts DESC LIMIT 50')]

    def queue_completion(self,source,sid,ts,cwd):
        key=digest(source+':'+sid);ts=stamp(ts)
        event_id=digest(key+':'+digest(json.dumps([ts,'status','本轮完成',''],ensure_ascii=False)))
        project=Path(cwd).name or '服务器项目'
        alert=dict(id=event_id,session=key,ts=ts,title='Codex 本轮对话已完成',body='项目：'+project[:70]+'。打开监控台查看结果。')
        with self.lock:
            cursor=self.db.execute('INSERT OR IGNORE INTO alerts VALUES(?,?,?,?,?)',
                (event_id,key,ts,alert['title'],alert['body']))
            self.db.commit()
            return alert if cursor.rowcount else None

