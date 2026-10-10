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
      let format = route.format;
      if (route.format === "telegram-reader" || route.format === "x-reader") {
        const payload = JSON.parse(content);
        const page = payload.data?.html || payload.data?.content;
        if (payload.code !== 200 || typeof page !== "string" || payload.data.warning) throw new Error("公开页面读取服务未返回有效页面");
        content = page;
        if (new TextEncoder().encode(content).byteLength > MAX_BYTES) throw new Error("公开页面超过 2 MB");
        format = route.format === "x-reader" ? "x" : "telegram";
      }
      if (route.format === "cache") {
        const data = JSON.parse(content);
        const fetchedAt = Date.parse(data.fetched_at);
        if (data.source_url !== route.sourceUrl || typeof data.content !== "string" || !Number.isFinite(fetchedAt)) throw new Error("缓存格式或来源不匹配");
        if (Date.now() - fetchedAt > 6 * 3600000 || fetchedAt - Date.now() > 5 * 60000) throw new Error("缓存已过期或时间无效");
        format = data.format || "rss";
        if (format !== route.expectedFormat) throw new Error("缓存类型与消息来源不一致");
        content = data.content;
        if (new TextEncoder().encode(content).byteLength > MAX_BYTES) throw new Error("缓存内容超过 2 MB");
      }
      if (route.format === "x-json") {
        const payload = JSON.parse(content);
        if (payload.status !== "ok" || !Array.isArray(payload.items) || payload.items.length > 100) throw new Error("X 订阅转换未返回有效动态");
        const doc = document.implementation.createDocument(null, "rss");
        const channel = doc.createElement("channel"); doc.documentElement.append(channel);
        for (const entry of payload.items) {
          const item = doc.createElement("item"); channel.append(item);
          for (const [tag, value] of Object.entries({ title: entry.title, link: entry.link, description: entry.content || entry.description, pubDate: entry.pubDate })) {
            const child = doc.createElement(tag); child.textContent = typeof value === "string" ? value : ""; item.append(child);
          }
        }
        content = new XMLSerializer().serializeToString(doc); format = "x";
      }
      if (format === "x") {
        validateXContent(content, route.sourceUrl, route.postHosts);
        return { content, format: "x" };
      }
      if (format === "telegram") {
        const preview = document.createElement("template");
        preview.innerHTML = content;
        const channel = new URL(route.sourceUrl).pathname.split("/").filter(Boolean).at(-1).toLowerCase();
        const valid = Array.from(preview.content.querySelectorAll(".tgme_widget_message[data-post]")).some(message => {
          const post = message.getAttribute("data-post").split("/");
          return post.length === 2 && post[0].toLowerCase() === channel && /^[1-9][0-9]*$/.test(post[1]) && (route.history || message.querySelector(".tgme_widget_message_text")?.textContent.trim());
        });
        const channelHeader = Array.from(preview.content.querySelectorAll(".tgme_channel_info_header_username")).some(node => node.textContent.trim().toLowerCase() === `@${channel}`);
        if (!valid && !(route.history && channelHeader)) throw new Error("频道未返回可读取的公开文字消息");
        return { content, format: "telegram" };
      }
      if (format === "rss2json") {
        const data = JSON.parse(content);
        if (data.status !== "ok" || !Array.isArray(data.items)) throw new Error("转接未返回有效资讯");
      } else {
        if (/<!DOCTYPE|<!ENTITY/i.test(content)) throw new Error("来源格式无效");
        const xml = new DOMParser().parseFromString(content, "application/xml");
        if (xml.querySelector("parsererror") || !["rss", "feed", "RDF"].includes(xml.documentElement.localName)) throw new Error("来源未返回 RSS 或 Atom");
      }
      return { content, format: format === "rss2json" ? "rss2json" : "rss" };
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
    if (source.kind === "x") {
      const mirrors = source.routes || [];
      const official = mirrors.find(route => new URL(route.url).hostname === "syndication.twitter.com");
      const rss = mirrors.filter(route => new URL(route.url).pathname.endsWith("/rss"));
      const options = [
        ...(official ? [
          { name: "X 公开账号页面", url: `https://api.allorigins.win/raw?url=${encodeURIComponent(official.url)}`, sourceUrl: source.url, format: "x", timeout: 10000 },
          { name: "X 公开页面读取", url: `https://r.jina.ai/${official.url}`, sourceUrl: source.url, format: "x-reader", timeout: 20000,
            headers: { Accept: "application/json", "X-Respond-With": "html" } },
        ] : []),
        ...rss.flatMap(route => [
          { name: "公开 X RSS", url: route.url, sourceUrl: source.url, format: "x", timeout: 4000 },
          { name: "X RSS 转换", url: `https://api.rss2json.com/v1/api.json?rss_url=${encodeURIComponent(route.url)}`, sourceUrl: source.url, format: "x-json", timeout: 8000 },
          { name: "公开 X 页面", url: `https://api.allorigins.win/raw?url=${encodeURIComponent(route.url.replace(/\/rss$/, ""))}`, sourceUrl: source.url, format: "x", timeout: 8000 },
          { name: "X RSS 读取", url: `https://api.allorigins.win/raw?url=${encodeURIComponent(route.url)}`, sourceUrl: source.url, format: "x", timeout: 8000 },
        ]),
      ];
      if (source.cache_url) options.unshift({ name: "GitHub 动态缓存", url: source.cache_url, sourceUrl: source.url, format: "cache", expectedFormat: "x", timeout: 8000 });
      const postHosts = rss.map(route => new URL(route.url).hostname);
      return options.map(route => ({ ...route, postHosts }));
    }
    if (source.kind === "telegram") {
      const options = [
        { name: "Telegram 公开页面", url: source.url, sourceUrl: source.url, format: "telegram" },
        { name: "AllOrigins", url: `https://api.allorigins.win/raw?url=${encoded}`, sourceUrl: source.url, format: "telegram" },
        { name: "频道读取", url: `https://r.jina.ai/${source.url}`, sourceUrl: source.url, format: "telegram-reader", timeout: 25000,
          headers: { Accept: "application/json", "X-Respond-With": "html" } },
      ];
      if (source.cache_url) options.unshift({ name: "GitHub 新闻缓存", url: source.cache_url, sourceUrl: source.url, format: "cache", expectedFormat: "telegram" });
      return options;
    }
    const options = [
      { name: "rss2json", url: `https://api.rss2json.com/v1/api.json?rss_url=${encoded}`, format: "rss2json" },
      { name: "AllOrigins", url: `https://api.allorigins.win/raw?url=${encoded}`, format: "rss" },
    ];
    if (new URL(source.url).hostname === "www.espn.com") options.unshift({ name: "直接读取", url: source.url, format: "rss" });
    if (source.cache_url) options.unshift({ name: "GitHub 新闻缓存", url: source.cache_url, sourceUrl: source.url, format: "cache", expectedFormat: "rss" });
    return options;
  }

  function validateXContent(content, sourceUrl, postHosts = []) {
    const username = new URL(sourceUrl).pathname.slice(1).toLowerCase();
    const allowedHosts = new Set(["x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com", ...postHosts]);
    const isPost = value => {
      try {
        const url = new URL(value, sourceUrl);
        const match = /^\/([A-Za-z0-9_]{1,15})\/status\/([1-9][0-9]{0,19})\/?$/.exec(url.pathname);
        return Boolean(["http:", "https:"].includes(url.protocol) && allowedHosts.has(url.hostname) && !url.username && !url.password && !url.port
          && match && match[1].toLowerCase() === username && BigInt(match[2]) <= 9223372036854775807n);
      } catch { return false; }
    };
    if (/^\s*(?:<\?xml\b|<rss\b|<feed\b|<rdf:RDF\b)/i.test(content)) {
      if (/<!DOCTYPE|<!ENTITY/i.test(content)) throw new Error("X 订阅格式无效");
      const doc = new DOMParser().parseFromString(content, "application/xml");
      if (!doc.querySelector("parsererror") && Array.from(doc.querySelectorAll("item, entry")).some(item => {
        const link = item.querySelector("link");
        return link && isPost(link.getAttribute("href") || link.textContent.trim()) && item.querySelector("description, summary, content")?.textContent.trim();
      })) return;
    } else {
      const doc = document.createElement("template"); doc.innerHTML = content;
      const data = doc.content.querySelector('script#__NEXT_DATA__[type="application/json"]');
      if (data) {
        const payload = JSON.parse(data.textContent);
        const entries = payload?.props?.pageProps?.timeline?.entries;
        if (Array.isArray(entries) && entries.length <= 200 && entries.some(entry => {
          const tweet = entry?.type === "tweet" && entry.content?.tweet;
          const text = tweet?.full_text || tweet?.text;
          const id = tweet?.id_str;
          return typeof tweet?.user?.screen_name === "string" && tweet.user.screen_name.toLowerCase() === username && !tweet.user.protected
            && typeof id === "string" && isPost(`https://x.com/${username}/status/${id}`) && typeof text === "string" && text.trim()
            && (!tweet.permalink || (typeof tweet.permalink === "string" && isPost(tweet.permalink)
              && new URL(tweet.permalink, sourceUrl).pathname.replace(/\/$/, "").toLowerCase() === `/${username}/status/${id}`));
        })) return;
        throw new Error("X 公开页面未返回这个账号的有效动态");
      }
      if (Array.from(doc.content.querySelectorAll(".timeline-item[data-username]")).some(item =>
        item.dataset.username.toLowerCase() === username && item.querySelector(".tweet-content")?.textContent.trim()
        && isPost(item.querySelector(".tweet-link")?.getAttribute("href")))) return;
    }
    throw new Error("免费 X 来源暂无可读取的该账号动态");
  }

  async function readX(source, signal) {
    const controller = new AbortController();
    const abort = () => controller.abort(); signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
    const timeout = setTimeout(abort, 45000);
    const options = routes(source); let next = 0;
    async function worker() {
      while (next < options.length && !controller.signal.aborted) {
        try { return await readFeed(options[next++], controller.signal); }
        catch (error) { if (controller.signal.aborted) throw error; }
      }
      throw new Error("免费 X 来源暂时不可用");
    }
    try { return await Promise.any([worker(), worker()]); }
    catch { throw new Error("免费 X 来源暂时不可用，可以稍后重试"); }
    finally { abort(); clearTimeout(timeout); signal.removeEventListener("abort", abort); }
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
        if (source.kind === "x") {
          try { const feed = await readX(source, signal); content = feed.content; format = feed.format; }
          catch (error) { if (signal.aborted) return; failures.push(error.message); }
        }
        for (const route of source.kind === "x" ? [] : routes(source)) {
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

  async function historyPage(source, signal) {
    for (const route of routes({ kind: "telegram", url: source.fetch_url })) {
      try { return (await readFeed({ ...route, history: true }, signal)).content; }
      catch (error) { if (signal.aborted) throw error; }
    }
    throw new Error("暂时无法加载更早消息，请稍后重试");
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

  return { collect, articlePage, historyPage };
})();
