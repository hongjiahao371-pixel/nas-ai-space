#!/usr/bin/env python3
"""Isolated same-host A/B; never changes production defaults or weakens gates."""
import argparse
import importlib.util
import json
import platform
import re
import signal
import traceback
from pathlib import Path

spec = importlib.util.spec_from_file_location('ollama_acceptance', Path(__file__).with_name('test-ollama-runtime.py'))
original = importlib.util.module_from_spec(spec)
spec.loader.exec_module(original)
MODELS = ('qwen3-embedding:0.6b', 'qwen3-vl:2b-instruct-q4_K_M')
VERSIONS = ('0.30.8', '0.32.3')


def eligible(system, machine, cpu):
    return (system == 'Linux' and machine in ('x86_64', 'amd64')
            and cpu.get('vendor_id') == 'GenuineIntel'
            and {'amx_tile', 'amx_int8'} <= set(cpu.get('flags', '').split()))


def identities(models):
    result = {}
    for name in MODELS:
        matches = [m for m in models if m.get('name') == name]
        if len(matches) != 1 or not re.fullmatch(r'(?:sha256:)?[0-9a-f]{64}', matches[0].get('digest', '')):
            raise ValueError('Missing or ambiguous immutable model identity: ' + name)
        result[name] = matches[0]['digest'].removeprefix('sha256:')
    return result


def known_embedding_crash(errors):
    return any(e.get('method') == 'POST' and e.get('path') == '/api/embed'
               and e.get('model') == MODELS[0] and e.get('status') == 500
               and re.search(r'\bSIGSEGV\b|segmentation fault', e.get('response_body', ''), re.I)
               for e in errors)


def conclusion(phases):
    if len(phases) != 2 or not all(p.get('model_ids') for p in phases):
        return 'inconclusive_setup'
    if phases[0]['model_ids'] != phases[1]['model_ids']:
        return 'inconclusive_model_drift'
    if phases[0]['status'] == 'passed':
        return 'inconclusive_baseline_not_reproduced'
    if not phases[0].get('inference_failed'):
        return 'inconclusive_setup'
    if not phases[0].get('known_embedding_crash'):
        return 'inconclusive_different_baseline_failure'
    return 'candidate_passes_baseline_inference_fails' if phases[1]['status'] == 'passed' else 'candidate_failed'


class LockedAcceptance(original.OllamaAcceptance):
    def __init__(self, args, lock, version):
        super().__init__(args)
        self.lock, self.version = lock, version
        self.model_ids = None
        self.inference_started = False
        self.inference_failed = False

    def record(self, name, value):
        if name == 'downloaded_models':
            self.model_ids = identities(value)
            if self.lock and self.model_ids != self.lock:
                raise ValueError('Model digests changed between A/B; refusing inference')
        return super().record(name, value)

    def request(self, base, path, **kwargs):
        if path in ('/api/embed', '/api/generate'):
            if not self.model_ids:
                raise ValueError('Inference without model identity lock')
            self.inference_started = True
        try:
            return super().request(base, path, **kwargs)
        except Exception:
            if path in ('/api/embed', '/api/generate'):
                self.inference_failed = True
            raise

    def wait(self, name, check, timeout):
        result = super().wait(name, check, timeout)
        # Verify actual runtime, not only a mutable Docker tag.
        if name == 'Ollama server':
            version = self.request(self.port('ollama', 11434), '/api/version').get('version')
            if version != self.version:
                raise ValueError('Runtime version differs from requested version: '+str(version))
            self.record('server_version', version)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', default='ollama-ab-evidence')
    args = parser.parse_args()
    root = Path(args.evidence); root.mkdir(parents=True, exist_ok=True)
    cpu = original.host_cpu_summary(Path('/proc/cpuinfo').read_text())
    summary = {'status': 'inconclusive_ineligible_host', 'host_cpu': cpu, 'phases': [],
               'limitations': ['Not a NAS test, performance claim or proof of AMX root cause.',
                              'Image/model digests are resolved and locked within this run, not across runs.']}
    def save():
        (root/'comparison.json').write_text(json.dumps(summary, indent=2)+'\n')
    save()
    if not eligible(platform.system(), platform.machine(), cpu):
        print('INCONCLUSIVE: native Intel AMX host required; no inference attempted')
        return 2
    def interrupted(signum, frame):
        raise KeyboardInterrupt('A/B interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    lock = None
    for version in VERSIONS:
        trial = LockedAcceptance(argparse.Namespace(image='ollama/ollama:'+version,
                                  evidence=str(root/version)), lock, version)
        try:
            trial.docker('pull', trial.args.image, timeout=600)
            info = trial.image_info(trial.args.image)
            digests = [d for d in info.get('RepoDigests', []) if re.fullmatch(r'ollama/ollama@sha256:[0-9a-f]{64}', d)]
            if len(digests) != 1:
                raise ValueError('Official image digest not uniquely resolved')
            trial.args.image = digests[0]
            trial.record('pinned_image', {'version': version, 'reference': digests[0]})
            trial.run()
        except Exception as exc:
            trial.report.update(status='failed', error=f'{type(exc).__name__}: {exc}')
            (trial.evidence/'failure.log').write_text(traceback.format_exc())
        finally:
            trial.cleanup()
        summary['phases'].append({'version': version, 'status': trial.report['status'],
                                  'model_ids': trial.model_ids, 'inference_started': trial.inference_started,
                                  'inference_failed': trial.inference_failed,
                                  'known_embedding_crash': bool(known_embedding_crash(trial.report.get('http_errors', []))),
                                  'http_errors': trial.report.get('http_errors', [])})
        if version == VERSIONS[0]:
            lock = trial.model_ids
        save()
        if not lock:
            summary['status'] = 'inconclusive_setup'; save(); return 2
    summary['status'] = conclusion(summary['phases']); save()
    print(summary['status'])
    return 0 if summary['status'] == 'candidate_passes_baseline_inference_fails' else 2


if __name__ == '__main__':
    raise SystemExit(main())
