"""Idea-only initialization: an editable proposal, then one explicit transaction.

LLM-generated names are resolved locally. A proposal cannot add a manuscript
block, an event or a dated state; tentative plot ideas remain uncertain threads.
No proposal is applied while the model is running, or by merely saving a draft.
"""
from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import uuid4

from pydantic import Field, StringConstraints
from .analysis import compact
from .domain import utc_now
from .knowledge import normalize_name
from .llm import LLMClient, LLMError, request_token_estimate
from .manuscript import ManuscriptError, RevisionConflictError
from .settings import Strict, Name, Kind, FieldName, Value, Profile, Entry, entry_slot
from .structured_llm import StructuredLLM, StructuredError, strict_loads


class SeedFact(Strict):
    field: FieldName
    value: Value


class SeedEntity(Strict):
    name: Name
    kind: Kind
    aliases: list[Name] = Field(max_length=16)
    facts: list[SeedFact] = Field(max_length=16)


class SeedRelationship(Strict):
    a: Name
    b: Name
    label: Name
    description: Value


class SeedThread(Strict):
    title: Name
    description: Value
    related_entities: list[Name] = Field(max_length=16)


class IdeaResult(Strict):
    premise_summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
    entities: list[SeedEntity] = Field(max_length=40)
    relationships: list[SeedRelationship] = Field(max_length=80)
    threads: list[SeedThread] = Field(max_length=40)
    style_notes: list[Value] = Field(max_length=16)


class IdeaRequest(Strict):
    idea_text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)]
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_tokens: int = Field(default=3072, ge=256, le=8192)


class IdeaResume(Strict):
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_proposal_version: int = Field(ge=0)


class ProposalDraft(Strict):
    expected_proposal_version: int = Field(ge=0)
    draft: IdeaResult


class ProposalAccept(ProposalDraft):
    acknowledge_incomplete: bool = False
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


def validate_seed(result: IdeaResult) -> None:
    """Entity names/aliases must resolve uniquely within this proposal."""
    owners = {}
    for index, entity in enumerate(result.entities):
        for name in [entity.name, *entity.aliases]:
            key = normalize_name(name)
            if key in owners and owners[key] != index:
                raise StructuredError("semantic", f"构思实体名称或别名不唯一：{name}")
            owners[key] = index
        fields = [normalize_name(f.field) for f in entity.facts]
        if len(set(fields)) != len(fields):
            raise StructuredError("semantic", f"构思人物 {entity.name} 的字段重复")
    seen_rel, seen_thread = set(), set()
    for rel in result.relationships:
        a, b = owners.get(normalize_name(rel.a)), owners.get(normalize_name(rel.b))
        if a is None or b is None or a == b:
            raise StructuredError("semantic", "构思关系必须引用两位已声明且不同的实体")
        key = a, b, normalize_name(rel.label)
        if key in seen_rel:
            raise StructuredError("semantic", "构思关系重复，请合并后保存")
        seen_rel.add(key)
    for thread in result.threads:
        if any(normalize_name(n) not in owners for n in thread.related_entities):
            raise StructuredError("semantic", "构思线索引用了未声明实体")
        key = normalize_name(thread.title)
        if key in seen_thread:
            raise StructuredError("semantic", "构思线索标题重复")
        seen_thread.add(key)


def build_idea_messages(text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content":
         "你是小说构思助手。将用户的创意扩充为可编辑的初始设定，不要写正文，"
         "不要宣称任何事件已经发生。用户明确的限制优先，缺失内容可提出建议。"
         "仅输出一个符合 Schema 的 JSON 对象，所有顶层字段必须出现，没内容用空数组。"
         "实体名称和别名必须唯一；关系和线索仅引用本次 entities 声明的人物或地点。"
         "不要生成数据库 ID；facts 是初始设定，不是发生在后期的事实。"
         "最多建议 6 个实体、每人 4 个事实、6 条关系和 4 条潜在线索，保持简洁。"},
        {"role": "user", "content": compact({"idea": text, "schema": IdeaResult.model_json_schema()})},
    ]


class IdeaService:
    def __init__(self, session):
        self.session = session
        session.store.connection.execute(
            "UPDATE idea_proposals SET status='interrupted',error=?,updated_at=? WHERE status='running'",
            ("程序退出时构思请求未完成；请重新生成", utc_now()))

    def _row_locked(self, pid: str):
        row = self.session.store.connection.execute("SELECT * FROM idea_proposals WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise ManuscriptError("构思提案不存在")
        return row

    def _stale_locked(self, row) -> bool:
        s = self.session
        s.knowledge.refresh_locked()
        return row["base_revision_no"] != s.manuscript.revision_no or row["base_setting_version"] != s.knowledge.version

    def _view_locked(self, row, *, detail=True):
        d = {k: row[k] for k in ("id", "status", "version", "base_revision_no", "base_setting_version", "error", "created_at", "updated_at")}
        d["stale"] = row["status"] != "accepted" and self._stale_locked(row)
        d["requires_review"] = bool(row["requires_review"])
        d["quality"] = strict_loads(row["quality_json"])
        from .small_model import rows_for_owner
        steps = (rows_for_owner(self.session, "idea", row["id"]) if detail else
                 [dict(r) for r in self.session.store.connection.execute("SELECT step_key,status,task_label FROM model_steps WHERE owner_type='idea' AND owner_id=? ORDER BY rowid",(row['id'],))])
        latest_steps=list({r['step_key']:r for r in steps}.values())
        d["step_progress"] = {"total":len(latest_steps), "completed":sum(r['status']=='complete' for r in latest_steps),
                              "current":next((r['task_label'] for r in reversed(steps) if r['status']=='running'), None)}
        if detail:
            d["steps"] = steps
        if detail:
            d["idea_text"] = row["idea_text"]
            for name in ("result", "draft"):
                raw = row[name+"_json"]
                if raw is None:
                    d[name] = None
                    continue
                try:
                    obj = IdeaResult.model_validate(strict_loads(raw), strict=True)
                    validate_seed(obj)
                    d[name] = obj.model_dump()
                except (ValueError, StructuredError, TypeError) as exc:
                    raise ManuscriptError("构思记录无法通过磁盘数据校验，未写入设定") from exc
            for name in ("llm_run_ids", "repairs"):
                d[name] = strict_loads(row[name+"_json"])
        return d

    async def get(self, pid: str):
        async with self.session.lock:
            return self._view_locked(self._row_locked(pid))

    async def list(self):
        s = self.session
        async with s.lock:
            return [self._view_locked(r, detail=False) for r in s.store.connection.execute(
                "SELECT * FROM idea_proposals ORDER BY rowid DESC LIMIT 100")]

    async def resume(self, pid: str, body: IdeaResume, *, transport=None):
        async with self.session.lock:
            row=self._row_locked(pid)
            if self.session.project.data.config.analysis_protocol != 'small':
                raise ManuscriptError('逐项恢复只用于小问题模式；旧严格提案请重新生成')
            quality=strict_loads(row['quality_json'])
            request=IdeaRequest(idea_text=row['idea_text'],expected_version=body.expected_version,
                max_tokens=quality.get('requested_output_tokens',self.session.project.data.config.small_output_tokens))
        return await self.generate(request,transport=transport,_resume_id=pid,_resume_version=body.expected_proposal_version)

    async def generate(self, body: IdeaRequest, *, transport=None, _resume_id=None, _resume_version=None):
        s = self.session
        if s.model_gate.locked() or s.jobs.busy or s.generation.busy or s.consistency.busy:
            raise LLMError("busy", "模型正在执行其他任务，请先等待或暂停它")
        async with s.model_gate:
            async with s.lock:
                s.settings._check_locked(body.expected_version)
                if s.manuscript.text:
                    raise ManuscriptError("初始构思入口只用于空正文项目；已有小说请在设定页编辑")
                config = s.project.data.config
                if not config.analysis_model:
                    raise LLMError("configuration", "请先填写分析模型名称")
                messages = build_idea_messages(body.idea_text)
                if config.analysis_protocol == "strict" and request_token_estimate(messages)+body.max_tokens > int(config.context_window*config.context_safety_ratio):
                    raise LLMError("context_budget", "构思想法、Schema 与输出预留超过预算，请缩短想法或减少输出预留")
                pid, base, now = _resume_id or uuid4().hex, s.manuscript.revision_no, utc_now()
                if _resume_id:
                    row=self._row_locked(pid)
                    if row['status'] in {'running','accepted'} or row['version'] != _resume_version or self._stale_locked(row):
                        raise RevisionConflictError('提案已运行、确认或改变，请刷新后核对')
                    if row['draft_json'] != row['result_json']:
                        raise ManuscriptError('提案已有人工编辑，不会为恢复任务覆盖它。请保留草稿或重新生成新提案')
                    from .analysis_jobs import model_signature
                    from .domain import ProjectConfig
                    if model_signature(ProjectConfig.model_validate_json(row['config_json'])) != model_signature(config):
                        raise ManuscriptError('模型/小问题配置已改变，请新建提案，避免复用不同配置的结果')
                    s._commit_snapshot_locked(extra=lambda c:c.execute("UPDATE idea_proposals SET status='running',error=NULL,version=version+1,updated_at=? WHERE id=?",(now,pid)))
                else:
                    s._commit_snapshot_locked(extra=lambda c: c.execute("""INSERT INTO idea_proposals
                        (id,idea_text,base_revision_no,base_setting_version,config_json,status,created_at,updated_at,quality_json)
                        VALUES(?,?,?,?,?,'running',?,?,?)""", (pid, body.idea_text, base, body.expected_version,
                        compact(config.model_dump()), now, now, compact({'complete':False,'requested_output_tokens':body.max_tokens}))))
            try:
                async with LLMClient(config, on_run=s.record_llm_run, transport=transport) as client:
                    if config.analysis_protocol == "small":
                        from .small_workflows import expand_small
                        async def progress(value, quality):
                            async with s.lock:
                                # Publish only a valid partial proposal, never canonical settings.
                                s._commit_snapshot_locked(extra=lambda c:c.execute("""UPDATE idea_proposals SET
                                    result_json=?,draft_json=?,quality_json=?,requires_review=1,updated_at=? WHERE id=? AND status='running'""",
                                    (value.model_dump_json(),value.model_dump_json(),compact({**quality,'complete':False,'running':True,'requested_output_tokens':body.max_tokens}),utc_now(),pid)))
                        result = await expand_small(s,client,body.idea_text,pid,body.max_tokens,progress)
                    else:
                        result = await StructuredLLM(client, on_validation=s.record_validation).call(
                            messages=messages, schema=IdeaResult, model=config.analysis_model,
                            temperature=config.analysis_temperature, max_tokens=body.max_tokens,
                            purpose="idea_expansion", semantic_validator=validate_seed)
                async with s.lock:
                    status = "stale" if self._stale_locked(self._row_locked(pid)) else "ready"
                    s._commit_snapshot_locked(extra=lambda c: c.execute("""UPDATE idea_proposals SET
                        status=?,version=version+1,result_json=?,draft_json=?,llm_run_ids_json=?,repairs_json=?,requires_review=?,updated_at=?,quality_json=?
                        WHERE id=?""", (status, result.value.model_dump_json(), result.value.model_dump_json(),
                        compact(result.run_ids), compact(result.repairs), int(result.requires_review), utc_now(), compact({**getattr(result,'quality',{}),'requested_output_tokens':body.max_tokens}),pid)))
            except asyncio.CancelledError:
                await self._fail(pid, "interrupted", "请求取消；模型服务端可能仍在计算")
                raise
            except Exception as exc:
                error = str(exc) if isinstance(exc, (LLMError, StructuredError, ManuscriptError)) else "构思失败；请检查磁盘和日志"
                await self._fail(pid, "failed", error)
                raise
        return await self.get(pid)

    async def _fail(self, pid, status, error):
        s = self.session
        async with s.lock:
            s._commit_snapshot_locked(extra=lambda c: c.execute(
                "UPDATE idea_proposals SET status=?,error=?,updated_at=? WHERE id=? AND status='running'",
                (status, error[:2000], utc_now(), pid)))

    def _editable_locked(self, pid, version):
        row = self._row_locked(pid)
        if row["status"] not in {"ready", "stale", "failed", "interrupted"} or row['draft_json'] is None:
            raise ManuscriptError("此构思不是可编辑的提案")
        if row["version"] != version:
            raise RevisionConflictError("构思草稿已在另一页面修改，请刷新后重试")
        return row

    async def save_draft(self, pid: str, body: ProposalDraft):
        validate_seed(body.draft)
        s = self.session
        async with s.lock:
            self._editable_locked(pid, body.expected_proposal_version)
            s._commit_snapshot_locked(extra=lambda c: c.execute(
                "UPDATE idea_proposals SET draft_json=?,version=version+1,updated_at=? WHERE id=?",
                (body.draft.model_dump_json(), utc_now(), pid)))
        return await self.get(pid)

    def _plan_locked(self, result: IdeaResult):
        """Build a complete all-or-nothing human settings batch without mutations."""
        s = self.session
        profiles, entries, names = {}, {}, {}
        existing = s.knowledge.graph["entities"]
        for entity in result.entities:
            match = {e["id"] for e in existing if e["kind"] == entity.kind and
                     any(normalize_name(entity.name) == normalize_name(n) for n in e["names"])}
            if len(match) > 1:
                raise ManuscriptError("已有实体名称不唯一，请先在设定页消歧")
            eid = next(iter(match)) if match else uuid4().hex
            old = s.settings.profiles.get(eid)
            if old and old.active:
                profile = old  # Do not change an existing human name/aliases/notes.
            else:
                profile = Profile(id=eid, name=entity.name, kind=entity.kind, aliases=entity.aliases)
                proposed_names = {normalize_name(n) for n in [profile.name, *profile.aliases]}
                for other in [*s.settings.profiles.values(), *profiles.values()]:
                    if other.id != eid and other.active and other.kind == profile.kind and proposed_names & {normalize_name(n) for n in [other.name, *other.aliases]}:
                        raise ManuscriptError("构思别名与已有人工档案冲突；请修改提案后再确认")
                profiles[eid] = profile
            for name in [entity.name, *entity.aliases]:
                names[normalize_name(name)] = eid
            for fact in entity.facts:
                entry = Entry(id=uuid4().hex, kind="fact", payload={"entity_id":eid, **fact.model_dump()})
                entries[entry.id] = entry
        # Batch references must not collapse two proposed endpoints onto one person.
        for rel in result.relationships:
            a,b = names[normalize_name(rel.a)], names[normalize_name(rel.b)]
            if a == b:
                raise ManuscriptError("两个构思名字匹配了同一人物，不能建立自我关系")
            e = Entry(id=uuid4().hex, kind="relationship", payload={"a":a,"b":b,"label":rel.label,"description":rel.description})
            entries[e.id] = e
        for thread in result.threads:
            e = Entry(id=uuid4().hex, kind="thread", payload={"title":thread.title,"description":thread.description,
                "status":"uncertain", "related_entities":list(dict.fromkeys(names[normalize_name(n)] for n in thread.related_entities))})
            entries[e.id] = e
        for kind,text in [("premise",result.premise_summary), *[("style",t) for t in result.style_notes]]:
            e=Entry(id=uuid4().hex,kind="note",payload={"kind":kind,"text":text})
            entries[e.id]=e
        slots = {entry_slot(e): e for e in s.settings.entries.values() if e.active}
        final = {}
        for e in entries.values():
            slot = entry_slot(e)
            old = slots.get(slot)
            if old:
                if old.payload != e.payload:
                    raise ManuscriptError("构思与已有人工字段/关系/线索冲突；请修改提案或先编辑已有设定。未部分写入。")
                continue
            slots[slot] = e
            final[e.id] = e
        return profiles, final

    async def accept(self, pid: str, body: ProposalAccept):
        validate_seed(body.draft)
        s = self.session
        async with s.lock:
            row = self._editable_locked(pid, body.expected_proposal_version)
            if not strict_loads(row['quality_json']).get('complete', True) and not body.acknowledge_incomplete:
                raise ManuscriptError("提案含失败/不完整小步骤；请人工补齐并明确确认接受不完整提案，或重试缺失步骤")
            s.settings._check_locked(body.expected_version)
            if self._stale_locked(row) or s.manuscript.text:
                raise RevisionConflictError("提案生成后正文或设定已改变，请核对当前设定并重新生成；草稿仍保留")
            profiles, entries = self._plan_locked(body.draft)
            after = {"proposal_id":pid,"profiles":[p.model_dump() for p in profiles.values()],
                     "entries":[e.model_dump() for e in entries.values()]}
            def apply(conn):
                for p in profiles.values():
                    conn.execute("INSERT INTO setting_entities VALUES(?,?) ON CONFLICT(id) DO UPDATE SET record_json=excluded.record_json", (p.id,p.model_dump_json()))
                for e in entries.values():
                    conn.execute("INSERT INTO setting_entries VALUES(?,?,?,?)", (e.id,entry_slot(e),1,e.model_dump_json()))
                conn.execute("UPDATE idea_proposals SET status='accepted',draft_json=?,version=version+1,updated_at=? WHERE id=?",
                    (body.draft.model_dump_json(),utc_now(),pid))
            s.settings._persist_locked("accept_idea",pid,None,after,apply)
            s.settings.profiles.update(profiles)
            s.settings.entries.update(entries)
        return {"proposal":await self.get(pid), "settings":await s.knowledge.view()}
