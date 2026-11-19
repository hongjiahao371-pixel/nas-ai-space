"""Fail-closed, version-specific CPU evidence for the isolated Ollama A/B only."""
import re


def container_cpu_boundary(info, nodes):
    h = info['HostConfig']
    for field in ('Devices','DeviceRequests','DeviceCgroupRules','Binds','VolumesFrom','CapAdd'):
        if h.get(field) is not None and not isinstance(h[field],list):
            raise ValueError('Unexpected Docker isolation field type')
    if not isinstance(h.get('CapDrop'),list) or not isinstance(info.get('Mounts'),list):
        raise ValueError('Missing isolation lists')
    if (h.get('Privileged') is not False or h.get('Runtime') != 'runc'
            or h.get('Devices') or h.get('DeviceRequests') or h.get('DeviceCgroupRules')
            or h.get('Binds') or h.get('VolumesFrom') or h.get('CapAdd')
            or h.get('IpcMode') != 'private'
            or h.get('UsernsMode') not in ('', 'private')
            or h.get('UTSMode') not in ('', 'private')
            or h.get('CgroupnsMode') not in ('', 'private')
            or 'ALL' not in h.get('CapDrop', [])
            or h.get('PidMode') not in ('', 'private')
            or any(m.get('Type') != 'volume' for m in info.get('Mounts', []))):
        raise ValueError('Container GPU/device isolation not established')
    allowed = {'/dev/null','/dev/zero','/dev/full','/dev/random','/dev/urandom','/dev/tty','/dev/pts/ptmx'}
    rows = nodes.splitlines()
    if not rows or len(rows) > 64 or '/dev/null' not in rows:
        raise ValueError('Missing or unbounded device enumeration')
    if any(n not in allowed and not re.fullmatch(r'/dev/pts/[0-9]+',n) for n in rows):
        raise ValueError('Unexpected device exposed to container')
    return {'runtime':'runc','privileged':False,'device_requests':0,'mapped_devices':0,
            'device_nodes':sorted(rows),'mount_types':['volume']*len(info.get('Mounts', []))}


def amx_cpu_proof(version, model, digest, response, full_log, request_log, isolation, num_gpu):
    if version != '0.32.3' or not isolation or type(num_gpu) is not int or num_gpu != 0:
        raise ValueError('AMX exception not eligible')
    models = response.get('models')
    if not isinstance(models,list) or len(models) != 1:
        raise ValueError('Expected one isolated loaded model')
    row = models[0]
    if (row.get('name') != model or row.get('model') != model
            or row.get('digest','').removeprefix('sha256:') != digest
            or type(row.get('size_vram')) is not int or row['size_vram'] <= 0):
        raise ValueError('Model identity/VRAM mismatch')
    if len(full_log.encode()) > 2*1024*1024 or not request_log:
        raise ValueError('Missing or unbounded runner log')
    compute = [line for line in full_log.splitlines() if 'msg="inference compute"' in line]
    if not compute or any(not re.search(r'\bid=cpu\s+library=cpu\b',line) for line in compute):
        raise ValueError('Non-CPU or missing discovery evidence')
    starts = [line for line in request_log.splitlines() if 'msg="starting llama-server"' in line]
    if len(starts) != 1 or not re.search(r'\s-ngl 0(?:\s|\\?"|$)',starts[0]):
        raise ValueError('Expected exactly one fresh CPU-only runner')
    if re.search(r'offloaded\s+[1-9][0-9]*/[0-9]+\s+layers to GPU|using device (?!CPU\b)',request_log):
        raise ValueError('GPU offload evidence')
    # Mirror official v0.32.3 memoryParsingWriter key and last-value semantics.
    pattern = r'(?m)(?:^|\n)[^\n:]*?([A-Za-z_][A-Za-z0-9_]*):\s+(\S+)\s+(model|KV|compute|output|RS)\s+buffer size\s*=\s*([\d.]+)\s*MiB'
    # Docker --timestamps adds an ISO prefix containing colons; remove only it.
    log = re.sub(r'(?m)^\d{4}-\d\d-\d\dT\S+\s+', '',request_log)
    table = {}
    for component,backend,kind,mib in re.findall(pattern,log):
        if backend not in ('CPU','BLAS','AMX') and not backend.startswith('CPU_'):
            raise ValueError('Unknown/non-CPU buffer backend')
        if len(component)>80 or len(backend)>80:
            raise ValueError('Unbounded buffer label')
        amount = float(mib)
        if not 0 <= amount < 2**40:
            raise ValueError('Invalid buffer amount')
        table[component,backend,kind] = int(amount*1024*1024)
    # Unknown buffer-like lines must not be silently dropped by the parser.
    lines = [line for line in log.splitlines() if 'buffer size' in line]
    if len(re.findall(pattern,log)) != len(lines) or not table or len(table)>64:
        raise ValueError('Incomplete or unbounded buffer accounting')
    amx = sum(v for (_,backend,_),v in table.items() if backend=='AMX')
    if amx <= 0 or amx != row['size_vram'] or not any(k[1]=='CPU' for k in table):
        raise ValueError('AMX accounting does not exactly explain reported VRAM')
    return {'method':'amx_accounting_cpu_proof','version':version,'model':model,'digest':digest,
            'num_gpu':num_gpu,'size_vram_reported':row['size_vram'],'amx_bytes':amx,
            'buffers':[{'component':c,'backend':b,'kind':k,'bytes':v} for (c,b,k),v in sorted(table.items())],
            'isolation':isolation,'fresh_runner_count':1,'cpu_discovery':True}
