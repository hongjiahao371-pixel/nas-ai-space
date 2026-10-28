#!/usr/bin/env python3
"""Verified release upgrades and non-destructive, coordinated index recovery."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
RUNNER = Path(__file__).resolve().parent / 'nas-ai'
API = 'https://api.github.com/repos/hongjiahao371-pixel/nas-ai-space'
PROTECTED = {'.git', '.env', '.nas-ai-profile', '.nas-ai-releases', '.nas-ai-restores',
             '.nas-ai-release-files.json', '.venv', 'data', 'library', 'uploads', 'recycle', 'runtime', 'work', 'backups'}


def require_supported_architecture(version=None):
    architecture = platform.machine().lower()
    if architecture not in {'x86_64', 'amd64', 'aarch64', 'arm64'}:
        raise ValueError('不支持此处理器架构：' + architecture + '。需要64位 x86-64 或 ARM64 系统；32位 ARM 请先安装64位系统。')
    if struct.calcsize('P') != 8:
        raise ValueError('当前使用32位系统或 Python。ARM64 / x86-64 安装需要64位系统和64位 Python 3。')
    if architecture in {'aarch64', 'arm64'} and version:
        if not re.fullmatch(r'v?\d+\.\d+\.\d+', version):
            raise ValueError('版本号无效')
        if tuple(map(int, version.lstrip('v').split('.'))) < (1, 7, 3):
            raise ValueError('ARM64 需要 v1.7.3 或更新版本，不能安装或回退到仅支持 x86-64 的 ' + version)
    return 'arm64' if architecture in {'aarch64', 'arm64'} else 'amd64'


def configuration():
    result = {}
    if (ROOT / '.env').exists():
        for line in (ROOT / '.env').read_text().splitlines():
            key, separator, value = line.partition('=')
            if separator and not key.startswith('#'):
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                result[key] = value.replace('$$', '$')
    return result


def set_config(values):
    path = ROOT / '.env'
    lines = path.read_text().splitlines()
    lines = [line for line in lines if line.partition('=')[0] not in values]
    for key, value in values.items():
        if '\n' in value or '\r' in value:
            raise ValueError('配置值不能包含换行')
        escaped = value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '$$')
        lines.append(f'{key}="{escaped}"')
    temporary = path.with_suffix('.new')
    temporary.write_text('\n'.join(lines) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def validate_proxy(value):
    if not value:
        return ''
    parsed = urlsplit(value)
    if (len(value) > 2048 or any(c.isspace() for c in value) or parsed.scheme not in {'http', 'https'}
            or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {'', '/'}):
        raise ValueError('下载代理请填写 http://地址:端口，不支持带账号密码的代理地址')
    if parsed.port is None:
        raise ValueError('下载代理需要填写端口')
    return value.rstrip('/')


def run(*args, capture=False):
    return subprocess.run(['bash', str(RUNNER), *args], cwd=ROOT, check=True,
                          env={**os.environ, 'NAS_AI_PROJECT_DIR': str(ROOT)},
                          text=True, stdout=subprocess.PIPE if capture else None)


def request(path, method='GET'):
    config = configuration()
    req = urllib.request.Request('http://127.0.0.1:' + config.get('NAS_AI_PORT', '8766') + path,
                                 headers={'Authorization': 'Bearer ' + config.get('NAS_AI_API_TOKEN', '')},
                                 method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=900) as response:
        return json.load(response)


def sha(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def safe_member(name):
    path = Path(name)
    return bool(name and not path.is_absolute() and '..' not in path.parts
                and path.parts[0] not in PROTECTED and not path.parts[0].startswith('.nas-ai-'))


def unpack_release(archive, destination, version, checksums):
    if not re.fullmatch(r'v\d+\.\d+\.\d+', version):
        raise ValueError('版本号无效')
    filename = 'nas-ai-space-' + version + '.tar.gz'
    entries = [line.split() for line in checksums.read_text().splitlines()]
    expected = [parts[0] for parts in entries if len(parts) == 2 and parts[1].lstrip('*') == filename]
    if len(expected) != 1 or not re.fullmatch('[a-f0-9]{64}', expected[0]) or sha(archive) != expected[0]:
        raise ValueError('发布包校验失败，现有程序未改动')
    prefix = 'nas-ai-space-' + version + '/'
    with tarfile.open(archive, 'r:gz') as bundle:
        members = bundle.getmembers()
        if len(members) > 20000 or sum(m.size for m in members) > 512 * 1024 * 1024:
            raise ValueError('发布包大小异常')
        seen = set()
        for member in members:
            if member.isdir():
                continue
            if not member.isfile() or not member.name.startswith(prefix):
                raise ValueError('发布包包含不安全条目')
            name = member.name[len(prefix):]
            if not safe_member(name) or name in seen:
                raise ValueError('发布包路径无效或重复')
            seen.add(name)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(member) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    for required in ['scripts/nas-ai', 'scripts/manage.py', 'docker-compose.yml', 'app/main.py']:
        if not (destination / required).is_file():
            raise ValueError('发布包缺少必要文件：' + required)
    source = (destination / 'app/main.py').read_text()
    if f'version="{version[1:]}"' not in source:
        raise ValueError('发布包版本与选择的版本不一致')
    return sorted(seen)


def public_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'NAS-AI-Space'}), timeout=30) as response:
        return json.load(response)


def fetch(url, destination):
    with urllib.request.urlopen(url, timeout=60) as response, destination.open('wb') as output:
        shutil.copyfileobj(response, output)


def code_files():
    result = []
    for current, dirs, files in os.walk(ROOT):
        base = Path(current)
        dirs[:] = [d for d in dirs if d != '__pycache__' and not (base / d).is_symlink()
                   and safe_member(str((base / d).relative_to(ROOT)))]
        for name in files:
            path = base / name
            relative = str(path.relative_to(ROOT))
            if safe_member(relative) and not path.is_symlink() and not name.endswith(('.pyc', '.db', '.tar', '.tar.gz')):
                result.append(relative)
    return sorted(result)


def copy_code(source, destination, names):
    for name in names:
        target = destination / name
        cursor = destination
        linked = cursor.is_symlink()
        for part in Path(name).parts:
            cursor = cursor / part
            linked = linked or cursor.is_symlink()
        if linked:
            raise ValueError('安装目录包含符号链接，已停止：' + name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)


def checked_start(expected=None):
    run('start')
    health = request('/api/health')
    if expected and health.get('version') != expected:
        raise RuntimeError('启动后的版本与目标版本不一致')
    if not request('/api/ready').get('ready'):
        raise RuntimeError('升级后就绪检查失败')


def vision_default_migration(config):
    return {key: 'qwen3-vl:2b-instruct-q4_K_M' for key in ('NAS_AI_VISION_MODEL', 'NAS_AI_CHAT_MODEL')
            if config.get(key) == 'qwen3-vl:2b'}


def update(args):
    require_supported_architecture(args.version)
    if not (ROOT / '.nas-ai-profile').is_file():
        raise ValueError('这是手工部署目录，请使用原部署流程；通用更新不会猜测或替换现有服务组合')
    tracked = ROOT / '.nas-ai-release-files.json'
    if tracked.exists():
        baseline = json.loads(tracked.read_text())
        if isinstance(baseline, dict) and any(not safe_member(name) or not (ROOT / name).is_file() or sha(ROOT / name) != digest
                                              for name, digest in baseline.items()):
            raise ValueError('已安装程序有本地改动，请先保存；程序未改动')
    elif (ROOT / '.git').exists():
        status = subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True)
        if status.strip():
            raise ValueError('源码有本地改动，请先保存或提交；程序未改动')
    old = request('/api/health')['version']
    try:
        require_supported_architecture(old)
    except ValueError as error:
        raise ValueError('现有版本不能作为 ARM64 升级的回退版本，请保留原目录，在新目录安装 v1.7.3 或更新版本。') from error
    with tempfile.TemporaryDirectory(prefix='.nas-ai-download-', dir=ROOT.parent) as temporary:
        stage = Path(temporary)
        if args.archive:
            version, archive, checksums = args.version, Path(args.archive), Path(args.checksums)
        else:
            release = public_json(API + ('/releases/tags/' + args.version if args.version else '/releases/latest'))
            version = release['tag_name']
            require_supported_architecture(version)
            if release.get('draft') or release.get('prerelease'):
                raise ValueError('请选择已正式发布的版本')
            if version == 'v' + old:
                print('已经是当前正式版：' + version, flush=True)
                return
            archive, checksums = stage / 'release.tar.gz', stage / 'SHA256SUMS'
            base = 'https://github.com/hongjiahao371-pixel/nas-ai-space/releases/download/' + version + '/'
            if not re.fullmatch(r'v\d+\.\d+\.\d+', version):
                raise ValueError('版本号无效')
            print('正在下载并校验 ' + version, flush=True)
            fetch(base + 'nas-ai-space-' + version + '.tar.gz', archive)
            fetch(base + 'SHA256SUMS', checksums)
        candidate = stage / 'source'
        names = unpack_release(archive, candidate, version, checksums)
        if shutil.disk_usage(ROOT).free < 2 * 1024**3 + archive.stat().st_size * 5:
            raise ValueError('升级需要至少2GB空闲空间并容纳新旧源码')
        previous_names = code_files()
        snapshot = ROOT / '.nas-ai-releases' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
        snapshot.mkdir(parents=True, mode=0o700)
        copy_code(ROOT, snapshot / 'source', previous_names)
        config = configuration()
        migration = vision_default_migration(config)
        manifest = {'old_version': old, 'new_version': version[1:], 'old_files': previous_names, 'new_files': names,
                    'model_defaults_before': {key: config[key] for key in migration}}
        (snapshot / 'state.json').write_text(json.dumps(manifest))
        print('正在保存升级前的数据与索引备份', flush=True)
        request('/api/operations/backups', 'POST')
        services = ['app'] + (['multimodal-indexer'] if configuration().get('NAS_AI_SEARCH_MODE') == 'media' else [])
        run('compose-exec', 'stop', *services)
        try:
            copy_code(candidate, ROOT, names)
            # Only files from the previous release manifest can be pruned; user files stay intact.
            tracked = ROOT / '.nas-ai-release-files.json'
            if tracked.exists():
                for name in json.loads(tracked.read_text()):
                    if name not in names and safe_member(name):
                        (ROOT / name).unlink(missing_ok=True)
            if migration:
                set_config(migration)
                print('正在更新旧版默认看图模型为直接回答版本；自定义模型配置保持原值', flush=True)
            checked_start(version[1:])
        except Exception:
            print('升级未通过，正在回退程序；数据备份与诊断材料均已保留', flush=True)
            restore_code(snapshot)
            raise
        (ROOT / '.nas-ai-release-files.json').write_text(json.dumps({name: sha(ROOT / name) for name in names}))
        print('升级成功：' + version + '。旧程序和升级前备份已保留。', flush=True)


def restore_code(snapshot):
    state = json.loads((snapshot / 'state.json').read_text())
    require_supported_architecture(state['old_version'])
    for name in set(state['new_files']) - set(state['old_files']):
        if safe_member(name):
            (ROOT / name).unlink(missing_ok=True)
    copy_code(snapshot / 'source', ROOT, state['old_files'])
    if state.get('model_defaults_before'):
        set_config(state['model_defaults_before'])
    checked_start(state['old_version'])
    (ROOT / '.nas-ai-release-files.json').write_text(json.dumps({name: sha(ROOT / name) for name in state['old_files']}))


def restore(args):
    if not (ROOT / '.nas-ai-profile').is_file():
        raise ValueError('手工部署目录请使用原恢复流程')
    config = configuration()
    source = (ROOT / config.get('NAS_AI_DATA_PATH', 'data')).resolve()
    name = args.name
    if Path(name).name != name or not re.fullmatch(r'recovery-[A-Za-z0-9-]+\.tar', name):
        raise ValueError('完整素材方案请选择 recovery- 开头的完整恢复包（.tar）')
    if not (source / 'backups/recovery' / name).is_file():
        raise ValueError('恢复包不存在')
    if not args.yes and input('输入 RESTORE ' + name + ' 确认恢复：') != 'RESTORE ' + name:
        raise ValueError('已取消恢复')
    # Verify before changing mounts or stopping services.
    check = 'from app.config import settings; from app.database import Database; from app.services.recovery import RecoveryService; import sys; print(RecoveryService(Database(settings.database_path),settings).verify(sys.argv[1])["verified"])'
    run('compose-exec', 'run', '--rm', '--no-deps', '--entrypoint', 'python', 'app', '-c', check, name)
    request('/api/operations/backups', 'POST')
    identifier = uuid.uuid4().hex[:12]
    recovery = ROOT / '.nas-ai-restores' / identifier
    target = recovery / 'data'
    target.mkdir(parents=True, mode=0o700)
    original = (ROOT / '.env').read_bytes()
    saved = recovery / 'previous.env'
    saved.write_bytes(original)
    saved.chmod(0o600)
    services = ['app'] + (['multimodal-indexer'] if config.get('NAS_AI_SEARCH_MODE') == 'media' else [])
    run('compose-exec', 'stop', *services)
    try:
        project = config.get('COMPOSE_PROJECT_NAME', 'nas-ai-space')
        set_config({'NAS_AI_DATA_PATH': str(target), 'NAS_AI_QDRANT_VOLUME': project + '_restore_' + identifier})
        run('compose-exec', 'up', '-d', '--no-deps', 'qdrant')
        for _ in range(30):
            probe = run('compose-exec', 'ps', '--format', 'json', 'qdrant', capture=True).stdout
            if 'healthy' in probe and 'unhealthy' not in probe:
                break
            time.sleep(2)
        run('compose-exec', 'run', '--rm', '--no-deps', '-v', str(source) + ':/source:ro',
            '-v', str(target) + ':/target', '-e', 'NAS_AI_DATA_DIR=/source', '--entrypoint', 'python',
            'app', 'scripts/restore-recovery.py', name, '--confirm', name, '--target', '/target', '--qdrant-url', 'http://qdrant:6333')
        checked_start()
    except Exception:
        (ROOT / '.env').write_bytes(original)
        (ROOT / '.env').chmod(0o600)
        run('start')
        raise
    print('完整恢复成功。恢复前的数据目录和向量卷均保留，原文件与模型未改动。', flush=True)


def change_mode(mode):
    if not (ROOT / '.nas-ai-profile').is_file() or mode not in {'media', 'text'}:
        raise ValueError('当前安装不能切换这个检索方案')
    config = configuration()
    old_mode = config.get('NAS_AI_SEARCH_MODE', 'text')
    if old_mode == mode:
        print('已经在使用这个检索方案', flush=True)
        return
    if request('/api/health').get('bootstrap_required'):
        raise ValueError('请先打开 AI Space 完成管理员设置，再切换检索方案')
    request('/api/operations/backups', 'POST')
    original = (ROOT / '.env').read_bytes()
    if old_mode == 'media':
        run('compose-exec', 'stop', 'multimodal-indexer', 'multimodal')
    try:
        set_config({'NAS_AI_SEARCH_MODE': mode})
        checked_start()
    except Exception:
        (ROOT / '.env').write_bytes(original)
        (ROOT / '.env').chmod(0o600)
        run('start')
        raise
    print('检索方案已切换。原资料、账号和已有索引均已保留。', flush=True)


def main():
    os.umask(0o077)
    lock = (ROOT / '.nas-ai-operation.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ValueError('已有升级或恢复操作在进行，请稍后重试')
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    upgrade = commands.add_parser('update')
    upgrade.add_argument('--version')
    upgrade.add_argument('--archive')
    upgrade.add_argument('--checksums')
    recovery = commands.add_parser('restore')
    recovery.add_argument('name')
    recovery.add_argument('--yes', action='store_true')
    commands.add_parser('rollback')
    mode = commands.add_parser('mode')
    mode.add_argument('mode', choices=['media', 'text'])
    network = commands.add_parser('download-proxy')
    network.add_argument('url')
    args = parser.parse_args()
    require_supported_architecture()
    if args.command == 'update':
        if args.archive and not (args.version and args.checksums):
            parser.error('本地升级包需要 --version 和 --checksums')
        update(args)
    elif args.command == 'restore':
        restore(args)
    elif args.command == 'mode':
        change_mode(args.mode)
    elif args.command == 'download-proxy':
        if not (ROOT / '.nas-ai-profile').is_file():
            raise ValueError('手工部署目录请使用原网络配置流程')
        set_config({'NAS_AI_DOWNLOAD_PROXY': validate_proxy(args.url)})
        print('下载网络设置已保存，正在继续启动', flush=True)
        run('start')
    else:
        choices = sorted((ROOT / '.nas-ai-releases').glob('*/state.json'))
        if not choices:
            raise ValueError('没有可回退的升级记录')
        restore_code(choices[-1].parent)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('操作未完成：' + str(error), file=sys.stderr)
        sys.exit(1)
