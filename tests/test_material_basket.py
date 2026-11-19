from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import io
import json
import subprocess
import time
import zipfile
from tempfile import TemporaryDirectory
from uuid import uuid4
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_core import app, state
from app.database import Database
from app.main import settings
from app.services.material_basket import MaterialBasket
from app.services.workspaces import WorkspaceService
from app.security import token_digest


URL = "/api/material-basket"
EMPTY_BASKET = {"items": [], "total": 0, "clips": [], "clip_total": 0, "combined_total": 0}


class MaterialBasketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = TestClient(app)
        cls.client = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def setUp(self):
        self.owner, self.owner_headers = self.account("owner")
        self.file = self.upload()
        self.second_file = self.upload()
        self.member, self.member_headers = self.account("member", [self.file["library_id"]])
        project = self.client.post("/api/projects", json={"name": f"basket-{uuid4().hex}"}, headers=self.owner_headers)
        self.assertEqual(project.status_code, 201, project.text)
        self.project = project.json()["id"]
        self.addCleanup(state.database.execute, "DELETE FROM projects WHERE id = ?", (self.project,))

    def account(self, role, library_ids=()):
        name = f"basket-{role}-{uuid4().hex}"
        user = state.database.create_user(name, name, "unused-password-hash", role, list(library_ids))
        self.addCleanup(state.database.execute, "DELETE FROM users WHERE id = ?", (user["id"],))
        token = uuid4().hex
        state.database.create_session(
            user["id"], token_digest(token), (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        return user, {"Authorization": f"Bearer {token}"}

    def upload(self):
        response = self.client.post("/api/uploads", content=b"synthetic material fixture", headers={
            **self.owner_headers, "X-Filename": f"basket-{uuid4().hex}.txt",
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["file"]

    def add(self, *file_ids, headers=None):
        return self.client.post(URL, json={"file_ids": file_ids}, headers=headers or self.owner_headers)

    def basket(self, headers=None):
        response = self.client.get(URL, headers=headers or self.owner_headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def handoff(self, headers=None):
        return self.client.post(URL + "/handoff", json={"project_id": self.project}, headers=headers or self.owner_headers)

    def test_order_dedup_persistence_and_account_separation(self):
        first, second = self.file["id"], self.second_file["id"]
        self.assertEqual(self.add(first, first, second).status_code, 200)
        new_token = uuid4().hex
        state.database.create_session(
            self.owner["id"], token_digest(new_token), (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        again = self.basket({"Authorization": f"Bearer {new_token}"})
        self.assertEqual([item["id"] for item in again["items"]], [first, second])
        self.assertEqual(self.basket(self.member_headers), EMPTY_BASKET)
        self.assertEqual(self.client.delete(f"{URL}/{first}", headers=self.member_headers).json()["total"], 0)
        self.assertEqual(self.basket()["total"], 2)
        self.assertEqual(self.client.delete(f"{URL}/{second}", headers=self.owner_headers).json()["total"], 1)
        self.assertEqual(self.client.delete(URL, headers=self.owner_headers).json(), EMPTY_BASKET)

    def test_invisible_add_is_atomic_and_revocation_hides_metadata(self):
        first, second = self.file["id"], self.second_file["id"]
        self.assertEqual(self.add(first, 9_000_000, headers=self.member_headers).status_code, 404)
        self.assertEqual(self.basket(self.member_headers)["total"], 0)
        self.assertEqual(self.add(first, second, headers=self.member_headers).status_code, 200)
        state.workspaces.set_member(self.project, self.member["id"], "editor")
        state.database.execute("DELETE FROM library_permissions WHERE user_id = ?", (self.member["id"],))
        self.assertEqual(self.basket(self.member_headers), {
            **EMPTY_BASKET, "items": [{"id": first, "available": False}, {"id": second, "available": False}],
            "total": 2, "combined_total": 2,
        })
        self.assertEqual(self.handoff(self.member_headers).status_code, 409)

    def test_access_stale_import_is_atomic(self):
        first, second = self.file["id"], self.second_file["id"]
        self.assertEqual(self.add(first, second).status_code, 200)
        missing_file = state.database.get_file(second)
        Path(missing_file["path"]).unlink()  # synthetic test input, index deliberately retained
        self.assertEqual(self.basket()["items"][1], {"id": second, "available": False})
        state.database.execute("DELETE FROM files WHERE id = ?", (second,))
        self.assertEqual(self.basket()["items"][1], {"id": second, "available": False})
        self.assertEqual(self.handoff().status_code, 409)
        self.assertEqual(state.workspaces.list_assets(self.project)["total"], 0)

    def test_source_changed_before_handoff_needs_rescan(self):
        first = self.file["id"]
        self.assertEqual(self.add(first).status_code, 200)
        original = state.database.get_file(first)
        Path(original["path"]).write_bytes(b"synthetic updated content, not yet indexed")
        self.assertEqual(self.basket()["items"], [{"id": first, "available": False}])
        self.assertEqual(self.handoff().status_code, 409)
        self.assertEqual(state.workspaces.list_assets(self.project)["total"], 0)

    def test_handoff_project_permissions_idempotence_and_retry(self):
        first, second = self.file["id"], self.second_file["id"]
        self.assertEqual(self.add(first, second, headers=self.member_headers).status_code, 200)
        self.assertEqual(self.handoff(self.member_headers).status_code, 404)
        state.workspaces.set_member(self.project, self.member["id"], "reviewer")
        self.assertEqual(self.handoff(self.member_headers).status_code, 403)
        state.workspaces.set_member(self.project, self.member["id"], "editor")
        old = state.workspaces.create_asset(self.project, state.database.get_file(first), None, "existing", self.owner["id"])
        handoff = self.handoff(self.member_headers)
        self.assertEqual(handoff.status_code, 200, handoff.text)
        result = handoff.json()
        self.assertEqual((result["project_id"], result["added"], result["skipped"]), (self.project, 1, 1))
        self.assertEqual(result["items"][0], {"id": old["id"], "file_id": first, "title": "existing", "created": False})
        self.assertEqual([item["file_id"] for item in result["items"]], [first, second])
        repeat = self.handoff(self.member_headers).json()
        self.assertEqual((repeat["added"], repeat["skipped"]), (0, 2))
        self.assertEqual([item["id"] for item in repeat["items"]], [item["id"] for item in result["items"]])
        self.assertEqual(self.basket(self.member_headers)["total"], 2)
        self.assertEqual(state.workspaces.list_assets(self.project)["total"], 2)

    def test_handoff_concurrent_retry_serializes(self):
        self.assertEqual(self.add(self.file["id"], self.second_file["id"]).status_code, 200)
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: self.handoff(), range(2)))
        self.assertEqual([item.status_code for item in results], [200, 200])
        self.assertEqual(sorted(item.json()["added"] for item in results), [0, 2])
        self.assertEqual(state.workspaces.list_assets(self.project)["total"], 2)

    def test_upgrade_can_initialize_again_without_rewriting_existing_records(self):
        with TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.db")
            self.addCleanup(database.close)
            database.initialize()
            database.execute("DROP TABLE material_basket_items")
            workspaces = WorkspaceService(database)
            owner = database.create_user("pre-basket-owner", "owner", "unused", "owner", [])
            project = workspaces.create_project("pre-basket project", "", "#7c8cff", owner["id"])
            database.initialize()
            basket = MaterialBasket(database, workspaces)
            self.assertEqual(basket.ids(owner["id"]), [])
            basket.add(owner["id"], [123], lambda file_id: {"id": file_id})
            database.initialize()
            self.assertEqual(basket.ids(owner["id"]), [123])
            self.assertEqual(workspaces.get_project(project["id"])["name"], "pre-basket project")

    def test_capacity_machine_and_anonymous_access(self):
        first = self.file["id"]
        self.assertEqual(self.add(*([first] * 201)).status_code, 422)
        self.assertEqual(self.client.get(URL).status_code, 401)
        with patch("app.main.settings", replace(settings, api_token="test-machine-token")):
            self.assertEqual(self.client.get(URL, headers={"X-API-Token": "test-machine-token"}).status_code, 409)
        with state.database.transaction() as connection:
            connection.executemany("INSERT INTO material_basket_items (user_id, file_id) VALUES (?, ?)",
                                   ((self.owner["id"], 9_000_000 + i) for i in range(200)))
        self.assertEqual(self.add(first).status_code, 409)
        self.assertEqual(self.basket()["total"], 200)

    def make_video(self, file=None, duration=12.25):
        file = file or self.file
        state.database.execute(
            """UPDATE files SET kind='video', duration=?, metadata_json='{"frame_rate": 24}' WHERE id=?""",
            (duration, file["id"]),
        )
        return file["id"]

    def add_clip(self, file_id, start_time=1, end_time=2, headers=None):
        return self.client.post(URL + "/clips", json={
            "file_id": file_id, "start_time": start_time, "end_time": end_time,
        }, headers=headers or self.owner_headers)

    def test_clip_dedup_multiple_ranges_and_persisted_order(self):
        file_id = self.make_video()
        first = self.add_clip(file_id, 1, 2).json()["clips"][0]["id"]
        repeated = self.add_clip(file_id, 1.0000001, 2).json()
        self.assertEqual(repeated["clip_total"], 1)
        self.assertEqual(repeated["clips"][0]["id"], first)
        second = self.add_clip(file_id, 3, 4).json()["clips"][1]["id"]
        third = self.add_clip(file_id, 4, 6).json()["clips"][2]["id"]
        updated = self.client.put(URL + "/clips/order", json={"clip_ids": [third, second, first]}, headers=self.owner_headers)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual([clip["id"] for clip in self.basket()["clips"]], [third, second, first])
        self.assertEqual([clip["position"] for clip in self.basket()["clips"]], [0, 1, 2])
        self.assertEqual(self.client.patch(URL + f"/clips/{second}", json={
            "start_time": 1, "end_time": 2,
        }, headers=self.owner_headers).status_code, 409)
        self.assertEqual(self.client.patch(URL + f"/clips/{second}", json={
            "start_time": 3.234567, "end_time": 4,
        }, headers=self.owner_headers).json()["clips"][1]["start_time"], 3.234567)
        self.assertEqual(self.client.delete(URL + f"/clips/{second}", headers=self.owner_headers).status_code, 200)
        self.assertEqual([clip["position"] for clip in self.basket()["clips"]], [0, 1])
        self.assertEqual(self.client.delete(URL, headers=self.owner_headers).json(), EMPTY_BASKET)

    def test_clip_ranges_kind_combined_limit_and_unknown_duration(self):
        file_id = self.make_video()
        for start, end in ((-1, 2), (2, 2), (3, 1), (0, 0.0000001), (0, 99999999), ("1", 2), (True, 2)):
            self.assertEqual(self.add_clip(file_id, start, end).status_code, 422)
        for token in (b'NaN', b'Infinity', b'1e999'):
            raw = b'{"file_id":%d,"start_time":0,"end_time":%s}' % (file_id, token)
            response = self.client.post(URL + "/clips", content=raw, headers={
                **self.owner_headers, "Content-Type": "application/json",
            })
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.add_clip(file_id, 0, 12.26).status_code, 409)
        self.assertEqual(self.add_clip(self.second_file["id"]).status_code, 409)
        self.make_video(self.second_file, duration=None)
        self.assertEqual(self.add_clip(self.second_file["id"]).status_code, 409)
        self.assertEqual(self.add_clip(file_id, 0, 12.25).status_code, 200)
        with state.database.transaction() as connection:
            connection.executemany("INSERT INTO material_basket_items (user_id, file_id) VALUES (?, ?)",
                                   ((self.owner["id"], 9_000_000 + i) for i in range(199)))
        self.assertEqual(self.add(file_id).status_code, 409)
        self.assertEqual(self.add_clip(file_id, 0, 2).status_code, 409)
        self.assertEqual(self.add_clip(file_id, 0, 12.25).json()["combined_total"], 200)

    def test_clip_owner_separation_reorder_and_revocation(self):
        file_id = self.make_video()
        clip_id = self.add_clip(file_id, headers=self.member_headers).json()["clips"][0]["id"]
        self.assertEqual(self.basket(), EMPTY_BASKET)
        self.assertEqual(self.client.patch(URL + f"/clips/{clip_id}", json={"start_time": 2, "end_time": 3},
                                           headers=self.owner_headers).status_code, 404)
        self.assertEqual(self.client.delete(URL + f"/clips/{clip_id}", headers=self.owner_headers).status_code, 404)
        for ids in ([clip_id], [clip_id, clip_id]):
            self.assertEqual(self.client.put(URL + "/clips/order", json={"clip_ids": ids},
                                             headers=self.owner_headers).status_code, 409)
        state.database.execute("DELETE FROM library_permissions WHERE user_id=?", (self.member["id"],))
        self.assertEqual(self.basket(self.member_headers)["clips"], [{
            "id": clip_id, "file_id": file_id, "start_time": 1.0, "end_time": 2.0, "position": 0, "available": False,
        }])
        self.assertEqual(self.client.delete(URL + f"/clips/{clip_id}", headers=self.member_headers).json(), EMPTY_BASKET)

    def test_handoff_unions_sources_uses_exact_existing_version_and_stays_atomic(self):
        file_id = self.make_video()
        other_id = self.second_file["id"]
        old = state.workspaces.create_asset(self.project, state.database.get_file(file_id), None, "existing", self.owner["id"])
        existing_version = old["versions"][0]["id"]
        cover_file = self.upload()
        state.workspaces.add_version(old["id"], state.database.get_file(cover_file["id"]), "new", "", self.owner["id"])
        self.assertEqual(self.add(other_id, file_id).status_code, 200)
        second_id = self.add_clip(file_id, 2.125, 3).json()["clips"][0]["id"]
        first_id = self.add_clip(file_id, 1, 1.5).json()["clips"][1]["id"]
        self.client.put(URL + "/clips/order", json={"clip_ids": [first_id, second_id]}, headers=self.owner_headers)
        state.database.execute("UPDATE files SET duration=2 WHERE id=?", (file_id,))
        self.assertEqual(self.handoff().status_code, 409)
        self.assertEqual(state.workspaces.list_assets(self.project)["total"], 1)
        self.make_video()
        result = self.handoff().json()
        self.assertEqual((result["added"], result["skipped"]), (1, 1))
        self.assertEqual([clip["id"] for clip in result["clips"]], [first_id, second_id])
        self.assertEqual(result["clips"][0], {
            "id": first_id, "position": 0, "asset_id": old["id"], "version_id": existing_version, "version_number": 1,
            "file_id": file_id, "title": "existing", "start_time": 1.0, "end_time": 1.5,
            "duration": 0.5, "source_duration": 12.25, "frame_rate": 24.0,
            "source_revision": f"{state.database.get_file(file_id)['mtime_ns']}:{state.database.get_file(file_id)['size']}",
        })
        repeated = self.handoff().json()
        self.assertEqual((repeated["added"], repeated["skipped"]), (0, 2))
        self.assertEqual(repeated["clips"], result["clips"])

    def test_clip_schema_upgrade_preserves_material_basket_items(self):
        with TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.db")
            self.addCleanup(database.close)
            database.initialize()
            database.execute("DROP TABLE material_basket_clips")
            basket = MaterialBasket(database, WorkspaceService(database))
            owner = database.create_user("pre-clip-owner", "owner", "unused", "owner", [])
            # Seed the exact old schema record, before a new code startup creates clips.
            database.execute("INSERT INTO material_basket_items (user_id,file_id) VALUES (?,?)", (owner["id"], 123))
            database.initialize()
            self.assertEqual(basket.snapshot(owner["id"]), ([123], []))

    def test_saved_clip_revision_survives_metadata_but_not_rescanned_replacement(self):
        file_id = self.make_video()
        clip = self.add_clip(file_id).json()["clips"][0]
        state.database.execute("UPDATE files SET ai_caption='metadata only' WHERE id=?", (file_id,))
        self.assertTrue(self.basket()["clips"][0]["available"])
        original = state.database.get_file(file_id)
        path = Path(original["path"])
        path.write_bytes(path.read_bytes() + b"replacement revision")
        stat = path.stat()
        state.database.execute("UPDATE files SET size=?,mtime_ns=? WHERE id=?", (stat.st_size, stat.st_mtime_ns, file_id))
        entry = self.basket()["clips"][0]
        self.assertFalse(entry["available"])
        self.assertNotIn("name", entry)
        self.assertEqual(self.handoff().status_code, 409)
        self.assertEqual(self.add_clip(file_id).status_code, 409)
        self.assertEqual(self.client.patch(URL + f"/clips/{clip['id']}", json={"start_time":0,"end_time":2}, headers=self.owner_headers).status_code, 409)
        self.assertEqual(state.database.fetchone("SELECT COUNT(*) n FROM assets WHERE project_id=?", (self.project,))["n"], 0)

    def test_unbound_old_clip_rows_fail_closed_after_additive_migration(self):
        file_id = self.make_video()
        self.add_clip(file_id)
        state.database.execute("UPDATE material_basket_clips SET source_mtime_ns=NULL,source_size=NULL WHERE user_id=?", (self.owner["id"],))
        self.assertFalse(self.basket()["clips"][0]["available"])
        self.assertEqual(self.handoff().status_code, 409)
        with TemporaryDirectory() as directory:
            database = Database(Path(directory) / "index.db")
            self.addCleanup(database.close)
            database.initialize()
            owner = database.create_user("old-range-owner", "owner", "unused", "owner", [])
            database.execute("ALTER TABLE material_basket_clips DROP COLUMN source_mtime_ns")
            database.execute("ALTER TABLE material_basket_clips DROP COLUMN source_size")
            database.execute("INSERT INTO material_basket_clips(user_id,file_id,start_time,end_time,position) VALUES (?,?,?,?,?)", (owner["id"], 123, 1, 2, 0))
            database.initialize()
            row = database.fetchone("SELECT * FROM material_basket_clips WHERE user_id=?", (owner["id"],))
            self.assertIsNone(row["source_mtime_ns"])
            self.assertIsNone(row["source_size"])
            self.assertEqual(row["start_time"], 1)

    def test_authenticated_clip_basket_to_real_edit_handoff_zip(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "clip-basket.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=25",
                "-t", "5", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(path),
            ], check=True)
            sources = []
            for _ in range(2):
                response = self.client.post("/api/uploads", content=path.read_bytes(), headers={
                    **self.owner_headers, "X-Filename": f"clip-basket-{uuid4().hex}.mp4",
                })
                self.assertEqual(response.status_code, 201, response.text)
                sources.append(response.json()["file"]["id"])
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            files = [state.database.get_file(file_id) for file_id in sources]
            if all(file["duration"] and file["width"] and "frame_rate" in file["metadata_json"] for file in files):
                break
            time.sleep(0.05)
        else:
            self.fail("Generated video did not finish metadata indexing")
        # Reuse the historical V1 mapping although the project's cover is V2.
        asset = state.workspaces.create_asset(self.project, files[0], None, "video", self.owner["id"])
        version1 = asset["versions"][0]["id"]
        version2 = state.workspaces.add_version(asset["id"], files[1], "new", "", self.owner["id"])["id"]
        entries = []
        for file_id, start, end in ((sources[0], 1.2, 2.4), (sources[1], 2, 3), (sources[0], 0, 1)):
            added = self.add_clip(file_id, start, end)
            self.assertEqual(added.status_code, 200, added.text)
            entries.append(added.json()["clips"][-1]["id"])
        ordered = self.client.put(URL + "/clips/order", json={"clip_ids": entries[::-1]}, headers=self.owner_headers)
        self.assertEqual(ordered.status_code, 200, ordered.text)
        handoff = self.handoff()
        self.assertEqual(handoff.status_code, 200, handoff.text)
        for file in files:
            self.assertNotIn(file["path"], handoff.text)
        mapped = handoff.json()["clips"]
        self.assertEqual([clip["version_id"] for clip in mapped], [version1, version2, version1])
        self.assertEqual([clip["version_number"] for clip in mapped], [1, 2, 1])
        result = self.client.post(f"/api/projects/{self.project}/edit-handoff", headers=self.owner_headers, json={
            "frame_rate": "25",
            "clips": [{k: clip[k] for k in ("version_id", "start_time", "end_time", "source_revision")}
                      | {"media_path": f"/Volumes/FixtureEdit/v{clip['version_number']}.mp4"} for clip in mapped],
        })
        self.assertEqual(result.status_code, 200, result.text[:500])
        with zipfile.ZipFile(io.BytesIO(result.content)) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
            all_contents = b"".join(bundle.read(name) for name in bundle.namelist())
        for file in files:
            self.assertNotIn(file["path"].encode(), all_contents)
        self.assertEqual([entry["version_id"] for entry in manifest["clips"]], [version1, version2, version1])
        self.assertEqual([entry["file_id"] for entry in manifest["clips"]], [sources[0], sources[1], sources[0]])
        self.assertEqual([(entry["requested_start_seconds"], entry["requested_end_seconds"])
                          for entry in manifest["clips"]], [(0, 1), (2, 3), (1.2, 2.4)])
        self.assertEqual(manifest["clips"][0]["resource"], manifest["clips"][2]["resource"])
        self.assertNotEqual(manifest["clips"][0]["resource"], manifest["clips"][1]["resource"])

        file = state.database.get_file(sources[0])
        source_path = Path(file["path"])
        source_path.write_bytes(source_path.read_bytes() + b"changed after handoff")
        stat = source_path.stat()
        state.database.execute("UPDATE files SET size=?,mtime_ns=? WHERE id=?", (stat.st_size,stat.st_mtime_ns,sources[0]))
        rejected = self.client.post(f"/api/projects/{self.project}/edit-handoff", headers=self.owner_headers, json={
            "frame_rate":"25", "clips":[{k:mapped[0][k] for k in ("version_id","start_time","end_time","source_revision")} | {"media_path":"/Volumes/FixtureEdit/source.mp4"}],
        })
        self.assertEqual(rejected.status_code,409)
