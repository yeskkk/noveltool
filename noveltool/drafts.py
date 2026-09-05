"""Human draft buffers and the single atomic candidate-to-manuscript boundary.

Edits live in memory until the normal batch flush, explicit save, or another
checkpoint. A failed transaction never clears pending drafts. Model requests
are not involved in editing, assembly, or committing a draft.
"""
from __future__ import annotations
from dataclasses import dataclass, replace
from pydantic import ConfigDict, Field
from .db import SaveConflictError
from .domain import ProjectData, utc_now
from .manuscript import ManuscriptError, RevisionConflictError, check_text, count_chars, text_hash
from .revision import RevisionEngine
from .rewrite import RewriteTarget, verify_target
from .settings import Strict


class DraftInput(Strict):
    # Whitespace in the author's prose must survive validation unchanged.
    model_config = ConfigDict(str_strip_whitespace=False)
    expected_draft_version: int = Field(ge=0)
    text: str = Field(max_length=1_000_000)
    force_save: bool = False


class CommitDraft(Strict):
    model_config = ConfigDict(str_strip_whitespace=False)
    expected_draft_version: int = Field(ge=0)
    text: str = Field(max_length=1_000_000)
    allow_out_of_range: bool = False
    sync_after_commit: bool = True
    check_after_commit: bool = True


@dataclass(frozen=True, slots=True)
class PendingDraft:
    text: str
    version: int
    disk_version: int
    updated_at: str


class GenerationDrafts:
    def __init__(self, generation):
        self.generation = generation
        self.session = generation.session
        self.pending: dict[str, PendingDraft] = {}

    def overlay_locked(self, row: dict) -> dict:
        pending = self.pending.get(row['id'])
        return {
            'draft_text': pending.text if pending else row['draft_text'],
            'draft_version': pending.version if pending else row['draft_version'],
            'saved_draft_version': pending.disk_version if pending else row['draft_version'],
            'draft_dirty': pending is not None,
            'draft_save_error': self.session.project.last_save_error if pending else None,
        }

    def persist_pending(self, conn) -> None:
        """Called INSIDE the caller's transaction; do not mutate buffers here."""
        for tid, draft in self.pending.items():
            cur = conn.execute("""UPDATE generation_tasks SET draft_text=?,draft_version=?,updated_at=?
                WHERE id=? AND draft_version=? AND status <> 'committed'""",
                (draft.text, draft.version, draft.updated_at, tid, draft.disk_version))
            if cur.rowcount != 1:
                raise SaveConflictError('草稿磁盘版本不一致；事务回滚，内存草稿仍保留')

    def mark_persisted(self) -> None:
        """Called only AFTER COMMIT, including commits triggered by other services."""
        for tid in self.pending:
            self.generation._invalidate(tid)
        self.pending.clear()

    async def save(self, tid: str, body: DraftInput) -> dict:
        s = self.session
        check_text(body.text)
        async with s.lock:
            row = self.generation._bundle_locked(tid)[0]
            if row['status'] == 'committed':
                raise ManuscriptError('该草稿已经确认；修改正文请使用正文编辑或返修')
            view = self.overlay_locked(row)
            if body.expected_draft_version != view['draft_version']:
                raise RevisionConflictError('草稿已被另一个页面修改；当前输入未覆盖，请先复制保留，再重新载入')
            if body.text != view['draft_text']:
                self.pending[tid] = PendingDraft(body.text, view['draft_version'] + 1,
                                                view['saved_draft_version'], utc_now())
                s.project.data = ProjectData(s.project._changed_meta(), s.project.data.config)
                s.project.dirty.update({'meta', 'generation_drafts'})
            if body.force_save:
                s._flush_locked()
            return self.generation._view_locked(tid)

    def _receipt_locked(self, tid: str, revision_id: str, *, repeated: bool) -> dict:
        row = self.session.store.connection.execute(
            'SELECT id,revision_no,kind FROM revisions WHERE id=?', (revision_id,)).fetchone()
        if row is None:
            raise ManuscriptError('确认记录缺少正文版本，请检查项目备份')
        return {
            'task': self.generation._view_locked(tid),
            'revision': dict(row),
            'current_revision_no': self.session.manuscript.revision_no,
            'already_committed': repeated,
            'semantic_sync': 'pending',
            'notice': '正文已经保存。设定同步是独立步骤，失败不会回滚正文；状态可在同步页面检查。确认记录属于历史，正文以后仍可撤销或返修。',
        }

    async def preview(self, tid: str, body: CommitDraft) -> dict:
        import difflib
        check_text(body.text)
        async with self.session.lock:
            row=self.generation._bundle_locked(tid)[0]
            if body.expected_draft_version!=self.overlay_locked(row)['draft_version']:
                raise RevisionConflictError('草稿版本已变化，请重新核对')
            if row['task_type']!='rewrite':
                raise ManuscriptError('差异预览只适用于返修任务')
            target=RewriteTarget.model_validate(row['rewrite_target'],strict=True)
            lines=list(difflib.unified_diff(target.selected_text.splitlines(),body.text.splitlines(),
                                           fromfile='原选区',tofile='最终草稿',lineterm=''))
            return {'original':target.selected_text,'replacement':body.text,
                    'start_cp':target.start_cp,'end_cp':target.end_cp,
                    'diff':'\n'.join(lines[:600]),'diff_truncated':len(lines)>600,
                    'stale':self.generation._stale_locked(row),
                    'notice':'只替换这个原选区，选区前后文字逐字保留；差异预览不提交正文。'}

    async def commit(self, tid: str, body: CommitDraft) -> dict:
        s = self.session
        check_text(body.text)
        if not body.text.strip():
            raise ManuscriptError('不能确认空白草稿')
        async with s.lock:
            row, _, context, request, _ = self.generation._bundle_locked(tid)
            digest = text_hash(body.text)
            # A retried HTTP response must never append the same task twice,
            # even if the historical revision has since been undone.
            if row['status'] == 'committed':
                if digest != row['committed_text_hash']:
                    raise RevisionConflictError('任务已经确认且文本不同；不能重复追加，请到正文页编辑')
                return self._receipt_locked(tid, row['committed_revision_id'], repeated=True)
            if self.generation.active_id == tid:
                raise ManuscriptError('请等待候选完成，或暂停当前任务后再确认正文')
            if self.generation._stale_locked(row):
                raise RevisionConflictError('正文或设定已变化；旧任务不能直接确认，草稿保留供复制或人工返修')
            draft = self.overlay_locked(row)
            if body.expected_draft_version != draft['draft_version']:
                raise RevisionConflictError('草稿版本已变化；未追加正文，请先保存当前输入并重新核对')
            chars = count_chars(body.text)
            if not context['min_chars'] <= chars <= context['max_chars'] and not body.allow_out_of_range:
                raise ManuscriptError(f'当前草稿 {chars} 字，不在 {context["min_chars"]}–{context["max_chars"]} 范围内；请修改或明确允许超出范围')
            if row['task_type']=='rewrite':
                target=RewriteTarget.model_validate(row['rewrite_target'],strict=True)
                verify_target(target,s.manuscript)
                plan=RevisionEngine.replace(s.manuscript,target.start_cp,target.end_cp,body.text,
                                            row['base_revision_no'],request.context.instruction,kind='rewrite')
                if plan is None:
                    raise ManuscriptError('最终稿与原选区完全相同；没有可确认的改动，请编辑草稿或保留原文')
            else:
                plan = RevisionEngine.append(s.manuscript, body.text, row['base_revision_no'])
            plan = replace(plan, revision=replace(plan.revision, instruction=request.context.instruction))
            version = draft['draft_version'] + int(body.text != draft['draft_text'])
            def apply(conn):
                # Pending drafts and the Revision have already been written by
                # _persist_extras in this SAME transaction, not yet committed.
                cur = conn.execute("""UPDATE generation_tasks SET status='committed',draft_text=?,
                    draft_version=?,committed_revision_id=?,committed_text_hash=?,error=NULL,updated_at=?
                    WHERE id=? AND draft_version=? AND status <> 'committed'""",
                    (body.text, version, plan.revision.id, digest, utc_now(), tid, draft['draft_version']))
                if cur.rowcount != 1:
                    raise SaveConflictError('确认时草稿版本已变，正文和草稿事务全部回滚')
            s._commit_snapshot_locked(plan=plan, extra=apply)
            self.generation._invalidate(tid)
            return self._receipt_locked(tid, plan.revision.id, repeated=False)
