"""Real subprocess/wheel/upgrade/crash smoke checks on disposable project files.

Usage: python tools/release_smoke.py --installed /path/to/pip-target
        --old-source /path/to/noveltool-v0.13.0 --output /path/to/report-dir
The old version and installed new wheel execute in separate processes/import paths.
No real model or network service is required; HTTP goes only to local uvicorn.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed',type=Path,required=True)
    parser.add_argument('--old-source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    checks=[]
    def env(root):return {**os.environ,'PYTHONPATH':str(root.resolve()),'PYTHONNOUSERSITE':'1'}
    def run(code,root,cwd):
        return subprocess.run([sys.executable,'-W','error','-c',code],cwd=cwd,env=env(root),text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True)
    with tempfile.TemporaryDirectory(prefix='noveltool-release-') as td:
        folder=Path(td);project=folder/'upgrade.sqlite3'
        old_code=r'''
import asyncio,json
from pathlib import Path
import httpx
from noveltool.db import ProjectStore
from noveltool.runtime import ProjectSession
from noveltool.domain import ProjectConfig
from noveltool.context import ContextRequest
from noveltool.generation import GenerationRequest
from noveltool.drafts import DraftInput
from noveltool import __version__
p=Path('upgrade.sqlite3');st=ProjectStore.create(p,'升级测试',ProjectConfig(writer_model='fake',analysis_model='fake'));st.close()
async def main():
 s=ProjectSession(ProjectStore.open(p))
 try:
  await s.import_manuscript('原文😀第一段。\n\n这是第二段。',0)
  await s.replace_manuscript(0,2,'手动返修',1)
  view=await s.knowledge.view()
  b=GenerationRequest(context=ContextRequest(expected_revision_no=2,expected_version=view['version'],min_chars=1,max_chars=100),candidate_count=1)
  tr=httpx.MockTransport(lambda r:httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'旧版本产生的完整候选。'}}]}))
  t=await s.generation.start(b,transport=tr);await s.generation.task
  await s.generation.drafts.save(t['id'],DraftInput(text='保留这个旧版本拼装草稿。',expected_draft_version=0,force_save=True))
  Path('old-state.json').write_text(json.dumps({'version':__version__,'schema':s.project.data.meta.schema_version,'text':s.manuscript.text,'task':t['id']},ensure_ascii=False))
 finally:await s.close()
asyncio.run(main())
'''
        run(old_code,args.old_source,folder)
        old=json.loads((folder/'old-state.json').read_text());assert old['version']=='0.13.0'
        new_code=r'''
import asyncio,json
from pathlib import Path
from importlib.resources import files
from noveltool.db import ProjectStore
from noveltool.runtime import ProjectSession
from noveltool import __version__
old=json.loads(Path('old-state.json').read_text())
async def main():
 st=ProjectStore.open(Path('upgrade.sqlite3'));assert st.migration_backup and st.migration_backup.is_file()
 s=ProjectSession(st)
 try:
  assert s.manuscript.text==old['text']
  assert (await s.generation.view(old['task']))['draft_text']=='保留这个旧版本拼装草稿。'
  assert s.project.data.meta.schema_version==14
  assert (await s.maintenance.check(2))['ok']
  assert files('noveltool').joinpath('static/maintenance.html').is_file()
  assert len([p for p in files('noveltool').joinpath('sql').iterdir() if p.name.endswith('.sql')])==14
  await s.undo_manuscript(2)
  assert s.manuscript.text=='原文😀第一段。\n\n这是第二段。'
  print(json.dumps({'new_version':__version__,'schema':14,'old_schema':old['schema'],'backup':st.migration_backup.name}))
 finally:await s.close()
asyncio.run(main())
'''
        result=run(new_code,args.installed,folder);print(result.stdout)
        checks.append('v0.13.0 独立进程创建并保存正文/候选/草稿，v0.17.0 wheel 升级先备份；保存草稿保留，旧正文撤销逐字恢复')
        backups=list(folder.glob('*.bak'));assert len(backups)==1
        run("from pathlib import Path;from noveltool.db import ProjectStore;s=ProjectStore.open(Path(%r));assert s.load().meta.schema_version==11;s.close()" % backups[0].name,args.old_source,folder)
        checks.append('升级前备份仍可由原 v0.13.0 打开，未被新 Schema 污染')
        def start():
            sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
            log=(args.output/'process.log').open('ab')
            proc=subprocess.Popen([sys.executable,'-m','noveltool','--project',str(project),'--port',str(port)],cwd=folder,env=env(args.installed),stdout=log,stderr=log)
            log.close();client=httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=10)
            for _ in range(100):
                try:
                    if client.get('/health').status_code==200:return proc,client
                except httpx.HTTPError:pass
                if proc.poll() is not None:raise RuntimeError('server terminated')
                time.sleep(.05)
            proc.kill();proc.wait();client.close();raise RuntimeError('server startup timeout')
        proc,client=start()
        try:
            assert client.get('/health').json()['version']=='0.17.0'
            for url in ('/generation','/sync','/consistency','/maintenance','/static/maintenance.js'):
                assert client.get(url).status_code==200
            headers={'X-Noveltool-Token':client.get('/api/session').json()['csrf_token']}
            model_calls=[]
            class FakeEndpoint(BaseHTTPRequestHandler):
                def do_POST(self):
                    body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
                    model_calls.append({'path':self.path,'body':body})
                    raw=json.dumps({'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'通过真正 HTTP 返回的模拟候选。'}}]},ensure_ascii=False).encode('utf-8')
                    self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                def log_message(self,*args):pass
            model=ThreadingHTTPServer(('127.0.0.1',0),FakeEndpoint)
            thread=threading.Thread(target=model.serve_forever,daemon=True);thread.start()
            try:
                cfg=client.get('/api/config').json();cfg['config']['api_base_url']=f'http://127.0.0.1:{model.server_port}/v1'
                assert client.put('/api/config',headers=headers,json={'expected_memory_version':cfg['memory_version'],'config':cfg['config']}).status_code==200
                catalog=client.get('/api/settings').json();doc=client.get('/api/manuscript').json()
                task=client.post('/api/generation',headers=headers,json={'context':{'expected_revision_no':doc['revision_no'],'expected_version':catalog['version'],'min_chars':1,'max_chars':100},'candidate_count':2})
                assert task.status_code==202,task.text
                for _ in range(100):
                    value=client.get('/api/generation/'+task.json()['id']).json()
                    if not value['live']:break
                    time.sleep(.02)
                assert value['usable_count']==2 and len(model_calls)==2
                assert all(c['path']=='/v1/chat/completions' and c['body']['model']=='fake' for c in model_calls)
                checks.append('安装的 wheel 经真实 HTTP 访问临时兼容端点：两个候选对应两次独立请求；响应为测试桩而非真实语言模型')
            finally:model.shutdown();model.server_close();thread.join(timeout=5)
            cfg=client.get('/api/config').json()
            assert client.put('/api/project',headers=headers,json={'expected_memory_version':cfg['memory_version'],'title':'SIGTERM 正常退出保存'}).status_code==200
            assert client.get('/api/status').json()['dirty']
            proc.send_signal(signal.SIGTERM);proc.wait(timeout=15);assert proc.returncode in (0,-signal.SIGTERM)
        finally:
            client.close()
            if proc.poll() is None:proc.kill();proc.wait()
        proc,client=start()
        try:
            cfg=client.get('/api/config').json();assert cfg['title']=='SIGTERM 正常退出保存'
            headers={'X-Noveltool-Token':client.get('/api/session').json()['csrf_token']}
            current=client.get('/api/manuscript').json()
            r=client.post('/api/manuscript/append',headers=headers,json={'expected_revision_no':current['revision_no'],'text':'进程强制终止前已确认的段落。'});assert r.status_code==200
            saved=r.json()['text'];rev=r.json()['revision_no']
            backup=client.post('/api/maintenance/backup',headers=headers,json={'expected_revision_no':rev});assert backup.status_code==200
            (folder/'download.sqlite3').write_bytes(backup.content)
            cfg=client.get('/api/config').json()
            client.put('/api/project',headers=headers,json={'expected_memory_version':cfg['memory_version'],'title':'此内存修改预期丢失'})
            assert client.get('/api/status').json()['dirty']
            proc.kill();proc.wait(timeout=10)
        finally:
            client.close()
            if proc.poll() is None:proc.kill();proc.wait()
        proc,client=start()
        try:
            assert client.get('/api/manuscript').json()['text']==saved
            assert client.get('/api/config').json()['title']=='SIGTERM 正常退出保存'
            checks.append('真实 uvicorn 本地 HTTP：SIGTERM 保存 dirty 配置；SIGKILL 后已确认正文保留，未写盘配置按预期丢失')
            proc.send_signal(signal.SIGTERM);proc.wait(timeout=15)
        finally:
            client.close()
            if proc.poll() is None:proc.kill();proc.wait()
        run("from pathlib import Path;from noveltool.db import ProjectStore;from noveltool.revision import load_manuscript;s=ProjectStore.open(Path('download.sqlite3'));assert load_manuscript(s.connection).text==%r;s.close()" % saved,args.installed,folder)
        checks.append('真实 HTTP 下载的备份可以独立打开，包含已确认正文，无需原数据库 WAL')
    report={'checks':checks,'old_version':old['version'],'old_schema':old['schema'],'new_version':'0.17.0','new_schema':14,'external_model_calls':0}
    (args.output/'release-smoke.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
