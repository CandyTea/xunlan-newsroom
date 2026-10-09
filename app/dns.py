"""Bounded HTTPS DNS lookup for feeds whose system lookup fails."""
import asyncio
import ipaddress
import json
import os

import httpx

DNS_ENDPOINT = "https://doh.pub/dns-query"
DNS_MAX_BYTES = 64 * 1024


def dns_mode():
    mode = os.getenv("NEWSROOM_DNS_MODE", "fallback").strip().lower()
    if mode not in {"system", "fallback", "https"}:
        raise ValueError("NEWSROOM_DNS_MODE 必须为 system、fallback 或 https")
    return mode


async def resolve_https(hostname, proxy=None, *, allow_direct=True):
    routes = [proxy, None] if proxy and allow_direct else [proxy]
    async with asyncio.timeout(6):
        for index, route in enumerate(routes):
            try:
                async with httpx.AsyncClient(timeout=3, trust_env=False, proxy=route,
                                             follow_redirects=False) as client:
                    async with client.stream("GET", DNS_ENDPOINT, params={"name": hostname, "type": "A"},
                                             headers={"Accept": "application/dns-json"}) as response:
                        response.raise_for_status()
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > DNS_MAX_BYTES:
                                raise ValueError("加密 DNS 响应超过大小上限")
                try:
                    payload = json.loads(data)
                except (ValueError, UnicodeError):
                    raise ValueError("加密 DNS 未返回有效解析结果") from None
                if not isinstance(payload, dict) or payload.get("Status") != 0:
                    raise ValueError("加密 DNS 未返回有效解析结果")
                answers = payload.get("Answer", [])
                if not isinstance(answers, list):
                    raise ValueError("加密 DNS 未返回有效解析结果")
                addresses = []
                for answer in answers:
                    if not isinstance(answer, dict):
                        raise ValueError("加密 DNS 未返回有效解析结果")
                    if answer.get("type") not in (1, 28):
                        continue
                    try:
                        address = ipaddress.ip_address(answer.get("data", ""))
                    except ValueError:
                        raise ValueError("加密 DNS 未返回有效解析结果") from None
                    if not address.is_global:
                        raise ValueError("加密 DNS 解析到了非公网地址，已拒绝访问")
                    if address.version == 4:
                        addresses.append(str(address))
                if not addresses:
                    raise ValueError("加密 DNS 未返回公网 IPv4 地址")
                return list(dict.fromkeys(addresses))
            except (httpx.HTTPError, TimeoutError):
                if index == len(routes) - 1:
                    raise ValueError("无法连接加密 DNS，请检查服务器网络或使用系统 DNS") from None
