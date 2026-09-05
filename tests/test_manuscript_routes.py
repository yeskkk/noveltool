import pytest
from noveltool.manuscript_routes import minimal_change


def post(client, headers, path, data):
    return client.post('/api/'+path, headers=headers, json=data)


def test_pages_and_export(client):
    assert client.get('/manuscript').status_code == 200
    assert client.get('/static/manuscript.js').status_code == 200
    result = client.get('/api/manuscript').json()
    assert result['text'] == '' and result['revision_no'] == 0
    assert client.get('/api/manuscript/export').content == b''


def test_import_normalizes_only_line_endings_and_bom(client, write_headers):
    r = post(client, write_headers, 'manuscript/import', {
        'expected_revision_no':0, 'text':'\ufeff　甲😀\r\n\r\n尾 \r', 'paragraph_mode':'auto'})
    assert r.status_code == 200
    assert r.json()['text'] == '　甲😀\n\n尾 \n'
    assert client.get('/api/manuscript/export').content.decode() == '　甲😀\n\n尾 \n'
    assert client.get('/api/status').json()['dirty'] is False
    bad = post(client, write_headers, 'manuscript/import', {'expected_revision_no':1, 'text':'新书'})
    assert bad.status_code == 422


def test_unicode_replace_empty_insert_delete_and_undo(client, write_headers):
    post(client, write_headers, 'manuscript/import', {'expected_revision_no':0, 'text':'甲😀乙\n\n末尾'})
    r=post(client, write_headers,'manuscript/replace',{
        'expected_revision_no':1,'start_cp':1,'end_cp':2,'selected_text':'😀','text':'替换'})
    assert r.json()['text']=='甲替换乙\n\n末尾'
    r=post(client, write_headers,'manuscript/replace',{
        'expected_revision_no':2,'start_cp':1,'end_cp':1,'selected_text':'','text':'插入'})
    assert r.json()['text'].startswith('甲插入替换')
    r=post(client, write_headers,'manuscript/replace',{
        'expected_revision_no':3,'start_cp':1,'end_cp':3,'selected_text':'插入','text':''})
    assert r.json()['text'].startswith('甲替换')
    r=post(client,write_headers,'revisions/undo',{'expected_revision_no':4})
    assert r.json()['text'].startswith('甲插入替换')


def test_stale_and_wrong_selected_text_are_never_applied(client,write_headers):
    post(client,write_headers,'manuscript/import',{'expected_revision_no':0,'text':'abcd'})
    body={'expected_revision_no':0,'start_cp':0,'end_cp':1,'selected_text':'a','text':'错误'}
    assert post(client,write_headers,'manuscript/replace',body).status_code==409
    body.update(expected_revision_no=1,selected_text='b')
    assert post(client,write_headers,'manuscript/replace',body).status_code==422
    assert client.get('/api/manuscript').json()['text']=='abcd'


def test_lines_and_full_edit(client,write_headers):
    post(client,write_headers,'manuscript/import',{'expected_revision_no':0,'text':'甲\n😀\n尾'})
    r=post(client,write_headers,'manuscript/line-range',{'expected_revision_no':1,'first_line':2,'last_line':2})
    assert r.json()['selected_text']=='😀\n'
    assert (r.json()['start_cp'],r.json()['end_cp'])==(2,4)
    r=post(client,write_headers,'manuscript/edit',{'expected_revision_no':1,'text':'甲\n😀!!\n尾'})
    assert r.json()['text']=='甲\n😀!!\n尾'
    assert len(client.get('/api/revisions').json()['revisions'])==2


@pytest.mark.parametrize('before,after', [('abc','abc'),('😀ab','😀Xab'),('ab',''),('','ab'),('甲\n\n尾','甲\n\n末尾')])
def test_minimal_change(before,after):
    start,end,text=minimal_change(before,after)
    assert before[:start]+text+before[end:]==after


@pytest.mark.parametrize('extra', [{'expected_revision_no':True}, {'text':99}, {'invented':[]}])
def test_strict_requests(client,write_headers,extra):
    data={'expected_revision_no':0,'text':'文本',**extra}
    assert post(client,write_headers,'manuscript/import',data).status_code==422


def test_text_endpoint_needs_write_token(client):
    assert client.post('/api/manuscript/import',json={'text':'不能写','expected_revision_no':0}).status_code==403


def test_body_size_header_guard(client,write_headers):
    r=client.post('/api/manuscript/import',content='{}',headers={**write_headers,'Content-Length':str(40*1024*1024)})
    assert r.status_code==413


def test_xss_is_data_not_html(client,write_headers):
    text='<script>window.bad=1</script>\n</textarea><img src=x onerror=alert(1)>'
    r=post(client,write_headers,'manuscript/import',{'expected_revision_no':0,'text':text})
    assert r.json()['text']==text
    assert 'window.bad' not in client.get('/manuscript').text
