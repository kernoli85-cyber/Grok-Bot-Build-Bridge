#!/usr/bin/env python3
"""Unit tests for balu_notify — no network, no real secrets."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ROOT))

import balu_notify as bn  # noqa: E402


class FakeResponse:
    def __init__(self, status: int = 200, headers=None):
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestConfig(unittest.TestCase):
    def test_rejects_non_https(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.json"
            p.write_text(
                json.dumps({"url": "http://example.com/x", "key": "k"}),
                encoding="utf-8",
            )
            os.chmod(p, 0o600)
            with self.assertRaises(SystemExit) as cm:
                bn.load_config(p)
            self.assertIn("https", str(cm.exception).lower())

    def test_rejects_open_perms(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.json"
            p.write_text(
                json.dumps({"url": "https://example.com/x", "key": "secret"}),
                encoding="utf-8",
            )
            os.chmod(p, 0o644)
            with self.assertRaises(SystemExit) as cm:
                bn.load_config(p)
            self.assertIn("permissions", str(cm.exception).lower())

    def test_accepts_https_600(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.json"
            p.write_text(
                json.dumps({"url": "https://example.com/hook", "key": "sekrit"}),
                encoding="utf-8",
            )
            os.chmod(p, 0o600)
            cfg = bn.load_config(p)
            self.assertEqual(cfg["url"], "https://example.com/hook")
            self.assertEqual(cfg["key"], "sekrit")


class TestPayload(unittest.TestCase):
    def test_default_ping(self):
        body = bn.build_payload(None, "balu-notify")
        self.assertEqual(body["status"], "ping")
        self.assertEqual(body["event"], "balu_notify")
        self.assertNotIn("transcript", body)

    def test_status_limit(self):
        with self.assertRaises(SystemExit):
            bn.build_payload("x" * (bn.MAX_STATUS_CHARS + 1), "s")


class TestPost(unittest.TestCase):
    def test_posts_200_and_hides_key_in_stdout(self):
        captured = {}

        class Opener:
            def open(self, req, timeout=10):
                captured["url"] = req.full_url
                captured["auth"] = req.get_header("Authorization")
                captured["method"] = req.get_method()
                captured["body"] = req.data
                return FakeResponse(200)

        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = bn.post_webhook(
                "https://example.com/hook",
                "SUPERSECRETKEY",
                {"source": "t", "event": "balu_notify", "version": 1, "status": "ping"},
                opener=Opener(),
            )
        self.assertEqual(code, 200)
        out = buf.getvalue()
        self.assertIn("HTTP 200", out)
        self.assertNotIn("SUPERSECRETKEY", out)
        self.assertEqual(captured["auth"], "Bearer SUPERSECRETKEY")
        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["url"].startswith("https://"))

    def test_refuses_redirect(self):
        class Opener:
            def open(self, req, timeout=10):
                raise urllib.error.HTTPError(
                    req.full_url, 302, "redirect refused (302)", {}, None
                )

        buf = io.StringIO()
        with mock.patch("sys.stderr", buf):
            code = bn.post_webhook(
                "https://example.com/hook",
                "k",
                {"status": "ping"},
                opener=Opener(),
            )
        self.assertEqual(code, 302)
        self.assertNotIn("k", buf.getvalue())  # key not in stderr either from our code


class TestDryRun(unittest.TestCase):
    def test_dry_run_no_network(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.json"
            p.write_text(
                json.dumps({"url": "https://example.com/h", "key": "SECRETVALUE"}),
                encoding="utf-8",
            )
            os.chmod(p, 0o600)
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = bn.main(["--config", str(p), "--dry-run"])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertNotIn("SECRETVALUE", out)
            self.assertNotIn("example.com", out)  # url not printed on dry-run
            data = json.loads(out)
            self.assertTrue(data["ok"])
            self.assertIn("status", data["payload_keys"])


if __name__ == "__main__":
    unittest.main()
