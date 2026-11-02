"""Private desktop task adapter. Kanban remains the only task state machine."""
from contextlib import closing, contextmanager
import hashlib
import json
import logging
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.hermess_private_api import enabled

router = APIRouter(prefix='/api/hermess/tasks')
log = logging.getLogger(__name__)
CREATOR = 'hermess-personal-v1'
VERSION = 1


def board_for(profile: str) -> str:
    return 'hermess-personal-' + hashlib.sha256(profile.encode()).hexdigest()[:16]


def research_task(task, board: str) -> bool:
    # The dashboard may reassign a card; the research restriction travels with
    # its immutable creator marker and must not disappear on reassignment.
    return task.created_by == CREATOR


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['capabilities', 'list', 'create', 'detail', 'reply', 'resume', 'archive', 'events']
    profile: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9_-]+$')
    request_id: str | None = None
    task_id: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=20000)
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=200)


def _uuid(value: str | None) -> str:
    try:
        return str(UUID(value or ''))
    except ValueError:
        raise HTTPException(422, 'invalid_request_id') from None


def _text(value: str | None) -> str:
    if not value or not value.strip() or '\0' in value:
        raise HTTPException(422, 'missing_text')
    return value.strip()


def _authorize(request: Request, profile: str) -> None:
    # Never accept a dashboard cookie, remote bearer token, browser Origin, or
    # query token as authority to create background desktop work.
    from hermes_cli.web_server import _has_valid_session_token
    from hermes_cli.web_server_profiles import _resolve_profile_dir
    if not enabled():
        raise HTTPException(404, 'not_found')
    if 'origin' in request.headers:
        raise HTTPException(403, 'browser_origin_refused')
    grant = getattr(request.state, 'native_http_principal', None)
    if grant is None and (not request.headers.get('x-hermes-session-token')
                          or not _has_valid_session_token(request)):
        raise HTTPException(401, 'native_auth_required')
    target = _resolve_profile_dir(profile).resolve(strict=True)
    if grant is not None and target != Path(grant['profile_id']).resolve(strict=True):
        raise HTTPException(403, 'profile_scope_mismatch')


def _task(conn, task_id, profile):
    task = kb.get_task(conn, task_id or '')
    if task is None or task.created_by != CREATOR or (task.assignee or 'default') != profile:
        raise HTTPException(404, 'task_not_found')
    return task


def _view(conn, task):
    # Deliberately omit process identities, claim secrets and absolute paths.
    return dict(id=task.id, title=task.title, body=task.body, status=task.status,
                profile=task.assignee or 'default', created_at=task.created_at,
                result=task.result, summary=kb.latest_summary(conn, task.id),
                failure=task.last_failure_error, block_kind=task.block_kind)


def _events(conn, profile, after, limit):
    rows = conn.execute(
        'SELECT e.id,e.task_id,e.kind,e.created_at FROM task_events e '
        'JOIN tasks t ON t.id=e.task_id WHERE e.id>? AND t.created_by=? '
        'AND COALESCE(t.assignee,\'default\')=? ORDER BY e.id LIMIT ?',
        (after, CREATOR, profile, limit)).fetchall()
    return [dict(row) for row in rows]


def _detail(conn, task):
    comments = conn.execute('SELECT id,author,body,created_at FROM task_comments '
                            'WHERE task_id=? ORDER BY id DESC LIMIT 200', (task.id,)).fetchall()
    attachments = kb.list_attachments(conn, task.id)
    return dict(task=_view(conn, task), comments=[dict(row) for row in reversed(comments)],
                events=_events_for_task(conn, task.id),
                attachments=[dict(id=a.id, filename=a.filename, content_type=a.content_type,
                                  size=a.size) for a in attachments])


def _events_for_task(conn, task_id):
    rows = conn.execute('SELECT id,kind,created_at FROM task_events WHERE task_id=? '
                        'ORDER BY id DESC LIMIT 200', (task_id,)).fetchall()
    return [dict(row) for row in reversed(rows)]


def _live_workers(conn, task_id):
    from hermes_cli.kanban_db_dispatch import _worker_alive
    # block_task clears the task PID without killing its interpreter. Inspect
    # persisted run identities too before permitting another dispatch.
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind IN ('spawned','worker_bound')",
                        (task_id,)).fetchall()
    for row in rows:
        data = json.loads(row[0] or '{}')
        pid = data.get('pid') or data.get('worker_pid')
        fingerprint = data.get('started_at') or data.get('worker_started_at')
        if pid and _worker_alive(pid, fingerprint):
            return True
    return False


def _resume(conn, payload):
    task = _task(conn, payload.task_id, payload.profile)
    request_id = _uuid(payload.request_id)
    # Serialize desktop resume requests across processes. The task itself is
    # still transitioned exclusively by the existing Kanban operation.
    db_path = Path(conn.execute('PRAGMA database_list').fetchone()[2])
    with _operation_lock(db_path.with_name('hermess-resume.lock')):
        previous = conn.execute("SELECT kind,payload FROM task_events WHERE task_id=? AND kind IN ('hermess_resuming','hermess_resumed') ORDER BY id DESC",
                                (task.id,)).fetchall()
        saved = next((row for row in previous if json.loads(row['payload']).get('request_id') == request_id), None)
        if saved:
            if saved['kind'] != 'hermess_resumed':
                raise HTTPException(409, 'resume_outcome_unknown')
            return _detail(conn, _task(conn, task.id, payload.profile))
        task = _task(conn, task.id, payload.profile)
        if task.status != 'blocked' or _live_workers(conn, task.id):
            raise HTTPException(409, 'task_not_safely_resumable')
        with kb.write_txn(conn):
            kb._append_event(conn, task.id, 'hermess_resuming', {'request_id': request_id})
        if not kb.unblock_task(conn, task.id):
            raise HTTPException(409, 'task_changed')
        with kb.write_txn(conn):
            kb._append_event(conn, task.id, 'hermess_resumed', {'request_id': request_id})
    return _detail(conn, _task(conn, task.id, payload.profile))


@contextmanager
def _operation_lock(path):
    with path.open('a+b') as handle:
        try:
            acquired = kbc._try_lock_nb(handle)
        except OSError:
            acquired = False
        if not acquired:
            raise HTTPException(409, 'operation_in_progress')
        try:
            yield
        finally:
            kbc._unlock(handle)


def install_private_routes(app):
    if enabled() and not getattr(app.state, 'hermess_tasks_installed', False):
        app.include_router(router)
        app.state.hermess_tasks_installed = True


def _create(conn, payload, board):
    request_id = _uuid(payload.request_id)
    title, body = _text(payload.title), _text(payload.body)
    digest = hashlib.sha256(json.dumps([title, body], ensure_ascii=False).encode()).hexdigest()
    key = f'{CREATOR}:{request_id}'
    # The outer IMMEDIATE txn makes the existing Kanban pre-check atomic.
    # Include archived cards: retries must not resurrect a stopped request.
    with kb.write_txn(conn):
        previous = conn.execute('SELECT id,title,body FROM tasks WHERE idempotency_key=?', (key,)).fetchone()
        if previous:
            if previous['title'] != title or previous['body'] != body:
                raise HTTPException(409, 'request_id_conflict')
            task_id = previous['id']
        else:
            task_id = kb.create_task(conn, title=title, body=body, assignee=payload.profile,
                created_by=CREATOR, workspace_kind='scratch', idempotency_key=key,
                max_runtime_seconds=1800, max_retries=0, board=board)
            kb._append_event(conn, task_id, 'hermess_created', {'request_id': request_id, 'digest': digest})
    return _detail(conn, _task(conn, task_id, payload.profile))


def _reply(conn, payload):
    task = _task(conn, payload.task_id, payload.profile)
    request_id, body = _uuid(payload.request_id), _text(payload.body)
    digest = hashlib.sha256(body.encode()).hexdigest()
    # One durable input operation, even if its HTTP answer was lost.
    with kb.write_txn(conn):
        task = _task(conn, task.id, payload.profile)
        rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='hermess_input'",
                            (task.id,)).fetchall()
        saved = next((json.loads(row[0]) for row in rows if json.loads(row[0]).get('request_id') == request_id), None)
        if saved:
            if saved['digest'] != digest:
                raise HTTPException(409, 'request_id_conflict')
            return _detail(conn, task)
        if task.status != 'blocked':
            raise HTTPException(409, 'task_not_blocked')
        # unblock_task has its own transaction and post-commit semantics. A
        # reply is recorded first; resume is a separate explicit operation.
        kb.add_comment(conn, task.id, 'user', body)
        kb._append_event(conn, task.id, 'hermess_input', {'request_id': request_id, 'digest': digest})
    return _detail(conn, _task(conn, task.id, payload.profile))


@router.post('/request')
def task_request(payload: TaskRequest, request: Request):
    _authorize(request, payload.profile)
    if payload.action == 'capabilities':
        return dict(version=VERSION, capabilities=['create', 'list', 'detail', 'reply', 'resume', 'archive', 'events'],
                    research_toolsets=['hermess-research'])
    board = board_for(payload.profile)
    if not kb.board_exists(board):
        if payload.action != 'create':
            if payload.action == 'events':
                return {'events': [], 'next': payload.after}
            if payload.action == 'list':
                return {'tasks': []}
            raise HTTPException(404, 'task_not_found')
        kb.create_board(board, name='Hermess personal tasks')
    with kb.scoped_current_board(board), closing(kbc.connect(board=board)) as conn:
        if payload.action == 'list':
            tasks = conn.execute('SELECT id FROM tasks WHERE created_by=? AND COALESCE(assignee,\'default\')=? '
                                 'ORDER BY created_at DESC,id LIMIT ? OFFSET ?',
                                 (CREATOR, payload.profile, payload.limit, payload.after)).fetchall()
            return dict(tasks=[_view(conn, _task(conn, row['id'], payload.profile)) for row in tasks])
        if payload.action == 'events':
            events = _events(conn, payload.profile, payload.after, payload.limit)
            return dict(events=events, next=events[-1]['id'] if events else payload.after)
        correlation = _uuid(payload.request_id) if payload.action in {'create', 'reply', 'resume'} else payload.task_id
        log.info('hermess task operation start action=%s operation=%s', payload.action, correlation)
        try:
            if payload.action == 'create':
                result = _create(conn, payload, board)
            elif payload.action == 'reply':
                result = _reply(conn, payload)
            elif payload.action == 'resume':
                result = _resume(conn, payload)
            elif payload.action == 'archive':
                task = _task(conn, payload.task_id, payload.profile)
                if task.status != 'archived' and not kb.archive_task(conn, task.id):
                    raise HTTPException(409, 'task_changed')
                result = _detail(conn, _task(conn, task.id, payload.profile))
                result['termination_pending'] = _live_workers(conn, task.id)
            else:
                result = _detail(conn, _task(conn, payload.task_id, payload.profile))
        except Exception:
            log.error('hermess task operation failed action=%s operation=%s', payload.action, correlation)
            raise
        log.info('hermess task operation complete action=%s operation=%s', payload.action, correlation)
        return result
