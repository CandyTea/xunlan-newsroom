"""Offline business, security, parsing, persistence, and scheduling checks."""
import asyncio
import json
import os
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from app.collector import Collector, canonical_url, fetch_public, matches_watch, parse_rss, parse_steam, resolve_public_url
from app.db import Database
from app.main import create_app
from app.models import Source, public_url_syntax
from app.scheduler import due_slots, next_run, slots_between

RSS = b'''<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Nintendo studio announces a game</title><link>https://example.com/news/1?utm_source=feed</link>
<description>&lt;p&gt;Nintendo game context&lt;/p&gt;&lt;script&gt;evil()&lt;/script&gt;</description>
<pubDate>Thu, 08 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Independent sports story</title><link>https://example.com/news/2</link><description>Team wins</description></item>
</channel></rss>'''
ATOM = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Real fixture title</title>
<link rel="self" href="https://example.com/api"/><link rel="alternate" href="/story"/>
<summary type="html">&lt;p&gt;Plain &amp;amp; useful&lt;/p&gt;</summary><updated>2026-10-08T12:00:00+08:00</updated>
</entry></feed>'''


def empty_db(directory):
    db = Database(Path(directory) / "test.sqlite3")
    db.initialize()
    with db.connection(write=True) as conn:
        conn.execute("DELETE FROM sources")
    return db


def add_source(db, name="Fixture", category="games"):
    with db.connection(write=True) as conn:
        source_id = conn.execute("INSERT INTO sources(name,kind,url,category,enabled) VALUES(?,'rss',?,?,1)",
                                 (name, "https://example.com/feed", category)).lastrowid
        return dict(conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())


def add_schedule(db, time="09:00", days=None):
    with db.connection(write=True) as conn:
        return conn.execute("INSERT INTO schedules(name,time,days,categories,enabled,created_at) VALUES(?,?,?,?,1,?)",
                            ("Morning", time, json.dumps(days if days is not None else list(range(7))), '["games"]', "2026-10-01T00:00:00+00:00")).lastrowid


class ParsingTests(unittest.TestCase):
    def test_rss_strips_html_and_preserves_dates(self):
        items = parse_rss(RSS, "https://example.com/feed")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["summary"], "Nintendo game context")
        self.assertEqual(items[0]["published_at"], "2026-10-08T10:00:00+00:00")
        self.assertIsNone(items[1]["published_at"])

    def test_atom_relative_link_and_timezone(self):
        item = parse_rss(ATOM, "https://example.com/feed")[0]
        self.assertEqual(item["url"], "https://example.com/story")
        self.assertEqual(item["summary"], "Plain & useful")
        self.assertEqual(item["published_at"], "2026-10-08T04:00:00+00:00")

    def test_entities_rejected_even_utf16(self):
        xml = '<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel/></rss>'
        for encoding in ("utf-8", "utf-16"):
            with self.subTest(encoding=encoding), self.assertRaises(ValueError):
                parse_rss(xml.encode(encoding), "https://example.com/")

    def test_non_feed_rejected(self):
        with self.assertRaises(ValueError):
            parse_rss(b"<html>Not an RSS feed</html>", "https://example.com/")

    def test_canonical_tracking_dedup(self):
        self.assertEqual(canonical_url("https://EXAMPLE.com:443/news?a=2&utm_campaign=x#top"),
                         canonical_url("https://example.com/news?fbclid=tracking&a=2"))

    def test_steam_validates_appid(self):
        payload = {"appnews": {"appid": 570, "newsitems": [{"title": "Update", "url": "https://store.steampowered.com/news/app/570/view/1", "contents": "[b]Patch[/b]", "date": 1700000000}]}}
        data = json.dumps(payload).encode()
        self.assertEqual(parse_steam(data, 570)[0]["summary"], "Patch")
        with self.assertRaises(ValueError):
            parse_steam(data, 730)

    def test_watch_boundaries_context_and_exclusion(self):
        watch = {"enabled": True, "name": "EA", "aliases": ["艺电"], "ticker": "", "keywords": ["game"], "exclude_keywords": ["rumor"]}
        self.assertFalse(matches_watch({"title": "TEAM game", "summary": ""}, watch))
        self.assertTrue(matches_watch({"title": "EA new game", "summary": ""}, watch))
        self.assertFalse(matches_watch({"title": "EA new game rumor", "summary": ""}, watch))
        self.assertFalse(matches_watch({"title": "EA earnings", "summary": ""}, watch))
        self.assertTrue(matches_watch({"title": "艺电发布 game", "summary": ""}, watch))


class URLTests(unittest.IsolatedAsyncioTestCase):
    def test_private_and_credential_urls_rejected(self):
        for url in ("http://127.0.0.1/feed", "http://10.0.0.1/", "http://169.254.169.254/", "http://[::1]/", "http://user:secret@example.com/", "file:///etc/passwd", "http://localhost/", "https://example.com:8080/", "http://example.local/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_url_syntax(url)

    async def test_mixed_public_private_dns_rejected(self):
        loop = asyncio.get_running_loop()
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=answers)):
            with self.assertRaises(ValueError):
                await resolve_public_url("https://example.com/feed")

    async def test_public_ip_is_pinned_with_original_identity(self):
        loop = asyncio.get_running_loop()
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=answers)):
            original, pinned, host, hostname = await resolve_public_url("https://example.com/feed")
        self.assertEqual(pinned, "https://1.1.1.1:443/feed")
        self.assertEqual(host, "example.com")
        self.assertEqual(hostname, "example.com")

    async def test_redirect_to_private_is_not_requested(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})
        real_client = httpx.AsyncClient
        def client_factory(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            return real_client(transport=httpx.MockTransport(handler), trust_env=False)
        resolver = AsyncMock(side_effect=[("https://example.com/feed", "https://1.1.1.1/feed", "example.com", "example.com"), ValueError("private")])
        with patch("app.collector.resolve_public_url", resolver), patch("app.collector.httpx.AsyncClient", client_factory):
            with self.assertRaises(ValueError):
                await fetch_public("https://example.com/feed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].headers["host"], "example.com")
        self.assertEqual(calls[0].extensions["sni_hostname"], "example.com")

    async def test_download_size_cap(self):
        def handler(request):
            return httpx.Response(200, headers={"content-length": "3000000"}, content=b"large")
        real_client = httpx.AsyncClient
        resolver = AsyncMock(return_value=("https://example.com/feed", "https://1.1.1.1/feed", "example.com", "example.com"))
        with patch("app.collector.resolve_public_url", resolver), patch("app.collector.httpx.AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler))):
            with self.assertRaisesRegex(ValueError, "2 MB"):
                await fetch_public("https://example.com/feed")


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = empty_db(self.temp.name)
        self.source = add_source(self.db)

    def tearDown(self):
        self.temp.cleanup()

    async def test_overlapping_runs_dedup_and_article_state_survives(self):
        gate = asyncio.Event()
        async def fetcher(url):
            await gate.wait()
            return RSS, url
        collector = Collector(self.db, fetcher)
        results = await asyncio.gather(collector.start(["games"]), collector.start(["games"]))
        self.assertEqual(sum(accepted for accepted, _ in results), 1)
        gate.set()
        await collector.task
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE articles SET saved=1,read=1 WHERE id=1")
        await collector.start(["games"])
        await collector.task
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 2)
            self.assertEqual(tuple(conn.execute("SELECT saved,read FROM articles WHERE id=1").fetchone()), (1, 1))
            self.assertEqual(conn.execute("SELECT new_count FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0], 0)

    async def test_partial_failure_preserves_success_and_status(self):
        add_source(self.db, "Broken")
        count = 0
        async def fetcher(url):
            nonlocal count
            count += 1
            if count == 2:
                raise ValueError("invalid feed")
            return RSS, url
        collector = Collector(self.db, fetcher)
        await collector.start(["games"])
        await collector.task
        with self.db.connection() as conn:
            run = conn.execute("SELECT * FROM runs").fetchone()
            self.assertEqual((run["status"], run["new_count"], run["source_count"]), ("partial", 2, 2))
            self.assertIn("Broken", run["error"])
            self.assertIsNotNone(conn.execute("SELECT last_error FROM sources WHERE name='Broken'").fetchone()[0])

    async def test_schedule_claim_persists_after_restart(self):
        schedule_id = add_schedule(self.db)
        now = datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc)
        slots = due_slots(self.db, now, now, startup=True)
        self.assertEqual(len(slots), 1)
        collector = Collector(self.db, AsyncMock(return_value=(RSS, "https://example.com/feed")))
        self.assertTrue((await collector.start([], "scheduled", slots))[0])
        await collector.task
        self.db.initialize()
        self.assertEqual(due_slots(self.db, now, now, startup=True), [])
        self.assertFalse((await collector.start([], "scheduled", slots))[0])
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM schedule_slots WHERE schedule_id=?", (schedule_id,)).fetchone()[0], 1)

    async def test_deleted_or_disabled_due_schedule_is_not_claimed(self):
        schedule_id = add_schedule(self.db)
        slot = {"id": schedule_id, "scheduled_at": "2026-10-09T01:00:00+00:00", "categories": ["games"]}
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE schedules SET enabled=0 WHERE id=?", (schedule_id,))
        collector = Collector(self.db)
        self.assertFalse((await collector.start([], "scheduled", [slot]))[0])

    async def test_restart_marks_inflight_run_interrupted(self):
        with self.db.connection(write=True) as conn:
            conn.execute("INSERT INTO runs(trigger,started_at,status) VALUES('manual','2026-10-08T00:00:00+00:00','running')")
        self.db.initialize()
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("SELECT status FROM runs").fetchone()[0], "interrupted")


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = empty_db(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_weekday_timezone_and_next_time(self):
        schedule = {"enabled": True, "time": "09:00", "days": [4]}
        start = datetime(2026, 10, 8, 23, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(slots_between(schedule, start, end, "Asia/Shanghai"), [datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc)])
        self.assertEqual(next_run(schedule, end, "Asia/Shanghai"), "2026-10-16T01:00:00+00:00")

    def test_catchup_disabled_and_window(self):
        add_schedule(self.db)
        now = datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc)
        self.assertEqual(due_slots(self.db, now, now, startup=True), [])
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps({"timezone": "Asia/Shanghai", "catch_up": False, "catch_up_hours": 4}),))
        now = datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(due_slots(self.db, now, now, startup=True), [])
        self.assertEqual(len(due_slots(self.db, datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc), now)), 1)

    def test_coalesces_multiple_missed_days_to_latest_slot(self):
        add_schedule(self.db)
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps({"timezone": "Asia/Shanghai", "catch_up": True, "catch_up_hours": 72}),))
        now = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)
        slots = due_slots(self.db, now, now, startup=True)
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["scheduled_at"], "2026-10-12T01:00:00+00:00")

    def test_dst_nonexistent_time_and_fold_are_not_duplicated(self):
        schedule = {"enabled": True, "time": "02:30", "days": [6]}
        start = datetime(2026, 3, 8, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 9, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(slots_between(schedule, start, end, "America/New_York"), [])
        schedule["time"] = "01:30"
        start = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(len(slots_between(schedule, start, end, "America/New_York")), 1)


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"SETUP_TOKEN": "", "COOKIE_SECURE": "0"}):
            self.app = create_app(self.temp.name, start_scheduler=False)
        self.db = self.app.state.db
        self.client = TestClient(self.app)
        self.client.__enter__()
        result = self.client.post("/api/setup", json={"username": "reader", "password": "test-password-123"})
        self.assertEqual(result.status_code, 200)
        self.headers = {"X-CSRF-Token": result.json()["csrf_token"]}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def test_csrf_origin_and_cookie_security(self):
        self.assertIn("HttpOnly", self.client.post("/api/login", json={"username": "reader", "password": "test-password-123"}).headers["set-cookie"])
        self.assertEqual(self.client.post("/api/logout", json={}).status_code, 403)
        token = self.client.get("/api/session").json()["csrf_token"]
        self.assertEqual(self.client.post("/api/logout", json={}, headers={"X-CSRF-Token": token, "Origin": "https://attacker.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/logout", json={}, headers={"X-CSRF-Token": token}).status_code, 200)
        self.assertEqual(self.client.get("/api/articles").status_code, 401)

    def test_watch_rematch_article_filters_and_delete_integrity(self):
        source = self.client.get("/api/sources").json()["items"][0]
        self.app.state.collector.persist_articles(source, parse_rss(RSS, "https://example.com/feed"))
        watch = {"type": "studio", "name": "Nintendo", "aliases": [], "keywords": [], "exclude_keywords": [], "enabled": True, "market": "", "ticker": ""}
        response = self.client.post("/api/watches", json=watch, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        watch_id = response.json()["id"]
        result = self.client.get(f"/api/articles?following=true&watch_id={watch_id}").json()
        self.assertEqual(result["total"], 1)
        article_id = result["items"][0]["id"]
        result = self.client.patch(f"/api/articles/{article_id}", json={"saved": True, "read": True}, headers=self.headers)
        self.assertTrue(result.json()["saved"])
        self.assertEqual(self.client.get("/api/articles?saved=true&unread=true").json()["total"], 0)
        watch["exclude_keywords"] = ["game"]
        self.client.put(f"/api/watches/{watch_id}", json=watch, headers=self.headers)
        self.assertEqual(self.client.get("/api/articles?following=true").json()["total"], 0)
        self.client.delete(f"/api/watches/{watch_id}", headers=self.headers)
        self.client.delete(f"/api/sources/{source['id']}", headers=self.headers)
        self.assertEqual(self.client.get("/api/articles").json()["total"], 2)
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_schedule_settings_persistence_and_secret_free_export(self):
        self.assertEqual(self.client.get("/api/schedules").json(), {"items": []})
        schedule = {"name": "Morning", "time": "08:30", "days": [0, 1, 2, 3, 4], "categories": ["games", "stocks"], "enabled": True}
        result = self.client.post("/api/schedules", json=schedule, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertIsNotNone(result.json()["next_run_at"])
        settings = {"timezone": "UTC", "catch_up": False, "catch_up_hours": 2}
        self.assertEqual(self.client.put("/api/settings", json=settings, headers=self.headers).json(), settings)
        fresh = create_app(self.temp.name, start_scheduler=False)
        self.assertEqual(fresh.state.db.settings(), settings)
        exported = self.client.get("/api/export").json()
        self.assertEqual(exported["settings"], settings)
        self.assertNotIn("password", json.dumps(exported))
        self.assertNotIn("csrf", json.dumps(exported))

    def test_source_validation_and_steam_official_endpoint(self):
        body = {"name": "Private", "kind": "rss", "url": "http://127.0.0.1/", "category": "games", "enabled": True}
        self.assertEqual(self.client.post("/api/sources", json=body, headers=self.headers).status_code, 422)
        body.update({"name": "Steam", "kind": "steam", "steam_appid": 570, "url": "https://attacker.example/"})
        with patch("app.main.resolve_public_url", AsyncMock(return_value=("", "", "", ""))):
            result = self.client.post("/api/sources", json=body, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["url"].startswith("https://api.steampowered.com/"))

    def test_no_initial_articles_and_input_limits(self):
        self.assertEqual(self.client.get("/api/articles").json()["items"], [])
        self.assertEqual(self.client.get("/api/watches").json()["items"], [])
        self.assertEqual(self.client.get("/api/articles?page_size=101").status_code, 422)
        self.assertEqual(self.client.get("/api/articles?category=invalid").status_code, 422)
        self.assertEqual(self.client.put("/api/settings", json={"timezone": "Bad/Timezone"}, headers=self.headers).status_code, 422)
        self.assertEqual(self.client.post("/api/login", content=b"x" * 70000).status_code, 413)

    def test_first_setup_token_and_single_account(self):
        self.assertEqual(self.client.post("/api/setup", json={"username": "other", "password": "test-password-123"}).status_code, 409)
        with tempfile.TemporaryDirectory() as second, patch.dict(os.environ, {"SETUP_TOKEN": "only-this-token"}):
            secured = create_app(second, start_scheduler=False)
            with TestClient(secured) as client:
                self.assertTrue(client.get("/api/session").json()["setup_token_required"])
                credentials = {"username": "reader", "password": "test-password-123"}
                self.assertEqual(client.post("/api/setup", json=credentials).status_code, 403)
                credentials["setup_token"] = "only-this-token"
                self.assertEqual(client.post("/api/setup", json=credentials).status_code, 200)


if __name__ == "__main__":
    unittest.main()
