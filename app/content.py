"""Extract readable paragraphs from public article pages and cache them privately."""
import asyncio
import json
import re
import textwrap

import httpx
from trafilatura import extract

from .collector import MAX_BYTES, canonical_url, fetch_public
from .db import utc_now

MAX_CONTENT_CHARS = 60000


class ContentError(Exception):
    pass


def extract_content(html, article):
    raw = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    if (len(html) if isinstance(html, bytes) else len(raw.encode("utf-8"))) > MAX_BYTES:
        raise ContentError("来源页面超过大小上限，暂时无法在站内显示。")
    if re.search(r'"isAccessibleForFree"\s*:\s*(?:false|"false")', raw, re.I):
        raise ContentError("来源标记此报道需要订阅，请前往来源网站阅读。")
    result = extract(html, url=article["url"], output_format="json", with_metadata=True,
                     include_comments=False, include_tables=True, favor_precision=True,
                     max_tree_size=30000)
    if not result:
        raise ContentError("来源没有提供可提取的正文，可能需要登录、订阅或加载动态内容。")
    data = json.loads(result)
    title = (data.get("title") or "").strip()
    if re.search(r"^(?:access denied|just a moment|verify you are human|page not found|error 40[134]|robot check)", title, re.I):
        raise ContentError("来源暂时不允许读取正文，请稍后重试或前往来源网站。")
    text = data.get("text") or ""
    if len(text) < 150:
        raise ContentError("来源只返回了短摘要或提示信息，尚未取得正文。")
    if len(text) > MAX_CONTENT_CHARS:
        raise ContentError("这篇报道超过站内正文长度上限，请前往来源网站阅读。")
    paragraphs = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if line:
            paragraphs.extend(textwrap.wrap(line, width=2500, break_on_hyphens=False))
    if not paragraphs or len(paragraphs) > 300:
        raise ContentError("来源没有提供可显示的正文段落。")
    summary = " ".join(article["summary"].split())
    joined = " ".join(paragraphs)
    if summary and joined == summary:
        raise ContentError("来源仅返回已有摘要，尚未取得正文。")
    return {"paragraphs": paragraphs, "author": (data.get("author") or "")[:200], "fetched_at": utc_now()}


class ArticleReader:
    def __init__(self, db):
        self.db = db
        self.semaphore = asyncio.Semaphore(2)
        self.inflight = {}

    def cached(self, conn, article):
        row = conn.execute("SELECT paragraphs,author,fetched_at FROM article_content WHERE article_id=? AND source_url=?",
                           (article["id"], article["url"])).fetchone()
        return {"paragraphs": json.loads(row["paragraphs"]), "author": row["author"], "fetched_at": row["fetched_at"]} if row else None

    def store(self, article, content):
        with self.db.connection(write=True) as conn:
            row = conn.execute("SELECT url FROM articles WHERE id=?", (article["id"],)).fetchone()
            if row is None or row["url"] != article["url"]:
                raise ContentError("这条资讯已移除或链接已改变，请重新打开。")
            cached = self.cached(conn, article)
            if cached:
                return cached
            conn.execute("INSERT INTO article_content(article_id,source_url,paragraphs,author,fetched_at) VALUES(?,?,?,?,?) ON CONFLICT(article_id) DO UPDATE SET source_url=excluded.source_url,paragraphs=excluded.paragraphs,author=excluded.author,fetched_at=excluded.fetched_at",
                         (article["id"], article["url"], json.dumps(content["paragraphs"], ensure_ascii=False), content["author"], content["fetched_at"]))
        return content

    async def import_html(self, article, article_url, html):
        if canonical_url(article_url) != canonical_url(article["url"]):
            raise ContentError("正文链接与当前资讯不一致，未保存。")
        with self.db.connection() as conn:
            cached = self.cached(conn, article)
        if cached:
            return cached
        content = await asyncio.to_thread(extract_content, html, article)
        return self.store(article, content)

    async def read(self, article):
        with self.db.connection() as conn:
            cached = self.cached(conn, article)
        if cached:
            return cached
        key = (article["id"], article["url"])
        task = self.inflight.get(key)
        if task is None:
            if len(self.inflight) >= 12:
                raise ContentError("正文读取任务较多，请稍后再试。")
            task = asyncio.create_task(self.fetch(article))
            self.inflight[key] = task

            def finished(completed):
                self.inflight.pop(key, None)
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def fetch(self, article):
        async with self.semaphore:
            with self.db.connection() as conn:
                cached = self.cached(conn, article)
            if cached:
                return cached
            try:
                async with asyncio.timeout(20):
                    html, _ = await fetch_public(article["url"])
                content = await asyncio.to_thread(extract_content, html, article)
            except (TimeoutError, OSError, ValueError, httpx.HTTPError) as exc:
                raise ContentError("暂时无法取得来源正文，可以重试或前往来源网站阅读。") from exc
            return self.store(article, content)

    async def stop(self):
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
