import re
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Category = Literal["games", "sports", "stocks", "politics"]
CATEGORIES = ("games", "sports", "stocks", "politics")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(StrictModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=256)


class Setup(Credentials):
    setup_token: str = Field(default="", max_length=256)


class Watch(StrictModel):
    type: Literal["company", "studio", "team", "league", "country", "politician"]
    name: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    keywords: list[str] = Field(default_factory=list, max_length=30)
    exclude_keywords: list[str] = Field(default_factory=list, max_length=30)
    enabled: bool = True
    market: str = Field(default="", max_length=30)
    ticker: str = Field(default="", max_length=30)
    league_id: str = Field(default="", max_length=80)

    @field_validator("name", "market", "ticker", "league_id")
    @classmethod
    def trim(cls, value, info):
        value = value.strip()
        if info.field_name == "name" and not value:
            raise ValueError("关注名称不能为空")
        return value

    @field_validator("aliases", "keywords", "exclude_keywords")
    @classmethod
    def terms(cls, values):
        if any(not value.strip() or len(value) > 120 for value in values):
            raise ValueError("每个关键词应为 1–120 个字符")
        return list(dict.fromkeys(value.strip() for value in values))

    @model_validator(mode="after")
    def league_for_team(self):
        if self.league_id and self.type not in ("team", "league"):
            raise ValueError("只有球队或联赛关注可以指定联赛选项")
        return self


class Schedule(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    time: str
    days: list[int] = Field(min_length=1, max_length=7)
    categories: list[Category] = Field(min_length=1, max_length=4)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("计划名称不能为空")
        return value.strip()

    @field_validator("time")
    @classmethod
    def time_valid(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("时间格式应为 HH:MM")
        return value

    @field_validator("days")
    @classmethod
    def days_valid(cls, value):
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("星期应为 0–6，周一为 0")
        return sorted(set(value))

    @field_validator("categories")
    @classmethod
    def unique_categories(cls, value):
        return list(dict.fromkeys(value))


class Source(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["rss", "steam"] = "rss"
    url: str = Field(default="", max_length=2048)
    category: Category
    enabled: bool = True
    steam_appid: int | None = Field(default=None, gt=0, le=2_147_483_647)

    @model_validator(mode="after")
    def validate_source(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("来源名称不能为空")
        if self.kind == "steam":
            if not self.steam_appid:
                raise ValueError("Steam 来源需要有效的 App ID")
            self.url = f"https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid={self.steam_appid}&count=100&maxlength=1800&format=json"
        else:
            self.url = public_url_syntax(self.url)
            self.steam_appid = None
        return self


class Settings(StrictModel):
    timezone: str = Field(default="Asia/Shanghai", max_length=80)
    catch_up: bool = True
    catch_up_hours: int = Field(default=4, ge=1, le=72)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("未知时区，请使用 Asia/Shanghai 等 IANA 名称")
        return value


class FetchRequest(StrictModel):
    categories: list[Category] = Field(default_factory=lambda: list(CATEGORIES), min_length=1, max_length=4)


class ArticlePatch(StrictModel):
    saved: bool | None = None
    read: bool | None = None


def public_url_syntax(value):
    try:
        parts = urlsplit(value.strip())
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
                or parts.port not in (None, 80, 443) or "\\" in value
                or any(ord(char) < 32 for char in value)):
            raise ValueError("只支持不含账号信息的公网 HTTP/HTTPS 地址及 80/443 端口")
        hostname = parts.hostname.encode("idna").decode("ascii").lower()
        if hostname in ("localhost", "localhost.localdomain") or hostname.endswith((".localhost", ".local", ".internal")):
            raise ValueError("来源必须使用公网地址")
        import ipaddress
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("来源必须使用公网地址")
        host = f"[{hostname}]" if ":" in hostname else hostname
        if parts.port is not None:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))
    except (UnicodeError, OverflowError) as exc:
        raise ValueError("来源网址格式无效") from exc
