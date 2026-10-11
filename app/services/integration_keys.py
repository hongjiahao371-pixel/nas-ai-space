"""Revocable, library-scoped machine credentials. Raw tokens are never persisted."""
from __future__ import annotations
import secrets
from datetime import datetime, timedelta, timezone
from app.database import utc_now
from app.security import token_digest


def public_keys(db):
    rows = db.fetchall("SELECT id,name,token_prefix,created_at,expires_at,revoked_at FROM integration_keys ORDER BY id DESC")
    for row in rows:
        row['library_ids'] = [int(x['library_id']) for x in db.fetchall(
            "SELECT library_id FROM integration_key_libraries WHERE key_id=? ORDER BY library_id", (row['id'],))]
        row['active'] = not row['revoked_at'] and (not row['expires_at'] or row['expires_at'] > utc_now())
    return rows


def create_key(db, name, library_ids, expires_days=None):
    ids = sorted(set(library_ids))
    if not name.strip() or not ids:
        raise ValueError('请输入名称并至少选择一个媒体库')
    for identity in ids:
        library = db.get_library(identity)
        if not library or not library.get('enabled'):
            raise ValueError('只能授权已启用的媒体库')
    token = 'ais_read_' + secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=expires_days)).isoformat(timespec='seconds') if expires_days else None
    with db.transaction() as connection:
        identity = connection.execute(
            "INSERT INTO integration_keys(name,token_hash,token_prefix,created_at,expires_at) VALUES (?,?,?,?,?)",
            (name.strip(), token_digest(token), token[:13] + '…', utc_now(), expires)).lastrowid
        connection.executemany("INSERT INTO integration_key_libraries VALUES (?,?)", [(identity, x) for x in ids])
    return {'id': identity, 'name': name.strip(), 'token': token, 'library_ids': ids, 'expires_at': expires,
            'readonly': True, 'notice': '密钥仅显示这一次，请保存在调用方的服务端配置中'}


def resolve_key(db, token):
    row = db.fetchone("SELECT id,name FROM integration_keys WHERE token_hash=? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)",
                       (token_digest(token), utc_now()))
    if not row:
        return None
    ids = [int(x['library_id']) for x in db.fetchall(
        "SELECT p.library_id FROM integration_key_libraries p JOIN libraries l ON l.id=p.library_id WHERE p.key_id=? AND l.enabled=1 ORDER BY p.library_id", (row['id'],))]
    return {'user_id': None, 'username': 'integration:' + str(row['id']), 'display_name': row['name'],
            'role': 'integration', 'library_ids': ids, 'auth_type': 'integration_key', 'key_id': row['id']}


def allowed_request(method, path):
    import re
    if method == 'GET':
        return path in {'/api/integrations/capabilities', '/api/search'} or bool(re.fullmatch(
            r'/api/files/[1-9][0-9]*(?:/(?:content|thumbnail|moments|frame))?', path))
    return method == 'POST' and path in {'/api/ask', '/api/search/image', '/api/integrations/vision'}
