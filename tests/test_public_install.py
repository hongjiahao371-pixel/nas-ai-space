import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import subprocess
import sys
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
        # Old-release fixtures exercise upgrade mechanics on their supported architecture.
        architecture = patch.object(manager.platform, 'machine', return_value='x86_64')
        architecture.start()
        self.addCleanup(architecture.stop)

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

    def test_query_runtime_reuses_same_model_without_media_projector(self):
        command = runtime.query_command(Path('/models'))
        self.assertIn('/models/embeddinggemma-2-Q8_0.gguf', command)
        self.assertNotIn('--mmproj', command)
        self.assertEqual(command[command.index('--load-mode') + 1], 'mmap+mlock')
        self.assertEqual(command[command.index('--parallel') + 1], '1')
        self.assertEqual(command[command.index('--pooling') + 1], 'mean')

    def test_opted_in_memory_profile_survives_compose_command_selection(self):
        script = '''script=$1; set -- help; source "$script" >/dev/null
env_value() { case "$1" in NAS_AI_SEARCH_MODE) echo media;; NAS_AI_SEARCH_MEMORY_PROFILE) echo true;; esac; }
profile_files intel
'''
        result = subprocess.check_output(['bash','-c',script,'test',str(REPO/'scripts/nas-ai')], text=True)
        self.assertEqual(result.splitlines(), ['docker-compose.yml','compose.intel.yml','compose.media.yml','compose.search-memory.yml','compose.search-media-memory.yml'])

    def test_text_memory_profile_does_not_require_a_media_service(self):
        script = '''script=$1; set -- help; source "$script" >/dev/null
env_value() { case "$1" in NAS_AI_SEARCH_MODE) echo text;; NAS_AI_SEARCH_MEMORY_PROFILE) echo true;; esac; }
profile_files cpu
'''
        result = subprocess.check_output(['bash','-c',script,'test',str(REPO/'scripts/nas-ai')], text=True)
        self.assertEqual(result.splitlines(), ['docker-compose.yml','compose.search-memory.yml'])

    def test_valid_archive_preserves_executable(self):
        archive, checks = self.bundle()
        names = manager.unpack_release(archive, self.root / 'stage', 'v1.7.0', checks)
        self.assertEqual(len(names), 4)
        self.assertEqual((self.root / 'stage/scripts/nas-ai').stat().st_mode & 0o777, 0o755)

    def test_intel_corporation_is_not_mistaken_for_ati(self):
        script = '''script=$1; set -- help; source "$script" >/dev/null
uname() { printf 'x86_64\\n'; }
has() { [[ "$1" == lspci ]]; }
function [ () { if [[ "$1" == '-d' && "$2" == '/dev/dri' ]]; then return 0; fi; builtin [ "$@"; }
lspci() { printf '%s\\n' '00:02.0 VGA compatible controller: Intel Corporation Raptor Lake-P [UHD Graphics]'; }
detect_profile
lspci() { printf '%s\\n' '03:00.0 VGA compatible controller: Advanced Micro Devices, Inc. [AMD/ATI] Navi'; }
detect_profile
'''
        result = subprocess.check_output(['bash', '-c', script, 'test', str(REPO / 'scripts/nas-ai')], text=True)
        self.assertEqual(result.splitlines(), ['intel', 'amd-vulkan'])

    def test_download_proxy_validation(self):
        self.assertEqual(manager.validate_proxy('http://192.0.2.1:8080/'), 'http://192.0.2.1:8080')
        self.assertEqual(manager.validate_proxy(''), '')
        for value in ['file:///etc/passwd', 'socks5://localhost:1080', 'http://u:p@localhost:8080', 'http://localhost', 'http://localhost:8/a', 'http://localhost:8\n']:
            with self.subTest(value=value), self.assertRaises(ValueError): manager.validate_proxy(value)

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

    def test_shell_config_reader_preserves_special_characters(self):
        (self.root / '.env').write_text('')
        value = '/media/资料 $cash "name"'
        with patch.object(manager, 'ROOT', self.root):
            manager.set_config({'NAS_LIBRARY_PATH': value})
        script = 'script=$1; set -- help; source "$script" >/dev/null; env_value NAS_LIBRARY_PATH'
        result = subprocess.check_output(['bash', '-c', script, 'test', str(REPO / 'scripts/nas-ai')],
                                         env={**__import__('os').environ, 'NAS_AI_PROJECT_DIR': str(self.root)}, text=True)
        self.assertEqual(result.rstrip('\n'), value)

    def test_offline_start_requires_every_selected_cached_image(self):
        script = r'''script=$1; set -- help; source "$script" >/dev/null
uname() { printf 'x86_64\n'; }
compose() { printf '%s\n' '{"services":{"app":{"image":"app:v1"},"multimodal":{"image":"media:v1"}}}'; }
fake_docker() { [[ "$3" != "media:v1" || "$available_media" == yes ]] || return 1; printf 'linux/amd64\n'; }
DOCKER_CMD=(fake_docker)
available_media=no
cached_images_available app && printf 'text-ok\n'
if cached_images_available app multimodal; then printf 'bad\n'; else printf 'missing-media\n'; fi
available_media=yes
cached_images_available app multimodal && printf 'media-ok\n'
'''
        result = subprocess.check_output(['bash', '-c', script, 'test', str(REPO / 'scripts/nas-ai')], text=True)
        self.assertEqual(result.splitlines(), ['text-ok', 'missing-media', 'media-ok'])

    def test_arm64_selects_cpu_even_when_gpu_tools_are_present(self):
        script = r'''script=$1; fake_architecture=$2; set -- help; source "$script" >/dev/null
uname() { printf '%s\n' "$fake_architecture"; }
has() { return 0; }
nvidia-smi() { printf 'GPU 0\n'; }
detect_profile
'''
        for architecture in ('aarch64', 'arm64'):
            with self.subTest(architecture=architecture):
                result = subprocess.check_output(['bash', '-c', script, 'test', str(REPO / 'scripts/nas-ai'), architecture], text=True)
                self.assertEqual(result.strip(), 'cpu')

    def test_arm64_setup_uses_cpu_and_rejects_acceleration_before_writing(self):
        script = r'''script=$1; requested=$2; set -- help; source "$script" >/dev/null
uname() { printf 'aarch64\n'; }
total_memory_mb() { printf '8192\n'; }
setup_command --non-interactive --profile "$requested"
'''
        for profile in ('intel', 'amd-vulkan', 'amd-rocm', 'nvidia', 'intel-openvino', 'cpu'):
            root = self.root / profile; root.mkdir()
            result = subprocess.run(['bash', '-c', script, 'test', str(REPO / 'scripts/nas-ai'), profile],
                                    env={**__import__('os').environ, 'NAS_AI_PROJECT_DIR': str(root)},
                                    capture_output=True, text=True)
            with self.subTest(profile=profile):
                if profile == 'cpu':
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual((root / '.nas-ai-profile').read_text().strip(), 'cpu')
                    self.assertIn('NAS_AI_SEARCH_MODE=text', (root / '.env').read_text())
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('ARM64 当前使用 CPU', result.stderr)
                    self.assertEqual(list(root.iterdir()), [])

    def test_32bit_arm_setup_is_rejected_before_creating_files(self):
        script = r'''script=$1; set -- help; source "$script" >/dev/null
uname() { printf 'armv7l\n'; }
setup_command --non-interactive --profile cpu
'''
        result = subprocess.run(['bash', '-c', script, 'test', str(REPO / 'scripts/nas-ai')],
                                env={**__import__('os').environ, 'NAS_AI_PROJECT_DIR': str(self.root)},
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('32位 ARM', result.stderr)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_arm64_kernel_with_32bit_userspace_is_rejected(self):
        script = r'''script=$1; set -- help; source "$script" >/dev/null
uname() { printf 'aarch64\n'; }
getconf() { printf '32\n'; }
setup_command --non-interactive --profile cpu
'''
        result = subprocess.run(['bash', '-c', script, 'test', str(REPO / 'scripts/nas-ai')],
                                env={**__import__('os').environ, 'NAS_AI_PROJECT_DIR': str(self.root)},
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('32位系统', result.stderr)
        self.assertEqual(list(self.root.iterdir()), [])
        source = (REPO / 'install.sh').read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        with patch.object(manager.platform, 'machine', return_value='aarch64'), \
                patch.object(manager.struct, 'calcsize', return_value=4), \
                patch.object(sys, 'argv', ['install', '--version', 'v1.7.3']), \
                patch.object(manager.urllib.request, 'urlopen') as download:
            with self.assertRaisesRegex(ValueError, '32位系统'):
                manager.require_supported_architecture('v1.7.3')
            with self.assertRaisesRegex(SystemExit, '32位系统'):
                exec(compile(source, 'install.sh', 'exec'), {})
            download.assert_not_called()

    def test_arm64_compose_rejects_saved_accelerated_profile_before_docker(self):
        script = r'''script=$1; set -- help; source "$script" >/dev/null
uname() { printf 'arm64\n'; }
read_profile() { printf 'intel\n'; }
ensure_docker_access() { printf 'unexpected-docker-call\n'; }
compose up -d
'''
        result = subprocess.run(['bash', '-c', script, 'test', str(REPO / 'scripts/nas-ai')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ARM64 当前使用 CPU', result.stderr)
        self.assertNotIn('unexpected-docker-call', result.stdout)

    def test_arm64_offline_start_rejects_cached_amd64_images(self):
        script = r'''script=$1; set -- help; source "$script" >/dev/null
uname() { printf 'aarch64\n'; }
compose() { printf '%s\n' '{"services":{"app":{"image":"app:v1"}}}'; }
fake_docker() { printf 'linux/%s\n' "$cached_architecture"; }
DOCKER_CMD=(fake_docker)
cached_architecture=amd64
if cached_images_available app; then printf 'bad\n'; else printf 'wrong-architecture\n'; fi
cached_architecture=arm64
cached_images_available app && printf 'arm64-ok\n'
'''
        result = subprocess.check_output(['bash', '-c', script, 'test', str(REPO / 'scripts/nas-ai')], text=True)
        self.assertEqual(result.splitlines(), ['wrong-architecture', 'arm64-ok'])

    def test_start_does_not_launch_successfully_pulled_wrong_architecture(self):
        script = r'''script=$1; set -- help; source "$script" >/dev/null
uname() { printf 'aarch64\n'; }
doctor_command() { :; }
env_value() { printf '\n'; }
compose() {
  case "$1" in
    pull) return 0;;
    config) printf '%s\n' '{"services":{"app":{"image":"old-amd64:v1"}}}';;
    *) printf 'unexpected-compose-start\n';;
  esac
}
fake_docker() { printf 'linux/amd64\n'; }
DOCKER_CMD=(fake_docker)
start_command
'''
        result = subprocess.run(['bash', '-c', script, 'test', str(REPO / 'scripts/nas-ai')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('镜像与本机处理器不匹配', result.stderr)
        self.assertNotIn('unexpected-compose-start', result.stdout)

    def test_install_entry_rejects_32bit_arm_and_old_arm64_releases_before_download(self):
        source = (REPO / 'install.sh').read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        target = self.root / 'new-install'
        for architecture, version, message in [('armv7l', 'v1.7.3', '32位 ARM'), ('arm64', 'v1.7.2', 'v1.7.3'), ('aarch64', 'v1.6.5', 'v1.7.3')]:
            with self.subTest(architecture=architecture, version=version), \
                    patch.object(manager.platform, 'machine', return_value=architecture), \
                    patch.object(sys, 'argv', ['install', '--version', version, '--directory', str(target)]), \
                    patch.object(manager.urllib.request, 'urlopen') as download:
                with self.assertRaisesRegex(SystemExit, message):
                    exec(compile(source, 'install.sh', 'exec'), {})
                download.assert_not_called()
                self.assertFalse(target.exists())

    def test_arm64_release_guard_allows_new_versions_and_rejects_old(self):
        for architecture in ('aarch64', 'arm64'):
            with patch.object(manager.platform, 'machine', return_value=architecture):
                for version in ('v1.7.3', '1.7.3', 'v1.7.10', 'v2.0.0'):
                    self.assertEqual(manager.require_supported_architecture(version), 'arm64')
                for version in ('v1.7.2', '1.7.0', 'v1.6.5'):
                    with self.assertRaisesRegex(ValueError, 'v1.7.3'):
                        manager.require_supported_architecture(version)
        with patch.object(manager.platform, 'machine', return_value='armv7l'):
            with self.assertRaisesRegex(ValueError, '32位 ARM'):
                manager.require_supported_architecture()

    def test_arm64_old_release_update_and_rollback_do_not_change_installation(self):
        (self.root / 'app.py').write_text('unchanged')
        snapshot = self.root / 'snapshot'; snapshot.mkdir()
        (snapshot / 'state.json').write_text(json.dumps({'old_version': '1.7.2'}))
        with patch.object(manager.platform, 'machine', return_value='aarch64'), \
                patch.object(manager, 'ROOT', self.root), patch.object(manager, 'run') as run, \
                patch.object(manager, 'request') as request:
            with self.assertRaisesRegex(ValueError, 'v1.7.3'):
                manager.update(argparse.Namespace(version='v1.7.2', archive=None, checksums=None))
            with self.assertRaisesRegex(ValueError, 'v1.7.3'):
                manager.restore_code(snapshot)
            run.assert_not_called()
            request.assert_not_called()
        self.assertEqual((self.root / 'app.py').read_text(), 'unchanged')

    def test_arm64_upgrade_requires_a_compatible_rollback_version(self):
        (self.root / '.nas-ai-profile').write_text('cpu\n')
        with patch.object(manager.platform, 'machine', return_value='aarch64'), \
                patch.object(manager, 'ROOT', self.root), patch.object(manager, 'run') as run, \
                patch.object(manager, 'public_json') as release, \
                patch.object(manager, 'request', return_value={'version': '1.7.2'}) as request:
            with self.assertRaisesRegex(ValueError, '新目录安装'):
                manager.update(argparse.Namespace(version='v1.7.3', archive=None, checksums=None))
            run.assert_not_called()
            release.assert_not_called()
            request.assert_called_once_with('/api/health')
        self.assertFalse((self.root / '.nas-ai-releases').exists())

    def test_only_legacy_default_vision_models_are_migrated(self):
        old = {'NAS_AI_VISION_MODEL':'qwen3-vl:2b','NAS_AI_CHAT_MODEL':'custom-private-model'}
        self.assertEqual(manager.vision_default_migration(old), {'NAS_AI_VISION_MODEL':'qwen3-vl:2b-instruct-q4_K_M'})
        self.assertEqual(old['NAS_AI_VISION_MODEL'], 'qwen3-vl:2b')
        self.assertEqual(manager.vision_default_migration({'NAS_AI_VISION_MODEL':'other'}), {})

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

    def test_short_eof_keeps_partial_and_retries_from_last_byte(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        data = b'model-data' * 100; ranges = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                offset = 137 if self.headers.get('Range') else 0
                ranges.append(self.headers.get('Range'))
                self.send_response(206 if offset else 200)
                if offset: self.send_header('Content-Range', f'bytes {offset}-{len(data)-1}/{len(data)}')
                self.end_headers(); self.wfile.write(data[offset:] if offset else data[:137])
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with patch.object(runtime.time, 'sleep'):
                runtime.download(f'http://127.0.0.1:{server.server_port}/model', self.root / 'model',
                                 hashlib.sha256(data).hexdigest(), len(data))
            self.assertEqual(ranges, [None, 'bytes=137-'])
            self.assertEqual((self.root / 'model').read_bytes(), data)
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
