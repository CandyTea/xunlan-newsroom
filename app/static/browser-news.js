"use strict";

window.BrowserNews = (() => {
  const MAX_BYTES = 2 * 1024 * 1024;

  async function readFeed(route, signal) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) controller.abort();
    const timeout = setTimeout(abort, 15000);
    try {
      const response = await fetch(route.url, {
        mode: "cors", credentials: "omit", referrerPolicy: "no-referrer", signal: controller.signal,
      });
      if (!response.ok) throw new Error(`来源返回 HTTP ${response.status}`);
      if (Number(response.headers.get("Content-Length")) > MAX_BYTES) throw new Error("来源内容超过 2 MB");
      let content = "";
      if (response.body) {
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let size = 0;
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            size += value.byteLength;
            if (size > MAX_BYTES) { await reader.cancel(); throw new Error("来源内容超过 2 MB"); }
            content += decoder.decode(value, { stream: true });
          }
          content += decoder.decode();
        } finally { reader.releaseLock(); }
      } else {
        content = await response.text();
        if (new TextEncoder().encode(content).byteLength > MAX_BYTES) throw new Error("来源内容超过 2 MB");
      }
      if (route.format === "rss2json") {
        const data = JSON.parse(content);
        if (data.status !== "ok" || !Array.isArray(data.items)) throw new Error("转接未返回有效资讯");
      } else {
        if (/<!DOCTYPE|<!ENTITY/i.test(content)) throw new Error("来源格式无效");
        const xml = new DOMParser().parseFromString(content, "application/xml");
        if (xml.querySelector("parsererror") || !["rss", "feed", "RDF"].includes(xml.documentElement.localName)) throw new Error("来源未返回 RSS 或 Atom");
      }
      return content;
    } finally {
      clearTimeout(timeout);
      signal.removeEventListener("abort", abort);
    }
  }

  function routes(source) {
    const encoded = encodeURIComponent(source.url);
    const options = [
      { url: `https://api.rss2json.com/v1/api.json?rss_url=${encoded}`, format: "rss2json" },
      { url: `https://api.allorigins.win/raw?url=${encoded}`, format: "rss" },
    ];
    if (new URL(source.url).hostname === "www.espn.com") options.unshift({ url: source.url, format: "rss" });
    return options;
  }

  async function collect({ sources, importFeed, onProgress, signal }) {
    const result = { sourceIds: [], newCount: 0, failed: [] };
    let next = 0;
    let completed = 0;
    async function worker() {
      while (next < sources.length && !signal.aborted) {
        const source = sources[next++];
        let content;
        let format;
        for (const route of routes(source)) {
          try { content = await readFeed(route, signal); format = route.format; break; }
          catch (error) {
            if (signal.aborted) return;
            // Import failures are handled separately; only external reads switch routes.
          }
        }
        if (signal.aborted) return;
        if (content !== undefined) {
          try {
            const imported = await importFeed({ source_id: source.id, source_url: source.url, format, content });
            if (signal.aborted) return;
            result.sourceIds.push(source.id);
            result.newCount += imported.new_count;
          } catch (error) {
            if (signal.aborted) return;
            if ([401, 403].includes(error.status)) throw error;
            result.failed.push({ name: source.name, reason: error.message });
          }
        } else result.failed.push({ name: source.name, reason: "直连或转接不可用，请稍后重试" });
        onProgress(++completed, sources.length, result);
      }
    }
    await Promise.all([worker(), worker()]);
    return result;
  }

  return { collect };
})();
