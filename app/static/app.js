"use strict";

const CATEGORIES = { games: "游戏", sports: "体育", stocks: "股票", politics: "政治" };
const WATCH_TYPES = { company: "公司", studio: "游戏工作室", team: "球队", country: "国家", politician: "政客" };
const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const ICONS = {
  news: '<rect x="3" y="4" width="18" height="16" rx="1"/><path d="M7 8h6v5H7zM16 8h2M16 12h2M7 16h11"/>',
  bookmark: '<path d="M6 3h12v18l-6-4-6 4z"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 10h18M7 14h2M15 14h2M7 17h2"/>',
  settings: '<path d="m9 3-.7 2.3-2 .9-2.2-.5-1.5 2.6 1.5 1.8v2.3l-1.5 1.8 1.5 2.6 2.3-.5 2 .9.7 2.3h3l.7-2.3 2-.9 2.2.5 1.5-2.6-1.5-1.8V10l1.5-1.8-1.5-2.6-2.2.5-2-.9L12 3z" transform="translate(1.5 1)"/><circle cx="12" cy="12" r="3"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5M6.1 6.1a8 8 0 0 1 13.2 2.5M17.9 17.9A8 8 0 0 1 4.7 15.4"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  edit: '<path d="m15 4 5 5M4 20l4-1 12-12a2 2 0 0 0-3-3L5 16z"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  external: '<path d="M14 3h7v7M21 3l-11 11M10 3H4v17h17v-6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  inbox: '<path d="m3 4-1 12v4h20v-4L21 4zM2 14h6l2 3h4l2-3h6"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 15v6h16v-6"/>',
  logout: '<path d="M10 4H4v16h6M10 12h11m-5-5 5 5-5 5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7v.1"/>',
  warning: '<path d="m12 3 10 18H2zM12 9v5M12 17v.1"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
};
const state = { session: null, view: "news", category: "", q: "", saved: false, unread: false, watchId: "", page: 1, articles: [], total: 0, pageSize: 20, watches: [], sources: [], schedules: [], settings: { timezone: "Asia/Shanghai", catch_up: true, catch_up_hours: 4 }, status: {}, manageWatches: false, renderId: 0, modalId: 0, toastTimer: null, fetching: false, pollBusy: false };
const $ = (selector, root = document) => root.querySelector(selector);
const main = $("#main-content");
const modal = $("#modal");

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "checked") node.checked = Boolean(value);
    else if (key === "disabled") node.disabled = Boolean(value);
    else if (key === "value") node.value = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child !== null && child !== undefined && child !== false) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
function icon(name) {
  const span = el("span", { "aria-hidden": "true" });
  span.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ICONS.info}</svg>`;
  return span.firstElementChild;
}
function button(text, handler, className = "button", iconName = null, attrs = {}) {
  return el("button", { type: "button", class: className, onclick: handler, ...attrs }, [iconName && icon(iconName), text]);
}
function safeUrl(value) {
  try { const url = new URL(String(value)); return ["http:", "https:"].includes(url.protocol) ? url.href : null; } catch { return null; }
}
function toast(message) {
  clearTimeout(state.toastTimer);
  const node = $("#toast"); node.textContent = message; node.hidden = false;
  state.toastTimer = setTimeout(() => { node.hidden = true; }, 4500);
}
class ApiError extends Error { constructor(message, status) { super(message); this.status = status; } }
async function api(path, options = {}, retry = true) {
  const method = options.method || "GET";
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && state.session?.csrf_token) headers["X-CSRF-Token"] = state.session.csrf_token;
  let response;
  try { response = await fetch(`/api${path}`, { method, headers, credentials: "same-origin", body: options.body === undefined ? undefined : JSON.stringify(options.body) }); }
  catch { throw new ApiError("无法连接服务器，请检查网络后重试。", 0); }
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) {
    if (response.status === 401 && path !== "/login") {
      state.renderId += 1; state.session = { authenticated: false, setup_required: false }; closeModal(); showAuth();
      throw new ApiError("登录已过期，请重新登录。", 401);
    }
    if (response.status === 403 && retry && method !== "GET" && !["/setup", "/login"].includes(path)) {
      const fresh = await api("/session", {}, false);
      if (fresh.authenticated && fresh.csrf_token !== state.session?.csrf_token) { state.session = fresh; return api(path, options, false); }
    }
    let detail = data.detail;
    if (Array.isArray(detail)) detail = detail.map(item => item.msg || "请检查输入内容").join("；");
    throw new ApiError(typeof detail === "string" ? detail : `请求失败（${response.status}），请稍后重试。`, response.status);
  }
  return data;
}
function showError(error) { if (error.status !== 401) toast(error.message || "操作失败，请稍后重试。"); }
function dateTime(value, options = {}) {
  if (!value) return "尚无记录";
  const date = new Date(value); if (Number.isNaN(date.getTime())) return "时间未知";
  try { return new Intl.DateTimeFormat("zh-CN", { timeZone: state.settings.timezone, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false, ...options }).format(date); }
  catch { return date.toLocaleString("zh-CN"); }
}
function articleTime(value) {
  if (!value) return "发布时间未提供";
  const date = new Date(value); if (Number.isNaN(date.getTime())) return "发布时间未提供";
  const delta = Date.now() - date.getTime();
  if (delta >= 0 && delta < 60000) return "刚刚";
  if (delta >= 60000 && delta < 3600000) return `${Math.floor(delta / 60000)} 分钟前`;
  if (delta >= 3600000 && delta < 86400000) return `${Math.floor(delta / 3600000)} 小时前`;
  return dateTime(value);
}
function loading(message = "正在载入…") {
  return el("div", { class: "loading-state", role: "status", "aria-label": message }, [el("p", { text: message }), ...Array.from({ length: 3 }, () => el("div", { class: "loading-row", "aria-hidden": "true" }, [el("div", { class: "loading-bar" }), el("div", { class: "loading-bar short" })]))]);
}
function errorPanel(error, retry) {
  return el("div", { class: "error-state", role: "alert" }, [el("strong", { text: "暂时无法载入" }), el("p", { text: error.message }), button("重试", retry, "button button-small", "refresh")]);
}
function empty(title, copy, actions = [], iconName = "inbox") {
  return el("div", { class: "empty-state" }, [icon(iconName), el("h2", { text: title }), el("p", { text: copy }), actions.length > 0 && el("div", { class: "empty-actions" }, actions)]);
}
function heading(title, subtitle, actions = []) {
  return el("div", { class: "view-heading" }, [el("div", {}, [el("h1", { text: title }), el("p", { text: subtitle })]), actions.length > 0 && el("div", { class: "heading-actions" }, actions)]);
}
function closeModal() { state.modalId += 1; if (modal.open) modal.close(); }
function openModal(title, content, className = "") {
  $("#modal-title").textContent = title; $("#modal-body").replaceChildren(content); modal.className = `modal ${className}`;
  if (!modal.open) modal.showModal();
  modal.scrollTop = 0;
  return ++state.modalId;
}
function modalError(form, error) {
  let node = $(".form-error", form);
  if (!node) { node = el("p", { class: "form-error", role: "alert" }); form.insertBefore(node, $(".form-footer", form)); }
  node.textContent = error.message; node.hidden = false; node.scrollIntoView({ block: "nearest" });
}
function formField(title, input, hint) { return el("label", {}, [title, input, hint && el("small", { text: hint })]); }
function input(name, value = "", attrs = {}) { return el("input", { name, value, ...attrs }); }
function select(name, options, value, attrs = {}) { return el("select", { name, ...attrs }, Object.entries(options).map(([key, title]) => el("option", { value: key, selected: key === String(value) }, title))); }
function check(name, title, checked = true) { return el("label", { class: "checkbox-label" }, [input(name, "on", { type: "checkbox", checked }), title]); }
function formFooter(submitText = "保存") { return el("div", { class: "form-footer" }, [button("取消", closeModal), el("button", { type: "submit", class: "button button-primary", text: submitText })]); }
function listValues(value) { return String(value || "").split(/[,，、;；\n]+/).map(item => item.trim()).filter(Boolean); }
function choices(name, labels, selected) {
  return el("div", { class: "choice-grid" }, Object.entries(labels).map(([value, title]) => el("label", {}, [input(name, value, { type: "checkbox", checked: selected.map(String).includes(value) }), title])));
}
async function confirmDelete(title, copy, action) {
  const box = el("div", {}, [el("p", { class: "confirm-text", text: copy })]);
  const submit = button("确认删除", async () => { submit.disabled = true; try { await action(); closeModal(); toast("已删除"); await refreshData(); await renderView(); } catch (error) { showError(error); submit.disabled = false; } }, "button button-danger", "trash");
  box.append(el("div", { class: "form-footer" }, [button("取消", closeModal), submit])); openModal(title, box);
}

function showAuth() {
  $("#boot").hidden = true; $("#app").hidden = true; $("#auth").hidden = false;
  const setup = state.session?.setup_required;
  $("#auth-title").textContent = setup ? "建立你的阅读室" : "欢迎回来";
  $("#auth-description").textContent = setup ? "创建管理员账号，再选择来源与关注对象。" : "登录，继续阅读你关注的资讯。";
  $("#auth-submit").textContent = setup ? "创建账号" : "登录";
  $("#auth-password").autocomplete = setup ? "new-password" : "current-password";
  $("#auth-password").minLength = setup ? 8 : 1;
  $("#auth-username").minLength = setup ? 2 : 1;
  $("#auth-password").placeholder = setup ? "至少 8 个字符，建议使用较长密码" : "输入密码";
  $("#setup-token-field").hidden = !(setup && state.session.setup_token_required);
  $("#auth-setup-token").required = Boolean(setup && state.session.setup_token_required);
  if (state.session?.username) $("#auth-username").value = state.session.username;
}
async function refreshData() {
  const results = await Promise.allSettled([api("/settings"), api("/watches"), api("/status")]);
  if (!state.session?.authenticated) return;
  if (results[0].status === "fulfilled") state.settings = results[0].value;
  if (results[1].status === "fulfilled") state.watches = results[1].value.items || [];
  if (results[2].status === "fulfilled") state.status = results[2].value;
  renderSidebar(); updateFetchButtons();
}
async function enterApp() {
  $("#boot").hidden = true; $("#auth").hidden = true; $("#app").hidden = false;
  await refreshData(); if (!state.session?.authenticated) return;
  route();
}
function route() {
  const next = location.hash.slice(1); state.view = ["news", "following", "schedules", "settings"].includes(next) ? next : "news";
  state.page = 1; state.watchId = ""; state.manageWatches = false;
  closeModal(); renderView();
  document.querySelectorAll("[data-view]").forEach(link => { if (link.dataset.view === state.view) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current"); });
  document.title = `${{ news: "新闻", following: "我的关注", schedules: "收取计划", settings: "设置与来源" }[state.view]} · 讯览`;
}
async function renderView() {
  if (!state.session?.authenticated) return;
  const id = ++state.renderId;
  if (state.view === "news" || state.view === "following") { if (state.manageWatches) return renderWatches(id); return renderReader(id); }
  if (state.view === "schedules") return renderSchedules(id);
  return renderSettings(id);
}
function setFilter(key, value) { state[key] = value; state.page = 1; renderView(); }
function readerEmpty() {
  if (state.q || state.saved || state.unread || state.watchId || state.category) return empty("还没有符合条件的资讯", "试试其他栏目或关键词，也可以清除筛选查看已收取的全部资讯。", [button("清除筛选", () => { state.q = ""; state.saved = false; state.unread = false; state.watchId = ""; state.category = ""; state.page = 1; renderView(); }, "button button-small")]);
  if (state.view === "following") return empty(state.watches.length ? "等待你的关注资讯" : "从一个关注对象开始", state.watches.length ? "目前还没有资讯命中关注对象。收取新资讯，或编辑别名与上下文关键词来调整匹配。" : "添加公司、工作室、球队、国家或政客。讯览会从真实资讯中找出相关报道。", [button(state.watches.length ? "管理关注" : "添加关注", () => state.watches.length ? showWatchManager() : watchForm(), "button button-primary", "plus"), state.watches.length && button("收取资讯", () => requestFetch(), "button", "refresh")].filter(Boolean), "bookmark");
  return empty("阅读室已准备好", "添加你信任的资讯来源，再收取第一批报道。你也可以先设置关注对象与每天的收取时间。", [button("收取资讯", () => requestFetch(), "button button-primary", "refresh"), button("管理来源", () => { location.hash = "settings"; }, "button")]);
}
async function renderReader(id) {
  const following = state.view === "following";
  const subtitle = following ? "从已收取的资讯中，找到你关心的人和事。" : "按栏目阅读，按兴趣关注。";
  const head = heading(following ? "我的关注" : "今日资讯", subtitle, following ? [button("管理关注", showWatchManager, "button", "edit")] : []);
  const categories = el("nav", { class: "category-tabs", "aria-label": "资讯栏目" }, Object.entries({ "": "全部", ...CATEGORIES }).map(([key, name]) => button(name, () => setFilter("category", key), "", null, { "aria-pressed": String(state.category === key) })));
  const search = el("form", { class: "search-form", role: "search", onsubmit: event => { event.preventDefault(); setFilter("q", $("input", search).value.trim()); } }, [input("q", state.q, { type: "search", placeholder: "搜索标题与摘要", "aria-label": "搜索资讯", maxlength: 200 }), el("button", { class: "icon-button", type: "submit", "aria-label": "搜索" }, icon("search"))]);
  const filters = el("div", { class: "reader-filters" }, [button("未读", () => setFilter("unread", !state.unread), "filter-button", "eye", { "aria-pressed": String(state.unread) }), button("已收藏", () => setFilter("saved", !state.saved), "filter-button", "bookmark", { "aria-pressed": String(state.saved) })]);
  const tools = el("div", { class: "reader-tools" }, [search, filters]);
  if (following && state.watches.length) {
    const watchSelect = select("watch_id", { "": "全部关注对象", ...Object.fromEntries(state.watches.map(w => [w.id, w.name])) }, state.watchId, { class: "watch-selector", "aria-label": "筛选关注对象", onchange: event => setFilter("watchId", event.target.value) });
    tools.append(watchSelect);
  }
  const list = el("div", { id: "reader-list", "aria-live": "polite" }, loading("正在载入资讯…"));
  main.replaceChildren(head, following && !state.watches.length ? el("div", { class: "following-intro" }, [el("p", {}, [el("strong", { text: "还没有关注对象" }), " · 添加关注后，相关资讯会出现在这里。"]), button("添加", () => watchForm(), "button button-small", "plus")]) : "", categories, tools, list);
  try {
    const params = new URLSearchParams({ page: String(state.page), page_size: String(state.pageSize), following: String(following), saved: String(state.saved), unread: String(state.unread) });
    if (state.category) params.set("category", state.category); if (state.q) params.set("q", state.q); if (state.watchId) params.set("watch_id", state.watchId);
    const data = await api(`/articles?${params}`); if (id !== state.renderId) return;
    state.articles = data.items || []; state.total = data.total || 0;
    if (!state.articles.length) { list.replaceChildren(readerEmpty()); return; }
    list.replaceChildren(el("div", { class: "list-meta" }, [el("span", { text: `共 ${state.total} 条${following ? "关注" : ""}资讯` }), el("span", { text: "按发布时间排序" })]), el("div", { class: "news-list" }, state.articles.map(articleRow)));
    const pages = Math.ceil(state.total / state.pageSize);
    if (pages > 1) list.append(el("nav", { class: "pagination", "aria-label": "资讯分页" }, [button("上一页", () => changePage(-1), "button button-small", null, { disabled: state.page <= 1 }), el("span", { text: `${state.page} / ${pages}` }), button("下一页", () => changePage(1), "button button-small", null, { disabled: state.page >= pages })]));
  } catch (error) { if (id === state.renderId && error.status !== 401) list.replaceChildren(errorPanel(error, renderView)); }
}
function changePage(delta) { state.page += delta; renderView(); window.scrollTo({ top: 0, behavior: "instant" }); }
function articleRow(article) {
  const title = el("h2", {}, button(article.title || "无标题资讯", () => articleDetail(article.id), "article-link"));
  const meta = el("div", { class: "news-meta" }, [el("span", { class: "news-category", text: CATEGORIES[article.category] || "资讯" }), el("span", { class: "meta-divider", text: "·" }), el("span", { text: article.source_name || "原始来源" }), el("span", { class: "meta-divider", text: "·" }), el("time", { datetime: article.published_at || article.fetched_at, text: articleTime(article.published_at) })]);
  const save = button(article.saved ? "已收藏" : "收藏", async event => {
    event.stopPropagation(); save.disabled = true;
    try { const updated = await api(`/articles/${article.id}`, { method: "PATCH", body: { saved: !article.saved } }); article.saved = typeof updated.saved === "boolean" ? updated.saved : !article.saved; save.replaceChildren(icon("bookmark"), article.saved ? "已收藏" : "收藏"); save.setAttribute("aria-pressed", String(article.saved)); toast(article.saved ? "已加入收藏" : "已取消收藏"); if (state.saved && !article.saved) renderView(); }
    catch (error) { showError(error); } finally { save.disabled = false; }
  }, "save-inline", "bookmark", { "aria-pressed": String(Boolean(article.saved)), "aria-label": `${article.saved ? "取消收藏" : "收藏"}：${article.title}` });
  const hits = (article.watches || []).map(w => w.name).join("、");
  const content = el("div", {}, [title, meta, article.summary && el("p", { class: "news-summary", text: article.summary }), el("div", { class: "news-tail" }, [el("span", { class: "watch-hit", text: hits ? `关注命中 · ${hits}` : "" }), save])]);
  const row = el("article", { class: `news-item${article.read ? " is-read" : ""}`, "data-article-id": article.id }, content);
  const imageUrl = safeUrl(article.image_url); if (imageUrl) row.append(el("img", { class: "news-image", src: imageUrl, alt: "", loading: "lazy", referrerpolicy: "no-referrer", onerror: event => event.target.remove() }));
  return row;
}
async function articleDetail(id) {
  const modalId = openModal("资讯", loading("正在打开资讯…"), "article-modal");
  try {
    const article = await api(`/articles/${id}`); if (!modal.open || state.modalId !== modalId) return;
    const sourceUrl = safeUrl(article.url);
    const detail = el("article", { class: "article-detail" }, [el("p", { class: "article-kicker", text: CATEGORIES[article.category] || "资讯" }), el("h1", { text: article.title || "无标题资讯" }), el("div", { class: "news-meta" }, [el("span", { text: article.source_name || "原始来源" }), el("span", { class: "meta-divider", text: "·" }), el("time", { datetime: article.published_at, text: article.published_at ? dateTime(article.published_at, { year: "numeric" }) : "发布时间未提供" })])]);
    const imageUrl = safeUrl(article.image_url); if (imageUrl) detail.append(el("img", { src: imageUrl, alt: "", referrerpolicy: "no-referrer", onerror: event => event.target.remove() }));
    detail.append(el("section", { class: "article-summary" }, [el("h3", { text: "来源摘要" }), el("p", { text: article.summary || "该来源未提供摘要，请前往原文阅读。" }), el("div", { class: "article-source-note", text: "内容由公开来源提供。摘要可能不完整，报道全文与后续更正以原始来源为准。" })]));
    if (article.watches?.length) detail.append(el("p", { class: "article-watches", text: `与你的关注相关：${article.watches.map(w => w.name).join("、")}` }));
    const save = button(article.saved ? "已收藏" : "收藏", () => patchDetail("saved", !article.saved), "button", "bookmark", { "aria-pressed": String(Boolean(article.saved)) });
    const read = button("标为未读", () => patchDetail("read", !article.read), "button", "eye");
    const original = sourceUrl ? el("a", { href: sourceUrl, target: "_blank", rel: "noopener noreferrer", class: "button button-primary" }, ["阅读原文", icon("external")]) : el("span", { class: "field-help", text: "原文链接不可用" });
    detail.append(el("div", { class: "article-actions" }, [original, save, read]));
    $("#modal-body").replaceChildren(detail); $("#modal-title").textContent = "阅读资讯";
    function sync() { save.replaceChildren(icon("bookmark"), article.saved ? "已收藏" : "收藏"); save.setAttribute("aria-pressed", String(Boolean(article.saved))); read.replaceChildren(icon("eye"), article.read ? "标为未读" : "标为已读"); }
    async function patchDetail(key, value) {
      save.disabled = true; read.disabled = true;
      try { const updated = await api(`/articles/${id}`, { method: "PATCH", body: { [key]: value } }); article[key] = typeof updated[key] === "boolean" ? updated[key] : value; sync(); updateArticle(article); toast(key === "saved" ? (value ? "已加入收藏" : "已取消收藏") : (value ? "已标为已读" : "已标为未读")); }
      catch (error) { showError(error); } finally { save.disabled = false; read.disabled = false; }
    }
    if (!article.read) { try { await api(`/articles/${id}`, { method: "PATCH", body: { read: true } }); article.read = true; updateArticle(article); } catch (error) { showError(error); } }
    sync();
  } catch (error) { if (error.status !== 401 && modal.open && state.modalId === modalId) $("#modal-body").replaceChildren(errorPanel(error, () => articleDetail(id))); }
}
function updateArticle(article) {
  const index = state.articles.findIndex(item => item.id === article.id); if (index >= 0) state.articles[index] = article;
  const row = document.querySelector(`[data-article-id="${Number(article.id)}"]`); if (row) row.replaceWith(articleRow(article));
  refreshData();
}

function showWatchManager() { state.manageWatches = true; renderView(); }
async function renderWatches(id) {
  main.replaceChildren(heading("管理关注", "名称与别名匹配资讯，可用上下文关键词缩小范围。", [button("添加关注", () => watchForm(), "button button-primary", "plus")]), el("div", { class: "sub-toolbar" }, [el("span", { text: "关注对象支持更新后重新匹配已有资讯。" }), button("返回资讯", () => { state.manageWatches = false; renderView(); }, "button button-small")]), loading());
  try {
    const data = await api("/watches"); if (id !== state.renderId) return; state.watches = data.items || [];
    main.lastElementChild.replaceWith(state.watches.length ? el("div", { class: "manage-list" }, state.watches.map(watchRow)) : empty("添加第一个关注对象", "例如一家游戏工作室、一支球队或一家公司。别名帮助讯览识别同一对象的不同名称。", [button("添加关注", () => watchForm(), "button button-primary", "plus")], "bookmark")); renderSidebar();
  } catch (error) { if (id === state.renderId && error.status !== 401) main.lastElementChild.replaceWith(errorPanel(error, renderView)); }
}
function watchRow(watch) {
  const details = [watch.aliases?.length ? `别名：${watch.aliases.join("、")}` : "未设置别名", watch.keywords?.length ? `上下文（需全部命中）：${watch.keywords.join("、")}` : "", watch.exclude_keywords?.length ? `排除：${watch.exclude_keywords.join("、")}` : "", watch.ticker ? `证券：${watch.market || ""} ${watch.ticker}`.trim() : ""].filter(Boolean);
  return el("article", { class: "manage-row" }, [el("div", { class: "manage-copy" }, [el("h3", { class: watch.enabled ? "" : "disabled-text" }, [watch.name, el("span", { class: "manage-type", text: WATCH_TYPES[watch.type] || watch.type })]), ...details.map(text => el("p", { text })), el("span", { class: `enabled-label${watch.enabled ? "" : " off"}`, text: watch.enabled ? "正在关注" : "已停用" })]), el("div", { class: "row-actions" }, [button("", () => watchForm(watch), "icon-button", "edit", { "aria-label": `编辑关注 ${watch.name}` }), button("", () => confirmDelete("删除关注", `删除「${watch.name}」及其匹配关系，已收取的资讯会保留。`, () => api(`/watches/${watch.id}`, { method: "DELETE" })), "icon-button", "trash", { "aria-label": `删除关注 ${watch.name}` })])]);
}
function watchForm(watch = {}) {
  const form = el("form", { class: "modal-form" });
  form.append(el("p", { class: "field-caption", text: "名称、别名或证券代码命中任意一个即可；设置上下文后，还需同时命中全部上下文关键词。任意排除词命中都会排除该资讯。" }), el("div", { class: "form-row" }, [formField("对象类型", select("type", WATCH_TYPES, watch.type || "company")), formField("名称", input("name", watch.name || "", { required: true, maxlength: 120, placeholder: "如：任天堂" }))]), formField("别名", el("textarea", { name: "aliases", rows: 2, maxlength: 2000, placeholder: "如：Nintendo，任天堂株式会社", text: (watch.aliases || []).join("\n") }), "用逗号、分号或换行分隔；含逗号的名称请使用简短别名。"), formField("上下文关键词（可选）", input("keywords", (watch.keywords || []).join("，"), { maxlength: 2000, placeholder: "如：游戏，主机" }), "用于避免同名误匹配。填写多个词时，资讯需同时包含全部关键词；用逗号、分号或换行分隔。"), formField("排除关键词（可选）", input("exclude_keywords", (watch.exclude_keywords || []).join("，"), { maxlength: 2000, placeholder: "命中任意排除词的资讯将被排除" })), el("div", { class: "form-row" }, [formField("市场（可选）", input("market", watch.market || "", { maxlength: 30, placeholder: "如：NASDAQ、港股" })), formField("证券代码（可选）", input("ticker", watch.ticker || "", { maxlength: 30, placeholder: "如：AAPL、0700" }))]), check("enabled", "启用此关注对象", watch.enabled !== false), formFooter(watch.id ? "保存修改" : "添加关注"));
  form.addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(form); const submit = $("[type=submit]", form); submit.disabled = true;
    const body = { type: data.get("type"), name: data.get("name").trim(), aliases: listValues(data.get("aliases")), keywords: listValues(data.get("keywords")), exclude_keywords: listValues(data.get("exclude_keywords")), market: data.get("market").trim(), ticker: data.get("ticker").trim(), enabled: data.has("enabled") };
    if (!body.name) { modalError(form, new Error("请输入关注对象名称。")); submit.disabled = false; return; }
    try { await api(watch.id ? `/watches/${watch.id}` : "/watches", { method: watch.id ? "PUT" : "POST", body }); closeModal(); toast(watch.id ? "关注已更新，已有资讯已重新匹配" : "已添加关注，并匹配已有资讯"); await refreshData(); renderView(); }
    catch (error) { if (error.status !== 401) modalError(form, error); } finally { submit.disabled = false; }
  }); openModal(watch.id ? "编辑关注" : "添加关注", form);
}

async function renderSchedules(id) {
  main.replaceChildren(heading("收取计划", `按你的时间收取资讯 · ${state.settings.timezone}`, [button("添加计划", () => scheduleForm(), "button button-primary", "plus")]), el("p", { class: "fetch-note", text: "每个计划对应一个时间点。可设置不同的星期和栏目，保存后立即生效。" }), loading("正在载入收取计划…"));
  try {
    const data = await api("/schedules"); if (id !== state.renderId) return; state.schedules = data.items || [];
    main.lastElementChild.replaceWith(state.schedules.length ? el("div", { class: "manage-list" }, state.schedules.map(scheduleRow)) : empty("让资讯按时到来", "还没有收取计划。选择适合自己的时间与栏目，服务器会在指定时间收取公开来源。", [button("添加计划", () => scheduleForm(), "button button-primary", "plus")], "calendar"));
  } catch (error) { if (id === state.renderId && error.status !== 401) main.lastElementChild.replaceWith(errorPanel(error, renderView)); }
}
function scheduleRow(schedule) {
  const days = schedule.days || []; const label = days.length === 7 ? "每天" : days.map(day => WEEKDAYS[day]).join("、");
  return el("article", { class: "manage-row" }, [el("div", { class: "manage-copy" }, [el("div", { class: `schedule-time${schedule.enabled ? "" : " disabled-text"}` }, [schedule.time, el("small", { text: label })]), el("h3", { text: schedule.name }), el("p", { text: (schedule.categories || []).map(category => CATEGORIES[category]).filter(Boolean).join(" / ") }), el("p", { text: schedule.enabled ? `下次收取：${schedule.next_run_at ? dateTime(schedule.next_run_at) : "等待计算"}` : "此计划已暂停" }), schedule.last_run_at && el("p", { text: `上次执行：${dateTime(schedule.last_run_at)}` }), el("span", { class: `enabled-label${schedule.enabled ? "" : " off"}`, text: schedule.enabled ? "计划已启用" : "已暂停" })]), el("div", { class: "row-actions" }, [button("", () => scheduleForm(schedule), "icon-button", "edit", { "aria-label": `编辑计划 ${schedule.name}` }), button("", () => confirmDelete("删除收取计划", `删除「${schedule.name}」后，服务器将不再按此计划收取资讯。`, () => api(`/schedules/${schedule.id}`, { method: "DELETE" })), "icon-button", "trash", { "aria-label": `删除计划 ${schedule.name}` })])]);
}
function scheduleForm(schedule = {}) {
  const form = el("form", { class: "modal-form" }, [formField("计划名称", input("name", schedule.name || "", { required: true, maxlength: 80, placeholder: "如：早间资讯" })), el("div", { class: "form-row" }, [formField("收取时间", input("time", schedule.time || "08:30", { type: "time", required: true, step: 60 })), el("div", {}, [el("p", { class: "field-caption", text: "使用时区" }), el("p", { class: "muted", text: state.settings.timezone, style: "padding-top:12px;font-size:13px" })])]), el("fieldset", {}, [el("legend", { text: "重复星期" }), choices("days", Object.fromEntries(WEEKDAYS.map((name, index) => [index, name])), schedule.days || [0, 1, 2, 3, 4, 5, 6])]), el("fieldset", {}, [el("legend", { text: "收取栏目" }), choices("categories", CATEGORIES, schedule.categories || Object.keys(CATEGORIES))]), check("enabled", "启用此计划", schedule.enabled !== false), formFooter(schedule.id ? "保存修改" : "添加计划")]);
  form.addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(form); const submit = $("[type=submit]", form);
    const body = { name: data.get("name").trim(), time: data.get("time"), days: data.getAll("days").map(Number), categories: data.getAll("categories"), enabled: data.has("enabled") };
    if (!body.name || !body.days.length || !body.categories.length) { modalError(form, new Error("请填写名称，并至少选择一个星期和一个栏目。")); return; }
    submit.disabled = true;
    try { await api(schedule.id ? `/schedules/${schedule.id}` : "/schedules", { method: schedule.id ? "PUT" : "POST", body }); closeModal(); toast("收取计划已保存"); await refreshData(); renderView(); }
    catch (error) { if (error.status !== 401) modalError(form, error); } finally { submit.disabled = false; }
  }); openModal(schedule.id ? "编辑收取计划" : "添加收取计划", form);
}

function section(title, copy, content, action) {
  return el("section", { class: "settings-section" }, [el("div", { class: "section-heading" }, [el("div", {}, [el("h2", { text: title }), copy && el("p", { text: copy })]), action]), content]);
}
async function renderSettings(id) {
  main.replaceChildren(heading("设置与来源", "管理资讯来源、收取偏好与阅读室。"), loading("正在载入设置…"));
  try {
    const [settings, sources, runs] = await Promise.all([api("/settings"), api("/sources"), api("/runs?limit=20")]); if (id !== state.renderId) return;
    state.settings = settings; state.sources = sources.items || [];
    main.replaceChildren(heading("设置与来源", "管理资讯来源、收取偏好与阅读室。"), section("收取状态", "手动收取与定时计划使用相同的资讯来源。", el("div", { id: "settings-status" }, statusBox())), section("资讯来源", "仅支持公开 RSS 与 Steam 官方资讯。", state.sources.length ? el("div", { class: "manage-list sources-list" }, state.sources.map(sourceRow)) : el("p", { class: "inline-empty", text: "还没有资讯来源。添加 RSS 地址或 Steam 游戏编号后，即可收取。" }), button("添加来源", () => sourceForm(), "button button-small", "plus")), section("收取偏好", "时区影响所有计划，补收会合并遗漏的任务。", settingsForm()), section("最近收取记录", "查看每次收取的结果与来源错误。", el("div", { id: "run-history" }, runTable(runs.items || [])), button("刷新", refreshRuns, "button button-small", "refresh")), section("账号与配置", null, el("div", { class: "account-row" }, [el("div", {}, [el("strong", { text: state.session.username || "管理员" }), el("p", { text: "导出文件包含关注、来源、计划与偏好设置。" })]), el("div", { class: "account-actions" }, [button("导出配置", exportConfig, "button", "download"), button("退出登录", logout, "button", "logout")])])));
    renderSidebar();
  } catch (error) { if (id === state.renderId && error.status !== 401) main.lastElementChild.replaceWith(errorPanel(error, renderView)); }
}
function statusBox() {
  const status = state.status;
  const box = el("div", { class: "status-box" }, [statusLine("refresh", "当前状态", status.fetching ? "正在收取资讯…" : "等待下次收取"), statusLine("clock", "最近成功", status.last_success_at ? dateTime(status.last_success_at) : "尚未成功收取"), statusLine("calendar", "下次计划", status.next_run_at ? dateTime(status.next_run_at) : "未启用收取计划")]);
  if (status.last_error) box.append(el("p", { class: "source-error field-help", text: status.last_error }));
  box.append(button(status.fetching ? "正在收取…" : "立即收取全部栏目", () => requestFetch(), "button", "refresh", { disabled: Boolean(status.fetching), "data-fetch-button": true })); return box;
}
function sourceRow(source) {
  return el("article", { class: "manage-row" }, [el("div", { class: "manage-copy" }, [el("h3", { class: source.enabled ? "" : "disabled-text" }, [source.name, el("span", { class: "manage-type", text: `${CATEGORIES[source.category] || "资讯"} · ${source.kind === "steam" ? "Steam" : "RSS"}` })]), el("p", { class: "source-url", text: source.kind === "steam" ? `Steam App ID：${source.steam_appid || "未设置"}` : source.url }), el("p", { text: source.last_success_at ? `最近成功：${dateTime(source.last_success_at)}` : "尚未收取" }), source.last_error && el("p", { class: "source-error", text: source.last_error }), !source.enabled && el("span", { class: "enabled-label off", text: "已停用" })]), el("div", { class: "row-actions" }, [button("", () => sourceForm(source), "icon-button", "edit", { "aria-label": `编辑来源 ${source.name}` }), button("", () => confirmDelete("删除资讯来源", `删除「${source.name}」后将不再从该来源收取资讯，已收取的资讯会保留。`, () => api(`/sources/${source.id}`, { method: "DELETE" })), "icon-button", "trash", { "aria-label": `删除来源 ${source.name}` })])]);
}
function sourceForm(source = {}) {
  const kind = select("kind", { rss: "公开 RSS / Atom", steam: "Steam 官方游戏资讯" }, source.kind || "rss");
  const url = input("url", source.url || "", { type: "url", maxlength: 2000, placeholder: "https://example.com/feed.xml" });
  const appid = input("steam_appid", source.steam_appid || "", { type: "number", min: 1, max: 2147483647, step: 1, placeholder: "如：570" });
  const urlField = formField("订阅地址", url, "填写可公开访问的 RSS 或 Atom 地址。");
  const steamField = formField("Steam 游戏 App ID", appid, "可在商店地址 store.steampowered.com/app/数字 中找到。");
  const form = el("form", { class: "modal-form" }, [formField("来源名称", input("name", source.name || "", { required: true, maxlength: 120, placeholder: "为这个来源起个名称" })), el("div", { class: "form-row" }, [formField("来源类型", kind), formField("所属栏目", select("category", CATEGORIES, source.category || "games"))]), urlField, steamField, check("enabled", "启用此来源", source.enabled !== false), formFooter(source.id ? "保存修改" : "添加来源")]);
  function toggle() { const steam = kind.value === "steam"; urlField.hidden = steam; url.disabled = steam; url.required = !steam; steamField.hidden = !steam; appid.disabled = !steam; appid.required = steam; }
  kind.addEventListener("change", toggle); toggle();
  form.addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(form); const submit = $("[type=submit]", form); const steam = data.get("kind") === "steam";
    const body = { name: data.get("name").trim(), kind: data.get("kind"), category: data.get("category"), enabled: data.has("enabled"), url: steam ? `https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid=${Number(data.get("steam_appid"))}&count=20&maxlength=0` : data.get("url").trim() };
    if (steam) body.steam_appid = Number(data.get("steam_appid"));
    if (!body.name || !safeUrl(body.url)) { modalError(form, new Error("请填写来源名称和有效的公开 HTTP / HTTPS 地址。")); return; }
    submit.disabled = true;
    try { await api(source.id ? `/sources/${source.id}` : "/sources", { method: source.id ? "PUT" : "POST", body }); closeModal(); toast("资讯来源已保存，可以立即收取"); await refreshData(); renderView(); }
    catch (error) { if (error.status !== 401) modalError(form, error); } finally { submit.disabled = false; }
  }); openModal(source.id ? "编辑资讯来源" : "添加资讯来源", form);
}
function settingsForm() {
  const timezone = input("timezone", state.settings.timezone, { required: true, list: "timezones", maxlength: 80, placeholder: "Asia/Shanghai" });
  const datalist = el("datalist", { id: "timezones" }, ["Asia/Shanghai", "Asia/Hong_Kong", "Asia/Tokyo", "Asia/Singapore", "Europe/London", "America/New_York", "America/Los_Angeles", "UTC"].map(value => el("option", { value })));
  const catchup = check("catch_up", "服务恢复后补收遗漏的计划", state.settings.catch_up);
  const hours = input("catch_up_hours", state.settings.catch_up_hours, { type: "number", min: 1, max: 72, step: 1, required: true });
  const form = el("form", { class: "settings-form" }, [formField("计划时区", timezone, "填写 IANA 时区名称，如 Asia/Shanghai。"), datalist, catchup, formField("补收时间范围（小时）", hours, "只补收此范围内的遗漏任务，多个遗漏时段合并为一次。"), el("button", { type: "submit", class: "button button-primary", text: "保存偏好" })]);
  $("input", catchup).addEventListener("change", event => { hours.disabled = !event.target.checked; }); hours.disabled = !state.settings.catch_up;
  form.addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(form); const submit = $("[type=submit]", form); const zone = data.get("timezone").trim();
    try { new Intl.DateTimeFormat("zh-CN", { timeZone: zone }); } catch { toast("请输入有效的时区名称，如 Asia/Shanghai。"); return; }
    submit.disabled = true;
    try { state.settings = await api("/settings", { method: "PUT", body: { timezone: zone, catch_up: data.has("catch_up"), catch_up_hours: Number(data.get("catch_up_hours") || state.settings.catch_up_hours) } }); toast("收取偏好已保存"); await refreshData(); renderSidebar(); }
    catch (error) { showError(error); } finally { submit.disabled = false; }
  }); return form;
}
function runTable(runs) {
  if (!runs.length) return el("p", { class: "inline-empty", text: "尚无收取记录。添加来源后手动收取，或等待已启用的计划。" });
  const statusNames = { running: "进行中", success: "成功", completed: "成功", failed: "失败", partial: "部分成功", error: "失败" };
  const triggers = { manual: "手动收取", schedule: "定时计划", scheduled: "定时计划", catch_up: "遗漏补收", catchup: "遗漏补收" };
  return el("table", { class: "run-table" }, [el("caption", { class: "skip-link", text: "最近收取记录" }), el("thead", {}, el("tr", {}, [el("th", { scope: "col", text: "时间 / 触发" }), el("th", { scope: "col", text: "结果" }), el("th", { scope: "col", text: "新增" })])), el("tbody", {}, runs.map(run => el("tr", {}, [el("td", {}, [el("time", { datetime: run.started_at, text: dateTime(run.started_at) }), el("div", { class: "muted", text: triggers[run.trigger] || run.trigger || "收取" })]), el("td", {}, [el("span", { class: `run-status ${["success", "completed"].includes(run.status) ? "success" : ["failed", "error"].includes(run.status) ? "failed" : run.status === "partial" ? "partial" : ""}`, text: statusNames[run.status] || run.status }), run.error && el("div", { class: "run-error", text: run.error }), run.source_count !== undefined && el("div", { class: "muted", text: `${run.source_count} 个来源` })]), el("td", { text: run.new_count == null ? "—" : `${run.new_count} 条` })])))]);
}
async function refreshRuns() { const target = $("#run-history"); if (!target) return; try { const data = await api("/runs?limit=20"); if (target.isConnected) target.replaceChildren(runTable(data.items || [])); } catch (error) { showError(error); } }
async function exportConfig() {
  try { const data = await api("/export"); const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" })); const link = el("a", { href: url, download: `xunlan-config-${new Date().toISOString().slice(0, 10)}.json` }); document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); toast("配置已导出"); } catch (error) { showError(error); }
}
async function logout() { try { await api("/logout", { method: "POST", body: {} }); state.session = await api("/session"); state.renderId += 1; closeModal(); $("#auth-password").value = ""; showAuth(); toast("已退出登录"); } catch (error) { showError(error); } }

function statusLine(iconName, name, value) { return el("p", { class: "status-line" }, [icon(iconName), el("span", {}, [`${name} · `, el("strong", { text: value })])]); }
function renderSidebar() {
  const status = state.status;
  const fetch = button(status.fetching ? "正在收取…" : "立即收取", () => requestFetch(), "button", "refresh", { disabled: Boolean(status.fetching), "data-fetch-button": true });
  const runtime = el("section", { class: "aside-section" }, [el("h2", { class: "aside-title", text: "阅读室" }), statusLine("refresh", "收取状态", status.fetching ? "收取中" : "已就绪"), statusLine("clock", "最近更新", status.last_success_at ? dateTime(status.last_success_at) : "尚未收取"), statusLine("calendar", "下次收取", status.next_run_at ? dateTime(status.next_run_at) : "未设置计划"), fetch]);
  const watches = el("section", { class: "aside-section" }, [el("h2", { class: "aside-title", text: "关注线索" }), state.watches.length ? el("div", {}, state.watches.filter(w => w.enabled).slice(0, 5).map(w => el("div", { class: "aside-watch" }, [el("span", { text: w.name }), el("small", { text: WATCH_TYPES[w.type] || "" })]))) : el("p", { class: "aside-description", text: "添加你关心的公司、工作室、球队、国家或政客。相关资讯会汇集到「关注」。" }), button(state.watches.length ? "查看关注" : "添加关注", () => { if (!state.watches.length) watchForm(); else if (state.view === "following") showWatchManager(); else location.hash = "following"; }, "button button-small", state.watches.length ? "arrow" : "plus")]);
  const note = el("section", { class: "aside-section aside-note" }, ["为关注的事，", el("br"), "留一段阅读时间。", el("small", { text: `公开来源 · 原文可追溯\u2002\u2002${state.settings.timezone}` })]);
  $("#sidebar").replaceChildren(runtime, watches, note);
}
function updateFetchButtons() {
  const busy = Boolean(state.status.fetching || state.fetching);
  $("#header-fetch").disabled = busy; $("#header-fetch").setAttribute("aria-label", busy ? "正在收取资讯" : "收取资讯");
  const span = $("#header-fetch span:last-child"); if (span) span.textContent = busy ? "正在收取…" : "收取资讯";
  document.querySelectorAll("[data-fetch-button]").forEach(node => { node.disabled = busy; });
}
function requestFetch() {
  if (state.status.fetching || state.fetching) { toast("资讯正在收取，请稍候。"); return; }
  const form = el("form", { class: "modal-form" }, [el("p", { class: "field-caption", text: "从已启用的来源收取最新资讯。收取将在后台进行，你可以继续阅读。" }), el("fieldset", {}, [el("legend", { text: "选择收取栏目" }), choices("categories", CATEGORIES, state.category ? [state.category] : Object.keys(CATEGORIES))]), formFooter("开始收取")]);
  form.addEventListener("submit", async event => {
    event.preventDefault(); const categories = new FormData(form).getAll("categories"); if (!categories.length) { modalError(form, new Error("请至少选择一个栏目。")); return; }
    const submit = $("[type=submit]", form); submit.disabled = true; state.fetching = true; updateFetchButtons();
    try { const result = await api("/fetch", { method: "POST", body: { categories } }); closeModal(); toast(result.message || "已开始收取，完成后资讯会自动更新"); state.status.fetching = true; renderSidebar(); await pollStatus(); }
    catch (error) { if (error.status !== 401) modalError(form, error); } finally { state.fetching = false; updateFetchButtons(); submit.disabled = false; }
  }); openModal("收取资讯", form);
}
async function pollStatus() {
  if (!state.session?.authenticated || state.pollBusy || document.hidden) return;
  state.pollBusy = true;
  try {
    const wasFetching = Boolean(state.status.fetching); state.status = await api("/status"); renderSidebar(); updateFetchButtons();
    const box = $("#settings-status"); if (box) box.replaceChildren(statusBox());
    if (wasFetching && !state.status.fetching) {
      toast(state.status.last_error ? "收取已结束，部分来源有错误，请查看记录" : "收取已完成");
      if (["news", "following"].includes(state.view) && !state.manageWatches) renderView();
      if (state.view === "settings") renderView();
    } else if (state.view === "settings" && state.status.fetching) refreshRuns();
  } catch (error) { if (error.status === 401) showAuth(); } finally { state.pollBusy = false; }
}

document.querySelectorAll("[data-icon]").forEach(node => node.replaceChildren(icon(node.dataset.icon)));
$("#modal-close").addEventListener("click", closeModal);
modal.addEventListener("click", event => { if (event.target === modal) { const rect = modal.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeModal(); } });
$("#header-fetch").addEventListener("click", requestFetch);
window.addEventListener("hashchange", () => { if (state.session?.authenticated) route(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollStatus(); });
$("#auth-form").addEventListener("submit", async event => {
  event.preventDefault(); const submit = $("#auth-submit"); const errorNode = $("#auth-error"); errorNode.hidden = true; submit.disabled = true;
  const body = { username: $("#auth-username").value.trim(), password: $("#auth-password").value };
  if (state.session?.setup_required && state.session.setup_token_required) body.setup_token = $("#auth-setup-token").value;
  try { state.session = await api(state.session?.setup_required ? "/setup" : "/login", { method: "POST", body }); $("#auth-password").value = ""; $("#auth-setup-token").value = ""; await enterApp(); }
  catch (error) { errorNode.textContent = error.message; errorNode.hidden = false; }
  finally { submit.disabled = false; }
});
$("#masthead-date").textContent = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "long" }).format(new Date());
(async () => {
  try { state.session = await api("/session"); if (state.session.authenticated) await enterApp(); else showAuth(); }
  catch (error) { $("#boot").replaceChildren(el("span", { class: "brand", text: "讯览·" }), el("p", { text: error.message }), button("重新连接", () => location.reload(), "button", "refresh")); }
})();
setInterval(pollStatus, 12000);
