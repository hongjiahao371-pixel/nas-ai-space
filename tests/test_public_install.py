import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from tests import test_improvements as fixtures

REPO = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, REPO / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manager = load('test_manage', 'scripts/manage.py')
wizard_module = load('test_wizard', 'scripts/setup-web.py')
runtime = load('test_runtime', 'docker/prepare-multimodal.py')


class ReleaseInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def bundle(self, extra=None):
        archive = self.root / 'release.tar.gz'
        files = {'scripts/nas-ai': b'#!/bin/bash\n', 'scripts/manage.py': b'# manage\n',
                 'docker-compose.yml': b'services: {}', 'app/main.py': b'version="1.7.0"'}
        files.update(extra or {})
        with tarfile.open(archive, 'w:gz') as tar:
            for name, body in files.items():
                member = tarfile.TarInfo('nas-ai-space-v1.7.0/' + name)
                member.size = len(body)
                member.mode = 0o755 if name == 'scripts/nas-ai' else 0o644
                tar.addfile(member, io.BytesIO(body))
        checks = self.root / 'SHA256SUMS'
        checks.write_text(manager.sha(archive) + '  nas-ai-space-v1.7.0.tar.gz\n')
        return archive, checks

    def test_valid_archive_preserves_executable(self):
        archive, checks = self.bundle()
        names = manager.unpack_release(archive, self.root / 'stage', 'v1.7.0', checks)
        self.assertEqual(len(names), 4)
        self.assertEqual((self.root / 'stage/scripts/nas-ai').stat().st_mode & 0o777, 0o755)

    def test_hash_mismatch_does_not_extract(self):
        archive, checks = self.bundle()
        archive.write_bytes(archive.read_bytes() + b'changed')
        with self.assertRaises(ValueError):
            manager.unpack_release(archive, self.root / 'stage', 'v1.7.0', checks)
        self.assertFalse((self.root / 'stage').exists())

    def test_archive_rejects_traversal_and_persistent_paths(self):
        for name in ['../escape', '/absolute', '.env', 'data/database', 'uploads/photo', '.nas-ai-restores/old']:
            with self.subTest(name=name):
                archive, checks = self.bundle({name: b'bad'})
                with self.assertRaises(ValueError):
                    manager.unpack_release(archive, self.root / 'stage', 'v1.7.0', checks)
        self.assertFalse((self.root / 'escape').exists())

    def test_config_roundtrip_paths_dollar_and_quotes(self):
        (self.root / '.env').write_text('NAS_AI_API_TOKEN=secret\n')
        with patch.object(manager, 'ROOT', self.root):
            manager.set_config({'NAS_LIBRARY_PATH': '/media/a $cash "name"', 'NAS_AI_PORT': '12345'})
            value = manager.configuration()
            self.assertEqual(value['NAS_LIBRARY_PATH'], '/media/a $cash "name"')
            self.assertEqual(value['NAS_AI_API_TOKEN'], 'secret')
            self.assertEqual((self.root / '.env').stat().st_mode & 0o777, 0o600)

    def test_copy_refuses_destination_symlink(self):
        source = self.root / 'source'; destination = self.root / 'destination'; outside = self.root / 'outside'
        source.mkdir(); destination.mkdir(); outside.mkdir()
        (source / 'app').mkdir(); (source / 'app/code.py').write_text('new')
        (destination / 'app').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            manager.copy_code(source, destination, ['app/code.py'])
        self.assertFalse((outside / 'code.py').exists())

    def test_failed_update_restores_old_source_and_preserves_private_data(self):
        archive, checks = self.bundle()
        installed = self.root / 'installed'; installed.mkdir()
        (installed / 'app').mkdir(); (installed / 'app/main.py').write_text('old')
        (installed / '.env').write_text('NAS_AI_API_TOKEN=never-copy\n')
        (installed / '.nas-ai-profile').write_text('cpu\n')
        (installed / 'data').mkdir(); (installed / 'data/private').write_text('keep')
        args = argparse.Namespace(archive=str(archive), checksums=str(checks), version='v1.7.0')
        with patch.object(manager, 'ROOT', installed), patch.object(manager, 'request', return_value={'version': '1.6.5'}), \
                patch.object(manager, 'run'), patch.object(manager, 'checked_start', side_effect=[RuntimeError('failed'), None]):
            with self.assertRaises(RuntimeError):
                manager.update(args)
        self.assertEqual((installed / 'app/main.py').read_text(), 'old')
        self.assertEqual((installed / 'data/private').read_text(), 'keep')
        self.assertEqual((installed / '.env').read_text(), 'NAS_AI_API_TOKEN=never-copy\n')
        self.assertFalse(list((installed / '.nas-ai-releases').glob('*/source/.env')))

    def test_verified_model_cache_avoids_network(self):
        path = self.root / 'model'; path.write_bytes(b'model')
        with patch.object(runtime.urllib.request, 'urlopen') as request:
            runtime.download('https://example.invalid/model', path, manager.sha(path), 5)
        request.assert_not_called()

    def test_interrupted_download_resumes_and_verifies_final_bytes(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        data = b'public-model' * 100
        partial = self.root / 'model.part'; partial.write_bytes(data[:137])
        ranges = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                ranges.append(self.headers.get('Range'))
                self.send_response(206)
                self.send_header('Content-Range', f'bytes 137-{len(data)-1}/{len(data)}')
                self.end_headers(); self.wfile.write(data[137:])
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            runtime.download(f'http://127.0.0.1:{server.server_port}/model', self.root / 'model',
                             hashlib.sha256(data).hexdigest(), len(data))
            self.assertEqual(ranges, ['bytes=137-'])
            self.assertEqual((self.root / 'model').read_bytes(), data)
            self.assertFalse(partial.exists())
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_failed_recovery_reconnects_original_mounts_and_keeps_old_data(self):
        data = self.root / 'data'; (data / 'backups/recovery').mkdir(parents=True)
        (data / 'backups/recovery/recovery-test.tar').write_bytes(b'verified-by-service')
        (data / 'private').write_text('original')
        (self.root / '.env').write_text('NAS_AI_SEARCH_MODE=media\nCOMPOSE_PROJECT_NAME=isolated-test\n')
        (self.root / '.nas-ai-profile').write_text('cpu\n')
        original = (self.root / '.env').read_bytes(); calls = []
        def run(*args, **kwargs):
            calls.append(args)
            if 'scripts/restore-recovery.py' in args: raise RuntimeError('injected restore failure')
            return argparse.Namespace(stdout='healthy')
        with patch.object(manager, 'ROOT', self.root), patch.object(manager, 'request'), patch.object(manager, 'run', run):
            with self.assertRaises(RuntimeError): manager.restore(argparse.Namespace(name='recovery-test.tar', yes=True))
        self.assertEqual((self.root / '.env').read_bytes(), original)
        self.assertEqual((data / 'private').read_text(), 'original')
        self.assertEqual(calls[-1], ('start',))

    def test_failed_mode_switch_preserves_config_and_restarts_previous_stack(self):
        (self.root / '.env').write_text('NAS_AI_SEARCH_MODE=text\n')
        (self.root / '.nas-ai-profile').write_text('cpu\n')
        with patch.object(manager, 'ROOT', self.root), patch.object(manager, 'request', return_value={}), \
                patch.object(manager, 'checked_start', side_effect=RuntimeError('model unavailable')), patch.object(manager, 'run') as run:
            with self.assertRaises(RuntimeError): manager.change_mode('media')
        self.assertEqual((self.root / '.env').read_text(), 'NAS_AI_SEARCH_MODE=text\n')
        run.assert_called_once_with('start')

    def test_runtime_safe_library_symlinks_and_no_escape(self):
        archive = self.root / 'runtime.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            for name in ['runtime/llama-server', 'runtime/libggml.so.1']:
                m = tarfile.TarInfo(name); m.size = 4; bundle.addfile(m, io.BytesIO(b'test'))
            m = tarfile.TarInfo('runtime/libggml.so'); m.type = tarfile.SYMTYPE; m.linkname = 'libggml.so.1'; bundle.addfile(m)
        runtime.unpack_runtime(archive, self.root / 'runtime')
        self.assertEqual((self.root / 'runtime/libggml.so').read_bytes(), b'test')


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.library = self.root / 'photos'; self.library.mkdir()
        self.wizard = wizard_module.Wizard([self.library])

    def tearDown(self):
        self.tmp.cleanup()

    def test_folder_browser_hides_files_hidden_dirs_and_external_symlinks(self):
        (self.library / '旅行').mkdir(); (self.library / '.private').mkdir()
        (self.library / 'file').write_text('private')
        (self.library / 'escape').symlink_to(self.root)
        result = self.wizard.folders(str(self.library))
        self.assertEqual([d['name'] for d in result['folders']], ['旅行'])
        with self.assertRaises(ValueError):
            self.wizard.folder(str(self.root))

    def test_http_requires_token_and_rejects_cross_origin(self):
        server = wizard_module.ThreadingHTTPServer(('127.0.0.1', 0), wizard_module.handler_for(self.wizard))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            for headers in [{}, {'X-Setup-Token': 'bad'}, {'X-Setup-Token': self.wizard.token, 'Origin': 'https://foreign.invalid'}]:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(base + '/folders', headers=headers))
                self.assertEqual(error.exception.code, 403)
            with urllib.request.urlopen(urllib.request.Request(base + '/folders', headers={'X-Setup-Token': self.wizard.token})) as response:
                self.assertEqual(json.load(response)['folders'][0]['name'], 'photos')
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_setup_wont_overwrite_existing_config_or_accept_unknown_action(self):
        with patch.object(wizard_module.manage, 'configuration', return_value={'NAS_AI_PORT': '8766'}):
            with self.assertRaises(ValueError):self.wizard.start({'action': 'install'})
            with self.assertRaises(ValueError):self.wizard.start({'action': 'shell', 'command': 'bad'})
            with self.assertRaises(ValueError):self.wizard.start([])


class FolderAPITests(unittest.TestCase):
    setUp = fixtures.ApiImprovementsTests.setUp
    tearDown = fixtures.ApiImprovementsTests.tearDown

    def test_browse_only_admin_and_authorized_roots(self):
        (self.library / '旅行').mkdir()
        (self.library / '.hidden').mkdir()
        (self.library / 'escape').symlink_to(self.root)
        route = '/api/libraries/folders'
        self.assertEqual(self.client.get(route).status_code, 401)
        result = self.client.get(route, headers=self.headers, params={'path': str(self.library)})
        self.assertEqual(result.status_code, 200)
        self.assertEqual([v['name'] for v in result.json()['folders']], ['旅行'])
        self.assertEqual(self.client.get(route, headers=self.headers, params={'path': str(self.root)}).status_code, 400)
        self.assertEqual(self.client.get(route, headers=self.headers, params={'path': str(self.library / 'escape')}).status_code, 400)

    def test_folder_pagination_and_bad_offset(self):
        for i in range(101):(self.library / f'folder-{i:03}').mkdir()
        route = '/api/libraries/folders'
        first = self.client.get(route, headers=self.headers, params={'path': str(self.library)}).json()
        last = self.client.get(route, headers=self.headers, params={'path': str(self.library), 'offset': first['next_offset']}).json()
        self.assertEqual(len(first['folders']), 100)
        self.assertEqual(len(last['folders']), 1)
        self.assertIsNone(last['next_offset'])
        self.assertEqual(self.client.get(route, headers=self.headers, params={'offset': -1}).status_code, 400)

    def test_member_cannot_browse_mounted_folders(self):
        from app.security import hash_password
        self.main.state.database.create_user('member', 'Member', hash_password('TestPassword123'), 'member', [])
        login = self.client.post('/api/auth/login', json={'username': 'member', 'password': 'TestPassword123'})
        self.assertEqual(login.status_code, 200)
        headers = {'Authorization': 'Bearer ' + login.json()['token']}
        self.assertEqual(self.client.get('/api/libraries/folders', headers=headers).status_code, 403)
