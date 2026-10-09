import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .collector import Collector, fetch_config, fetch_public, rematch_watch, resolve_public_url, watch_from_row
from .browser import browser_cache_url, browser_source_urls, parse_browser_feed
from .content import ArticleReader, ContentError
from .db import Database, utc_now
from .guests import GuestReaders
from .dns import dns_mode
from .models import ArticleHtml, ArticlePatch, BrowserFeed, BrowserReport, CATEGORIES, Credentials, FetchRequest, ReadingPreferences, Schedule, Settings, Setup, Source, TranslationRequest, TranslationSettings, Watch
from .scheduler import Scheduler, next_run, schedule_from_row
from .translation import PROVIDERS, TranslationError, Translator, needs_translation

ROOT = Path(__file__).resolve().parent.parent
COOKIE_NAME = "newsroom_session"
ADMIN_COOKIE_NAME = "newsroom_admin"
GUEST_COOKIE_NAME = "newsroom_guest"


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
        limit = 4 * 1024 * 1024 if scope["path"] == "/api/browser/import" and scope["method"] == "POST" else 64 * 1024
        if scope["path"].startswith("/api/articles/") and scope["path"].endswith("/content/import") and scope["method"] == "POST":
            limit = 4 * 1024 * 1024
        if scope["path"] == "/api/translation/settings" and scope["method"] == "PUT":
            limit = 256 * 1024
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            data.extend(message.get("body", b""))
            if len(data) > limit:
                await JSONResponse({"detail": "请求内容超过大小上限"}, status_code=413)(scope, receive, send)
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
    fetch_config()
    db = Database(Path(data_dir or os.getenv("NEWSROOM_DATA_DIR", ROOT / "data")) / "newsroom.sqlite3")
    db.initialize()
    collector = Collector(db, fetcher)
    translator = Translator(db)
    reader = ArticleReader(db)
    guests = GuestReaders(db)
    browser_urls = browser_source_urls()
    scheduler = Scheduler(db, collector)
    setup_token = os.getenv("SETUP_TOKEN", "")
    secure_cookie = os.getenv("COOKIE_SECURE", "0").lower() in ("1", "true", "yes")
    session_days = max(1, min(365, int(os.getenv("SESSION_DAYS", "30"))))
    login_failures = defaultdict(deque)
    login_lock = threading.Lock()
    dummy_hash = password_hash(secrets.token_urlsafe(32))

    @asynccontextmanager
    async def lifespan(app):
        if start_scheduler:
            scheduler.start()
        yield
        await scheduler.stop()
        await collector.stop()
        await translator.stop()
        await reader.stop()
        await guests.stop()

    app = FastAPI(title="Newsroom", version="1.4.0", lifespan=lifespan, docs_url=None, redoc_url=None)
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
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: http: data:; connect-src 'self' https://raw.githubusercontent.com https://www.espn.com https://api.rss2json.com https://api.allorigins.win https://r.jina.ai; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api/") or request.url.path in ("/", "/admin"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        errors = exc.errors()
        fields = ", ".join(".".join(map(str, error["loc"][1:])) for error in errors[:3])
        return JSONResponse({"detail": f"输入参数无效：{fields or '请求内容'}。请检查格式、长度及必填项"}, status_code=422)

    @app.exception_handler(TranslationError)
    async def translation_error(request, exc):
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status)

    @app.exception_handler(ContentError)
    async def content_error(request, exc):
        return JSONResponse({"detail": str(exc), "code": "content_unavailable"}, status_code=502)

    def existing_session(request):
        token = request.cookies.get(COOKIE_NAME, "")
        if not token or len(token) > 128:
            return None
        with db.connection() as conn:
            row = conn.execute("""SELECT sessions.*,users.username FROM sessions JOIN users ON users.id=sessions.user_id
                                WHERE token_hash=? AND expires_at>?""", (token_hash(token), utc_now())).fetchone()
        return dict(row) if row else None

    def existing_guest(request):
        token = request.cookies.get(GUEST_COOKIE_NAME, "")
        if not token or len(token) > 128:
            return None
        with db.connection() as conn:
            row = conn.execute("SELECT * FROM guest_sessions WHERE token_hash=? AND expires_at>?",
                               (token_hash(token), utc_now())).fetchone()
        return {**dict(row), "guest": True} if row else None

    def reader_id(session):
        return session["token_hash"] if session.get("guest") else "owner"

    def reading_services(session, article=None):
        if not session.get("guest"):
            return db, translator, reader
        services = guests.get(reader_id(session))
        if article is not None:
            services.db.ensure_article(article)
        return services.db, services.translator, services.reader

    def reader_timezone(session):
        if session.get("guest"):
            with db.connection() as conn:
                row = conn.execute("SELECT timezone FROM guest_preferences WHERE reader_id=?", (reader_id(session),)).fetchone()
            if row:
                return row["timezone"]
        return db.settings()["timezone"]

    def session_shape(session=None):
        with db.connection() as conn:
            setup_required = conn.execute("SELECT 1 FROM users").fetchone() is None
        guest = bool(session and session.get("guest"))
        return {"authenticated": session is not None and not guest, "guest": guest,
                "can_read": session is not None, "setup_required": setup_required,
                "reader_key": "guest:" + session["token_hash"][:16] if guest else None,
                "username": session.get("username") if session else None,
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

    def require_reader(request: Request):
        session = existing_session(request) or existing_guest(request)
        if session is None:
            raise HTTPException(401, "阅读会话已过期，请重新打开游客模式或登录")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session["csrf_token"]):
                raise HTTPException(403, "安全令牌无效，请刷新页面后重试")
            require_same_origin(request)
        return session

    def require_same_origin(request):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
            raise HTTPException(403, "禁止跨站提交请求")

    def existing_admin_grant(request, session):
        token = request.cookies.get(ADMIN_COOKIE_NAME, "")
        if not token or len(token) > 128:
            return None
        with db.connection() as conn:
            row = conn.execute("SELECT expires_at FROM admin_grants WHERE token_hash=? AND session_hash=? AND expires_at>?",
                               (token_hash(token), session["token_hash"], utc_now())).fetchone()
        return dict(row) if row else None

    def require_admin(request: Request, session=Depends(require_auth)):
        grant = existing_admin_grant(request, session)
        if grant is None:
            raise HTTPException(403, "此操作需要管理员验证，请进入管理页面重新输入账户密码")
        return grant

    def clear_admin_cookie(response):
        response.delete_cookie(ADMIN_COOKIE_NAME, path="/api", secure=secure_cookie, httponly=True, samesite="strict")

    def new_session(request, response, username):
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(days=session_days)
        with db.connection(write=True) as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at<=?", (utc_now(),))
            old_token = request.cookies.get(COOKIE_NAME, "")
            if old_token and len(old_token) <= 128:
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(old_token),))
            conn.execute("INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at) VALUES(?,1,?,?)", (token_hash(token), csrf, expires.isoformat()))
        clear_admin_cookie(response)
        response.set_cookie(COOKIE_NAME, token, max_age=session_days * 86400, httponly=True, secure=secure_cookie, samesite="strict", path="/")
        return session_shape({"username": username, "csrf_token": csrf})

    def verify_owner(body, request, admin=False):
        address = request.client.host if request.client else "unknown"
        key = ("admin" if admin else "reader", address)
        now = time.monotonic()
        with login_lock:
            failures = login_failures[key]
            while failures and failures[0] < now - 900:
                failures.popleft()
            if len(failures) >= 10:
                raise HTTPException(429, "登录尝试过多，请在 15 分钟后重试")
            # Reserve before password hashing so concurrent attempts cannot bypass the limit.
            failures.append(now)
            if len(login_failures) > 2048:
                for old_key in list(login_failures):
                    if not login_failures[old_key] or login_failures[old_key][-1] < now - 900:
                        del login_failures[old_key]
        with db.connection() as conn:
            user = conn.execute("SELECT username,password_hash FROM users WHERE id=1").fetchone()
        valid = password_valid(body.password, user["password_hash"] if user else dummy_hash)
        if not user or body.username.strip() != user["username"] or not valid:
            raise HTTPException(403 if admin else 401, "管理员用户名或密码错误" if admin else "用户名或密码错误")
        with login_lock:
            failures.clear()
        return user["username"]

    def ensure_row(conn, table, row_id, scope=None):
        if scope is None:
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
        else:
            row = conn.execute(f"SELECT * FROM {table} WHERE id=? AND reader_id=?", (row_id, scope)).fetchone()
        if row is None:
            raise HTTPException(404, "未找到该记录")
        return row

    def visible_article(conn, article_id, session):
        row = ensure_row(conn, "articles", article_id)
        if row["reader_id"] not in ("public", reader_id(session)):
            raise HTTPException(404, "未找到该资讯")
        return row

    def source_from_row(row):
        value = dict(row)
        value["enabled"] = bool(value["enabled"])
        return value

    def article_from_row(conn, row, session, translation_config=None):
        value = dict(row)
        value.pop("canonical_url", None)
        value.pop("source_id", None)
        value.pop("reader_id", None)
        if session.get("guest"):
            flags = conn.execute("SELECT saved,read FROM guest_article_state WHERE reader_id=? AND article_id=?",
                                 (reader_id(session), value["id"])).fetchone()
            value["saved"], value["read"] = (flags["saved"], flags["read"]) if flags else (0, 0)
        value["saved"] = bool(value["saved"])
        value["read"] = bool(value["read"])
        cache_db, scoped_translator, _ = reading_services(session)
        if cache_db is db:
            value["translation"] = scoped_translator.cached(conn, row, translation_config)
        else:
            with cache_db.connection() as cache_conn:
                value["translation"] = scoped_translator.cached(cache_conn, row, translation_config)
        value["needs_translation"] = needs_translation(row)
        value["watches"] = [dict(hit) for hit in conn.execute("""SELECT w.id,w.name,w.type FROM watches w
                              JOIN article_watches h ON h.watch_id=w.id WHERE h.article_id=? AND w.reader_id=? ORDER BY w.id""", (value["id"], reader_id(session)))]
        return value

    def schedules_list(scope="owner"):
        now = datetime.now(timezone.utc)
        zone = db.settings()["timezone"]
        with db.connection() as conn:
            values = [schedule_from_row(row) for row in conn.execute("SELECT * FROM schedules WHERE reader_id=? ORDER BY time,id", (scope,))]
        for value in values:
            value["next_run_at"] = next_run(value, now, value.pop("timezone", "") or zone)
            value.pop("reader_id", None)
        return values

    @app.get("/healthz")
    def health():
        with db.connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/api/session")
    def session(request: Request):
        return session_shape(existing_session(request) or existing_guest(request))

    @app.post("/api/guest")
    def enter_guest(request: Request, response: Response):
        require_same_origin(request)
        current = existing_session(request) or existing_guest(request)
        if current:
            return session_shape(current)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(days=365)
        guest = {"token_hash": token_hash(token), "csrf_token": csrf, "expires_at": expires.isoformat(), "guest": True}
        with db.connection(write=True) as conn:
            conn.execute("INSERT INTO guest_sessions(token_hash,csrf_token,expires_at) VALUES(?,?,?)",
                         (guest["token_hash"], csrf, guest["expires_at"]))
            conn.execute("INSERT INTO guest_preferences(reader_id,timezone) VALUES(?,?)",
                         (guest["token_hash"], db.settings()["timezone"]))
        response.set_cookie(GUEST_COOKIE_NAME, token, max_age=365 * 86400, httponly=True,
                            secure=secure_cookie, samesite="strict", path="/")
        return session_shape(guest)

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
        return new_session(request, response, username)

    @app.post("/api/login")
    def login(body: Credentials, request: Request, response: Response):
        require_same_origin(request)
        username = verify_owner(body, request)
        return new_session(request, response, username)

    @app.post("/api/logout")
    def logout(response: Response, session=Depends(require_auth)):
        with db.connection(write=True) as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (session["token_hash"],))
        response.delete_cookie(COOKIE_NAME, path="/", secure=secure_cookie, httponly=True, samesite="strict")
        clear_admin_cookie(response)
        return {"ok": True}

    @app.get("/api/admin/session")
    def admin_session(request: Request, session=Depends(require_auth)):
        grant = existing_admin_grant(request, session)
        return {"authenticated": grant is not None, "expires_at": grant["expires_at"] if grant else None}

    @app.post("/api/admin/login")
    def admin_login(body: Credentials, request: Request, response: Response, session=Depends(require_auth)):
        verify_owner(body, request, admin=True)
        now = datetime.now(timezone.utc)
        expires = min(now + timedelta(minutes=30), datetime.fromisoformat(session["expires_at"]))
        token = secrets.token_urlsafe(32)
        with db.connection(write=True) as conn:
            # A reader login/logout racing this verification revokes its capability.
            current = conn.execute("SELECT 1 FROM sessions WHERE token_hash=? AND expires_at>?", (session["token_hash"], now.isoformat())).fetchone()
            if current is None:
                raise HTTPException(401, "阅读会话已失效，请重新登录")
            conn.execute("DELETE FROM admin_grants WHERE session_hash=? OR expires_at<=?", (session["token_hash"], now.isoformat()))
            conn.execute("INSERT INTO admin_grants(token_hash,session_hash,created_at,expires_at) VALUES(?,?,?,?)",
                         (token_hash(token), session["token_hash"], now.isoformat(), expires.isoformat()))
        response.set_cookie(ADMIN_COOKIE_NAME, token, max_age=max(1, int((expires - now).total_seconds())),
                            httponly=True, secure=secure_cookie, samesite="strict", path="/api")
        return {"authenticated": True, "expires_at": expires.isoformat()}

    @app.post("/api/admin/logout")
    def admin_logout(response: Response, session=Depends(require_auth)):
        with db.connection(write=True) as conn:
            conn.execute("DELETE FROM admin_grants WHERE session_hash=?", (session["token_hash"],))
        clear_admin_cookie(response)
        return {"authenticated": False, "expires_at": None}

    @app.get("/api/catalog", dependencies=[Depends(require_reader)])
    def catalog():
        return db.catalog

    @app.get("/api/preferences")
    def preferences(session=Depends(require_reader)):
        _, scoped_translator, _ = reading_services(session)
        return {"timezone": reader_timezone(session), "translation": scoped_translator.reader_preferences()}

    @app.put("/api/preferences")
    def save_preferences(body: ReadingPreferences, session=Depends(require_reader)):
        if not session.get("guest"):
            raise HTTPException(403, "站点时区需要管理员权限")
        with db.connection(write=True) as conn:
            conn.execute("UPDATE guest_preferences SET timezone=? WHERE reader_id=?", (body.timezone, reader_id(session)))
            conn.execute("UPDATE schedules SET timezone=?,created_at=? WHERE reader_id=?",
                         (body.timezone, utc_now(), reader_id(session)))
        scheduler.notify()
        return preferences(session)

    @app.get("/api/translation/settings")
    def translation_settings(session=Depends(require_reader)):
        return reading_services(session)[1].public_settings()

    @app.put("/api/translation/settings")
    def save_translation_settings(body: TranslationSettings, session=Depends(require_reader)):
        return reading_services(session)[1].save_settings(body)

    @app.get("/api/translation/models")
    async def translation_models(provider: str = Query(..., max_length=40), session=Depends(require_reader)):
        if provider not in PROVIDERS:
            raise HTTPException(422, "翻译供应商无效")
        return {"models": await reading_services(session)[1].models(provider)}

    @app.get("/api/articles")
    def articles(category: str = "", q: str = Query("", max_length=200), following: bool = False,
                 saved: bool = False, unread: bool = False, watch_id: int | None = Query(None, gt=0),
                 company_id: int | None = Query(None, gt=0), league_id: str = Query("", max_length=80),
                 team_id: str = Query("", max_length=80),
                 page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), session=Depends(require_reader)):
        conditions = ["(a.reader_id='public' OR a.reader_id=?)"]
        params = [reader_id(session)]
        scope = reader_id(session)
        guest = bool(session.get("guest"))
        joins = " LEFT JOIN guest_article_state g ON g.article_id=a.id AND g.reader_id=?" if guest else ""
        join_params = [scope] if guest else []
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
            conditions.append("EXISTS(SELECT 1 FROM article_watches h JOIN watches w ON w.id=h.watch_id WHERE h.article_id=a.id AND w.reader_id=?)")
            params.append(scope)
        if saved:
            conditions.append("COALESCE(g.saved,0)=1" if guest else "a.saved=1")
        if unread:
            conditions.append("COALESCE(g.read,0)=0" if guest else "a.read=0")
        if watch_id:
            with db.connection() as conn:
                ensure_row(conn, "watches", watch_id, scope)
            conditions.append("EXISTS(SELECT 1 FROM article_watches h WHERE h.article_id=a.id AND h.watch_id=?)")
            params.append(watch_id)
        leagues = {league["id"] for league in db.catalog["leagues"]}
        teams = {team["id"]: team for team in db.catalog["teams"]}
        if league_id and league_id not in leagues:
            raise HTTPException(422, "联赛选项无效")
        if team_id and team_id not in teams:
            raise HTTPException(422, "球队选项无效")
        if league_id and team_id and teams[team_id]["league_id"] != league_id:
            raise HTTPException(422, "所选球队不属于该联赛")
        if league_id:
            conditions.append("""(EXISTS(SELECT 1 FROM article_topics t WHERE t.article_id=a.id AND t.topic_id=?) OR
                (a.category='sports' AND EXISTS(SELECT 1 FROM article_watches h JOIN watches w ON w.id=h.watch_id
                 WHERE h.article_id=a.id AND w.type='team' AND w.enabled=1 AND w.league_id=? AND w.reader_id=?)))""")
            params.extend(["league:" + league_id, league_id, scope])
        if team_id:
            conditions.append("EXISTS(SELECT 1 FROM article_topics t WHERE t.article_id=a.id AND t.topic_id=?)")
            params.append("team:" + team_id)
        if company_id:
            with db.connection() as conn:
                company = ensure_row(conn, "watches", company_id, scope)
                if company["type"] != "company" or not company["enabled"]:
                    raise HTTPException(422, "公司筛选必须选择已启用的公司关注")
            conditions.append("EXISTS(SELECT 1 FROM article_watches h WHERE h.article_id=a.id AND h.watch_id=?)")
            params.append(company_id)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        translation_config = reading_services(session)[1].snapshot()
        with db.connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM articles a" + joins + where, [*join_params, *params]).fetchone()[0]
            rows = conn.execute("SELECT a.* FROM articles a" + joins + where + " ORDER BY COALESCE(a.published_at,a.fetched_at) DESC,a.id DESC LIMIT ? OFFSET ?",
                                [*join_params, *params, page_size, (page - 1) * page_size]).fetchall()
            items = [article_from_row(conn, row, session, translation_config) for row in rows]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    @app.get("/api/articles/{article_id}")
    def article(article_id: int, session=Depends(require_reader)):
        with db.connection() as conn:
            value = article_from_row(conn, visible_article(conn, article_id, session), session)
        value.update(content_result(value, session))
        return value

    def content_result(value, session):
        cache_db, scoped_translator, scoped_reader = reading_services(session, value)
        with cache_db.connection() as conn:
            content = scoped_reader.cached(conn, value)
            translation = scoped_translator.cached_body(conn, value, content["paragraphs"]) if content else None
        return {"content": content, "body_translation": translation}

    @app.post("/api/articles/{article_id}/content")
    async def article_content(article_id: int, session=Depends(require_reader)):
        with db.connection() as conn:
            value = dict(visible_article(conn, article_id, session))
        await reading_services(session, value)[2].read(value)
        return content_result(value, session)

    @app.post("/api/articles/{article_id}/content/import")
    async def import_article_content(article_id: int, body: ArticleHtml, session=Depends(require_reader)):
        with db.connection() as conn:
            value = dict(visible_article(conn, article_id, session))
        await reading_services(session, value)[2].import_html(value, body.article_url, body.html)
        return content_result(value, session)

    @app.post("/api/articles/{article_id}/translate")
    async def translate_article(article_id: int, body: TranslationRequest, session=Depends(require_reader)):
        with db.connection() as conn:
            value = dict(visible_article(conn, article_id, session))
        cache_db, scoped_translator, scoped_reader = reading_services(session, value)
        if body.scope == "body":
            with cache_db.connection() as conn:
                content = scoped_reader.cached(conn, value)
            if content is None:
                raise HTTPException(409, "请先载入新闻正文，再点击翻译。")
            return {"translation": await scoped_translator.translate_body(value, content["paragraphs"])}
        return {"translation": await scoped_translator.translate(value, automatic=body.automatic)}

    @app.patch("/api/articles/{article_id}")
    def patch_article(article_id: int, body: ArticlePatch, session=Depends(require_reader)):
        with db.connection(write=True) as conn:
            visible_article(conn, article_id, session)
            if session.get("guest"):
                conn.execute("INSERT OR IGNORE INTO guest_article_state(reader_id,article_id) VALUES(?,?)",
                             (reader_id(session), article_id))
            for key in ("saved", "read"):
                value = getattr(body, key)
                if value is not None:
                    if session.get("guest"):
                        conn.execute(f"UPDATE guest_article_state SET {key}=? WHERE article_id=? AND reader_id=?",
                                     (int(value), article_id, reader_id(session)))
                    else:
                        conn.execute(f"UPDATE articles SET {key}=? WHERE id=?", (int(value), article_id))
            return article_from_row(conn, visible_article(conn, article_id, session), session)

    @app.get("/api/watches")
    def watches(session=Depends(require_reader)):
        with db.connection() as conn:
            return {"items": [watch_from_row(row) for row in conn.execute("SELECT * FROM watches WHERE reader_id=? ORDER BY id", (reader_id(session),))]}

    def write_watch(body, session, watch_id=None):
        if body.league_id and body.league_id not in {league["id"] for league in db.catalog["leagues"]}:
            raise HTTPException(422, "所属联赛选项无效")
        value = body.model_dump()
        for key in ("aliases", "keywords", "exclude_keywords"):
            value[key] = json.dumps(value[key], ensure_ascii=False)
        value["enabled"] = int(value["enabled"])
        with db.connection(write=True) as conn:
            if watch_id is None:
                watch_id = conn.execute("INSERT INTO watches(type,name,aliases,keywords,exclude_keywords,enabled,market,ticker,league_id,reader_id) VALUES(?,?,?,?,?,?,?,?,?,?)", (*value.values(), reader_id(session))).lastrowid
            else:
                ensure_row(conn, "watches", watch_id, reader_id(session))
                conn.execute("UPDATE watches SET type=?,name=?,aliases=?,keywords=?,exclude_keywords=?,enabled=?,market=?,ticker=?,league_id=? WHERE id=?", (*value.values(), watch_id))
            rematch_watch(conn, watch_id, db.catalog)
            return watch_from_row(ensure_row(conn, "watches", watch_id, reader_id(session)))

    @app.post("/api/watches")
    def add_watch(body: Watch, session=Depends(require_reader)):
        return write_watch(body, session)

    @app.put("/api/watches/{watch_id}")
    def edit_watch(watch_id: int, body: Watch, session=Depends(require_reader)):
        return write_watch(body, session, watch_id)

    @app.delete("/api/watches/{watch_id}")
    def delete_watch(watch_id: int, session=Depends(require_reader)):
        with db.connection(write=True) as conn:
            ensure_row(conn, "watches", watch_id, reader_id(session))
            conn.execute("DELETE FROM watches WHERE id=?", (watch_id,))
        return {"ok": True}

    @app.get("/api/sources", dependencies=[Depends(require_admin)])
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

    @app.post("/api/sources", dependencies=[Depends(require_admin)])
    async def add_source(body: Source):
        return await write_source(body)

    @app.put("/api/sources/{source_id}", dependencies=[Depends(require_admin)])
    async def edit_source(source_id: int, body: Source):
        return await write_source(body, source_id)

    @app.delete("/api/sources/{source_id}", dependencies=[Depends(require_admin)])
    def delete_source(source_id: int):
        with db.connection(write=True) as conn:
            ensure_row(conn, "sources", source_id)
            conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        return {"ok": True}

    @app.get("/api/schedules")
    def schedules(session=Depends(require_reader)):
        return {"items": schedules_list(reader_id(session))}

    def write_schedule(body, session, schedule_id=None):
        values = (body.name, body.time, json.dumps(body.days), json.dumps(body.categories), int(body.enabled))
        with db.connection(write=True) as conn:
            if schedule_id is None:
                schedule_id = conn.execute("INSERT INTO schedules(name,time,days,categories,enabled,created_at,reader_id,timezone) VALUES(?,?,?,?,?,?,?,?)", (*values, utc_now(), reader_id(session), reader_timezone(session) if session.get("guest") else "")).lastrowid
            else:
                old = ensure_row(conn, "schedules", schedule_id, reader_id(session))
                conn.execute("UPDATE schedules SET name=?,time=?,days=?,categories=?,enabled=? WHERE id=?", (*values, schedule_id))
                if any(old[key] != value for key, value in zip(("time", "days", "categories", "enabled"), values[1:])):
                    # A newly configured time starts from this edit; it does not retroactively run.
                    conn.execute("UPDATE schedules SET created_at=? WHERE id=?", (utc_now(), schedule_id))
        scheduler.notify()
        return next(value for value in schedules_list(reader_id(session)) if value["id"] == schedule_id)

    @app.post("/api/schedules")
    def add_schedule(body: Schedule, session=Depends(require_reader)):
        return write_schedule(body, session)

    @app.put("/api/schedules/{schedule_id}")
    def edit_schedule(schedule_id: int, body: Schedule, session=Depends(require_reader)):
        return write_schedule(body, session, schedule_id)

    @app.delete("/api/schedules/{schedule_id}")
    def delete_schedule(schedule_id: int, session=Depends(require_reader)):
        with db.connection(write=True) as conn:
            ensure_row(conn, "schedules", schedule_id, reader_id(session))
            conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
        scheduler.notify()
        return {"ok": True}

    @app.get("/api/settings", dependencies=[Depends(require_admin)])
    def settings():
        return db.settings()

    @app.put("/api/settings", dependencies=[Depends(require_admin)])
    def update_settings(body: Settings):
        value = body.model_dump()
        with db.connection(write=True) as conn:
            conn.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(value),))
        scheduler.notify()
        return value

    @app.post("/api/fetch")
    async def fetch(body: FetchRequest, request: Request, session=Depends(require_reader)):
        if body.failed_only:
            require_admin(request, require_auth(request))
        options = {"failed_only": body.failed_only}
        if body.skip_source_ids:
            options["skip_source_ids"] = body.skip_source_ids
        accepted, run_id = await collector.start(body.categories, **options)
        return {"accepted": accepted, "run_id": run_id, "message": "采集已启动" if accepted else "已有采集正在进行，未重复启动"}

    @app.get("/api/browser/sources", dependencies=[Depends(require_reader)])
    def browser_sources():
        with db.connection() as conn:
            items = [{**{key: row[key] for key in ("id", "name", "url", "category")}, "cache_url": browser_cache_url(row["url"])}
                     for row in conn.execute("SELECT * FROM sources WHERE enabled=1 AND kind='rss' ORDER BY id")
                     if row["url"] in browser_urls]
        return {"items": items}

    @app.post("/api/browser/import")
    def browser_import(body: BrowserFeed, session=Depends(require_reader)):
        with db.connection() as conn:
            row = ensure_row(conn, "sources", body.source_id)
            if not row["enabled"] or row["kind"] != "rss" or row["url"] != body.source_url or row["url"] not in browser_urls:
                raise HTTPException(409, "此来源已停用或地址改变，未保存客户端数据")
            source = dict(row)
        try:
            articles = parse_browser_feed(body.content, body.format, source["url"])
        except (ValueError, UnicodeError, ET.ParseError) as exc:
            raise HTTPException(422, "客户端取得的内容不是有效的资讯订阅") from exc
        with db.lock:
            with db.connection() as conn:
                row = ensure_row(conn, "sources", body.source_id)
                if not row["enabled"] or row["kind"] != "rss" or row["url"] != body.source_url:
                    raise HTTPException(409, "此来源已停用或地址改变，未保存客户端数据")
                source = dict(row)
            new_count = collector.persist_articles(source, articles, reader_id(session) if session.get("guest") else "public")
            if session.get("guest"):
                return {"source_id": body.source_id, "new_count": new_count, "item_count": len(articles)}
            with db.connection(write=True) as conn:
                now = utc_now()
                conn.execute("INSERT INTO metadata(key,value) VALUES('last_success_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now,))
                conn.execute("INSERT INTO runs(trigger,started_at,finished_at,status,new_count,source_count) VALUES('browser',?,?,'success',?,1)", (now, now, new_count))
        return {"source_id": body.source_id, "new_count": new_count, "item_count": len(articles)}

    @app.post("/api/browser/report")
    def browser_report(body: BrowserReport, session=Depends(require_reader)):
        if session.get("guest"):
            return {"recorded": False}
        errors = []
        with db.connection(write=True) as conn:
            for failure in body.failures:
                row = conn.execute("SELECT * FROM sources WHERE id=? AND enabled=1 AND kind='rss'", (failure.source_id,)).fetchone()
                if row is not None and row["url"] == failure.source_url and row["url"] in browser_urls:
                    errors.append(f"{row['name']}: {failure.reason}")
            if errors:
                now = utc_now()
                conn.execute("INSERT INTO runs(trigger,started_at,finished_at,status,new_count,source_count,error) VALUES('browser',?,?,'failed',0,?,?)", (now, now, len(errors), "；".join(errors)[:4000]))
        return {"recorded": bool(errors)}

    @app.get("/api/network", dependencies=[Depends(require_admin)])
    def network():
        mode, proxy = fetch_config()
        return {"fetch_mode": mode, "dns_mode": dns_mode(), "proxy_configured": bool(proxy)}

    @app.get("/api/runs", dependencies=[Depends(require_admin)])
    def runs(limit: int = Query(30, ge=1, le=100)):
        with db.connection() as conn:
            return {"items": [dict(row) for row in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]}

    @app.get("/api/status")
    def status(session=Depends(require_reader)):
        with db.connection() as conn:
            article_count = conn.execute("SELECT COUNT(*) FROM articles WHERE reader_id='public' OR reader_id=?", (reader_id(session),)).fetchone()[0]
            if session.get("guest"):
                unread_count = conn.execute("""SELECT COUNT(*) FROM articles a LEFT JOIN guest_article_state g
                    ON g.article_id=a.id AND g.reader_id=? WHERE COALESCE(g.read,0)=0 AND (a.reader_id='public' OR a.reader_id=?)""", (reader_id(session), reader_id(session))).fetchone()[0]
            else:
                unread_count = conn.execute("SELECT COUNT(*) FROM articles WHERE read=0 AND reader_id='public'").fetchone()[0]
            following_count = conn.execute("""SELECT COUNT(DISTINCT h.article_id) FROM article_watches h
                JOIN watches w ON w.id=h.watch_id WHERE w.reader_id=?""", (reader_id(session),)).fetchone()[0]
            source_count = conn.execute("SELECT COUNT(*) FROM sources WHERE enabled=1").fetchone()[0]
            success = conn.execute("SELECT value FROM metadata WHERE key='last_success_at'").fetchone()
            last_run = conn.execute("SELECT status,new_count,source_count FROM runs WHERE trigger!='browser' ORDER BY id DESC LIMIT 1").fetchone()
        future = [value["next_run_at"] for value in schedules_list(reader_id(session)) if value["next_run_at"]]
        return {"fetching": collector.fetching, "last_success_at": success[0] if success else None,
                "next_run_at": min(future) if future else None, "article_count": article_count,
                "unread_count": unread_count, "following_count": following_count, "source_count": source_count,
                "last_run": dict(last_run) if last_run else None, "last_error": None}

    @app.get("/api/export", dependencies=[Depends(require_admin)])
    def export(session=Depends(require_auth)):
        with db.connection() as conn:
            return {"settings": db.settings(), "sources": [source_from_row(row) for row in conn.execute("SELECT * FROM sources ORDER BY id")],
                    "watches": [watch_from_row(row) for row in conn.execute("SELECT * FROM watches WHERE reader_id=? ORDER BY id", (reader_id(session),))], "schedules": schedules_list()}

    static = ROOT / "app" / "static"
    if static.exists():
        app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        path = static / "index.html"
        if not path.exists():
            raise HTTPException(503, "网页资源尚未安装")
        return FileResponse(path)

    @app.get("/admin", include_in_schema=False)
    def admin_index():
        path = static / "admin.html"
        if not path.exists():
            raise HTTPException(503, "管理网页资源尚未安装")
        return FileResponse(path)

    return app


app = create_app()
