#!/usr/bin/env python3
"""Temporary, token-protected LAN installation and maintenance wizard."""
import argparse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('nas_manage', ROOT / 'scripts/manage.py')
manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)


class Wizard:
    def __init__(self, roots):
        self.token = secrets.token_urlsafe(32)
        self.roots = [p.resolve() for p in roots if p.is_dir()]
        self.lock = threading.Lock()
        self.log = deque(maxlen=200)
        self.busy = False
        self.result = ''
        self.updated = time.monotonic()

    def folder(self, value):
        path = Path(value).expanduser().resolve()
        if not path.is_dir() or not any(path == r or r in path.parents for r in self.roots):
            raise ValueError('请选择可用资料目录中的文件夹')
        if path == ROOT or ROOT in path.parents:
            raise ValueError('程序目录不能作为资料目录')
        if any(part.startswith('.') for part in path.parts):
            raise ValueError('不显示隐藏目录')
        return path

    def folders(self, value):
        if not value:
            return {'path': '', 'parent': None, 'folders': [{'name': p.name or str(p), 'path': str(p)} for p in self.roots]}
        path = self.folder(value)
        folders = []
        for child in path.iterdir():
            if child.name.startswith('.') or child.is_symlink():
                continue
            try:
                resolved = self.folder(str(child))
                folders.append({'name': child.name, 'path': str(resolved)})
            except (ValueError, OSError):
                continue
        if len(folders) > 1000:
            raise ValueError('子目录超过1000个，请在路径栏填写更具体的资料目录')
        return {'path': str(path), 'parent': '' if path in self.roots else str(path.parent),
                'folders': sorted(folders, key=lambda v: v['name'].casefold())}

    def status(self):
        config = manage.configuration()
        data = ROOT / config.get('NAS_AI_DATA_PATH', 'data')
        backups = sorted((data / 'backups/recovery').glob('recovery-*.tar'), reverse=True)
        if config.get('NAS_AI_SEARCH_MODE') != 'media':
            backups += sorted((data / 'backups').glob('nas-ai-space-*.db'), reverse=True)
        return {'configured': (ROOT / '.env').is_file(), 'busy': self.busy, 'result': self.result,
                'logs': list(self.log), 'port': config.get('NAS_AI_PORT', '8766'),
                'mode': config.get('NAS_AI_SEARCH_MODE', 'text'), 'backups': [p.name for p in backups[:20]]}

    def start(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('请求格式无效')
        action = payload.get('action')
        config = manage.configuration()
        if action == 'install':
            if config:
                raise ValueError('已经有安装配置，请使用继续启动；原配置不会被覆盖')
            library = self.folder(str(payload.get('library', '')))
            if library in ROOT.parents:
                raise ValueError('资料目录不能包含程序安装目录')
            mode = payload.get('mode', 'media')
            if mode not in {'media', 'text'}:
                raise ValueError('请选择检索方案')
            port = int(payload.get('port', 8766))
            if not 1024 <= port <= 65535:
                raise ValueError('请选择1024至65535之间的端口')
            with socket.socket() as probe:
                probe.bind(('0.0.0.0', port))
            commands = [['setup', '--non-interactive', '--library', str(library), '--search', mode, '--port', str(port)], ['start']]
            proxy = manage.validate_proxy(str(payload.get('proxy', '')))
            if proxy:
                commands[0] += ['--download-proxy', proxy]
        elif action in {'start', 'update', 'backup', 'rollback'} and config:
            commands = [[action]]
        elif action == 'mode' and config and payload.get('mode') in {'text', 'media'}:
            commands = [['mode', payload['mode']]]
        elif action == 'network' and config:
            commands = [['download-proxy', manage.validate_proxy(str(payload.get('proxy', '')))]]
        elif action == 'restore' and config:
            name = str(payload.get('name', ''))
            if name not in self.status()['backups'] or payload.get('confirm') != '恢复':
                raise ValueError('请选择已有备份，并输入“恢复”确认')
            commands = [['restore', name, '--yes']]
        else:
            raise ValueError('当前操作不可用')
        with self.lock:
            if self.busy:
                raise ValueError('上一项操作还在运行')
            self.busy, self.result = True, ''
            self.log.clear()
        threading.Thread(target=self.execute, args=(commands,), daemon=True).start()

    def execute(self, commands):
        try:
            for command in commands:
                with subprocess.Popen(['bash', str(ROOT / 'scripts/nas-ai'), *command], cwd=ROOT,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, errors='replace') as process:
                    for line in process.stdout:
                        token = manage.configuration().get('NAS_AI_API_TOKEN', '')
                        clean = line.replace(token, '[已隐藏]') if token else line
                        with self.lock:
                            self.log.append(clean.rstrip()[-2000:])
                    if process.wait() != 0:
                        raise RuntimeError('操作未完成，请查看下方原因；修复后可以重试')
            self.result = '操作完成'
        except Exception as error:
            self.result = str(error)
        finally:
            self.busy = False


def handler_for(wizard):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log tokens or filesystem selections.

        def respond(self, status, value, content_type='application/json'):
            body = value.encode() if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                return False
            return secrets.compare_digest(self.headers.get('X-Setup-Token', ''), wizard.token)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == '/':
                return self.respond(200, (ROOT / 'scripts/setup.html').read_text(), 'text/html')
            if not self.authorized():
                return self.respond(403, {'error': '请使用终端显示的完整安装链接'})
            wizard.updated = time.monotonic()
            try:
                if parsed.path == '/status':
                    return self.respond(200, wizard.status())
                if parsed.path == '/folders':
                    return self.respond(200, wizard.folders(parse_qs(parsed.query).get('path', [''])[0]))
                self.respond(404, {'error': '未找到'})
            except (ValueError, OSError) as error:
                self.respond(400, {'error': str(error)})

        def do_POST(self):
            if not self.authorized():
                return self.respond(403, {'error': '安装链接失效，请重新打开终端提供的链接'})
            if self.path != '/action':
                return self.respond(404, {'error': '未找到'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192:
                    raise ValueError('请求大小无效')
                wizard.start(json.loads(self.rfile.read(length)))
                self.respond(202, {'started': True})
            except (ValueError, OSError, TypeError) as error:
                self.respond(400, {'error': str(error)})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18765)
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--browse-root', action='append', type=Path)
    args = parser.parse_args()
    if (ROOT / '.env').exists() and not (ROOT / '.nas-ai-profile').is_file():
        raise SystemExit('当前目录是手工部署实例。请使用原部署流程；新安装请选择空目录。')
    # Authenticate Docker once in the terminal. The browser never collects a NAS password.
    subprocess.run(['bash', str(ROOT / 'scripts/nas-ai'), 'compose-exec', 'version'], check=True)
    roots = args.browse_root or [*Path('/').glob('volume*'), Path('/mnt'), Path('/media'), Path('/srv'), Path.home()]
    wizard = Wizard(roots)
    server = ThreadingHTTPServer((args.bind, args.port), handler_for(wizard))
    server.timeout = 1
    address = '127.0.0.1'
    if args.bind != '127.0.0.1':
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(('1.1.1.1', 80))
                address = probe.getsockname()[0]
        except OSError:
            address = socket.gethostname()
    print(f'请在浏览器打开：http://{address}:{args.port}/#{wizard.token}', flush=True)
    print('仅在可信局域网使用。保留此终端；Ctrl+C退出。空闲1小时后自动关闭。', flush=True)
    started = time.monotonic()
    try:
        while wizard.busy or (time.monotonic() - wizard.updated < 3600 and time.monotonic() - started < 14400):
            server.handle_request()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
