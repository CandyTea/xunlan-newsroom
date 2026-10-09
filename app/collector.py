"""Public feed fetching, parsing, matching, and the shared collection coordinator."""
import asyncio
import html
import ipaddress
import json
import logging
import os
import re
import socket
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from .catalog import article_topics, term_matches, watch_team
from .db import utc_now
from .models import public_url_syntax

logger = logging.getLogger(__name__)
MAX_BYTES = 2 * 1024 * 1024


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("p", "br", "div", "li", "h1", "h2", "h3"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value, limit=1800):
    parser = PlainText()
    parser.feed(value or "")
    text = " ".join("".join(parser.parts).split())
    # Steam sometimes returns BBCode rather than HTML.
    text = re.sub(r"\[/?(?:url|img|b|i|u|h[1-6]|list|\*|quote)(?:=[^\]]*)?\]", "", text, flags=re.I)
    return html.unescape(text)[:limit]


def canonical_url(url):
    url = public_url_syntax(url)
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in ("fbclid", "gclid", "mc_cid", "mc_eid")]
    port = parts.port
    host = parts.hostname.lower()
    host = f"[{host}]" if ":" in host else host
    if port and not (parts.scheme == "https" and port == 443 or parts.scheme == "http" and port == 80):
        host += f":{port}"
    return urlunsplit((parts.scheme, host, parts.path or "/", urlencode(sorted(query)), ""))


def parse_date(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value.strip())
        except (ValueError, TypeError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def matches_watch(article, watch, catalog=None):
    if not watch["enabled"]:
        return False
    text = article["title"] + " " + article["summary"]
    if any(term_matches(text, term) for term in watch["exclude_keywords"]):
        return False
    league_id = watch.get("league_id", "")
    if (league_id or watch.get("type") == "team") and article.get("category") != "sports":
        return False
    names = [watch["name"], watch["ticker"], *watch["aliases"]]
    team = watch_team(watch, catalog)
    if team:
        names.extend([team["name"], *team.get("aliases", [])])
    if watch.get("type") == "league" and league_id:
        if catalog is None:
            raise ValueError("预设联赛匹配需要已加载的兴趣目录")
        matched = "league:" + league_id in article_topics(article, catalog)
    else:
        matched = any(term_matches(text, name) for name in names)
    return matched and all(term_matches(text, term) for term in watch["keywords"])


def watch_from_row(row):
    watch = dict(row)
    watch["enabled"] = bool(watch["enabled"])
    for key in ("aliases", "keywords", "exclude_keywords"):
        watch[key] = json.loads(watch[key])
    return watch


def rematch_watch(conn, watch_id, catalog=None):
    row = conn.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
    if row is None:
        return
    watch = watch_from_row(row)
    conn.execute("DELETE FROM article_watches WHERE watch_id=?", (watch_id,))
    if watch["enabled"]:
        for article in conn.execute("SELECT id,title,summary,category FROM articles"):
            if matches_watch(dict(article), watch, catalog):
                conn.execute("INSERT INTO article_watches(article_id,watch_id) VALUES(?,?)", (article["id"], watch_id))


async def resolve_public_url(url):
    """Validate every DNS answer and return an IP-pinned URL plus TLS/Host identity."""
    url = public_url_syntax(url)
    parts = urlsplit(url)
    hostname = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    loop = asyncio.get_running_loop()
    answers = await asyncio.wait_for(loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM), timeout=5)
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("来源域名解析到了非公网地址，已拒绝访问")
    address = next((address for address in addresses if ":" not in address), addresses[0])
    address = f"[{address}]" if ":" in address else address
    pinned = urlunsplit((parts.scheme, f"{address}:{port}", parts.path, parts.query, ""))
    return url, pinned, parts.netloc, hostname


async def fetch_public(url):
    async with asyncio.timeout(35):
        for _ in range(6):
            original, pinned, host, hostname = await resolve_public_url(url)

            async def tls_identity(event, info):
                # httpcore 1.0.9's CONNECT transport omits sni_hostname.
                # Its trace hook runs before TLS with the actual kwargs,
                # allowing the tunnel to retain normal certificate checks.
                if event == "proxy.start_tls.started":
                    info["server_hostname"] = hostname

            # A new pool per redirect keeps TLS identities separate when two
            # different hostnames happen to resolve to the same public IP.
            async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=8), trust_env=False, follow_redirects=False,
                                         proxy=os.getenv("NEWSROOM_OUTBOUND_PROXY") or None) as client:
                async with client.stream("GET", pinned, headers={"Host": host, "User-Agent": "Newsroom/1.0 (personal RSS reader)",
                                                               "Accept": "application/rss+xml,application/atom+xml,application/json,text/xml,*/*"},
                                         extensions={"sni_hostname": hostname, "trace": tls_identity}) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("来源返回了不含目标地址的跳转")
                        url = urljoin(original, location)
                        continue
                    response.raise_for_status()
                    length = response.headers.get("content-length", "")
                    if length.isdigit() and int(length) > MAX_BYTES:
                        raise ValueError("来源响应超过 2 MB 上限")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise ValueError("来源响应超过 2 MB 上限")
                    return bytes(data), original
        raise ValueError("来源跳转次数超过上限")


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element, names):
    for child in element:
        if _local(child.tag) in names:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def _article(title, url, summary, published, image=None):
    try:
        url = public_url_syntax(url)
        canonical = canonical_url(url)
    except ValueError:
        return None
    if not plain_text(title, 500):
        return None
    if image:
        try:
            image = public_url_syntax(image)
        except ValueError:
            image = None
    return {"title": plain_text(title, 500), "url": url, "canonical_url": canonical,
            "summary": plain_text(summary), "published_at": parse_date(published), "image_url": image}


def parse_rss(data, base_url):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("拒绝包含实体定义的 XML")
    class FeedTreeBuilder(ET.TreeBuilder):
        def doctype(self, name, pubid, system):
            raise ValueError("拒绝包含文档类型或实体定义的 XML")
    root = ET.fromstring(data, parser=ET.XMLParser(target=FeedTreeBuilder()))
    if _local(root.tag) not in ("rss", "feed", "rdf"):
        raise ValueError("来源返回的内容不是 RSS 或 Atom")
    articles = []
    for element in root.iter():
        if _local(element.tag) not in ("item", "entry"):
            continue
        link = ""
        image = None
        for child in element:
            tag = _local(child.tag)
            if tag == "link":
                href = child.attrib.get("href")
                if href and child.attrib.get("rel", "alternate") == "alternate":
                    link = href
                elif not href and child.text:
                    link = child.text.strip()
            if tag in ("thumbnail", "content", "enclosure"):
                candidate = child.attrib.get("url")
                if candidate and (tag == "thumbnail" or child.attrib.get("type", "").startswith("image/") or child.attrib.get("medium") == "image"):
                    image = urljoin(base_url, candidate)
        if not link:
            guid = _child_text(element, {"guid"})
            if guid.startswith(("https://", "http://")):
                link = guid
        if not link:
            continue
        item = _article(_child_text(element, {"title"}), urljoin(base_url, link),
                        _child_text(element, {"description", "summary", "encoded", "content"}),
                        _child_text(element, {"pubdate", "published", "date", "updated"}), image)
        if item:
            articles.append(item)
        if len(articles) >= 200:
            break
    return articles


def parse_steam(data, appid):
    payload = json.loads(data)
    news = payload.get("appnews")
    if not isinstance(news, dict) or not isinstance(news.get("newsitems"), list):
        raise ValueError("Steam 返回了无法识别的新闻结构")
    if str(news.get("appid")) != str(appid):
        raise ValueError("Steam 响应 App ID 与来源配置不一致")
    articles = []
    for entry in news["newsitems"][:200]:
        published = None
        try:
            published = datetime.fromtimestamp(float(entry.get("date", 0)), timezone.utc).isoformat() if entry.get("date") else None
        except (ValueError, TypeError, OverflowError, OSError):
            pass
        item = _article(str(entry.get("title", "")), str(entry.get("url", "")), str(entry.get("contents", "")), published)
        if item:
            articles.append(item)
    return articles


def safe_error(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        return f"来源返回 HTTP {exc.response.status_code}"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "来源请求超时"
    if isinstance(exc, (httpx.ConnectError, socket.gaierror)):
        return "无法连接来源，请检查域名、网络或服务器出口"
    if isinstance(exc, ET.ParseError):
        return "来源 XML 格式无效"
    if isinstance(exc, json.JSONDecodeError):
        return "来源 JSON 格式无效"
    if isinstance(exc, ValueError):
        return str(exc)[:240]
    return "来源采集失败，请查看服务器日志"


class Collector:
    def __init__(self, db, fetcher=fetch_public):
        self.db = db
        self.fetcher = fetcher
        self.lock = asyncio.Lock()
        self.task = None
        self.run_id = None

    @property
    def fetching(self):
        return self.task is not None and not self.task.done()

    async def start(self, categories, trigger="manual", slots=None):
        async with self.lock:
            if self.fetching:
                return False, self.run_id
            now = utc_now()
            with self.db.connection(write=True) as conn:
                claimed = []
                if slots is not None:
                    for slot in slots:
                        current = conn.execute("SELECT enabled,categories,created_at FROM schedules WHERE id=?", (slot["id"],)).fetchone()
                        if not current or not current["enabled"] or current["created_at"] >= slot["scheduled_at"]:
                            continue
                        slot = {**slot, "categories": json.loads(current["categories"])}
                        if conn.execute("INSERT OR IGNORE INTO schedule_slots(schedule_id,scheduled_at) VALUES(?,?)",
                                        (slot["id"], slot["scheduled_at"])).rowcount:
                            claimed.append(slot)
                    if not claimed:
                        return False, None
                    categories = sorted({category for slot in claimed for category in slot["categories"]})
                run_id = conn.execute("INSERT INTO runs(trigger,started_at,status) VALUES(?,?,'running')", (trigger, now)).lastrowid
                for slot in claimed:
                    conn.execute("UPDATE schedule_slots SET run_id=? WHERE schedule_id=? AND scheduled_at=?", (run_id, slot["id"], slot["scheduled_at"]))
                    conn.execute("UPDATE schedules SET last_run_at=? WHERE id=?", (now, slot["id"]))
            self.run_id = run_id
            self.task = asyncio.create_task(self.collect(run_id, categories))
            return True, run_id

    def persist_articles(self, source, articles):
        count = 0
        with self.db.connection(write=True) as conn:
            current = conn.execute("SELECT enabled FROM sources WHERE id=?", (source["id"],)).fetchone()
            if current is None or not current["enabled"]:
                return 0
            watches = [watch_from_row(row) for row in conn.execute("SELECT * FROM watches WHERE enabled=1")]
            for article in articles:
                article = {**article, "category": source["category"]}
                inserted = conn.execute("""INSERT OR IGNORE INTO articles(canonical_url,title,url,summary,source_id,
                    source_name,category,published_at,fetched_at,image_url) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (article["canonical_url"], article["title"], article["url"], article["summary"], source["id"],
                     source["name"], source["category"], article["published_at"], utc_now(), article["image_url"]))
                if not inserted.rowcount:
                    continue
                count += 1
                conn.executemany("INSERT INTO article_topics(article_id,topic_id) VALUES(?,?)",
                                 [(inserted.lastrowid, topic) for topic in article_topics(article, self.db.catalog)])
                for watch in watches:
                    if matches_watch(article, watch, self.db.catalog):
                        conn.execute("INSERT INTO article_watches(article_id,watch_id) VALUES(?,?)", (inserted.lastrowid, watch["id"]))
            conn.execute("UPDATE sources SET last_success_at=?,last_error=NULL WHERE id=?", (utc_now(), source["id"]))
        return count

    async def collect(self, run_id, categories):
        new_count = 0
        errors = []
        attempted = 0
        try:
            with self.db.connection() as conn:
                sources = [dict(row) for row in conn.execute("SELECT * FROM sources WHERE enabled=1") if row["category"] in categories]
            for source in sources:
                # Re-read so disabling/deleting a pending source takes effect immediately.
                with self.db.connection() as conn:
                    current = conn.execute("SELECT * FROM sources WHERE id=? AND enabled=1", (source["id"],)).fetchone()
                if current is None:
                    continue
                source = dict(current)
                attempted += 1
                try:
                    data, base_url = await self.fetcher(source["url"])
                    articles = parse_steam(data, source["steam_appid"]) if source["kind"] == "steam" else parse_rss(data, base_url)
                    with self.db.connection() as conn:
                        still_exists = conn.execute("SELECT 1 FROM sources WHERE id=?", (source["id"],)).fetchone()
                    if still_exists:
                        new_count += self.persist_articles(source, articles)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = safe_error(exc)
                    errors.append(f"{source['name']}: {error}")
                    with self.db.connection(write=True) as conn:
                        conn.execute("UPDATE sources SET last_error=? WHERE id=?", (error, source["id"]))
                    logger.warning("Feed %s failed (%s)", source["id"], type(exc).__name__)
            status = "partial" if errors and len(errors) < attempted else "failed" if errors else "success"
            with self.db.connection(write=True) as conn:
                conn.execute("UPDATE runs SET finished_at=?,status=?,new_count=?,source_count=?,error=? WHERE id=?",
                             (utc_now(), status, new_count, attempted, "；".join(errors)[:4000] or None, run_id))
                if attempted > len(errors):
                    conn.execute("INSERT INTO metadata(key,value) VALUES('last_success_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (utc_now(),))
        except asyncio.CancelledError:
            with self.db.connection(write=True) as conn:
                conn.execute("UPDATE runs SET finished_at=?,status='interrupted',new_count=?,source_count=?,error=? WHERE id=?",
                             (utc_now(), new_count, attempted, "服务关闭中断了采集", run_id))
            raise
        except Exception:
            logger.exception("Collection run %s failed", run_id)
            with self.db.connection(write=True) as conn:
                conn.execute("UPDATE runs SET finished_at=?,status='failed',new_count=?,source_count=?,error=? WHERE id=?",
                             (utc_now(), new_count, attempted, "采集内部错误，请查看服务器日志", run_id))

    async def stop(self):
        if self.fetching:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
