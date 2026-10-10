"""Read public Telegram channel previews without account credentials."""
import html as text_html
import re
import textwrap
from urllib.parse import parse_qs, urlsplit

from lxml import etree, html


def channel_url(value):
    value = value.strip()
    if value.startswith(("t.me/", "telegram.me/")):
        value = "https://" + value
    if "://" in value:
        parts = urlsplit(value)
        if (parts.scheme not in ("http", "https") or parts.hostname not in ("t.me", "telegram.me")
                or parts.username or parts.password or parts.port not in (None, 80, 443)):
            raise ValueError("请输入 Telegram 公开频道用户名或 t.me 频道链接")
        path = parts.path.strip("/").split("/")
        if path and path[0] == "s":
            path = path[1:]
        if not path or len(path) > 2 or (len(path) == 2 and not path[1].isdigit()):
            raise ValueError("请使用公开频道链接，不支持私密邀请链接或数字频道 ID")
        value = path[0]
    value = value.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,31}", value):
        raise ValueError("请填写公开频道用户名，例如 @FabrizioRomanoTG；不支持纯数字 ID 或私密邀请链接")
    return "https://t.me/s/" + value.lower()


def parse_telegram_page(data, source_url):
    from .collector import MAX_BYTES, _article

    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("Telegram 页面超过 2 MB 上限")
    channel = urlsplit(channel_url(source_url)).path.rsplit("/", 1)[-1]
    try:
        root = html.fromstring(data.decode("utf-8", errors="replace"), parser=html.HTMLParser(no_network=True))
    except (etree.ParserError, ValueError) as exc:
        raise ValueError("Telegram 未返回有效的公开频道页面") from exc
    messages = root.xpath("//*[contains(concat(' ',normalize-space(@class),' '),' tgme_widget_message ')]")
    articles = []
    seen = set()
    message_ids = []
    for message in messages[:100]:
        post = message.get("data-post", "")
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]{2,31})/([1-9][0-9]{0,19})", post)
        if not match or match[1].lower() != channel or match[2] in seen:
            continue
        message_ids.append(int(match[2]))
        texts = message.xpath(".//*[contains(concat(' ',normalize-space(@class),' '),' tgme_widget_message_text ')]")
        if not texts:
            continue
        text = texts[0]
        for br in text.xpath(".//br"):
            br.tail = "\n" + (br.tail or "")
        lines = [" ".join(line.split()) for line in text.text_content().splitlines()]
        paragraphs = [piece for line in lines if line for piece in textwrap.wrap(line, width=2500, break_on_hyphens=False)]
        full_text = "\n".join(paragraphs)
        if not full_text or len(full_text) > 60000 or len(paragraphs) > 300:
            continue
        times = message.xpath(".//time/@datetime")
        photos = message.xpath(".//*[contains(concat(' ',normalize-space(@class),' '),' tgme_widget_message_photo_wrap ')]/@style")
        image = None
        if photos:
            photo = re.search(r"background-image\s*:\s*url\(['\"]?(https?://[^'\")\s]+)", photos[0])
            image = photo[1] if photo else None
        title = paragraphs[0][:180] + ("…" if len(paragraphs[0]) > 180 else "")
        article = _article(text_html.escape(title), f"https://t.me/{channel}/{match[2]}",
                           text_html.escape(full_text), times[0] if times else None, image)
        if article:
            article["content"] = {"paragraphs": paragraphs, "author": "@" + channel}
            articles.append(article)
            seen.add(match[2])
    older = []
    for link in root.xpath("//a[@data-before or contains(@href,'before=')]"):
        candidate = link.get("data-before", "")
        if not candidate:
            try:
                parts = urlsplit(link.get("href", ""))
            except ValueError:
                continue
            if parts.hostname not in (None, "t.me") or parts.path.rstrip("/").lower() != "/s/" + channel:
                continue
            candidate = parse_qs(parts.query).get("before", [""])[0]
        if re.fullmatch(r"[1-9][0-9]{0,19}", candidate):
            older.append(int(candidate))
    usernames = root.xpath("//*[contains(concat(' ',normalize-space(@class),' '),' tgme_channel_info_header_username ')]")
    recognized = bool(message_ids) or any(node.text_content().strip().lower() == "@" + channel for node in usernames)
    if not recognized:
        raise ValueError("Telegram 未返回该频道的有效公开预览")
    return {"articles": articles, "message_ids": message_ids, "older": older}


def parse_telegram(data, source_url):
    articles = parse_telegram_page(data, source_url)["articles"]
    if not articles:
        raise ValueError("频道没有可读取的公开文字消息，请检查用户名、频道公开状态或网页预览权限")
    return articles
