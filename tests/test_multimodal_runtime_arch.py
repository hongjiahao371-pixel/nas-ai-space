"""Exercise real download/extraction paths without downloading model weights."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('multimodal_runtime_arch', REPO / 'docker/prepare-multimodal.py')
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def elf(machine):
    header = bytearray(64)
    header[:6] = b'\x7fELF\x02\x01'
    header[18:20] = machine.to_bytes(2, 'little')
    return bytes(header)


def archive(machine):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as bundle:
        for name, content in [('llama-server', elf(machine)), ('libggml.so.1', elf(machine))]:
            member = tarfile.TarInfo('llama-b11480/' + name)
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))
        member = tarfile.TarInfo('llama-b11480/libggml.so')
        member.type = tarfile.SYMTYPE
        member.linkname = 'libggml.so.1'
        bundle.addfile(member)
    return output.getvalue()


class Response(io.BytesIO):
    status = 200
    headers = {}


class RuntimeArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'models'
        self.root.mkdir()
        self.assets = {}
        self.manifest = {'model_repo': 'fixture/model', 'model_revision': 'fixed', 'files': {}, 'runtimes': {}}
        for name in ('embeddinggemma-2-Q8_0.gguf', 'mmproj-embeddinggemma-2-Q8_0.gguf'):
            content = name.encode()
            self.manifest['files'][name] = {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
            self.assets['https://huggingface.co/fixture/model/resolve/fixed/' + name] = content
        for architecture, machine in [('amd64', 62), ('arm64', 183)]:
            content = archive(machine)
            url = 'https://runtime.invalid/' + architecture + '.tar.gz'
            self.assets[url] = content
            self.manifest['runtimes'][architecture] = {
                'tag': 'b11480', 'url': url, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest(),
            }
        self.manifest_path = Path(self.temporary.name) / 'manifest.json'
        self.save_manifest()

    def save_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest))

    def urlopen(self, request, **kwargs):
        return Response(self.assets[request.full_url])

    def boot(self, machine, query=False):
        with patch.object(runtime.platform, 'machine', return_value=machine), \
                patch.object(runtime.struct, 'calcsize', return_value=8), \
                patch.dict(runtime.os.environ, {
                    'NAS_AI_MEDIA_MODE': 'query' if query else '', 'LD_LIBRARY_PATH': '/models/runtime',
                }), patch.object(runtime.os, 'execve') as execute, \
                patch.object(runtime.urllib.request, 'urlopen', side_effect=self.urlopen) as download:
            runtime.main(self.root, self.manifest_path)
        return execute.call_args.args, download

    def test_known_64_bit_architecture_aliases_select_the_correct_package(self):
        for machine, expected in [('x86_64', 'amd64'), ('AMD64', 'amd64'), ('aarch64', 'arm64'), ('arm64', 'arm64')]:
            with self.subTest(machine=machine), patch.object(runtime.platform, 'machine', return_value=machine), \
                    patch.object(runtime.struct, 'calcsize', return_value=8):
                architecture, selected = runtime.select_runtime(self.manifest)
                self.assertEqual(architecture, expected)
                self.assertEqual(selected['url'], 'https://runtime.invalid/' + expected + '.tar.gz')

    def test_32_bit_and_unknown_architectures_fail_before_downloading(self):
        for machine, width in [('armv7l', 4), ('armv6l', 4), ('i686', 4), ('aarch64', 4), ('riscv64', 8)]:
            with self.subTest(machine=machine, width=width), \
                    patch.object(runtime.platform, 'machine', return_value=machine), \
                    patch.object(runtime.struct, 'calcsize', return_value=width), \
                    patch.object(runtime, 'download') as download, patch.object(runtime.os, 'execve') as execute:
                with self.assertRaisesRegex(ValueError, '仅支持 64 位'):
                    runtime.main(self.root, self.manifest_path)
                download.assert_not_called()
                execute.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_architecture_is_rejected_without_x86_fallback(self):
        del self.manifest['runtimes']['arm64']
        with patch.object(runtime.platform, 'machine', return_value='aarch64'), \
                patch.object(runtime.struct, 'calcsize', return_value=8):
            with self.assertRaisesRegex(ValueError, '没有适用于 arm64'):
                runtime.select_runtime(self.manifest)

    def test_moving_model_volume_to_arm_preserves_x86_and_legacy_caches(self):
        legacy = self.root / 'runtime'
        legacy.mkdir()
        (legacy / 'llama-server').write_bytes(elf(62))
        (legacy / 'libggml-cpu-x64.so').write_bytes(elf(62))
        (self.root / 'runtime.tar.gz').write_bytes(b'legacy archive')
        amd, _ = self.boot('x86_64')
        original = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        arm, downloaded = self.boot('aarch64')
        self.assertNotEqual(amd[0], arm[0])
        self.assertIn('runtime-amd64-', amd[0])
        self.assertIn('runtime-arm64-', arm[0])
        self.assertEqual(arm[2]['LD_LIBRARY_PATH'], str(Path(arm[0]).parent))
        self.assertEqual(downloaded.call_count, 1)  # Model weights are reused unchanged.
        for path, content in original.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        self.assertEqual((Path(arm[0]).parent / 'libggml.so').read_bytes(), elf(183))
        self.assertFalse((Path(arm[0]).parent / 'libggml-cpu-x64.so').exists())

    def test_query_reuses_ingest_runtime_and_writes_nothing_for_each_architecture(self):
        for machine in ('x86_64', 'aarch64'):
            with self.subTest(machine=machine):
                ingest, _ = self.boot(machine)
                before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                          for path in self.root.rglob('*') if path.is_file()}
                query, download = self.boot(machine, query=True)
                after = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                         for path in self.root.rglob('*') if path.is_file()}
                self.assertEqual(query[0], ingest[0])
                self.assertEqual(query[2]['LD_LIBRARY_PATH'], ingest[2]['LD_LIBRARY_PATH'])
                self.assertNotIn('--mmproj', query[1])
                self.assertIn('--mmproj', ingest[1])
                self.assertIn('mmap+mlock', query[1])
                download.assert_not_called()
                self.assertEqual(before, after)

    def test_query_cannot_execute_an_x86_binary_from_an_arm_directory(self):
        ingest, _ = self.boot('aarch64')
        Path(ingest[0]).write_bytes(elf(62))
        with self.assertRaisesRegex(ValueError, '不是 arm64'):
            self.boot('aarch64', query=True)

    def test_archive_digest_failure_never_extracts_or_executes(self):
        selected = self.manifest['runtimes']['arm64']
        self.assets[selected['url']] = b'!' * selected['size']
        with patch.object(runtime.platform, 'machine', return_value='aarch64'), \
                patch.object(runtime.struct, 'calcsize', return_value=8), \
                patch.object(runtime.urllib.request, 'urlopen', side_effect=self.urlopen), \
                patch.object(runtime.time, 'sleep'), patch.object(runtime, 'unpack_runtime') as unpack, \
                patch.object(runtime.os, 'execve') as execute:
            with self.assertRaisesRegex(ValueError, '校验失败'):
                runtime.prepare_runtime(self.root, 'arm64', self.manifest['runtimes']['arm64'])
            unpack.assert_not_called()
            execute.assert_not_called()

    def test_new_archive_digest_has_a_different_cache_directory(self):
        selected = self.manifest['runtimes']['arm64']
        old_archive, old_directory = runtime.runtime_paths(self.root, 'arm64', selected)
        new_archive, new_directory = runtime.runtime_paths(self.root, 'arm64', {**selected, 'sha256': 'a' * 64})
        self.assertNotEqual(old_archive, new_archive)
        self.assertNotEqual(old_directory, new_directory)


if __name__ == '__main__':
    unittest.main()
