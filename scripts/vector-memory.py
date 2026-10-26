#!/usr/bin/env python3
"""Explicit, reversible RAM profile for small existing Qdrant collections.

Run only after a verified recovery backup. No vectors or payloads are changed.
Large libraries stay on the default disk profile unless an operator opts in.
"""
import argparse
import hashlib
import json
import struct
import time
from pathlib import Path

import httpx


def ram_patch():
    return {"vectors": {"": {"on_disk": False}},
            "hnsw_config": {"on_disk": False}, "params": {"on_disk_payload": False}}


def rollback_patch(info):
    config = info['config']
    return {"vectors": {"": {"on_disk": config['params']['vectors'].get('on_disk', False)}},
            "hnsw_config": {"on_disk": config['hnsw_config'].get('on_disk', False)},
            "params": {"on_disk_payload": config['params'].get('on_disk_payload', False)}}


def fingerprint(client, collection):
    digest = hashlib.sha256(); count = 0; offset = None
    while True:
        body = {'limit': 512, 'with_payload': True, 'with_vector': True}
        if offset is not None: body['offset'] = offset
        response = client.post(f'/collections/{collection}/points/scroll', json=body)
        response.raise_for_status(); result = response.json()['result']
        for point in result['points']:
            vector = point['vector']
            if not isinstance(vector, list): raise ValueError('Only unnamed dense vectors are supported')
            data = json.dumps([point['id'], point.get('payload', {})], sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
            digest.update(struct.pack('<I', len(data))); digest.update(data)
            digest.update(struct.pack('<I', len(vector))); digest.update(struct.pack('<%sf' % len(vector), *vector))
            count += 1
        offset = result.get('next_page_offset')
        if offset is None: break
        if not result['points']: raise RuntimeError('Unexpected empty scroll page')
    return {'points': count, 'sha256': digest.hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://qdrant:6333')
    parser.add_argument('--collection', action='append', required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--backup-manifest', type=Path)
    parser.add_argument('--max-vector-mib', type=int, default=256)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    report = {'started_at': time.time(), 'applied': False, 'collections': {}}
    if args.report.exists(): raise ValueError('Use a new report path; previous evidence is never overwritten')
    def save():
        tmp = args.report.with_suffix('.tmp')
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n'); tmp.replace(args.report)
    backup = json.loads(args.backup_manifest.read_text()) if args.backup_manifest else {}
    if args.apply and not backup.get('verified_at'): raise ValueError('A verified recovery manifest is required')
    with httpx.Client(base_url=args.url.rstrip('/'), timeout=120, trust_env=False) as client:
        total = 0
        for collection in args.collection:
            response = client.get('/collections/'+collection); response.raise_for_status(); info = response.json()['result']
            vectors = info['config']['params']['vectors']
            if not isinstance(vectors.get('size'), int): raise ValueError('Only unnamed dense vectors are supported')
            if info['status'] != 'green' or info['optimizer_status'] != 'ok': raise RuntimeError('Wait for existing optimization to finish')
            total += info['points_count'] * vectors['size'] * 4
            if args.apply:
                expected = backup.get('collections', {}).get(collection, {})
                if expected.get('points') != info['points_count'] or expected.get('vectors', {}).get('size') != vectors['size']:
                    raise ValueError('Recovery manifest does not match this collection')
            report['collections'][collection] = {'before': info, 'rollback_patch': rollback_patch(info), 'fingerprint_before': fingerprint(client, collection)}
            save()
        report['raw_vector_bytes'] = total
        if total > args.max_vector_mib * 1024**2: raise ValueError('RAM profile exceeds the explicit vector budget')
        if not args.apply: save(); return
        try:
            for collection, item in report['collections'].items():
                response = client.patch('/collections/'+collection, json=ram_patch()); response.raise_for_status()
                item['patch_accepted'] = True; save()
                time.sleep(3)
                deadline = time.monotonic()+600; ready = 0
                while time.monotonic() < deadline:
                    response = client.get('/collections/'+collection); response.raise_for_status(); info = response.json()['result']
                    ready = ready+1 if info['status']=='green' and info['optimizer_status']=='ok' else 0
                    if ready >= 3: break
                    time.sleep(2)
                else: raise TimeoutError('Optimization did not finish')
                item['after'] = info; item['fingerprint_after'] = fingerprint(client, collection)
                if item['fingerprint_before'] != item['fingerprint_after']: raise RuntimeError('Point content changed during profile migration')
                params = info['config']['params']
                assert params['vectors']['on_disk'] is False and params['on_disk_payload'] is False
                assert info['config']['hnsw_config']['on_disk'] is False
                save()
            report['applied'] = True
        finally:
            report['finished_at'] = time.time(); save()
    print(json.dumps({'applied': report['applied'], 'raw_vector_bytes': total, 'report': str(args.report)}))


if __name__ == '__main__': main()
