"use strict";

const CATEGORIES = { games: "游戏", sports: "体育", stocks: "股票", politics: "政治" };
const WATCH_TYPES = { company: "公司", studio: "游戏工作室", league: "联赛", team: "球队", country: "国家", politician: "政客" };
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
const state = { session: null, view: "news", category: "", q: "", saved: false, unread: false, watchId: "", companyId: "", leagueId: "", teamId: "", sportsWatchId: "", page: 1, articles: [], total: 0, pageSize: 20, watches: [], catalog: { companies: [], leagues: [], teams: [] }, schedules: [], settings: { timezone: "Asia/Shanghai" }, status: {}, manageWatches: false, renderId: 0, modalId: 0, toastTimer: null, fetching: false, pollBusy: false };
const $ = (selector, root = document) => root.querySelector(selector);
const main = $("#main-content");
const modal = $("#modal");
const deviceNews = { busy: false, lastAttempt: 0, controller: null, pending: null };
const listTranslation = { controller: null, retryAfter: 0 };

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
class ApiError extends Error { constructor(message, status, code = "") { super(message); this.status = status; this.code = code; } }
async function api(path, options = {}, retry = true) {
  const method = options.method || "GET";
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && state.session?.csrf_token) headers["X-CSRF-Token"] = state.session.csrf_token;
  let response;
  try { response = await fetch(`/api${path}`, { method, headers, credentials: "same-origin", signal: options.signal, body: options.body === undefined ? undefined : JSON.stringify(options.body) }); }
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
    throw new ApiError(typeof detail === "string" ? detail : `请求失败（${response.status}），请稍后重试。`, response.status, data.code || "");
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
  deviceNews.controller?.abort();
  listTranslation.controller?.abort();
  deviceNews.lastAttempt = 0;
  $("#boot").hidden = true; $("#app").hidden = true; $("#auth").hidden = false;
  const setup = state.session?.setup_required;
  $("#auth-title").textContent = setup ? "建立你的阅读室" : "欢迎回来";
  $("#auth-description").textContent = setup ? "创建你的账号，再选择感兴趣的公司、联赛与球队。" : "登录，继续阅读你关注的资讯。";
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
  const results = await Promise.allSettled([api("/preferences"), api("/watches"), api("/status"), api("/catalog")]);
  if (!state.session?.authenticated) return;
  if (results[0].status === "fulfilled") state.settings = results[0].value;
  if (results[1].status === "fulfilled") state.watches = results[1].value.items || [];
  if (results[2].status === "fulfilled") state.status = results[2].value;
  if (results[3].status === "fulfilled") state.catalog = results[3].value;
  normalizeFocus();
  renderSidebar(); updateFetchButtons();
}
async function enterApp() {
  $("#boot").hidden = true; $("#auth").hidden = true; $("#app").hidden = false;
  await refreshData(); if (!state.session?.authenticated) return;
  restoreFocus();
  route();
  collectDeviceNews();
}

async function collectDeviceNews(categories = Object.keys(CATEGORIES)) {
  if (deviceNews.busy) return deviceNews.pending;
  if (!state.session?.authenticated) return [];
  const session = state.session;
  const controller = new AbortController();
  deviceNews.controller = controller; deviceNews.busy = true; deviceNews.lastAttempt = Date.now();
  deviceNews.pending = collectDeviceFeeds(categories, session, controller);
  try { return await deviceNews.pending; }
  finally { deviceNews.busy = false; deviceNews.controller = null; deviceNews.pending = null; }
}
async function collectDeviceFeeds(categories, session, controller) {
  try {
    const data = await api("/browser/sources", { signal: controller.signal });
    if (controller.signal.aborted) return [];
    const sources = data.items.filter(source => categories.includes(source.category));
    const result = await BrowserNews.collect({
      sources, signal: controller.signal,
      importFeed: body => {
        if (controller.signal.aborted || state.session !== session) throw new Error("当前收取已取消");
        return api("/browser/import", { method: "POST", body, signal: controller.signal });
      },
    });
    if (controller.signal.aborted) return [];
    if (result.failed.length) {
      try { await api("/browser/report", { method: "POST", body: { failures: result.failed.map(({ source_id, source_url, reason }) => ({ source_id, source_url, reason: reason.slice(0, 800) })) }, signal: controller.signal }); }
      catch (error) { if (error.status === 401 || controller.signal.aborted) return []; }
    }
    await refreshData();
    if (result.newCount && !document.hidden && ["news", "following"].includes(state.view) && !state.manageWatches && !modal.open) await renderReader(++state.renderId, true);
    return result.sourceIds;
  } catch {
    controller.abort();
    return [];
  }
}
function route() {
  const next = location.hash.slice(1); state.view = ["news", "following", "schedules", "settings"].includes(next) ? next : "news";
  state.page = 1; state.watchId = ""; state.q = ""; state.saved = false; state.unread = false; state.manageWatches = false;
  if (state.view === "news") restoreFocus(); else state.category = "";
  closeModal(); renderView();
  document.querySelectorAll("[data-view]").forEach(link => { if (link.dataset.view === state.view) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current"); });
  document.title = `${{ news: "新闻", following: "我的关注", schedules: "收取计划", settings: "设置" }[state.view]} · 讯览`;
}
async function renderView() {
  if (!state.session?.authenticated) return;
  listTranslation.controller?.abort();
  const id = ++state.renderId;
  if (state.view === "news" || state.view === "following") { if (state.manageWatches) return renderWatches(id); return renderReader(id); }
  if (state.view === "schedules") return renderSchedules(id);
  return renderSettings(id);
}
function setFilter(key, value) {
  if (key === "category" && value !== state.category) { state.companyId = ""; state.leagueId = ""; state.teamId = ""; state.sportsWatchId = ""; }
  state[key] = value; state.page = 1; if (state.view === "news") persistFocus(); renderView();
}
function focusStorageKey() { return `xunlan.focus.${encodeURIComponent(state.session?.username || "")}`; }
function normalizeFocus() {
  if (!state.watches.some(w => w.type === "company" && w.enabled && String(w.id) === state.companyId)) state.companyId = "";
  if (!state.catalog.leagues.some(l => l.id === state.leagueId)) state.leagueId = "";
  if (!state.catalog.teams.some(t => t.id === state.teamId && (!state.leagueId || t.league_id === state.leagueId))) state.teamId = "";
  const sportsWatch = state.watches.find(w => ["team", "league"].includes(w.type) && w.enabled && String(w.id) === state.sportsWatchId);
  if (!sportsWatch) state.sportsWatchId = "";
  else if (sportsWatch.type === "league" && sportsWatch.league_id) { state.leagueId = sportsWatch.league_id; state.sportsWatchId = ""; }
  else if (sportsWatch.type === "league") { state.leagueId = ""; state.teamId = ""; }
  else { state.leagueId = sportsWatch.league_id || ""; state.teamId = ""; }
  if (state.teamId) state.leagueId = state.catalog.teams.find(t => t.id === state.teamId)?.league_id || "";
  if (state.category !== "sports") { state.leagueId = ""; state.teamId = ""; state.sportsWatchId = ""; } else state.companyId = "";
}
function persistFocus() {
  try { localStorage.setItem(focusStorageKey(), JSON.stringify({ category: state.category, company_id: state.companyId, league_id: state.leagueId, team_id: state.teamId, sports_watch_id: state.sportsWatchId })); } catch { /* Storage may be unavailable in a private browser. */ }
}
function restoreFocus() {
  try {
    const stored = JSON.parse(localStorage.getItem(focusStorageKey()) || "{}");
    state.category = Object.hasOwn(CATEGORIES, stored.category) ? stored.category : "";
    state.companyId = String(stored.company_id || ""); state.leagueId = String(stored.league_id || ""); state.teamId = String(stored.team_id || ""); state.sportsWatchId = String(stored.sports_watch_id || "");
  } catch { state.category = ""; state.companyId = ""; state.leagueId = ""; state.teamId = ""; state.sportsWatchId = ""; }
  normalizeFocus(); persistFocus();
}
function clearFilters() { state.q = ""; state.saved = false; state.unread = false; state.watchId = ""; state.category = ""; state.companyId = ""; state.leagueId = ""; state.teamId = ""; state.sportsWatchId = ""; state.page = 1; if (state.view === "news") persistFocus(); renderView(); }
function setFocus(values) { Object.assign(state, values); state.page = 1; normalizeFocus(); persistFocus(); renderView(); }
function matchingWatch(preset, type) {
  const names = new Set([preset.name, ...(preset.aliases || [])].map(name => String(name).toLocaleLowerCase()));
  return state.watches.find(w => w.enabled && w.type === type && [w.name, ...(w.aliases || [])].some(name => names.has(String(name).toLocaleLowerCase())));
}
function focusBar() {
  if (state.category === "sports") return sportsFocusBar();
  const companies = state.watches.filter(w => w.enabled && w.type === "company");
  const chooser = select("company_focus", { "": "全部公司", ...Object.fromEntries(companies.map(w => [w.id, w.name])) }, state.companyId, { onchange: event => setFocus({ companyId: event.target.value }), "aria-label": "公司焦点" });
  return el("section", { class: "focus-bar", "aria-label": "公司焦点" }, [formField("公司焦点", chooser), button("添加公司", companyPicker, "button button-small", "plus"), el("p", { class: "focus-hint", text: companies.length ? "切换你关注的公司，集中阅读相关报道。" : "从常用公司中选择，也可以添加自己的关注。" })]);
}
function companyPicker() {
  const box = el("div", { class: "preset-picker" }, [el("p", { class: "field-caption", text: "选择公司添加到关注，中文与英文别名会一并填入。" })]);
  box.append(el("div", { class: "preset-list" }, state.catalog.companies.map(company => {
    const existing = matchingWatch(company, "company");
    return button(company.name, () => {
      if (existing) { closeModal(); setFocus({ companyId: String(existing.id) }); }
      else watchForm({ ...company, id: undefined, type: "company", enabled: true }, saved => { setFocus({ companyId: String(saved.id) }); });
    }, "preset-option", null, { "aria-label": existing ? `选择已关注公司 ${company.name}` : `添加公司 ${company.name}` });
  })));
  box.append(button("自定义公司", () => watchForm({ type: "company" }, saved => { setFocus({ companyId: String(saved.id) }); }), "button", "plus"));
  openModal("选择关注公司", box);
}
function sportsFocusBar() {
  const customLeague = state.watches.find(w => String(w.id) === state.sportsWatchId && w.type === "league");
  const leagueOptions = { "": "全部联赛", ...Object.fromEntries(state.catalog.leagues.map(l => [`catalog:${l.id}`, l.name])), ...Object.fromEntries(state.watches.filter(w => w.enabled && w.type === "league" && !w.league_id).map(w => [`watch:${w.id}`, `${w.name}（自定义）`])) };
  const selectedLeague = customLeague ? `watch:${customLeague.id}` : state.leagueId ? `catalog:${state.leagueId}` : "";
  const leagueSelect = select("league_focus", leagueOptions, selectedLeague, { "aria-label": "联赛", onchange: event => {
    const value = event.target.value;
    setFocus({ leagueId: value.startsWith("catalog:") ? value.slice(8) : "", teamId: "", sportsWatchId: value.startsWith("watch:") ? value.slice(6) : "" });
  } });
  const teams = state.catalog.teams.filter(t => !state.leagueId || t.league_id === state.leagueId);
  const customTeams = state.watches.filter(w => w.enabled && w.type === "team" && (!state.leagueId || w.league_id === state.leagueId));
  const teamOptions = { "": "全部球队", ...Object.fromEntries(teams.map(t => [`catalog:${t.id}`, t.name])), ...Object.fromEntries(customTeams.map(w => [`watch:${w.id}`, `${w.name}（关注）`])) };
  const customTeam = customTeams.find(w => String(w.id) === state.sportsWatchId);
  const selectedTeam = customTeam ? `watch:${customTeam.id}` : state.teamId ? `catalog:${state.teamId}` : "";
  const teamSelect = select("team_focus", teamOptions, selectedTeam, { "aria-label": "常用球队", disabled: Boolean(customLeague), onchange: event => {
    const value = event.target.value;
    const team = state.catalog.teams.find(t => t.id === value.slice(8));
    setFocus({ teamId: value.startsWith("catalog:") ? value.slice(8) : "", leagueId: value.startsWith("catalog:") && team ? team.league_id : state.leagueId, sportsWatchId: value.startsWith("watch:") ? value.slice(6) : "" });
  } });
  const current = state.teamId ? state.catalog.teams.find(t => t.id === state.teamId) : state.leagueId ? state.catalog.leagues.find(l => l.id === state.leagueId) : null;
  const type = state.teamId ? "team" : "league";
  const already = current && matchingWatch(current, type);
  const addCurrent = current ? button(already ? "已关注" : `关注${current.name}`, () => watchForm({ ...current, id: undefined, type, league_id: type === "team" ? current.league_id : current.id, enabled: true }), "button button-small", "bookmark", { disabled: Boolean(already) }) : null;
  return el("section", { class: "focus-bar sports-focus", "aria-label": "体育焦点" }, [el("div", { class: "focus-fields" }, [formField("联赛", leagueSelect), formField("常用球队", teamSelect)]), el("div", { class: "focus-actions" }, [addCurrent, button("添加球队", () => watchForm({ type: "team", league_id: state.leagueId, enabled: true }, saved => setFocus({ teamId: "", sportsWatchId: String(saved.id) })), "button button-small", "plus")]), el("p", { class: "focus-hint", text: customLeague ? "当前按已关注联赛匹配；也可切换到上方预设联赛浏览常用球队。" : "常用球队仅供快捷选择；更多球队可以自行添加。" })]);
}
function readerEmpty() {
  if (state.category === "sports" && !state.q && !state.saved && !state.unread) return empty("还没有符合条件的体育资讯", "尚未收取到相关报道。可以收取新资讯，或清除联赛与球队筛选后查看全部报道。", [button("收取资讯", () => requestFetch(), "button button-primary", "refresh"), button("清除筛选", clearFilters, "button button-small")]);
  if (state.q || state.saved || state.unread || state.watchId || state.category || (state.view === "news" && (state.companyId || state.leagueId || state.teamId || state.sportsWatchId))) return empty("还没有符合条件的资讯", "试试其他栏目、公司或球队，也可以清除筛选查看已收取的全部资讯。", [button("清除筛选", clearFilters, "button button-small")]);
  if (state.view === "following") return empty(state.watches.length ? "等待你的关注资讯" : "从一个关注对象开始", state.watches.length ? "目前还没有资讯命中关注对象。收取新资讯，或编辑别名与上下文关键词来调整匹配。" : "添加公司、工作室、球队、国家或政客。讯览会从真实资讯中找出相关报道。", [button(state.watches.length ? "管理关注" : "添加关注", () => state.watches.length ? showWatchManager() : watchForm(), "button button-primary", "plus"), state.watches.length && button("收取资讯", () => requestFetch(), "button", "refresh")].filter(Boolean), "bookmark");
  return empty("阅读室已准备好", "收取第一批报道，或先添加感兴趣的公司、联赛与球队，再设置每天的收取时间。", [button("收取资讯", () => requestFetch(), "button button-primary", "refresh"), button("添加关注", () => watchForm(), "button", "plus")]);
}
async function renderReader(id, quiet = false) {
  listTranslation.controller?.abort();
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
  const list = quiet ? $("#reader-list") : el("div", { id: "reader-list", "aria-live": "polite" }, loading("正在载入资讯…"));
  if (!list) return;
  if (!quiet) main.replaceChildren(head, following && !state.watches.length ? el("div", { class: "following-intro" }, [el("p", {}, [el("strong", { text: "还没有关注对象" }), " · 添加关注后，相关资讯会出现在这里。"]), button("添加", () => watchForm(), "button button-small", "plus")]) : "", categories, !following && focusBar(), tools, list);
  try {
    const params = new URLSearchParams({ page: String(state.page), page_size: String(state.pageSize), following: String(following), saved: String(state.saved), unread: String(state.unread) });
    if (state.category) params.set("category", state.category); if (state.q) params.set("q", state.q); if (state.watchId) params.set("watch_id", state.watchId);
    if (!following) {
      if (state.category === "sports") {
        if (state.sportsWatchId) params.set("watch_id", state.sportsWatchId);
        else { if (state.leagueId) params.set("league_id", state.leagueId); if (state.teamId) params.set("team_id", state.teamId); }
      } else if (state.companyId) params.set("company_id", state.companyId);
    }
    const data = await api(`/articles?${params}`); if (id !== state.renderId || (quiet && (modal.open || document.hidden))) return;
    const scrollY = window.scrollY;
    state.articles = data.items || []; state.total = data.total || 0;
    if (!state.articles.length) { list.replaceChildren(readerEmpty()); return; }
    list.replaceChildren(el("div", { class: "list-meta" }, [el("span", { text: `共 ${state.total} 条${following ? "关注" : ""}资讯` }), el("span", { text: "按发布时间排序" })]), el("div", { class: "news-list" }, state.articles.map(articleRow)));
    const pages = Math.ceil(state.total / state.pageSize);
    if (pages > 1) list.append(el("nav", { class: "pagination", "aria-label": "资讯分页" }, [button("上一页", () => changePage(-1), "button button-small", null, { disabled: state.page <= 1 }), el("span", { text: `${state.page} / ${pages}` }), button("下一页", () => changePage(1), "button button-small", null, { disabled: state.page >= pages })]));
    if (quiet) window.scrollTo({ top: scrollY, behavior: "instant" });
    autoTranslateList(id);
  } catch (error) { if (!quiet && id === state.renderId && error.status !== 401) list.replaceChildren(errorPanel(error, renderView)); }
}
function changePage(delta) { state.page += delta; renderView(); window.scrollTo({ top: 0, behavior: "instant" }); }
function articleRow(article) {
  const translated = state.settings.translation?.ready && state.settings.translation.auto_translate_list && article.translation;
  const display = translated || article;
  const title = el("h2", {}, button(display.title || "无标题资讯", () => articleDetail(article.id), "article-link"));
  const meta = el("div", { class: "news-meta" }, [el("span", { class: "news-category", text: CATEGORIES[article.category] || "资讯" }), el("span", { class: "meta-divider", text: "·" }), el("span", { text: article.source_name || "原始来源" }), el("span", { class: "meta-divider", text: "·" }), el("time", { datetime: article.published_at || article.fetched_at, text: articleTime(article.published_at) })]);
  const save = button(article.saved ? "已收藏" : "收藏", async event => {
    event.stopPropagation(); save.disabled = true;
    try { const updated = await api(`/articles/${article.id}`, { method: "PATCH", body: { saved: !article.saved } }); article.saved = typeof updated.saved === "boolean" ? updated.saved : !article.saved; save.replaceChildren(icon("bookmark"), article.saved ? "已收藏" : "收藏"); save.setAttribute("aria-pressed", String(article.saved)); toast(article.saved ? "已加入收藏" : "已取消收藏"); if (state.saved && !article.saved) renderView(); }
    catch (error) { showError(error); } finally { save.disabled = false; }
  }, "save-inline", "bookmark", { "aria-pressed": String(Boolean(article.saved)), "aria-label": `${article.saved ? "取消收藏" : "收藏"}：${article.title}` });
  const hits = (article.watches || []).map(w => w.name).join("、");
  const content = el("div", {}, [title, meta, display.summary && el("p", { class: "news-summary", text: display.summary }), el("div", { class: "news-tail" }, [el("span", { class: "watch-hit", text: hits ? `关注命中 · ${hits}` : "" }), save])]);
  const row = el("article", { class: `news-item${article.read ? " is-read" : ""}`, "data-article-id": article.id }, content);
  const imageUrl = safeUrl(article.image_url); if (imageUrl) row.append(el("img", { class: "news-image", src: imageUrl, alt: "", loading: "lazy", referrerpolicy: "no-referrer", onerror: event => event.target.remove() }));
  return row;
}

async function autoTranslateList(id) {
  const preferences = state.settings.translation;
  if (listTranslation.controller && !listTranslation.controller.signal.aborted) return;
  if (!state.session?.authenticated || !["news", "following"].includes(state.view) || state.manageWatches) return;
  if (!preferences?.ready || !preferences.auto_translate_list || document.hidden || Date.now() < listTranslation.retryAfter) return;
  const pending = state.articles.filter(article => article.needs_translation && !article.translation);
  if (!pending.length) return;
  const controller = new AbortController();
  listTranslation.controller = controller;
  const revision = preferences.revision;
  const active = () => !controller.signal.aborted && state.session?.authenticated && id === state.renderId && state.settings.translation?.revision === revision;
  let next = 0;
  let stopped = false;
  async function worker() {
    while (active() && !stopped && !document.hidden && next < pending.length) {
      const article = pending[next++];
      try {
        const result = await api(`/articles/${article.id}/translate`, { method: "POST", body: { automatic: true }, signal: controller.signal });
        if (!active()) return;
        const current = state.articles.find(item => item.id === article.id);
        if (!current) continue;
        current.translation = result.translation;
        const row = $(`[data-article-id="${Number(article.id)}"]`, main);
        if (row) { const scrollY = window.scrollY; row.replaceWith(articleRow(current)); window.scrollTo({ top: scrollY, behavior: "instant" }); }
      } catch (error) {
        if (!active()) return;
        if (!error.status || error.status === 401 || error.status === 409 || error.status === 503 || error.code.startsWith("provider_") || error.code === "key_storage") {
          stopped = true;
          listTranslation.retryAfter = Date.now() + 60000;
        }
      }
    }
  }
  try { await Promise.all([worker(), worker()]); }
  finally { if (listTranslation.controller === controller) listTranslation.controller = null; }
}
async function articleDetail(id) {
  const modalId = openModal("资讯", loading("正在打开资讯…"), "article-modal");
  try {
    const article = await api(`/articles/${id}`); if (!modal.open || state.modalId !== modalId) return;
    const sourceUrl = safeUrl(article.url);
    const title = el("h1", { text: article.title || "无标题资讯" });
    const summary = el("p", { text: article.summary || "该来源未提供摘要，请前往原文阅读。" });
    const detail = el("article", { class: "article-detail" }, [el("p", { class: "article-kicker", text: CATEGORIES[article.category] || "资讯" }), title, el("div", { class: "news-meta" }, [el("span", { text: article.source_name || "原始来源" }), el("span", { class: "meta-divider", text: "·" }), el("time", { datetime: article.published_at, text: article.published_at ? dateTime(article.published_at, { year: "numeric" }) : "发布时间未提供" })])]);
    const imageUrl = safeUrl(article.image_url); if (imageUrl) detail.append(el("img", { src: imageUrl, alt: "", referrerpolicy: "no-referrer", onerror: event => event.target.remove() }));
    detail.append(el("section", { class: "article-summary" }, [el("h3", { text: "来源摘要" }), summary, el("div", { class: "article-source-note", text: "内容由公开来源提供。摘要可能不完整，报道全文与后续更正以原始来源为准。" })]));
    if (article.watches?.length) detail.append(el("p", { class: "article-watches", text: `与你的关注相关：${article.watches.map(w => w.name).join("、")}` }));
    const save = button(article.saved ? "已收藏" : "收藏", () => patchDetail("saved", !article.saved), "button", "bookmark", { "aria-pressed": String(Boolean(article.saved)) });
    const read = button("标为未读", () => patchDetail("read", !article.read), "button", "eye");
    const original = sourceUrl ? el("a", { href: sourceUrl, target: "_blank", rel: "noopener noreferrer", class: "button button-primary" }, ["阅读原文", icon("external")]) : el("span", { class: "field-help", text: "原文链接不可用" });
    const translationError = el("p", { class: "form-error translation-error", role: "alert", hidden: true });
    let showingTranslation = false;
    const translate = button("翻译", async () => {
      if (showingTranslation) {
        title.textContent = article.title || "无标题资讯";
        summary.textContent = article.summary || "该来源未提供摘要，请前往原文阅读。";
        showingTranslation = false; translate.textContent = "翻译"; return;
      }
      translate.disabled = true; translate.textContent = "翻译中…"; translationError.hidden = true;
      try {
        if (!state.settings.translation?.ready || !article.translation) {
          const result = await api(`/articles/${id}/translate`, { method: "POST", body: { automatic: false } });
          article.translation = result.translation;
        }
        if (!modal.open || state.modalId !== modalId) return;
        title.textContent = article.translation.title;
        summary.textContent = article.translation.summary || "该来源未提供摘要，请前往原文阅读。";
        showingTranslation = true; translate.textContent = "查看原文"; updateArticle(article);
      } catch (error) {
        if (error.status !== 401 && modal.open && state.modalId === modalId) { translationError.textContent = error.message; translationError.hidden = false; }
      } finally { translate.disabled = false; if (!showingTranslation) translate.textContent = "翻译"; }
    }, "button");
    detail.append(el("div", { class: "article-actions" }, [original, article.needs_translation && translate, save, read]), translationError);
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
  const index = state.articles.findIndex(item => item.id === article.id);
  if (index >= 0) { if (!article.translation) article.translation = state.articles[index].translation; state.articles[index] = article; }
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
  const details = [watch.aliases?.length ? `别名：${watch.aliases.join("、")}` : "未设置别名", watch.league_id ? `绑定联赛：${state.catalog.leagues.find(l => l.id === watch.league_id)?.name || watch.league_id}` : "", watch.keywords?.length ? `上下文（需全部命中）：${watch.keywords.join("、")}` : "", watch.exclude_keywords?.length ? `排除：${watch.exclude_keywords.join("、")}` : "", watch.ticker ? `证券：${watch.market || ""} ${watch.ticker}`.trim() : ""].filter(Boolean);
  return el("article", { class: "manage-row" }, [el("div", { class: "manage-copy" }, [el("h3", { class: watch.enabled ? "" : "disabled-text" }, [watch.name, el("span", { class: "manage-type", text: WATCH_TYPES[watch.type] || watch.type })]), ...details.map(text => el("p", { text })), el("span", { class: `enabled-label${watch.enabled ? "" : " off"}`, text: watch.enabled ? "正在关注" : "已停用" })]), el("div", { class: "row-actions" }, [button("", () => watchForm(watch), "icon-button", "edit", { "aria-label": `编辑关注 ${watch.name}` }), button("", () => confirmDelete("删除关注", `删除「${watch.name}」及其匹配关系，已收取的资讯会保留。`, () => api(`/watches/${watch.id}`, { method: "DELETE" })), "icon-button", "trash", { "aria-label": `删除关注 ${watch.name}` })])]);
}
function watchForm(watch = {}, onSaved = null) {
  const type = select("type", WATCH_TYPES, watch.type || "company");
  const league = select("league_id", { "": "不绑定预设联赛", ...Object.fromEntries(state.catalog.leagues.map(l => [l.id, l.name])) }, watch.league_id || "");
  const leagueField = formField("所属 / 绑定联赛（可选）", league, "绑定预设联赛后，联赛关注也会包含成员球队资讯。自定义联赛可留空，按名称和别名匹配。");
  const form = el("form", { class: "modal-form" }, [
    el("p", { class: "field-caption", text: "名称、别名或证券代码命中任意一个即可；设置上下文后，还需同时命中全部上下文关键词。任意排除词命中都会排除该资讯。" }),
    el("div", { class: "form-row" }, [formField("对象类型", type), formField("名称", input("name", watch.name || "", { required: true, maxlength: 120, placeholder: "如：任天堂、金州勇士" }))]),
    leagueField,
    formField("别名", el("textarea", { name: "aliases", rows: 2, maxlength: 2000, placeholder: "如：Nintendo，任天堂株式会社", text: (watch.aliases || []).join("\n") }), "用逗号、分号或换行分隔；含逗号的名称请使用简短别名。"),
    formField("上下文关键词（可选）", input("keywords", (watch.keywords || []).join("，"), { maxlength: 2000, placeholder: "如：游戏，主机" }), "用于避免同名误匹配。填写多个词时，资讯需同时包含全部关键词；用逗号、分号或换行分隔。"),
    formField("排除关键词（可选）", input("exclude_keywords", (watch.exclude_keywords || []).join("，"), { maxlength: 2000, placeholder: "命中任意排除词的资讯将被排除" })),
    el("div", { class: "form-row" }, [formField("市场（可选）", input("market", watch.market || "", { maxlength: 30, placeholder: "如：NASDAQ、港股" })), formField("证券代码（可选）", input("ticker", watch.ticker || "", { maxlength: 30, placeholder: "如：AAPL、0700" }))]),
    check("enabled", "启用此关注对象", watch.enabled !== false), formFooter(watch.id ? "保存修改" : "添加关注")
  ]);
  function toggleLeague() { const available = ["league", "team"].includes(type.value); leagueField.hidden = !available; league.disabled = !available; }
  type.addEventListener("change", toggleLeague); toggleLeague();
  form.addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(form); const submit = $("[type=submit]", form); submit.disabled = true;
    const body = { type: data.get("type"), name: data.get("name").trim(), aliases: listValues(data.get("aliases")), keywords: listValues(data.get("keywords")), exclude_keywords: listValues(data.get("exclude_keywords")), market: data.get("market").trim(), ticker: data.get("ticker").trim(), league_id: data.get("league_id") || "", enabled: data.has("enabled") };
    if (!body.name) { modalError(form, new Error("请输入关注对象名称。")); submit.disabled = false; return; }
    try {
      const saved = await api(watch.id ? `/watches/${watch.id}` : "/watches", { method: watch.id ? "PUT" : "POST", body });
      closeModal(); toast(watch.id ? "关注已更新，已有资讯已重新匹配" : "已添加关注，并匹配已有资讯"); await refreshData();
      if (onSaved) onSaved(saved); else renderView();
    } catch (error) { if (error.status !== 401) modalError(form, error); } finally { submit.disabled = false; }
  }); openModal(watch.id ? "编辑关注" : "添加关注", form);
}
async function renderSchedules(id) {
  main.replaceChildren(heading("收取计划", `按你的时间收取资讯 · ${state.settings.timezone}`, [button("添加计划", () => scheduleForm(), "button button-primary", "plus")]), el("p", { class: "fetch-note", text: "每个计划对应一个时间点。可设置不同的星期和栏目，保存后立即生效。" }), loading("正在载入收取计划…"));
  try {
    const [data, preferences] = await Promise.all([api("/schedules"), api("/preferences")]); if (id !== state.renderId) return; state.schedules = data.items || []; state.settings = preferences;
    $(".view-heading p", main).textContent = `按你的时间收取资讯 · ${state.settings.timezone}`;
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

async function renderSettings(id) {
  const account = el("section", { class: "settings-section reader-settings" }, [
    el("div", { class: "section-heading" }, el("h2", { text: "我的阅读室" })),
    el("div", { class: "account-row" }, [el("div", {}, [el("strong", { text: state.session.username || "我的账号" }), el("p", { text: "关注对象与收藏会保存在你的阅读室中。" })]), button("退出登录", logout, "button", "logout")])
  ]);
  const preferences = el("section", { class: "settings-section reader-settings" }, [
    el("div", { class: "section-heading" }, el("h2", { text: "阅读与收取" })),
    el("dl", { class: "reading-preferences" }, [el("dt", { text: "收取时区" }), el("dd", { text: state.settings.timezone }), el("dt", { text: "资讯排序" }), el("dd", { text: "按来源发布时间，由新到旧" }), el("dt", { text: "公司焦点" }), el("dd", { text: "在当前浏览器记住你的公司、联赛与球队选择" })]),
    el("p", { class: "field-caption", text: "打开资讯会标为已读；收藏可用于稍后阅读。摘要来自公开来源，完整报道请前往原文。" })
  ]);
  const translation = el("section", { class: "settings-section translation-settings" }, loading("正在载入翻译设置…"));
  main.replaceChildren(heading("设置", "让阅读更适合你的节奏。"), account, preferences, translation);
  try {
    const config = await api("/translation/settings");
    if (id !== state.renderId) return;
    translation.replaceChildren(el("div", { class: "section-heading" }, el("h2", { text: "新闻翻译" })), translationForm(config, id));
  } catch (error) { if (id === state.renderId && error.status !== 401) translation.replaceChildren(errorPanel(error, renderView)); }
}

function translationForm(config, renderId) {
  let profiles = config.profiles;
  const prompts = { ...config.prompts };
  let promptCategory = "games";
  const provider = select("translation_provider", Object.fromEntries(profiles.map(profile => [profile.id, profile.name])), config.provider);
  const protocol = select("translation_protocol", { anthropic: "Anthropic Messages", openai: "OpenAI 兼容 Chat Completions" }, "anthropic");
  const baseUrl = input("translation_url", "", { type: "url", required: true, maxlength: 2048, placeholder: "https://api.deepseek.com/anthropic", autocomplete: "off", spellcheck: "false" });
  const apiKey = input("translation_key", "", { type: "password", maxlength: 4096, autocomplete: "new-password", spellcheck: "false", "aria-label": "供应商 API Key" });
  const keyHelp = el("small");
  const clearKey = check("clear_translation_key", "删除此供应商已保存的 API Key", false);
  const enabled = check("translation_enabled", "启用新闻翻译", config.enabled);
  const automatic = check("translation_automatic", "自动翻译列表中的标题和摘要", config.auto_translate_list);
  const model = input("translation_model", "", { maxlength: 200, placeholder: "填写供应商的模型 ID", autocomplete: "off", spellcheck: "false" });
  const modelChoice = select("translation_model_choice", { "": "手动填写模型 ID" }, "");
  const category = select("translation_prompt_category", { ...CATEGORIES, stocks: "金融 / 股票" }, promptCategory);
  const prompt = el("textarea", { name: "translation_prompt", rows: 12, maxlength: 6000, required: true, value: prompts[promptCategory] });
  const message = el("p", { class: "field-caption translation-message", role: "status", hidden: true });
  const save = el("button", { type: "submit", class: "button button-primary" }, "保存翻译设置");
  const fetchModels = button("保存并获取模型", loadModels, "button");
  const form = el("form", { class: "translation-form" }, [
    el("p", { class: "field-caption", text: "列表自动显示中文标题和摘要；打开资讯后先显示原文，点击“翻译”才显示译文。已有译文会复用。" }),
    el("div", { class: "translation-options" }, [enabled, automatic]),
    el("div", { class: "translation-grid" }, [formField("供应商", provider), formField("接口协议", protocol)]),
    formField("API 地址", baseUrl, "填写供应商的 HTTPS 接口地址；DeepSeek Anthropic 可直接使用上面的示例。"),
    el("label", {}, ["API Key", apiKey, keyHelp]),
    clearKey,
    el("div", { class: "translation-grid" }, [formField("选择模型", modelChoice), formField("模型 ID", model, "也可以直接输入供应商支持的模型 ID。")]),
    el("div", { class: "translation-model-actions" }, [fetchModels, el("small", { text: "先保存接口和 Key，再获取模型列表。供应商未提供列表时可手动填写。" })]),
    el("div", { class: "translation-prompt-heading" }, [formField("分领域翻译提示词", category), button("恢复当前领域默认提示词", () => { prompt.value = config.default_prompts[promptCategory]; prompts[promptCategory] = prompt.value; }, "button button-small")]),
    formField("提示词内容", prompt, "游戏、体育、金融和政治分别保存。内置提示词保留术语、专名、数字和原文的不确定性，你可以自行修改。"),
    el("p", { class: "field-caption", text: "翻译调用你选择的供应商并消耗 API 额度。当前覆盖新闻标题与来源摘要，报道全文请前往原始网站。" }),
    message,
    el("div", { class: "form-footer" }, save)
  ]);
  function selectedProfile() { return profiles.find(profile => profile.id === provider.value); }
  function updateModels(ids) {
    const options = { "": "手动填写模型 ID", ...Object.fromEntries(ids.map(value => [value, value])) };
    modelChoice.replaceChildren(...Object.entries(options).map(([value, name]) => el("option", { value }, name)));
    modelChoice.value = ids.includes(model.value) ? model.value : "";
  }
  function updateKeyHelp() {
    const configured = selectedProfile().api_key_configured;
    keyHelp.textContent = configured ? "已保存 API Key；留空保留，填写新值则替换。密钥不会回显。" : "尚未保存 API Key，请填写你自己的密钥。";
    apiKey.placeholder = configured ? "留空保留已保存的 Key" : "填写 API Key";
    clearKey.hidden = !configured;
  }
  function loadProfile() {
    const profile = selectedProfile();
    protocol.value = profile.protocol; baseUrl.value = profile.base_url; model.value = profile.model;
    apiKey.value = ""; $("input", clearKey).checked = false;
    updateModels(profile.models); updateKeyHelp(); message.hidden = true;
    $(".form-error", form)?.remove();
  }
  provider.addEventListener("change", loadProfile);
  modelChoice.addEventListener("change", () => { if (modelChoice.value) model.value = modelChoice.value; else model.focus(); });
  model.addEventListener("input", () => { modelChoice.value = Array.from(modelChoice.options).some(option => option.value === model.value) ? model.value : ""; });
  category.addEventListener("change", () => { prompts[promptCategory] = prompt.value; promptCategory = category.value; prompt.value = prompts[promptCategory]; });
  function setBusy(busy) { form.querySelectorAll("input,select,textarea,button").forEach(node => { node.disabled = busy; }); }
  async function saveConfig() {
    prompts[promptCategory] = prompt.value;
    if (Object.values(prompts).some(value => !value.trim())) throw new Error("每个领域的提示词都需要填写，请检查其他领域。");
    listTranslation.controller?.abort();
    const saved = await api("/translation/settings", { method: "PUT", body: {
      provider: provider.value, protocol: protocol.value, base_url: baseUrl.value.trim(), model: model.value.trim(),
      api_key: apiKey.value.trim() || null, clear_api_key: $("input", clearKey).checked,
      enabled: $("input", enabled).checked, auto_translate_list: $("input", automatic).checked, prompts
    } });
    apiKey.value = ""; $("input", clearKey).checked = false; profiles = saved.profiles; updateKeyHelp();
    listTranslation.retryAfter = 0;
    await refreshData();
    return saved;
  }
  async function loadModels() {
    if (!form.reportValidity()) return;
    $(".form-error", form)?.remove(); message.hidden = true; setBusy(true);
    try {
      const saved = await saveConfig();
      const result = await api(`/translation/models?provider=${encodeURIComponent(saved.provider)}`);
      if (renderId !== state.renderId) return;
      selectedProfile().models = result.models; updateModels(result.models);
      message.textContent = result.models.length ? `已获取 ${result.models.length} 个模型。选择后保存即可使用。` : "供应商没有返回可用模型，请手动填写模型 ID。";
      message.hidden = false;
    } catch (error) { if (renderId === state.renderId && error.status !== 401) modalError(form, error); }
    finally { setBusy(false); }
  }
  form.addEventListener("submit", async event => {
    event.preventDefault(); $(".form-error", form)?.remove(); message.hidden = true; setBusy(true);
    try {
      const saved = await saveConfig();
      if (renderId !== state.renderId) return;
      message.textContent = !saved.enabled ? "翻译已关闭。" : saved.ready ? "翻译设置已保存，返回资讯列表即可自动翻译。" : "配置已保存；请补全 API Key 和模型 ID 后启用翻译。";
      message.hidden = false;
    } catch (error) { if (renderId === state.renderId && error.status !== 401) modalError(form, error); }
    finally { setBusy(false); }
  });
  loadProfile();
  return form;
}
async function logout() {
  deviceNews.controller?.abort();
  listTranslation.controller?.abort();
  try { await api("/logout", { method: "POST", body: {} }); state.session = await api("/session"); state.renderId += 1; closeModal(); $("#auth-password").value = ""; showAuth(); toast("已退出登录"); }
  catch (error) { showError(error); }
}
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
  const form = el("form", { class: "modal-form" }, [el("p", { class: "field-caption", text: "选择需要更新的栏目，你可以继续阅读。" }), el("fieldset", {}, [el("legend", { text: "选择收取栏目" }), choices("categories", CATEGORIES, state.category ? [state.category] : Object.keys(CATEGORIES))]), formFooter("开始收取")]);
  form.addEventListener("submit", async event => {
    event.preventDefault(); const categories = new FormData(form).getAll("categories"); if (!categories.length) { modalError(form, new Error("请至少选择一个栏目。")); return; }
    const session = state.session;
    const submit = $("[type=submit]", form); submit.disabled = true; state.fetching = true; closeModal(); updateFetchButtons();
    try {
      const skip_source_ids = await collectDeviceNews(categories);
      if (state.session !== session || !state.session?.authenticated) return;
      const result = await api("/fetch", { method: "POST", body: { categories, skip_source_ids } });
      toast(result.message || "已开始收取，完成后资讯会自动更新"); state.status.fetching = true; renderSidebar(); await pollStatus();
    } catch (error) { showError(error); } finally { state.fetching = false; updateFetchButtons(); submit.disabled = false; }
  }); openModal("收取资讯", form);
}
async function pollStatus() {
  if (!state.session?.authenticated || state.pollBusy || document.hidden) return;
  state.pollBusy = true;
  try {
    const wasFetching = Boolean(state.status.fetching); state.status = await api("/status"); renderSidebar(); updateFetchButtons();
    if (!wasFetching && state.status.fetching && !state.fetching && Date.now() - deviceNews.lastAttempt > 60000) collectDeviceNews();
    if (wasFetching && !state.status.fetching) {
      if (["news", "following"].includes(state.view) && !state.manageWatches && !modal.open) renderReader(++state.renderId, true);
    }
    if (!deviceNews.busy && !state.fetching && Date.now() - deviceNews.lastAttempt >= 15 * 60000) collectDeviceNews();
  } catch (error) { if (error.status === 401) showAuth(); } finally { state.pollBusy = false; }
}

document.querySelectorAll("[data-icon]").forEach(node => node.replaceChildren(icon(node.dataset.icon)));
$("#modal-close").addEventListener("click", closeModal);
modal.addEventListener("click", event => { if (event.target === modal) { const rect = modal.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeModal(); } });
$("#header-fetch").addEventListener("click", requestFetch);
window.addEventListener("hashchange", () => { if (state.session?.authenticated) route(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) { pollStatus(); autoTranslateList(state.renderId); } });
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
