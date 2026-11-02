"""Real HTTP handlers and Kanban policies, isolated from the operator's home."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from hermes_cli import web_server as web
    from hermes_cli.hermess_tasks import install_private_routes
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setenv('HERMESS_DESKTOP_PRIVATE_API', '1')
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_BOARD', raising=False)
    monkeypatch.setattr(web, '_SESSION_TOKEN', 'isolated-test-token')
    (tmp_path / 'config.yaml').write_text('model:\n  default: test-model\n', encoding='utf-8')
    named = tmp_path / 'profiles' / 'research'
    named.mkdir(parents=True)
    (named / 'config.yaml').write_text('model:\n  default: test-model\n', encoding='utf-8')
    install_private_routes(web.app)
    client = TestClient(web.app)

    def send(action, *, profile='default', headers=None, **values):
        return client.post('/api/hermess/tasks/request',
            headers={'X-Hermes-Session-Token': 'isolated-test-token'} if headers is None else headers,
            json=dict(action=action, profile=profile, **values))
    return SimpleNamespace(send=send, home=tmp_path, client=client)


def create(api, **values):
    response = api.send('create', **dict(request_id=str(uuid4()), title='研究记账软件', body='比较三款，列出来源。', **values))
    assert response.status_code == 200, response.text
    return response.json()['task']


def connect(profile='default'):
    from hermes_cli.hermess_tasks import board_for
    from hermes_cli.kanban_db_connect import connect as kb_connect
    return closing(kb_connect(board=board_for(profile)))


def test_native_auth_mode_and_profile_validation(api, monkeypatch):
    assert api.send('capabilities').json()['version'] == 1
    assert api.send('list', headers={}).status_code == 401
    assert api.send('list', headers={'X-Hermes-Session-Token': 'wrong'}).status_code == 401
    assert api.send('list', headers={'Authorization': 'Bearer isolated-test-token'}).status_code == 401
    assert api.send('list', headers={'X-Hermes-Session-Token': 'isolated-test-token', 'Origin': ''}).status_code == 403
    assert api.send('list', profile='missing').status_code == 404
    assert api.send('list', profile='../research').status_code == 422
    assert api.send('create', request_id='bad', title='x', body='x').status_code == 422
    assert api.send('create', request_id=str(uuid4()), title='x', body='x', assignee='research').status_code == 422
    monkeypatch.delenv('HERMESS_DESKTOP_PRIVATE_API')
    assert api.send('list').status_code == 404


def test_create_persistence_concurrency_scope_and_archived_replay(api):
    payload = dict(request_id=str(uuid4()), title='研究软件', body='三款，有来源。')
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: api.send('create', **payload), range(12)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    ids = {r.json()['task']['id'] for r in responses}
    assert len(ids) == 1
    task_id = ids.pop()
    assert len(api.send('list').json()['tasks']) == 1
    assert api.send('create', **{**payload, 'body': 'different'}).status_code == 409
    assert api.send('detail', profile='research', task_id=task_id).status_code == 404
    assert api.send('archive', profile='research', task_id=task_id).status_code == 404
    assert api.send('archive', task_id=task_id).status_code == 200
    replay = api.send('create', **payload)
    assert replay.json()['task']['status'] == 'archived'
    with connect() as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
    assert api.send('detail', task_id=task_id).json()['task']['id'] == task_id
    assert api.send('list', profile='research').json() == {'tasks': []}


def test_reply_and_resume_preserve_original_card_and_event_cursor(api):
    from hermes_cli import kanban_db as kb
    task = create(api)
    with connect() as conn:
        assert kb.block_task(conn, task['id'], reason='需要预算范围', kind='needs_input')
    reply = dict(task_id=task['id'], request_id=str(uuid4()), body='预算 200 元')
    assert api.send('reply', **reply).status_code == 200
    assert api.send('reply', **reply).status_code == 200
    assert api.send('reply', **{**reply, 'body': '改了'}).status_code == 409
    detail = api.send('detail', task_id=task['id']).json()
    assert detail['task']['summary'] == '需要预算范围'
    assert len(detail['comments']) == 1
    resume = dict(task_id=task['id'], request_id=str(uuid4()))
    assert api.send('resume', **resume).json()['task']['status'] == 'ready'
    with connect() as conn:
        kb.block_task(conn, task['id'], reason='新的问题', kind='needs_input')
    # Old resume does not clear a later block.
    assert api.send('resume', **resume).json()['task']['status'] != 'ready'
    first = api.send('events', limit=2).json()
    second = api.send('events', after=first['next'], limit=200).json()
    assert len(first['events']) == 2 and second['events']
    assert min(e['id'] for e in second['events']) > first['next']
    assert api.send('events', after=second['next']).json()['events'] == []


def test_resume_refuses_live_interpreter_and_unknown_previous_outcome(api, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as dispatch
    task = create(api)
    fingerprint = dispatch._process_fingerprint(os.getpid())
    with connect() as conn:
        kb.block_task(conn, task['id'], reason='input', kind='needs_input')
        with kb.write_txn(conn):
            kb._append_event(conn, task['id'], 'worker_bound', {'pid': os.getpid(), 'started_at': fingerprint})
    assert api.send('resume', task_id=task['id'], request_id=str(uuid4())).status_code == 409
    # Archive records the true liveness result rather than claiming a stopped worker.
    result = api.send('archive', task_id=task['id'])
    assert result.json()['termination_pending'] is True
    other = create(api)
    with connect() as conn:
        kb.block_task(conn, other['id'], reason='input', kind='needs_input')
    request_id = str(uuid4())
    original = kb.unblock_task
    def lost_commit(conn, task_id):
        original(conn, task_id)
        raise RuntimeError('injected crash after unblock commit')
    monkeypatch.setattr(kb, 'unblock_task', lost_commit)
    with pytest.raises(RuntimeError, match='injected crash'):
        api.send('resume', task_id=other['id'], request_id=request_id)
    monkeypatch.setattr(kb, 'unblock_task', original)
    with connect() as conn:
        kb.block_task(conn, other['id'], reason='later block', kind='needs_input')
    result = api.send('resume', task_id=other['id'], request_id=request_id)
    assert result.status_code == 409 and result.json()['detail'] == 'resume_outcome_unknown'
    assert api.send('detail', task_id=other['id']).json()['task']['status'] != 'ready'


def test_resume_lock_contention_fails_closed_on_windows(api):
    from hermes_cli.hermess_tasks import _operation_lock, board_for
    from hermes_cli import kanban_db as kb
    task = create(api)
    with connect() as conn:
        kb.block_task(conn, task['id'], reason='input', kind='needs_input')
    lock_path = kb.kanban_db_path(board_for('default')).with_name('hermess-resume.lock')
    with _operation_lock(lock_path):
        response = api.send('resume', task_id=task['id'], request_id=str(uuid4()))
    assert response.status_code == 409
    assert response.json()['detail'] == 'operation_in_progress'


def test_research_policy_receiver_pins_tools_and_rejects_stale_claim(api, monkeypatch):
    from gateway.session_kanban import build_kanban_policy
    from hermes_cli import kanban_db as kb
    from hermes_cli.hermess_tasks import board_for
    from hermes_state_runtime import RuntimeStoreError
    from toolsets import resolve_toolset
    task = create(api)
    with connect() as conn:
        kb.recompute_ready(conn)
        claimed = kb.claim_task(conn, task['id'])
        workspace = api.home / 'research-workspace'
        workspace.mkdir()
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET workspace_path=? WHERE id=?', (str(workspace), task['id']))
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env', lambda name: str(api.home))
    owner = str(api.home)
    connection = SimpleNamespace(native_owner=True, authority=SimpleNamespace(profile_id=owner),
        actor=SimpleNamespace(profile_id=owner, capabilities={'session:create'}))
    params = dict(board=board_for('default'), task_id=task['id'],
                  run_id=claimed.current_run_id, claim_lock=claimed.claim_lock)
    config = {'model': {'default': 'test-model', 'provider': 'custom'},
              'platform_toolsets': {'cli': ['terminal', 'browser', 'web', 'kanban']},
              'mcp_servers': {'sample': {'command': 'unused-test-command', 'enabled': True}}}
    policy, _ = build_kanban_policy(connection, params, config)
    assert policy.toolsets == ('hermess-research',)
    tools = set(resolve_toolset('hermess-research'))
    assert {'web_search', 'kanban_complete', 'kanban_block'} <= tools
    assert not tools & {'kanban_create', 'delegate_task', 'terminal', 'browser_navigate', 'computer_use'}
    assert policy.current_toolsets(config) == ('hermess-research',)
    # Test the actual worker auto-injection seam as well as the policy.
    import model_tools
    monkeypatch.setenv('HERMES_KANBAN_TASK', task['id'])
    monkeypatch.setattr(model_tools, '_is_dispatcher_owned_worker', lambda: True)
    selected = model_tools._select_tool_names(['hermess-research'], None, True)
    assert selected == tools
    definitions = model_tools.get_tool_definitions(enabled_toolsets=list(policy.toolsets), quiet_mode=True)
    names = {entry['function']['name'] for entry in definitions}
    assert 'kanban_create' not in names and 'terminal' not in names
    assert not any(name.startswith('mcp_') for name in names)
    with pytest.raises(RuntimeStoreError):
        build_kanban_policy(connection, {**params, 'claim_lock': 'stale'}, config)
    connection.actor.profile_id = 'other-owner'
    with pytest.raises(RuntimeStoreError):
        build_kanban_policy(connection, params, config)
