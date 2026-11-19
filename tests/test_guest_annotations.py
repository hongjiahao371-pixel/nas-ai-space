from datetime import datetime, timedelta, timezone
import time
import unittest

from fastapi.testclient import TestClient
from test_core import app, state  # shared fixture isolates app settings before import


class GuestAnnotationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = TestClient(app)
        cls.client = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def setUp(self):
        self.project = self.client.post("/api/projects", json={"name": f"guest-annotation-{time.time_ns()}"}).json()["id"]
        self.addCleanup(state.database.execute, "DELETE FROM projects WHERE id = ?", (self.project,))
        self.asset, self.version = self.make_asset()
        self.share = self.make_share()
        self.drawing = [{"points": [{"x": 0.03, "y": 0.7}, {"x": 1, "y": 0}], "color": "#FFCC57"}]

    def make_asset(self, kind="video"):
        # A synthetic file with fixture metadata is enough for these API tests.
        upload = self.client.post("/api/uploads", content=b"fixture", headers={"X-Filename": f"draw-{time.time_ns()}.txt"})
        self.assertEqual(upload.status_code, 201)
        asset = self.client.post(f"/api/projects/{self.project}/assets", json={
            "file_id": upload.json()["file"]["id"], "title": "fixture",
        })
        self.assertEqual(asset.status_code, 201)
        asset = asset.json()
        vid = asset["versions"][0]["id"]
        state.database.execute("UPDATE asset_versions SET kind = ?, duration = ? WHERE id = ?", (
            kind, 12 if kind == "video" else None, vid,
        ))
        return asset["id"], vid

    def make_share(self, **kwargs):
        response = self.client.post(f"/api/projects/{self.project}/shares", json={
            "asset_id": self.asset, "name": "guest-annotation test", **kwargs,
        })
        self.assertEqual(response.status_code, 201)
        return response.json()

    def submit(self, share=None, **kwargs):
        return self.client.post(f"/api/public/shares/{(share or self.share)['token']}/comments", json={
            "asset_id": self.asset, "version_id": self.version, "body": "修掉这块", "guest_name": "访客",
            "time_start": 1.25, "drawing": self.drawing, **kwargs,
        })

    def test_drawing_round_trip_and_legacy_comment(self):
        hidden = self.client.post(f"/api/assets/{self.asset}/comments", json={
            "body": "团队内部", "version_id": self.version, "time_start": 1.25,
            "comment_type": "drawing", "drawing": self.drawing, "visibility": "team",
        })
        self.assertEqual(hidden.status_code, 201)
        plain = self.submit(drawing=[], body="普通文字", time_start=None)
        self.assertEqual(plain.status_code, 201)
        self.assertEqual(plain.json()["comment_type"], "text")
        response = self.submit()
        self.assertEqual(response.status_code, 201, response.text)
        comment = response.json()
        self.assertEqual(comment["comment_type"], "drawing")
        self.assertEqual(comment["time_start"], 1.25)
        self.assertEqual(comment["version_id"], self.version)
        self.assertEqual(comment["drawing"], [{**self.drawing[0], "color": "#ffcc57"}])
        self.assertTrue({"username", "user_id", "review_session_id", "visibility"}.isdisjoint(comment))
        public = self.client.post(f"/api/public/shares/{self.share['token']}", json={})
        self.assertEqual(public.status_code, 200)
        comments = public.json()["assets"][0]["comments"]
        self.assertEqual(comments, [plain.json(), comment])
        team = self.client.get(f"/api/assets/{self.asset}").json()["comments"]
        self.assertEqual(next(item for item in team if item["id"] == comment["id"])["drawing"], comment["drawing"])
        self.assertIn(hidden.json()["id"], {item["id"] for item in team})

    def test_invalid_geometry_and_times(self):
        point = {"x": 0.1, "y": 0.2}
        for bad in (
            None, "not a list", [{}], [{"points": []}], [{"points": [point], "color": "url(test)"}],
            [{"points": [{"x": -0.1, "y": 1}]}], [{"points": [{"x": 0.1, "y": 1.1}]}],
            [{"points": [{"x": True, "y": 0}]}], [{"points": [{"x": "0.1", "y": 0}]}],
            [{"points": [point] * 257}], [{"points": [point]}] * 33, [{"points": [point] * 256}] * 9,
        ):
            with self.subTest(invalid=str(bad)[:50]):
                self.assertEqual(self.submit(drawing=bad).status_code, 422)
        raw = b'{"asset_id":%d,"version_id":%d,"guest_name":"tester","body":"fixture","time_start":1,"drawing":[{"points":[{"x":NaN,"y":0}]}]}' % (
            self.asset, self.version,
        )
        endpoint = f"/api/public/shares/{self.share['token']}/comments"
        self.assertEqual(self.client.post(endpoint, content=raw, headers={"Content-Type": "application/json"}).status_code, 422)
        self.assertEqual(self.client.post(endpoint, content=b"x" * (160 * 1024 + 1)).status_code, 413)
        for overrides in ({"time_start": None}, {"time_start": 13}, {"time_end": 5}, {"version_id": None}):
            self.assertEqual(self.submit(**overrides).status_code, 409)
        self.assertEqual(self.submit(time_start=-1).status_code, 422)
        self.assertEqual(self.submit(time_start="Infinity").status_code, 422)
        self.assertFalse(state.workspaces.asset_detail(self.asset)["comments"])

    def test_image_supported_and_non_visual_media_rejected(self):
        self.asset, self.version = self.make_asset("image")
        share = self.make_share()
        self.assertEqual(self.submit(share, time_start=None).status_code, 201)
        self.assertEqual(self.submit(share).status_code, 409)
        self.asset, self.version = self.make_asset("audio")
        self.assertEqual(self.submit(self.make_share()).status_code, 409)

    def test_passcode_no_comment_wrong_asset_expiry_and_revocation(self):
        private = self.make_share(access_code="test-passcode")
        self.assertEqual(self.submit(private, access_code="incorrect").status_code, 401)
        self.assertEqual(self.submit(private, access_code="test-passcode").status_code, 201)
        self.assertEqual(self.submit(self.make_share(can_comment=False)).status_code, 403)
        another_asset, another_version = self.make_asset()
        self.assertEqual(self.submit(asset_id=another_asset, version_id=another_version).status_code, 404)
        self.assertEqual(self.submit(asset_id=987654321).status_code, 404)
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        state.database.execute("UPDATE share_links SET expires_at = ? WHERE id = ?", (past, self.share["id"]))
        self.assertEqual(self.submit().status_code, 404)
        self.client.put(f"/api/shares/{private['id']}/enabled?enabled=false")
        self.assertEqual(self.submit(private, access_code="test-passcode").status_code, 404)

    def test_version_visibility_is_applied_to_creation_and_reading(self):
        all_versions = self.make_share(can_view_versions=True)
        old = self.submit(all_versions)
        self.assertEqual(old.status_code, 201)
        fixture = self.client.post("/api/uploads", content=b"fixture new version", headers={"X-Filename": f"new-{time.time_ns()}.txt"})
        version = self.client.post(f"/api/assets/{self.asset}/versions", json={"file_id": fixture.json()["file"]["id"]})
        self.assertEqual(version.status_code, 201, version.text)
        self.assertEqual(self.submit().status_code, 409)
        restricted = self.client.post(f"/api/public/shares/{self.share['token']}", json={}).json()["assets"][0]
        self.assertNotIn(self.version, {item["id"] for item in restricted["versions"]})
        self.assertFalse(restricted["comments"])
        unrestricted = self.client.post(f"/api/public/shares/{all_versions['token']}", json={}).json()["assets"][0]
        self.assertEqual(unrestricted["comments"], [old.json()])
