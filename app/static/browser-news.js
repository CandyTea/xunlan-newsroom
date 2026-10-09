"use strict";

window.BrowserNews = (() => {
  const MAX_BYTES = 2 * 1024 * 1024;

  async function readFeed(route, signal) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) controller.abort();
    const duration = route.timeout || 15000;
    const timeout = setTimeout(abort, duration);
    try {
      const response = await fetch(route.url, {
        mode: "cors", credentials: "omit", referrerPolicy: "no-referrer", signal: controller.signal,
        headers: route.headers,
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
      if (route.format === "article") return { content };
      if (route.format === "cache") {
        const data = JSON.parse(content);
        const fetchedAt = Date.parse(data.fetched_at);
        if (data.source_url !== route.sourceUrl || typeof data.content !== "string" || !Number.isFinite(fetchedAt)) throw new Error("缓存格式或来源不匹配");
        if (Date.now() - fetchedAt > 6 * 3600000 || fetchedAt - Date.now() > 5 * 60000) throw new Error("缓存已过期或时间无效");
        content = data.content;
        if (new TextEncoder().encode(content).byteLength > MAX_BYTES) throw new Error("缓存内容超过 2 MB");
      }
      if (route.format === "rss2json") {
        const data = JSON.parse(content);
        if (data.status !== "ok" || !Array.isArray(data.items)) throw new Error("转接未返回有效资讯");
      } else {
        if (/<!DOCTYPE|<!ENTITY/i.test(content)) throw new Error("来源格式无效");
        const xml = new DOMParser().parseFromString(content, "application/xml");
        if (xml.querySelector("parsererror") || !["rss", "feed", "RDF"].includes(xml.documentElement.localName)) throw new Error("来源未返回 RSS 或 Atom");
      }
      return { content, format: route.format === "rss2json" ? "rss2json" : "rss" };
    } catch (error) {
      if (controller.signal.aborted && !signal.aborted) throw new Error(`请求超过 ${duration / 1000} 秒，已超时`);
      if (error instanceof TypeError) throw new Error("浏览器无法读取响应，可能是网络或跨域限制");
      if (error instanceof SyntaxError) throw new Error("返回内容不是有效 JSON");
      throw error;
    } finally {
      clearTimeout(timeout);
      signal.removeEventListener("abort", abort);
    }
  }

  function routes(source) {
    const encoded = encodeURIComponent(source.url);
    const options = [
      { name: "rss2json", url: `https://api.rss2json.com/v1/api.json?rss_url=${encoded}`, format: "rss2json" },
      { name: "AllOrigins", url: `https://api.allorigins.win/raw?url=${encoded}`, format: "rss" },
    ];
    if (new URL(source.url).hostname === "www.espn.com") options.unshift({ name: "直接读取", url: source.url, format: "rss" });
    if (source.cache_url) options.unshift({ name: "GitHub 新闻缓存", url: source.cache_url, sourceUrl: source.url, format: "cache" });
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
        const failures = [];
        for (const route of routes(source)) {
          try { const feed = await readFeed(route, signal); content = feed.content; format = feed.format; break; }
          catch (error) {
            if (signal.aborted) return;
            failures.push(`${route.name}：${error.message}`);
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
            result.failed.push({ source_id: source.id, source_url: source.url, name: source.name, reason: error.message });
          }
        } else result.failed.push({ source_id: source.id, source_url: source.url, name: source.name, reason: failures.join("；") });
        onProgress?.(++completed, sources.length, result);
      }
    }
    await Promise.all([worker(), worker()]);
    return result;
  }

  async function articlePage(url, signal, reader = false) {
    const route = reader ? {
      url: `https://r.jina.ai/${url}`, format: "article", timeout: 25000,
      headers: { Accept: "application/json", "X-Respond-With": "html" },
    } : { url: `https://api.allorigins.win/raw?url=${encodeURIComponent(url)}`, format: "article" };
    const result = await readFeed(route, signal);
    if (!reader) return result.content;
    const payload = JSON.parse(result.content);
    if (payload.code !== 200 || typeof payload.data?.content !== "string" || payload.data.warning) throw new Error("正文读取服务未返回有效页面");
    const html = payload.data.content;
    if (!/<(?:!doctype|html|article|p)[\s>]/i.test(html)) throw new Error("正文读取服务未返回 HTML");
    return html;
  }

  return { collect, articlePage };
})();
