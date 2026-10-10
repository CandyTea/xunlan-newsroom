"""Read public X posts from embedded profiles and optional free mirrors."""
import asyncio
import html as text_html
import json
import os
import re
import textwrap
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from lxml import etree, html

DEFAULT_MIRRORS = ("https://nitter.poast.org",)
EMBED_ORIGIN = "https://syndication.twitter.com"
X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
PRESETS = {"elonmusk": "马斯克", "thsottiaux": "Tibo · OpenAI"}


def account_url(value):
    value = value.strip()
    if value.startswith(("x.com/", "twitter.com/", "www.x.com/", "www.twitter.com/")):
        value = "https://" + value
    if "://" in value:
        parts = urlsplit(value)
        if (parts.scheme not in ("http", "https") or parts.hostname not in X_HOSTS
                or parts.username or parts.password or parts.port not in (None, 80, 443)):
            raise ValueError("请输入 X 账号用户名或 x.com 个人主页链接")
        value = parts.path.strip("/")
    value = value.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", value) or value.lower() in {
        "home", "explore", "search", "settings", "notifications", "messages", "i", "intent", "share",
    }:
        raise ValueError("请输入有效的 X 用户名，例如 @elonmusk 或 @thsottiaux；不支持私密账号")
    return "https://x.com/" + value.lower()


def mirror_urls():
    from .models import public_url_syntax
    configured = os.getenv("NEWSROOM_X_MIRRORS", "").strip()
    result = []
    for value in configured.split(",") if configured else DEFAULT_MIRRORS:
        value = public_url_syntax(value.strip()).rstrip("/")
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.path or parts.query or not re.fullmatch(r"[a-z0-9.-]+", parts.hostname):
            raise ValueError("NEWSROOM_X_MIRRORS 应为逗号分隔的公网 HTTPS 镜像根地址")
        if value not in result:
            result.append(value)
    if not result or len(result) > 6:
        raise ValueError("请配置 1–6 个免费 X 镜像")
    return result


def source_routes(source_url):
    username = urlsplit(account_url(source_url)).path.strip("/")
    return [{"url": EMBED_ORIGIN + "/srv/timeline-profile/screen-name/" + username, "format": "x"}] + [
        {"url": mirror + "/" + username + suffix, "format": "x"}
        for suffix in ("/rss", "") for mirror in mirror_urls()
    ]


def post_url(value, username, base_url):
    parts = urlsplit(urljoin(base_url, value))
    allowed = X_HOSTS | {urlsplit(base_url).hostname} | {urlsplit(url).hostname for url in mirror_urls()}
    if (parts.scheme not in ("http", "https") or parts.hostname not in allowed
            or parts.username or parts.password or parts.port not in (None, 80, 443)):
        return None
    match = re.fullmatch(r"/([A-Za-z0-9_]{1,15})/status/([1-9][0-9]{0,19})/?", parts.path)
    if not match or match[1].lower() != username or int(match[2]) > 9_223_372_036_854_775_807:
        return None
    return f"https://x.com/{username}/status/{match[2]}"


def document(data, *, keep_timeline_data=False):
    try:
        root = html.fromstring(data, parser=html.HTMLParser(no_network=True))
    except (etree.ParserError, ValueError) as exc:
        raise ValueError("免费 X 来源未返回有效页面") from exc
    if root.tag in ("script", "style", "noscript"):
        root.clear()
    for node in root.xpath("//script|//style|//noscript"):
        if keep_timeline_data and node.tag == "script" and node.get("id") == "__NEXT_DATA__" and node.get("type") == "application/json":
            continue
        node.drop_tree()
    return root


def paragraphs(node):
    for br in node.xpath(".//br"):
        br.tail = "\n" + (br.tail or "")
    text = node.text_content()
    if len(text) > 60000:
        raise ValueError("X 动态文字超过读取上限")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    result = [piece for line in lines if line for piece in textwrap.wrap(line, width=2500, break_on_hyphens=False)]
    if len(result) > 300:
        raise ValueError("X 动态段落超过读取上限")
    return result


def make_post(url, content, published, username, image=None):
    from .collector import _article
    if not url or not content:
        return None
    text = "\n".join(content)
    title = content[0][:180] + ("…" if len(content[0]) > 180 else "")
    article = _article(text_html.escape(title), url, text_html.escape(text), published, image)
    if article:
        article["content"] = {"paragraphs": content, "author": "@" + username}
    return article


def embedded_posts(root, username):
    scripts = root.xpath('//script[@id="__NEXT_DATA__" and @type="application/json"]/text()')
    if not scripts:
        return None
    try:
        payload = json.loads(scripts[0])
        entries = payload["props"]["pageProps"]["timeline"]["entries"]
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        raise ValueError("X 公开页面未返回有效动态数据") from exc
    if not isinstance(entries, list) or len(entries) > 200:
        raise ValueError("X 公开页面动态数量无效")
    articles = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "tweet":
            continue
        content = entry.get("content")
        tweet = content.get("tweet") if isinstance(content, dict) else None
        if not isinstance(tweet, dict):
            continue
        user = tweet.get("user")
        if not isinstance(user, dict):
            continue
        screen_name = user.get("screen_name")
        if not isinstance(screen_name, str) or screen_name.lower() != username or user.get("protected"):
            continue
        post_id = tweet.get("id_str")
        text = tweet.get("full_text") or tweet.get("text")
        if not isinstance(post_id, str) or not isinstance(text, str) or not text.strip():
            continue
        url = post_url(f"https://x.com/{username}/status/{post_id}", username, "https://x.com/")
        permalink = tweet.get("permalink")
        if not url or url in seen or (permalink and (not isinstance(permalink, str) or post_url(permalink, username, "https://x.com/") != url)):
            continue
        # Treat JSON text as text; never render or execute the page's scripts.
        body = paragraphs(document(text_html.escape(text_html.unescape(text))))
        published = None
        created = tweet.get("created_at")
        if isinstance(created, str):
            try:
                published = datetime.strptime(created, "%a %b %d %H:%M:%S %z %Y").isoformat()
            except ValueError:
                pass
        article = make_post(url, body, published, username)
        if article:
            articles.append(article)
            seen.add(url)
    return articles


def parse_x(data, source_url, base_url=None):
    from .collector import MAX_BYTES, _child_text, _local
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("免费 X 来源超过 2 MB 上限")
    data = data.removeprefix(b"\xef\xbb\xbf")
    username = urlsplit(account_url(source_url)).path.strip("/")
    base_url = base_url or mirror_urls()[0] + "/" + username
    articles = []
    seen = set()
    if re.match(rb"\s*(?:<\?xml\b|<rss\b|<feed\b|<rdf:RDF\b)", data, re.I):
        if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            raise ValueError("拒绝包含实体定义的 XML")
        root = ET.fromstring(data)
        if _local(root.tag) not in ("rss", "feed", "rdf"):
            raise ValueError("免费 X 来源未返回有效订阅")
        channel = next((node for node in root if _local(node.tag) == "channel"), root)
        for entry in channel:
            if _local(entry.tag) not in ("item", "entry"):
                continue
            links = [child.get("href") or (child.text or "") for child in entry
                     if _local(child.tag) == "link" and child.get("rel", "alternate") == "alternate"]
            url = next((valid for link in links if (valid := post_url(link.strip(), username, base_url))), None)
            if not url or url in seen:
                continue
            body = _child_text(entry, {"description", "summary", "encoded", "content"})
            body_root = document(body) if body.strip() else None
            content = paragraphs(body_root) if body_root is not None else []
            image = None
            if body_root is not None:
                pictures = body_root.xpath("//img/@src")
                image = urljoin(base_url, pictures[0]) if pictures else None
            article = make_post(url, content, _child_text(entry, {"pubdate", "published", "date", "updated"}), username, image)
            if article:
                articles.append(article); seen.add(url)
            if len(articles) >= 100:
                break
    else:
        root = document(data, keep_timeline_data=True)
        embedded = embedded_posts(root, username)
        if embedded is not None:
            articles = embedded
        timeline_nodes = [] if embedded is not None else root.xpath("//*[contains(concat(' ',normalize-space(@class),' '),' timeline-item ')]")
        for node in timeline_nodes[:200]:
            if node.get("data-username", "").lower() != username:
                continue
            bodies = node.xpath(".//*[contains(concat(' ',normalize-space(@class),' '),' tweet-content ')]")
            links = node.xpath("./a[contains(concat(' ',normalize-space(@class),' '),' tweet-link ')]/@href")
            if not bodies or not links:
                continue
            url = post_url(links[0], username, base_url)
            if not url or url in seen:
                continue
            dates = node.xpath(".//*[contains(concat(' ',normalize-space(@class),' '),' tweet-header ')]//*[contains(concat(' ',normalize-space(@class),' '),' tweet-date ')]/a/@title")
            pictures = node.xpath(".//a[contains(concat(' ',normalize-space(@class),' '),' still-image ')]/@href")
            image = urljoin(base_url, pictures[0]) if pictures else None
            if image and urlsplit(image).path.startswith("/pic/"):
                candidate = unquote(urlsplit(image).path[5:])
                image = candidate if candidate.startswith("https://") else None
            article = make_post(url, paragraphs(bodies[0]), dates[0] if dates else None, username, image)
            if article:
                articles.append(article); seen.add(url)
    if not articles:
        raise ValueError("免费 X 来源暂无可读取的该账号动态，请稍后重试")
    # Snowflake IDs retain the true posting time when a mirror uses a display-only date.
    for article in articles:
        if article["published_at"] is None:
            post_id = int(urlsplit(article["url"]).path.rsplit("/", 1)[-1])
            milliseconds = (post_id >> 22) + 1288834974657
            article["published_at"] = datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat()
    return articles


async def fetch_x(source_url, fetcher=None):
    from .collector import fetch_public
    fetcher = fetcher or fetch_public
    semaphore = asyncio.Semaphore(2)

    async def attempt(route):
        async with semaphore:
            try:
                async with asyncio.timeout(7):
                    data, base_url = await fetcher(route["url"])
                articles = parse_x(data, source_url, base_url)
                return articles, data
            except (OSError, TimeoutError, ValueError, ET.ParseError, httpx.HTTPError):
                return None

    tasks = [asyncio.create_task(attempt(route)) for route in source_routes(source_url)]
    try:
        async with asyncio.timeout(25):
            for result in asyncio.as_completed(tasks):
                if (value := await result) is not None:
                    return value
    except TimeoutError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    raise ValueError("免费 X 来源暂时不可用，已保存的动态不受影响，可稍后重试")
