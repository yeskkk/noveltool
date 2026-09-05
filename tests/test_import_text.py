import codecs
import pytest
from noveltool.import_text import decode_import,heading_candidates
from noveltool.manuscript import ManuscriptError

@pytest.mark.parametrize('encoding,auto', [('utf-8',True),('utf-8-sig',True),('utf-16',True),('utf-16-le',False),('utf-16-be',False),('gb18030',False)])
def test_encoding_preserves_original_bytes_and_normalizes(encoding,auto):
    text='第一章\r\n\r\n　甲把钥匙交给乙。\r结尾\r\n'
    raw=text.encode(encoding)
    p=decode_import(raw,'小说.txt','auto' if auto else encoding)
    assert p.raw==raw and p.text=='第一章\n\n　甲把钥匙交给乙。\n结尾\n'
    assert len(p.raw_hash)==64 and len(p.normalized_hash)==64
    assert p.view()['headings'][0]['line']==1

@pytest.mark.parametrize('raw,encoding',[(b'', 'auto'),(b'\xffbad','auto'),(b'\x00file','utf-8'),(b'\x01abc','utf-8'),(b'  \n','auto'),('小说'.encode('utf-32'),'auto'),('中文'.encode('gb18030'),'auto'),(b'abc','wrong')])
def test_invalid_imports_rejected(raw,encoding):
    with pytest.raises(ManuscriptError):decode_import(raw,'a.txt',encoding)

@pytest.mark.parametrize('name',['','a\n.txt','a'*256,'\ud800'])
def test_filename_validation(name):
    with pytest.raises(ManuscriptError):decode_import(b'abc',name)

def test_filename_is_metadata_not_path():
    p=decode_import(b'abc','../../other.txt')
    assert p.filename=='../../other.txt' # never opened as a path

def test_limits_modes_and_bounded_preview():
    with pytest.raises(ManuscriptError):decode_import(b'a'*(20*1024*1024+1),'a.txt')
    with pytest.raises(ManuscriptError):decode_import(b'a','a.txt',mode='wrong')
    p=decode_import(('文字\n'*5000).encode(),'large.txt')
    assert p.view()['preview_truncated'] and len(p.view()['preview_text'])==6000
    assert len(heading_candidates(('第一章\n'*500)))==200


def test_ambiguous_bytes_require_human_encoding_choice():
    raw='小说'.encode('gb18030')
    # These particular bytes are ALSO valid UTF-8. No heuristic can prove the author's intent.
    assert decode_import(raw,'ambiguous.txt').text!='小说'
    assert decode_import(raw,'ambiguous.txt','gb18030').text=='小说'
