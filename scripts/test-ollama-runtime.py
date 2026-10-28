#!/usr/bin/env python3
"""Run the public CPU stack's default Qwen models on a native Linux runner.

Only synthetic input is sent to a disposable local Ollama container. This tests
model startup/inference, not answer quality or a particular NAS's performance.
"""
import argparse
import base64
import importlib.util
import json
import math
import platform
import signal
import time
import traceback
from pathlib import Path

spec = importlib.util.spec_from_file_location('native_runtime', Path(__file__).with_name('test-container-runtime.py'))
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class OllamaAcceptance(runtime.Acceptance):
    def run(self):
        self.report['scope'] = ['native Linux Ollama CPU', 'default Qwen text embedding', 'default Qwen vision generation']
        self.report['not_covered'] = ['physical NAS hardware', 'GPU/NPU', 'speech', 'model quality', 'NAS load/latency']
        daemon = json.loads(self.docker('info', '--format', '{{json .}}'))
        self.docker('pull', self.args.image, timeout=600)
        image = self.image_info(self.args.image)
        self.arch = runtime.require_native(platform.system(), platform.machine(), daemon, [image])
        self.record('native_platform', {'host': platform.machine(), 'architecture': self.arch,
                                      'image': {k: image[k] for k in ('Id', 'Os', 'Architecture')}})
        self.docker('network', 'create', self.network)
        self.network_created = True
        volume = self.prefix + '-ollama'
        self.docker('volume', 'create', volume)
        self.volumes.append(volume)
        self.start('ollama', self.args.image, ['--publish', '127.0.0.1::11434', '--memory', '6g',
                   '--cpus', '2', '--env', 'OLLAMA_MAX_LOADED_MODELS=1', '--env', 'OLLAMA_NUM_PARALLEL=1',
                   '--mount', f'type=volume,src={volume},dst=/root/.ollama'])
        url = self.port('ollama', 11434)
        self.wait('Ollama server', lambda: self.request(url, '/api/tags'), 90)
        machine = self.docker('exec', self.containers['ollama'], 'uname', '-m')
        if runtime.architecture(machine) != self.arch:
            raise ValueError('Ollama container architecture does not match native runner')
        for model in ('qwen3-embedding:0.6b', 'qwen3-vl:2b-instruct-q4_K_M'):
            print('Downloading public default model: ' + model, flush=True)
            result = self.request(url, '/api/pull', method='POST', payload={'name': model, 'stream': False}, timeout=1200)
            if result.get('status') != 'success':
                raise ValueError('Default model pull failed: ' + str(result))
        models = self.request(url, '/api/tags')['models']
        self.record('downloaded_models', [{k: m.get(k) for k in ('name', 'digest', 'size')} for m in models])
        started = time.monotonic()
        embedded = self.request(url, '/api/embed', method='POST', payload={
            'model': 'qwen3-embedding:0.6b', 'input': ['synthetic red square on a white background'],
            'keep_alive': '1m'}, timeout=300)
        vectors = embedded.get('embeddings', [])
        if len(vectors) != 1 or len(vectors[0]) != 1024 or not all(math.isfinite(v) for v in vectors[0]):
            raise ValueError('Default Qwen embedding did not produce one finite 1024-dimensional vector')
        if sum(v*v for v in vectors[0]) <= 1e-9:
            raise ValueError('Default Qwen embedding returned a zero vector')
        self.record('qwen_embedding', {'dimensions': 1024, 'elapsed_seconds': round(time.monotonic()-started, 3)})
        self.check_cpu(url, 'qwen3-embedding:0.6b')
        started = time.monotonic()
        generated = self.request(url, '/api/generate', method='POST', payload={
            'model': 'qwen3-vl:2b-instruct-q4_K_M', 'prompt': 'Describe this image briefly.',
            'images': [base64.b64encode(runtime.synthetic_png()).decode()], 'stream': False,
            'options': {'num_predict': 32, 'num_ctx': 2048, 'temperature': 0}, 'keep_alive': '1m'}, timeout=600)
        if generated.get('done') is not True or not generated.get('response', '').strip() or generated.get('eval_count', 0) <= 0:
            raise ValueError('Default Qwen vision generation did not produce a completed response')
        self.record('qwen_vision', {'done': True, 'eval_count': generated['eval_count'],
                                   'response': generated['response'], 'elapsed_seconds': round(time.monotonic()-started, 3)})
        self.check_cpu(url, 'qwen3-vl:2b-instruct-q4_K_M')
        self.running()
        self.report['status'] = 'passed'

    def check_cpu(self, url, model):
        loaded = self.request(url, '/api/ps')['models']
        matches = [m for m in loaded if m.get('name') == model or m.get('model') == model]
        if len(matches) != 1 or matches[0].get('size_vram') != 0:
            raise ValueError('Expected the selected model to be loaded entirely on CPU')
        self.record('cpu_' + model.split(':')[0], {k: matches[0].get(k) for k in ('name', 'size', 'size_vram')})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='ollama/ollama:0.30.8')
    parser.add_argument('--evidence', default='ollama-runtime-evidence')
    args = parser.parse_args()
    acceptance = OllamaAcceptance(args)
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'Interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        acceptance.run()
    except BaseException as exc:
        acceptance.report['status'] = 'failed'
        acceptance.report['error'] = f'{type(exc).__name__}: {exc}'
        (acceptance.evidence / 'failure.log').write_text(traceback.format_exc())
        print(acceptance.report['error'], flush=True)
    finally:
        acceptance.cleanup()
    print('Native Ollama acceptance: ' + acceptance.report['status'], flush=True)
    return 0 if acceptance.report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
