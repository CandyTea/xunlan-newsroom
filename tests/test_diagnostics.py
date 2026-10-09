"""Read-only source diagnostics CLI checks."""
import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app import diagnostics


def source(name, category="sports", enabled=True):
    return {"id": name, "name": name, "kind": "rss", "url": "https://example.com/feed",
            "category": category, "enabled": enabled, "steam_appid": None}


class DiagnosticsTests(unittest.TestCase):
    def test_missing_database_uses_defaults_without_creating_data_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp) / "missing"
            defaults = [(None, source("default"))]
            with patch.dict(os.environ, {"NEWSROOM_DATA_DIR": str(data_dir)}), \
                    patch.object(diagnostics, "load_default_sources", return_value=defaults):
                self.assertEqual(diagnostics.load_sources(), [defaults[0][1]])
            self.assertFalse(data_dir.exists())

    def test_existing_database_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            database_path = Path(temp) / "newsroom.sqlite3"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute("CREATE TABLE sources (id, name, kind, url, category, enabled, steam_appid)")
                connection.execute("INSERT INTO sources VALUES (1,'configured','rss','https://example.com/feed','sports',1,NULL)")
                connection.commit()
            finally:
                connection.close()
            original = database_path.read_bytes()

            with patch.dict(os.environ, {"NEWSROOM_DATA_DIR": temp}):
                loaded = diagnostics.load_sources()

            self.assertEqual(loaded[0]["name"], "configured")
            self.assertEqual(database_path.read_bytes(), original)

    def test_category_and_include_disabled_filter_sources(self):
        sources = [source("sports-on"), source("games-on", "games"), source("sports-off", enabled=False)]
        observed = []

        async def check(selected):
            observed.append([item["name"] for item in selected])
            return [{"source": item, "ok": True, "count": 1, "error": ""} for item in selected]

        with patch.object(diagnostics, "load_sources", return_value=sources), \
                patch.object(diagnostics, "fetch_config", return_value=("direct", None)), \
                patch.object(diagnostics, "_check_sources", side_effect=check):
            with contextlib.redirect_stdout(io.StringIO()):
                status = diagnostics.main(["--category", "sports"])
            self.assertEqual(status, 0)
            with contextlib.redirect_stdout(io.StringIO()):
                status = diagnostics.main(["--category", "sports", "--include-disabled"])

        self.assertEqual(status, 0)
        self.assertEqual(observed, [["sports-on"], ["sports-on", "sports-off"]])

    def test_network_failure_returns_nonzero_without_printing_proxy_credentials(self):
        proxy_secret = "user:do-not-print@proxy.example"
        with patch.object(diagnostics, "load_sources", return_value=[source("sports-on")]), \
                patch.object(diagnostics, "fetch_config", return_value=("proxy", f"http://{proxy_secret}:8080")), \
                patch.object(diagnostics, "fetch_public", new_callable=AsyncMock,
                             side_effect=httpx.ConnectError(f"failed via {proxy_secret}")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = diagnostics.main(["--category", "sports"])

        self.assertEqual(status, 1)
        self.assertNotIn(proxy_secret, output.getvalue())
        self.assertIn("失败", output.getvalue())

    def test_direct_mode_reports_configured_proxy_and_accepts_empty_rss(self):
        proxy_secret = "user:do-not-print@proxy.example:8080"
        with patch.dict(os.environ, {"NEWSROOM_OUTBOUND_PROXY": f"http://{proxy_secret}"}), \
                patch.object(diagnostics, "load_sources", return_value=[source("sports-on")]), \
                patch.object(diagnostics, "fetch_config", return_value=("direct", None)), \
                patch.object(diagnostics, "fetch_public", new_callable=AsyncMock,
                             return_value=(b"<rss><channel/></rss>", "https://example.com/feed")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = diagnostics.main(["--category", "sports"])

        rendered = output.getvalue()
        self.assertEqual(status, 0)
        self.assertNotIn(proxy_secret, rendered)
        self.assertIn("已配置代理: 是", rendered)
        self.assertIn("当前模式使用代理: 否", rendered)
        self.assertIn("通过 count=0（连接有效，暂无条目）", rendered)
        self.assertIn("汇总: 通过 1，失败 0", rendered)


if __name__ == "__main__":
    unittest.main()
