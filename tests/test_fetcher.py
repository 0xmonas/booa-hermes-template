"""Unit tests for the BOOA identity fetcher.

Run:
    python -m unittest tests.test_fetcher
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

import httpx

from booa import fetcher


def _client_returning(status_for):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            status = status_for(url)
            if status != 200:
                body = '{"error":"Too many requests"}'
            elif url.endswith("agent.json"):
                body = '{"name": "Ink-Sync"}'
            else:
                body = "# SOUL"
            return httpx.Response(status, text=body, request=httpx.Request("GET", url))

    return FakeClient


class FetchIdentityTest(unittest.TestCase):
    def test_refuses_error_bodies(self):
        for status in (429, 500, 502):
            fake = _client_returning(lambda url, s=status: s if url.endswith("soul.md") else 200)
            with mock.patch.object(fetcher.httpx, "AsyncClient", fake):
                with self.assertRaises(httpx.HTTPStatusError):
                    asyncio.run(fetcher.fetch_booa_identity(1496))

    def test_still_maps_404_to_token_not_found(self):
        fake = _client_returning(lambda url: 404 if url.endswith("soul.md") else 200)
        with mock.patch.object(fetcher.httpx, "AsyncClient", fake):
            with self.assertRaises(fetcher.TokenNotFound):
                asyncio.run(fetcher.fetch_booa_identity(1496))

    def test_returns_files_on_success(self):
        with mock.patch.object(fetcher.httpx, "AsyncClient", _client_returning(lambda url: 200)):
            data = asyncio.run(fetcher.fetch_booa_identity(1496))
        self.assertEqual(data["soul_md"], "# SOUL")


if __name__ == "__main__":
    unittest.main()
