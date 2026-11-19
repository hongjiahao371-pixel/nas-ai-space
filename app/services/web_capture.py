"""Bounded, user-selected text imports. Never resolves or fetches a source URL."""
import hashlib
import json
import os
import secrets
import stat
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
MAX_CAPTURE_BYTES = 256 * 1024
SCHEMA = 'nas-ai-web-capture/v1'

def _pairs(items):
    result = {}
    for key, value in items:
        if key in result: raise ValueError('重复字段')
        result[key] = value
    return result

def parse_capture(raw):
    if not raw or len(raw) > MAX_CAPTURE_BYTES: raise ValueError('摘录大小限制')
    try: data = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc: raise ValueError('无效JSON') from exc
    fields = {'schema','title','source_url','captured_at','text','selection_only'}
    if not isinstance(data,dict) or set(data)!=fields or data['schema']!=SCHEMA or data['selection_only'] is not True: raise ValueError('不支持的字段')
    for key, maximum in [('title',300),('source_url',2048),('captured_at',40),('text',100000)]:
        value=data[key]
        if not isinstance(value,str) or not value.strip() or len(value)>maximum: raise ValueError('字段超限')
        if any(0xD800<=ord(c)<=0xDFFF or (ord(c)<32 and (key!='text' or c not in '\t\n\r')) or ord(c)==127 for c in value): raise ValueError('无效字符')
    try:
        u=urlsplit(data['source_url'])
        if u.scheme not in {'http','https'} or not u.hostname or u.username is not None or u.password is not None or u.query or u.fragment or '\\' in data['source_url'] or any(c.isspace() for c in data['source_url']): raise ValueError()
        if u.port is not None and not 1<=u.port<=65535: raise ValueError()
        stamp=datetime.fromisoformat(data['captured_at'].replace('Z','+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds()!=0: raise ValueError()
    except (ValueError,TypeError) as exc: raise ValueError('来源URL或UTC时间无效') from exc
    return data

def capture_document(data):
    metadata={k:v for k,v in data.items() if k!='text'}
    metadata['text_sha256']=hashlib.sha256(data['text'].encode('utf-8')).hexdigest()
    return ('网页选区摘录（用户提供，来源和采集时间未经独立核验）\n'+json.dumps(metadata,ensure_ascii=False,sort_keys=True)+'\n\n'+data['text']+'\n').encode('utf-8')

def publish_capture(settings, project_id, data):
    target=settings.ingest_root/f'project-{project_id}';root=settings.upload_root.resolve()
    try: relative=target.relative_to(root)
    except ValueError as exc: raise ValueError('目录越界') from exc
    if any(part in {'.','..'} for part in relative.parts): raise ValueError('目录越界')
    body=capture_document(data);name='web-capture-'+hashlib.sha256(body).hexdigest()+'.txt'
    directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    temporary=None
    try:
        for part in relative.parts:
            try: os.mkdir(part,mode=0o700,dir_fd=directory)
            except FileExistsError: pass
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            os.close(directory);directory=child
        temporary='.capture-'+secrets.token_hex(16)+'.part'
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        with os.fdopen(fd,'wb') as handle: handle.write(body);handle.flush();os.fsync(handle.fileno())
        try:
            os.link(temporary,name,src_dir_fd=directory,dst_dir_fd=directory,follow_symlinks=False)
            os.fsync(directory)
            return target/name,True
        except FileExistsError:
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            with os.fdopen(fd,'rb') as existing:
                if not stat.S_ISREG(os.fstat(existing.fileno()).st_mode) or existing.read(len(body)+1)!=body: raise ValueError('已有内容已修改，不覆盖')
            return target/name,False
    finally:
        if temporary is not None:
            try: os.unlink(temporary,dir_fd=directory)
            except FileNotFoundError: pass
        os.close(directory)
