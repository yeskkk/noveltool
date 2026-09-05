"""Explicit local diagnostics, read-only history and complete SQLite snapshots.

Nothing here schedules a model call. Diagnostics are on demand, not a periodic
full-database scan. Backups flush server-side pending edits first; browser-only
text cannot be included. SQLite backup() includes committed WAL contents.
"""
from __future__ import annotations
from dataclasses import asdict
from difflib import unified_diff
from hashlib import sha256
from itertools import islice
from pathlib import Path
import os
import sqlite3
import tempfile

from .domain import utc_now
from .db import ProjectError, InvalidProjectError
from .manuscript import ManuscriptError, text_hash
from .revision import _revision_from_row, load_manuscript

RECOVERY_TABLES = {
    'analysis_runs': ('running',),
    'analysis_jobs': ('running', 'pausing'),
    'idea_proposals': ('running',),
    'generation_tasks': ('generating', 'pausing'),
    'consistency_jobs': ('running', 'pausing', 'waiting_sync'),
}


def startup_recovery_counts(connection):
    """Capture before service constructors convert abandoned work to interrupted."""
    return {table: connection.execute(
        f"SELECT count(*) FROM {table} WHERE status IN ({','.join('?' for _ in statuses)})",
        statuses).fetchone()[0] for table, statuses in RECOVERY_TABLES.items()}


def database_snapshot(source: sqlite3.Connection) -> Path:
    fd, name = tempfile.mkstemp(prefix='noveltool-backup-', suffix='.sqlite3')
    os.close(fd)  # mkstemp creates an exclusive, owner-only file.
    path = Path(name)
    destination = None
    try:
        destination = sqlite3.connect(path)
        source.backup(destination)
        # A standalone file, not a copy depending on a separate WAL sidecar.
        destination.execute('PRAGMA journal_mode=DELETE')
        if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise InvalidProjectError('备份完整性检查失败，未提供下载')
        destination.close(); destination = None
        with path.open('rb') as stream:
            os.fsync(stream.fileno())
        return path
    except BaseException:
        if destination is not None: destination.close()
        path.unlink(missing_ok=True)
        raise


class MaintenanceService:
    def __init__(self, session):
        self.session = session

    async def overview(self):
        s = self.session
        async with s.lock:
            c = s.store.connection
            unfinished = {}
            for table in RECOVERY_TABLES:
                unfinished[table] = [dict(r) for r in c.execute(
                    f"SELECT id,status FROM {table} WHERE status IN ('interrupted','paused','failed','partial','stale') ORDER BY rowid DESC LIMIT 20")]
            return {
                'startup_recovery': s.startup_recovery,
                'unfinished': unfinished,
                'dirty_sections': sorted(s.project.dirty),
                'last_save_error': s.project.last_save_error,
                'revision_no': s.manuscript.revision_no,
                'schema_version': s.project.data.meta.schema_version,
                'database_bytes': s.store.path.stat().st_size,
                'wal_bytes': Path(str(s.store.path)+'-wal').stat().st_size if Path(str(s.store.path)+'-wal').exists() else 0,
                'migration_backup': str(s.store.migration_backup) if s.store.migration_backup else None,
                'notice': '重启不会自动请求模型。先到对应页面检查基线，再明确继续。浏览器未提交和上次退出前未写盘的草稿可能丢失。',
            }

    async def check(self, expected_revision_no: int):
        s = self.session
        async with s.lock:
            s.manuscript.check_revision(expected_revision_no)
            c = s.store.connection
            errors = []
            total_errors = 0
            def error(message):
                nonlocal total_errors
                total_errors += 1
                if len(errors) < 200: errors.append(message)
            for row in c.execute('PRAGMA integrity_check'):
                if row[0] != 'ok': error(f'SQLite: {row[0]}')
            for row in c.execute('PRAGMA foreign_key_check'):
                error(f'外键: {tuple(row)}')
            blocks = 0
            for row in c.execute('SELECT id,text,content_hash FROM manuscript_blocks'):
                blocks += 1
                if text_hash(row['text']) != row['content_hash']: error(f"正文块哈希错误: {row['id']}")
            imports = 0
            for row in c.execute('SELECT id,raw_bytes,raw_hash FROM source_imports'):
                imports += 1
                if sha256(row['raw_bytes']).hexdigest() != row['raw_hash']: error(f"原始导入文件哈希错误: {row['id']}")
            previous_hash, revision_count = text_hash(''), 0
            for row in c.execute('SELECT revision_no,before_hash,after_hash FROM revisions ORDER BY revision_no'):
                revision_count += 1
                if row['revision_no'] != revision_count or row['before_hash'] != previous_hash:
                    error(f"正文历史序号或哈希链错误: {row['revision_no']}")
                previous_hash = row['after_hash']
            try:
                saved = load_manuscript(c)
                if saved != s.manuscript: error('磁盘正文与程序内存不一致')
            except ProjectError as exc: error(str(exc))
            return {'ok': total_errors == 0, 'errors': errors, 'error_count': total_errors,
                    'errors_truncated': total_errors > len(errors), 'blocks_checked': blocks,
                    'imports_checked': imports, 'revisions_checked': revision_count,
                    'revision_no': s.manuscript.revision_no, 'checked_at': utc_now(),
                    'notice': '检查 SQLite、外键、全部正文块及原始文件哈希、历史哈希链和当前正文。它不是所有 JSON 字段或小说语义的完整验证，也不会自动修库。'}

    async def backup(self, expected_revision_no: int):
        s = self.session
        async with s.lock:
            s.manuscript.check_revision(expected_revision_no)
            s._flush_locked()
            try:
                return database_snapshot(s.store.connection)
            except (sqlite3.Error, OSError) as exc:
                raise ProjectError('备份生成失败，请检查磁盘；原项目未被替换') from exc

    async def revision(self, number: int):
        s = self.session
        async with s.lock:
            c = s.store.connection
            row = c.execute('SELECT * FROM revisions WHERE revision_no=?', (number,)).fetchone()
            if row is None: raise ManuscriptError('正文版本不存在')
            rev = _revision_from_row(row)
            def text(ids):
                out = []
                for bid in ids:
                    block = c.execute('SELECT text,content_hash FROM manuscript_blocks WHERE id=?', (bid,)).fetchone()
                    if block is None or text_hash(block['text']) != block['content_hash']:
                        raise InvalidProjectError('历史正文块缺失或损坏；没有伪造差异')
                    out.append(block['text'])
                return ''.join(out)
            old, new = text(rev.old_ids), text(rev.new_ids)
            diff = list(islice(unified_diff(old.splitlines(keepends=True),new.splitlines(keepends=True),fromfile='变更前的块',tofile='变更后的块'), 601))
            return {'revision': asdict(rev), 'before': old, 'after': new,
                    'diff': ''.join(diff[:600]), 'diff_truncated': len(diff) > 600,
                    'notice': '展示此版本涉及的完整旧块/新块，可能包含选区旁未变的文字；不是整本小说的历史快照。只读，不能任意回退；连续撤销在正文页执行。'}

    async def run(self, run_id: str):
        s = self.session
        async with s.lock:
            pending = next((r for r in s._pending_llm_runs if r.id == run_id), None)
            row = s.store.connection.execute('SELECT * FROM llm_runs WHERE id=?', (run_id,)).fetchone() if not pending else None
            if not pending and row is None: raise ManuscriptError('模型调用记录不存在')
            value = asdict(pending) if pending else dict(row)
            record = s._pending_validations.get(run_id)
            if record: value.update(parsed_json=record.parsed_json, validation_status=record.status, validation_error=record.error)
            value['pending_save'] = pending is not None or record is not None
            value['truncated_fields'] = []
            for key in ('request_json', 'raw_response', 'parsed_json'):
                if value.get(key) and len(value[key]) > 64000:
                    value[key] = value[key][:64000]
                    value['truncated_fields'].append(key)
            value['notice'] = '内容日志只按调用时/结束时的保留设置显示；关闭日志不删除更早历史。此页每个大字段最多显示 64000 字符，明确列出截断字段。文本含私人资料，请勿随意转发。'
            return value
