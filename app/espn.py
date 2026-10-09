"""Normalize ESPN's public league news when its RSS cannot be read."""
import json
import xml.etree.ElementTree as ET
from email.utils import format_datetime
from datetime import datetime

import httpx

from .collector import _article, fetch_public, safe_error

NBA_NEWS = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news"
ESPN_FALLBACKS = {
    "https://www.espn.com/espn/rss/nba/news": [NBA_NEWS],
    "https://www.espn.com/espn/rss/news": [
        NBA_NEWS,
        "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/news",
        "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/news",
    ],
}


def parse_news(data):
    payload = json.loads(data)
    if not isinstance(payload, dict) or not isinstance(payload.get("articles"), list):
        raise ValueError("ESPN 新闻接口格式无效")
    articles = []
    for entry in payload["articles"][:200]:
        if not isinstance(entry, dict):
            continue
        links = entry.get("links")
        web = links.get("web") if isinstance(links, dict) else None
        url = web.get("href") if isinstance(web, dict) else None
        values = [entry.get("headline"), url, entry.get("description") or "", entry.get("published") or ""]
        if not all(isinstance(value, str) for value in values):
            continue
        images = entry.get("images")
        image = images[0].get("url") if isinstance(images, list) and images and isinstance(images[0], dict) else None
        article = _article(*values, image if isinstance(image, str) else None)
        if article:
            articles.append(article)
    return articles


async def fallback_rss(source):
    articles = {}
    errors = []
    for url in ESPN_FALLBACKS.get(source["url"], []):
        try:
            data, _ = await fetch_public(url)
            parsed = parse_news(data)
            if not parsed:
                raise ValueError("ESPN 新闻接口未提供资讯")
            for article in parsed:
                articles[article["canonical_url"]] = article
        except (OSError, ValueError, httpx.HTTPError) as error:
            errors.append({"url": url, "error": safe_error(error)})
    if not articles:
        raise ValueError("ESPN RSS 与备用新闻接口均未取得资讯")
    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = source["name"]
    ET.SubElement(channel, "link").text = "https://www.espn.com/"
    ET.SubElement(channel, "description").text = "ESPN public league news"
    for article in articles.values():
        item = ET.SubElement(channel, "item")
        for tag, field in (("title", "title"), ("link", "url"), ("description", "summary")):
            ET.SubElement(item, tag).text = article[field]
        if article["published_at"]:
            ET.SubElement(item, "pubDate").text = format_datetime(datetime.fromisoformat(article["published_at"]))
        if article["image_url"]:
            ET.SubElement(item, "thumbnail", url=article["image_url"])
    return ET.tostring(root, encoding="utf-8"), errors
