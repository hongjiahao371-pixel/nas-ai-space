"""Frame search opens a real synthetic MP4; only similarity inference is stubbed."""
import base64
import io
import shutil
import subprocess
import unittest
from unittest.mock import patch

from PIL import Image

from test_improvements import ApiImprovementsTests

@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required")
class FrameSearchTests(unittest.TestCase):
    setUp = ApiImprovementsTests.setUp
    tearDown = ApiImprovementsTests.tearDown
    hit = ApiImprovementsTests.hit

    def setUpVideos(self):
        from app.services.scanner import scan_library
        source = self.library / "合成查询.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:size=160x90:rate=25",
            "-t", "2", "-threads", "1", "-c:v", "mpeg4", str(source),
        ], check=True, timeout=15)
        for filename in ("合成候选.mp4", "撤权素材.mp4"):
            shutil.copyfile(source, self.library / filename)
        scan_library(self.db, self.lib, lambda *_: None, lambda: False)
        self.db.execute("UPDATE files SET duration=2 WHERE kind='video'")
        rows = {row["name"]: row for row in self.db.fetchall("SELECT * FROM files WHERE kind='video'")}
        return [rows[name] for name in (source.name, "合成候选.mp4", "撤权素材.mp4")]

    def frame_hit(self, row, score, source_label="直接视频画面"):
        hit = self.hit(row, score)
        hit["payload"].update(source_label=source_label, start_time=0.8)
        return hit

    def test_real_frame_and_filtered_results(self):
        source, match, hidden = self.setUpVideos()
        alternate = self.root / "other"
        alternate.mkdir()
        library = self.db.create_library("不可见", str(alternate))
        self.db.execute("UPDATE files SET library_id=? WHERE id=?", (library["id"], hidden["id"]))
        hidden = self.db.get_file(hidden["id"])
        from app.security import hash_password
        self.db.create_user("frame-qa", "frame-qa", hash_password("TestPassword123"), "viewer", [self.lib["id"]])
        login = self.client.post("/api/auth/login", json={"username": "frame-qa", "password": "TestPassword123"})
        self.assertEqual(login.status_code, 200, login.text)
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        details = self.client.get(f'/api/files/{source["id"]}', headers=headers).json()
        self.assertEqual(details["frame_revision"], f'{source["mtime_ns"]}:{source["size"]}')
        fake = [self.frame_hit(source, .99), self.frame_hit(hidden, .98),
                self.frame_hit(match, .9, "视频音轨"), self.frame_hit(match, .72)]
        with patch.object(self.main.state.search.multimodal, "image_search", return_value=fake) as query:
            r = self.client.post(f'/api/files/{source["id"]}/frame-similar', headers=headers,
                                 params={"revision": details["frame_revision"], "time": .45})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.headers["Cache-Control"], "private, no-store")
            self.assertEqual(r.json()["source_time"], .45)
            self.assertEqual([(i["id"], i["match_time"], i["score"]) for i in r.json()["results"]],
                             [(match["id"], .8, .72)])
            args, kwargs = query.call_args
            self.assertEqual(args[2:], ("video", [self.lib["id"]]))
            self.assertEqual(kwargs, {"source_label": "直接视频画面"})
            data_url = args[0]["content"][0]["image_url"]["url"]
            with Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1]))) as img:
                r, g, b = img.convert("RGB").getpixel((30, 30))
                self.assertGreater(r, 220)
                self.assertLess(max(g, b), 20)

    def test_bad_revision_time_and_disabled_model(self):
        source, _, _ = self.setUpVideos()
        url = f'/api/files/{source["id"]}/frame-similar'
        revision = f'{source["mtime_ns"]}:{source["size"]}'
        cases = [({}, 422), ({"revision": "old", "time": .25}, 409),
                 ({"revision": revision, "time": 2}, 409), ({"revision": revision, "time": -1}, 422)]
        with patch.object(self.main.state.search.multimodal, "image_search") as query:
            for params, expected in cases:
                response = self.client.post(url, headers=self.headers, params=params)
                self.assertEqual(response.status_code, expected, response.text)
            service = self.main.state.search.multimodal
            with patch.object(service, "enabled", False):
                r = self.client.post(url, headers=self.headers, params={"revision": revision, "time": .25})
                self.assertEqual(r.status_code, 409, r.text)
                self.assertIn("启用素材语义模型", r.json()["detail"])
            query.assert_not_called()

    def test_busy_slot_returns_retryable_error(self):
        source, _, _ = self.setUpVideos()
        slot = self.main._video_frame_search_slot
        self.assertTrue(slot.acquire(blocking=False))
        try:
            r = self.client.post(f'/api/files/{source["id"]}/frame-similar', headers=self.headers,
                                 params={"revision": f'{source["mtime_ns"]}:{source["size"]}', "time": .25})
            self.assertEqual(r.status_code, 503, r.text)
        finally:
            slot.release()

# Do not rerun the inherited fixture's test suite via imported class.
del ApiImprovementsTests
