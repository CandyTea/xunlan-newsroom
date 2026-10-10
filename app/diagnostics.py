"""Read-only network checks for configured news sources."""
import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

from .collector import fetch_config, fetch_public, parse_source, safe_error
from .dns import dns_mode
from .db import load_default_sources
from .models import CATEGORIES

ROOT = Path(__file__).resolve().parent.parent


def load_sources():
    """Read configured sources without initializing or changing the database."""
    data_dir = Path(os.getenv("NEWSROOM_DATA_DIR", ROOT / "data"))
    database_path = data_dir / "newsroom.sqlite3"
    if not database_path.is_file():
        return [dict(source) for _, source in load_default_sources()]

    uri = database_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(
            "SELECT id,name,kind,url,category,enabled,steam_appid FROM sources ORDER BY category,id"
        )]
    finally:
        connection.close()


async def _check_sources(sources):
    semaphore = asyncio.Semaphore(3)

    def report(result):
        source = result["source"]
        status = "通过" if result["ok"] else "失败"
        line = f"{source['name']} [{source['category']}] {status} count={result['count']}"
        if result["ok"] and result["count"] == 0:
            line += "（连接有效，暂无条目）"
        if result["error"]:
            line += f" error={result['error']}"
        print(line, flush=True)

    async def check(source):
        async with semaphore:
            try:
                if source["kind"] == "x":
                    from .xfeeds import fetch_x
                    articles, _ = await fetch_x(source["url"])
                else:
                    data, final_url = await fetch_public(source["url"])
                    articles = parse_source(data, source, final_url)
                result = {"source": source, "ok": True, "count": len(articles), "error": ""}
            except Exception as exc:
                result = {"source": source, "ok": False, "count": 0, "error": safe_error(exc)}
            report(result)
            return result

    return await asyncio.gather(*(check(source) for source in sources))


def _parser():
    parser = argparse.ArgumentParser(description="检查新闻来源的网络出口与可读取条目数")
    parser.add_argument("--category", choices=CATEGORIES, help="只检查指定类别；默认检查所有启用来源")
    parser.add_argument("--include-disabled", action="store_true", help="同时检查已关闭的来源")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)

    try:
        mode, proxy = fetch_config()
    except (ValueError, OSError):
        print("网络配置无效，请检查抓取模式和代理设置", file=sys.stderr)
        return 2

    try:
        sources = load_sources()
    except (OSError, sqlite3.Error, ValueError):
        print("无法读取来源配置", file=sys.stderr)
        return 2

    selected = [source for source in sources
                if (args.category is None or source["category"] == args.category)
                and (source["enabled"] or args.include_disabled)]
    if not selected:
        print("没有符合条件的来源可供检查", file=sys.stderr)
        return 2

    proxy_configured = bool(os.getenv("NEWSROOM_OUTBOUND_PROXY", "").strip())
    print(f"抓取模式: {mode}; DNS 模式: {dns_mode()}; 已配置代理: {'是' if proxy_configured else '否'}; "
          f"当前模式使用代理: {'是' if proxy else '否'}", flush=True)
    results = asyncio.run(_check_sources(selected))
    passed = sum(result["ok"] for result in results)
    failed = len(results) - passed
    print(f"汇总: 通过 {passed}，失败 {failed}", flush=True)
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
