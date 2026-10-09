"""Import public feed data obtained through the reader's browser network."""
import json
from hashlib import sha256
from urllib.parse import urlsplit

from .collector import MAX_BYTES, _article, parse_rss
from .db import load_default_sources

FOREIGN_HOSTS = {"feeds.bbci.co.uk", "www.espn.com", "www.theguardian.com", "www.ftchinese.com"}
CACHE_BASE_URL = "https://raw.githubusercontent.com/CandyTea/xunlan-newsroom/news-cache/public-feeds"


def cache_filename(source_url):
    return sha256(source_url.encode("utf-8")).hexdigest() + ".json"


def browser_cache_url(source_url):
    return f"{CACHE_BASE_URL}/{cache_filename(source_url)}"


def browser_source_urls():
    return {source["url"] for _, source in load_default_sources()
            if source["kind"] == "rss" and urlsplit(source["url"]).hostname in FOREIGN_HOSTS}


def parse_browser_feed(content, format, source_url):
    data = content.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("来源响应超过 2 MB 上限")
    if format == "rss":
        return parse_rss(data, source_url)
    payload = json.loads(data)
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise ValueError("转接服务未返回有效资讯")
    entries = payload.get("items")
    if not isinstance(entries, list) or len(entries) > 100:
        raise ValueError("转接服务返回的资讯格式无效")
    articles = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("转接服务返回的资讯格式无效")
        title = entry.get("title", "")
        link = entry.get("link", "")
        summary = entry.get("description") or entry.get("content") or ""
        published = entry.get("pubDate") or ""
        image = entry.get("thumbnail") or None
        if not all(isinstance(value, str) for value in (title, link, summary, published)):
            raise ValueError("转接服务返回的资讯格式无效")
        if image is not None and not isinstance(image, str):
            image = None
        article = _article(title, link, summary, published, image)
        if article:
            articles.append(article)
    return articles
