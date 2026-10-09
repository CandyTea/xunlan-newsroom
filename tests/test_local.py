"""Local launch proxy selection remains separate from server configuration."""
import os
import unittest
from unittest.mock import patch

from app.local import configure_local_network


class LocalNetworkTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"NEWSROOM_FETCH_MODE": "auto", "NEWSROOM_OUTBOUND_PROXY": ""}))

    def test_uses_https_then_http_system_proxy_without_printing_credentials(self):
        with patch("app.local.getproxies", return_value={"https": "http://user:secret@127.0.0.1:7890", "http": "http://127.0.0.1:7891"}):
            self.assertTrue(configure_local_network())
        self.assertEqual(os.environ["NEWSROOM_OUTBOUND_PROXY"], "http://user:secret@127.0.0.1:7890")

    def test_explicit_proxy_is_preserved(self):
        with patch.dict(os.environ, {"NEWSROOM_OUTBOUND_PROXY": "http://127.0.0.1:8080"}), \
             patch("app.local.getproxies") as detected:
            self.assertTrue(configure_local_network())
            detected.assert_not_called()
            self.assertEqual(os.environ["NEWSROOM_OUTBOUND_PROXY"], "http://127.0.0.1:8080")

    def test_direct_mode_does_not_use_system_proxy(self):
        with patch.dict(os.environ, {"NEWSROOM_FETCH_MODE": "direct"}), \
             patch("app.local.getproxies", return_value={"https": "http://127.0.0.1:7890"}) as detected:
            self.assertFalse(configure_local_network())
            detected.assert_not_called()

    def test_http_proxy_is_used_when_https_is_unavailable(self):
        with patch("app.local.getproxies", return_value={"http": "http://127.0.0.1:7890"}):
            self.assertTrue(configure_local_network())
        self.assertEqual(os.environ["NEWSROOM_OUTBOUND_PROXY"], "http://127.0.0.1:7890")

    def test_missing_or_unsupported_proxy_does_not_create_an_outlet(self):
        for proxies in ({}, {"https": "socks5://127.0.0.1:1080"}):
            with self.subTest(proxies=proxies), patch("app.local.getproxies", return_value=proxies):
                self.assertFalse(configure_local_network())
                self.assertEqual(os.environ["NEWSROOM_OUTBOUND_PROXY"], "")
