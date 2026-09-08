"""Accept-all is a single explicit, versioned action, never auto-approval.

The weak-model fixture returns ordinary short answers. Larger catalog fixtures
reuse individually valid source-bound rows, without sending oversized LLM output.
"""
import asyncio
from contextlib import closing
import json
import sqlite3
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from noveltool import __version__
from noveltool.app import create_app
from noveltool.db import ProjectStore, SaveFailedError
from noveltool.runtime import ProjectSession
from noveltool.settings import Versioned, ProfileWrite, ReviewWrite
from noveltool.manuscript import RevisionConflictError
from test_analysis import session_ready, finding, SOURCE, prepare
from test_llm import envelope
from test_settings import entry
from test_small_model import ready, responder, SOURCE as SMALL_SOURCE


async def reviewed_source(path):
    s = await ready(path, SMALL_SOURCE)
    await s.analysis.run_chunk(s.imports.last_plan.id, 0, 1, transport=responder())
    return s


def statuses(s):
    return {r['id']: r['status'] for r in s.store.connection.execute('SELECT id,status FROM observations')}


def changes(s):
    return [dict(r) for r in s.store.connection.execute('SELECT * FROM setting_changes ORDER BY version')]


async def accept_all(s):
    view = await s.knowledge.view()
    return await s.settings.accept_all_pending(Versioned(expected_version=view['version']))


def clone_rows(s, count):
    """Build a large review catalog on the same real, validated source run."""
    row = s.store.connection.execute('SELECT * FROM observations ORDER BY ordinal DESC LIMIT 1').fetchone()
    rows = [(uuid4().hex, row['run_id'], row['ordinal']+i+1, row['kind'],
             row['payload_json'], row['evidence_json'], 'pending') for i in range(count)]
    s.store.connection.executemany('INSERT INTO observations VALUES(?,?,?,?,?,?,?)', rows)
    s.analysis.epoch += 1
    return [r[0] for r in rows]


def test_accept_all_weak_results_immediately_persist_reopen_no_model_or_text_change(project_path):
    async def scenario():
        s = await reviewed_source(project_path)
        try:
            before = await s.knowledge.view()
            assert len(before['observations']) == 4 and not before['entities']
            count_runs = s.store.connection.execute('SELECT COUNT(*) FROM llm_runs').fetchone()[0]
            after = await accept_all(s)
            assert after['bulk_review']['accepted_count'] == 4
            assert all(r['status'] == 'accepted' for r in after['observations'])
            assert len(after['entities']) == 1 and after['entities'][0]['facts'] and after['entities'][0]['states']
            assert not after['review_queue']
            assert after['version'] != before['version']
            assert s.manuscript.text == SMALL_SOURCE and s.manuscript.revision_no == 1
            assert s.store.connection.execute('SELECT COUNT(*) FROM llm_runs').fetchone()[0] == count_runs
            audit = changes(s)
            assert len(audit) == 1 and audit[0]['action'] == 'review_all_pending'
            assert set(json.loads(audit[0]['before_json']).values()) == {'pending'}
            assert set(json.loads(audit[0]['after_json']).values()) == {'accepted'}
            with closing(sqlite3.connect(project_path)) as disk:
                assert disk.execute("SELECT COUNT(*) FROM observations WHERE status='accepted'").fetchone()[0] == 4
            version = after['version']
        finally:
            await s.close()
        s = ProjectSession(ProjectStore.open(project_path))
        try:
            view = await s.knowledge.view()
            assert view['version'] == version and len(view['entities']) == 1
            assert all(r['status'] == 'accepted' for r in view['observations'])
        finally:
            await s.close()
    asyncio.run(scenario())


def test_bulk_leaves_accepted_rejected_and_manual_overrides_untouched(project_path):
    async def scenario():
        s = await reviewed_source(project_path)
        try:
            view = await s.knowledge.view()
            entity = next(r for r in view['observations'] if r['kind'] == 'entity')
            event = next(r for r in view['observations'] if r['kind'] == 'event')
            view = await s.settings.review(ReviewWrite(expected_version=view['version'], observation_ids=[entity['id']], decision='accepted'))
            view = await s.settings.review(ReviewWrite(expected_version=view['version'], observation_ids=[event['id']], decision='rejected'))
            eid = view['entities'][0]['id']
            fact = next(r for r in view['observations'] if r['kind'] == 'fact' and r['payload']['mode'] == 'stable')
            await entry(s, 'fact', {'entity_id': eid, 'field': fact['payload']['field'], 'value': '作者指定的职业'}, replaces=[fact['id']])
            profiles = dict(s.settings.profiles)
            entries = dict(s.settings.entries)
            result = await accept_all(s)
            assert result['bulk_review']['accepted_count'] == 2
            assert statuses(s)[event['id']] == 'rejected' and statuses(s)[entity['id']] == 'accepted'
            assert s.settings.profiles == profiles and s.settings.entries == entries
            assert result['entities'][0]['facts'][0]['values'][0]['value'] == '作者指定的职业'
            last = changes(s)[-1]
            assert entity['id'] not in json.loads(last['after_json']) and event['id'] not in json.loads(last['after_json'])
        finally:
            await s.close()
    asyncio.run(scenario())


def test_more_than_512_one_transaction_without_large_sql_in_list(project_path):
    async def scenario():
        s = await reviewed_source(project_path)
        try:
            clone_rows(s, 1200)
            # The one-row-at-a-time statement must not depend on SQLite's large
            # SQL-variable build options. More than 512 rows still commit once.
            old_limit = s.store.connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 40)
            trace = []
            s.store.connection.set_trace_callback(trace.append)
            result = await accept_all(s)
            s.store.connection.set_trace_callback(None)
            s.store.connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old_limit)
            assert result['bulk_review']['accepted_count'] == 1204
            assert all(x == 'accepted' for x in statuses(s).values())
            assert sum(q.startswith('BEGIN') for q in trace) == 1
            assert trace.count('COMMIT') == 1
            assert len(changes(s)) == 1
            assert len(json.loads(changes(s)[0]['after_json'])) == 1204
        finally:
            await s.close()
    asyncio.run(scenario())


def test_empty_or_already_accepted_is_noop_without_audit_or_version_change(project_path):
    async def scenario():
        s = ProjectSession(ProjectStore.open(project_path))
        try:
            version = (await s.knowledge.view())['version']
            saved = s.project.saved_version
            result = await accept_all(s)
            assert result['bulk_review']['accepted_count'] == 0
            assert result['version'] == version and s.project.saved_version == saved
            assert not changes(s)
        finally:
            await s.close()
    asyncio.run(scenario())


def test_repeat_after_refresh_noop_but_stale_double_click_rejected(project_path):
    async def scenario():
        s = await reviewed_source(project_path)
        try:
            old = (await s.knowledge.view())['version']
            first = await accept_all(s)
            second = await accept_all(s)
            assert second['bulk_review']['accepted_count'] == 0 and second['version'] == first['version']
            with pytest.raises(RevisionConflictError):
                await s.settings.accept_all_pending(Versioned(expected_version=old))
            assert len(changes(s)) == 1
        finally:
            await s.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['text', 'manual', 'analysis', 'review'])
def test_old_page_never_accepts_a_changed_catalog(project_path, change):
    async def scenario():
        s = await reviewed_source(project_path)
        try:
            old = (await s.knowledge.view())['version']
            if change == 'text':
                await s.append_manuscript('之后。', 1)
            elif change == 'manual':
                await s.settings.put_profile(ProfileWrite(expected_version=old, name='周', kind='character'))
            elif change == 'analysis':
                await s.analysis.run_chunk(s.imports.last_plan.id, 0, 1, pass_type='narrative', transport=responder())
            else:
                oid = next(iter(statuses(s)))
                await s.settings.review(ReviewWrite(expected_version=old, observation_ids=[oid], decision='rejected'))
            saved_statuses, saved_changes = statuses(s), changes(s)
            with pytest.raises(RevisionConflictError):
                await s.settings.accept_all_pending(Versioned(expected_version=old))
            assert statuses(s) == saved_statuses and changes(s) == saved_changes
        finally:
            await s.close()
    asyncio.run(scenario())


def test_failed_db_write_rolls_back_entire_batch_and_audit(project_path):
    async def scenario():
        s = await reviewed_source(project_path)
        try:
            before = await s.knowledge.view()
            saved = s.project.saved_version
            # Fail only after an earlier row has been updated: this detects
            # accidental per-row commits, not just a failure on the first item.
            s.store.connection.execute("""CREATE TRIGGER fail_bulk BEFORE UPDATE OF status ON observations
                WHEN NEW.status='accepted' AND (SELECT COUNT(*) FROM observations WHERE status='accepted') >= 1
                BEGIN SELECT RAISE(ABORT,'simulated disk write failure'); END""")
            with pytest.raises(SaveFailedError):
                await accept_all(s)
            assert set(statuses(s).values()) == {'pending'}
            assert not changes(s) and s.settings.version == 0
            assert s.project.saved_version == saved
            assert (await s.knowledge.view())['version'] == before['version']
            s.store.connection.execute('DROP TRIGGER fail_bulk')
            assert (await accept_all(s))['bulk_review']['accepted_count'] == 4
        finally:
            await s.close()
    asyncio.run(scenario())


def test_audit_failure_rolls_back_observations(project_path):
    async def scenario():
        s = await reviewed_source(project_path)
        try:
            s.store.connection.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON setting_changes BEGIN SELECT RAISE(ABORT,'audit failed'); END")
            with pytest.raises(SaveFailedError):
                await accept_all(s)
            assert set(statuses(s).values()) == {'pending'} and not changes(s)
            s.store.connection.execute('DROP TRIGGER fail_audit')
        finally:
            await s.close()
    asyncio.run(scenario())


def test_invalid_record_and_retired_source_are_not_accepted(project_path):
    async def scenario():
        s = await reviewed_source(project_path)
        try:
            ids = list(statuses(s))
            s.store.connection.execute("UPDATE observations SET evidence_json='[]' WHERE id=?", (ids[0],))
            s.analysis.epoch += 1
            result = await accept_all(s)
            assert result['errors'] and result['bulk_review']['accepted_count'] == 3
            assert statuses(s)[ids[0]] == 'pending'
            # Replacing the source retires the old input, so old pending rows
            # are still in history but no longer an accept-all target.
            await s.replace_manuscript(0, len(SMALL_SOURCE), '新正文。', 1)
            result = await accept_all(s)
            assert result['bulk_review']['accepted_count'] == 0
            assert statuses(s)[ids[0]] == 'pending'
        finally:
            await s.close()
    asyncio.run(scenario())


def test_superseded_old_success_is_not_selected(project_path):
    async def scenario():
        s = await session_ready(project_path)
        try:
            tr = httpx.MockTransport(lambda req: httpx.Response(200, json=envelope(json.dumps(finding()))))
            first = await s.analysis.run_chunk(s.imports.last_plan.id, 0, 1, transport=tr)
            latest = await s.analysis.run_chunk(s.imports.last_plan.id, 0, 1, transport=tr)
            result = await accept_all(s)
            assert result['bulk_review']['accepted_count'] == 3
            assert all(statuses(s)[r['id']] == 'pending' for r in first['observations'])
            assert all(statuses(s)[r['id']] == 'accepted' for r in latest['observations'])
        finally:
            await s.close()
    asyncio.run(scenario())


def test_unmodified_previous_revision_is_still_valid_after_append(project_path):
    async def scenario():
        s = await reviewed_source(project_path)
        try:
            await s.append_manuscript('下一段。', 1)
            result = await accept_all(s)
            assert result['bulk_review']['accepted_count'] == 4
            assert result['revision_no'] == 2
        finally:
            await s.close()
    asyncio.run(scenario())


def test_partial_success_can_be_accepted_but_not_claim_full_coverage(project_path):
    async def scenario():
        s = await ready(project_path, SMALL_SOURCE)
        try:
            run = await s.analysis.run_chunk(s.imports.last_plan.id, 0, 1,
                transport=responder({'什么身份': '{"value":"半截'}))
            assert not run['quality']['complete']
            result = await accept_all(s)
            assert result['bulk_review']['accepted_count'] == 3
            assert result['semantic_sync']['coverage']['facts'] == 0
            assert result['entities']
        finally:
            await s.close()
    asyncio.run(scenario())


def test_concurrent_same_version_requests_commit_exactly_once(project_path):
    async def scenario():
        s = await reviewed_source(project_path)
        try:
            body = Versioned(expected_version=(await s.knowledge.view())['version'])
            results = await asyncio.gather(s.settings.accept_all_pending(body), s.settings.accept_all_pending(body), return_exceptions=True)
            assert sum(isinstance(r, RevisionConflictError) for r in results) == 1
            assert sum(isinstance(r, dict) and r['bulk_review']['accepted_count'] == 4 for r in results) == 1
            assert len(changes(s)) == 1
        finally:
            await s.close()
    asyncio.run(scenario())


def test_new_observations_are_not_automatically_accepted(project_path):
    async def scenario():
        s = await reviewed_source(project_path)
        try:
            await accept_all(s)
            run = await s.analysis.run_chunk(s.imports.last_plan.id, 0, 1, pass_type='narrative', transport=responder())
            assert run['observations'] and all(r['status'] == 'pending' for r in run['observations'])
            assert len(changes(s)) == 1
        finally:
            await s.close()
    asyncio.run(scenario())


def test_accept_all_does_not_hide_factual_conflicts(project_path):
    async def scenario():
        s = await session_ready(project_path)
        try:
            data = finding()
            data['facts'] = [{**data['facts'][0], 'mode': 'stable', 'field': '职业', 'value': v} for v in ['记者', '律师']]
            await s.analysis.run_chunk(s.imports.last_plan.id, 0, 1,
                transport=httpx.MockTransport(lambda req: httpx.Response(200, json=envelope(json.dumps(data)))))
            result = await accept_all(s)
            assert result['entities'][0]['facts'][0]['conflict']
            assert any('不同取值' in q['reason'] for q in result['review_queue'])
        finally:
            await s.close()
    asyncio.run(scenario())


def test_http_scope_token_validation_history_and_pages(project_path):
    tr = httpx.MockTransport(lambda req: httpx.Response(200, json=envelope(json.dumps(finding()))))
    with TestClient(create_app(project_path, llm_transport=tr), base_url='http://127.0.0.1') as c:
        h = {'X-Noveltool-Token': c.get('/api/session').json()['csrf_token']}
        plan = prepare(c, h)
        assert c.post('/api/analysis/chunks/0/run', headers=h, json=plan).status_code == 200
        body = {'expected_version': c.get('/api/settings').json()['version']}
        url = '/api/settings/review/accept-all'
        assert c.post(url, json=body).status_code == 403
        assert c.post(url, headers={**h, 'Origin': 'https://other.example'}, json=body).status_code == 403
        assert c.post(url, headers=h, json={}).status_code == 422
        assert c.post(url, headers=h, json={**body, 'observation_ids': []}).status_code == 422
        result = c.post(url, headers=h, json=body)
        assert result.status_code == 200 and result.json()['bulk_review']['accepted_count'] == 3
        assert c.post(url, headers=h, json=body).status_code == 409
        assert c.get('/api/settings/history').json()['changes'][0]['action'] == 'review_all_pending'
        for page in ['/settings', '/timeline']:
            html = c.get(page).text
            assert 'id="accept-all-pending"' in html and 'id="review-counts"' in html
            assert 'id="observation-review"' in html and 'href="#observation-review"' in html
        assert c.get('/health').json()['version'] == __version__


def test_defensive_rowcount_mismatch_also_rolls_back(project_path):
    async def scenario():
        s = await reviewed_source(project_path)
        try:
            oid = next(iter(statuses(s)))
            s.store.connection.execute(f"""CREATE TRIGGER skip_review BEFORE UPDATE OF status ON observations
                WHEN NEW.id='{oid}' BEGIN SELECT RAISE(IGNORE); END""")
            with pytest.raises(RevisionConflictError, match='回滚'):
                await accept_all(s)
            assert set(statuses(s).values()) == {'pending'} and not changes(s)
            s.store.connection.execute('DROP TRIGGER skip_review')
        finally:
            await s.close()
    asyncio.run(scenario())


def test_unsuccessful_analysis_observations_remain_outside_current_scope(project_path):
    async def scenario():
        s = await reviewed_source(project_path)
        try:
            s.store.connection.execute("UPDATE analysis_runs SET status='failed'")
            s.analysis.epoch += 1
            result = await accept_all(s)
            assert result['bulk_review']['accepted_count'] == 0
            assert set(statuses(s).values()) == {'pending'}
            assert not changes(s)
        finally:
            await s.close()
    asyncio.run(scenario())
