"""Coordinated metadata/vector recovery sets. Original media remain NAS backup responsibility."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime,timezone
from pathlib import Path
import httpx
from app.database import Database
from app.services.multimodal import MultimodalService
from app.services.vectors import VectorStore


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def verify_sqlite(path):
    with sqlite3.connect('file:'+str(Path(path).resolve())+'?mode=ro',uri=True) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('备份数据库完整性校验失败')


class RecoveryService:
    def __init__(self,database,settings,vectors=None,multimodal=None):
        self.database=database;self.settings=settings
        self.vectors=vectors or VectorStore(settings)
        self.multimodal=multimodal or MultimodalService(settings)
        self.directory=settings.data_dir/'backups'/'recovery'

    def list(self):
        if not self.directory.exists():return []
        items=[]
        for p in sorted(self.directory.glob('recovery-*.tar'),key=lambda x:x.stat().st_mtime,reverse=True):
            manifest_path=p.with_suffix('.json')
            try:
                manifest=json.loads(manifest_path.read_text())
                items.append({'name':p.name,'bytes':p.stat().st_size,'created_at':manifest['created_at'],
                              'collections':list(manifest['collections']),'multimodal':bool(manifest.get('multimodal')),
                              'age_seconds':round(time.time()-p.stat().st_mtime),'verified':bool(manifest.get('verified_at'))})
            except (OSError,ValueError,KeyError):
                items.append({'name':p.name,'bytes':p.stat().st_size,'verified':False,'age_seconds':round(time.time()-p.stat().st_mtime)})
        return items

    def create(self):
        self.directory.mkdir(parents=True,exist_ok=True);self.directory.chmod(0o700)
        name='recovery-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+os.urandom(3).hex()+'.tar'
        target=self.directory/name;partial=target.with_suffix('.part')
        manifest={'schema':1,'created_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
                  'multimodal':self.multimodal.enabled,'collections':{},'files':{},
                  'scope':'application metadata and both vector indexes; excludes original media, uploads, models and runtime'}
        try:
            with self.multimodal.maintenance_lock() if self.multimodal.enabled else nullcontext():
                with tempfile.TemporaryDirectory(dir=self.directory,prefix='.staging-') as tmp:
                    staging=Path(tmp)
                    self.database.backup(staging/'main.db')
                    if self.multimodal.enabled and self.multimodal.state_path.exists():
                        with sqlite3.connect('file:'+str(self.multimodal.state_path)+'?mode=ro',uri=True) as source:
                            with sqlite3.connect(staging/'multimodal.db') as dest:source.backup(dest)
                        verify_sqlite(staging/'multimodal.db')
                    stores=[self.vectors]+([self.multimodal.vectors] if self.multimodal.enabled else [])
                    for index,store in enumerate(stores):
                        collection=store.settings.qdrant_collection
                        response=store._http().get(f'{store.settings.qdrant_url}/collections/{collection}',timeout=30)
                        if response.status_code==404:
                            manifest['collections'][collection]={'absent':True};continue
                        response.raise_for_status();info=response.json()['result']
                        snapshot=store.create_snapshot(self.settings.automatic_backup_retention)
                        file_name=f'vectors-{index}.snapshot'
                        shutil.copyfile(store.settings.vector_backup_dir/snapshot['name'],staging/file_name)
                        manifest['collections'][collection]={'file':file_name,'points':info['points_count'],
                            'vectors':info['config']['params']['vectors']}
                    for p in staging.iterdir():
                        if p.suffix in {'.db','.snapshot'}:
                            manifest['files'][p.name]={'bytes':p.stat().st_size,'sha256':digest(p)}
                    manifest['verified_at']=datetime.now(timezone.utc).isoformat(timespec='seconds')
                    (staging/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False))
                    with tarfile.open(partial,'w') as archive:
                        for file_name in ['manifest.json',*manifest['files']]:
                            archive.add(staging/file_name,arcname=file_name,recursive=False)
                    partial.chmod(0o600);partial.replace(target)
                    sidecar=target.with_suffix('.json');sidecar.write_text(json.dumps(manifest,ensure_ascii=False));sidecar.chmod(0o600)
            for old in self.list()[self.settings.automatic_backup_retention:]:
                (self.directory/old['name']).unlink(missing_ok=True)
                (self.directory/old['name']).with_suffix('.json').unlink(missing_ok=True)
            return self.list()[0]
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    def _bundle(self,name):
        if Path(name).name!=name or not name.startswith('recovery-') or not name.endswith('.tar'):
            raise ValueError('恢复包名称无效')
        p=self.directory/name
        if not p.is_file():raise FileNotFoundError('恢复包不存在')
        return p

    def unpack_verified(self,name,directory):
        with tarfile.open(self._bundle(name),'r') as archive:
            members=archive.getmembers()
            if len(members)>10 or any(not m.isfile() or Path(m.name).name!=m.name or m.size>4*1024**3 for m in members):
                raise ValueError('恢复包包含不安全条目')
            raw=archive.extractfile('manifest.json')
            if raw is None:raise ValueError('缺少恢复清单')
            manifest=json.loads(raw.read(1024*1024))
            if manifest.get('schema')!=1 or set(m.name for m in members)!={'manifest.json',*manifest['files']}:
                raise ValueError('恢复清单与文件不一致')
            if 'main.db' not in manifest['files']:raise ValueError('恢复包缺少主数据库')
            for m in members:
                if m.name=='manifest.json':continue
                with archive.extractfile(m) as source,(directory/m.name).open('wb') as dest:
                    shutil.copyfileobj(source,dest)
                p=directory/m.name;p.chmod(0o600)
                spec=manifest['files'][m.name]
                if p.stat().st_size!=spec['bytes'] or digest(p)!=spec['sha256']:raise ValueError('恢复包校验不匹配')
                if p.suffix=='.db':verify_sqlite(p)
            for collection,spec in manifest['collections'].items():
                if not spec.get('absent') and spec.get('file') not in manifest['files']:
                    raise ValueError('恢复清单缺少向量文件')
            return manifest

    def verify(self,name):
        with tempfile.TemporaryDirectory() as tmp:
            manifest=self.unpack_verified(name,Path(tmp))
        return {'name':name,'verified':True,'collections':manifest['collections'],'created_at':manifest['created_at']}

    def restore_to(self,name,target:Path,qdrant_url:str,prefix:str=''):
        # Deliberately require a fresh target. Production services must be stopped and
        # restored into a fresh directory before the operator switches the data mount.
        target=target.resolve()
        if target==self.settings.data_dir.resolve() or (target.exists() and any(target.iterdir())):
            raise ValueError('恢复目标必须是独立空目录，不能覆盖正在运行的应用')
        if not re_safe_prefix(prefix):raise ValueError('隔离集合前缀无效')
        target.mkdir(parents=True,exist_ok=True);target.chmod(0o700)
        with tempfile.TemporaryDirectory(dir=target,prefix='.restore-') as tmp:
            staging=Path(tmp);manifest=self.unpack_verified(name,staging)
            import re
            if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',prefix+name) for name in manifest['collections']):
                raise ValueError('恢复集合名称无效')
            results={}
            with httpx.Client(timeout=900,follow_redirects=False) as client:
                for original,spec in manifest['collections'].items():
                    collection=prefix+original
                    base=qdrant_url.rstrip('/')+'/collections/'+collection
                    if client.get(base,timeout=30).status_code!=404:
                        raise ValueError('恢复目标集合已经存在，请使用独立 Qdrant 或新的前缀')
                    if spec.get('absent'):continue
                    response=client.put(base,json={'vectors':spec['vectors']},timeout=30);response.raise_for_status()
                    with (staging/spec['file']).open('rb') as handle:
                        response=client.post(base+'/snapshots/upload',params={'priority':'snapshot'},
                                             files={'snapshot':(spec['file'],handle,'application/octet-stream')})
                        response.raise_for_status()
                    response=client.get(base,timeout=30);response.raise_for_status();actual=response.json()['result']['points_count']
                    if actual!=spec['points']:raise ValueError('恢复后的向量数量不一致')
                    results[collection]=actual
            shutil.copyfile(staging/'main.db',target/'nas-ai-space.db')
            (target/'nas-ai-space.db').chmod(0o600)
            if (staging/'multimodal.db').exists():
                # Expired interactive leases and old heartbeat are not a running worker.
                with sqlite3.connect(staging/'multimodal.db') as db:
                    db.execute('DELETE FROM worker_status')
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='interactive_requests'").fetchone():db.execute('DELETE FROM interactive_requests')
                shutil.copyfile(staging/'multimodal.db',target/'multimodal-index.db');(target/'multimodal-index.db').chmod(0o600)
            verify_sqlite(target/'nas-ai-space.db')
            return {'restored':True,'collections':results,'database':'ok','target':str(target)}


def re_safe_prefix(prefix):
    import re
    return not prefix or bool(re.fullmatch(r'[A-Za-z0-9_-]{1,60}',prefix))
