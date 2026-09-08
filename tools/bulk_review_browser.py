"""Optional Chromium accept-all regression, with native HTTP preferred.

Requires playwright and an installed Chromium; not a runtime dependency. Creates
only a temporary test project, populated by deterministic weak-model responses.
If the environment blocks browser loopback requests, records use of a Python
HTTP bridge explicitly (that mode does NOT validate native networking or CSP).
"""
from __future__ import annotations

import argparse
from contextlib import closing
import asyncio
import base64
import json
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from noveltool import __version__
from noveltool.db import ProjectStore
from noveltool.settings import ReviewWrite
from test_bulk_review import reviewed_source, clone_rows


def seed(path):
    with ProjectStore.create(path, '批量接纳页面验收'):
        pass

    async def work():
        s = await reviewed_source(path)
        try:
            clone_rows(s, 600)
            view = await s.knowledge.view()
            entity = next(r for r in view['observations'] if r['kind'] == 'entity')
            event = next(r for r in view['observations'] if r['kind'] == 'event')
            for oid, decision in [(entity['id'], 'accepted'), (event['id'], 'rejected')]:
                view = await s.settings.review(ReviewWrite(expected_version=view['version'], observation_ids=[oid], decision=decision))
            return s.manuscript.text
        finally:
            await s.close()
    return asyncio.run(work())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', default=shutil.which('chromium') or '/usr/bin/chromium')
    parser.add_argument('--output', type=Path, default=ROOT / 'verification' / 'bulk-review-browser')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    errors, checks, dialogs = [], [], []
    with tempfile.TemporaryDirectory(prefix='noveltool-bulk-ui-') as td:
        path = Path(td) / 'project.sqlite3'
        source = seed(path)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        log = (args.output / 'server.log').open('w')
        proc = subprocess.Popen([sys.executable, '-m', 'noveltool', '--project', str(path), '--port', str(port)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        try:
            base = f'http://127.0.0.1:{port}'
            with httpx.Client(base_url=base, timeout=10, trust_env=False) as http:
                for _ in range(100):
                    if proc.poll() is not None:
                        raise RuntimeError('测试服务器提前退出')
                    try:
                        if http.get('/health').status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.05)
                else:
                    raise RuntimeError('测试服务器未就绪')
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path=args.browser, headless=True, args=['--no-sandbox'])
                    ctx = browser.new_context(viewport={'width': 1280, 'height': 960}, locale='zh-CN')
                    page = ctx.new_page()
                    mode, native_error = 'native_http', None
                    try:
                        page.goto(base + '/settings', timeout=10000)
                        page.wait_for_function("document.querySelector('#review-counts')?.textContent.includes('待审 602 条')", timeout=5000)
                    except Exception as exc:
                        native_error = str(exc).split('\n')[0]
                        mode = 'python_http_bridge'
                        page.close()
                        page = None

                    def load(route, reuse=None):
                        page = reuse or ctx.new_page()
                        page.on('pageerror', lambda err: errors.append(str(err)))
                        if mode == 'native_http':
                            if reuse is None:
                                page.goto(base + route)
                        else:
                            def fetch(url, options):
                                response = http.request(options.get('method', 'GET'), url,
                                    headers=options.get('headers', {}), content=options.get('body'))
                                return {'status': response.status_code, 'headers': dict(response.headers),
                                        'body': base64.b64encode(response.content).decode()}
                            page.expose_function('__bulkRequest', fetch)
                            html = http.get(route).text
                            scripts = re.findall(r'<script[^>]*src="([^"]+)"[^>]*></script>', html)
                            html = re.sub(r'<script[^>]*src="[^"]+"[^>]*></script>', '', html)
                            html = re.sub(r'<link[^>]*href="/static/app.css"[^>]*>', '', html)
                            page.set_content(html)
                            page.add_style_tag(content=http.get('/static/app.css').text)
                            page.add_script_tag(content="""window.fetch=async(url,options={})=>{const r=await window.__bulkRequest(String(url),options);return new Response(Uint8Array.from(atob(r.body),c=>c.charCodeAt(0)),{status:r.status,headers:r.headers});};""")
                            for script in scripts:
                                page.add_script_tag(content=http.get(script).text)
                        page.wait_for_function("document.querySelector('#review-counts')?.textContent.includes('当前来源有效')")
                        return page

                    page = load('/settings', page)
                    stale = load('/timeline')
                    assert page.locator('#review-items input[data-observation-id]').count() == 80
                    assert '602' in page.inner_text('#accept-all-pending')
                    checks.append('602 条待审、列表只展开 80 条，按钮仍显示全部数量')
                    page.select_option('#review-filter', 'rejected')
                    assert page.locator('#review-items input').count() == 1

                    def dialog(accept=True):
                        def handle(d):
                            dialogs.append(d.message)
                            d.accept() if accept else d.dismiss()
                        return handle

                    page.once('dialog', dialog(False))
                    page.click('#accept-all-pending')
                    assert sum(r['status'] == 'pending' for r in http.get('/api/settings').json()['observations']) == 602
                    checks.append('取消确认不会接纳，已拒绝筛选不限制全量按钮范围')
                    page.fill('#profile-notes', '尚未保存的人工修改')
                    page.click('#accept-all-pending')
                    assert '未保存' in page.inner_text('#message')
                    assert page.input_value('#profile-notes') == '尚未保存的人工修改'
                    page.once('dialog', dialog())
                    page.click('#refresh')
                    page.wait_for_function("document.querySelector('#profile-notes').value === ''")
                    checks.append('未保存的人工表单会阻止批量刷新覆盖')

                    with closing(sqlite3.connect(path)) as conn, conn:
                        conn.execute("CREATE TRIGGER fail_batch BEFORE INSERT ON setting_changes WHEN NEW.action='review_all_pending' BEGIN SELECT RAISE(ABORT,'simulated audit failure'); END")
                    page.once('dialog', dialog())
                    page.click('#accept-all-pending')
                    page.wait_for_function("document.querySelector('#message').textContent.includes('失败')")
                    assert sum(r['status'] == 'pending' for r in http.get('/api/settings').json()['observations']) == 602
                    assert page.is_enabled('#accept-all-pending')
                    with closing(sqlite3.connect(path)) as conn, conn:
                        conn.execute('DROP TRIGGER fail_batch')
                    checks.append('事务失败显示错误、恢复按钮且没有部分接纳')

                    page.once('dialog', dialog())
                    page.click('#accept-all-pending')
                    page.wait_for_function("document.querySelector('#message').textContent.includes('已一次接纳 602 条')")
                    view = http.get('/api/settings').json()
                    assert sum(r['status'] == 'pending' for r in view['observations']) == 0
                    assert sum(r['status'] == 'accepted' for r in view['observations']) == 603
                    assert sum(r['status'] == 'rejected' for r in view['observations']) == 1
                    assert page.is_disabled('#accept-all-pending')
                    assert http.get('/api/manuscript').json()['text'] == source
                    checks.append('一次接纳全部 602 条，保留 1 条拒绝和已有接纳，正文未变')
                    page.locator('#observation-review').scroll_into_view_if_needed()
                    page.screenshot(path=str(args.output / 'settings-accepted.png'))

                    stale.once('dialog', dialog())
                    stale.click('#accept-all-pending')
                    stale.wait_for_function("document.querySelector('#message').textContent.includes('已经变化')")
                    history = http.get('/api/settings/history').json()['changes']
                    assert sum(r['action'] == 'review_all_pending' for r in history) == 1
                    stale.click('#refresh')
                    stale.wait_for_function("document.querySelector('#review-counts').textContent.includes('待审 0 条')")
                    assert stale.is_disabled('#accept-all-pending')
                    checks.append('旧时间线页面版本冲突被拒绝，刷新后数量为零')
                    assert not errors, errors
                    browser.close()
                result = {'program': __version__, 'browser_mode': mode, 'native_error': native_error,
                          'checks': checks, 'page_errors': errors, 'confirmation_count': len(dialogs),
                          'real_user_model': False}
                (args.output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
                print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
            log.close()


if __name__ == '__main__':
    main()
