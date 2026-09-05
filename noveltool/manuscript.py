"""Pure, lossless manuscript representation. No database, network or UI imports.

Block.text INCLUDES its following separators. Joining blocks never invents or
removes whitespace. The browser/import boundary may explicitly normalize CRLF;
the revision engine itself is lossless, including indentation and empty lines.
"""
from __future__ import annotations
from dataclasses import dataclass
from hashlib import sha256
import re
from typing import Literal
from uuid import uuid4

ParagraphMode = Literal["auto", "blankline", "line"]
MAX_TEXT_CHARS = 5_000_000


class ManuscriptError(ValueError):
    pass


class RevisionConflictError(ManuscriptError):
    pass


def check_text(text: str) -> None:
    if not isinstance(text, str):
        raise ManuscriptError("正文必须是字符串")
    if len(text) > MAX_TEXT_CHARS:
        raise ManuscriptError(f"正文最多 {MAX_TEXT_CHARS} 个 Unicode 字符")
    if "\x00" in text or any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        raise ManuscriptError("正文不能含 NUL 或未配对的 Unicode 代理字符")


def text_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def count_chars(text: str) -> int:
    return sum(not c.isspace() for c in text)


def split_text(text: str, mode: ParagraphMode = "auto") -> tuple[str, ...]:
    check_text(text)
    if mode not in {"auto", "blankline", "line"}:
        raise ManuscriptError("未知的分段方式")
    if mode == "auto":
        mode = "blankline" if re.search(r"\n[ \t\r]*\n", text) else "line"
    pattern = r"\n" if mode == "line" else r"\n(?:[ \t\r]*\n)+"
    pieces, start = [], 0
    for match in re.finditer(pattern, text):
        pieces.append(text[start:match.end()])
        start = match.end()
    if start < len(text):
        pieces.append(text[start:])
    return tuple(pieces)


@dataclass(frozen=True, slots=True)
class Block:
    id: str
    text: str
    created_revision_id: str

    @classmethod
    def new(cls, text: str, revision_id: str) -> Block:
        return cls(uuid4().hex, text, revision_id)


@dataclass(frozen=True, slots=True)
class BlockSpan:
    block_id: str
    start_cp: int
    end_cp: int


@dataclass(frozen=True, slots=True)
class RenderedManuscript:
    text: str
    spans: tuple[BlockSpan, ...]


@dataclass(frozen=True, slots=True)
class Manuscript:
    revision_no: int = 0
    blocks: tuple[Block, ...] = ()

    def render(self) -> RenderedManuscript:
        spans, at = [], 0
        for block in self.blocks:
            spans.append(BlockSpan(block.id, at, at + len(block.text)))
            at += len(block.text)
        return RenderedManuscript("".join(b.text for b in self.blocks), tuple(spans))

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.blocks)

    def check_revision(self, expected: int) -> None:
        if type(expected) is not int or expected != self.revision_no:
            raise RevisionConflictError("正文版本已变化；请重新载入，不能把旧选区应用到新正文")


def validate_range(text: str, start: int, end: int) -> None:
    if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(text):
        raise ManuscriptError("选区必须满足 0 ≤ start ≤ end ≤ 正文长度（Unicode code point）")


def range_for_lines(text: str, first: int, last: int) -> tuple[int, int]:
    # Physical LF lines, NOT the browser's width-dependent visual wraps.
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(starts):
        raise ManuscriptError(f"行范围无效；当前正文有 {len(starts)} 个逻辑行")
    return starts[first - 1], starts[last] if last < len(starts) else len(text)
