"""DNS recovery, egress policy, and public-address checks without network access."""
import asyncio
import json
import os
import socket
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.collector import fetch_config, resolve_public_url
from app.dns import DNS_MAX_BYTES, dns_mode, resolve_https


class DNSTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"NEWSROOM_DNS_MODE": "fallback", "NEWSROOM_FETCH_MODE": "direct", "NEWSROOM_OUTBOUND_PROXY": ""}))

    async def test_failed_or_nonpublic_system_lookup_recovers_with_public_https_result(self):
        loop = asyncio.get_running_loop()
        for outcome in (socket.gaierror(socket.EAI_NONAME, "no answer"), TimeoutError(),
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]):
            lookup = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
            with self.subTest(outcome=outcome), patch.object(loop, "getaddrinfo", lookup), \
                 patch("app.collector.resolve_https", AsyncMock(return_value=["1.1.1.1"])) as backup:
                original, pinned, host, hostname = await resolve_public_url("https://example.com/feed")
                self.assertEqual((original, pinned, host, hostname), ("https://example.com/feed", "https://1.1.1.1:443/feed", "example.com", "example.com"))
                backup.assert_awaited_once_with("example.com", None, allow_direct=False)

    async def test_good_system_dns_does_not_use_backup(self):
        loop = asyncio.get_running_loop()
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=answers)), \
             patch("app.collector.resolve_https", AsyncMock()) as backup:
            await resolve_public_url("https://example.com/feed")
            backup.assert_not_awaited()

    async def test_https_only_skips_system_dns_and_rotates_public_addresses(self):
        loop = asyncio.get_running_loop()
        with patch.dict(os.environ, {"NEWSROOM_DNS_MODE": "https"}), \
             patch.object(loop, "getaddrinfo", AsyncMock()) as system, \
             patch("app.collector.resolve_https", AsyncMock(return_value=["1.1.1.1", "8.8.8.8"])):
            self.assertEqual((await resolve_public_url("https://example.com/feed", ip_index=1))[1], "https://8.8.8.8:443/feed")
            system.assert_not_awaited()

    async def test_backup_cannot_connect_to_private_or_mixed_addresses(self):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(side_effect=socket.gaierror())), \
             patch("app.collector.resolve_https", AsyncMock(return_value=["1.1.1.1", "169.254.169.254"])):
            with self.assertRaisesRegex(ValueError, "非公网"):
                await resolve_public_url("https://example.com/feed")

    async def test_literal_addresses_are_validated_without_dns_in_https_mode(self):
        with patch.dict(os.environ, {"NEWSROOM_DNS_MODE": "https"}), \
             patch("app.collector.resolve_https", AsyncMock()) as backup:
            self.assertEqual((await resolve_public_url("https://1.1.1.1/feed"))[1], "https://1.1.1.1:443/feed")
            with self.assertRaises(ValueError):
                await resolve_public_url("http://127.0.0.1/feed")
            backup.assert_not_awaited()

    def test_invalid_dns_configuration_is_rejected(self):
        with patch.dict(os.environ, {"NEWSROOM_DNS_MODE": "unknown"}):
            with self.assertRaises(ValueError):
                fetch_config()

    async def test_https_response_rejects_invalid_private_and_oversized_payloads(self):
        payloads = [b"not JSON", json.dumps({"Status": 3}).encode(),
                    json.dumps({"Status": 0, "Answer": [{"type": 1, "data": "1.1.1.1"}, {"type": 1, "data": "127.0.0.1"}]}).encode(),
                    json.dumps({"Status": 0, "Answer": [{"type": 1, "data": "invalid"}]}).encode(),
                    b"x" * (DNS_MAX_BYTES + 1)]
        real_client = httpx.AsyncClient
        for payload in payloads:
            with self.subTest(payload=payload[:80]), patch("app.dns.httpx.AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload)), trust_env=False)):
                with self.assertRaises(ValueError):
                    await resolve_https("example.com")

    async def test_https_lookup_switches_route_only_when_allowed(self):
        real_client = httpx.AsyncClient
        for allow_direct in (False, True):
            routes = []
            def factory(**kwargs):
                route = kwargs["proxy"]
                routes.append(route)
                self.assertFalse(kwargs["trust_env"])
                self.assertFalse(kwargs["follow_redirects"])
                def handler(request):
                    if route:
                        raise httpx.ProxyError("proxy-secret")
                    return httpx.Response(200, json={"Status": 0, "Answer": [{"type": 1, "data": "1.1.1.1"}]})
                return real_client(transport=httpx.MockTransport(handler), trust_env=False)
            with self.subTest(allow_direct=allow_direct), patch("app.dns.httpx.AsyncClient", factory):
                if allow_direct:
                    self.assertEqual(await resolve_https("example.com", "http://proxy.example", allow_direct=True), ["1.1.1.1"])
                    self.assertEqual(routes, ["http://proxy.example", None])
                else:
                    with self.assertRaisesRegex(ValueError, "加密 DNS") as error:
                        await resolve_https("example.com", "http://proxy.example", allow_direct=False)
                    self.assertNotIn("proxy-secret", str(error.exception))
                    self.assertEqual(routes, ["http://proxy.example"])


if __name__ == "__main__":
    unittest.main()
