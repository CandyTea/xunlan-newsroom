"""Small SQLite store. Each operation owns its connection and transaction."""
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .catalog import load_catalog, reindex_topics


def utc_now():
    return datetime.now(timezone.utc).isoformat()


DEFAULT_SETTINGS = {"timezone": "Asia/Shanghai", "catch_up": True, "catch_up_hours": 4}


def default_media_name(source):
    host = (urlsplit(source["url"]).hostname or "").lower()
    if host == "espn.com" or host.endswith(".espn.com"):
        return "ESPN"
    if host in ("feeds.bbci.co.uk", "bbc.co.uk", "bbc.com") or host.endswith(".bbc.co.uk"):
        return "BBC"
    if host == "theguardian.com" or host.endswith(".theguardian.com"):
        return "The Guardian"
    if host == "t.me" and urlsplit(source["url"]).path.rstrip("/").lower() == "/s/fabrizioromanotg":
        return "罗马诺"
    return source["name"]


def ensure_media(conn, name):
    conn.execute("INSERT OR IGNORE INTO media(name) VALUES(?)", (name,))
    return conn.execute("SELECT id FROM media WHERE name=?", (name,)).fetchone()["id"]


def load_default_sources():
    from .models import Source
    path = Path(__file__).with_name("default_sources.json")
    if not path.exists():
        return []
    defaults = []
    seed_ids = set()
    for entry in json.loads(path.read_text(encoding="utf-8")):
        entry = dict(entry)
        seed_id = entry.pop("seed_id", None)
        if seed_id is not None:
            if (not isinstance(seed_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", seed_id)
                    or seed_id in seed_ids):
                raise ValueError("默认来源 seed_id 必须有效且唯一")
            seed_ids.add(seed_id)
        defaults.append((seed_id, Source(**entry).model_dump()))
    return defaults


def seed_default_sources(conn):
    seeded = conn.execute("SELECT 1 FROM metadata WHERE key='sources_seeded'").fetchone()
    for seed_id, source in load_default_sources():
        # Legacy defaults are only seeded on a fresh install. Stable IDs opt
        # new additions into a one-time upgrade, even if later deleted.
        if seeded and seed_id is None:
            continue
        key = "default_source:" + seed_id if seed_id else None
        if key and conn.execute("SELECT 1 FROM metadata WHERE key=?", (key,)).fetchone():
            continue
        exists = conn.execute("SELECT 1 FROM sources WHERE kind=? AND url=?",
                              (source["kind"], source["url"])).fetchone()
        if not exists:
            media_id = ensure_media(conn, source["media_name"] or default_media_name(source))
            conn.execute("INSERT INTO sources(name,kind,url,category,enabled,steam_appid,media_id) VALUES(?,?,?,?,?,?,?)",
                         (source["name"], source["kind"], source["url"], source["category"],
                          int(source["enabled"]), source["steam_appid"], media_id))
        if key:
            conn.execute("INSERT INTO metadata(key,value) VALUES(?,'1')", (key,))
    conn.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('sources_seeded','1')")


READING_SCHEMA = """
                CREATE TABLE IF NOT EXISTS translation_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS translation_providers (
                    id TEXT PRIMARY KEY, protocol TEXT NOT NULL, base_url TEXT NOT NULL,
                    model TEXT NOT NULL, encrypted_key TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS article_translations (
                    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                    fingerprint TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, translated_at TEXT NOT NULL,
                    PRIMARY KEY(article_id,fingerprint));
                CREATE TABLE IF NOT EXISTS article_content (
                    article_id INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
                    source_url TEXT NOT NULL, paragraphs TEXT NOT NULL, author TEXT NOT NULL,
                    fetched_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS article_content_translations (
                    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                    fingerprint TEXT NOT NULL, part INTEGER NOT NULL,
                    title TEXT NOT NULL, paragraphs TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, translated_at TEXT NOT NULL,
                    PRIMARY KEY(article_id,fingerprint,part));
"""


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.catalog = load_catalog()

    @contextmanager
    def connection(self, write=False):
        with self.lock:
            conn = sqlite3.connect(self.path, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            try:
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
                if write:
                    conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def initialize(self):
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL,
                    password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                    csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS admin_grants (
                    token_hash TEXT PRIMARY KEY,
                    session_hash TEXT NOT NULL UNIQUE REFERENCES sessions(token_hash) ON DELETE CASCADE,
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                    url TEXT NOT NULL, category TEXT NOT NULL, enabled INTEGER NOT NULL,
                    steam_appid INTEGER, last_success_at TEXT, last_error TEXT);
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY, canonical_url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, url TEXT NOT NULL, summary TEXT NOT NULL,
                    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
                    source_name TEXT NOT NULL, category TEXT NOT NULL,
                    published_at TEXT, fetched_at TEXT NOT NULL, image_url TEXT,
                    saved INTEGER NOT NULL DEFAULT 0, read INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS articles_date ON articles(COALESCE(published_at,fetched_at) DESC,id DESC);
                CREATE TABLE IF NOT EXISTS watches (
                    id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
                    aliases TEXT NOT NULL, keywords TEXT NOT NULL, exclude_keywords TEXT NOT NULL,
                    enabled INTEGER NOT NULL, market TEXT NOT NULL, ticker TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS article_watches (
                    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                    watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
                    PRIMARY KEY(article_id,watch_id));
                CREATE INDEX IF NOT EXISTS hits_watch ON article_watches(watch_id,article_id);
                CREATE TABLE IF NOT EXISTS article_topics (
                    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                    topic_id TEXT NOT NULL, PRIMARY KEY(article_id,topic_id));
                CREATE INDEX IF NOT EXISTS topics_filter ON article_topics(topic_id,article_id);
                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, time TEXT NOT NULL,
                    days TEXT NOT NULL, categories TEXT NOT NULL, enabled INTEGER NOT NULL,
                    last_run_at TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, trigger TEXT NOT NULL, started_at TEXT NOT NULL,
                    finished_at TEXT, status TEXT NOT NULL, new_count INTEGER NOT NULL DEFAULT 0,
                    source_count INTEGER NOT NULL DEFAULT 0, error TEXT);
                CREATE TABLE IF NOT EXISTS schedule_slots (
                    schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
                    scheduled_at TEXT NOT NULL, run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
                    PRIMARY KEY(schedule_id,scheduled_at));
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            conn.executescript(READING_SCHEMA)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS media (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS media_follows (
                    reader_id TEXT NOT NULL, media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
                    PRIMARY KEY(reader_id,media_id));
            """)
            for table in ("sources", "articles"):
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if "media_id" not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN media_id INTEGER REFERENCES media(id)")
            conn.execute("CREATE INDEX IF NOT EXISTS articles_media ON articles(media_id,id)")
            conn.execute("CREATE INDEX IF NOT EXISTS sources_media ON sources(media_id,enabled)")
            for source in conn.execute("SELECT * FROM sources WHERE media_id IS NULL").fetchall():
                media_id = ensure_media(conn, default_media_name(source))
                conn.execute("UPDATE sources SET media_id=? WHERE id=?", (media_id, source["id"]))
            conn.execute("""UPDATE articles SET media_id=(SELECT media_id FROM sources WHERE sources.id=articles.source_id)
                            WHERE media_id IS NULL AND source_id IS NOT NULL""")
            for row in conn.execute("SELECT DISTINCT source_name FROM articles WHERE media_id IS NULL").fetchall():
                media_id = ensure_media(conn, row["source_name"])
                conn.execute("UPDATE articles SET media_id=? WHERE media_id IS NULL AND source_name=?", (media_id, row["source_name"]))
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS guest_sessions (
                    token_hash TEXT PRIMARY KEY, csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS guest_preferences (
                    reader_id TEXT PRIMARY KEY REFERENCES guest_sessions(token_hash) ON DELETE CASCADE,
                    timezone TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS guest_article_state (
                    reader_id TEXT NOT NULL REFERENCES guest_sessions(token_hash) ON DELETE CASCADE,
                    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                    saved INTEGER NOT NULL DEFAULT 0, read INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(reader_id,article_id));
            """)
            for table in ("watches", "schedules"):
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if "reader_id" not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN reader_id TEXT NOT NULL DEFAULT 'owner'")
                conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_reader ON {table}(reader_id,id)")
            article_columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
            if "reader_id" not in article_columns:
                conn.execute("ALTER TABLE articles ADD COLUMN reader_id TEXT NOT NULL DEFAULT 'public'")
            conn.execute("CREATE INDEX IF NOT EXISTS articles_reader ON articles(reader_id,id)")
            schedule_columns = {row["name"] for row in conn.execute("PRAGMA table_info(schedules)")}
            if "timezone" not in schedule_columns:
                conn.execute("ALTER TABLE schedules ADD COLUMN timezone TEXT NOT NULL DEFAULT ''")
            watch_columns = {row["name"] for row in conn.execute("PRAGMA table_info(watches)")}
            if "league_id" not in watch_columns:
                conn.execute("ALTER TABLE watches ADD COLUMN league_id TEXT NOT NULL DEFAULT ''")
            conn.execute("INSERT OR IGNORE INTO settings(id,value) VALUES(1,?)", (json.dumps(DEFAULT_SETTINGS),))
            conn.execute("UPDATE runs SET status='interrupted',finished_at=?,error=? WHERE status='running'",
                         (utc_now(), "服务重启中断了此次采集；已保存的文章仍然保留"))
            seed_default_sources(conn)
            conn.execute("DELETE FROM admin_grants WHERE expires_at<=?", (utc_now(),))
            conn.execute("DELETE FROM sessions WHERE expires_at<=?", (utc_now(),))
            if reindex_topics(conn, self.catalog):
                from .collector import rematch_watch
                for watch in conn.execute("SELECT id FROM watches WHERE type IN ('team','league')"):
                    rematch_watch(conn, watch["id"], self.catalog)
            conn.commit()

    def settings(self):
        with self.connection() as conn:
            return json.loads(conn.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
