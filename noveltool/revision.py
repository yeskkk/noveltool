"""Plan first, commit once, publish after COMMIT. Undo is an inverse revision.

Only the current blocks are hot. Old blocks and the undo target are fetched on
request. Revisions and block text are immutable; reactivation changes seq only.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
import json
import sqlite3
from uuid import uuid4

from .db import InvalidProjectError, SaveConflictError
from .domain import utc_now
from .manuscript import (Block, Manuscript, ManuscriptError, RevisionConflictError,
                         check_text, split_text, text_hash, validate_range)


@dataclass(frozen=True, slots=True)
class Revision:
    id: str
    revision_no: int
    kind: str
    undo_target_id: str | None
    splice_start: int
    old_ids: tuple[str, ...]
    new_ids: tuple[str, ...]
    before_hash: str
    after_hash: str
    selection_start: int | None
    selection_end: int | None
    instruction: str
    created_at: str


@dataclass(frozen=True, slots=True)
class RevisionPlan:
    revision: Revision
    manuscript: Manuscript
    inserted_blocks: tuple[Block, ...]


def _plan(state: Manuscript, *, kind: str, offset: int, remove: int,
          added: tuple[Block, ...], revision_id: str, instruction: str = "",
          start: int | None = None, end: int | None = None,
          undo_target: str | None = None, inserted: tuple[Block, ...] | None = None) -> RevisionPlan:
    blocks = state.blocks[:offset] + added + state.blocks[offset + remove:]
    updated = Manuscript(state.revision_no + 1, blocks)
    check_text(updated.text)
    revision = Revision(
        revision_id, updated.revision_no, kind, undo_target, offset,
        tuple(b.id for b in state.blocks[offset:offset + remove]), tuple(b.id for b in added),
        text_hash(state.text), text_hash(updated.text), start, end, instruction, utc_now(),
    )
    return RevisionPlan(revision, updated, added if inserted is None else inserted)


class RevisionEngine:
    @staticmethod
    def import_text(state: Manuscript, text: str, expected: int, mode: str = "auto") -> RevisionPlan:
        state.check_revision(expected)
        if state.blocks:
            raise ManuscriptError("导入只允许用于空正文；不能覆盖已有小说")
        if not text or not text.strip():
            raise ManuscriptError("导入正文不能为空")
        rid = uuid4().hex
        blocks = tuple(Block.new(p, rid) for p in split_text(text, mode))
        return _plan(state, kind="import", offset=0, remove=0, added=blocks, revision_id=rid)

    @staticmethod
    def append(state: Manuscript, text: str, expected: int) -> RevisionPlan:
        state.check_revision(expected)
        check_text(text)
        if not text.strip():
            raise ManuscriptError("追加正文不能为空")
        # Append never retires the previous paragraph. Any required separator
        # belongs to the new blocks and is therefore removed again by Undo.
        prefix = "" if not state.blocks or state.text.endswith("\n\n") else ("\n" if state.text.endswith("\n") else "\n\n")
        rid = uuid4().hex
        added = tuple(Block.new(p, rid) for p in split_text(prefix + text))
        return _plan(state, kind="append", offset=len(state.blocks), remove=0,
                     added=added, revision_id=rid)

    @staticmethod
    def replace(state: Manuscript, start: int, end: int, replacement: str,
                expected: int, instruction: str = "", kind: str = "manual_edit") -> RevisionPlan | None:
        state.check_revision(expected)
        rendered = state.render()
        validate_range(rendered.text, start, end)
        check_text(replacement)
        if kind not in {"rewrite", "manual_edit"}:
            raise ManuscriptError("不合法的替换操作")
        if rendered.text[start:end] == replacement:
            return None
        rid = uuid4().hex
        if not state.blocks:
            added = tuple(Block.new(p, rid) for p in split_text(replacement))
            return _plan(state, kind=kind, offset=0, remove=0, added=added,
                         revision_id=rid, start=start, end=end, instruction=instruction)
        first = next((i for i, s in enumerate(rendered.spans) if s.end_cp > start), len(state.blocks)-1)
        last = first if start == end else next(i for i, s in enumerate(rendered.spans) if s.end_cp >= end)
        left, right = rendered.spans[first].start_cp, rendered.spans[last].end_cp
        merged = rendered.text[left:start] + replacement + rendered.text[end:right]
        added = tuple(Block.new(p, rid) for p in split_text(merged))
        return _plan(state, kind=kind, offset=first, remove=last-first+1, added=added,
                     revision_id=rid, instruction=instruction, start=start, end=end)

    @staticmethod
    def undo(state: Manuscript, target: Revision, old_blocks: tuple[Block, ...], expected: int) -> RevisionPlan:
        state.check_revision(expected)
        current = tuple(b.id for b in state.blocks[target.splice_start:target.splice_start+len(target.new_ids)])
        if current != target.new_ids or text_hash(state.text) != target.after_hash:
            raise RevisionConflictError("撤销目标与当前正文不一致；拒绝破坏正文")
        if tuple(b.id for b in old_blocks) != target.old_ids:
            raise ManuscriptError("撤销历史块不完整")
        return _plan(state, kind="undo", offset=target.splice_start, remove=len(target.new_ids),
                     added=old_blocks, revision_id=uuid4().hex, undo_target=target.id, inserted=())


def _revision_from_row(row: sqlite3.Row) -> Revision:
    data = dict(row)
    for key in ("old_ids", "new_ids"):
        ids = json.loads(data.pop(key + "_json"))
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids) or len(set(ids)) != len(ids):
            raise InvalidProjectError("损坏的 revision block 引用")
        data[key] = tuple(ids)
    return Revision(**data)


def load_manuscript(conn: sqlite3.Connection) -> Manuscript:
    rows = conn.execute("SELECT * FROM manuscript_blocks WHERE seq IS NOT NULL ORDER BY seq").fetchall()
    for i, row in enumerate(rows):
        if row["seq"] != i or text_hash(row["text"]) != row["content_hash"]:
            raise InvalidProjectError("正文块顺序或内容哈希校验失败")
    latest = conn.execute("SELECT * FROM revisions ORDER BY revision_no DESC LIMIT 1").fetchone()
    blocks = tuple(Block(r["id"], r["text"], r["created_revision_id"]) for r in rows)
    state = Manuscript(latest["revision_no"] if latest else 0, blocks)
    if latest and latest["after_hash"] != text_hash(state.text):
        raise InvalidProjectError("当前正文与最后一次 revision 不匹配")
    if not latest and blocks:
        raise InvalidProjectError("正文缺少 revision")
    return state


def next_undo(conn: sqlite3.Connection) -> tuple[Revision, tuple[Block, ...]]:
    row = conn.execute("""SELECT r.* FROM revisions r WHERE r.kind <> 'undo'
        AND NOT EXISTS (SELECT 1 FROM revisions u WHERE u.undo_target_id = r.id)
        ORDER BY r.revision_no DESC LIMIT 1""").fetchone()
    if row is None:
        raise ManuscriptError("没有可撤销的正文修改")
    revision = _revision_from_row(row)
    blocks = []
    for bid in revision.old_ids:
        old = conn.execute("SELECT * FROM manuscript_blocks WHERE id=?", (bid,)).fetchone()
        if old is None or text_hash(old["text"]) != old["content_hash"]:
            raise InvalidProjectError("撤销所需的历史正文丢失或损坏")
        blocks.append(Block(old["id"], old["text"], old["created_revision_id"]))
    return revision, tuple(blocks)


def persist_plan(conn: sqlite3.Connection, plan: RevisionPlan) -> None:
    """Called INSIDE ProjectStore.flush transaction, never opens its own."""
    latest = conn.execute("SELECT revision_no,after_hash FROM revisions ORDER BY revision_no DESC LIMIT 1").fetchone()
    revision = plan.revision
    if ((latest["revision_no"] if latest else 0) != revision.revision_no - 1
            or (latest["after_hash"] if latest else text_hash("")) != revision.before_hash):
        raise SaveConflictError("磁盘正文版本冲突；提交已取消")
    data = asdict(revision)
    data["old_ids_json"] = json.dumps(data.pop("old_ids"))
    data["new_ids_json"] = json.dumps(data.pop("new_ids"))
    conn.execute(f"INSERT INTO revisions ({','.join(data)}) VALUES ({','.join('?' for _ in data)})", list(data.values()))
    # A single active-order index can be renumbered safely only after clearing it.
    conn.execute("UPDATE manuscript_blocks SET seq=NULL WHERE seq IS NOT NULL")
    for b in plan.inserted_blocks:
        conn.execute("INSERT INTO manuscript_blocks(id,text,content_hash,seq,created_revision_id) VALUES (?,?,?,NULL,?)",
                     (b.id, b.text, text_hash(b.text), b.created_revision_id))
    for i, b in enumerate(plan.manuscript.blocks):
        cur = conn.execute("UPDATE manuscript_blocks SET seq=? WHERE id=?", (i, b.id))
        if cur.rowcount != 1:
            raise InvalidProjectError("正文提交引用不存在的 block")


def revision_history(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    return [asdict(_revision_from_row(row)) for row in conn.execute(
        "SELECT * FROM revisions ORDER BY revision_no DESC LIMIT ?", (max(1, min(limit, 200)),))]
