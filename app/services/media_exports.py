"""Bounded scene sampling and non-destructive, permission-checked clip exports."""
from __future__ import annotations
import math
import re
import shutil
import subprocess
from pathlib import Path
from app.services.multimodal import MultimodalService


def scene_frames(source: Path, temporary: Path, count: int, duration: float) -> list[tuple[float,Path]]:
    if not count or not shutil.which('ffmpeg'):return []
    directory=temporary/'scenes';directory.mkdir(exist_ok=True)
    # A bounded pass complements uniform samples; it is not a full-frame action index.
    command=['ffmpeg','-hide_banner','-loglevel','info','-threads','1','-i',str(source),
             '-an','-vf',"select='gt(scene,0.30)',showinfo,scale='min(960,iw)':-2",
             '-fps_mode','vfr','-frames:v',str(count),'-q:v','3',str(directory/'scene-%02d.jpg')]
    try:
        result=subprocess.run(command,capture_output=True,timeout=60)
        stderr=result.stderr.decode(errors='replace')
    except subprocess.TimeoutExpired as exc:
        stderr=(exc.stderr or b'').decode(errors='replace')
    stamps=[float(x) for x in re.findall(r'pts_time:([\d.]+)',stderr)]
    images=sorted(directory.glob('scene-*.jpg'))
    return [(stamp,image) for stamp,image in zip(stamps,images) if math.isfinite(stamp) and 0<=stamp<duration]


def authorized_file(database,file_id:int,user_id:int|None):
    file=database.get_file(file_id)
    if not file:raise FileNotFoundError('素材不存在')
    if user_id is not None:
        user=database.get_user(user_id)
        if not user or not user.get('enabled') or (user['role'] not in {'owner','admin'} and file['library_id'] not in user.get('library_ids',[])):
            raise PermissionError('素材访问权限已变更')
    return file


def export_clip(database,settings,export_id:int,cancelled=lambda:False):
    row=database.fetchone('SELECT * FROM clip_exports WHERE id=?',(export_id,))
    if not row:raise FileNotFoundError('片段任务不存在')
    file=authorized_file(database,int(row['file_id']),row['user_id'])
    source=MultimodalService(settings).source_path(file)
    if file['kind']!='video':raise ValueError('只有视频可导出片段')
    duration=float(file.get('duration') or 0)
    start,end=float(row['start_time']),min(float(row['end_time']),duration)
    if not all(math.isfinite(x) for x in [start,end]) or start<0 or end<=start or end-start>300:
        raise ValueError('请选择有效的视频区间，最长5分钟')
    directory=settings.data_dir/'clip-exports';directory.mkdir(parents=True,exist_ok=True)
    destination=directory/f'clip-{export_id}.mp4';partial=directory/f'clip-{export_id}.part.mp4'
    try:
        if cancelled():raise InterruptedError('片段导出已取消')
        process=subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','error','-y','-threads','1',
             '-ss',str(start),'-i',str(source),'-t',str(end-start),'-map','0:v:0','-map','0:a?',
             '-vf',"scale='min(1920,iw)':-2",'-c:v','libx264','-threads','1','-preset','veryfast',
             '-crf','23','-pix_fmt','yuv420p','-c:a','aac','-b:a','128k','-movflags','+faststart',str(partial)],
             stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        import time
        started=time.monotonic()
        while process.poll() is None:
            if cancelled() or time.monotonic()-started>900:
                process.kill();process.communicate()
                raise InterruptedError('片段导出已取消或超时')
            time.sleep(.2)
        error=process.communicate()[1]
        if process.returncode:raise RuntimeError(error.decode(errors='replace')[-1000:])
        from app.services.extractors import _probe_media
        actual=float(_probe_media(partial).get('duration') or 0)
        if abs(actual-(end-start))>0.5:raise RuntimeError('导出片段时长校验失败')
        authorized_file(database,int(row['file_id']),row['user_id'])
        MultimodalService(settings).source_path(file)
        partial.chmod(0o600);partial.replace(destination)
        database.execute("UPDATE clip_exports SET status='ready',path=?,error='' WHERE id=?",(str(destination),export_id))
        return {'id':export_id,'duration':actual,'bytes':destination.stat().st_size}
    except Exception as exc:
        partial.unlink(missing_ok=True)
        database.execute("UPDATE clip_exports SET status='error',error=? WHERE id=?",(str(exc)[:500],export_id))
        raise
