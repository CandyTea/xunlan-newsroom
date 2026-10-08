import hashlib
import hmac
import json
import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .collector import Collector, fetch_public, rematch_watch, resolve_public_url, watch_from_row
from .db import Database, utc_now
from .models import ArticlePatch, CATEGORIES, Credentials, FetchRequest, Schedule, Settings, Setup, Source, Watch
from .scheduler import Scheduler, next_run, schedule_from_row

ROOT = Path(__file__).resolve().parent.parent
COOKIE_NAME = "newsroom_session"


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return f"pbkdf2_sha256$600000${salt.hex()}${digest.hex()}"


def password_valid(password, stored):
    try:
        algorithm, iterations, salt, expected = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class BodyLimitMiddleware:
    """Bound JSON bodies before Starlette/Pydantic allocates them."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH", "DELETE"):
            await self.app(scope, receive, send)
            return
        data = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            data.extend(message.get("body", b""))
            if len(data) > 64 * 1024:
                await JSONResponse({"detail": "请求内容超过 64 KB 上限"}, status_code=413)(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        sent = False

        async def bounded_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(data), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)


def create_app(data_dir=None, start_scheduler=True, fetcher=fetch_public):
    db = Database(Path(data_dir or os.getenv("NEWSROOM_DATA_DIR", ROOT / "data")) / "newsroom.sqlite3")
    db.initialize()
    collector = Collector(db, fetcher)
    scheduler = Scheduler(db, collector)
    setup_token = os.getenv("SETUP_TOKEN", "")
    secure_cookie = os.getenv("COOKIE_SECURE", "0").lower() in ("1", "true", "yes")
    session_days = max(1, min(365, int(os.getenv("SESSION_DAYS", "30"))))
    login_failures = defaultdict(deque)
    dummy_hash = password_hash(secrets.token_urlsafe(32))

    @asynccontextmanager
    async def lifespan(app):
        if start_scheduler:
            scheduler.start()
        yield
        await scheduler.stop()
        await collector.stop()

    app = FastAPI(title="Newsroom", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(BodyLimitMiddleware)
    app.state.db = db
    app.state.collector = collector
    app.state.scheduler = scheduler

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: http: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api/") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        errors = exc.errors()
        fields = ", ".join(".".join(map(str, error["loc"][1:])) for error in errors[:3])
        return JSONResponse({"detail": f"输入参数无效：{fields or '请求内容'}。请检查格式、长度及必填项"}, status_code=422)

    def existing_session(request):
        token = request.cookies.get(COOKIE_NAME, "")
        if not token or len(token) > 128:
            return None
        with db.connection() as conn:
            row = conn.execute("""SELECT sessions.*,users.username FROM sessions JOIN users ON users.id=sessions.user_id
                                WHERE token_hash=? AND expires_at>?""", (token_hash(token), utc_now())).fetchone()
        return dict(row) if row else None

    def session_shape(session=None):
        with db.connection() as conn:
            setup_required = conn.execute("SELECT 1 FROM users").fetchone() is None
        return {"authenticated": session is not None, "setup_required": setup_required,
                "username": session["username"] if session else None,
                "csrf_token": session["csrf_token"] if session else None,
                "setup_token_required": bool(setup_token) and setup_required}

    def require_auth(request: Request):
        session = existing_session(request)
        if session is None:
            raise HTTPException(401, "请先登录，或重新登录已过期的会话")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            csrf = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(csrf, session["csrf_token"]):
                raise HTTPException(403, "安全令牌无效，请刷新页面后重试")
            require_same_origin(request)
        return session

    def require_same_origin(request):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
            raise HTTPException(403, "禁止跨站提交请求")

    def new_session(response, username):
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(days=session_days)
        with db.connection(write=True) as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at<=?", (utc_now(),))
            conn.execute("INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at) VALUES(?,1,?,?)", (token_hash(token), csrf, expires.isoformat()))
        response.set_cookie(COOKIE_NAME, token, max_age=session_days * 86400, httponly=True, secure=secure_cookie, samesite="strict", path="/")
        return session_shape({"username": username, "csrf_token": csrf})

    def ensure_row(conn, table, row_id):
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "未找到该记录")
        return row

    def source_from_row(row):
        value = dict(row)
        value["enabled"] = bool(value["enabled"])
        return value

    def article_from_row(conn, row):
        value = dict(row)
        value.pop("canonical_url", None)
        value.pop("source_id", None)
        value["saved"] = bool(value["saved"])
        value["read"] = bool(value["read"])
        value["watches"] = [dict(hit) for hit in conn.execute("""SELECT w.id,w.name,w.type FROM watches w
                              JOIN article_watches h ON h.watch_id=w.id WHERE h.article_id=? ORDER BY w.id""", (value["id"],))]
        return value

    def schedules_list():
        now = datetime.now(timezone.utc)
        zone = db.settings()["timezone"]
        with db.connection() as conn:
            values = [schedule_from_row(row) for row in conn.execute("SELECT * FROM schedules ORDER BY time,id")]
        for value in values:
            value["next_run_at"] = next_run(value, now, zone)
        return values

    @app.get("/healthz")
    def health():
        with db.connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/api/session")
    def session(request: Request):
        return session_shape(existing_session(request))

    @app.post("/api/setup")
    def setup(body: Setup, request: Request, response: Response):
        require_same_origin(request)
        if setup_token and not hmac.compare_digest(body.setup_token, setup_token):
            raise HTTPException(403, "安装令牌无效，请使用服务器 SETUP_TOKEN 配置值")
        username = body.username.strip()
        if len(username) < 2 or len(body.password) < 8:
            raise HTTPException(422, "用户名至少 2 个字符，密码至少 8 个字符")
        hashed = password_hash(body.password)
        with db.connection(write=True) as conn:
            if conn.execute("SELECT 1 FROM users").fetchone():
                raise HTTPException(409, "账户已经创建，请登录")
            conn.execute("INSERT INTO users(id,username,password_hash,created_at) VALUES(1,?,?,?)", (username, hashed, utc_now()))
        return new_session(response, username)

    @app.post("/api/login")
    def login(body: Credentials, request: Request, response: Response):
        require_same_origin(request)
        address = request.client.host if request.client else "unknown"
        now = time.monotonic()
        failures = login_failures[address]
        while failures and failures[0] < now - 900:
            failures.popleft()
        if len(failures) >= 10:
            raise HTTPException(429, "登录尝试过多，请在 15 分钟后重试")
        with db.connection() as conn:
            user = conn.execute("SELECT username,password_hash FROM users WHERE id=1").fetchone()
        valid_password = password_valid(body.password, user["password_hash"] if user else dummy_hash)
        if not user or body.username.strip() != user["username"] or not valid_password:
            failures.append(now)
            if len(login_failures) > 2048:
                for key in list(login_failures):
                    if not login_failures[key] or login_failures[key][-1] < now - 900:
                        del login_failures[key]
            raise HTTPException(401, "用户名或密码错误")
        failures.clear()
        return new_session(response, user["username"])

    @app.post("/api/logout")
    def logout(response: Response, session=Depends(require_auth)):
        with db.connection(write=True) as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (session["token_hash"],))
        response.delete_cookie(COOKIE_NAME, path="/", secure=secure_cookie, httponly=True, samesite="strict")
        return {"ok": True}

    @app.get("/api/articles", dependencies=[Depends(require_auth)])
    def articles(category: str = "", q: str = Query("", max_length=200), following: bool = False,
                 saved: bool = False, unread: bool = False, watch_id: int | None = Query(None, gt=0),
                 page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        conditions = []
        params = []
        if category:
            if category not in CATEGORIES:
                raise HTTPException(422, "新闻分类无效")
            conditions.append("a.category=?")
            params.append(category)
        if q.strip():
            query = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            conditions.append("(a.title LIKE ? ESCAPE '\\' OR a.summary LIKE ? ESCAPE '\\')")
            params.extend([query, query])
        if following:
            conditions.append("EXISTS(SELECT 1 FROM article_watches h WHERE h.article_id=a.id)")
        if saved:
            conditions.append("a.saved=1")
        if unread:
            conditions.append("a.read=0")
        if watch_id:
            conditions.append("EXISTS(SELECT 1 FROM article_watches h WHERE h.article_id=a.id AND h.watch_id=?)")
            params.append(watch_id)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with db.connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM articles a" + where, params).fetchone()[0]
            rows = conn.execute("SELECT a.* FROM articles a" + where + " ORDER BY COALESCE(published_at,fetched_at) DESC,id DESC LIMIT ? OFFSET ?",
                                [*params, page_size, (page - 1) * page_size]).fetchall()
            items = [article_from_row(conn, row) for row in rows]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    @app.get("/api/articles/{article_id}", dependencies=[Depends(require_auth)])
    def article(article_id: int):
        with db.connection() as conn:
            return article_from_row(conn, ensure_row(conn, "articles", article_id))

    @app.patch("/api/articles/{article_id}", dependencies=[Depends(require_auth)])
    def patch_article(article_id: int, body: ArticlePatch):
        with db.connection(write=True) as conn:
            ensure_row(conn, "articles", article_id)
            for key in ("saved", "read"):
                value = getattr(body, key)
                if value is not None:
                    conn.execute(f"UPDATE articles SET {key}=? WHERE id=?", (int(value), article_id))
            return article_from_row(conn, ensure_row(conn, "articles", article_id))

    @app.get("/api/watches", dependencies=[Depends(require_auth)])
    def watches():
        with db.connection() as conn:
            return {"items": [watch_from_row(row) for row in conn.execute("SELECT * FROM watches ORDER BY id")]}

    def write_watch(body, watch_id=None):
        value = body.model_dump()
        for key in ("aliases", "keywords", "exclude_keywords"):
            value[key] = json.dumps(value[key], ensure_ascii=False)
        value["enabled"] = int(value["enabled"])
        with db.connection(write=True) as conn:
            if watch_id is None:
                watch_id = conn.execute("INSERT INTO watches(type,name,aliases,keywords,exclude_keywords,enabled,market,ticker) VALUES(?,?,?,?,?,?,?,?)", tuple(value.values())).lastrowid
            else:
                ensure_row(conn, "watches", watch_id)
                conn.execute("UPDATE watches SET type=?,name=?,aliases=?,keywords=?,exclude_keywords=?,enabled=?,market=?,ticker=? WHERE id=?", (*value.values(), watch_id))
            rematch_watch(conn, watch_id)
            return watch_from_row(ensure_row(conn, "watches", watch_id))

    @app.post("/api/watches", dependencies=[Depends(require_auth)])
    def add_watch(body: Watch):
        return write_watch(body)

    @app.put("/api/watches/{watch_id}", dependencies=[Depends(require_auth)])
    def edit_watch(watch_id: int, body: Watch):
        return write_watch(body, watch_id)

    @app.delete("/api/watches/{watch_id}", dependencies=[Depends(require_auth)])
    def delete_watch(watch_id: int):
        with db.connection(write=True) as conn:
            ensure_row(conn, "watches", watch_id)
            conn.execute("DELETE FROM watches WHERE id=?", (watch_id,))
        return {"ok": True}

    @app.get("/api/sources", dependencies=[Depends(require_auth)])
    def sources():
        with db.connection() as conn:
            return {"items": [source_from_row(row) for row in conn.execute("SELECT * FROM sources ORDER BY category,id")]}

    async def write_source(body, source_id=None):
        try:
            await resolve_public_url(body.url)
        except (ValueError, TimeoutError, OSError) as exc:
            raise HTTPException(422, "来源网址无法解析为允许的公网地址") from exc
        values = (body.name, body.kind, body.url, body.category, int(body.enabled), body.steam_appid)
        with db.connection(write=True) as conn:
            if source_id is None:
                source_id = conn.execute("INSERT INTO sources(name,kind,url,category,enabled,steam_appid) VALUES(?,?,?,?,?,?)", values).lastrowid
            else:
                ensure_row(conn, "sources", source_id)
                conn.execute("UPDATE sources SET name=?,kind=?,url=?,category=?,enabled=?,steam_appid=?,last_error=NULL WHERE id=?", (*values, source_id))
            return source_from_row(ensure_row(conn, "sources", source_id))

    @app.post("/api/sources", dependencies=[Depends(require_auth)])
    async def add_source(body: Source):
        return await write_source(body)

    @app.put("/api/sources/{source_id}", dependencies=[Depends(require_auth)])
    async def edit_source(source_id: int, body: Source):
        return await write_source(body, source_id)

    @app.delete("/api/sources/{source_id}", dependencies=[Depends(require_auth)])
    def delete_source(source_id: int):
        with db.connection(write=True) as conn:
            ensure_row(conn, "sources", source_id)
            conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        return {"ok": True}

    @app.get("/api/schedules", dependencies=[Depends(require_auth)])
    def schedules():
        return {"items": schedules_list()}

    def write_schedule(body, schedule_id=None):
        values = (body.name, body.time, json.dumps(body.days), json.dumps(body.categories), int(body.enabled))
        with db.connection(write=True) as conn:
            if schedule_id is None:
                schedule_id = conn.execute("INSERT INTO schedules(name,time,days,categories,enabled,created_at) VALUES(?,?,?,?,?,?)", (*values, utc_now())).lastrowid
            else:
                old = ensure_row(conn, "schedules", schedule_id)
                conn.execute("UPDATE schedules SET name=?,time=?,days=?,categories=?,enabled=? WHERE id=?", (*values, schedule_id))
                if any(old[key] != value for key, value in zip(("time", "days", "categories", "enabled"), values[1:])):
                    # A newly configured time starts from this edit; it does not retroactively run.
                    conn.execute("UPDATE schedules SET created_at=? WHERE id=?", (utc_now(), schedule_id))
        scheduler.notify()
        return next(value for value in schedules_list() if value["id"] == schedule_id)

    @app.post("/api/schedules", dependencies=[Depends(require_auth)])
    def add_schedule(body: Schedule):
        return write_schedule(body)

    @app.put("/api/schedules/{schedule_id}", dependencies=[Depends(require_auth)])
    def edit_schedule(schedule_id: int, body: Schedule):
        return write_schedule(body, schedule_id)

    @app.delete("/api/schedules/{schedule_id}", dependencies=[Depends(require_auth)])
    def delete_schedule(schedule_id: int):
        with db.connection(write=True) as conn:
            ensure_row(conn, "schedules", schedule_id)
            conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
        scheduler.notify()
        return {"ok": True}

    @app.get("/api/settings", dependencies=[Depends(require_auth)])
    def settings():
        return db.settings()

    @app.put("/api/settings", dependencies=[Depends(require_auth)])
    def update_settings(body: Settings):
        value = body.model_dump()
        with db.connection(write=True) as conn:
            conn.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(value),))
        scheduler.notify()
        return value

    @app.post("/api/fetch", dependencies=[Depends(require_auth)])
    async def fetch(body: FetchRequest):
        accepted, run_id = await collector.start(body.categories)
        return {"accepted": accepted, "run_id": run_id, "message": "采集已启动" if accepted else "已有采集正在进行，未重复启动"}

    @app.get("/api/runs", dependencies=[Depends(require_auth)])
    def runs(limit: int = Query(30, ge=1, le=100)):
        with db.connection() as conn:
            return {"items": [dict(row) for row in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]}

    @app.get("/api/status", dependencies=[Depends(require_auth)])
    def status():
        with db.connection() as conn:
            article_count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            unread_count = conn.execute("SELECT COUNT(*) FROM articles WHERE read=0").fetchone()[0]
            following_count = conn.execute("SELECT COUNT(DISTINCT article_id) FROM article_watches").fetchone()[0]
            source_count = conn.execute("SELECT COUNT(*) FROM sources WHERE enabled=1").fetchone()[0]
            success = conn.execute("SELECT value FROM metadata WHERE key='last_success_at'").fetchone()
            last = conn.execute("SELECT error FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        future = [value["next_run_at"] for value in schedules_list() if value["next_run_at"]]
        return {"fetching": collector.fetching, "last_success_at": success[0] if success else None,
                "next_run_at": min(future) if future else None, "article_count": article_count,
                "unread_count": unread_count, "following_count": following_count, "source_count": source_count,
                "last_error": last[0] if last else None}

    @app.get("/api/export", dependencies=[Depends(require_auth)])
    def export():
        with db.connection() as conn:
            return {"settings": db.settings(), "sources": [source_from_row(row) for row in conn.execute("SELECT * FROM sources ORDER BY id")],
                    "watches": [watch_from_row(row) for row in conn.execute("SELECT * FROM watches ORDER BY id")], "schedules": schedules_list()}

    static = ROOT / "app" / "static"
    if static.exists():
        app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        path = static / "index.html"
        if not path.exists():
            raise HTTPException(503, "网页资源尚未安装")
        return FileResponse(path)

    return app


app = create_app()
