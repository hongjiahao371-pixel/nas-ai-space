import io
import json
import math
import unittest
import zipfile
from fractions import Fraction
from xml.etree import ElementTree as ET
from app.services.edit_handoff import build_edit_handoff, editor_file_uri


def clip(**kwargs):
    return dict(version_id=1, file_id=2, name='猫 & 水 <01>.mp4', duration=20,
                width=1920, height=1080, frame_rate=25, audio_codec='aac',
                start_time=1.01, end_time=3.99, media_path='/Volumes/我的素材/猫 & 水#1.mp4') | kwargs


def unpack(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return ET.fromstring(z.read('timeline.fcpxml')), json.loads(z.read('manifest.json'))


class EditHandoffUnitTests(unittest.TestCase):
    def test_resources_paths_ranges_order_and_repeat(self):
        xml, m = unpack(build_edit_handoff('猫 & 水', [clip(), clip(start_time=5, end_time=6)]))
        assets = xml.findall('./resources/asset')
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].find('media-rep').get('src'), 'file:///Volumes/%E6%88%91%E7%9A%84%E7%B4%A0%E6%9D%90/%E7%8C%AB%20%26%20%E6%B0%B4%231.mp4')
        self.assertEqual(m['clips'][0]['start'], '26/25s')
        self.assertEqual(m['clips'][0]['end'], '99/25s')
        cuts = xml.findall('.//spine/asset-clip')
        self.assertEqual([c.get('offset') for c in cuts], ['0s', '73/25s'])
        self.assertEqual(xml.find('.//sequence').get('duration'), '98/25s')
        self.assertEqual(cuts[0].get('name'), '猫 & 水 <01>.mp4')
        self.assertEqual(len(m['warnings']), 1)

    def test_ntsc_rational_no_float_drift(self):
        xml, m = unpack(build_edit_handoff('NTSC', [clip(frame_rate=29.97003, start_time=0, end_time=10)], '30000/1001'))
        self.assertEqual(xml.find('./resources/format').get('frameDuration'), '1001/30000s')
        self.assertEqual(m['timeline_duration'], '299299/30000s')

    def test_path_scheme_traversal_control_and_escaping(self):
        self.assertEqual(editor_file_uri(r'C:\clips\a b.mov'), 'file:///C:/clips/a%20b.mov')
        for path in ['https://host/a', 'file:///a', '//host/share/a', r'\\host\share\a', '../a', '/a/../b', '/a/./b', '/a/', '/a\x00b', '/a\nb', ' /a']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                editor_file_uri(path)

    def test_reject_invalid_ranges_rate_and_metadata(self):
        for overrides in [dict(start_time=-1), dict(start_time=math.nan), dict(end_time=math.inf), dict(end_time=True), dict(end_time=21), dict(start_time=5,end_time=5), dict(start_time=1.01,end_time=1.02), dict(frame_rate=None), dict(frame_rate=30), dict(width=0), dict(width=True), dict(name='a\x00b')]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                build_edit_handoff('test',[clip(**overrides)])
        for clips in [[], [clip()]*201]:
            with self.assertRaises(ValueError):build_edit_handoff('test',clips)
        with self.assertRaises(ValueError):build_edit_handoff('test',[clip()], '25/1')

    def test_conflicting_version_mapping_rejected(self):
        with self.assertRaises(ValueError):
            build_edit_handoff('test',[clip(),clip(media_path='/Volumes/other.mov')])

    def test_no_source_path_or_unselected_fields_leak(self):
        data = build_edit_handoff('test',[clip(path='/secret/NAS/a.mov', comments=['secret text'])])
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            content = b''.join(z.read(n) for n in z.namelist())
        self.assertNotIn(b'/secret', content)
        self.assertNotIn(b'secret text', content)
