"""Human-owned settings overlays, stable anchors, and optimistic edit guards.

An analysis only changes observations. Profiles/entries below are explicit user
edits, each persisted atomically with an audit record. In-memory copies are
published only after commit. Disabling an overlay restores automatic information.
"""
from __future__ import annotations
from hashlib import sha256
import json
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator, ValidationError
from .analysis import compact
from .domain import utc_now
from .knowledge import normalize_name
from .manuscript import ManuscriptError, RevisionConflictError

ID = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
FieldName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Value = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Kind = Literal["character", "location", "organization", "item", "setting"]
EntryKind = Literal["fact", "state", "relationship", "event", "thread", "note"]


class Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_unicode(self):
        from .manuscript import check_text
        def visit(value):
            if isinstance(value, str): check_text(value)
            elif isinstance(value, list):
                for v in value: visit(v)
            elif isinstance(value, dict):
                for k,v in value.items(): visit(k); visit(v)
        visit(self.model_dump())
        return self


class Profile(Strict):
    id: ID
    name: Name
    kind: Kind
    aliases: list[Name] = Field(default_factory=list, max_length=64)
    notes: str = Field(default="", max_length=12000)
    active: bool = True


class FactPayload(Strict):
    entity_id: ID
    field: FieldName
    value: Value


class StatePayload(FactPayload):
    operation: Literal["set", "add", "remove"] = "set"


class RelationshipPayload(Strict):
    a: ID
    b: ID
    label: Name
    description: Value

    @model_validator(mode="after")
    def distinct(self):
        if self.a == self.b:
            raise ValueError("关系的两端不能是同一个实体")
        return self


class EventPayload(Strict):
    summary: Value
    participants: list[ID] = Field(default_factory=list, max_length=64)
    story_time: str | None = Field(default=None, max_length=160)
    story_order: float | None = None


class ThreadPayload(Strict):
    title: Name
    description: Value
    status: Literal["open", "resolved", "abandoned", "uncertain"] = "open"
    related_entities: list[ID] = Field(default_factory=list, max_length=64)


class NotePayload(Strict):
    kind: Literal["premise", "planning", "style", "summary", "pov", "theme", "motif", "misc"]
    text: str = Field(min_length=1, max_length=20000)


PAYLOADS = {"fact": FactPayload, "state": StatePayload, "relationship": RelationshipPayload,
            "event": EventPayload, "thread": ThreadPayload, "note": NotePayload}


class Anchor(Strict):
    block_id: ID
    offset: int = Field(ge=0)


class Entry(Strict):
    id: ID
    kind: EntryKind
    payload: dict[str, Any]
    anchor: Anchor | None = None
    replaces: list[ID] = Field(default_factory=list, max_length=256)
    active: bool = True

    @model_validator(mode="after")
    def valid_payload(self):
        PAYLOADS[self.kind].model_validate(self.payload, strict=True)
        if self.kind == "event" and self.anchor is None:
            raise ValueError("已发生事件必须定位到正文；未发生的计划请保存为规划笔记")
        return self


class Versioned(Strict):
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProfileWrite(Versioned):
    name: Name
    kind: Kind
    aliases: list[Name] = Field(default_factory=list, max_length=64)
    notes: str = Field(default="", max_length=12000)
    active: bool = True


class EntryWrite(Versioned):
    kind: EntryKind
    payload: dict[str, Any]
    at_cp: int | None = Field(default=None, ge=0)
    replaces: list[ID] = Field(default_factory=list, max_length=256)
    active: bool = True


class ReviewWrite(Versioned):
    observation_ids: list[ID] = Field(min_length=1, max_length=512)
    decision: Literal["pending", "accepted", "rejected"]


def anchor_at(doc, at_cp: int | None) -> Anchor | None:
    if at_cp is None:
        return None
    if not doc.blocks or not 0 <= at_cp <= len(doc.text):
        raise ManuscriptError("定位超出当前正文；空正文不能定位事件")
    cursor = 0
    for block in doc.blocks:
        end = cursor + len(block.text)
        # End boundary belongs to preceding block, stable across later append.
        if at_cp <= end:
            return Anchor(block_id=block.id, offset=at_cp-cursor)
        cursor = end
    raise ManuscriptError("无法解析正文定位")


def anchor_position(doc, anchor: Anchor | None) -> int | None:
    if anchor is None:
        return 0
    at = 0
    for b in doc.blocks:
        if b.id == anchor.block_id:
            return at+anchor.offset if anchor.offset <= len(b.text) else None
        at += len(b.text)
    return None


def entry_slot(entry: Entry) -> str:
    p = entry.payload
    scope = entry.anchor.model_dump() if entry.anchor else None
    if entry.kind in {"fact", "state"}:
        key = [entry.kind, p["entity_id"], normalize_name(p["field"]), scope]
        if entry.kind == "state" and p.get("operation", "set") != "set":
            key.extend([p["operation"], p["value"]])
    elif entry.kind == "relationship":
        key = [entry.kind, p["a"], p["b"], normalize_name(p["label"]), scope]
    elif entry.kind == "thread":
        key = [entry.kind, normalize_name(p["title"]), scope]
    else:
        key = [entry.kind, entry.id]  # Multiple notes/events at one position are legitimate.
    return sha256(compact(key).encode()).hexdigest()


class SettingsService:
    def __init__(self, session):
        self.session = session
        self.profiles: dict[str, Profile] = {}
        self.entries: dict[str, Entry] = {}
        self.load_errors: list[str] = []
        self.version = session.store.connection.execute("SELECT COALESCE(MAX(version),0) FROM setting_changes").fetchone()[0]
        for table, schema, target in (("setting_entities", Profile, self.profiles), ("setting_entries", Entry, self.entries)):
            for row in session.store.connection.execute(f"SELECT * FROM {table}"):
                try:
                    obj = schema.model_validate_json(row["record_json"])
                    if obj.id != row["id"]:
                        raise ValueError("ID 不匹配")
                    if table == "setting_entries" and (entry_slot(obj) != row["slot"] or obj.active != bool(row["active"])):
                        raise ValueError("索引不匹配")
                    target[obj.id] = obj
                except (ValueError, TypeError):
                    self.load_errors.append(f"人工设定 {row['id']} 无法载入；已隔离，不影响正文")

    def _check_locked(self, version: str):
        self.session.knowledge.refresh_locked()
        if self.session.knowledge.version != version:
            raise RevisionConflictError("设定或正文已经变化，请刷新后再编辑，未覆盖较新的内容")

    def _persist_locked(self, action: str, target_id: str, before, after, apply):
        next_version = self.version+1
        def persist(conn):
            apply(conn)
            conn.execute("INSERT INTO setting_changes VALUES(?,?,?,?,?,?)",
                         (next_version, action, target_id, compact(before) if before is not None else None,
                          compact(after), utc_now()))
        self.session._commit_snapshot_locked(extra=persist)
        self.version = next_version

    async def put_profile(self, body: ProfileWrite, pid: str | None = None) -> dict:
        s = self.session
        async with s.lock:
            self._check_locked(body.expected_version)
            graph = s.knowledge.graph
            if pid is not None and pid not in self.profiles and not any(e["id"] == pid for e in graph["entities"]):
                raise ManuscriptError("实体不存在")
            profile = Profile(id=pid or uuid4().hex, **body.model_dump(exclude={"expected_version"}))
            if profile.active:
                names = {normalize_name(n) for n in [profile.name, *profile.aliases]}
                for other in self.profiles.values():
                    if other.id != profile.id and other.active and other.kind == profile.kind:
                        if names & {normalize_name(n) for n in [other.name, *other.aliases]}:
                            raise ManuscriptError("名称或别名已被另一份人工档案占用；请先调整该档案")
            old = self.profiles.get(profile.id)
            self._persist_locked("profile", profile.id, old.model_dump() if old else None, profile.model_dump(),
                lambda c: c.execute("INSERT INTO setting_entities VALUES(?,?) ON CONFLICT(id) DO UPDATE SET record_json=excluded.record_json",
                                    (profile.id, profile.model_dump_json())))
            self.profiles[profile.id] = profile
        return await s.knowledge.view()

    async def put_entry(self, body: EntryWrite, eid: str | None = None) -> dict:
        s = self.session
        async with s.lock:
            self._check_locked(body.expected_version)
            if eid is not None and eid not in self.entries:
                raise ManuscriptError("人工条目不存在；自动观察需要另建人工覆盖")
            anchor = anchor_at(s.manuscript, body.at_cp)
            try:
                entry = Entry(id=eid or uuid4().hex, kind=body.kind, payload=body.payload,
                              anchor=anchor, replaces=body.replaces, active=body.active)
            except ValidationError as exc:
                raise ManuscriptError("人工条目格式不正确：" + "; ".join(e["msg"] for e in exc.errors(include_input=False))) from exc
            ids = {e["id"] for e in s.knowledge.graph["entities"]}
            p = entry.payload
            refs = ([p["entity_id"]] if entry.kind in {"fact", "state"} else
                    [p["a"], p["b"]] if entry.kind == "relationship" else
                    p.get("participants", p.get("related_entities", [])))
            if entry.active and any(ref not in ids for ref in refs):
                raise ManuscriptError("条目引用的实体不存在，请先建立或恢复实体档案")
            current = {r["id"] for r in s.knowledge.records}
            previous_sources = set(self.entries[eid].replaces) if eid in self.entries else set()
            if entry.active and any(ref not in current | previous_sources for ref in entry.replaces):
                raise ManuscriptError("被覆盖的观察不是当前有效结果，请刷新后重选")
            if entry.active and any(e.active and e.id != entry.id and entry_slot(e) == entry_slot(entry) for e in self.entries.values()):
                raise ManuscriptError("同一位置已有该字段的人工设定，请编辑已有条目")
            old = self.entries.get(entry.id)
            pinned = {}
            for ref in (refs if entry.active else []):
                if ref not in self.profiles or not self.profiles[ref].active:
                    entity = next(x for x in s.knowledge.graph["entities"] if x["id"] == ref)
                    pinned[ref] = Profile(id=ref, name=entity["name"], kind=entity["kind"],
                                          aliases=[n for n in entity["names"] if n != entity["name"]])
            def persist_entry(conn):
                for profile in pinned.values():
                    conn.execute("INSERT INTO setting_entities VALUES(?,?) ON CONFLICT(id) DO UPDATE SET record_json=excluded.record_json", (profile.id, profile.model_dump_json()))
                conn.execute("INSERT INTO setting_entries VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET slot=excluded.slot,active=excluded.active,record_json=excluded.record_json",
                             (entry.id, entry_slot(entry), int(entry.active), entry.model_dump_json()))
            self._persist_locked("entry", entry.id, old.model_dump() if old else None,
                                 {"entry": entry.model_dump(), "pinned_profiles": [p.model_dump() for p in pinned.values()]}, persist_entry)
            self.profiles.update(pinned)
            self.entries[entry.id] = entry
        return await s.knowledge.view()

    async def disable_entry(self, eid: str, version: str) -> dict:
        s = self.session
        async with s.lock:
            self._check_locked(version)
            old = self.entries.get(eid)
            if old is None:
                raise ManuscriptError("人工条目不存在")
            entry = old.model_copy(update={"active": False})
            self._persist_locked("disable_entry", eid, old.model_dump(), entry.model_dump(), lambda c: c.execute(
                "UPDATE setting_entries SET active=0,record_json=? WHERE id=?", (entry.model_dump_json(), eid)))
            self.entries[eid] = entry
        return await s.knowledge.view()

    async def review(self, body: ReviewWrite) -> dict:
        s = self.session
        async with s.lock:
            self._check_locked(body.expected_version)
            # Only currently source-validated observations may be reviewed.
            # Reused originals retain their ids even when the plan/revision changes.
            placeholders = ",".join("?" for _ in body.observation_ids)
            rows = s.store.connection.execute(f"""SELECT o.id,o.status,r.plan_id,r.base_revision_no,r.status AS run_status
                FROM observations o JOIN analysis_runs r ON r.id=o.run_id WHERE o.id IN ({placeholders})""",
                body.observation_ids).fetchall()
            current_ids = {r["id"] for r in s.knowledge.records}
            if any(r["id"] not in current_ids for r in rows) or len(rows) != len(set(body.observation_ids)) or any(r["run_status"] != "done" for r in rows):
                raise ManuscriptError("只能审核当前原文仍有效的成功观察")
            def apply(conn):
                conn.executemany("UPDATE observations SET status=? WHERE id=?", [(body.decision, r["id"]) for r in rows])
            self._persist_locked("review", uuid4().hex, {r["id"]: r["status"] for r in rows},
                                 {r["id"]: body.decision for r in rows}, apply)
        return await s.knowledge.view()

    async def accept_all_pending(self, body: Versioned) -> dict:
        """Accept the current source-valid pending set in one audited transaction.

        The browser submits only the catalog version, not a paginated ID list.
        The project lock and version guard fix exactly the set the user saw:
        newer analysis requires a refresh rather than accepting unseen results.
        Superseded/invalid rows never enter knowledge.records. Rejections and
        manual overlays are untouched; a no-op creates no audit/version change.
        """
        s = self.session
        async with s.lock:
            self._check_locked(body.expected_version)
            pending = [r for r in s.knowledge.records if r["status"] == "pending"]
            count = len(pending)
            if pending:
                before = {r["id"]: "pending" for r in pending}
                after = {oid: "accepted" for oid in before}

                def apply(conn):
                    # executemany uses one parameter per statement, avoiding a
                    # giant IN (...) and the selected-review 512-ID API limit.
                    cursor = conn.executemany(
                        "UPDATE observations SET status='accepted' WHERE id=? AND status='pending'",
                        ((oid,) for oid in before),
                    )
                    if cursor.rowcount != count:
                        raise RevisionConflictError("观察已变化，批量接纳已全部回滚，请刷新后重试")

                self._persist_locked("review_all_pending", uuid4().hex, before, after, apply)
        result = await s.knowledge.view()
        result["bulk_review"] = {"accepted_count": count}
        return result

    def entry_views_locked(self):
        return [{**e.model_dump(), "at_cp": anchor_position(self.session.manuscript, e.anchor),
                 "anchor_stale": e.anchor is not None and anchor_position(self.session.manuscript, e.anchor) is None}
                for e in self.entries.values()]
