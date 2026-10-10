"""Continue public Telegram previews from a reader's oldest saved message."""
import asyncio
import re
from urllib.parse import urlsplit

import httpx

from .db import utc_now
from .telegram import channel_url, parse_telegram_page


class HistoryError(ValueError):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


class TelegramHistory:
    def __init__(self, db, collector):
        self.db = db
        self.collector = collector
        self.semaphore = asyncio.Semaphore(2)

    def progress(self, conn, source, scope):
        row = conn.execute("SELECT * FROM telegram_history WHERE reader_id=? AND source_id=?", (scope, source["id"])).fetchone()
        channel = urlsplit(channel_url(source["url"])).path.rsplit("/", 1)[-1]
        pattern = re.compile(r"https://t\.me/" + re.escape(channel) + r"/([1-9][0-9]{0,18})", re.I)
        ids = []
        for article in conn.execute("SELECT url FROM articles WHERE source_id=? AND (reader_id='public' OR reader_id=?)", (source["id"], scope)):
            match = pattern.fullmatch(article["url"])
            if match:
                ids.append(int(match[1]))
        before = min(ids) if ids else None
        if row and row["source_url"] == source["url"]:
            saved = row["before_id"]
            if saved is not None:
                before = min(before, saved) if before is not None else saved
                return before, before == 1 or (bool(row["exhausted"]) and before == saved)
            if before is None:
                return None, bool(row["exhausted"])
        return before, before == 1

    def sources(self, media_id, scope):
        with self.db.connection() as conn:
            items = []
            for row in conn.execute("SELECT * FROM sources WHERE media_id=? AND enabled=1 AND kind='telegram' ORDER BY id", (media_id,)).fetchall():
                before, exhausted = self.progress(conn, row, scope)
                items.append({"id": row["id"], "name": row["name"], "url": row["url"], "before": before,
                              "exhausted": exhausted, "fetch_url": row["url"] + (f"?before={before}" if before else "")})
            return items

    def source(self, conn, media_id, scope, body):
        source = conn.execute("SELECT * FROM sources WHERE id=? AND media_id=? AND enabled=1 AND kind='telegram'",
                              (body.source_id, media_id)).fetchone()
        if source is None:
            raise HistoryError("此频道已停用或媒体归属改变", 409)
        before, exhausted = self.progress(conn, source, scope)
        if body.before != before:
            raise HistoryError("历史进度已更新，请重试加载", 409)
        if exhausted:
            raise HistoryError("已到频道可读取历史的起点", 409)
        return dict(source)

    def save(self, media_id, scope, body, content, expected_url=None):
        with self.db.connection() as conn:
            source = self.source(conn, media_id, scope, body)
        expected_url = expected_url or getattr(body, "source_url", source["url"])
        if expected_url != source["url"]:
            raise HistoryError("频道地址已改变，请重新加载", 409)
        try:
            page = parse_telegram_page(content, source["url"])
        except ValueError as exc:
            raise HistoryError("来源未返回有效的频道历史页面") from exc
        ids = [value for value in page["message_ids"] if body.before is None or value < body.before]
        links = [value for value in page["older"] if body.before is None or value < body.before]
        if not ids and page["message_ids"]:
            raise HistoryError("频道未返回更早的消息，请稍后重试")
        before = min(ids) if ids else min(links) if links else body.before
        if before is not None and before > 9_223_372_036_854_775_807:
            raise HistoryError("频道历史消息编号无效")
        exhausted = before == 1 or (not ids and not links)
        articles = [article for article in page["articles"]
                    if body.before is None or int(article["url"].rsplit("/", 1)[-1]) < body.before]
        with self.db.lock:
            with self.db.connection() as conn:
                source = self.source(conn, media_id, scope, body)
                if source["url"] != expected_url:
                    raise HistoryError("频道地址已改变，请重新加载", 409)
            count = self.collector.persist_articles(source, articles, "public" if scope == "owner" else scope)
            with self.db.connection(write=True) as conn:
                conn.execute("""INSERT INTO telegram_history(reader_id,source_id,source_url,before_id,exhausted,updated_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(reader_id,source_id) DO UPDATE SET
                    source_url=excluded.source_url,before_id=excluded.before_id,exhausted=excluded.exhausted,updated_at=excluded.updated_at""",
                    (scope, source["id"], source["url"], before, int(exhausted), utc_now()))
        return {"source_id": source["id"], "new_count": count, "item_count": len(articles), "before": before, "exhausted": exhausted}

    async def fetch(self, media_id, scope, body):
        with self.db.connection() as conn:
            source = self.source(conn, media_id, scope, body)
        url = source["url"] + (f"?before={body.before}" if body.before else "")
        async with self.semaphore:
            try:
                async with asyncio.timeout(12):
                    data, _ = await self.collector.fetcher(url)
            except (TimeoutError, OSError, ValueError, httpx.HTTPError) as exc:
                raise HistoryError("暂时无法加载更早消息，请稍后重试", 503) from exc
        return self.save(media_id, scope, body, data, source["url"])
