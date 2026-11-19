#!/usr/bin/env python3
"""One-shot native AMD comparison, independent of Intel AMX experiments."""
import argparse
import base64
import importlib.util
import json
import math
import platform
import re
import signal
import traceback
import urllib.error
from pathlib import Path

spec = importlib.util.spec_from_file_location('native', Path(__file__).with_name('test-container-runtime.py'))
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
IMAGES = {
    '0.30.8': 'ollama/ollama@sha256:05b6fe5143ed006d6d4abd39bdd575f962a5822bdf81e6fbb5e6894eb984ab9c',
    '0.32.3': 'ollama/ollama@sha256:ec24bcaa2a810eb74171ce7c517813ef4821ed678988845e8d76cf62467036d4',
}
MODELS = {
    'embedding': ('qwen3-embedding:0.6b', 'ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d'),
    'vision': ('qwen3-vl:2b-instruct-q4_K_M', 'afc393e3572c07db0bc84aa460d928a16c75f3fbb9ca9e0dccc4fbd2dafb573e'),
}


def cpu_info(text):
    result = {}
    for line in text.splitlines():
        k, sep, v = line.partition(':')
        if sep and k.strip() in ('vendor_id','model name','flags') and k.strip() not in result:
            result[k.strip()] = v.strip()[:4096]
    return result


def eligible(system, machine, cpu):
    return system == 'Linux' and machine in ('x86_64','amd64') and cpu.get('vendor_id') == 'AuthenticAMD'


def isolation(info, nodes):
    h = info['HostConfig']
    for key in ('Devices','DeviceRequests','DeviceCgroupRules','Binds','VolumesFrom','CapAdd'):
        if h.get(key) is not None and (not isinstance(h[key],list) or h[key]):
            raise ValueError('Unexpected device/privilege/mount configuration')
    if (h.get('Privileged') is not False or h.get('Runtime') != 'runc'
            or not isinstance(h.get('CapDrop'),list) or 'ALL' not in h['CapDrop']
            or h.get('IpcMode') != 'private'
            or any(h.get(k) not in ('','private') for k in ('PidMode','UsernsMode','UTSMode','CgroupnsMode'))
            or not isinstance(info.get('Mounts'),list) or any(m.get('Type')!='volume' for m in info['Mounts'])):
        raise ValueError('Pure CPU isolation not established')
    allowed = {'/dev/null','/dev/zero','/dev/full','/dev/random','/dev/urandom','/dev/tty','/dev/pts/ptmx'}
    names = nodes.splitlines()
    if not names or len(names)>64 or '/dev/null' not in names or any(n not in allowed and not re.fullmatch(r'/dev/pts/[0-9]+',n) for n in names):
        raise ValueError('Unexpected or missing container device nodes')
    return {'runtime':'runc','privileged':False,'device_nodes':sorted(names),'num_gpu':0}


def model_identity(rows, name, digest):
    if not isinstance(rows,list) or len(rows)!=1 or rows[0].get('name')!=name or rows[0].get('digest','').removeprefix('sha256:')!=digest:
        raise ValueError('Model identity differs from fixed comparison pin')
    return rows[0]


def safe_ps(response):
    rows=response.get('models') if isinstance(response,dict) else None
    if not isinstance(rows,list):return {'models_valid':False}
    safe=[]
    names={name for name,_ in MODELS.values()}
    for row in rows[:8]:
        if not isinstance(row,dict):safe.append({'valid':False});continue
        item={}
        for key in ('name','model'):
            if key in row:item[key]=row[key] if isinstance(row[key],str) and row[key] in names else '[unexpected]'
        if 'digest' in row:
            value=row['digest'];item['digest']=value if isinstance(value,str) and re.fullmatch(r'(?:sha256:)?[a-f0-9]{64}',value) else '[invalid]'
        for key in ('size','size_vram'):
            if key in row:item[key]=row[key] if type(row[key]) is int and 0<=row[key]<2**63 else '[invalid]'
        safe.append(item)
    return {'models':safe,'truncated':len(rows)>8}


def strict_cpu(response, name, digest):
    row = model_identity(response.get('models'),name,digest)
    if row.get('model')!=name or type(row.get('size_vram')) is not int or row['size_vram']!=0:
        raise ValueError('Expected exact selected model entirely on CPU')
    return {'name':name,'digest':digest,'size_vram':0}


def validate_result(kind, data):
    if kind == 'embedding':
        vectors = data.get('embeddings')
        if (not isinstance(vectors,list) or len(vectors)!=1 or not isinstance(vectors[0],list)
                or len(vectors[0])!=1024 or any(type(v) not in (int,float) or not math.isfinite(v) for v in vectors[0])
                or not math.isfinite(math.hypot(*vectors[0])) or math.hypot(*vectors[0])<=1e-9):
            raise ValueError('Expected one finite nonzero 1024D embedding')
        return {'dimensions':1024}
    if data.get('done') is not True or not isinstance(data.get('response'),str) or not data['response'].strip() or type(data.get('eval_count')) is not int or data['eval_count']<=0:
        raise ValueError('Vision generation not complete')
    return {'done':True,'eval_count':data['eval_count'],'response':data['response'][:4096],
            'claim':'generation completed; visual accuracy not evaluated'}


class Trial(native.Acceptance):
    def request(self, base, path, **kwargs):
        try:
            return super().request(base,path,**kwargs)
        except urllib.error.HTTPError as exc:
            try:
                body=exc.read(8193)
                failure={'path':path.split('?')[0],'status':exc.code,
                         'response_body':self.redact(body[:8192].decode('utf-8',errors='replace')),
                         'truncated':len(body)>8192}
                self.report.setdefault('http_errors',[]).append(failure)
                self.report['http_errors']=self.report['http_errors'][-8:]
                self.save()
            finally:exc.close()
            raise

    def boundary(self):
        info = json.loads(self.docker('inspect','--format','{{json .}}',self.containers['ollama']))
        return isolation(info,self.docker('exec',self.containers['ollama'],'find','/dev','-type','c'))

    def run(self):
        version,kind = self.args.version,self.args.kind
        model,digest = MODELS[kind]
        self.report.update(scope=['isolated native AMD CPU','synthetic '+kind],
                           not_covered=['NAS','production','GPU','performance','answer accuracy'],
                           version=version,model=model,model_digest=digest,image_pin=IMAGES[version],stage='setup')
        self.save()
        self.docker('pull',IMAGES[version],timeout=600)
        daemon=json.loads(self.docker('info','--format','{{json .}}'))
        image=self.image_info(IMAGES[version])
        self.arch=native.require_native(platform.system(),platform.machine(),daemon,[image])
        if self.arch!='amd64':raise ValueError('AMD comparison requires native amd64')
        self.record('loaded_image', {k:image[k] for k in ('Id','Os','Architecture','RepoDigests')})
        self.docker('network','create',self.network);self.network_created=True
        volume=self.prefix+'-model';self.docker('volume','create',volume);self.volumes.append(volume)
        self.start('ollama',IMAGES[version],['--publish','127.0.0.1::11434','--memory','6g','--cpus','2',
            '--env','OLLAMA_MAX_LOADED_MODELS=1','--env','OLLAMA_NUM_PARALLEL=1',
            '--mount',f'type=volume,src={volume},dst=/root/.ollama'])
        url=self.port('ollama',11434)
        self.wait('Ollama',lambda:self.request(url,'/api/tags'),90)
        if self.request(url,'/api/version').get('version')!=version:raise ValueError('Version mismatch')
        if native.architecture(self.docker('exec',self.containers['ollama'],'uname','-m'))!='amd64':raise ValueError('Emulated runtime')
        before=self.boundary();self.record('cpu_boundary_before',before)
        if self.request(url,'/api/pull',method='POST',payload={'name':model,'stream':False},timeout=1200).get('status')!='success':raise ValueError('Model pull failed')
        model_identity(self.request(url,'/api/tags').get('models'),model,digest)
        self.record('model_pin',{'name':model,'digest':digest})
        self.report['stage']='inference';self.save()
        payload={'model':model,'keep_alive':'1m','options':{'num_gpu':0}}
        if kind=='embedding':
            payload['input']=['synthetic red square on a white background'];path='/api/embed';timeout=300
        else:
            payload.update(prompt='Describe this image briefly.',images=[base64.b64encode(native.synthetic_png()).decode()],stream=False)
            payload['options'].update(num_predict=32,num_ctx=2048,temperature=0);path='/api/generate';timeout=600
        result=self.request(url,path,method='POST',payload=payload,timeout=timeout)
        self.record('inference',validate_result(kind,result))
        self.report['stage']='cpu_verification';self.save()
        sample=self.request(url,'/api/ps')
        self.record('cpu_sample',safe_ps(sample))
        self.record('cpu',strict_cpu(sample,model,digest))
        after=self.boundary()
        if before!=after:raise ValueError('Device boundary changed')
        self.record('cpu_boundary_after',after);self.running()
        self.report.update(status='passed',stage='complete')


def run_all(root, cpu, trial_class=Trial):
    report={'status':'running','host_cpu':cpu,'trials':[],'not_covered':['NAS','performance','production']}
    def save(): (root/'comparison.json').write_text(json.dumps(report,indent=2)+'\n')
    save()
    for version in IMAGES:
        for kind in MODELS:
            trial=trial_class(argparse.Namespace(version=version,kind=kind,evidence=str(root/version/kind)))
            try:trial.run()
            except Exception as exc:
                trial.report.update(status='failed',error=f'{type(exc).__name__}: {exc}')
                (trial.evidence/'failure.log').write_text(traceback.format_exc())
            finally:trial.cleanup()
            report['trials'].append({'version':version,'kind':kind,'status':trial.report['status'],
                                     'stage':trial.report.get('stage'),'cleanup_errors':trial.report.get('cleanup_errors',[])})
            if trial.report.get('cleanup_errors'):
                report['status']='inconclusive_cleanup';save();return 2
            save()
    report['status']='both_versions_passed_both_workloads' if all(t['status']=='passed' for t in report['trials']) else 'comparison_has_failures'
    save();return 0 if report['status']=='both_versions_passed_both_workloads' else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--evidence',default='ollama-amd-evidence');args=parser.parse_args()
    root=Path(args.evidence);root.mkdir(parents=True,exist_ok=True)
    cpu=cpu_info(Path('/proc/cpuinfo').read_text())
    if not eligible(platform.system(),platform.machine(),cpu):
        (root/'comparison.json').write_text(json.dumps({'status':'inconclusive_not_amd','host_cpu':cpu,'trials':[]},indent=2)+'\n')
        print('INCONCLUSIVE: native AMD host required; no retries');return 2
    def interrupted(signum, frame):raise KeyboardInterrupt('Interrupted')
    signal.signal(signal.SIGTERM,interrupted)
    return run_all(root,cpu)


if __name__=='__main__':raise SystemExit(main())
