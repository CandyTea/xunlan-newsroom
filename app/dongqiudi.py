"""Read Dongqiudi's public web feed and its explicit article metadata."""
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from lxml import etree, html

from .collector import _article

HOME_URL = "https://www.dongqiudi.com/"
LIST_URL = HOME_URL + "api/app/tabs/web/1.json"
DETAIL_URL = HOME_URL + "api/v2/article/detail/"
ARTICLE_URL = "https://pc.dongqiudi.com/articles/{}.html"
ARTICLE_ID = re.compile(r"/articles?/([1-9]\d{0,19})(?:\.html)?$")
TAG_ID = re.compile(r"/(team|player)/(\d+)$")


class RestrictedArticle(ValueError):
    pass


def published_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        date = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return date.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def document(data):
    raw = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("懂球帝未返回有效页面")
    try:
        root = html.fromstring(raw)
    except etree.ParserError as exc:
        raise ValueError("懂球帝返回的页面无法解析") from exc
    for node in root.xpath("//script|//style|//noscript"):
        node.drop_tree()
    return root


def node_text(root, xpath):
    nodes = root.xpath(xpath)
    return " ".join(nodes[0].text_content().split()) if nodes else ""


def public_article_id(url):
    parts = urlsplit(url)
    if parts.hostname not in ("www.dongqiudi.com", "pc.dongqiudi.com", "m.dongqiudi.com"):
        return None
    match = ARTICLE_ID.fullmatch(parts.path)
    return match[1] if match else None


def list_page(data, *, allow_empty=False, require_json=False):
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeError):
        payload = None
    items = []
    after = None
    if isinstance(payload, dict) and isinstance(payload.get("articles"), list):
        if require_json and (str(payload.get("id")) != "1" or payload.get("code") not in (None, 0, 200)):
            raise ValueError("懂球帝返回的新闻列表不属于当前公开栏目")
        if len(payload["articles"]) > 200:
            raise ValueError("懂球帝新闻列表超过单页大小上限")
        for entry in payload["articles"]:
            if not isinstance(entry, dict) or entry.get("channel", "article") != "article":
                continue
            article_id = str(entry.get("id", ""))
            if not re.fullmatch(r"[1-9]\d{0,19}", article_id):
                continue
            # Pinned stories use a future sort date; created_at is their actual date.
            item = _article(str(entry.get("title") or ""), ARTICLE_URL.format(article_id),
                            str(entry.get("description") or ""),
                            published_time(entry.get("created_at") or entry.get("published_at")), entry.get("thumb"))
            if item:
                author = entry.get("author") or {}
                item["dqd"] = {"author": str(author.get("name") or "")[:200] if isinstance(author, dict) else "",
                               "tags": [], "complete": False}
                items.append(item)
        if payload.get("next"):
            parts = urlsplit(str(payload["next"]))
            query = parse_qs(parts.query)
            raw_after = query.get("after", [""])[0]
            raw_page = query.get("page", [""])[0]
            if (parts.hostname not in ("api.dongqiudi.com", "www.dongqiudi.com")
                    or parts.path not in ("/app/tabs/web/1.json", "/api/app/tabs/web/1.json")
                    or not raw_after.isdigit() or not raw_page.isdigit()
                    or not 0 < int(raw_after) < 2**63 or not 1 < int(raw_page) <= 1000000):
                raise ValueError("懂球帝返回的继续读取位置无效")
            after = {"after": int(raw_after), "page": int(raw_page)}
    else:
        if require_json:
            raise ValueError("懂球帝没有返回可继续读取的公开新闻列表")
        root = document(data)
        seen = set()
        for link in root.xpath('//a[contains(@href,"/articles/")]'):
            article_id = public_article_id(HOME_URL.rstrip("/") + link.get("href", "")) if link.get("href", "").startswith("/") else public_article_id(link.get("href", ""))
            if not article_id or article_id in seen:
                continue
            title = node_text(link, ".//h2|.//h3") or link.get("title", "")
            if not title:
                continue
            seen.add(article_id)
            images = link.xpath(".//img/@src")
            item = _article(title, ARTICLE_URL.format(article_id), "", None, images[0] if images else None)
            if item:
                item["dqd"] = {"author": link.get("author", "")[:200], "tags": [], "complete": False}
                items.append(item)
    if not items and not (allow_empty and isinstance(payload, dict) and isinstance(payload.get("articles"), list)):
        raise ValueError("懂球帝未返回可识别的公开新闻列表")
    return items, after


def next_list_url(cursor):
    return LIST_URL + "?" + urlencode({"after": cursor["after"], "page": cursor["page"]})


def tags_from_links(links):
    tags = {}
    for href, name in links:
        if not isinstance(href, str):
            continue
        match = TAG_ID.fullmatch(urlsplit(href).path)
        name = " ".join(str(name or "").split())[:120]
        if match and name:
            kind, tag_id = match.groups()
            if len(tag_id) <= 20:
                tags[(kind, tag_id)] = {"kind": kind, "id": tag_id, "name": name}
    return list(tags.values())[:100]


def body_paragraphs(root):
    paragraphs = []
    for node in root.xpath(".//p|.//h2|.//h3|.//li[not(.//p)]"):
        value = " ".join(node.text_content().split())
        if value:
            paragraphs.append(value)
    if not paragraphs:
        value = " ".join(root.text_content().split())
        if value:
            paragraphs = [value]
    if not paragraphs or len(paragraphs) > 300 or sum(map(len, paragraphs)) > 60000:
        raise ValueError("懂球帝文章未提供可保存的公开正文")
    return paragraphs


def detail_page(data, article_id):
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeError):
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        value = payload["data"]
        if str(value.get("article_id")) != article_id:
            raise ValueError("懂球帝返回的文章编号不一致")
        if value.get("is_pay_show") in (True, 1, "1") or value.get("redirect_url"):
            raise RestrictedArticle("这篇懂球帝文章未提供直接可读取的公开正文")
        root = document(value.get("body") or "<div></div>")
        title = value.get("title") or ""
        author = str(value.get("writer") or "").strip()[:200]
        date = published_time(value.get("time"))
        image = value.get("thumb")
        infos = value.get("infos")
        channels = infos.get("channels", []) if isinstance(infos, dict) else []
        if not isinstance(channels, list):
            raise ValueError("懂球帝文章标签格式无效")
        tags = tags_from_links((entry.get("href", ""), entry.get("tag", ""))
                               for entry in channels if isinstance(entry, dict))
    else:
        raw = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
        if re.search(r'"isAccessibleForFree"\s*:\s*(?:false|"false")', raw, re.I):
            raise RestrictedArticle("这篇懂球帝文章需要在来源网站阅读")
        page = document(data)
        bodies = page.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," article-body ")]')
        if not bodies:
            raise ValueError("懂球帝页面未包含公开正文")
        root = bodies[0]
        title = node_text(page, "//h1")
        author = node_text(page, '//*[contains(@class,"article-head__author-name")]')[:200]
        dates = page.xpath('//meta[@property="article:published_time"]/@content')
        date = published_time(dates[0]) if dates else None
        images = root.xpath(".//img/@src")
        image = images[0] if images else None
        tags = tags_from_links((a.get("href", ""), a.text_content()) for a in page.xpath(
            '//a[contains(@class,"related__chip") or contains(@class,"topic-chip")]'))
    paragraphs = body_paragraphs(root)
    item = _article(str(title), ARTICLE_URL.format(article_id), " ".join(paragraphs)[:1800], date, image)
    if not item:
        raise ValueError("懂球帝文章缺少有效标题或链接")
    item["content"] = {"paragraphs": paragraphs, "author": author}
    item["dqd"] = {"author": author, "tags": tags, "complete": True}
    return item
