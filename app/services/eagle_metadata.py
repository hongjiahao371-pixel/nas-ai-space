"""Limited, sample-backed Eagle pack metadata reader. No network or source writes.

Not a general Eaglepack specification. Only root pack.json + <id>.info originals.
"""
import json
import re
from urllib.parse import urlsplit
from zipfile import ZipFile

MAX_JSON = 2 * 1024 * 1024


def _text(value, limit=4000):
    if not isinstance(value, str) or len(value) > limit or any((ord(c) < 32 and c not in '\n\r\t') or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError('Eagle metadata text is invalid or too long')
    return value


def _component(value):
    value = _text(value, 255)
    if not value or value in {'.', '..'} or any(c in value for c in '/\\\x00'):
        raise ValueError('Eagle metadata contains an unsafe filename component')
    return value


def _json(archive, name):
    info = archive.getinfo(name)
    if info.file_size > MAX_JSON:
        raise ValueError('Eagle metadata JSON exceeds 2 MiB')
    def unique(pairs):
        out = {}
        for k,v in pairs:
            if k in out:
                raise ValueError('Eagle metadata has duplicate JSON keys')
            out[k]=v
        return out
    try:
        with archive.open(info) as stream:
            raw = stream.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise ValueError('Eagle metadata JSON exceeds limit')
        return json.loads(raw, object_pairs_hook=unique)
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid Eagle metadata JSON') from exc


def read_eagle_metadata(archive: ZipFile):
    """Return original relative paths -> display fields, plus explicit warnings.

    No pack.json means legacy generic extraction, with no Eagle metadata claim.
    pack.json with an unknown/ambiguous layout fails closed, before extraction.
    """
    names = archive.namelist()
    if 'pack.json' not in names:
        return {}, ['Unrecognized Eaglepack layout: generic files only; metadata not imported.']
    if len(names) != len(set(names)):
        raise ValueError('Eagle package has duplicate member names')
    pack = _json(archive, 'pack.json')
    if not isinstance(pack, dict) or not isinstance(pack.get('images'), list) or len(pack['images']) > 10000:
        raise ValueError('Unsupported Eaglepack images layout')
    folder_paths = {}
    stack = [(pack.get('folder'), [])] if pack.get('folder') else []
    while stack:
        node, parents = stack.pop()
        if not isinstance(node, dict) or len(parents) >= 32 or len(folder_paths) >= 10000:
            raise ValueError('Unsupported Eagle folder tree')
        fid = _component(node.get('id'))
        if fid in folder_paths:
            raise ValueError('Duplicate Eagle folder ID')
        label = _text(node.get('name'), 255)
        folder_paths[fid] = parents + [label]
        children = node.get('children', [])
        if not isinstance(children, list):
            raise ValueError('Unsupported Eagle folder children')
        stack.extend((child, parents+[label]) for child in children)
    result, warnings, seen = {}, [], set()
    for item in pack['images']:
        if not isinstance(item, dict):
            raise ValueError('Invalid Eagle image record')
        iid = _component(item.get('id'))
        if iid in seen:
            raise ValueError('Duplicate Eagle item ID')
        seen.add(iid)
        title, ext = _component(item.get('name')), _component(item.get('ext'))
        if not re.fullmatch('[A-Za-z0-9]{1,16}', ext):
            raise ValueError('Unsupported Eagle media extension')
        path = f'{iid}.info/{title}.{ext}'
        meta_name = f'{iid}.info/metadata.json'
        if path not in names or meta_name not in names:
            raise ValueError('Eagle original or item metadata missing; bookmarks are not supported')
        detail = _json(archive, meta_name)
        if not isinstance(detail, dict):
            raise ValueError('Invalid Eagle item metadata')
        # Never choose between competing records silently. Null folder IDs in the
        # official-blog-linked sample are ignored, not treated as real folders.
        for key in ('id','name','ext','tags','star','annotation','url','folders','isDeleted'):
            left, right = item.get(key), detail.get(key)
            if key == 'folders':
                if not isinstance(left or [], list) or not isinstance(right or [], list):
                    raise ValueError('Invalid Eagle folder references')
                left = [x for x in (left or []) if x is not None]
                right = [x for x in (right or []) if x is not None]
            if left != right:
                raise ValueError('Conflicting Eagle pack and item metadata')
        if 'isDeleted' in item and type(item['isDeleted']) is not bool:
            raise ValueError('Invalid Eagle deleted flag')
        if item.get('isDeleted') is True:
            warnings.append(f'Skipped deleted Eagle item {iid}')
            continue
        tags = item.get('tags', [])
        if not isinstance(tags, list) or len(tags) > 100:
            raise ValueError('Invalid Eagle tags')
        tags = [_text(t, 255) for t in tags]
        rating = item.get('star', 0)
        if type(rating) is not int or not 0 <= rating <= 5:
            raise ValueError('Invalid Eagle rating')
        note = _text(item.get('annotation', ''), 12000)
        url = _text(item.get('url', ''), 4096)
        if url:
            try:
                parsed = urlsplit(url)
                if any(c.isspace() for c in url) or parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError()
            except ValueError:
                url = ''
                warnings.append(f'Omitted unsafe source URL on Eagle item {iid}')
        folders = item.get('folders', [])
        if not isinstance(folders, list) or len(folders)>100:
            raise ValueError('Invalid Eagle folder references')
        context=[]
        for fid in folders:
            if fid is None:
                continue
            if not isinstance(fid,str):
                raise ValueError('Invalid Eagle folder ID')
            if fid in folder_paths:
                context.append(folder_paths[fid])
            else:
                warnings.append(f'Unresolved folder reference on Eagle item {iid}')
        size = item.get('size')
        if type(size) is not int or size != archive.getinfo(path).file_size:
            raise ValueError('Eagle original size does not match metadata')
        # Description remains plain text; no HTML, URLs or paths are executed.
        description = '\n'.join(['Eagle imported metadata (limited layout)',
            'Tags: '+', '.join(tags), 'Note: '+note, 'Source URL: '+url,
            'Folder context: '+json.dumps(context,ensure_ascii=False)])
        if len(description)>16000:
            raise ValueError('Eagle combined metadata exceeds 16000 characters')
        result[path] = {'title':title,'rating':rating,'description':description}
    return result, warnings
