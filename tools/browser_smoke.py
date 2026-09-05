"""Optional UI smoke test using real Chromium JavaScript and a Python HTTP bridge.

This harness deliberately DOES NOT claim to test native browser networking/CSP:
HTML and local assets are installed into about:blank; fetch() crosses a binding
into FastAPI TestClient. Use when a managed Chromium disallows loopback URLs.
Requires development dependencies plus playwright and a Chromium executable.
No real model, no external network, no user project. Temporary test DB only.
"""
from __future__ import annotations
import argparse
import base64
import json
from pathlib import Path
import re
import sys
import tempfile

import httpx
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
from noveltool.app import create_app
from noveltool.db import ProjectStore
from noveltool.domain import ProjectConfig
from test_llm import envelope
from test_semantic import response as analysis_response
from test_consistency import finding
from test_rewrite import SOURCE, NEW


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser',default='/usr/bin/chromium')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    requests=[];errors=[];checks=[]
    def handler(req):
        payload=json.loads(req.content);body=json.loads(payload['messages'][-1]['content'])
        requests.append(payload)
        if 'sources' in body:return httpx.Response(200,json=envelope(json.dumps(finding(req),ensure_ascii=False)))
        if 'schema' in body:return analysis_response(req)
        return httpx.Response(200,json=envelope(NEW))
    with tempfile.TemporaryDirectory(prefix='noveltool-ui-') as td:
        path=Path(td)/'sample.sqlite3'
        store=ProjectStore.create(path,'浏览器流程验收',ProjectConfig(writer_model='fake-writer',analysis_model='fake-analyzer',min_chars=1,max_chars=200,candidate_count=2))
        store.close()
        with TestClient(create_app(path,llm_transport=httpx.MockTransport(handler)),base_url='http://127.0.0.1') as client, sync_playwright() as pw:
            h={'X-Noveltool-Token':client.get('/api/session').json()['csrf_token']}
            assert client.post('/api/manuscript/import',headers=h,json={'text':SOURCE,'expected_revision_no':0}).status_code==200
            browser=pw.chromium.launch(executable_path=args.browser,headless=True,args=['--no-sandbox'])
            context=browser.new_context(viewport={'width':1440,'height':1000},locale='zh-CN',accept_downloads=True)
            def load(path):
                page=context.new_page()
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('dialog',lambda dialog:dialog.accept())
                def request(url,options):
                    method=options.get('method','GET')
                    result=client.request(method,url,headers=options.get('headers',{}),content=options.get('body'))
                    return {'status':result.status_code,'headers':dict(result.headers),'body':base64.b64encode(result.content).decode('ascii')}
                page.expose_function('__novelRequest',request)
                html=client.get(path).text
                scripts=re.findall(r'<script[^>]*src="([^"]+)"[^>]*></script>',html)
                html=re.sub(r'<script[^>]*src="[^"]+"[^>]*></script>','',html)
                html=re.sub(r'<link[^>]*href="/static/app.css"[^>]*>','',html)
                page.set_content(html)
                page.add_style_tag(content=client.get('/static/app.css').text)
                page.add_script_tag(content='''window.fetch=async (url,options={})=>{const r=await window.__novelRequest(String(url),options);return new Response(Uint8Array.from(atob(r.body),c=>c.charCodeAt(0)),{status:r.status,headers:r.headers});};''')
                for script in scripts:page.add_script_tag(content=client.get(script).text)
                return page
            page=load('/generation')
            page.wait_for_function("document.querySelector('#project-info').textContent.includes('Revision 1')")
            page.select_option('#task-type','rewrite')
            # Actual browser UTF-16 selection: Python CP [2,18) follows an emoji.
            a16=len(SOURCE[:2].encode('utf-16-le'))//2;b16=len(SOURCE[:18].encode('utf-16-le'))//2
            page.eval_on_selector('#rewrite-source','(el,p)=>{el.focus();el.setSelectionRange(p[0],p[1]);}',[a16,b16])
            page.click('#rewrite-use-selection')
            assert page.input_value('#rewrite-start')=='2' and page.input_value('#rewrite-end')=='18'
            checks.append('真实 UTF-16 鼠标选区转换为 code point，跨 emoji 正确')
            page.fill('#instruction','保留钥匙，没有交给周。');page.click('#preview')
            page.wait_for_function("document.querySelector('#context-preview').textContent.includes('messages')")
            package=json.loads(page.text_content('#context-preview'))
            payload=json.loads(package['messages'][-1]['content'])
            assert any(part['text']==SOURCE[2:18] for part in payload['sections'])
            page.click('#start')
            page.wait_for_function("document.querySelector('#task-info').textContent.includes('可用文本 2/2')",timeout=15000)
            assert client.get('/api/manuscript').json()['text']==SOURCE
            # Select one candidate fragment, then manually assemble the final complete text.
            candidate=page.locator('#candidates textarea').first
            candidate.evaluate('(el)=>{el.focus();el.setSelectionRange(0,5);}')
            page.get_by_role('button',name='选中文字插入草稿',exact=True).first.click()
            assert page.input_value('#draft')==NEW[:5]
            page.fill('#draft',NEW)
            page.wait_for_function("document.querySelector('#draft-status').textContent.includes('程序内存')",timeout=10000)
            page.click('#save-draft')
            page.wait_for_function("document.querySelector('#draft-status').textContent.includes('保存到磁盘')")
            tid=client.get('/api/generation').json()['tasks'][0]['id']
            assert client.get(f'/api/generation/{tid}').json()['draft_text']==NEW
            checks.append('候选选段、人工拼装、自动同步内存和立即保存草稿')
            page.click('#preview-rewrite');page.wait_for_function("document.querySelector('#rewrite-diff').textContent.includes('原选区')")
            page.click('#commit-draft')
            page.wait_for_function("document.querySelector('#draft-status').textContent.includes('已确认')",timeout=15000)
            assert client.get('/api/manuscript').json()['text']==SOURCE[:2]+NEW+SOURCE[18:]
            # Wait for sync and queued check via the real program tasks (no mock state).
            session=client.app.state.session
            if session.jobs.task:client.portal.call(lambda:session.jobs.task)
            if session.consistency.task:client.portal.call(lambda:session.consistency.task)
            assert client.get('/api/sync').json()['coverage']['status']=='synced'
            jobs=client.get('/api/consistency').json()['jobs'];assert jobs and jobs[0]['status']=='done'
            issue=client.get('/api/consistency/'+jobs[0]['id']).json()['issues'];assert issue
            checks.append('差异预览、原子返修确认、增量同步及顺序执行后文检查')
            page.screenshot(path=str(args.output/'rewrite-browser.png'),full_page=True)
            cpage=load('/consistency');cpage.wait_for_selector('#issues section')
            cpage.get_by_role('button',name='已忽略',exact=True).first.click()
            cpage.wait_for_function("document.querySelector('#issues h4').textContent.includes('已忽略')")
            cpage.select_option('#issue-filter','open');assert not cpage.locator('#issues section').count()
            cpage.select_option('#issue-filter','all')
            cpage.screenshot(path=str(args.output/'consistency-browser.png'),full_page=True)
            checks.append('一致性问题证据、人工忽略状态与筛选器')
            mpage=load('/maintenance');mpage.wait_for_function("document.querySelector('#overview').textContent.includes('Revision 2')")
            mpage.click('#check-project');mpage.wait_for_function("document.querySelector('#check-result').textContent.includes('\"ok\": true')")
            mpage.locator('#revision-history button').first.click()
            assert NEW in mpage.inner_text('#history-after')
            mpage.locator('#runs button').first.click();mpage.wait_for_function("document.querySelector('#run-detail').textContent.includes('consistency_check')")
            mpage.screenshot(path=str(args.output/'maintenance-browser.png'),full_page=True)
            checks.append('维护页完整性检查、历史差异、模型调用日志')
            rpage=load('/manuscript');rpage.wait_for_function("document.querySelector('#revision').textContent.includes('2')")
            rpage.click('#undo');rpage.wait_for_function("document.querySelector('#revision').textContent.includes('3')")
            assert client.get('/api/manuscript').json()['text']==SOURCE
            checks.append('浏览器确认撤销，正文逐字恢复（含 emoji 与段落）')
            assert not errors,errors
            browser.close()
    report={'transport':'Chromium JS + Python FastAPI TestClient fetch bridge; no native browser networking/CSP claim','checks':checks,'javascript_errors':errors,'mock_model_calls':len(requests)}
    (args.output/'browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
