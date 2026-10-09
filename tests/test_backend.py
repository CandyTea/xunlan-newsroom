"""Offline business, security, parsing, persistence, and scheduling checks."""
import asyncio
import json
import os
import socket
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from app.catalog import article_topics, load_catalog
from app.collector import Collector, MAX_BYTES, canonical_url, fetch_config, fetch_public, matches_watch, parse_rss, parse_steam, rematch_watch, resolve_public_url, safe_error
from app.db import Database, load_default_sources
from app.main import ADMIN_COOKIE_NAME, COOKIE_NAME, create_app, password_hash, token_hash
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
    def test_missing_catalog_fails_clearly(self):
        with patch("app.catalog.Path.read_text", side_effect=FileNotFoundError("interest_catalog.json")):
            with self.assertRaises(FileNotFoundError):
                load_catalog()

    def test_catalog_topics_are_sports_only_with_english_boundaries(self):
        catalog = load_catalog()
        self.assertEqual(article_topics({"title": "Golden State Warriors win", "summary": "", "category": "sports"}, catalog),
                         {"team:golden-state-warriors", "league:nba"})
        self.assertEqual(article_topics({"title": "Golden State Warriors movie", "summary": "", "category": "games"}, catalog), set())
        self.assertEqual(article_topics({"title": "NBA2K update and EPLANE", "summary": "", "category": "sports"}, catalog), set())

    def test_team_short_names_are_explicit_and_have_word_boundaries(self):
        catalog = load_catalog()
        for title, team_id, league_id in [("WARRIORS win", "golden-state-warriors", "nba"),
                                          ("Lakers trade update", "los-angeles-lakers", "nba"),
                                          ("Barça win", "fc-barcelona", "la-liga")]:
            with self.subTest(title=title):
                self.assertEqual(article_topics({"title": title, "summary": "", "category": "sports"}, catalog),
                                 {"team:" + team_id, "league:" + league_id})
        for title in ("Warriors2 update", "Spurs prepare for a fixture", "Heat and thunder delay a match",
                      "City and United meet", "Fishing nets and rockets"):
            with self.subTest(title=title):
                self.assertEqual(article_topics({"title": title, "summary": "", "category": "sports"}, catalog), set())

    def test_team_watch_expands_exact_names_and_preserves_constraints(self):
        catalog = load_catalog()
        watch = {"type": "team", "enabled": True, "name": "勇士", "aliases": [], "ticker": "",
                 "league_id": "nba", "keywords": ["playoffs"], "exclude_keywords": ["rumor"]}
        article = {"title": "Warriors win", "summary": "playoffs", "category": "sports"}
        self.assertTrue(matches_watch(article, watch, catalog))
        self.assertTrue(matches_watch(article, {**watch, "name": "Favorites", "aliases": ["勇士"]}, catalog))
        self.assertTrue(matches_watch(article, {**watch, "league_id": ""}, catalog))
        self.assertFalse(matches_watch({**article, "summary": "regular season"}, watch, catalog))
        self.assertFalse(matches_watch({**article, "summary": "playoffs rumor"}, watch, catalog))
        self.assertFalse(matches_watch(article, {**watch, "enabled": False}, catalog))
        self.assertFalse(matches_watch({**article, "category": "games"}, watch, catalog))
        self.assertFalse(matches_watch({**article, "category": "games"}, {**watch, "league_id": ""}, catalog))
        self.assertFalse(matches_watch(article, {**watch, "league_id": "premier-league"}, catalog))
        self.assertFalse(matches_watch(article, {**watch, "type": "company", "league_id": ""}, catalog))
        self.assertFalse(matches_watch(article, {**watch, "name": "勇士球迷"}, catalog))
        self.assertFalse(matches_watch(article, {**watch, "name": "NBA", "keywords": []}, catalog))
        self.assertFalse(matches_watch(article, {**watch, "name": "Favorites", "aliases": [], "keywords": ["勇士"]}, catalog))
        self.assertTrue(matches_watch({**article, "title": "Harbour Falcons win"},
                                      {**watch, "name": "Harbour Falcons"}, catalog))

    def test_ambiguous_catalog_alias_does_not_expand_without_a_league(self):
        catalog = load_catalog()
        catalog["teams"].append({"id": "other-warriors", "name": "Other Warriors", "aliases": ["Warriors"],
                                 "league_id": "premier-league"})
        watch = {"type": "team", "enabled": True, "name": " warriors ", "aliases": [], "ticker": "",
                 "league_id": "", "keywords": [], "exclude_keywords": []}
        article = {"title": "金州勇士赢球", "summary": "", "category": "sports"}
        self.assertFalse(matches_watch(article, watch, catalog))
        self.assertTrue(matches_watch(article, {**watch, "league_id": "nba"}, catalog))

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
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"NEWSROOM_FETCH_MODE": "direct", "NEWSROOM_OUTBOUND_PROXY": "", "NEWSROOM_DNS_MODE": "system"}))
        self.resolver = AsyncMock(return_value=("https://example.com/feed", "https://1.1.1.1:443/feed", "example.com", "example.com"))

    def client_factory(self, handler, routes):
        real_client = httpx.AsyncClient
        def factory(**kwargs):
            routes.append(kwargs["proxy"])
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            self.assertEqual(kwargs["timeout"].connect, 10)
            self.assertEqual(kwargs["timeout"].read, 20)
            return real_client(transport=httpx.MockTransport(handler), trust_env=False)
        return factory

    def test_private_and_credential_urls_rejected(self):
        for url in ("http://127.0.0.1/feed", "http://10.0.0.1/", "http://169.254.169.254/", "http://[::1]/", "http://user:secret@example.com/", "file:///etc/passwd", "http://localhost/", "https://example.com:8080/", "http://example.local/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_url_syntax(url)

    async def test_mixed_public_private_dns_rejected(self):
        loop = asyncio.get_running_loop()
        for family, private in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
                       (family, socket.SOCK_STREAM, 6, "", (private, 443))]
            with self.subTest(private=private), patch.object(loop, "getaddrinfo", AsyncMock(return_value=answers)):
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

    async def test_ipv6_only_dns_is_pinned_and_rotated(self):
        loop = asyncio.get_running_loop()
        addresses = ("2606:4700:4700::1111", "2606:4700:4700::1001")
        answers = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 443, 0, 0)) for address in addresses]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=answers)):
            original, pinned, host, hostname = await resolve_public_url("https://example.com/feed", ip_index=1)
        self.assertEqual(pinned, "https://[2606:4700:4700::1001]:443/feed")
        self.assertEqual((original, host, hostname), ("https://example.com/feed", "example.com", "example.com"))

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
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, headers={"content-length": "3000000"}, content=b"large")
        real_client = httpx.AsyncClient
        resolver = AsyncMock(return_value=("https://example.com/feed", "https://1.1.1.1/feed", "example.com", "example.com"))
        with patch("app.collector.resolve_public_url", resolver), patch("app.collector.httpx.AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler))):
            with self.assertRaisesRegex(ValueError, "2 MB"):
                await fetch_public("https://example.com/feed")
        self.assertEqual(len(calls), 1)

    async def test_network_failures_retry_and_recover(self):
        for error_type in (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError, httpx.ProxyError):
            with self.subTest(error_type=error_type):
                requests, routes = [], []
                def handler(request):
                    requests.append(request)
                    if len(requests) == 1:
                        raise error_type("temporary failure", request=request)
                    return httpx.Response(200, content=RSS)
                factory = self.client_factory(handler, routes)
                with patch("app.collector.resolve_public_url", self.resolver), patch("app.collector.httpx.AsyncClient", factory), \
                     patch("app.collector.asyncio.sleep", AsyncMock()) as sleep:
                    data, original = await fetch_public("https://example.com/feed")
                self.assertEqual((data, original), (RSS, "https://example.com/feed"))
                self.assertEqual(len(requests), 2)
                self.assertEqual(routes, [None, None])
                sleep.assert_awaited_once_with(0.5)

    async def test_auto_falls_back_to_direct_and_rotates_each_routes_ips(self):
        requests, routes = [], []
        proxy = "http://user:proxy-secret@proxy.example:8080"
        def handler(request):
            requests.append(request)
            if len(requests) < 4:
                raise httpx.ConnectError("temporary failure", request=request)
            return httpx.Response(200, content=RSS)
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in ("1.1.1.1", "8.8.8.8")]
        loop = asyncio.get_running_loop()
        with patch.dict(os.environ, {"NEWSROOM_FETCH_MODE": "auto", "NEWSROOM_OUTBOUND_PROXY": proxy}), \
             patch.object(loop, "getaddrinfo", AsyncMock(return_value=answers)) as dns, \
             patch("app.collector.httpx.AsyncClient", self.client_factory(handler, routes)), \
             patch("app.collector.asyncio.sleep", AsyncMock()):
            data, original = await fetch_public("https://example.com/feed")
        self.assertEqual((data, original), (RSS, "https://example.com/feed"))
        self.assertEqual(routes, [proxy, None, proxy, None])
        self.assertEqual([request.url.host for request in requests], ["1.1.1.1", "1.1.1.1", "8.8.8.8", "8.8.8.8"])
        self.assertEqual(dns.await_count, 4)
        for request in requests:
            self.assertEqual(request.headers["host"], "example.com")
            self.assertEqual(request.extensions["sni_hostname"], "example.com")
            info = {"server_hostname": request.url.host}
            await request.extensions["trace"]("proxy.start_tls.started", info)
            self.assertEqual(info["server_hostname"], "example.com")

    async def test_proxy_mode_never_falls_back_to_direct(self):
        requests, routes = [], []
        proxy = "http://proxy.example:8080"
        def handler(request):
            requests.append(request)
            raise httpx.ProxyError("cannot connect through proxy", request=request)
        with patch.dict(os.environ, {"NEWSROOM_FETCH_MODE": "proxy", "NEWSROOM_OUTBOUND_PROXY": proxy}), \
             patch("app.collector.resolve_public_url", self.resolver), \
             patch("app.collector.httpx.AsyncClient", self.client_factory(handler, routes)), \
             patch("app.collector.asyncio.sleep", AsyncMock()) as sleep:
            with self.assertRaises(httpx.ProxyError):
                await fetch_public("https://example.com/feed")
        self.assertEqual(routes, [proxy] * 4)
        self.assertEqual(len(requests), 4)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [0.5, 1, 2])

    async def test_direct_and_auto_without_explicit_proxy_ignore_environment_proxies(self):
        for mode in ("direct", "auto"):
            with self.subTest(mode=mode):
                routes = []
                def handler(request):
                    return httpx.Response(200, content=RSS)
                with patch.dict(os.environ, {"NEWSROOM_FETCH_MODE": mode, "NEWSROOM_OUTBOUND_PROXY": "invalid" if mode == "direct" else "",
                                             "HTTP_PROXY": "http://ambient.invalid", "HTTPS_PROXY": "http://ambient.invalid"}), \
                     patch("app.collector.resolve_public_url", self.resolver), \
                     patch("app.collector.httpx.AsyncClient", self.client_factory(handler, routes)):
                    await fetch_public("https://example.com/feed")
                self.assertEqual(routes, [None])

    async def test_direct_retry_rotates_ipv4_without_switching_to_ipv6(self):
        requests, routes = [], []
        def handler(request):
            requests.append(request)
            if len(requests) < 3:
                raise httpx.ConnectError("unreachable", request=request)
            return httpx.Response(200, content=RSS)
        answers = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443, 0, 0)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=answers)), \
             patch("app.collector.httpx.AsyncClient", self.client_factory(handler, routes)), \
             patch("app.collector.asyncio.sleep", AsyncMock()):
            await fetch_public("https://example.com/feed")
        self.assertEqual([request.url.host for request in requests], ["1.1.1.1", "8.8.8.8", "1.1.1.1"])

    async def test_dns_is_revalidated_before_retry_and_mixed_answers_stop(self):
        requests, routes = [], []
        def handler(request):
            requests.append(request)
            raise httpx.ConnectError("unreachable", request=request)
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        mixed = [*public, (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(side_effect=[public, mixed])) as dns, \
             patch("app.collector.httpx.AsyncClient", self.client_factory(handler, routes)), \
             patch("app.collector.asyncio.sleep", AsyncMock()) as sleep:
            with self.assertRaisesRegex(ValueError, "非公网"):
                await fetch_public("https://example.com/feed")
        self.assertEqual(len(requests), 1)
        self.assertEqual(dns.await_count, 2)
        sleep.assert_awaited_once()

    async def test_temporary_http_errors_respect_retry_after(self):
        cases = [(429, "3", 3), (502, None, 0.5), (503, "invalid", 0.5), (504, "0", 0.5)]
        for status, retry_after, expected in cases:
            with self.subTest(status=status):
                requests, routes = [], []
                def handler(request):
                    requests.append(request)
                    if len(requests) == 1:
                        return httpx.Response(status, headers={"retry-after": retry_after} if retry_after else {})
                    return httpx.Response(200, content=RSS)
                with patch("app.collector.resolve_public_url", self.resolver), \
                     patch("app.collector.httpx.AsyncClient", self.client_factory(handler, routes)), \
                     patch("app.collector.asyncio.sleep", AsyncMock()) as sleep:
                    await fetch_public("https://example.com/feed")
                self.assertEqual(len(requests), 2)
                sleep.assert_awaited_once_with(expected)

    async def test_retry_after_http_date_and_excessive_delay(self):
        routes = []
        retry_at = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10), usegmt=True)
        requests = []
        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(429, headers={"retry-after": retry_at})
            return httpx.Response(200, content=RSS)
        with patch("app.collector.resolve_public_url", self.resolver), \
             patch("app.collector.httpx.AsyncClient", self.client_factory(handler, routes)), \
             patch("app.collector.asyncio.sleep", AsyncMock()) as sleep:
            await fetch_public("https://example.com/feed")
        self.assertGreater(sleep.await_args.args[0], 8)
        self.assertLessEqual(sleep.await_args.args[0], 10)
        requests.clear()
        retry_at = "3600"
        with patch("app.collector.resolve_public_url", self.resolver), \
             patch("app.collector.httpx.AsyncClient", self.client_factory(handler, [])), \
             patch("app.collector.asyncio.sleep", AsyncMock()) as sleep:
            with self.assertRaises(httpx.HTTPStatusError):
                await fetch_public("https://example.com/feed")
        self.assertEqual(len(requests), 1)
        sleep.assert_not_awaited()

    async def test_permanent_http_errors_do_not_retry(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                routes = []
                def handler(request):
                    return httpx.Response(status)
                with patch("app.collector.resolve_public_url", self.resolver), \
                     patch("app.collector.httpx.AsyncClient", self.client_factory(handler, routes)), \
                     patch("app.collector.asyncio.sleep", AsyncMock()) as sleep:
                    with self.assertRaises(httpx.HTTPStatusError):
                        await fetch_public("https://example.com/feed")
                self.assertEqual(routes, [None])
                sleep.assert_not_awaited()

    async def test_streaming_size_cap_does_not_retry(self):
        class LargeStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"a" * MAX_BYTES
                yield b"overflow"
        routes = []
        def handler(request):
            return httpx.Response(200, stream=LargeStream())
        with patch("app.collector.resolve_public_url", self.resolver), \
             patch("app.collector.httpx.AsyncClient", self.client_factory(handler, routes)), \
             patch("app.collector.asyncio.sleep", AsyncMock()) as sleep:
            with self.assertRaisesRegex(ValueError, "2 MB"):
                await fetch_public("https://example.com/feed")
        self.assertEqual(routes, [None])
        sleep.assert_not_awaited()

    async def test_redirect_dns_is_validated_after_a_retry(self):
        requests, routes = [], []
        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                raise httpx.ConnectError("unreachable", request=request)
            return httpx.Response(302, headers={"location": "https://other.example/feed"})
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(side_effect=[public, public, private])) as dns, \
             patch("app.collector.httpx.AsyncClient", self.client_factory(handler, routes)), \
             patch("app.collector.asyncio.sleep", AsyncMock()) as sleep:
            with self.assertRaisesRegex(ValueError, "非公网"):
                await fetch_public("https://example.com/feed")
        self.assertEqual(len(requests), 2)
        self.assertEqual(dns.await_args_list[-1].args[0], "other.example")
        sleep.assert_awaited_once()

    async def test_redirect_limit_is_shared_across_retries(self):
        requests, routes = [], []
        def handler(request):
            requests.append(request)
            if len(requests) == 4:
                raise httpx.ConnectError("unreachable", request=request)
            return httpx.Response(302, headers={"location": "/next"})
        with patch("app.collector.resolve_public_url", self.resolver), \
             patch("app.collector.httpx.AsyncClient", self.client_factory(handler, routes)), \
             patch("app.collector.asyncio.sleep", AsyncMock()) as sleep:
            with self.assertRaisesRegex(ValueError, "跳转次数"):
                await fetch_public("https://example.com/feed")
        self.assertEqual(len(requests), 7)
        sleep.assert_awaited_once()

    async def test_attempt_timeout_recovers(self):
        routes = []
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.Event().wait()
            return httpx.Response(200, content=RSS)
        with patch("app.collector.resolve_public_url", self.resolver), \
             patch("app.collector.httpx.AsyncClient", self.client_factory(handler, routes)), \
             patch("app.collector.FETCH_ATTEMPT_TIMEOUT", 0.02), \
             patch("app.collector.asyncio.sleep", AsyncMock()):
            data, _ = await fetch_public("https://example.com/feed")
        self.assertEqual(data, RSS)
        self.assertEqual(calls, 2)

    async def test_total_budget_bounds_all_retries(self):
        routes = []
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            await asyncio.Event().wait()
        loop = asyncio.get_running_loop()
        started = loop.time()
        with patch("app.collector.resolve_public_url", self.resolver), \
             patch("app.collector.httpx.AsyncClient", self.client_factory(handler, routes)), \
             patch("app.collector.FETCH_ATTEMPT_TIMEOUT", 0.03), patch("app.collector.FETCH_BUDGET", 0.08), \
             patch("app.collector.retry_delay", return_value=0), \
             patch("app.collector.asyncio.sleep", AsyncMock()):
            with self.assertRaises(TimeoutError):
                await fetch_public("https://example.com/feed")
        self.assertLess(loop.time() - started, 0.4)
        self.assertGreaterEqual(calls, 2)
        self.assertLessEqual(calls, 3)

    async def test_cancellation_interrupts_request_without_retry(self):
        routes = []
        entered = asyncio.Event()
        async def handler(request):
            entered.set()
            await asyncio.Event().wait()
        with patch("app.collector.resolve_public_url", self.resolver), \
             patch("app.collector.httpx.AsyncClient", self.client_factory(handler, routes)), \
             patch("app.collector.asyncio.sleep", AsyncMock()) as sleep:
            task = asyncio.create_task(fetch_public("https://example.com/feed"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(routes, [None])
        sleep.assert_not_awaited()

    async def test_cancellation_interrupts_backoff(self):
        routes = []
        entered = asyncio.Event()
        def handler(request):
            raise httpx.ConnectError("unreachable", request=request)
        async def backoff(delay):
            entered.set()
            await asyncio.Event().wait()
        with patch("app.collector.resolve_public_url", self.resolver), \
             patch("app.collector.httpx.AsyncClient", self.client_factory(handler, routes)), \
             patch("app.collector.asyncio.sleep", backoff):
            task = asyncio.create_task(fetch_public("https://example.com/feed"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(routes, [None])

    async def test_dns_only_temporary_errors_retry(self):
        loop = asyncio.get_running_loop()
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        def handler(request):
            return httpx.Response(200, content=RSS)
        with patch.object(loop, "getaddrinfo", AsyncMock(side_effect=[socket.gaierror(socket.EAI_AGAIN, "temporary"), public])), \
             patch("app.collector.httpx.AsyncClient", self.client_factory(handler, [])), \
             patch("app.collector.asyncio.sleep", AsyncMock()) as sleep:
            await fetch_public("https://example.com/feed")
        sleep.assert_awaited_once()
        with patch.object(loop, "getaddrinfo", AsyncMock(side_effect=socket.gaierror(socket.EAI_NONAME, "unknown hostname"))) as dns, \
             patch("app.collector.httpx.AsyncClient", self.client_factory(handler, [])), \
             patch("app.collector.asyncio.sleep", AsyncMock()) as sleep:
            with self.assertRaises(socket.gaierror):
                await fetch_public("https://example.com/feed")
        self.assertEqual(dns.await_count, 1)
        sleep.assert_not_awaited()

    def test_proxy_config_and_errors_do_not_expose_credentials(self):
        secret = "credentials-must-stay-private"
        for proxy in (f"socks5://user:{secret}@proxy.example:1080", f"http://user:{secret}@proxy.example:70000"):
            with self.subTest(proxy=proxy), patch.dict(os.environ, {"NEWSROOM_FETCH_MODE": "proxy", "NEWSROOM_OUTBOUND_PROXY": proxy}):
                with self.assertRaises(ValueError) as error:
                    fetch_config()
                self.assertNotIn(secret, str(error.exception))
                self.assertNotIn("proxy.example", safe_error(error.exception))
        self.assertNotIn(secret, safe_error(httpx.ProxyError(f"proxy failed: http://user:{secret}@proxy.example")))

    def test_invalid_fetch_config_is_rejected_before_database_initialization(self):
        cases = [("unsupported", "", "NEWSROOM_FETCH_MODE"), ("proxy", "", "NEWSROOM_OUTBOUND_PROXY")]
        for mode, proxy, expected in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {"NEWSROOM_FETCH_MODE": mode, "NEWSROOM_OUTBOUND_PROXY": proxy}), \
                 patch("app.main.Database.initialize") as initialize:
                with self.assertRaisesRegex(ValueError, expected):
                    create_app(directory, start_scheduler=False)
                initialize.assert_not_called()
                self.assertFalse((Path(directory) / "newsroom.sqlite3").exists())


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = empty_db(self.temp.name)
        self.source = add_source(self.db)

    def tearDown(self):
        self.temp.cleanup()

    async def test_slow_source_does_not_block_fast_source_and_stop_cancels_workers(self):
        fast = add_source(self.db, "Fast")
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE sources SET url='https://example.com/fast' WHERE id=?", (fast["id"],))
        fast_done = asyncio.Event()
        slow_started = asyncio.Event()
        gate = asyncio.Event()
        cancelled = asyncio.Event()
        async def fetcher(url):
            if url.endswith("/fast"):
                fast_done.set()
                return RSS, url
            slow_started.set()
            try:
                await gate.wait()
            finally:
                cancelled.set()
        collector = Collector(self.db, fetcher)
        await collector.start(["games"])
        try:
            await asyncio.wait_for(fast_done.wait(), timeout=1)
            await asyncio.wait_for(slow_started.wait(), timeout=1)
            with self.db.connection() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 2)
        finally:
            await collector.stop()
        self.assertTrue(cancelled.is_set())
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("SELECT status FROM runs").fetchone()[0], "interrupted")

    async def test_parallel_collection_is_bounded_and_pending_disable_is_respected(self):
        sources = [self.source, *(add_source(self.db, f"Source {i}") for i in range(5))]
        gate = asyncio.Event()
        three_started = asyncio.Event()
        active = peak = calls = 0
        async def fetcher(url):
            nonlocal active, peak, calls
            active += 1
            calls += 1
            peak = max(peak, active)
            if active == 3:
                three_started.set()
            try:
                await gate.wait()
                return RSS, url
            finally:
                active -= 1
        collector = Collector(self.db, fetcher)
        await collector.start(["games"])
        try:
            await asyncio.wait_for(three_started.wait(), timeout=1)
            with self.db.connection(write=True) as conn:
                conn.execute("UPDATE sources SET enabled=0 WHERE id=?", (sources[-1]["id"],))
            gate.set()
            await collector.task
        finally:
            await collector.stop()
        self.assertEqual((peak, calls, active), (3, 5, 0))

    async def test_retry_failed_sources_skips_healthy_disabled_and_other_categories(self):
        healthy = add_source(self.db, "Healthy")
        disabled = add_source(self.db, "Disabled")
        sports = add_source(self.db, "Sports", "sports")
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE sources SET last_error='temporary' WHERE id IN (?,?,?)", (self.source["id"], disabled["id"], sports["id"]))
            conn.execute("UPDATE sources SET enabled=0 WHERE id=?", (disabled["id"],))
        fetcher = AsyncMock(return_value=(RSS, "https://example.com/feed"))
        collector = Collector(self.db, fetcher)
        await collector.start(["games"], failed_only=True)
        await collector.task
        fetcher.assert_awaited_once()
        with self.db.connection() as conn:
            self.assertIsNone(conn.execute("SELECT last_error FROM sources WHERE id=?", (self.source["id"],)).fetchone()[0])
            self.assertEqual(tuple(conn.execute("SELECT status,source_count,new_count FROM runs").fetchone()), ("success", 1, 2))
            self.assertIsNone(conn.execute("SELECT last_success_at FROM sources WHERE id=?", (healthy["id"],)).fetchone()[0])

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

    async def test_invalid_xml_is_not_fetched_again(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=b"<rss><channel>")
        real_client = httpx.AsyncClient
        resolver = AsyncMock(return_value=("https://example.com/feed", "https://1.1.1.1:443/feed", "example.com", "example.com"))
        collector = Collector(self.db)
        with patch.dict(os.environ, {"NEWSROOM_FETCH_MODE": "direct"}), \
             patch("app.collector.resolve_public_url", resolver), \
             patch("app.collector.httpx.AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), trust_env=False)):
            await collector.start(["games"])
            await collector.task
        self.assertEqual(len(requests), 1)
        with self.db.connection() as conn:
            run = conn.execute("SELECT status,error FROM runs").fetchone()
            self.assertEqual(run["status"], "failed")
            self.assertIn("来源 XML 格式无效", run["error"])

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

    def become_admin(self):
        result = self.client.post("/api/admin/login", json={"username": "reader", "password": "test-password-123"}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        return result

    def test_csrf_origin_and_cookie_security(self):
        self.assertIn("HttpOnly", self.client.post("/api/login", json={"username": "reader", "password": "test-password-123"}).headers["set-cookie"])
        self.assertEqual(self.client.post("/api/logout", json={}).status_code, 403)
        token = self.client.get("/api/session").json()["csrf_token"]
        self.assertEqual(self.client.post("/api/logout", json={}, headers={"X-CSRF-Token": token, "Origin": "https://attacker.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/logout", json={}, headers={"X-CSRF-Token": token}).status_code, 200)
        self.assertEqual(self.client.get("/api/articles").status_code, 401)

    def test_watch_rematch_article_filters_and_delete_integrity(self):
        self.become_admin()
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
        self.become_admin()
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
        self.become_admin()
        body = {"name": "Private", "kind": "rss", "url": "http://127.0.0.1/", "category": "games", "enabled": True}
        self.assertEqual(self.client.post("/api/sources", json=body, headers=self.headers).status_code, 422)
        body.update({"name": "Steam", "kind": "steam", "steam_appid": 570, "url": "https://attacker.example/"})
        with patch("app.main.resolve_public_url", AsyncMock(return_value=("", "", "", ""))):
            result = self.client.post("/api/sources", json=body, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["url"].startswith("https://api.steampowered.com/"))

    def test_no_initial_articles_and_input_limits(self):
        self.become_admin()
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

    def test_all_admin_apis_deny_readers_and_unauthenticated_clients(self):
        routes = [("GET", "/api/sources", None), ("POST", "/api/sources", {"name": "Feed", "url": "https://example.com/", "category": "games"}),
                  ("PUT", "/api/sources/1", {"name": "Feed", "url": "https://example.com/", "category": "games"}),
                  ("DELETE", "/api/sources/1", None), ("GET", "/api/settings", None), ("PUT", "/api/settings", {"timezone": "UTC"}),
                  ("GET", "/api/runs", None), ("GET", "/api/export", None), ("GET", "/api/network", None),
                  ("POST", "/api/fetch", {"failed_only": True})]
        anonymous = TestClient(self.app)
        for method, path, body in routes:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.client.request(method, path, json=body, headers=self.headers).status_code, 403)
                self.assertEqual(anonymous.request(method, path, json=body).status_code, 401)
        self.assertEqual(self.client.get("/api/admin/session").json(), {"authenticated": False, "expires_at": None})
        self.assertEqual(self.client.get("/api/preferences").json(), {"timezone": "Asia/Shanghai"})
        with self.db.connection(write=True) as conn:
            conn.execute("INSERT INTO runs(trigger,started_at,status,error) VALUES('manual','2026-01-01','failed','sensitive source error')")
        self.assertIsNone(self.client.get("/api/status").json()["last_error"])

    def test_admin_network_hides_credentials_and_retry_passes_selection(self):
        self.become_admin()
        with patch.dict(os.environ, {"NEWSROOM_FETCH_MODE": "proxy", "NEWSROOM_OUTBOUND_PROXY": "http://user:secret@proxy.example:8080", "NEWSROOM_DNS_MODE": "fallback"}):
            result = self.client.get("/api/network")
        self.assertEqual(result.json(), {"fetch_mode": "proxy", "dns_mode": "fallback", "proxy_configured": True})
        self.assertNotIn("secret", result.text)
        with patch.object(self.app.state.collector, "start", AsyncMock(return_value=(True, 123))) as start:
            response = self.client.post("/api/fetch", json={"categories": ["sports"], "failed_only": True}, headers=self.headers)
        self.assertEqual(response.json()["run_id"], 123)
        start.assert_awaited_once_with(["sports"], failed_only=True)

    def test_admin_requires_credentials_again_and_csrf(self):
        credentials = {"username": "reader", "password": "test-password-123"}
        self.assertEqual(self.client.post("/api/admin/login", json=credentials).status_code, 403)
        self.assertEqual(self.client.post("/api/admin/login", json=credentials, headers={**self.headers, "Origin": "https://attacker.example"}).status_code, 403)
        credentials["password"] = "incorrect-password"
        self.assertEqual(self.client.post("/api/admin/login", json=credentials, headers=self.headers).status_code, 403)
        self.assertTrue(self.client.get("/api/session").json()["authenticated"])
        result = self.become_admin()
        cookie = result.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Path=/api", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertIn("Max-Age=1800", cookie)
        self.assertTrue(self.client.get("/api/admin/session").json()["authenticated"])
        self.assertEqual(self.client.get("/api/settings").status_code, 200)
        self.assertEqual(self.client.put("/api/settings", json={"timezone": "UTC"}).status_code, 403)
        self.assertEqual(self.client.post("/api/admin/logout", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/admin/logout", json={}, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/settings").status_code, 403)
        self.assertTrue(self.client.get("/api/session").json()["authenticated"])
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM admin_grants").fetchone()[0], 0)

    def test_admin_expiry_session_binding_rotation_and_reader_logout(self):
        self.become_admin()
        admin_token = self.client.cookies.get(ADMIN_COOKIE_NAME)
        old_reader_token = self.client.cookies.get(COOKIE_NAME)
        second = TestClient(self.app)
        second.post("/api/login", json={"username": "reader", "password": "test-password-123"})
        second.cookies.set(ADMIN_COOKIE_NAME, admin_token, domain="testserver.local", path="/api")
        self.assertFalse(second.get("/api/admin/session").json()["authenticated"])
        self.assertEqual(second.get("/api/sources").status_code, 403)
        self.assertTrue(self.client.get("/api/admin/session").json()["authenticated"])
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE admin_grants SET expires_at='2000-01-01T00:00:00+00:00'")
        self.assertFalse(self.client.get("/api/admin/session").json()["authenticated"])
        self.assertEqual(self.client.get("/api/export").status_code, 403)
        self.become_admin()
        old_admin_token = self.client.cookies.get(ADMIN_COOKIE_NAME)
        result = self.client.post("/api/login", json={"username": "reader", "password": "test-password-123"})
        self.headers = {"X-CSRF-Token": result.json()["csrf_token"]}
        self.assertFalse(self.client.get("/api/admin/session").json()["authenticated"])
        with self.db.connection() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM sessions WHERE token_hash=?", (token_hash(old_reader_token),)).fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM admin_grants").fetchone()[0], 0)
        revoked = TestClient(self.app)
        revoked.cookies.set(COOKIE_NAME, old_reader_token, domain="testserver.local", path="/")
        revoked.cookies.set(ADMIN_COOKIE_NAME, old_admin_token, domain="testserver.local", path="/api")
        self.assertEqual(revoked.get("/api/admin/session").status_code, 401)
        self.become_admin()
        self.client.post("/api/logout", json={}, headers=self.headers)
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM admin_grants").fetchone()[0], 0)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_admin_reauthentication_rate_limit(self):
        with patch("app.main.password_valid", return_value=False):
            for _ in range(10):
                self.assertEqual(self.client.post("/api/admin/login", json={"username": "reader", "password": "wrong"}, headers=self.headers).status_code, 403)
            self.assertEqual(self.client.post("/api/admin/login", json={"username": "reader", "password": "wrong"}, headers=self.headers).status_code, 429)

    def test_expired_reader_session_cannot_use_unexpired_admin_grant(self):
        self.become_admin()
        with self.db.connection(write=True) as conn:
            conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(self.client.get("/api/admin/session").status_code, 401)
        self.assertEqual(self.client.get("/api/sources").status_code, 401)
        self.assertFalse(self.client.get("/api/session").json()["authenticated"])

    def test_admin_cookie_secure_matches_https_reader_cookie(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"COOKIE_SECURE": "1", "SETUP_TOKEN": ""}):
            secure_app = create_app(directory, start_scheduler=False)
            with TestClient(secure_app, base_url="https://testserver") as client:
                setup = client.post("/api/setup", json={"username": "reader", "password": "test-password-123"})
                self.assertIn("Secure", setup.headers["set-cookie"])
                admin = client.post("/api/admin/login", json={"username": "reader", "password": "test-password-123"},
                                    headers={"X-CSRF-Token": setup.json()["csrf_token"]})
                self.assertEqual(admin.status_code, 200)
                self.assertIn("Secure", admin.headers["set-cookie"])
                self.assertTrue(client.get("/api/admin/session").json()["authenticated"])

    def test_company_filter_validates_enabled_company_and_composes(self):
        source = add_source(self.db)
        self.app.state.collector.persist_articles(source, parse_rss(RSS, "https://example.com/feed"))
        company = {"type": "company", "name": "Nintendo", "aliases": [], "keywords": [], "exclude_keywords": [], "enabled": True}
        company_id = self.client.post("/api/watches", json=company, headers=self.headers).json()["id"]
        self.assertEqual(self.client.get(f"/api/articles?company_id={company_id}").json()["total"], 1)
        self.assertEqual(self.client.get(f"/api/articles?company_id={company_id}&q=sports").json()["total"], 0)
        self.assertEqual(self.client.get("/api/articles?company_id=99999").status_code, 404)
        company["enabled"] = False
        self.client.put(f"/api/watches/{company_id}", json=company, headers=self.headers)
        self.assertEqual(self.client.get(f"/api/articles?company_id={company_id}").status_code, 422)
        company.update({"enabled": True, "type": "studio"})
        self.client.put(f"/api/watches/{company_id}", json=company, headers=self.headers)
        self.assertEqual(self.client.get(f"/api/articles?company_id={company_id}").status_code, 422)

    def test_catalog_sports_filters_and_custom_teams(self):
        catalog = self.client.get("/api/catalog").json()
        self.assertEqual({entry["id"] for entry in catalog["leagues"]}, {"nba", "premier-league", "la-liga"})
        source = add_source(self.db, category="sports")
        fixtures = [("Golden State Warriors win", "warriors"), ("NBA draft update", "nba"),
                    ("EPL title race", "epl"), ("Real Madrid wins", "real"), ("巴萨训练新闻", "barca"),
                    ("Harbour Falcons new lineup", "custom")]
        items = [{"title": title, "summary": "sport context", "canonical_url": f"https://example.com/{key}", "url": f"https://example.com/{key}", "published_at": None, "image_url": None} for title, key in fixtures]
        self.app.state.collector.persist_articles(source, items)
        game_source = add_source(self.db, "Games", "games")
        self.app.state.collector.persist_articles(game_source, [{**items[0], "canonical_url": "https://example.com/movie", "url": "https://example.com/movie"}])
        self.assertEqual(self.client.get("/api/articles?league_id=nba").json()["total"], 2)
        self.assertEqual(self.client.get("/api/articles?league_id=nba&team_id=golden-state-warriors").json()["total"], 1)
        self.assertEqual(self.client.get("/api/articles?team_id=real-madrid").json()["total"], 1)
        self.assertEqual(self.client.get("/api/articles?league_id=la-liga").json()["total"], 2)
        self.assertEqual(self.client.get("/api/articles?league_id=premier-league&team_id=real-madrid").status_code, 422)
        self.assertEqual(self.client.get("/api/articles?team_id=unknown").status_code, 422)
        self.assertEqual(self.client.get("/api/articles?league_id=unknown").status_code, 422)
        custom = {"type": "team", "name": "Harbour Falcons", "league_id": "nba"}
        watch_id = self.client.post("/api/watches", json=custom, headers=self.headers).json()["id"]
        self.assertEqual(self.client.get("/api/articles?league_id=nba").json()["total"], 3)
        self.assertEqual(self.client.get(f"/api/articles?watch_id={watch_id}").json()["total"], 1)
        custom["enabled"] = False
        self.client.put(f"/api/watches/{watch_id}", json=custom, headers=self.headers)
        self.assertEqual(self.client.get("/api/articles?league_id=nba").json()["total"], 2)
        for kind in ("company", "studio", "politician"):
            self.assertEqual(self.client.post("/api/watches", json={"type": kind, "name": "Invalid", "league_id": "nba"}, headers=self.headers).status_code, 422)
        self.assertEqual(self.client.post("/api/watches", json={"type": "team", "name": "Invalid", "league_id": "wrong"}, headers=self.headers).status_code, 422)

    def test_preset_league_follow_rematch_and_collection_share_matcher(self):
        source = add_source(self.db, category="sports")
        fixtures = [{"title": "Golden State Warriors win", "summary": "playoffs", "canonical_url": "https://example.com/pre", "url": "https://example.com/pre", "published_at": None, "image_url": None}]
        self.app.state.collector.persist_articles(source, fixtures)
        watch = {"type": "league", "name": "NBA", "league_id": "nba", "keywords": ["playoffs"], "exclude_keywords": ["rumor"]}
        response = self.client.post("/api/watches", json=watch, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        watch_id = response.json()["id"]
        self.assertEqual(self.client.get(f"/api/articles?watch_id={watch_id}").json()["total"], 1)
        new = [{**fixtures[0], "url": "https://example.com/new", "canonical_url": "https://example.com/new"},
               {**fixtures[0], "summary": "playoffs rumor", "url": "https://example.com/rumor", "canonical_url": "https://example.com/rumor"},
               {**fixtures[0], "summary": "regular season", "url": "https://example.com/context", "canonical_url": "https://example.com/context"}]
        self.app.state.collector.persist_articles(source, new)
        game_source = add_source(self.db, "Games", "games")
        self.app.state.collector.persist_articles(game_source, [{**fixtures[0], "url": "https://example.com/game", "canonical_url": "https://example.com/game"}])
        self.assertEqual(self.client.get(f"/api/articles?watch_id={watch_id}").json()["total"], 2)
        self.client.put(f"/api/watches/{watch_id}", json=watch, headers=self.headers)
        self.assertEqual(self.client.get(f"/api/articles?watch_id={watch_id}").json()["total"], 2)
        watch["enabled"] = False
        self.client.put(f"/api/watches/{watch_id}", json=watch, headers=self.headers)
        self.assertEqual(self.client.get(f"/api/articles?watch_id={watch_id}").json()["total"], 0)
        custom = self.client.post("/api/watches", json={"type": "league", "name": "playoffs"}, headers=self.headers)
        self.assertEqual(custom.status_code, 200)
        self.assertEqual(self.client.get(f"/api/articles?watch_id={custom.json()['id']}").json()["total"], 4)

    def test_chinese_team_watch_matches_existing_and_new_english_news(self):
        source = add_source(self.db, category="sports")
        item = {"title": "Warriors win", "summary": "playoffs", "canonical_url": "https://example.com/existing-warriors",
                "url": "https://example.com/existing-warriors", "published_at": None, "image_url": None}
        self.app.state.collector.persist_articles(source, [item])
        watch = {"type": "team", "name": "勇士", "keywords": ["playoffs"], "exclude_keywords": ["rumor"]}
        response = self.client.post("/api/watches", json=watch, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        watch_id = response.json()["id"]
        self.assertEqual(response.json()["aliases"], [])
        self.assertEqual(self.client.get(f"/api/articles?watch_id={watch_id}").json()["total"], 1)
        self.app.state.collector.persist_articles(source, [
            {**item, "title": "Golden State Warriors win again", "canonical_url": "https://example.com/new-warriors", "url": "https://example.com/new-warriors"},
            {**item, "summary": "playoffs rumor", "canonical_url": "https://example.com/warriors-rumor", "url": "https://example.com/warriors-rumor"},
            {**item, "summary": "regular season", "canonical_url": "https://example.com/warriors-context", "url": "https://example.com/warriors-context"}])
        game_source = add_source(self.db, category="games")
        self.app.state.collector.persist_articles(game_source, [
            {**item, "canonical_url": "https://example.com/warriors-game", "url": "https://example.com/warriors-game"}])
        self.assertEqual(self.client.get(f"/api/articles?watch_id={watch_id}").json()["total"], 2)
        self.client.put(f"/api/watches/{watch_id}", json=watch, headers=self.headers)
        self.assertEqual(self.client.get(f"/api/articles?watch_id={watch_id}").json()["total"], 2)


class MigrationTests(unittest.TestCase):
    def test_default_source_ids_are_validated(self):
        source = {"name": "New feed", "url": "https://example.com/feed", "category": "sports"}
        for entries in ([{**source, "seed_id": "bad key"}],
                        [{**source, "seed_id": "same"}, {**source, "seed_id": "same"}]):
            with self.subTest(entries=entries), patch("app.db.Path.read_text", return_value=json.dumps(entries)):
                with self.assertRaisesRegex(ValueError, "seed_id"):
                    load_default_sources()

    def test_default_source_upgrade_is_once_and_preserves_user_choices(self):
        defaults = [
            (None, Source(name="Old deleted", url="https://example.com/old-deleted", category="sports").model_dump()),
            (None, Source(name="Old disabled", url="https://example.com/old-disabled", category="sports").model_dump()),
            ("new-one", Source(name="New one", url="https://example.com/new-one", category="sports").model_dump()),
            ("new-two", Source(name="New two", url="https://example.com/new-two", category="sports").model_dump())]
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            with patch("app.db.load_default_sources", return_value=defaults[:2]):
                db.initialize()
            with db.connection(write=True) as conn:
                conn.execute("DELETE FROM sources WHERE name='Old deleted'")
                conn.execute("UPDATE sources SET name='User renamed',enabled=0,last_success_at='previous',last_error='kept' WHERE name='Old disabled'")
                conn.execute("INSERT INTO sources(name,kind,url,category,enabled) VALUES('My feed','rss','https://example.com/new-two','games',0)")
            with patch("app.db.load_default_sources", return_value=defaults):
                db.initialize()
                db.initialize()
                with db.connection() as conn:
                    rows = {row["url"]: dict(row) for row in conn.execute("SELECT * FROM sources")}
                    self.assertEqual(set(rows), {"https://example.com/old-disabled", "https://example.com/new-one", "https://example.com/new-two"})
                    old = rows["https://example.com/old-disabled"]
                    self.assertEqual((old["name"], old["enabled"], old["last_success_at"], old["last_error"]),
                                     ("User renamed", 0, "previous", "kept"))
                    duplicate = rows["https://example.com/new-two"]
                    self.assertEqual((duplicate["name"], duplicate["category"], duplicate["enabled"]), ("My feed", "games", 0))
                    self.assertEqual(rows["https://example.com/new-one"]["enabled"], 1)
                with db.connection(write=True) as conn:
                    conn.execute("UPDATE sources SET enabled=0 WHERE url='https://example.com/new-one'")
                    conn.execute("DELETE FROM sources WHERE url='https://example.com/new-two'")
                db.initialize()
                with db.connection() as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 2)
                    self.assertEqual(conn.execute("SELECT enabled FROM sources WHERE url='https://example.com/new-one'").fetchone()[0], 0)
            extra = ("new-three", Source(name="New three", url="https://example.com/new-three", category="sports").model_dump())
            with patch("app.db.load_default_sources", return_value=[*defaults, extra]):
                db.initialize()
                db.initialize()
            with db.connection() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 3)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources WHERE url='https://example.com/new-two'").fetchone()[0], 0)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_fresh_install_records_upgrades_before_user_deletes_a_source(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            db.initialize()
            defaults = load_default_sources()
            with db.connection(write=True) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], len(defaults))
                for seed_id, _ in defaults:
                    if seed_id:
                        self.assertIsNotNone(conn.execute("SELECT 1 FROM metadata WHERE key=?", ("default_source:" + seed_id,)).fetchone())
                conn.execute("DELETE FROM sources")
            db.initialize()
            with db.connection() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)

    def test_matching_rule_upgrade_rematches_existing_team_watches_with_same_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            db = empty_db(directory)
            source = add_source(db, category="sports")
            item = {"title": "Warriors win", "summary": "playoffs", "canonical_url": "https://example.com/old-warriors",
                    "url": "https://example.com/old-warriors", "published_at": None, "image_url": None}
            Collector(db).persist_articles(source, [item])
            with db.connection(write=True) as conn:
                watch_id = conn.execute("INSERT INTO watches(type,name,aliases,keywords,exclude_keywords,enabled,market,ticker) VALUES('team','勇士','[]','[\"playoffs\"]','[\"rumor\"]',1,'','')").lastrowid
                conn.execute("UPDATE articles SET saved=1,read=1")
                version = conn.execute("SELECT value FROM metadata WHERE key='topic_index_version'").fetchone()[0]
                conn.execute("UPDATE metadata SET value=? WHERE key='topic_index_version'", (version.replace("sports-v2:", "sports-v1:"),))
            db.initialize()
            db.initialize()
            with db.connection() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM article_watches WHERE watch_id=?", (watch_id,)).fetchone()[0], 1)
                self.assertEqual(tuple(conn.execute("SELECT saved,read FROM articles").fetchone()), (1, 1))
                self.assertEqual(tuple(conn.execute("SELECT name,aliases,keywords,exclude_keywords FROM watches WHERE id=?", (watch_id,)).fetchone()),
                                 ("勇士", "[]", '["playoffs"]', '["rumor"]'))

    def test_old_database_preserves_user_article_watch_schedule_and_reindexes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "newsroom.sqlite3"
            conn = sqlite3.connect(path)
            conn.executescript('''
                CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT NOT NULL,password_hash TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE watches(id INTEGER PRIMARY KEY,type TEXT NOT NULL,name TEXT NOT NULL,aliases TEXT NOT NULL,keywords TEXT NOT NULL,exclude_keywords TEXT NOT NULL,enabled INTEGER NOT NULL,market TEXT NOT NULL,ticker TEXT NOT NULL);
                CREATE TABLE articles(id INTEGER PRIMARY KEY,canonical_url TEXT NOT NULL UNIQUE,title TEXT NOT NULL,url TEXT NOT NULL,summary TEXT NOT NULL,source_id INTEGER,source_name TEXT NOT NULL,category TEXT NOT NULL,published_at TEXT,fetched_at TEXT NOT NULL,image_url TEXT,saved INTEGER NOT NULL DEFAULT 0,read INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE schedules(id INTEGER PRIMARY KEY,name TEXT NOT NULL,time TEXT NOT NULL,days TEXT NOT NULL,categories TEXT NOT NULL,enabled INTEGER NOT NULL,last_run_at TEXT,created_at TEXT NOT NULL);
                CREATE TABLE settings(id INTEGER PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO metadata VALUES('sources_seeded','1');
                INSERT INTO watches VALUES(7,'company','Apple','[]','[]','[]',1,'NASDAQ','AAPL');
                INSERT INTO articles(id,canonical_url,title,url,summary,source_name,category,fetched_at,saved,read) VALUES(11,'https://example.com/old','Golden State Warriors win','https://example.com/old','','Old feed','sports','2026-10-01T00:00:00+00:00',1,1);
                INSERT INTO schedules VALUES(4,'Old morning','08:30','[0,1,2,3,4]','["sports"]',1,'2026-10-01T00:30:00+00:00','2026-09-01T00:00:00+00:00');
                INSERT INTO settings VALUES(1,'{"timezone":"UTC","catch_up":false,"catch_up_hours":2}');
            ''')
            conn.execute("INSERT INTO users VALUES(1,'reader',?,'2026-09-01T00:00:00+00:00')", (password_hash("existing-password"),))
            conn.commit()
            conn.close()
            app = create_app(directory, start_scheduler=False)
            with TestClient(app) as client:
                login = client.post("/api/login", json={"username": "reader", "password": "existing-password"})
                self.assertEqual(login.status_code, 200)
                watch = client.get("/api/watches").json()["items"][0]
                self.assertEqual((watch["id"], watch["ticker"], watch["league_id"]), (7, "AAPL", ""))
                article = client.get("/api/articles?league_id=nba").json()["items"][0]
                self.assertEqual((article["id"], article["saved"], article["read"]), (11, True, True))
                self.assertEqual(client.get("/api/preferences").json(), {"timezone": "UTC"})
                self.assertEqual(client.get("/api/schedules").json()["items"][0]["id"], 4)
                self.assertEqual(client.get("/api/sources").status_code, 403)
            app.state.db.initialize()
            with app.state.db.connection() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM article_topics").fetchone()[0], 2)
                self.assertEqual({row[0] for row in conn.execute("SELECT url FROM sources")},
                                 {source["url"] for seed_id, source in load_default_sources() if seed_id})
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_catalog_change_reindexes_historical_articles(self):
        with tempfile.TemporaryDirectory() as directory:
            db = empty_db(directory)
            source = add_source(db, category="sports")
            collector = Collector(db)
            item = {"title": "New Club wins", "summary": "", "canonical_url": "https://example.com/newclub", "url": "https://example.com/newclub", "published_at": None, "image_url": None}
            collector.persist_articles(source, [item])
            with db.connection(write=True) as conn:
                watch_id = conn.execute("INSERT INTO watches(type,name,aliases,keywords,exclude_keywords,enabled,market,ticker,league_id) VALUES('league','NBA','[]','[]','[]',1,'','','nba')").lastrowid
                team_watch_id = conn.execute("INSERT INTO watches(type,name,aliases,keywords,exclude_keywords,enabled,market,ticker,league_id) VALUES('team','新俱乐部','[]','[]','[]',1,'','','nba')").lastrowid
                rematch_watch(conn, watch_id, db.catalog)
                rematch_watch(conn, team_watch_id, db.catalog)
            with db.connection() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM article_topics").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM article_watches").fetchone()[0], 0)
            db.catalog["teams"].append({"id": "new-club", "name": "新俱乐部", "aliases": ["New Club"], "league_id": "nba"})
            db.initialize()
            with db.connection() as conn:
                self.assertEqual({row[0] for row in conn.execute("SELECT topic_id FROM article_topics")}, {"team:new-club", "league:nba"})
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM article_watches WHERE watch_id=?", (watch_id,)).fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM article_watches WHERE watch_id=?", (team_watch_id,)).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
