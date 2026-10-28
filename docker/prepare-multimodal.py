"""Download pinned public assets, verify every byte, then run the media model."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import tarfile
import tempfile
import time
import urllib.request


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def download(url, path, sha256, size=None):
    if path.is_file() and (size is None or path.stat().st_size == size) and digest(path) == sha256:
        print('已校验：' + path.name, flush=True)
        return
    partial = path.with_suffix(path.suffix + '.part')
    if partial.is_file() and (size is None or partial.stat().st_size == size) and digest(partial) == sha256:
        partial.replace(path)
        return
    if size is not None and partial.exists() and partial.stat().st_size >= size:
        partial.unlink()
    for attempt in range(4):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            print(f'正在连接模型下载源：{path.name}（第{attempt + 1}次，已保留{offset // 1048576} MB）', flush=True)
            request = urllib.request.Request(url, headers={'Range': f'bytes={offset}-'} if offset else {})
            with urllib.request.urlopen(request, timeout=60) as response:
                resumed = offset and response.status == 206
                if resumed and not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                    raise ValueError('下载续传位置不匹配')
                if not resumed:
                    offset = 0
                last = 0
                with partial.open('ab' if resumed else 'wb') as stream:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        stream.write(block)
                        offset += len(block)
                        if size and offset > size:
                            raise ValueError('下载文件超过预期大小')
                        if time.monotonic() - last > 5:
                            print(f'正在下载 {path.name}：{offset // 1048576} MB' + (f' / {size // 1048576} MB' if size else ''), flush=True)
                            last = time.monotonic()
            if size is not None and partial.stat().st_size < size:
                raise ConnectionError('连接提前结束，尚未下载完整，保留分段继续续传')
            if (size is not None and partial.stat().st_size != size) or digest(partial) != sha256:
                partial.unlink(missing_ok=True)
                raise ValueError('文件校验失败，重新下载')
            partial.replace(path)
            return
        except Exception:
            if attempt == 3:
                raise
            print('下载中断，保留进度并自动重试；持续失败时请检查网络或在维护向导设置下载代理。', flush=True)
            time.sleep(2 ** attempt)


def unpack_runtime(archive, target):
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        stage = Path(temporary)
        with tarfile.open(archive) as bundle:
            links = []
            for member in bundle.getmembers():
                if member.issym() and '.so' in Path(member.name).name:
                    if Path(member.linkname).name != member.linkname or member.linkname in {'.', '..'}:
                        raise ValueError('运行文件包含不安全链接')
                    links.append((Path(member.name).name, member.linkname))
                    continue
                if not member.isfile():
                    if member.isdir():
                        continue
                    raise ValueError('运行文件包含非普通文件')
                name = Path(member.name).name
                if name == 'llama-server' or '.so' in name:
                    destination = stage / name
                    if destination.exists():
                        raise ValueError('运行文件名称重复')
                    with bundle.extractfile(member) as source, destination.open('wb') as output:
                        shutil.copyfileobj(source, output)
                    destination.chmod(0o755)
            for name, link in links:
                if (stage / name).exists() or (stage / name).is_symlink():
                    raise ValueError('运行文件名称重复')
                (stage / name).symlink_to(link)
            if any(not (stage / name).is_file() for name, _ in links):
                raise ValueError('运行文件链接没有有效目标')
        if not (stage / 'llama-server').is_file():
            raise ValueError('缺少 llama-server')
        target.mkdir(exist_ok=True)
        for source in stage.iterdir():
            source.replace(target / source.name)


def runtime_architecture():
    machine = platform.machine().lower()
    aliases = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}
    if struct.calcsize('P') != 8 or machine not in aliases:
        raise ValueError(f'不支持处理器架构 {machine}，素材模型仅支持 64 位 x86-64 和 ARM64')
    return aliases[machine]


def select_runtime(manifest):
    architecture = runtime_architecture()
    runtime = manifest['runtimes'].get(architecture)
    if runtime is None:
        raise ValueError(f'没有适用于 {architecture} 的已校验运行文件')
    return architecture, runtime


def runtime_paths(root, architecture, runtime):
    # Include both architecture and the complete archive digest. Old x86 caches
    # and libraries remain untouched when a model volume moves to an ARM host.
    checksum = runtime['sha256']
    if architecture not in {'amd64', 'arm64'} or not re.fullmatch(r'[0-9a-f]{64}', checksum):
        raise ValueError('运行文件架构或校验值无效')
    name = f'runtime-{architecture}-{checksum}'
    return root / (name + '.tar.gz'), root / name


def verify_runtime(target, architecture):
    with (target / 'llama-server').open('rb') as binary:
        header = binary.read(20)
    expected_machine = {'amd64': 62, 'arm64': 183}[architecture]
    if (len(header) != 20 or header[:6] != b'\x7fELF\x02\x01'
            or int.from_bytes(header[18:20], 'little') != expected_machine):
        raise ValueError(f'运行文件不是 {architecture} 的 64 位 Linux 程序')


def prepare_runtime(root, architecture, runtime):
    archive, target = runtime_paths(root, architecture, runtime)
    download(runtime['url'], archive, runtime['sha256'], runtime.get('size'))
    unpack_runtime(archive, target)
    verify_runtime(target, architecture)
    return target


def query_command(root, runtime_dir=None):
    runtime_dir = runtime_dir if runtime_dir is not None else root / 'runtime'
    return [str(runtime_dir / 'llama-server'), '--model', str(root / 'embeddinggemma-2-Q8_0.gguf'),
        '--alias', 'embeddinggemma2', '--host', '0.0.0.0', '--port', '8080', '--embedding', '--pooling', 'mean',
        '--parallel', '1', '--ctx-size', '2048', '--batch-size', '2048', '--ubatch-size', '2048',
        '--threads', '2', '--threads-batch', '2', '--n-gpu-layers', '0', '--load-mode', 'mmap+mlock']


def execute(command, runtime_dir):
    # Replace the image's legacy /models/runtime path rather than appending it:
    # every dlopen() and linked library must use the selected architecture.
    environment = dict(os.environ, LD_LIBRARY_PATH=str(runtime_dir))
    os.execve(command[0], command, environment)


def main(root=Path('/models'), manifest_path=Path('/opt/manifest.json')):
    manifest = json.loads(manifest_path.read_text())
    architecture, runtime = select_runtime(manifest)
    _, runtime_dir = runtime_paths(root, architecture, runtime)
    if os.getenv('NAS_AI_MEDIA_MODE') == 'query':
        # Compose starts us after the full runtime has verified its shared assets.
        # Read only: never download or unpack into another running server's volume.
        name = 'embeddinggemma-2-Q8_0.gguf'
        spec = manifest['files'][name]
        model = root / name
        if model.stat().st_size != spec['size'] or digest(model) != spec['sha256']:
            raise ValueError('查询模型校验失败，请检查素材模型服务')
        verify_runtime(runtime_dir, architecture)
        command = query_command(root, runtime_dir)
        execute(command, runtime_dir)
        return
    root.mkdir(parents=True, exist_ok=True)
    for name, spec in manifest['files'].items():
        url = f"https://huggingface.co/{manifest['model_repo']}/resolve/{manifest['model_revision']}/{name}"
        download(url, root / name, spec['sha256'], spec['size'])
    runtime_dir = prepare_runtime(root, architecture, runtime)
    print('模型和运行文件均已校验，正在启动素材检索', flush=True)
    execute([str(runtime_dir / 'llama-server'),
        '--model', str(root / 'embeddinggemma-2-Q8_0.gguf'), '--mmproj', str(root / 'mmproj-embeddinggemma-2-Q8_0.gguf'),
        '--alias', 'embeddinggemma2', '--host', '0.0.0.0', '--port', '8080', '--embedding', '--pooling', 'mean',
        '--parallel', '2', '--ctx-size', '8192', '--batch-size', '2048', '--ubatch-size', '2048',
        '--threads', '2', '--threads-batch', '2', '--n-gpu-layers', '0', '--no-mmproj-offload'], runtime_dir)


if __name__ == '__main__':
    main()
