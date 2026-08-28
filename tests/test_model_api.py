"""Unit tests for the dashboard model picker API.

Run:
    python -m unittest tests.test_model_api
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp())
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pw")
os.environ.setdefault("BOOA_INSECURE_COOKIES", "1")
os.environ.setdefault("BOOA_LOGIN_THROTTLE_SECONDS", "0")

import yaml
from starlette.testclient import TestClient

import server


CATALOG = {"data": [
    {"id": "anthropic/claude-sonnet-5", "name": "Claude Sonnet 5", "created": 2,
     "context_length": 1000000, "pricing": {"prompt": "0.000002", "completion": "0.00001"},
     "supported_parameters": ["tools", "temperature"]},
    {"id": "some/vision-only", "name": "No Tools", "created": 3,
     "context_length": 8192, "pricing": {}, "supported_parameters": ["temperature"]},
    {"id": "older/model", "name": "Older", "created": 1,
     "context_length": 4096, "pricing": {}, "supported_parameters": ["tools"]},
    {"id": "bad id with spaces", "name": "Bad", "created": 4,
     "context_length": 1, "pricing": {}, "supported_parameters": ["tools"]},
]}


class _FakeResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return CATALOG


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        return _FakeResponse()


class ModelApiTests(unittest.TestCase):
    def setUp(self):
        server._models_cache.update(ts=0.0, data=[])
        self.home = Path(server.HERMES_HOME)
        (self.home / "config.yaml").write_text(yaml.dump(
            {"model": {"default": "old/model", "provider": "openrouter"}},
        ))
        self.client = TestClient(server.app)
        self.client.post("/login", data={"username": "admin", "password": os.environ["ADMIN_PASSWORD"]})

    def tearDown(self):
        try:
            (self.home / "config.yaml").unlink()
        except FileNotFoundError:
            pass
        server._models_cache.update(ts=0.0, data=[])

    def test_models_requires_auth(self):
        self.assertEqual(TestClient(server.app).get("/api/models").status_code, 401)

    def test_models_filters_to_toolcapable_and_sorts_newest_first(self):
        with mock.patch.object(server.httpx, "AsyncClient", _FakeClient):
            res = self.client.get("/api/models")
        self.assertEqual(res.status_code, 200, res.text)
        ids = [m["id"] for m in res.json()["models"]]
        self.assertEqual(ids, ["anthropic/claude-sonnet-5", "older/model"])

    def test_set_model_requires_auth(self):
        res = TestClient(server.app).post("/api/model", json={"model": "a/b"})
        self.assertEqual(res.status_code, 401)

    def test_set_model_updates_config(self):
        res = self.client.post("/api/model", json={"model": "anthropic/claude-sonnet-5"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["restart_needed"])
        cfg = yaml.safe_load((self.home / "config.yaml").read_text())
        self.assertEqual(cfg["model"]["default"], "anthropic/claude-sonnet-5")
        self.assertEqual(cfg["model"]["provider"], "openrouter")

    def test_garbage_model_ids_rejected(self):
        for bad in ("", "a" * 200, "model with spaces", "x;rm -rf /", "ünïcode/model"):
            res = self.client.post("/api/model", json={"model": bad})
            self.assertEqual(res.status_code, 400, bad)


if __name__ == "__main__":
    unittest.main()
