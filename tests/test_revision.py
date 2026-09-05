import asyncio
import random
import sqlite3

import pytest

from noveltool.db import ProjectStore, SaveFailedError
from noveltool.domain import ProjectConfig
from noveltool.manuscript import ManuscriptError, RevisionConflictError
from noveltool.revision import load_manuscript
from noveltool.runtime import ProjectSession


def run_scenario(path, action):
    async def run():
        session = ProjectSession(ProjectStore.open(path))
        try:
            await action(session)
        finally:
            await session.close()
    asyncio.run(run())


def test_import_replace_undo_reopen(project_path):
    text = '　甲😀\n\n  第二段\n\n\n结尾 \n'
    async def action(s):
        await s.import_manuscript(text, 0)
        ids = [b.id for b in s.manuscript.blocks]
        await s.replace_manuscript(1, 8, '改\n\n文', 1)
        assert s.manuscript.text == text[:1] + '改\n\n文' + text[8:]
        await s.undo_manuscript(2)
        assert s.manuscript.text == text
        assert [b.id for b in s.manuscript.blocks] == ids
        assert not (await s.status())['dirty']
    run_scenario(project_path, action)
    with ProjectStore.open(project_path) as store:
        assert load_manuscript(store.connection).text == text
        assert load_manuscript(store.connection).revision_no == 3


def test_many_edits_then_undo_all(project_path):
    async def action(s):
        original = '甲😀\n\n乙\n结尾\n'
        await s.import_manuscript(original, 0)
        history = [original]
        rng = random.Random(71)
        for _ in range(250):
            text = s.manuscript.text
            a, b = sorted([rng.randrange(len(text)+1), rng.randrange(len(text)+1)])
            replacement = ''.join(rng.choice(['甲', '乙', '😀', '\n', ' ', '\t', 'e\u0301']) for _ in range(rng.randrange(8)))
            changed = await s.replace_manuscript(a, b, replacement, s.manuscript.revision_no)
            if changed:
                history.append(text[:a] + replacement + text[b:])
            assert s.manuscript.text == history[-1]
            assert load_manuscript(s.store.connection) == s.manuscript
        for expected in reversed(history[:-1]):
            await s.undo_manuscript(s.manuscript.revision_no)
            assert s.manuscript.text == expected
        await s.undo_manuscript(s.manuscript.revision_no)
        assert s.manuscript.text == ''
        with pytest.raises(ManuscriptError, match='没有可撤销'):
            await s.undo_manuscript(s.manuscript.revision_no)
    run_scenario(project_path, action)


def test_append_and_branch_after_undo(project_path):
    async def action(s):
        await s.import_manuscript('甲', 0)
        first = s.manuscript.blocks[0]
        await s.append_manuscript('乙', 1)
        assert s.manuscript.text == '甲\n\n乙'
        assert s.manuscript.blocks[0] == first
        await s.undo_manuscript(2)
        await s.append_manuscript('丙', 3)
        await s.undo_manuscript(4)
        assert s.manuscript.text == '甲'
        await s.undo_manuscript(5)
        assert s.manuscript.text == ''
    run_scenario(project_path, action)


def test_stale_revision_noop_and_import_guard(project_path):
    async def action(s):
        await s.import_manuscript('text', 0)
        assert not await s.replace_manuscript(0, 4, 'text', 1)
        assert s.manuscript.revision_no == 1
        with pytest.raises(RevisionConflictError):
            await s.replace_manuscript(0, 1, 'bad', 0)
        with pytest.raises(ManuscriptError):
            await s.import_manuscript('bad', 1)
        assert s.manuscript.text == 'text'
    run_scenario(project_path, action)


def test_sql_failure_leaves_memory_disk_and_pending_config_intact(project_path):
    async def action(s):
        await s.import_manuscript('before', 0)
        await s.update_config(ProjectConfig(min_chars=100, max_chars=200), s.project.data.meta.data_version)
        before_data = s.project.data
        before = s.manuscript
        s.store.connection.execute("CREATE TRIGGER fail_new_block BEFORE INSERT ON manuscript_blocks BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(SaveFailedError):
            await s.replace_manuscript(0, 6, 'after', 1)
        assert s.manuscript == before
        assert load_manuscript(s.store.connection) == before
        assert s.project.data == before_data
        assert s.project.dirty
        assert s.store.load().config.min_chars == 600
        s.store.connection.execute('DROP TRIGGER fail_new_block')
        await s.replace_manuscript(0, 6, 'after', 1)
        assert not s.project.dirty
        assert s.store.load().config.min_chars == 100
    run_scenario(project_path, action)


def test_blocks_and_history_are_immutable(project_path):
    async def action(s):
        await s.import_manuscript('甲\n\n乙', 0)
        for sql in ["UPDATE manuscript_blocks SET text='bad'", 'DELETE FROM manuscript_blocks',
                    "UPDATE revisions SET kind='append'", 'DELETE FROM revisions']:
            with pytest.raises(sqlite3.IntegrityError):
                s.store.connection.execute(sql)
    run_scenario(project_path, action)
