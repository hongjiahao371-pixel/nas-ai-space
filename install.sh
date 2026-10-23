#!/usr/bin/env bash
set -euo pipefail
command -v python3 >/dev/null || { printf '请先在NAS中安装 Python 3 和 Docker。\n' >&2; exit 1; }
python3 - "$@" <<'PY'
import argparse, fcntl, hashlib, importlib.util, json, os, pathlib, re, shutil, subprocess, tarfile, tempfile, urllib.request
os.umask(0o077)
p=argparse.ArgumentParser(description='下载校验后的正式版并打开安装向导')
p.add_argument('--directory',default='nas-ai-space')
p.add_argument('--version',default='')
p.add_argument('--port',type=int,default=18765)
p.add_argument('--upgrade',action='store_true',help='升级已有旧版安装，保留其配置与数据')
a=p.parse_args()
target=pathlib.Path(a.directory).expanduser().resolve()
if target.exists() and not a.upgrade:
    raise SystemExit('目标目录已存在。已有安装请运行 scripts/nas-ai wizard；新安装请指定另一个 --directory。')
version=a.version
if not version:
    req=urllib.request.Request('https://api.github.com/repos/hongjiahao371-pixel/nas-ai-space/releases/latest',headers={'User-Agent':'NAS-AI-Space'})
    with urllib.request.urlopen(req,timeout=30) as r:version=json.load(r)['tag_name']
if not re.fullmatch(r'v\d+\.\d+\.\d+',version):raise SystemExit('版本号无效')
name='nas-ai-space-'+version+'.tar.gz'
base='https://github.com/hongjiahao371-pixel/nas-ai-space/releases/download/'+version+'/'
target.parent.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(dir=target.parent,prefix='.ai-space-install-') as tmp:
    stage=pathlib.Path(tmp);archive=stage/name
    print('正在下载并校验 '+version,flush=True)
    with urllib.request.urlopen(base+name,timeout=60) as r,archive.open('wb') as f:shutil.copyfileobj(r,f)
    with urllib.request.urlopen(base+'SHA256SUMS',timeout=30) as r:checks=r.read(65536).decode()
    hashes=[line.split()[0] for line in checks.splitlines() if len(line.split())==2 and line.split()[1].lstrip('*')==name]
    digest=hashlib.sha256()
    with archive.open('rb') as f:
        for block in iter(lambda:f.read(1048576),b''):digest.update(block)
    if len(hashes)!=1 or digest.hexdigest()!=hashes[0]:raise SystemExit('发布包校验失败，未安装')
    source=stage/'source';source.mkdir();prefix='nas-ai-space-'+version+'/'
    with tarfile.open(archive) as bundle:
        members=bundle.getmembers()
        if len(members)>20000 or sum(m.size for m in members)>512*1024**2:raise SystemExit('发布包大小异常')
        seen=set()
        for member in members:
            if member.isdir():continue
            relative=member.name[len(prefix):];path=pathlib.Path(relative)
            if not member.isfile() or not member.name.startswith(prefix) or not relative or path.is_absolute() or '..' in path.parts or relative in seen:
                raise SystemExit('发布包包含无效路径')
            seen.add(relative);destination=source/path;destination.parent.mkdir(parents=True,exist_ok=True)
            with bundle.extractfile(member) as f,destination.open('wb') as out:shutil.copyfileobj(f,out)
            destination.chmod(0o755 if member.mode&0o111 else 0o644)
    if not (source/'scripts/setup-web.py').exists():raise SystemExit('这个旧版本没有浏览器安装向导，请选择 v1.7.0 或更新版本')
    if a.upgrade:
        if not (target/'.env').is_file() or not (target/'scripts/nas-ai').is_file():raise SystemExit('没有找到已有安装，请核对 --directory')
        checks_file=stage/'SHA256SUMS';checks_file.write_text(checks)
        spec=importlib.util.spec_from_file_location('upgrade',source/'scripts/manage.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);module.ROOT=target
        with (target/'.nas-ai-operation.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise SystemExit('已有升级或恢复操作正在进行，请稍后重试')
            module.update(argparse.Namespace(version=version,archive=str(archive),checksums=str(checks_file)))
    else:
        source.rename(target)
print('下载完成。接下来将显示浏览器安装链接。',flush=True)
subprocess.run(['bash',str(target/'scripts/nas-ai'),'wizard','--port',str(a.port)],check=True)
PY
