"""Download pinned public assets, verify every byte, then run the media model."""
import hashlib
import json
import os
from pathlib import Path
import shutil
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


def main():
    root = Path('/models')
    root.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(Path('/opt/manifest.json').read_text())
    for name, spec in manifest['files'].items():
        url = f"https://huggingface.co/{manifest['model_repo']}/resolve/{manifest['model_revision']}/{name}"
        download(url, root / name, spec['sha256'], spec['size'])
    runtime = manifest['runtime']
    archive = root / 'runtime.tar.gz'
    download(runtime['url'], archive, runtime['sha256'])
    unpack_runtime(archive, root / 'runtime')
    print('模型和运行文件均已校验，正在启动素材检索', flush=True)
    os.execv(str(root / 'runtime/llama-server'), [str(root / 'runtime/llama-server'),
        '--model', str(root / 'embeddinggemma-2-Q8_0.gguf'), '--mmproj', str(root / 'mmproj-embeddinggemma-2-Q8_0.gguf'),
        '--alias', 'embeddinggemma2', '--host', '0.0.0.0', '--port', '8080', '--embedding', '--pooling', 'mean',
        '--parallel', '2', '--ctx-size', '8192', '--batch-size', '2048', '--ubatch-size', '2048',
        '--threads', '2', '--threads-batch', '2', '--n-gpu-layers', '0', '--no-mmproj-offload'])


if __name__ == '__main__':
    main()
