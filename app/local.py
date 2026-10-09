"""Run the reader on this computer, using its configured HTTP proxy when available."""
import argparse
import os
import threading
import time
import webbrowser
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener, getproxies

import uvicorn

from .main import create_app


def configure_local_network():
    if os.getenv("NEWSROOM_FETCH_MODE", "auto").strip().lower() == "direct":
        return False
    if os.getenv("NEWSROOM_OUTBOUND_PROXY", "").strip():
        return True
    proxies = getproxies()
    for protocol in ("https", "http"):
        proxy = proxies.get(protocol, "")
        if proxy and urlsplit(proxy).scheme in {"http", "https"}:
            os.environ["NEWSROOM_OUTBOUND_PROXY"] = proxy
            return True
    return False


def open_reader_when_ready(stop):
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 30
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            with opener.open("http://127.0.0.1:8000/healthz", timeout=1) as response:
                if response.status == 200 and response.read(256) == b'{"status":"ok"}':
                    webbrowser.open("http://127.0.0.1:8000")
                    return
        except OSError:
            pass
        stop.wait(0.25)


def main(argv=None):
    parser = argparse.ArgumentParser(description="在当前电脑采集、保存和阅读资讯")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    args = parser.parse_args(argv)
    using_proxy = configure_local_network()
    app = create_app()
    print("新闻由当前电脑采集，保存在本机；不会同步到腾讯云。", flush=True)
    print("已使用本机 HTTP(S) 代理配置。" if using_proxy else "使用本机直连网络；未检测到 HTTP(S) 代理配置。", flush=True)
    stop = threading.Event()
    if not args.no_browser:
        threading.Thread(target=open_reader_when_ready, args=(stop,), daemon=True).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=8000, workers=1)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
