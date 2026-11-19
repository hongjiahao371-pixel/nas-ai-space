"""Pure, bounded FCPXML rough-cut export; callers must authorize every source.

Only explicit editor-machine paths are serialized. No file/network access here.
FCPXML structure checks do not establish import compatibility with an NLE.
"""
from __future__ import annotations

from fractions import Fraction
import io
import json
import math
import re
from urllib.parse import quote
import xml.etree.ElementTree as ET
import zipfile

RATES = {str(x): Fraction(x) for x in (24, 25, 30, 50, 60)} | {
    x: Fraction(x) for x in ('24000/1001', '30000/1001', '60000/1001')
}


def _text(value: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError('文字或路径为空或过长')
    if any(not (c in '\t\n\r' or '\x20' <= c <= '\ud7ff' or '\ue000' <= c <= '\ufffd' or '\U00010000' <= c <= '\U0010ffff') for c in value):
        raise ValueError('包含 XML 不支持的字符')
    return value


def editor_file_uri(path: str) -> str:
    """Accept local absolute paths, not URLs, UNC, traversal or guessed NAS roots."""
    path = _text(path)
    if path != path.strip() or any(c in path for c in '\r\n\t'):
        raise ValueError('剪辑机路径不可带前后空白或控制字符')
    if re.match(r'^[A-Za-z]:[\\/]', path):
        normalized = path.replace('\\', '/')
        prefix = 'file:///'
    elif path.startswith('/') and not path.startswith('//'):
        normalized = path
        prefix = 'file://'
    else:
        raise ValueError('请填写剪辑机本地绝对路径，不支持网址或网络 UNC 路径')
    if any(part in {'.', '..'} for part in normalized.split('/')) or normalized.endswith('/'):
        raise ValueError('剪辑机路径必须是具体文件且不能包含相对路径段')
    return prefix + quote(normalized, safe='/:')


def _seconds(value) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError('入出点与时长必须是有限秒数')
    if value < 0 or value > 31 * 86400:
        raise ValueError('入出点或时长超出范围')
    return Fraction(str(value))


def _time(value: Fraction) -> str:
    return f'{value.numerator}s' if value.denominator == 1 else f'{value.numerator}/{value.denominator}s'


def build_edit_handoff(project_name: str, clips: list[dict], frame_rate: str = '25') -> bytes:
    """clips: authorized snapshots + explicit in/out/path. Never accepts path fallback.

    Required snapshot keys: version_id, file_id, name, duration, width, height,
    frame_rate. audio_codec is optional; unverified audio is not advertised.
    Requested in is rounded UP and out DOWN to keep every cut inside the range.
    """
    project_name = _text(project_name, 255)
    if frame_rate not in RATES:
        raise ValueError('不支持的时间线帧率')
    if not isinstance(clips, list) or not 1 <= len(clips) <= 200:
        raise ValueError('每次请选择 1 至 200 个片段')
    rate = RATES[frame_rate]
    root = ET.Element('fcpxml', version='1.10')
    resources = ET.SubElement(root, 'resources')
    formats, assets = {}, {}
    normalized, warnings = [], []
    offset = Fraction(0)
    for index, clip in enumerate(clips, 1):
        for key in ('version_id', 'file_id', 'width', 'height'):
            if type(clip.get(key)) is not int or clip[key] <= 0:
                raise ValueError(f'片段 {index} 缺少有效版本或画面尺寸')
        if max(clip['width'], clip['height']) > 16384:
            raise ValueError('画面尺寸超出支持范围')
        source_rate = clip.get('frame_rate')
        if isinstance(source_rate, bool) or not isinstance(source_rate, (int, float)) or not math.isfinite(source_rate) or abs(source_rate - float(rate)) > 0.00001:
            raise ValueError(f'片段 {index} 帧率未知或与时间线不同，首版不支持混合帧率')
        start, end, total = (_seconds(clip.get(key)) for key in ('start_time', 'end_time', 'duration'))
        if not start < end <= total:
            raise ValueError(f'片段 {index} 入出点超出源素材或顺序错误')
        first, last = math.ceil(start * rate), math.floor(end * rate)
        if last <= first:
            raise ValueError(f'片段 {index} 不足一个完整帧')
        actual_start, duration = Fraction(first, 1) / rate, Fraction(last - first, 1) / rate
        uri = editor_file_uri(clip.get('media_path'))
        name = _text(clip.get('name'), 1024)
        dimensions = (clip['width'], clip['height'])
        if dimensions not in formats:
            fid = f'f{len(formats)+1}'
            formats[dimensions] = fid
            ET.SubElement(resources, 'format', id=fid, frameDuration=_time(1/rate), width=str(dimensions[0]), height=str(dimensions[1]))
        format_id = formats[dimensions]
        version = clip['version_id']
        signature = (clip['file_id'], uri, total, dimensions, bool(clip.get('audio_codec')))
        if version in assets and assets[version][1] != signature:
            raise ValueError('同一素材版本不能指向不同媒体或元数据')
        if version not in assets:
            aid = f'a{len(assets)+1}'
            assets[version] = (aid, signature)
            attrs = dict(id=aid, name=name, start='0s', duration=_time(Fraction(math.floor(total*rate),1)/rate), hasVideo='1', format=format_id)
            if clip.get('audio_codec'):
                attrs['hasAudio'] = '1'
            asset = ET.SubElement(resources, 'asset', **attrs)
            ET.SubElement(asset, 'media-rep', kind='original-media', src=uri)
        if actual_start != start or actual_start+duration != end:
            warnings.append(f'片段 {index} 已向区间内部对齐完整帧；请核对 manifest 中实际入出点。')
        normalized.append(dict(order=index, version_id=version, file_id=clip['file_id'], name=name,
            media_uri=uri, requested_start_seconds=float(start), requested_end_seconds=float(end),
            start=_time(actual_start), end=_time(actual_start+duration), offset=_time(offset),
            duration=_time(duration), resource=assets[version][0], format=format_id))
        offset += duration
    library = ET.SubElement(root, 'library')
    event = ET.SubElement(library, 'event', name=project_name)
    project = ET.SubElement(event, 'project', name=f'{project_name} 粗剪交接')
    sequence = ET.SubElement(project, 'sequence', format=normalized[0]['format'], duration=_time(offset), tcStart='0s', tcFormat='NDF')
    spine = ET.SubElement(sequence, 'spine')
    for clip in normalized:
        ET.SubElement(spine, 'asset-clip', name=clip['name'], ref=clip['resource'], offset=clip['offset'], start=clip['start'], duration=clip['duration'], format=clip['format'])
    manifest = dict(schema_version=1, export_format='fcpxml-1.10', timeline_frame_rate=frame_rate,
        timeline_duration=_time(offset), clips=normalized, warnings=warnings,
        limitations=['仅顺序粗剪，入出点为源文件零起点秒数，不保留嵌入时间码。',
                     '剪辑机路径由用户显式提供，未验证挂载或文件是否可在剪辑机读取。',
                     '不含媒体文件、字幕、特效、调色、审阅意见或代理；不支持混合帧率。',
                     'Apple 自身导出的 1.10+ 使用 fcpxmld 包；本文件为独立 fcpxml，目标版本兼容性待实机确认。',
                     '未进行 Final Cut Pro、Premiere Pro 或 DaVinci Resolve 实机导入验收。'])
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as package:
        package.writestr('timeline.fcpxml', ET.tostring(root, encoding='utf-8', xml_declaration=True))
        package.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        package.writestr('README.txt', '先将媒体挂载到 manifest 中的剪辑机路径，再尝试导入 timeline.fcpxml。\n'+ '\n'.join(manifest['limitations']))
    return output.getvalue()
