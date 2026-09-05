"""Optional real Chromium test. pip install playwright; provide --chromium path.
Uses only a new temporary project, never an existing user database.
"""
from __future__ import annotations
import argparse
import json
import re
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from pathlib import Path
import tempfile
from playwright.sync_api import sync_playwright, expect
from smoke_check import running_server


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--chromium', default='/usr/bin/chromium')
    parser.add_argument('--screenshot', type=Path)
    parser.add_argument('--bridge', action='store_true', help='Test-only Python HTTP bridge when browser localhost access is disabled')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='noveltool-browser-') as tmp:
        with running_server(Path(tmp)/'book.sqlite3',create=True) as (_,base):
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=args.chromium,headless=True,args=['--no-sandbox','--no-proxy-server'])
                page=browser.new_page(viewport={'width':1280,'height':960})
                errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('dialog',lambda dialog:dialog.accept())
                def open_editor(page,kind="manuscript"):
                    if not args.bridge:
                        page.goto(base+'/'+kind)
                        return
                    # This is explicitly a browser-DOM + live-backend test, not a
                    # browser-network or CSP test. It changes no production code.
                    root=Path(__file__).resolve().parents[1]/'noveltool'/'static'
                    with urlopen(base+'/'+kind) as response:
                        html=response.read().decode()
                    html=re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.S)
                    html=re.sub(r'<link[^>]*>', '', html)
                    page.set_content(html)
                    page.add_style_tag(path=str(root/'app.css'))
                    def http_bridge(path, options):
                        if not path.startswith('/') or path.startswith('//'):
                            raise ValueError('test bridge only accepts local paths')
                        body=options.get('body')
                        req=Request(base+path,data=(bytes(body) if isinstance(body,list) else body.encode()) if body is not None else None,
                                    method=options.get('method','GET'),headers=options.get('headers',{}))
                        try:
                            with urlopen(req,timeout=10) as response:
                                return {'status':response.status,'text':response.read().decode()}
                        except HTTPError as e:
                            return {'status':e.code,'text':e.read().decode()}
                    page.expose_function('noveltoolTestHttp',http_bridge)
                    page.evaluate("() => { window.fetch = async (path, options={}) => {if(options.body instanceof ArrayBuffer)options={...options,body:Array.from(new Uint8Array(options.body))};const r=await window.noveltoolTestHttp(path, options);return new Response(r.text,{status:r.status,headers:{'Content-Type':'application/json'}});}; }")
                    page.add_script_tag(path=str(root/(kind+'.js')))
                open_editor(page)
                expect(page.locator('#revision')).to_have_text('Revision 0')
                text='　甲😀乙。\n\n第二段。\n\n<script>window.injected=1</script>\n'
                page.locator('#draft').fill(text)
                page.locator('#import').click()
                expect(page.locator('#revision')).to_have_text('Revision 1')
                expect(page.locator('#manuscript')).to_have_value(text)
                assert page.evaluate('window.injected') is None
                # JS UTF-16 [2,4) is one emoji at Unicode code point [2,3).
                page.locator('#manuscript').evaluate('(el)=>el.setSelectionRange(2,4)')
                page.locator('#select').click()
                expect(page.locator('#draft')).to_have_value('😀')
                page.locator('#draft').fill('星光')
                page.locator('#commit').click()
                expect(page.locator('#revision')).to_have_text('Revision 2')
                expect(page.locator('#manuscript')).to_have_value(text.replace('😀','星光'))
                page.locator('#undo').click()
                expect(page.locator('#revision')).to_have_text('Revision 3')
                expect(page.locator('#manuscript')).to_have_value(text)
                page.locator('#first-line').fill('3');page.locator('#last-line').fill('3')
                page.locator('#select-lines').click()
                expect(page.locator('#draft')).to_have_value('第二段。\n')
                page.locator('#draft').fill('行范围改写。\n')
                page.locator('#commit').click()
                expect(page.locator('#revision')).to_have_text('Revision 4')
                page.locator('#edit-all').click()
                page.locator('#draft').fill(text)
                page.locator('#commit').click()
                expect(page.locator('#revision')).to_have_text('Revision 5')
                # A second browser tab updates the manuscript. Old selection keeps
                # its draft, but cannot overwrite the newer revision.
                page.locator('#manuscript').evaluate('(el)=>el.setSelectionRange(0,1)')
                page.locator('#select').click();page.locator('#draft').fill('保留我的草稿')
                second=browser.new_page();open_editor(second)
                expect(second.locator('#revision')).to_have_text('Revision 5')
                second.locator('#draft').fill('新段落');second.locator('#append').click()
                expect(second.locator('#revision')).to_have_text('Revision 6')
                page.locator('#commit').click()
                expect(page.locator('#message')).to_contain_text('正文版本已变化')
                expect(page.locator('#draft')).to_have_value('保留我的草稿')
                if args.screenshot:page.screenshot(path=str(args.screenshot),full_page=True)
                model_page=browser.new_page()
                model_page.on('pageerror',lambda e:errors.append(str(e)))
                open_editor(model_page, "model")
                expect(model_page.locator('#message')).to_contain_text('先确认模型名')
                model_page.locator('#raw-output').fill("{'entities':[], 'facts':[], 'events':[],}")
                model_page.locator('#validate-local').click()
                expect(model_page.locator('#validation-meta')).to_contain_text('local_repaired')
                assert json.loads(model_page.locator('#parsed-output').input_value()) == {'entities':[],'facts':[],'events':[]}
                model_page.locator('#raw-output').fill('{"entities":[],"entities":[],"facts":[],"events":[]}')
                model_page.locator('#validate-local').click()
                expect(model_page.locator('#message')).to_contain_text('重复键')
                second.locator('#edit-all').click()
                second.locator('#draft').fill('')
                second.locator('#commit').click()
                expect(second.locator('#revision')).to_have_text('Revision 7')
                imports=browser.new_page(viewport={'width':1280,'height':960})
                imports.on('pageerror',lambda e:errors.append(str(e)))
                open_editor(imports,'import')
                expect(imports.locator('#message')).to_contain_text('按顺序')
                raw=('第一章\r\n\r\n'+('　甲😀向前走。\r\n'*600)).encode('utf-16')
                imports.locator('#file').set_input_files({'name':'original.txt','mimeType':'text/plain','buffer':raw})
                imports.locator('#preview').click()
                expect(imports.locator('#preview-info')).to_contain_text('utf-16')
                expect(imports.locator('#commit')).to_be_enabled()
                imports.locator('#mode').select_option('blankline')
                expect(imports.locator('#commit')).to_be_disabled()
                imports.locator('#preview').click()
                expect(imports.locator('#commit')).to_be_enabled()
                imports.locator('#commit').click()
                expect(imports.locator('#document-info')).to_contain_text('Revision 8')
                expect(imports.locator('#sources')).to_contain_text('original.txt')
                imports.locator('#target').fill('512');imports.locator('#overlap').fill('64')
                imports.locator('#plan').click()
                expect(imports.locator('#plan-info')).to_contain_text('当前有效')
                assert imports.locator('#chunks details').count()>10
                if args.screenshot:imports.screenshot(path=str(args.screenshot.with_name('import-'+args.screenshot.name)),full_page=True)
                assert not errors,errors
                browser.close()
                print('PASS:', 'Chromium DOM + Python HTTP bridge' if args.bridge else 'direct Chromium HTTP', 'import, Unicode replace, undo, line edit, full edit, XSS, stale-tab draft retention, JSON validation, binary TXT preview/commit/chunk plan')

if __name__=='__main__':main()
