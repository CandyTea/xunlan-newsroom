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
from .dns import dns_mode, resolve_https
from .models import public_url_syntax

logger = logging.getLogger(__name__)
MAX_BYTES = 2 * 1024 * 1024
FETCH_BUDGET = 70
FETCH_ATTEMPT_TIMEOUT = 25
FETCH_ATTEMPTS = 4
RETRY_STATUSES = {429, 502, 503, 504}


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
    watch.pop("reader_id", None)
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
        for article in conn.execute("SELECT id,title,summary,category FROM articles WHERE reader_id='public' OR reader_id=?",
                                    (row["reader_id"],)):
            if matches_watch(dict(article), watch, catalog):
                conn.execute("INSERT INTO article_watches(article_id,watch_id) VALUES(?,?)", (article["id"], watch_id))


async def resolve_public_url(url, *, ip_index=0):
    """Validate every DNS answer and return an IP-pinned URL plus TLS/Host identity."""
    url = public_url_syntax(url)
    parts = urlsplit(url)
    hostname = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    loop = asyncio.get_running_loop()
    mode = dns_mode()
    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        addresses = [str(literal_address)]
    elif mode == "https":
        fetch_mode, proxy = fetch_config()
        addresses = await resolve_https(hostname, proxy, allow_direct=fetch_mode == "auto")
    else:
        try:
            answers = await asyncio.wait_for(loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM), timeout=5)
            addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
            if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
                raise ValueError("来源域名解析到了非公网地址，已拒绝访问")
        except (OSError, TimeoutError, ValueError):
            if mode == "system":
                raise
            fetch_mode, proxy = fetch_config()
            addresses = await resolve_https(hostname, proxy, allow_direct=fetch_mode == "auto")
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("来源域名解析到了非公网地址，已拒绝访问")
    ipv4 = [address for address in addresses if ":" not in address]
    addresses = ipv4 or addresses
    address = addresses[ip_index % len(addresses)]
    address = f"[{address}]" if ":" in address else address
    pinned = urlunsplit((parts.scheme, f"{address}:{port}", parts.path, parts.query, ""))
    return url, pinned, parts.netloc, hostname


def fetch_config():
    dns_mode()
    mode = os.getenv("NEWSROOM_FETCH_MODE", "auto").strip().lower()
    if mode not in {"auto", "direct", "proxy"}:
        raise ValueError("NEWSROOM_FETCH_MODE 必须为 auto、direct 或 proxy")
    if mode == "direct":
        return mode, None
    proxy = os.getenv("NEWSROOM_OUTBOUND_PROXY", "").strip() or None
    if mode == "proxy" and not proxy:
        raise ValueError("proxy 模式需要配置 NEWSROOM_OUTBOUND_PROXY")
    if proxy:
        try:
            parts = urlsplit(proxy)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.port == 0:
                raise ValueError()
            httpx.Proxy(proxy)
        except (ValueError, httpx.InvalidURL):
            raise ValueError("NEWSROOM_OUTBOUND_PROXY 必须为有效的 HTTP(S) 代理地址") from None
    return mode, proxy


def retry_delay(response, attempt):
    delay = 0.5 * 2 ** attempt
    if response is None:
        return delay
    retry_after = response.headers.get("retry-after", "").strip()
    if retry_after.isdigit():
        return max(delay, float(retry_after))
    if retry_after:
        try:
            retry_at = parsedate_to_datetime(retry_after)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(delay, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    return delay


async def fetch_public(url):
    mode, proxy = fetch_config()
    routes = [proxy, None] if mode == "auto" and proxy else [proxy]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + FETCH_BUDGET
    redirects = 0
    async with asyncio.timeout(FETCH_BUDGET):
        for attempt in range(FETCH_ATTEMPTS):
            try:
                async with asyncio.timeout(FETCH_ATTEMPT_TIMEOUT):
                    while True:
                        original, pinned, host, hostname = await resolve_public_url(url, ip_index=attempt // len(routes))

                        async def tls_identity(event, info):
                            # httpcore 1.0.9's CONNECT transport omits sni_hostname.
                            # The hook keeps normal certificate checks for the origin.
                            if event == "proxy.start_tls.started":
                                info["server_hostname"] = hostname

                        # Separate pools retain each redirect's TLS identity, even
                        # when different hostnames resolve to the same public IP.
                        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10), trust_env=False,
                                                     follow_redirects=False, proxy=routes[attempt % len(routes)]) as client:
                            user_agent = "Mozilla/5.0" if hostname in ("www.dongqiudi.com", "pc.dongqiudi.com", "m.dongqiudi.com") else "Newsroom/1.0 (personal RSS reader)"
                            async with client.stream("GET", pinned,
                                                     headers={"Host": host, "User-Agent": user_agent,
                                                              "Accept": "application/rss+xml,application/atom+xml,application/json,text/xml,*/*"},
                                                     extensions={"sni_hostname": hostname, "trace": tls_identity}) as response:
                                if response.status_code in (301, 302, 303, 307, 308):
                                    location = response.headers.get("location")
                                    if not location:
                                        raise ValueError("来源返回了不含目标地址的跳转")
                                    redirects += 1
                                    if redirects >= 6:
                                        raise ValueError("来源跳转次数超过上限")
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
            except (TimeoutError, httpx.TimeoutException, httpx.ConnectError, httpx.ProxyError,
                    httpx.RemoteProtocolError, httpx.HTTPStatusError, socket.gaierror) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in RETRY_STATUSES:
                    raise
                if isinstance(exc, socket.gaierror) and exc.errno != socket.EAI_AGAIN:
                    raise
                delay = retry_delay(exc.response if isinstance(exc, httpx.HTTPStatusError) else None, attempt)
                if attempt == FETCH_ATTEMPTS - 1 or delay >= deadline - loop.time():
                    raise
            await asyncio.sleep(delay)


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


def parse_source(data, source, base_url=None):
    if source["kind"] == "steam":
        return parse_steam(data, source["steam_appid"])
    if source["kind"] == "telegram":
        from .telegram import parse_telegram
        return parse_telegram(data, source["url"])
    if source["kind"] == "dongqiudi":
        from .dongqiudi import list_page
        return list_page(data)[0]
    return parse_rss(data, base_url or source["url"])


def safe_error(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        return f"来源返回 HTTP {exc.response.status_code}"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "来源请求超时"
    if isinstance(exc, (httpx.ConnectError, socket.gaierror)):
        return "无法连接来源，请检查域名、网络或服务器出口"
    if isinstance(exc, httpx.ProxyError):
        return "无法通过代理连接来源，请检查代理服务或出口设置"
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
        self.dqd_lock = asyncio.Lock()
        self.dqd_refreshed = {}

    @property
    def fetching(self):
        return self.task is not None and not self.task.done()

    async def start(self, categories, trigger="manual", slots=None, failed_only=False, skip_source_ids=()):
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
            self.task = asyncio.create_task(self.collect(run_id, categories, failed_only=failed_only, skip_source_ids=skip_source_ids))
            return True, run_id

    def persist_articles(self, source, articles, reader_id="public"):
        count = 0
        with self.db.connection(write=True) as conn:
            current = conn.execute("SELECT enabled,media_id,kind,url FROM sources WHERE id=?", (source["id"],)).fetchone()
            if current is None or not current["enabled"] or current["kind"] != source["kind"] or current["url"] != source["url"]:
                return 0
            watches = [watch_from_row(row) for row in conn.execute(
                "SELECT * FROM watches WHERE enabled=1 AND (?='public' OR reader_id=?)", (reader_id, reader_id))]
            for article in articles:
                article = {**article, "category": source["category"]}
                canonical = article["canonical_url"]
                if reader_id != "public":
                    if conn.execute("SELECT 1 FROM articles WHERE canonical_url=? AND reader_id='public'", (canonical,)).fetchone():
                        continue
                    canonical = reader_id + ":" + canonical
                inserted = conn.execute("""INSERT OR IGNORE INTO articles(canonical_url,title,url,summary,source_id,
                    source_name,category,published_at,fetched_at,image_url,reader_id,media_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (canonical, article["title"], article["url"], article["summary"], source["id"],
                     source["name"], source["category"], article["published_at"], utc_now(), article["image_url"], reader_id, current["media_id"]))
                if not inserted.rowcount:
                    if source["kind"] == "dongqiudi" and article.get("dqd", {}).get("complete"):
                        existing = conn.execute("SELECT id FROM articles WHERE canonical_url=?", (canonical,)).fetchone()
                        if existing:
                            self.persist_dqd(conn, existing["id"], article)
                    continue
                count += 1
                content = article.get("content")
                if source["kind"] == "telegram" and content:
                    conn.execute("INSERT INTO article_content(article_id,source_url,paragraphs,author,fetched_at) VALUES(?,?,?,?,?)",
                                 (inserted.lastrowid, article["url"], json.dumps(content["paragraphs"], ensure_ascii=False),
                                  content["author"], utc_now()))
                if source["kind"] == "dongqiudi":
                    self.persist_dqd(conn, inserted.lastrowid, article)
                else:
                    conn.executemany("INSERT INTO article_topics(article_id,topic_id) VALUES(?,?)",
                                     [(inserted.lastrowid, topic) for topic in article_topics(article, self.db.catalog)])
                if source["kind"] != "dongqiudi" or not article["dqd"]["complete"]:
                    for watch in watches:
                        if matches_watch(article, watch, self.db.catalog):
                            conn.execute("INSERT INTO article_watches(article_id,watch_id) VALUES(?,?)", (inserted.lastrowid, watch["id"]))
            if reader_id == "public":
                conn.execute("UPDATE sources SET last_success_at=?,last_error=NULL WHERE id=?", (utc_now(), source["id"]))
        return count

    def persist_dqd(self, conn, article_id, article):
        metadata = article["dqd"]
        conn.execute("INSERT INTO dongqiudi_articles(article_id,author,complete) VALUES(?,?,?) ON CONFLICT(article_id) DO UPDATE SET author=excluded.author,complete=excluded.complete",
                     (article_id, metadata["author"], int(metadata["complete"])))
        if not metadata["complete"]:
            return
        conn.execute("UPDATE articles SET title=?,summary=?,published_at=COALESCE(?,published_at),image_url=COALESCE(?,image_url) WHERE id=?",
                     (article["title"], article["summary"], article["published_at"], article["image_url"], article_id))
        conn.execute("DELETE FROM dongqiudi_tags WHERE article_id=?", (article_id,))
        conn.executemany("INSERT INTO dongqiudi_tags(article_id,kind,tag_id,name) VALUES(?,?,?,?)",
                         [(article_id, tag["kind"], tag["id"], tag["name"]) for tag in metadata["tags"]])
        content = article.get("content")
        if content:
            conn.execute("INSERT INTO article_content(article_id,source_url,paragraphs,author,fetched_at) VALUES(?,?,?,?,?) ON CONFLICT(article_id) DO NOTHING",
                         (article_id, article["url"], json.dumps(content["paragraphs"], ensure_ascii=False), content["author"], utc_now()))
        conn.execute("DELETE FROM article_topics WHERE article_id=?", (article_id,))
        conn.executemany("INSERT OR IGNORE INTO article_topics(article_id,topic_id) VALUES(?,?)",
                         [(article_id, topic) for topic in article_topics({**article, "category": "sports"}, self.db.catalog)])
        scope = conn.execute("SELECT reader_id FROM articles WHERE id=?", (article_id,)).fetchone()["reader_id"]
        for row in conn.execute("SELECT * FROM watches WHERE enabled=1 AND (?='public' OR reader_id=?)", (scope, scope)).fetchall():
            watch = watch_from_row(row)
            if matches_watch({**article, "category": "sports"}, watch, self.db.catalog):
                conn.execute("INSERT OR IGNORE INTO article_watches(article_id,watch_id) VALUES(?,?)", (article_id, watch["id"]))

    def dqd_has_more(self, source):
        with self.db.connection() as conn:
            row = conn.execute("SELECT source_url,exhausted FROM dongqiudi_history WHERE source_id=?", (source["id"],)).fetchone()
        return not row or row["source_url"] != source["url"] or not row["exhausted"]

    def persist_dqd_page(self, source, articles, cursor, advance):
        # Save articles first. An interrupted cursor write can only repeat a page.
        with self.db.lock:
            with self.db.connection() as conn:
                current = conn.execute("SELECT kind,url,enabled FROM sources WHERE id=?", (source["id"],)).fetchone()
            if not current or not current["enabled"] or current["kind"] != "dongqiudi" or current["url"] != source["url"]:
                raise ValueError("懂球帝来源已变更，请重新更新资讯")
            count = self.persist_articles(source, articles)
            if advance:
                with self.db.connection(write=True) as conn:
                    conn.execute("""INSERT INTO dongqiudi_history(source_id,source_url,after_id,page,exhausted,updated_at)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                        source_url=excluded.source_url,after_id=excluded.after_id,page=excluded.page,
                        exhausted=excluded.exhausted,updated_at=excluded.updated_at""",
                                 (source["id"], source["url"], cursor["after"] if cursor else None,
                                  cursor["page"] if cursor else None, int(cursor is None), utc_now()))
            return count

    async def collect_dongqiudi(self, source, *, older=False):
        from .dongqiudi import ARTICLE_URL, DETAIL_URL, HOME_URL, LIST_URL, RestrictedArticle, detail_page, list_page, next_list_url, public_article_id

        async with self.dqd_lock:
            now = asyncio.get_running_loop().time()
            key = source["id"]
            self.dqd_refreshed = {k: v for k, v in self.dqd_refreshed.items() if now - v < 60}
            with self.db.connection() as conn:
                current = conn.execute("SELECT * FROM sources WHERE id=? AND enabled=1 AND kind='dongqiudi'", (source["id"],)).fetchone()
                progress = conn.execute("SELECT * FROM dongqiudi_history WHERE source_id=?", (source["id"],)).fetchone()
            if current is None:
                raise ValueError("懂球帝来源已停用，请稍后再试")
            source = dict(current)
            if progress and progress["source_url"] != source["url"]:
                progress = None
            if older and progress and progress["exhausted"]:
                return {"new_count": 0, "partial": False, "recent": False, "has_more": False}
            if not older and key in self.dqd_refreshed:
                return {"new_count": 0, "partial": False, "recent": True, "has_more": self.dqd_has_more(source)}
            cursor = {"after": progress["after_id"], "page": progress["page"]} if older and progress else None
            advance = older or progress is None
            items = []
            failed = 0
            count = 0
            fetched_pages = 0
            async with asyncio.timeout(70):
                try:
                    async with asyncio.timeout(24):
                        for _ in range(4 if older else 8):
                            try:
                                async with asyncio.timeout(8):
                                    data, _ = await self.fetcher(next_list_url(cursor) if cursor else LIST_URL)
                                    batch, following = list_page(data, allow_empty=True, require_json=True)
                            except (ValueError, OSError, httpx.HTTPError, TimeoutError):
                                if older or fetched_pages:
                                    raise
                                async with asyncio.timeout(8):
                                    data, _ = await self.fetcher(HOME_URL)
                                    batch, _ = list_page(data)
                                count += self.persist_dqd_page(source, batch, None, False)
                                items.extend(batch)
                                fetched_pages += 1
                                failed += 1
                                break
                            if cursor and following and (following["after"] >= cursor["after"] or following["page"] <= cursor["page"]):
                                raise ValueError("懂球帝未返回更早新闻，已保留原来的读取位置")
                            if not advance and progress and not progress["exhausted"] and (following is None or following["after"] <= progress["after_id"]):
                                advance = True
                            count += self.persist_dqd_page(source, batch, following, advance)
                            items.extend(batch)
                            fetched_pages += 1
                            cursor = following
                            if cursor is None:
                                break
                except (ValueError, OSError, httpx.HTTPError, TimeoutError):
                    if not fetched_pages:
                        raise
                    failed += 1
                items = list({item["url"]: item for item in items}.values())
                with self.db.connection() as conn:
                    placeholders = ",".join("?" for _ in items)
                    known = {row["url"] for row in conn.execute("""SELECT a.url FROM articles a
                        JOIN dongqiudi_articles d ON d.article_id=a.id
                        WHERE d.complete=1 AND a.reader_id='public'
                        AND a.url IN (""" + placeholders + ")", [item["url"] for item in items])} if items else set()
                    backlog = [dict(row) for row in conn.execute("""SELECT a.url FROM articles a
                        JOIN dongqiudi_articles d ON d.article_id=a.id WHERE a.source_id=?
                        AND a.reader_id='public' AND d.complete=0
                        ORDER BY COALESCE(a.published_at,a.fetched_at) DESC,a.id DESC LIMIT 20""", (source["id"],))]
                pending_urls = list(dict.fromkeys([item["url"] for item in items if item["url"] not in known] + [row["url"] for row in backlog]))
                if len(pending_urls) > 60:
                    failed += 1
                pending_urls = pending_urls[:60]
                slots = asyncio.Semaphore(3)

                async def enrich(url):
                    nonlocal failed
                    async with slots:
                        article_id = public_article_id(url)
                        try:
                            async with asyncio.timeout(14):
                                try:
                                    async with asyncio.timeout(7):
                                        data, _ = await self.fetcher(DETAIL_URL + article_id)
                                        full = detail_page(data, article_id)
                                except RestrictedArticle:
                                    raise
                                except (ValueError, OSError, httpx.HTTPError, TimeoutError):
                                    data, _ = await self.fetcher(ARTICLE_URL.format(article_id))
                                    full = detail_page(data, article_id)
                            self.persist_articles(source, [full])
                        except (ValueError, OSError, httpx.HTTPError, TimeoutError):
                            failed += 1
                try:
                    async with asyncio.timeout(35):
                        async with asyncio.TaskGroup() as group:
                            for url in pending_urls:
                                group.create_task(enrich(url))
                except TimeoutError:
                    failed += 1
            if not older:
                self.dqd_refreshed[key] = asyncio.get_running_loop().time()
            return {"new_count": count, "partial": bool(failed), "recent": False, "has_more": self.dqd_has_more(source)}

    async def collect(self, run_id, categories, *, failed_only=False, skip_source_ids=()):
        new_count = 0
        errors = []
        attempted = 0
        try:
            with self.db.connection() as conn:
                sources = [dict(row) for row in conn.execute("SELECT * FROM sources WHERE enabled=1")
                           if row["category"] in categories and row["id"] not in skip_source_ids]
            semaphore = asyncio.Semaphore(3)

            async def check_source(source):
                async with semaphore:
                    await collect_source(source)

            async def collect_source(source):
                nonlocal attempted, new_count
                # Re-read so disabling/deleting a pending source takes effect immediately.
                with self.db.connection() as conn:
                    current = conn.execute("SELECT * FROM sources WHERE id=? AND enabled=1", (source["id"],)).fetchone()
                if current is None:
                    return
                source = dict(current)
                if source["category"] not in categories or (failed_only and not source["last_error"]):
                    return
                attempted += 1
                try:
                    if source["kind"] == "dongqiudi":
                        result = await self.collect_dongqiudi(source)
                        new_count += result["new_count"]
                        return
                    data, base_url = await self.fetcher(source["url"])
                    articles = parse_source(data, source, base_url)
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
            async with asyncio.TaskGroup() as group:
                for source in sources:
                    group.create_task(check_source(source))
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
