"""Private reading caches and provider credentials for anonymous visitors."""
import threading

from .content import ArticleReader
from .db import Database, READING_SCHEMA
from .translation import Translator


class GuestDatabase(Database):
    def initialize(self):
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS articles (id INTEGER PRIMARY KEY, url TEXT NOT NULL)")
            conn.executescript(READING_SCHEMA)

    def ensure_article(self, article):
        with self.connection(write=True) as conn:
            conn.execute("INSERT INTO articles(id,url) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET url=excluded.url",
                         (article["id"], article["url"]))


class GuestReader:
    def __init__(self, path):
        self.db = GuestDatabase(path)
        self.db.initialize()
        self.translator = Translator(self.db)
        self.reader = ArticleReader(self.db)

    async def stop(self):
        await self.translator.stop()
        await self.reader.stop()


class GuestReaders:
    def __init__(self, db):
        self.directory = db.path.parent / "guests"
        self.readers = {}
        self.lock = threading.Lock()

    def get(self, reader_id):
        # IDs originate from the verified session's SHA-256 hash, never request paths.
        with self.lock:
            if reader_id not in self.readers:
                self.readers[reader_id] = GuestReader(self.directory / reader_id / "reading.sqlite3")
            return self.readers[reader_id]

    async def stop(self):
        for reader in self.readers.values():
            await reader.stop()
