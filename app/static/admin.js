"use strict";

const CATEGORIES = { games: "游戏", sports: "体育", stocks: "股票", politics: "政治" };
const state = { reader: null, grant: null, settings: { timezone: "Asia/Shanghai", catch_up: true, catch_up_hours: 4 }, sources: [], status: {}, network: {}, retrying: false, renderId: 0, polling: false, toastTimer: null };
const $ = (selector, root = document) => root.querySelector(selector);
const main = $("#admin-main");
const modal = $("#modal");
const ICONS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  edit: '<path d="m15 4 5 5M4 20l4-1 12-12a2 2 0 0 0-3-3L5 16z"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5M6.1 6.1a8 8 0 0 1 13.2 2.5M17.9 17.9A8 8 0 0 1 4.7 15.4"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 10h18M7 14h2M15 14h2"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 15v6h16v-6"/>',
};
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "value") node.value = value;
    else if (key === "checked") node.checked = Boolean(value);
    else if (key === "disabled") node.disabled = Boolean(value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of Array.isArray(children) ? children : [children]) if (child !== undefined && child !== null && child !== false) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
function icon(name) { const span = el("span"); span.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ICONS.clock}</svg>`; return span.firstElementChild; }
function button(text, handler, className = "button", iconName, attrs = {}) { return el("button", { type: "button", class: className, onclick: handler, ...attrs }, [iconName && icon(iconName), text]); }
function input(name, value, attrs = {}) { return el("input", { name, value, ...attrs }); }
function select(name, options, value) { return el("select", { name }, Object.entries(options).map(([key, title]) => el("option", { value: key, selected: key === value }, title))); }
function field(title, control, hint) { return el("label", {}, [title, control, hint && el("small", { text: hint })]); }
function check(name, title, checked) { return el("label", { class: "checkbox-label" }, [input(name, "on", { type: "checkbox", checked }), title]); }
function toast(message) { clearTimeout(state.toastTimer); $("#toast").textContent = message; $("#toast").hidden = false; state.toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 4500); }
function closeModal() { if (modal.open) modal.close(); }
function openModal(title, content) { $("#modal-title").textContent = title; $("#modal-body").replaceChildren(content); if (!modal.open) modal.showModal(); modal.scrollTop = 0; }
function footer(text = "保存") { return el("div", { class: "form-footer" }, [button("取消", closeModal), el("button", { class: "button button-primary", type: "submit", text })]); }
function dateTime(value) {
  if (!value) return "尚无记录"; const date = new Date(value); if (Number.isNaN(date.getTime())) return "时间未知";
  try { return new Intl.DateTimeFormat("zh-CN", { timeZone: state.settings.timezone, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date); } catch { return date.toLocaleString("zh-CN"); }
}
class ApiError extends Error { constructor(message, status) { super(message); this.status = status; } }
function messageFrom(data, status) {
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) return data.detail.map(item => item.msg || "请检查输入内容").join("；");
  if (data.detail?.message) return data.detail.message;
  return `请求失败（${status}），请稍后重试。`;
}
async function api(path, options = {}, retry = true) {
  const method = options.method || "GET"; const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && state.reader?.csrf_token) headers["X-CSRF-Token"] = state.reader.csrf_token;
  let response;
  try { response = await fetch(`/api${path}`, { method, credentials: "same-origin", headers, body: options.body === undefined ? undefined : JSON.stringify(options.body) }); }
  catch { throw new ApiError("无法连接服务器，请检查网络后重试。", 0); }
  let data; try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) {
    if (response.status === 401) {
      if (path === "/admin/login") { const fresh = await api("/session", {}, false); state.reader = fresh; if (!fresh.authenticated) { state.grant = null; showGate(); } }
      else { state.reader = null; state.grant = null; showGate(); }
      if (!state.reader?.authenticated) throw new ApiError("阅读室登录已过期，请先重新登录。", 401);
    }
    if (response.status === 403 && method !== "GET" && retry) {
      const fresh = await api("/session", {}, false);
      if (fresh.authenticated && fresh.csrf_token !== state.reader?.csrf_token) { state.reader = fresh; return api(path, options, false); }
    }
    if (response.status === 403 && !["/admin/login", "/admin/session", "/admin/logout"].includes(path)) { lockLocally("管理权限已锁定，请再次输入账号密码。"); throw new ApiError("管理权限已锁定，请再次验证。", 403); }
    throw new ApiError(messageFrom(data, response.status), response.status);
  }
  return data;
}
function showError(error) { if (![401, 403].includes(error.status)) toast(error.message); }
function formError(form, error) {
  if ([401, 403].includes(error.status)) return;
  let node = $(".form-error", form); if (!node) { node = el("p", { class: "form-error", role: "alert" }); form.insertBefore(node, $(".form-footer", form)); }
  node.textContent = error.message; node.scrollIntoView({ block: "nearest" });
}
function showGate(message = "") {
  state.renderId += 1; closeModal(); main.replaceChildren();
  $("#admin-boot").hidden = true; $("#admin-app").hidden = true; $("#admin-auth").hidden = false;
  const readerLoggedIn = Boolean(state.reader?.authenticated);
  $("#reader-login-required").hidden = readerLoggedIn; $("#admin-login-form").hidden = !readerLoggedIn;
  $("#admin-auth-title").textContent = readerLoggedIn ? "验证收取管理权限" : "请先登录";
  $("#admin-auth-description").textContent = readerLoggedIn ? "再次输入当前账号密码。管理权限会在 30 分钟后自动锁定。" : "登录阅读室后，再验证收取管理权限。";
  $("#admin-username").value = readerLoggedIn ? state.reader.username : "";
  $("#admin-password").value = "";
  $("#admin-auth-error").textContent = message; $("#admin-auth-error").hidden = !message;
}
function lockLocally(message) { state.grant = null; showGate(message); }
function updateExpiry() { $("#admin-expiry").textContent = state.grant?.expires_at ? `权限有效至 ${dateTime(state.grant.expires_at)}` : "管理权限已验证"; }
async function enterAdmin() {
  $("#admin-boot").hidden = true; $("#admin-auth").hidden = true; $("#admin-app").hidden = false;
  updateExpiry(); await renderAdmin();
}
function loading() { return el("div", { class: "loading-state", role: "status" }, [el("p", { text: "正在载入收取设置…" }), el("div", { class: "loading-bar" }), el("div", { class: "loading-bar short" })]); }
function section(title, copy, content, action) { return el("section", { class: "settings-section" }, [el("div", { class: "section-heading" }, [el("div", {}, [el("h2", { text: title }), copy && el("p", { text: copy })]), action]), content]); }
async function renderAdmin() {
  const id = ++state.renderId; main.replaceChildren(loading());
  try {
    const [settings, sources, runs, status, network] = await Promise.all([api("/settings"), api("/sources"), api("/runs?limit=30"), api("/status"), api("/network")]);
    if (id !== state.renderId || !state.grant?.authenticated) return;
    state.settings = settings; state.sources = sources.items || []; state.status = status; state.network = network; updateExpiry();
    main.replaceChildren(
      el("div", { class: "view-heading" }, el("div", {}, [el("h1", { text: "收取管理" }), el("p", { text: "管理来源、服务端收取偏好与执行记录。" })])),
      section("来源连接", "本页重试由服务器执行。设备补收请返回阅读页，在「设置 → 海外资讯」中启动。", networkBox()),
      section("资讯来源", "仅支持公开 RSS / Atom 与 Steam 官方资讯。", el("div", { id: "source-list", class: "manage-list sources-list" }, state.sources.length ? state.sources.map(sourceRow) : el("p", { class: "inline-empty", text: "还没有来源。添加公开订阅地址或 Steam 游戏 App ID。" })), button("添加来源", () => sourceForm(), "button button-small", "plus")),
      section("服务端收取偏好", "时区影响所有收取计划，补收会合并遗漏的任务。", settingsForm()),
      section("收取状态", "计划与手动收取共用已启用的来源。", el("div", { id: "admin-status" }, statusBox())),
      section("最近收取记录", "成功来源会保留资讯；失败原因记录在这里。", el("div", { id: "run-history" }, runTable(runs.items || [])), button("刷新", refreshRuns, "button button-small", "refresh")),
      section("配置导出", "导出来源、关注、计划与偏好；文件不含账号密码。", button("导出配置", exportConfig, "button", "download"))
    );
  } catch (error) {
    if (id === state.renderId && ![401, 403].includes(error.status)) main.replaceChildren(el("div", { class: "error-state", role: "alert" }, [el("strong", { text: "暂时无法载入" }), el("p", { text: error.message }), button("重试", renderAdmin, "button", "refresh")]));
  }
}
function sourceHint(error) {
  if (!error) return "";
  if (/DNS|域名解析|非公网/.test(error)) return "检查域名解析；备用解析也失败时，需检查服务器网络。";
  if (/HTTP 403/.test(error)) return "来源拒绝了访问；稍后再试或检查来源的访问限制。";
  if (/HTTP 404/.test(error)) return "订阅地址可能已变更，请核对来源官方网站。";
  if (/XML|JSON|格式|内容/.test(error)) return "返回内容不是有效订阅数据，请核对订阅地址。";
  if (/超时|连接/.test(error)) return "这是服务器请求失败。预置海外 RSS 可返回阅读页，在「设置 → 海外资讯」点击「重新补收」，查看手机或电脑的收取结果。本页重试仍使用服务器网络。";
  return "请核对来源地址和最近收取记录。";
}
function networkBox() {
  const failed = state.sources.filter(source => source.enabled && source.last_error).length;
  const modes = { auto: "自动选择", direct: "直连", proxy: "代理" };
  const dnsModes = { fallback: "系统解析，失败时使用备用加密 DNS", system: "系统 DNS", https: "加密 DNS" };
  return el("div", { id: "source-network", class: "status-box" }, [
    el("p", { class: "status-line", text: `连接方式：${modes[state.network.fetch_mode] || "未知"}；${state.network.proxy_configured ? "已配置代理" : "未配置代理"}` }),
    el("p", { class: "status-line", text: `域名解析：${dnsModes[state.network.dns_mode] || "未知"}` }),
    el("p", { class: "field-caption", text: "这里显示服务器连接情况。备用 DNS 只处理解析故障；若服务器无法连接海外来源，可以在阅读页通过当前设备补收。设备补收的失败原因在阅读页显示。" }),
    el("div", { class: "source-retry-actions" }, [el("span", { class: "muted", text: `${failed} 个启用来源收取失败` }), button("重试失败来源", retryFailedSources, "button button-small", "refresh", { disabled: !failed || state.status.fetching || state.retrying })])
  ]);
}
async function retryFailedSources() {
  if (state.status.fetching || state.retrying) return;
  state.retrying = true;
  const box = $("#source-network"); if (box) box.replaceWith(networkBox());
  try {
    const result = await api("/fetch", { method: "POST", body: { categories: Object.keys(CATEGORIES), failed_only: true } });
    state.status.fetching = true; toast(result.message); await renderAdmin();
  } catch (error) { showError(error); }
  finally { state.retrying = false; const target = $("#source-network"); if (target) target.replaceWith(networkBox()); }
}
function sourceRow(source) {
  return el("article", { class: "manage-row" }, [el("div", { class: "manage-copy" }, [el("h3", { class: source.enabled ? "" : "disabled-text" }, [source.name, el("span", { class: "manage-type", text: `${CATEGORIES[source.category] || "资讯"} · ${source.kind === "steam" ? "Steam" : "RSS"}` })]), el("p", { class: "source-url", text: source.kind === "steam" ? `Steam App ID：${source.steam_appid || "未设置"}` : source.url }), el("p", { text: source.last_success_at ? `最近成功（服务器或设备）：${dateTime(source.last_success_at)}` : "尚未成功收取" }), source.last_error && el("p", { class: "source-error", text: `服务器收取：${source.last_error}` }), source.last_error && el("p", { class: "muted", text: sourceHint(source.last_error) }), !source.enabled && el("span", { class: "enabled-label off", text: "已停用" })]), el("div", { class: "row-actions" }, [button("", () => sourceForm(source), "icon-button", "edit", { "aria-label": `编辑来源 ${source.name}` }), button("", () => deleteSource(source), "icon-button", "trash", { "aria-label": `删除来源 ${source.name}` })])]);
}
function sourceForm(source = {}) {
  const kind = select("kind", { rss: "公开 RSS / Atom", steam: "Steam 官方游戏资讯" }, source.kind || "rss");
  const url = input("url", source.url || "", { type: "url", maxlength: 2048, placeholder: "https://example.com/feed.xml" });
  const appid = input("steam_appid", source.steam_appid || "", { type: "number", min: 1, max: 2147483647, step: 1, placeholder: "如：570" });
  const urlField = field("订阅地址", url, "使用可公开访问的 HTTP / HTTPS RSS 或 Atom 地址。");
  const steamField = field("Steam 游戏 App ID", appid, "可在商店地址 store.steampowered.com/app/数字 中找到。");
  const form = el("form", { class: "modal-form" }, [field("来源名称", input("name", source.name || "", { required: true, maxlength: 120, placeholder: "为来源起一个名称" })), el("div", { class: "form-row" }, [field("来源类型", kind), field("所属栏目", select("category", CATEGORIES, source.category || "games"))]), urlField, steamField, check("enabled", "启用此来源", source.enabled !== false), footer(source.id ? "保存修改" : "添加来源")]);
  function toggle() { const steam = kind.value === "steam"; urlField.hidden = steam; url.disabled = steam; url.required = !steam; steamField.hidden = !steam; appid.disabled = !steam; appid.required = steam; }
  kind.addEventListener("change", toggle); toggle();
  form.addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(form); const submit = $("[type=submit]", form); const steam = data.get("kind") === "steam";
    const body = { name: data.get("name").trim(), kind: data.get("kind"), category: data.get("category"), enabled: data.has("enabled"), url: steam ? "" : data.get("url").trim() };
    if (steam) body.steam_appid = Number(data.get("steam_appid"));
    if (!body.name) { formError(form, new Error("请输入来源名称。")); return; }
    if (!steam) { try { const parsed = new URL(body.url); if (!["http:", "https:"].includes(parsed.protocol)) throw new Error(); } catch { formError(form, new Error("请输入有效的公开 HTTP / HTTPS 地址。")); return; } }
    submit.disabled = true;
    try { await api(source.id ? `/sources/${source.id}` : "/sources", { method: source.id ? "PUT" : "POST", body }); closeModal(); toast("资讯来源已保存"); await renderAdmin(); }
    catch (error) { formError(form, error); } finally { submit.disabled = false; }
  }); openModal(source.id ? "编辑资讯来源" : "添加资讯来源", form);
}
function deleteSource(source) {
  const content = el("div", {}, el("p", { class: "confirm-text", text: `删除「${source.name}」后将不再从该来源收取，已收取的资讯会保留。` }));
  const submit = button("确认删除", async () => {
    submit.disabled = true;
    try { await api(`/sources/${source.id}`, { method: "DELETE" }); closeModal(); toast("来源已删除"); await renderAdmin(); } catch (error) { showError(error); } finally { submit.disabled = false; }
  }, "button button-danger", "trash");
  content.append(el("div", { class: "form-footer" }, [button("取消", closeModal), submit])); openModal("删除资讯来源", content);
}
function settingsForm() {
  const zone = input("timezone", state.settings.timezone, { required: true, list: "timezones", maxlength: 80 });
  const datalist = el("datalist", { id: "timezones" }, ["Asia/Shanghai", "Asia/Hong_Kong", "Asia/Tokyo", "Asia/Singapore", "Europe/London", "America/New_York", "America/Los_Angeles", "UTC"].map(value => el("option", { value })));
  const catchup = check("catch_up", "服务恢复后补收遗漏的计划", state.settings.catch_up);
  const hours = input("catch_up_hours", state.settings.catch_up_hours, { type: "number", min: 1, max: 72, step: 1, required: true });
  const form = el("form", { class: "settings-form" }, [field("计划时区", zone, "使用 IANA 时区名称，如 Asia/Shanghai。此设置影响全部收取计划。"), datalist, catchup, field("补收时间范围（小时）", hours, "只补收此范围内的遗漏任务，多个遗漏时段合并为一次。"), el("button", { type: "submit", class: "button button-primary", text: "保存收取偏好" })]);
  $("input", catchup).addEventListener("change", event => { hours.disabled = !event.target.checked; }); hours.disabled = !state.settings.catch_up;
  form.addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(form); const submit = $("[type=submit]", form); const timezone = data.get("timezone").trim();
    try { new Intl.DateTimeFormat("zh-CN", { timeZone: timezone }); } catch { toast("请输入有效的时区名称，如 Asia/Shanghai。"); return; }
    submit.disabled = true;
    try { state.settings = await api("/settings", { method: "PUT", body: { timezone, catch_up: data.has("catch_up"), catch_up_hours: Number(data.get("catch_up_hours") || state.settings.catch_up_hours) } }); toast("服务端收取偏好已保存"); await renderAdmin(); }
    catch (error) { showError(error); } finally { submit.disabled = false; }
  }); return form;
}
function statusLine(name, value, iconName) { return el("p", { class: "status-line" }, [icon(iconName), el("span", {}, [`${name} · `, el("strong", { text: value })])]); }
function statusBox() {
  return el("div", { class: "status-box" }, [statusLine("当前状态", state.status.fetching ? "正在收取资讯…" : "等待下次收取", "refresh"), statusLine("最近成功", state.status.last_success_at ? dateTime(state.status.last_success_at) : "尚未成功收取", "clock"), statusLine("下次计划", state.status.next_run_at ? dateTime(state.status.next_run_at) : "未启用收取计划", "calendar")]);
}
function runTable(runs) {
  if (!runs.length) return el("p", { class: "inline-empty", text: "尚无收取记录。在阅读室手动收取，或等待已启用的计划。" });
  const names = { running: "进行中", success: "成功", completed: "成功", failed: "失败", partial: "部分成功", error: "失败", interrupted: "已中断" };
  const triggers = { manual: "手动收取", schedule: "定时计划", scheduled: "定时计划", catch_up: "遗漏补收", catchup: "遗漏补收", browser: "当前设备补收" };
  return el("table", { class: "run-table" }, [el("caption", { class: "skip-link", text: "最近收取记录" }), el("thead", {}, el("tr", {}, [el("th", { scope: "col", text: "时间 / 触发" }), el("th", { scope: "col", text: "结果" }), el("th", { scope: "col", text: "新增" })])), el("tbody", {}, runs.map(run => el("tr", {}, [el("td", {}, [el("time", { datetime: run.started_at, text: dateTime(run.started_at) }), el("div", { class: "muted", text: triggers[run.trigger] || run.trigger || "收取" })]), el("td", {}, [el("span", { class: `run-status ${["success", "completed"].includes(run.status) ? "success" : ["failed", "error"].includes(run.status) ? "failed" : run.status === "partial" ? "partial" : ""}`, text: names[run.status] || run.status }), run.error && el("div", { class: "run-error", text: run.error }), run.source_count !== undefined && el("div", { class: "muted", text: `${run.source_count} 个来源` })]), el("td", { text: run.new_count == null ? "—" : `${run.new_count} 条` })])))]);
}
async function refreshRuns() { const target = $("#run-history"); if (!target) return; try { const data = await api("/runs?limit=30"); if (target.isConnected) target.replaceChildren(runTable(data.items || [])); } catch (error) { showError(error); } }
async function exportConfig() {
  try { const data = await api("/export"); const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" })); const link = el("a", { href: url, download: `xunlan-config-${new Date().toISOString().slice(0, 10)}.json` }); document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); toast("配置已导出"); } catch (error) { showError(error); }
}
async function pollAdmin() {
  if (!state.reader?.authenticated || !state.grant?.authenticated || state.polling || document.hidden) return;
  state.polling = true;
  try {
    const grant = await api("/admin/session");
    if (!grant.authenticated) { lockLocally("管理权限已到期，请再次输入账号密码。"); return; }
    state.grant = grant; updateExpiry(); const status = await api("/status"); if (!state.grant?.authenticated) return;
    const completed = state.status.fetching && !status.fetching; state.status = status;
    const box = $("#admin-status"); if (box) box.replaceChildren(statusBox());
    if (status.fetching || completed) await refreshRuns();
    if (completed) {
      const sources = await api("/sources");
      if (!state.grant?.authenticated) return;
      state.sources = sources.items || [];
      const list = $("#source-list");
      if (list) {
        if (state.sources.length) list.replaceChildren(...state.sources.map(sourceRow));
        else list.replaceChildren(el("p", { class: "inline-empty", text: "还没有来源。添加公开订阅地址或 Steam 游戏 App ID。" }));
      }
    }
    const network = $("#source-network"); if (network) network.replaceWith(networkBox());
  } catch (error) { showError(error); } finally { state.polling = false; }
}
$("#modal-close").addEventListener("click", closeModal);
modal.addEventListener("click", event => { if (event.target === modal) { const rect = modal.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeModal(); } });
$("#admin-lock").addEventListener("click", async () => { try { await api("/admin/logout", { method: "POST", body: {} }); lockLocally(); toast("管理已锁定，阅读室仍保持登录"); } catch (error) { showError(error); } });
$("#admin-login-form").addEventListener("submit", async event => {
  event.preventDefault(); const password = $("#admin-password").value; $("#admin-password").value = "";
  const submit = $("#admin-login-submit"); submit.disabled = true; $("#admin-auth-error").hidden = true;
  try { state.grant = await api("/admin/login", { method: "POST", body: { username: state.reader.username, password } }); if (state.grant.authenticated) await enterAdmin(); else showGate("未能验证管理权限，请重新输入密码。"); }
  catch (error) { if (state.reader?.authenticated) { $("#admin-auth-error").textContent = error.message; $("#admin-auth-error").hidden = false; } }
  finally { submit.disabled = false; $("#admin-password").value = ""; }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollAdmin(); });
(async () => {
  try { state.reader = await api("/session"); if (!state.reader.authenticated) { showGate(); return; } state.grant = await api("/admin/session"); if (state.grant.authenticated) await enterAdmin(); else showGate(); }
  catch (error) { $("#admin-boot").hidden = true; showGate(); if (![401, 403].includes(error.status)) { $("#admin-auth-error").textContent = error.message; $("#admin-auth-error").hidden = false; } }
})();
setInterval(pollAdmin, 15000);
