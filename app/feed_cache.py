"""Publish preset public overseas feeds and Telegram channel previews."""
import argparse
import asyncio
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from .browser import browser_source_urls, cache_filename
from .collector import MAX_BYTES, fetch_public, parse_rss, parse_source, safe_error
from .db import load_default_sources, utc_now
from .espn import ESPN_FALLBACKS, fallback_rss
from .xfeeds import fetch_x


def previous_cache(filename):
    result = subprocess.run(
        ["git", "show", f"refs/remotes/origin/news-cache:public-feeds/{filename}"],
        capture_output=True, check=False,
    )
    return result.stdout if result.returncode == 0 else None


async def build_cache(output):
    output.mkdir(parents=True, exist_ok=True)
    allowed = browser_source_urls()
    sources = [source for _, source in load_default_sources() if source["url"] in allowed]
    results = []
    semaphore = asyncio.Semaphore(3)

    async def collect(source):
        filename = cache_filename(source["url"])
        prior = previous_cache(filename)
        if prior is not None:
            (output / filename).write_bytes(prior)
        async with semaphore:
            try:
                route = "telegram-public-preview" if source["kind"] == "telegram" else "rss"
                route_errors = []
                try:
                    if source["kind"] == "x":
                        articles, data = await fetch_x(source["url"])
                        route = "x-public-page"
                    else:
                        data, base_url = await fetch_public(source["url"])
                        articles = parse_source(data, source, base_url)
                    if not articles:
                        raise ValueError("来源没有返回可保存的资讯")
                except (OSError, ValueError, ET.ParseError, httpx.HTTPError):
                    if source["url"] not in ESPN_FALLBACKS:
                        raise
                    data, route_errors = await fallback_rss(source)
                    articles = parse_rss(data, source["url"])
                    route = "espn-public-api"
                format = source["kind"] if source["kind"] in ("telegram", "x") else "rss"
                content = data.decode("utf-8", errors="replace") if format in ("telegram", "x") else ET.tostring(ET.fromstring(data), encoding="unicode")
                payload = {"source_url": source["url"], "fetched_at": utc_now(), "format": format, "content": content}
                encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                if len(encoded) > MAX_BYTES:
                    raise ValueError("来源响应超过缓存大小上限")
                (output / filename).write_bytes(encoded)
                results.append({"source_url": source["url"], "status": "success", "item_count": len(articles), "fetched_at": payload["fetched_at"], "route": route, "route_errors": route_errors})
                print(f"Collected {source['name']}: {len(articles)} articles", flush=True)
            except (OSError, ValueError, ET.ParseError, httpx.HTTPError) as error:
                results.append({"source_url": source["url"], "status": "failed", "error": safe_error(error), "retained_previous": prior is not None})
                print(f"Failed {source['name']}: {safe_error(error)}", flush=True)

    await asyncio.gather(*(collect(source) for source in sources))
    manifest = {"updated_at": utc_now(), "sources": sorted(results, key=lambda item: item["source_url"])}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if not any(item["status"] == "success" for item in results):
        raise SystemExit("No overseas feeds collected; previous cache retained where available")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    asyncio.run(build_cache(options.output))


if __name__ == "__main__":
    main()
