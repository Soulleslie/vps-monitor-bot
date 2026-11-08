"use strict";
// VPS 补货监控 · Web 后台
// 安全约定：所有来自商家网站或用户输入的文本都经过 esc() 转义后才插入页面；不使用内联脚本/样式（CSP）。

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const view = $("#view");
const REFRESH_MS = 15000;

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------------------------------------------------------- 图标
const ICONS = {
  overview: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  sites: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  chats: '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4 20-7z"/>',
  settings: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5M21 12H9"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36L21 8"/><path d="M21 3v5h-5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
  external: '<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  alert: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
  box: '<path d="M21 8 12 3 3 8v8l9 5 9-5V8z"/><path d="m3 8 9 5 9-5M12 13v8"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
  play: '<path d="m6 4 14 8-14 8z"/>',
  back: '<path d="m15 18-6-6 6-6"/>',
  key: '<circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.7 12.3 9.3-9.3M17 6l3 3M14 9l2 2"/>',
  bot: '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 8V4M9 14h.01M15 14h.01"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
  send: '<path d="m3 11 18-8-8 18-2-8z"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
};
const icon = (name, cls = "") => `<svg class="i ${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ""}</svg>`;
$$("[data-icon]").forEach(el => {
  const label = el.textContent;
  el.innerHTML = icon(el.dataset.icon) + `<span class="lbl">${esc(label)}</span>`;
});

// ---------------------------------------------------------------- 格式化
const pad = n => String(n).padStart(2, "0");
function fmtTime(ts, withYear = false) {
  if (!ts) return "-";
  const d = new Date(ts * 1000), now = new Date();
  const date = (withYear || d.getFullYear() !== now.getFullYear() ? `${d.getFullYear()}-` : "") + `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  return `${date} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function fmtDur(s) {
  if (s == null) return "-";
  s = Math.max(0, Math.floor(s));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return s % 60 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${s / 60} 分钟`;
  if (s < 86400) return s % 3600 === 0 ? `${s / 3600} 小时` : `${Math.floor(s / 3600)} 小时 ${Math.floor(s % 3600 / 60)} 分`;
  return `${Math.floor(s / 86400)} 天 ${Math.floor(s % 86400 / 3600)} 小时`;
}
function ago(ts) {
  if (!ts) return "从未";
  const s = Math.floor(Date.now() / 1000) - ts;
  if (s < 10) return "刚刚";
  if (s < 60) return `${s} 秒前`;
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)} 天前`;
  return fmtTime(ts);
}
const chatType = t => ({ private: "私聊", group: "群组", supergroup: "群组", channel: "频道" }[t] || "会话");
const initial = s => esc((String(s || "?").replace(/^@/, "").trim()[0] || "?").toUpperCase());
function stockBadge(p) {
  if (!p.present) return `<span class="badge">已下架</span>`;
  if (p.status === "in_stock") return `<span class="badge ok">有货${p.qty != null ? " · " + esc(p.qty) : ""}</span>`;
  if (p.status === "out_of_stock") return `<span class="badge bad">缺货</span>`;
  return `<span class="badge">未知</span>`;
}
const EVENT = { restock: ["ok", "补货"], soldout: ["bad", "售罄"], new: ["warn", "上新"] };
function timelineItem(e, withSite) {
  const [cls, label] = EVENT[e.kind] || ["", e.kind];
  const sub = [];
  if (withSite) sub.push(`<a href="#/sites/${e.site_id}">${esc(e.site_name)}</a>`);
  if (e.kind === "soldout" && e.duration) sub.push(`有货持续 ${fmtDur(e.duration)}`);
  if (e.kind !== "soldout" && e.qty) sub.push(`库存 ${esc(e.qty)}`);
  return `<li><span class="dot ${cls} tl-dot"></span>
    <div class="tl-main"><div class="tl-title"><span class="badge ${cls}">${label}</span> ${esc(e.name)}</div>
    ${sub.length ? `<div class="tl-sub">${sub.join(" · ")}</div>` : ""}</div>
    <time title="${fmtTime(e.ts, true)}">${fmtTime(e.ts)}</time></li>`;
}
function emptyState(title, text, ic = "box") {
  return `<div class="empty">${icon(ic)}<b>${esc(title)}</b>${text ? `<div class="sm">${text}</div>` : ""}</div>`;
}
// CSP 不允许内联 style 属性，进度条宽度在渲染后用 JS 设置
function applyMeters(root = view) { $$(".meter > i[data-w]", root).forEach(i => { i.style.width = i.dataset.w + "%"; }); }

// ---------------------------------------------------------------- 提示与确认框
function toast(msg, kind = "ok") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = icon(kind === "bad" ? "alert" : "check") + `<div>${esc(msg)}</div>`;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 250); }, kind === "bad" ? 5500 : 2800);
}
function confirmDialog({ title, text = "", ok = "确定", danger = false }) {
  return new Promise(resolve => {
    const modal = $("#modal"), okBtn = $("#modal-ok"), cancelBtn = $("#modal-cancel");
    $("#modal-title").textContent = title;
    $("#modal-text").textContent = text;
    okBtn.textContent = ok;
    okBtn.classList.toggle("danger-fill", danger);
    modal.classList.remove("hidden");
    okBtn.focus();
    const done = v => {
      modal.classList.add("hidden");
      okBtn.onclick = cancelBtn.onclick = modal.onclick = document.onkeydown = null;
      resolve(v);
    };
    okBtn.onclick = () => done(true);
    cancelBtn.onclick = () => done(false);
    modal.onclick = ev => { if (ev.target === modal) done(false); };
    document.onkeydown = ev => { if (ev.key === "Escape") done(false); };
  });
}

// ---------------------------------------------------------------- 请求
async function api(method, path, data) {
  const opt = { method, headers: { "X-Requested-With": "vrb" }, credentials: "same-origin" };
  if (data !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(data); }
  let res;
  try { res = await fetch(path, opt); } catch (_) { throw new Error("无法连接服务器，请检查网络"); }
  let json = {};
  try { json = await res.json(); } catch (_) { /* 非 JSON 响应 */ }
  if (res.status === 401 && path !== "/api/login") { showLogin(); throw new Error("未登录"); }
  if (!res.ok) throw new Error(json.error || `请求失败（HTTP ${res.status}）`);
  return json;
}
async function busy(btn, fn) {
  let html;
  if (btn) { html = btn.innerHTML; btn.disabled = true; btn.innerHTML = `<span class="spin"></span>${btn.textContent.trim() ? `<span>${esc(btn.textContent.trim())}</span>` : ""}`; }
  try { return await fn(); }
  catch (e) { if (e.message !== "未登录") toast(e.message, "bad"); }
  finally { if (btn && btn.isConnected) { btn.disabled = false; btn.innerHTML = html; } }
}

// ---------------------------------------------------------------- 自动刷新
const live = { timer: null, ticker: null, at: 0, fn: null, dirty: false };
function stopLive() { clearInterval(live.timer); clearInterval(live.ticker); live.fn = null; live.dirty = false; }
function startLive(fn) {
  stopLive();
  live.fn = fn; live.at = Date.now();
  live.timer = setInterval(async () => {
    if (document.visibilityState !== "visible" || live.dirty || !$("#modal").classList.contains("hidden")
        || !$("#drawer").classList.contains("hidden")) return;
    const a = document.activeElement;
    if (a && view.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return;  // 正在输入时不刷新
    const y = window.scrollY;
    try { await live.fn(true); live.at = Date.now(); window.scrollTo(0, y); } catch (_) { /* 下次再试 */ }
  }, REFRESH_MS);
  live.ticker = setInterval(updateLiveTag, 1000);
}
function updateLiveTag() {
  const el = $("#live-tag");
  if (!el) return;
  const s = Math.floor((Date.now() - live.at) / 1000);
  el.innerHTML = `<span class="dot live"></span>实时 · ${s < 3 ? "刚刚更新" : `${s} 秒前更新`}`;
}
const liveTag = () => `<span class="live-tag" id="live-tag"><span class="dot live"></span>实时 · 刚刚更新</span>`;

// ---------------------------------------------------------------- 登录
function showLogin() {
  stopLive();
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  setTimeout(() => $("#login-token").focus(), 0);
}
$("#login-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  $("#login-error").textContent = "";
  const btn = ev.submitter;
  await busy(btn, async () => {
    try {
      await api("POST", "/api/login", { token: $("#login-token").value });
    } catch (e) { $("#login-error").textContent = e.message; return; }
    $("#login-token").value = "";
    $("#login").classList.add("hidden");
    $("#app").classList.remove("hidden");
    route();
    checkUpdateBadge();
  });
});
$("#logout").addEventListener("click", async () => {
  if (!await confirmDialog({ title: "退出登录？", ok: "退出" })) return;
  try { await api("POST", "/api/logout"); } catch (_) { /* 忽略 */ }
  showLogin();
});

function setBotState(bot) {
  const el = $("#bot-state");
  let dot, title, sub;
  if (bot.running) { dot = "ok"; title = "机器人在线"; sub = "@" + esc(bot.username); }
  else if (!bot.has_token) { dot = "warn"; title = "未设置 Token"; sub = "前往设置填写"; }
  else { dot = "bad"; title = "机器人未连接"; sub = esc(bot.error || "请检查 Token"); }
  el.innerHTML = `<span class="dot ${dot}"></span><div class="bs-text"><b>${title}</b><span>${sub}</span></div>`;
}

// ---------------------------------------------------------------- 路由
function skeleton() {
  return `<div class="page-head"><div><div class="skel skel-title"></div></div></div>
    <div class="stats">${"<div class='stat'><div class='skel ico'></div><div class='grow'><div class='skel skel-line'></div></div></div>".repeat(4)}</div>
    <div class="card"><div class="card-body">${"<div class='skel skel-line'></div>".repeat(5)}</div></div>`;
}
async function route() {
  stopLive();
  closeDrawer();
  const [, tab, id] = (location.hash || "#/overview").split("/");
  $$("#tabs a").forEach(a => a.classList.toggle("active", a.dataset.tab === (tab || "overview")));
  view.innerHTML = skeleton();
  try {
    if (tab === "sites" && id) await pageSite(Number(id));
    else if (tab === "sites") await pageSites();
    else if (tab === "chats") await pageChats();
    else if (tab === "settings") await pageSettings();
    else await pageOverview();
    window.scrollTo(0, 0);
  } catch (e) {
    if (e.message !== "未登录") view.innerHTML = `<div class="card">${emptyState("加载失败", esc(e.message), "alert")}</div>`;
  }
}
window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- 概览
function chartSvg(daily, width) {
  const W = Math.max(260, Math.round(width || 700)), H = 150, top = 10, bottom = 22, n = daily.length, slot = W / n;
  const labelEvery = W < 480 ? 3 : 2;
  const max = Math.max(1, ...daily.map(d => Math.max(d.restock, d.soldout)));
  const scale = v => (H - top - bottom) * v / max;
  const ticks = [max, Math.round(max / 2)].filter((v, i, a) => v > 0 && a.indexOf(v) === i);
  let s = ticks.map(t => { const y = H - bottom - scale(t); return `<line class="grid" x1="0" x2="${W}" y1="${y}" y2="${y}"/><text x="0" y="${y - 4}">${t}</text>`; }).join("");
  s += `<line class="grid" x1="0" x2="${W}" y1="${H - bottom}" y2="${H - bottom}"/>`;
  daily.forEach((d, i) => {
    const x = i * slot + slot * 0.22, bw = slot * 0.34, h1 = scale(d.restock), h2 = scale(d.soldout);
    s += `<rect class="bar" x="${x}" y="${H - bottom - h1}" width="${bw}" height="${Math.max(h1, d.restock ? 2 : 0)}" rx="3"><title>${d.date} 补货/上新 ${d.restock} 次</title></rect>`;
    s += `<rect class="bar sold" x="${x + bw + 2}" y="${H - bottom - h2}" width="${bw * 0.7}" height="${Math.max(h2, d.soldout ? 2 : 0)}" rx="3"><title>${d.date} 售罄 ${d.soldout} 次</title></rect>`;
    if ((n - 1 - i) % labelEvery === 0) s +=`<text x="${i * slot + slot / 2}" y="${H - 6}" text-anchor="middle">${d.date.slice(5)}</text>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="近 14 天补货次数">${s}</svg>`;
}

async function pageOverview(silent) {
  const [d, sites] = await Promise.all([api("GET", "/api/overview"), api("GET", "/api/sites")]);
  setBotState(d.bot);
  const c = d.counts;
  const attention = sites.sites.filter(s => s.enabled && (s.fail_count || !s.targets));
  const callout = !d.bot.has_token
    ? `<div class="callout info">${icon("bot")}<div><b>第一步：连接 Telegram 机器人</b>还没有设置 Bot Token，补货消息无法推送。<a href="#/settings">前往设置 →</a></div></div>`
    : !d.bot.running ? `<div class="callout">${icon("alert")}<div><b>机器人未连接</b>${esc(d.bot.error || "")} <a href="#/settings">检查设置 →</a></div></div>` : "";
  const restock14 = d.daily.reduce((a, x) => a + x.restock, 0);
  view.innerHTML = `
    <div class="page-head"><div><h1>概览</h1><p>监控运行状态与库存变动</p></div>${liveTag()}</div>
    ${callout}
    <div class="stats">
      <div class="stat"><div class="ico">${icon("sites")}</div><div><div class="label">监控网址</div><div class="value">${c.sites}</div><div class="sub">检查中 ${c.active_sites} · 异常 ${c.failing_sites}</div></div></div>
      <div class="stat"><div class="ico ok">${icon("box")}</div><div><div class="label">有货机型</div><div class="value">${c.in_stock}<small> / ${c.products}</small></div><div class="sub">当前页面上识别到的机型</div></div></div>
      <div class="stat"><div class="ico info">${icon("chats")}</div><div><div class="label">推送对象</div><div class="value">${c.chats}</div><div class="sub">私聊 / 群组 / 频道</div></div></div>
      <div class="stat"><div class="ico warn">${icon("activity")}</div><div><div class="label">24 小时库存变动</div><div class="value">${c.events_24h}</div><div class="sub">近 14 天补货 ${restock14} 次</div></div></div>
    </div>
    <div class="cols">
      <div>
        <div class="card"><div class="card-head"><div><h2>近 14 天补货趋势</h2><p>每天的补货/上新与售罄次数</p></div></div>
          <div class="chart"><div id="chart-box"></div>
            <div class="legend"><span><i class="lg-ok"></i>补货 / 上新</span><span><i class="lg-bad"></i>售罄</span></div></div></div>
        <div class="card"><div class="card-head"><div><h2>最近库存变动</h2><p>最新 30 条</p></div></div>
          <div class="card-body flush">${d.events.length ? `<ul class="timeline">${d.events.map(e => timelineItem(e, true)).join("")}</ul>` : emptyState("暂无记录", "添加监控网址后，补货和售罄会记录在这里", "activity")}</div></div>
      </div>
      <div>
        <div class="card"><div class="card-head"><h2>运行状态</h2></div><div class="card-body">
          <dl class="kv">
            <dt>机器人</dt><dd>${d.bot.running ? `<span class="badge ok">在线</span> @${esc(d.bot.username)}` : d.bot.has_token ? `<span class="badge bad">未连接</span>` : `<span class="badge warn">未设置</span>`}</dd>
            <dt>默认检查间隔</dt><dd>每 ${fmtDur(d.check_interval)}</dd>
            <dt>已运行</dt><dd>${fmtDur(d.uptime)}</dd>
            <dt>本次启动检查</dt><dd class="num">${d.checks} 次</dd>
            <dt>本次启动推送事件</dt><dd class="num">${d.pushed_events} 个</dd>
          </dl></div></div>
        <div class="card"><div class="card-head"><div><h2>需要关注</h2><p>抓取失败或未设置推送对象的网址</p></div></div>
          <div class="card-body flush">${attention.length ? `<ul class="timeline">${attention.map(s => `
            <li><span class="dot ${s.fail_count ? "bad" : "warn"} tl-dot"></span><div class="tl-main">
              <div class="tl-title"><a href="#/sites/${s.id}">${esc(s.name)}</a></div>
              <div class="tl-sub">${s.fail_count ? `连续失败 ${s.fail_count} 次：${esc(s.last_error)}` : "未设置推送对象，不会检查"}</div></div><time></time></li>`).join("")}</ul>`
            : emptyState("一切正常", "", "check")}</div></div>
      </div>
    </div>`;
  const chartBox = $("#chart-box");
  chartBox.innerHTML = chartSvg(d.daily, chartBox.clientWidth);
  if (!silent) startLive(pageOverview);
}

// ---------------------------------------------------------------- 监控网址列表
const sitesUI = { q: "", filter: "all", data: null };
function siteStatus(s) {
  if (!s.enabled) return ["", "已停用"];
  if (s.fail_count) return ["bad", `失败 ${s.fail_count} 次`];
  if (!s.targets) return ["warn", "未设置推送"];
  return ["ok", "正常"];
}
function renderSiteRows() {
  const { data, q, filter } = sitesUI;
  const rows = data.sites.filter(s => {
    if (q && !(`${s.name} ${s.url}`.toLowerCase().includes(q))) return false;
    if (filter === "stock") return s.in_stock > 0;
    if (filter === "error") return s.fail_count > 0;
    if (filter === "idle") return !s.targets || !s.enabled;
    return true;
  });
  const body = $("#site-rows");
  if (!rows.length) { body.innerHTML = `<tr><td colspan="7">${emptyState(data.sites.length ? "没有符合条件的网址" : "还没有监控网址", data.sites.length ? "" : "在上方粘贴商品分组页网址开始监控", "sites")}</td></tr>`; return; }
  body.innerHTML = rows.map(s => {
    const [cls, label] = siteStatus(s);
    const pct = s.products ? Math.round(s.in_stock / s.products * 100) : 0;
    return `<tr class="click" data-id="${s.id}">
      <td><div class="cell-title">${esc(s.name)}</div><div class="cell-sub">${esc(s.url)}</div></td>
      <td><div class="nowrap num sm">${s.in_stock} / ${s.products}</div><div class="meter"><i data-w="${pct}"></i></div></td>
      <td class="num">${s.targets || "-"}</td>
      <td class="nowrap">${fmtDur(s.interval_sec || data.default_interval)}${s.interval_sec ? "" : ` <span class="muted xs">默认</span>`}</td>
      <td class="nowrap">${s.last_restock ? ago(s.last_restock) : `<span class="muted">-</span>`}</td>
      <td class="nowrap">${ago(s.last_check)}</td>
      <td><span class="badge ${cls}" title="${esc(s.last_error || "")}"><span class="dot ${cls}"></span>${label}</span></td></tr>`;
  }).join("");
  applyMeters(body);
  $$("tr.click", body).forEach(tr => tr.addEventListener("click", () => { location.hash = `#/sites/${tr.dataset.id}`; }));
}
async function pageSites(silent) {
  sitesUI.data = await api("GET", "/api/sites");
  if (silent && $("#site-rows")) { renderSiteRows(); return; }
  const n = sitesUI.data.sites.length;
  view.innerHTML = `
    <div class="page-head"><div><h1>监控网址</h1><p>共 ${n} 个网址 · 点击进入详情选择机型和推送对象</p></div>${liveTag()}</div>
    <div class="card"><div class="card-head"><div><h2>添加监控网址</h2><p>粘贴商品分组/列表页（如 /store/xxx 或 cart.php?gid=1）。添加时只记录当前库存，不会推送。</p></div></div>
      <div class="card-body"><form id="add-site" class="inline-form">
        <input class="grow" id="site-url" type="text" inputmode="url" placeholder="https://example.com/store/cloud-server" required>
        <input class="w" id="site-name" type="text" placeholder="显示名称（可选）">
        <button class="btn primary" type="submit">${icon("plus")}<span>读取并添加</span></button>
      </form></div></div>
    <div class="card">
      <div class="toolbar">
        <input type="search" id="site-q" placeholder="搜索名称或网址" value="${esc(sitesUI.q)}">
        <div class="seg" id="site-filter">${[["all", "全部"], ["stock", "有货"], ["error", "异常"], ["idle", "未检查"]]
          .map(([k, t]) => `<button type="button" data-f="${k}" class="${sitesUI.filter === k ? "on" : ""}">${t}</button>`).join("")}</div>
      </div>
      <div class="table-wrap"><table class="wide">
        <thead><tr><th>网址</th><th>有货 / 机型</th><th>推送对象</th><th>检查间隔</th><th>最近补货</th><th>上次检查</th><th>状态</th></tr></thead>
        <tbody id="site-rows"></tbody></table></div>
    </div>`;
  renderSiteRows();
  $("#site-q").addEventListener("input", ev => { sitesUI.q = ev.target.value.trim().toLowerCase(); renderSiteRows(); });
  $$("#site-filter button").forEach(b => b.addEventListener("click", () => {
    sitesUI.filter = b.dataset.f;
    $$("#site-filter button").forEach(x => x.classList.toggle("on", x === b));
    renderSiteRows();
  }));
  $("#add-site").addEventListener("submit", ev => {
    ev.preventDefault();
    busy(ev.submitter, async () => {
      const r = await api("POST", "/api/sites", { url: $("#site-url").value, name: $("#site-name").value });
      toast("已添加，请选择推送对象和机型");
      location.hash = `#/sites/${r.site.id}`;
    });
  });
  if (!silent) startLive(pageSites);
}

// ---------------------------------------------------------------- 检查间隔输入
const PRESETS = [[10, "10 秒"], [30, "30 秒"], [60, "1 分钟"], [300, "5 分钟"], [600, "10 分钟"], [1800, "30 分钟"]];
function intervalField(id, label, value, floor, allowEmpty) {
  return `<label class="field"><span>${label}</span>
    <input id="${id}" type="number" min="${floor}" step="1" value="${value ?? ""}" placeholder="${allowEmpty ? "留空使用全局默认" : ""}">
    <span class="presets">${PRESETS.map(([s, t]) => `<button type="button" class="chip" data-for="${id}" data-sec="${s}">${t}</button>`).join("")}</span>
    <span class="hint" id="${id}-hint"></span></label>`;
}
function bindInterval(id, floor, fallback) {
  const input = $("#" + id), hint = $("#" + id + "-hint");
  const update = () => {
    const raw = input.value.trim();
    const sec = raw === "" ? fallback : Number(raw);
    hint.className = "hint";
    $$(`.chip[data-for="${id}"]`).forEach(c => c.classList.toggle("on", raw !== "" && Number(c.dataset.sec) === sec));
    if (raw === "" && fallback == null) { hint.textContent = ""; return; }
    if (!Number.isInteger(sec) || sec < floor) { hint.textContent = `最少 ${floor} 秒`; hint.classList.add("error"); return; }
    const prefix = raw === "" ? `使用全局默认：每 ${fmtDur(sec)}` : `每 ${fmtDur(sec)} 检查一次`;
    if (sec < 30) { hint.textContent = `${prefix}。间隔很短，请留意商家是否会封 IP。`; hint.classList.add("warn-text"); }
    else hint.textContent = prefix;
  };
  input.addEventListener("input", update);
  $$(`.chip[data-for="${id}"]`).forEach(b => b.addEventListener("click", () => { input.value = b.dataset.sec; update(); input.dispatchEvent(new Event("change", { bubbles: true })); }));
  update();
}

// ---------------------------------------------------------------- 网址详情
async function pageSite(id, silent) {
  const [d, chats] = await Promise.all([api("GET", `/api/sites/${id}`), api("GET", "/api/chats")]);
  const s = d.site;
  const present = d.products.filter(p => p.present);
  const inStock = present.filter(p => p.status === "in_stock").length;
  const attached = new Set(d.targets.map(t => t.chat.chat_id));
  const free = chats.chats.filter(c => !attached.has(c.chat_id));
  const [cls, label] = siteStatus({ ...s, targets: d.targets.length });

  view.innerHTML = `
    <a class="crumb" href="#/sites">${icon("back")}监控网址</a>
    <div class="page-head">
      <div><h1>${esc(s.name)} <span class="badge ${cls}"><span class="dot ${cls}"></span>${label}</span></h1>
        <p><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.url)} ${icon("external")}</a></p></div>
      <div class="actions">${liveTag()}
        <button class="btn" id="btn-check">${icon("refresh")}<span>立即检查</span></button>
        <button class="btn" id="btn-toggle">${icon(s.enabled ? "pause" : "play")}<span>${s.enabled ? "停用" : "启用"}</span></button>
        <button class="btn danger" id="btn-delete">${icon("trash")}<span>删除</span></button>
      </div>
    </div>
    ${s.fail_count ? `<div class="callout">${icon("alert")}<div><b>最近 ${s.fail_count} 次抓取失败</b>${esc(s.last_error)}<div class="xs muted">失败期间检查间隔会自动翻倍（最长 30 分钟），恢复后自动回到设定值。</div></div></div>` : ""}
    <div class="cols">
      <div>
        <div class="card"><div class="card-head"><div><h2>推送对象与机型</h2><p>每个推送对象分别选择机型；没有推送对象的网址不会被检查</p></div></div>
          <div class="card-body"><div id="targets">${d.targets.length ? "" : emptyState("还没有推送对象", "从下方选择一个推送对象开始", "chats")}</div>
          ${free.length ? `<div class="inline-form"><select class="grow" id="new-target">${free.map(c => `<option value="${c.chat_id}">${esc(c.title)} · ${chatType(c.chat_type)} ${c.chat_id}</option>`).join("")}</select>
            <button class="btn" id="btn-add-target">${icon("plus")}<span>添加推送对象</span></button></div>`
          : `<p class="muted sm">${chats.chats.length ? "所有推送对象都已添加。" : "还没有任何推送对象，"}<a href="#/chats">管理推送对象 →</a></p>`}</div></div>
        <div class="card"><div class="card-head"><div><h2>机型库存</h2><p>${inStock} 个有货 / 共 ${present.length} 个机型</p></div></div>
          <div class="card-body flush">${d.products.length ? `<div class="table-wrap"><table class="wide">
            <thead><tr><th>机型</th><th>价格</th><th>库存</th><th>推送信息</th><th>最后出现</th><th></th></tr></thead>
            <tbody>${d.products.map(p => `<tr class="${p.present ? "" : "gone"}">
              <td><div class="cell-title">${esc(p.card.title)}</div>${p.card.title !== p.name ? `<div class="cell-sub">页面名称：${esc(p.name)}</div>` : ""}</td>
              <td class="nowrap">${esc(p.card.price || "-")}</td><td>${stockBadge(p)}</td>
              <td><div class="chips">${p.card.location ? `<span class="badge">${esc(p.card.location)}</span>` : ""}
                ${p.card.lines.map(l => `<span class="badge info">${esc(l)}</span>`).join("")}
                ${!p.card.location && !p.card.lines.length ? `<span class="muted xs">未识别</span>` : ""}
                ${Object.keys(p.meta || {}).length ? `<span class="badge accent">已自定义</span>` : ""}</div></td>
              <td class="nowrap muted">${ago(p.last_seen)}</td>
              <td><div class="actions nowrap"><button class="btn sm p-edit" data-pid="${p.id}">编辑推送</button>
                ${p.order_url ? `<a href="${esc(p.order_url)}" target="_blank" rel="noopener noreferrer" class="btn sm ghost" title="打开下单页">${icon("external")}</a>` : ""}</div></td></tr>`).join("")}</tbody></table></div>`
          : emptyState("没有识别到机型", "", "box")}</div></div>
      </div>
      <div>
        <div class="card"><div class="card-head"><h2>网址设置</h2></div><div class="card-body">
          <dl class="kv">
            <dt>上次检查</dt><dd>${fmtTime(s.last_check)}（${ago(s.last_check)}）</dd>
            <dt>解析方式</dt><dd>${esc(s.parse_method || "-")}</dd>
            <dt>添加时间</dt><dd>${fmtTime(s.created_at, true)}</dd>
          </dl><hr class="sep">
          <label class="field"><span>显示名称</span><input id="f-name" type="text" value="${esc(s.name)}"></label>
          ${intervalField("f-interval", "检查间隔（秒）", s.interval_sec, d.interval_floor, true)}
          <label class="field"><span>页面不显示库存时，访问购物车确认</span>
            <select id="f-probe"><option value="auto" ${s.probe_mode === "auto" ? "selected" : ""}>开启</option><option value="off" ${s.probe_mode === "off" ? "selected" : ""}>关闭</option></select></label>
          <hr class="sep"><div class="cell-title">推送显示</div><p class="xs muted">作用于这个网址下的所有机型；单个机型可在“编辑推送”中覆盖。</p>
          <label class="field"><span>商家名称（用于 #标签）</span><input id="m-merchant" type="text" value="${esc(s.meta.merchant || "")}" placeholder="${esc(d.merchant_default)}"></label>
          <label class="field"><span>名称前缀（🏷 机型名前面的部分）</span><input id="m-prefix" type="text" value="${esc(s.meta.prefix || "")}" placeholder="例如：AndNode-美国云"></label>
          <label class="field"><span>默认优惠码（🎟）</span><input id="m-coupon" type="text" value="${esc(s.meta.coupon || "")}" placeholder="例如：andnode（八折）"></label>
          <label class="field"><span>推广参数（追加到购买链接）</span><input id="m-aff" type="text" value="${esc(s.meta.aff || "")}" placeholder="例如：aff=123"></label>
          <label class="field"><span>额外标签（空格分隔）</span><input id="m-tags" type="text" value="${esc(s.meta.tags || "")}" placeholder="例如：特价 年付"></label>
          <button class="btn primary" id="btn-save-site">${icon("check")}<span>保存设置</span></button>
        </div></div>
        <div class="card"><div class="card-head"><div><h2>补货历史</h2><p>最近 30 条</p></div></div>
          <div class="card-body flush">${d.events.length ? `<ul class="timeline">${d.events.map(e => timelineItem(e, false)).join("")}</ul>` : emptyState("暂无记录", "", "activity")}</div></div>
      </div>
    </div>`;

  const box = $("#targets");
  d.targets.forEach(t => box.appendChild(targetEditor(id, t, present)));
  bindInterval("f-interval", d.interval_floor, d.default_interval);
  ["f-name", "f-interval", "f-probe", "m-merchant", "m-prefix", "m-coupon", "m-aff", "m-tags"]
    .forEach(k => $("#" + k).addEventListener("change", () => { live.dirty = true; }));
  $$(".p-edit").forEach(b => b.addEventListener("click", () => {
    openProductEditor(id, d.products.find(p => p.id === Number(b.dataset.pid)), d);
  }));

  $("#btn-check").addEventListener("click", ev => busy(ev.currentTarget, async () => {
    const r = await api("POST", `/api/sites/${id}/check`);
    if (r.ok) toast(`检查完成：识别 ${r.products} 个机型，${r.events} 个库存变动`); else toast(`检查失败：${r.error}`, "bad");
    live.dirty = false; await pageSite(id, true); live.at = Date.now();
  }));
  $("#btn-toggle").addEventListener("click", ev => busy(ev.currentTarget, async () => {
    await api("PATCH", `/api/sites/${id}`, { enabled: !s.enabled });
    toast(s.enabled ? "已停用，不再检查" : "已启用");
    await pageSite(id, true);
  }));
  $("#btn-delete").addEventListener("click", async ev => {
    const btn = ev.currentTarget;
    if (!await confirmDialog({ title: `删除「${s.name}」？`, text: "该网址的机型、订阅和补货历史都会被删除，无法恢复。", ok: "删除", danger: true })) return;
    busy(btn, async () => { await api("DELETE", `/api/sites/${id}`); toast("已删除"); location.hash = "#/sites"; });
  });
  $("#btn-save-site").addEventListener("click", ev => busy(ev.currentTarget, async () => {
    await api("PATCH", `/api/sites/${id}`, { name: $("#f-name").value, interval_sec: $("#f-interval").value || null, probe_mode: $("#f-probe").value });
    await api("PUT", `/api/sites/${id}/meta`, {
      merchant: $("#m-merchant").value, prefix: $("#m-prefix").value, coupon: $("#m-coupon").value,
      aff: $("#m-aff").value, tags: $("#m-tags").value,
    });
    toast("设置已保存，立即生效");
    live.dirty = false;
    await pageSite(id, true);
  }));
  const addBtn = $("#btn-add-target");
  if (addBtn) addBtn.addEventListener("click", () => {
    const sel = $("#new-target"), chatId = Number(sel.value);
    const chat = free.find(c => c.chat_id === chatId);
    if ($(".empty", box)) box.innerHTML = "";
    box.appendChild(targetEditor(id, { chat, all: false, products: [], keywords: [], unsaved: true }, present));
    sel.querySelector(`option[value="${chatId}"]`).remove();
    if (!sel.options.length) addBtn.closest(".inline-form").remove();
    live.dirty = true;
  });
  if (!silent) startLive(sid => pageSite(id, sid));
}

function targetEditor(siteId, t, products) {
  const el = document.createElement("div");
  el.className = "target";
  const picked = new Set(t.products);
  el.innerHTML = `
    <div class="target-head">
      <div class="target-name"><span class="avatar">${initial(t.chat.title)}</span>
        <div><div class="cell-title">${esc(t.chat.title)} ${t.chat.paused ? `<span class="badge warn">已暂停</span>` : ""} ${t.unsaved ? `<span class="badge accent t-unsaved">未保存</span>` : ""}</div>
        <div class="xs muted">${chatType(t.chat.chat_type)} · ${t.chat.chat_id}</div></div></div>
      <label class="toggle-line"><span class="switch"><input type="checkbox" class="t-all" ${t.all ? "checked" : ""}><span></span></span>全部机型（含以后上新的）</label>
    </div>
    <div class="target-body">
      <div class="pick-grid">${products.map(p => `
        <label class="pick ${picked.has(p.key) || t.all ? "on" : ""} ${t.all ? "disabled" : ""}">
          <input type="checkbox" class="t-prod" value="${esc(p.key)}" ${picked.has(p.key) ? "checked" : ""} ${t.all ? "disabled" : ""}>
          <span class="pick-name" title="${esc(p.name)}">${esc(p.name)}</span>${stockBadge(p)}</label>`).join("") || `<span class="muted">没有机型</span>`}</div>
      <label class="field"><span>关键词（逗号分隔；一个关键词内用空格分隔多个词，需全部包含。以后上架的匹配机型也会提醒）</span>
        <input type="text" class="t-kw" value="${esc(t.keywords.join(", "))}" placeholder="例如：HK 2C, 日本"></label>
      <p class="hint t-kw-preview"></p>
      <div class="actions">
        <button class="btn primary sm t-save">${icon("check")}<span>保存</span></button>
        <button class="btn ghost sm danger t-remove">${icon("x")}<span>移除此推送对象</span></button>
      </div>
    </div>`;
  const all = $(".t-all", el);
  const markDirty = () => {
    live.dirty = true;
    if (!$(".t-unsaved", el)) $(".cell-title", el).insertAdjacentHTML("beforeend", ` <span class="badge accent t-unsaved">未保存</span>`);
  };
  const syncPicks = () => $$(".pick", el).forEach(lb => {
    const cb = $(".t-prod", lb);
    cb.disabled = all.checked;
    lb.classList.toggle("disabled", all.checked);
    lb.classList.toggle("on", all.checked || cb.checked);
  });
  all.addEventListener("change", () => { syncPicks(); markDirty(); });
  $$(".t-prod", el).forEach(cb => cb.addEventListener("change", () => { syncPicks(); markDirty(); }));
  const kwInput = $(".t-kw", el), preview = $(".t-kw-preview", el);
  const keywords = () => kwInput.value.split(/[,，]/).map(k => k.trim()).filter(Boolean);
  const updatePreview = async () => {
    const list = keywords();
    if (!list.length) { preview.textContent = ""; return; }
    const names = new Set();
    for (const k of list) {
      try { (await api("GET", `/api/sites/${siteId}/keyword-preview?q=${encodeURIComponent(k)}`)).matches.forEach(n => names.add(n)); }
      catch (_) { return; }
    }
    preview.textContent = names.size ? `当前匹配：${[...names].join("、")}` : "当前没有匹配的机型，以后上架会提醒";
  };
  kwInput.addEventListener("input", markDirty);
  kwInput.addEventListener("change", updatePreview);
  updatePreview();
  $(".t-save", el).addEventListener("click", async ev => {
    const btn = ev.currentTarget;
    const prods = $$(".t-prod", el).filter(cb => cb.checked).map(cb => cb.value);
    if (!all.checked && !prods.length && !keywords().length &&
        !await confirmDialog({ title: "没有选择任何机型", text: "这个推送对象将收不到这个网址的补货提醒。确定保存？", ok: "仍然保存" })) return;
    busy(btn, async () => {
      await api("PUT", `/api/sites/${siteId}/targets/${t.chat.chat_id}`, { all: all.checked, products: prods, keywords: keywords() });
      t.unsaved = false;
      const tag = $(".t-unsaved", el); if (tag) tag.remove();
      live.dirty = $$(".t-unsaved", view).length > 0;
      toast(`已保存「${t.chat.title}」的订阅`);
    });
  });
  $(".t-remove", el).addEventListener("click", async ev => {
    const btn = ev.currentTarget;
    if (!await confirmDialog({ title: "移除推送对象？", text: `不再向「${t.chat.title}」推送这个网址的消息。`, ok: "移除", danger: true })) return;
    busy(btn, async () => {
      if (!t.unsaved) await api("DELETE", `/api/sites/${siteId}/targets/${t.chat.chat_id}`);
      toast("已移除");
      live.dirty = false;
      await pageSite(siteId, true);
    });
  });
  return el;
}

// ---------------------------------------------------------------- 侧边抽屉
function openDrawer(title, html) {
  $("#drawer-title").textContent = title;
  $("#drawer-body").innerHTML = html;
  $("#drawer").classList.remove("hidden");
  document.body.style.overflow = "hidden";
}
function closeDrawer() {
  $("#drawer").classList.add("hidden");
  $("#drawer-body").innerHTML = "";
  document.body.style.overflow = "";
}
$("#drawer-close").addEventListener("click", closeDrawer);
$("#drawer").addEventListener("click", ev => { if (ev.target.id === "drawer") closeDrawer(); });
document.addEventListener("keydown", ev => {
  if (ev.key === "Escape" && !$("#drawer").classList.contains("hidden") && $("#modal").classList.contains("hidden")) closeDrawer();
});

// ---------------------------------------------------------------- 机型推送内容编辑
function openProductEditor(siteId, p, d) {
  const m = p.meta || {}, det = p.card.detected || {};
  const locOptions = d.locations.map(l => `<option value="${esc(l.code)}" ${m.location === l.code ? "selected" : ""}>${esc(l.text)}（${esc(l.code)}）</option>`).join("");
  const field = (id, label, value, placeholder, hint) => `<label class="field"><span>${label}</span>
    <input id="${id}" type="text" value="${esc(value || "")}" placeholder="${esc(placeholder || "")}">${hint ? `<span class="auto">${hint}</span>` : ""}</label>`;
  openDrawer(`编辑推送内容 · ${p.name}`, `
    <div class="editor">
      <div>
        <div class="callout info">${icon("info")}<div>留空 = 使用自动识别的值；填 <code>-</code> = 这一项不显示。只影响以后发出的消息。</div></div>
        ${field("e-name", "显示名称", m.name, p.name, `完整名称 = 网址设置的名称前缀 + 显示名称，当前：<b>${esc(p.card.title)}</b>`)}
        <label class="field"><span>地区</span><select id="e-location">
          <option value="" ${!m.location || m.location === "auto" ? "selected" : ""}>自动识别${det.location_text ? "（" + esc(det.location_text) + "）" : "（未识别到）"}</option>
          <option value="none" ${m.location === "none" ? "selected" : ""}>不显示</option>${locOptions}</select></label>
        ${field("e-lines", "线路（用 / 分隔）", m.lines, det.lines || "例如：9929/CMIN2", det.lines ? `自动识别：<b>${esc(det.lines)}</b>` : "未识别到线路")}
        ${field("e-specs", "配置", m.specs, det.specs || "例如：2 Core / 2GB / 30GB / 1TB", det.specs ? `自动识别：<b>${esc(det.specs)}</b>` : "未识别到配置")}
        ${field("e-coupon", "优惠码", m.coupon, d.site.meta.coupon || "例如：andnode（八折）", d.site.meta.coupon ? `网址默认：<b>${esc(d.site.meta.coupon)}</b>` : "")}
        ${field("e-buy", "购买链接", m.buy_url, p.order_url || "https://", "留空使用页面上的下单链接（会附加网址设置里的推广参数）")}
        ${field("e-tags", "额外标签（空格分隔）", m.tags, "例如：限量 年付特惠", "")}
        <div class="actions">
          <button class="btn primary" id="e-save">${icon("check")}<span>保存</span></button>
          <button class="btn" id="e-reset">恢复自动识别</button>
          <button class="btn ghost" id="e-cancel">取消</button>
        </div>
      </div>
      <div class="preview-col">
        <div class="spread preview-tabs"><b class="sm">消息预览</b>
          <div class="seg" id="e-state"><button type="button" data-s="restock" class="on">补货</button><button type="button" data-s="soldout">售罄</button><button type="button" data-s="new">上新</button></div></div>
        <div class="tg"><div class="tg-bubble" id="e-preview"></div><div class="tg-meta" id="e-preview-meta"></div></div>
        <p class="xs muted">售罄时会直接修改原来那条补货消息，购买链接加删除线。</p>
      </div>
    </div>`);

  let state = "restock", timer = null;
  const collect = () => ({
    name: $("#e-name").value, location: $("#e-location").value, lines: $("#e-lines").value, specs: $("#e-specs").value,
    coupon: $("#e-coupon").value, buy_url: $("#e-buy").value, tags: $("#e-tags").value,
  });
  const refresh = async () => {
    const box = $("#e-preview");
    if (!box) return;
    try {
      const r = await api("POST", `/api/sites/${siteId}/products/${p.id}/preview`, { state, meta: collect() });
      box.innerHTML = r.html;  // 服务端已转义，只含 <a>/<s> 标签
      $$("a", box).forEach(a => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
      const t = new Date();
      $("#e-preview-meta").textContent = `${pad(t.getHours())}:${pad(t.getMinutes())}`;
    } catch (e) { box.textContent = e.message; }
  };
  const schedule = () => { clearTimeout(timer); timer = setTimeout(refresh, 250); };
  $$("#drawer-body input, #drawer-body select").forEach(el => el.addEventListener("input", schedule));
  $$("#e-state button").forEach(b => b.addEventListener("click", () => {
    state = b.dataset.s;
    $$("#e-state button").forEach(x => x.classList.toggle("on", x === b));
    refresh();
  }));
  $("#e-cancel").addEventListener("click", closeDrawer);
  $("#e-reset").addEventListener("click", () => {
    ["e-name", "e-lines", "e-specs", "e-coupon", "e-buy", "e-tags"].forEach(k => { $("#" + k).value = ""; });
    $("#e-location").value = "";
    refresh();
  });
  $("#e-save").addEventListener("click", ev => busy(ev.currentTarget, async () => {
    await api("PUT", `/api/sites/${siteId}/products/${p.id}/meta`, collect());
    closeDrawer();
    toast("推送内容已保存");
    await pageSite(siteId, true);
  }));
  refresh();
}

// ---------------------------------------------------------------- 推送对象
async function pageChats() {
  const [d, ov] = await Promise.all([api("GET", "/api/chats"), api("GET", "/api/overview")]);
  setBotState(ov.bot);
  const bot = ov.bot.username ? `<b>@${esc(ov.bot.username)}</b>` : "你的机器人";
  const sw = (flag, on) => `<label class="switch"><input type="checkbox" data-flag="${flag}" ${on ? "checked" : ""}><span></span></label>`;
  view.innerHTML = `
    <div class="page-head"><div><h1>推送对象</h1><p>补货消息会发送到这里的私聊、群组或频道</p></div></div>
    <div class="cols">
      <div>
        <div class="card"><div class="card-head"><div><h2>全部推送对象</h2><p>共 ${d.chats.length} 个</p></div></div>
          <div class="card-body flush">${d.chats.length ? `<div class="table-wrap"><table class="wide">
            <thead><tr><th>名称</th><th>网址</th><th>暂停推送</th><th>额外售罄提醒</th><th>上新提醒</th><th></th></tr></thead>
            <tbody>${d.chats.map(c => `<tr data-id="${c.chat_id}">
              <td><div class="target-name"><span class="avatar">${initial(c.title)}</span><div><div class="cell-title">${esc(c.title)}</div>
                <div class="xs muted">${chatType(c.chat_type)} · ${c.chat_id}</div></div></div></td>
              <td class="num">${c.sites}</td>
              <td>${sw("paused", c.paused)}</td><td>${sw("notify_soldout", c.notify_soldout)}</td><td>${sw("notify_new", c.notify_new)}</td>
              <td><div class="actions nowrap"><button class="btn sm c-test">${icon("send")}<span>测试</span></button>
                <button class="btn sm ghost danger c-del" title="删除">${icon("trash")}</button></div></td></tr>`).join("")}</tbody></table></div>`
          : emptyState("还没有推送对象", "按右侧步骤添加", "chats")}</div>
          <div class="card-body"><p class="muted xs">补货消息在售罄后会自动改为“已售罄”。“额外售罄提醒”会再单独发一条售罄消息，默认关闭。</p></div></div>
        <div class="card"><div class="card-head"><div><h2>群发公告</h2><p>发送给所有未暂停的推送对象</p></div></div>
          <div class="card-body"><textarea id="bc-text" placeholder="输入公告内容"></textarea>
          <div class="actions"><button class="btn" id="bc-send">${icon("send")}<span>发送</span></button></div></div></div>
      </div>
      <div>
        <div class="card"><div class="card-head"><h2>如何添加</h2></div><div class="card-body">
          <ol class="steps">
            <li><div><b>推送给自己</b><div class="sm muted">在 Telegram 给 ${bot} 发送 <code>/start</code>，刷新本页即可看到。</div></div></li>
            <li><div><b>推送到群组</b><div class="sm muted">把 ${bot} 拉进群，会自动出现在这里。</div></div></li>
            <li><div><b>推送到频道</b><div class="sm muted">把 ${bot} 设为频道管理员（需要发消息权限）。</div></div></li>
          </ol></div></div>
        <div class="card"><div class="card-head"><div><h2>手动添加</h2><p>机器人必须能访问该会话</p></div></div><div class="card-body">
          <form id="add-chat">
            <label class="field"><span>Chat ID</span><input id="chat-id" type="text" placeholder="123456789 或 -1001234567890" required></label>
            <label class="field"><span>备注名（可选）</span><input id="chat-title" type="text"></label>
            <button class="btn primary" type="submit">${icon("plus")}<span>添加</span></button>
          </form></div></div>
      </div>
    </div>`;

  $("#add-chat").addEventListener("submit", ev => {
    ev.preventDefault();
    busy(ev.submitter, async () => {
      await api("POST", "/api/chats", { chat_id: $("#chat-id").value, title: $("#chat-title").value });
      toast("已添加推送对象");
      await pageChats();
    });
  });
  $$("tr[data-id]").forEach(tr => {
    const id = tr.dataset.id, name = $(".cell-title", tr).textContent;
    $$("input[data-flag]", tr).forEach(cb => cb.addEventListener("change", async () => {
      cb.disabled = true;
      try { await api("PATCH", `/api/chats/${id}`, { [cb.dataset.flag]: cb.checked }); toast("已更新"); }
      catch (e) { cb.checked = !cb.checked; if (e.message !== "未登录") toast(e.message, "bad"); }
      finally { cb.disabled = false; }
    }));
    $(".c-test", tr).addEventListener("click", ev => busy(ev.currentTarget, async () => {
      await api("POST", `/api/chats/${id}/test`);
      toast(`测试消息已发送到「${name}」`);
    }));
    $(".c-del", tr).addEventListener("click", async ev => {
      const btn = ev.currentTarget;
      if (!await confirmDialog({ title: `删除「${name}」？`, text: "它在所有网址上的订阅都会被删除。", ok: "删除", danger: true })) return;
      busy(btn, async () => { await api("DELETE", `/api/chats/${id}`); toast("已删除"); await pageChats(); });
    });
  });
  $("#bc-send").addEventListener("click", async ev => {
    const btn = ev.currentTarget, text = $("#bc-text").value.trim();
    if (!text) { toast("请输入公告内容", "bad"); return; }
    if (!await confirmDialog({ title: "发送公告？", text: "将发送给所有未暂停的推送对象。", ok: "发送" })) return;
    busy(btn, async () => {
      const r = await api("POST", "/api/broadcast", { text });
      toast(`已发送 ${r.sent} / ${r.total}`);
      $("#bc-text").value = "";
    });
  });
}

// ---------------------------------------------------------------- 设置
async function pageSettings() {
  const d = await api("GET", "/api/settings");
  setBotState(d.bot);
  const ro = d.readonly;
  const botStatus = d.bot.running ? `<span class="badge ok"><span class="dot ok"></span>在线</span> @${esc(d.bot.username)}`
    : d.bot.has_token ? `<span class="badge bad"><span class="dot bad"></span>未连接</span> <span class="error sm">${esc(d.bot.error || "")}</span>`
    : `<span class="badge warn"><span class="dot warn"></span>未设置</span>`;
  view.innerHTML = `
    <div class="page-head"><div><h1>设置</h1><p>机器人连接、检查频率与安全</p></div></div>
    <div class="cols">
      <div>
        <div class="card"><div class="card-head"><div><h2>Telegram 机器人</h2><p>只负责推送补货消息</p></div></div><div class="card-body">
          <dl class="kv"><dt>状态</dt><dd>${botStatus}</dd>${d.bot_token_masked ? `<dt>当前 Token</dt><dd><code>${esc(d.bot_token_masked)}</code></dd>` : ""}</dl><hr class="sep">
          <label class="field"><span>${d.bot.has_token ? "更换 Bot Token" : "Bot Token"}</span>
            <input id="s-token" type="password" autocomplete="off" placeholder="123456789:AA..."></label>
          <div class="actions">
            <button class="btn primary" id="s-save-token">${icon("check")}<span>保存并连接</span></button>
            <button class="btn" id="s-test">测试</button>
            ${d.bot.has_token ? `<button class="btn" id="s-reconnect">${icon("refresh")}<span>重新连接</span></button>
              <button class="btn ghost danger" id="s-clear-token">${icon("trash")}<span>清除 Token</span></button>` : ""}
          </div>
          <p class="muted xs">获取方法：Telegram 搜索 @BotFather → /newbot → 按提示起名 → 复制返回的 Token。<br>
          更换或清除只删除本程序保存的 Token；若旧 Token 可能已泄露，请在 @BotFather 发送 /revoke 使其彻底作废。</p>
        </div></div>
        <div class="card"><div class="card-head"><div><h2>检查与推送</h2><p>修改后立即生效</p></div></div><div class="card-body">
          ${intervalField("s-interval", "默认检查间隔（秒）· 单个网址可在详情页单独设置", d.check_interval, d.interval_floor, false)}
          <div class="form-grid">
            <label class="field"><span>同一机型补货推送冷却（秒）</span><input id="s-cooldown" type="number" min="0" value="${d.notify_cooldown}"></label>
            <label class="field"><span>连续失败多少次后提醒</span><input id="s-fail" type="number" min="1" value="${d.fail_alert_threshold}"></label>
          </div>
          <button class="btn primary" id="s-save">${icon("check")}<span>保存</span></button>
          <p class="muted xs">从补货到收到推送，最长约等于一个检查间隔。同一域名下的多个网址，请求之间至少间隔 ${ro.host_min_gap} 秒。</p>
        </div></div>
      </div>
      <div>
        <div class="card" id="update-card"><div class="card-head"><div><h2>版本与更新</h2><p>从 GitHub 获取最新版本</p></div></div>
          <div class="card-body" id="update-body"><div class="skel skel-line"></div><div class="skel skel-line"></div></div></div>
        <div class="card"><div class="card-head"><div><h2>网页访问令牌</h2><p>用于登录本后台</p></div></div><div class="card-body">
          <label class="field"><span>当前访问令牌</span><input id="s-cur-web" type="password" autocomplete="current-password" placeholder="输入当前令牌以确认身份"></label>
          <button class="btn" id="s-rotate">${icon("key")}<span>生成新令牌</span></button>
          <p class="muted xs">更换后旧令牌立即失效，其他已登录的设备全部退出；当前浏览器保持登录。</p>
          <div id="s-new-web" class="newtoken hidden">
            <p><b>新的访问令牌</b><br><span class="xs">只显示这一次，请立即保存</span></p>
            <div class="actions"><code id="s-new-web-val"></code><button class="btn sm" id="s-copy">${icon("copy")}<span>复制</span></button></div>
          </div>
        </div></div>
        <div class="card"><div class="card-head"><div><h2>系统参数</h2><p>修改 .env 后重启生效</p></div></div><div class="card-body">
          <dl class="kv">
            <dt>并发抓取</dt><dd>${ro.concurrency}</dd>
            <dt>同域名请求间隔</dt><dd>${ro.host_min_gap} 秒</dd>
            <dt>请求超时</dt><dd>${ro.request_timeout} 秒</dd>
            <dt>抓取代理</dt><dd>${ro.fetch_proxy ? "已设置" : "未设置"}</dd>
            <dt>时区</dt><dd>${esc(ro.timezone)}</dd>
          </dl></div></div>
      </div>
    </div>`;

  bindInterval("s-interval", d.interval_floor, null);
  renderUpdateCard(false);
  $("#s-test").addEventListener("click", ev => busy(ev.currentTarget, async () => {
    const r = await api("POST", "/api/settings/test-token", { token: $("#s-token").value });
    toast(`Token 有效：@${r.username}`);
  }));
  $("#s-save-token").addEventListener("click", ev => {
    const token = $("#s-token").value.trim();
    if (!token) { toast("请先填写 Token", "bad"); return; }
    busy(ev.currentTarget, async () => {
      const r = await api("PUT", "/api/settings", { bot_token: token });
      setBotState(r.bot);
      toast(r.bot.running ? `已连接 @${r.bot.username}` : `已保存，但连接失败：${r.bot.error}`, r.bot.running ? "ok" : "bad");
      await pageSettings();
    });
  });
  const rc = $("#s-reconnect");
  if (rc) rc.addEventListener("click", ev => busy(ev.currentTarget, async () => {
    const r = await api("POST", "/api/bot/reconnect");
    toast(r.bot.running ? "已重新连接" : `连接失败：${r.bot.error}`, r.bot.running ? "ok" : "bad");
    await pageSettings();
  }));
  const clearBtn = $("#s-clear-token");
  if (clearBtn) clearBtn.addEventListener("click", async ev => {
    const btn = ev.currentTarget;
    if (!await confirmDialog({ title: "清除 Bot Token？", text: "机器人会立即断开，补货消息将无法推送，直到填写新的 Token。", ok: "清除", danger: true })) return;
    busy(btn, async () => {
      const r = await api("DELETE", "/api/settings/bot-token");
      setBotState(r.bot);
      toast("已清除 Bot Token，机器人已断开");
      await pageSettings();
    });
  });
  $("#s-save").addEventListener("click", ev => busy(ev.currentTarget, async () => {
    await api("PUT", "/api/settings", {
      check_interval: $("#s-interval").value, notify_cooldown: $("#s-cooldown").value, fail_alert_threshold: $("#s-fail").value,
    });
    toast("已保存，立即生效");
  }));
  $("#s-rotate").addEventListener("click", async ev => {
    const btn = ev.currentTarget, cur = $("#s-cur-web").value;
    if (!cur) { toast("请先输入当前访问令牌", "bad"); return; }
    if (!await confirmDialog({ title: "更换访问令牌？", text: "旧令牌会立即失效，其他已登录的设备都会退出。", ok: "更换", danger: true })) return;
    busy(btn, async () => {
      const r = await api("POST", "/api/auth/rotate-token", { current_token: cur });
      $("#s-cur-web").value = "";
      $("#s-new-web-val").textContent = r.token;
      $("#s-new-web").classList.remove("hidden");
      toast("已更换，旧令牌已失效");
    });
  });
  $("#s-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("#s-new-web-val").textContent); toast("已复制到剪贴板"); }
    catch (_) {
      const range = document.createRange(); range.selectNodeContents($("#s-new-web-val"));
      const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
      toast("已选中，请按 Ctrl+C 复制");
    }
  });
}

// ---------------------------------------------------------------- 版本与更新
const shortSha = s => (s || "").slice(0, 7);
function setUpdateBadge(info) {
  const el = $("#update-badge");
  const c = info && info.check;
  if (c && c.update_available) {
    const v = c.latest && c.latest.version ? `v${esc(c.latest.version)}` : esc(shortSha(c.latest && c.latest.commit));
    el.innerHTML = `${icon("refresh")}<span>新版本 ${v} 可更新</span>`;
    el.classList.remove("hidden");
  } else el.classList.add("hidden");
}
async function checkUpdateBadge() {
  try { setUpdateBadge(await api("GET", "/api/update")); } catch (_) { /* 静默 */ }
}

async function renderUpdateCard(refresh) {
  const box = $("#update-body");
  if (!box) return;
  let u;
  try { u = await api("GET", "/api/update" + (refresh ? "?refresh=1" : "")); }
  catch (e) { box.innerHTML = `<p class="error sm">${esc(e.message)}</p>`; return; }
  if (!$("#update-body")) return;  // 用户已离开设置页
  setUpdateBadge(u);
  const cur = u.current, c = u.check || {}, st = u.status || {};
  const curLabel = `v${esc(cur.version)}${cur.commit ? ` <code>${esc(shortSha(cur.commit))}</code>` : ""}`;
  let latest;
  if (c.error) latest = `<div class="callout">${icon("alert")}<div>${esc(c.error)}</div></div>`;
  else if (c.update_available) {
    const l = c.latest || {};
    latest = `<div class="callout info">${icon("refresh")}<div><b>发现新版本 ${l.version ? "v" + esc(l.version) : ""} <code>${esc(shortSha(l.commit))}</code></b>
      ${c.ahead_by ? `比当前版本新 ${c.ahead_by} 次提交` : esc(l.subject || "")}</div></div>
      ${c.commits && c.commits.length ? `<ul class="changelog">${c.commits.map(x => `<li><code>${esc(x.commit)}</code><span>${esc(x.subject)}</span>
        <time>${esc((x.date || "").slice(0, 10))}</time></li>`).join("")}</ul>` : ""}`;
  } else latest = `<p class="sm"><span class="badge ok"><span class="dot ok"></span>已是最新版本</span></p>`;

  const running = ["queued", "running"].includes(st.state);
  let stateHtml = "";
  if (running) stateHtml = `<div class="progress"><span class="spin"></span><div><b>${st.state === "queued" ? "已提交更新请求，等待服务器处理…" : esc(st.message || "正在更新…")}</b>
    <div class="xs muted">更新期间程序会重启，网页可能短暂无法访问，完成后会自动刷新。</div></div></div>`;
  else if (st.state === "failed") stateHtml = `<div class="callout">${icon("alert")}<div><b>上次更新失败</b>${esc(st.message || "")}
    ${st.log_tail ? `<pre class="log">${esc(st.log_tail)}</pre>` : ""}</div></div>`;
  else if (st.state === "success" && st.finished) stateHtml = `<p class="xs muted">上次更新：${fmtTime(st.finished)} · ${esc(st.message || "")}</p>`;

  const canUpdate = c.update_available && u.host_available && !running;
  box.innerHTML = `
    <div class="ver-row"><div><div class="xs muted">当前版本</div><div class="ver">${curLabel}</div></div>
      <div class="xs muted">${c.checked_at ? "检查于 " + ago(c.checked_at) : ""}</div></div>
    ${latest}
    ${stateHtml}
    <div class="actions mt">
      <button class="btn" id="u-check">${icon("refresh")}<span>检查更新</span></button>
      ${canUpdate ? `<button class="btn primary" id="u-apply">${icon("play")}<span>立即更新</span></button>` : ""}
    </div>
    ${!u.host_available ? `<p class="muted xs">当前安装方式不支持网页一键更新。请在服务器上运行 <code>vps-restock update</code>（一键安装版），或手动 <code>git pull</code> 后重新构建。</p>` : ""}`;
  $("#u-check").addEventListener("click", ev => busy(ev.currentTarget, () => renderUpdateCard(true)));
  const apply = $("#u-apply");
  if (apply) apply.addEventListener("click", async ev => {
    const btn = ev.currentTarget;
    if (!await confirmDialog({ title: "立即更新？", text: "将从 GitHub 下载最新版本并重新构建，程序会重启 1～3 分钟，期间补货检查会暂停。数据和配置会保留。", ok: "开始更新" })) return;
    busy(btn, async () => {
      await api("POST", "/api/update/apply");
      toast("已提交更新请求");
      await renderUpdateCard(false);  // 显示进度并开始轮询
    });
  });
  if (running) waitForUpdate(cur.commit);
}

let updatePoll = null;
function waitForUpdate(fromCommit) {
  if (updatePoll) return;
  const started = Date.now();
  let sawRestart = false;
  updatePoll = setInterval(async () => {
    if (Date.now() - started > 15 * 60 * 1000) { clearInterval(updatePoll); updatePoll = null; toast("更新等待超时，请在服务器上运行 vps-restock status 查看", "bad"); return; }
    let s;
    try { s = await api("GET", "/api/update/status"); }
    catch (_) { sawRestart = true; setProgressText("程序正在重启…"); return; }  // 重启期间连接失败是正常的
    const st = s.status || {};
    if (st.state === "failed") { clearInterval(updatePoll); updatePoll = null; toast("更新失败：" + (st.message || ""), "bad"); renderUpdateCard(true); return; }
    const changed = s.current.commit && fromCommit && s.current.commit !== fromCommit;
    if (st.state === "success" && (changed || sawRestart || !fromCommit)) {
      clearInterval(updatePoll); updatePoll = null;
      toast(st.message || "更新完成");
      setTimeout(() => location.reload(), 1500);  // 加载新版本的页面文件
      return;
    }
    setProgressText(st.state === "queued" ? "已提交更新请求，等待服务器处理…" : (st.message || "正在更新…"));
  }, 3000);
}
function setProgressText(t) { const b = $(".progress b"); if (b) b.textContent = t; }

// ---------------------------------------------------------------- 启动
(async () => {
  try {
    const d = await api("GET", "/api/overview");
    setBotState(d.bot);
    $("#app").classList.remove("hidden");
    route();
    checkUpdateBadge();
    setInterval(checkUpdateBadge, 30 * 60 * 1000);
  } catch (_) { showLogin(); }
})();
