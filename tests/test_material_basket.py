from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
        self.assertEqual(self.basket(self.member_headers), {"items": [], "total": 0})
        self.assertEqual(self.client.delete(f"{URL}/{first}", headers=self.member_headers).json()["total"], 0)
        self.assertEqual(self.basket()["total"], 2)
        self.assertEqual(self.client.delete(f"{URL}/{second}", headers=self.owner_headers).json()["total"], 1)
        self.assertEqual(self.client.delete(URL, headers=self.owner_headers).json(), {"items": [], "total": 0})

    def test_invisible_add_is_atomic_and_revocation_hides_metadata(self):
        first, second = self.file["id"], self.second_file["id"]
        self.assertEqual(self.add(first, 9_000_000, headers=self.member_headers).status_code, 404)
        self.assertEqual(self.basket(self.member_headers)["total"], 0)
        self.assertEqual(self.add(first, second, headers=self.member_headers).status_code, 200)
        state.workspaces.set_member(self.project, self.member["id"], "editor")
        state.database.execute("DELETE FROM library_permissions WHERE user_id = ?", (self.member["id"],))
        self.assertEqual(self.basket(self.member_headers), {
            "items": [{"id": first, "available": False}, {"id": second, "available": False}], "total": 2,
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
