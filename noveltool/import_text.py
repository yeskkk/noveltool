"""Strict text decoding and previews. Raw bytes are preserved separately."""
from __future__ import annotations
import codecs
from dataclasses import dataclass
from hashlib import sha256
import re
from uuid import uuid4
from .manuscript import ManuscriptError, check_text, split_text, text_hash, count_chars

MAX_IMPORT_BYTES = 20 * 1024 * 1024
PREVIEW_TTL_SECONDS = 900
ENCODINGS = {'auto', 'utf-8', 'utf-8-sig', 'utf-16', 'utf-16-le', 'utf-16-be', 'gb18030', 'gbk'}
HEADING = re.compile(r'^(?:第[〇零一二三四五六七八九十百千万两0-9]+[章回节卷部].{0,70}|'
                     r'chapter\s+[0-9ivxlcdm]+\b.{0,70}|序章|序言|楔子|尾声|后记|终章)$', re.I)

@dataclass(frozen=True, slots=True)
class ImportPreview:
    id: str
    filename: str
    encoding: str
    paragraph_mode: str
    raw: bytes
    text: str
    raw_hash: str
    normalized_hash: str
    block_count: int

    def view(self) -> dict:
        # Deliberately bounded; a whole novel is never repeated into a JSON preview.
        return {'preview_id': self.id, 'filename': self.filename, 'encoding': self.encoding,
                'paragraph_mode': self.paragraph_mode, 'raw_bytes': len(self.raw),
                'raw_hash': self.raw_hash, 'normalized_hash': self.normalized_hash,
                'char_count': count_chars(self.text), 'block_count': self.block_count,
                'preview_text': self.text[:6000], 'preview_truncated': len(self.text)>6000,
                'headings': heading_candidates(self.text), 'expires_in_seconds': PREVIEW_TTL_SECONDS}


def heading_candidates(text: str) -> list[dict]:
    result=[]
    for number, line in enumerate(text.split("\n"), 1):
        stripped=line.strip()
        if len(stripped)<=80 and HEADING.fullmatch(stripped):
            result.append({'line': number, 'title': stripped})
            if len(result)>=200: break
    return result


def decode_import(raw: bytes, filename: str, encoding: str='auto', mode: str='auto') -> ImportPreview:
    if not isinstance(raw, bytes) or not raw or len(raw)>MAX_IMPORT_BYTES:
        raise ManuscriptError('TXT 必须非空，且不超过 20 MiB')
    if encoding not in ENCODINGS:
        raise ManuscriptError('不支持的编码；请选择列表中的编码')
    if not isinstance(filename,str) or not filename.strip() or len(filename)>255 or any(ord(c)<32 for c in filename):
        raise ManuscriptError('文件名不能为空、不能包含控制字符，最多 255 字符')
    check_text(filename)
    if raw.startswith((codecs.BOM_UTF32_LE,codecs.BOM_UTF32_BE)):
        raise ManuscriptError('不支持 UTF-32；请先转换为 UTF-8')
    actual=encoding
    if encoding=='auto':
        if raw.startswith(codecs.BOM_UTF8): actual='utf-8-sig'
        elif raw.startswith((codecs.BOM_UTF16_LE,codecs.BOM_UTF16_BE)): actual='utf-16'
        else: actual='utf-8'
    try:
        text=raw.decode(actual,errors='strict')
    except UnicodeError as exc:
        raise ManuscriptError(f'无法严格按 {actual} 解码；请明确选择原文件编码（例如 GB18030），不进行乱码替换') from exc
    text=text.removeprefix('\ufeff').replace('\r\n','\n').replace('\r','\n')
    check_text(text)
    if not text.strip(): raise ManuscriptError('导入文本不能只有空白')
    if any(ord(c)<32 and c not in '\n\t' for c in text):
        raise ManuscriptError('文件含非文本控制字符，请确认上传的是 TXT')
    blocks=split_text(text,mode)
    if len(blocks)>100_000: raise ManuscriptError('分段超过 100000 段，请改用空行分段或清理文件')
    return ImportPreview(uuid4().hex,filename,actual,mode,raw,text,
                         sha256(raw).hexdigest(),text_hash(text),len(blocks))
