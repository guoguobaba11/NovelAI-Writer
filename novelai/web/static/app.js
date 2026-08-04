/* NovelAI Writer v0.6 - 作者工作台前端逻辑 */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const STATE = {
 dashboard: null,
 chapters: [],
 events: [],
 threads: [],
 characters: [],
 relationships: [],
 selected: null,
};

// v1.19.26: 全局 fetch timeout (默认 30s, 防后端挂死前端一直转)
const _DEFAULT_TIMEOUT_MS = 30000;
const LLM_TIMEOUT_MS = 600000; // 同步 LLM 端点（抽取/优化/简报）按章调 LLM, 可能跑几分钟
const BOOK_TARGET = 300000; // 全书目标字数（30 章 × 1 万字）
const CHAPTER_TARGET_WORDS = 10000; // 单章 AI 生成默认目标字数

// 获取某章的目标字数（用户可在编辑器"设本章目标"里逐章覆盖）
function _getChapterTargetWords(idx) {
 if (idx && STATE_TARGETS.get(idx)) return STATE_TARGETS.get(idx);
 // 也检查 localStorage 的全局默认
 const global = localStorage.getItem("novelai:default-target-words");
 if (global) return parseInt(global, 10) || CHAPTER_TARGET_WORDS;
 return CHAPTER_TARGET_WORDS;
}
const REGEN_TIMEOUT_MS = 300000; // 单章重新生成轮询超时（5 分钟，超时强停）
const POLL_INTERVAL_MS = 3000; // pipeline 流水线轮询间隔
async function _fetchWithTimeout(url, opts = {}, timeoutMs = _DEFAULT_TIMEOUT_MS) {
 const ac = new AbortController();
 const timer = setTimeout(() => ac.abort(new Error("请求超时 (" + Math.round(timeoutMs/1000) + "s)")), timeoutMs);
 try {
 return await fetch(url, { ...opts, signal: ac.signal });
 } finally {
 clearTimeout(timer);
 }
}

const API = {
 async get(p, timeoutMs) {
 const r = await _fetchWithTimeout("/api" + p, {}, timeoutMs ?? _DEFAULT_TIMEOUT_MS);
 if (!r.ok) throw new Error(await r.text());
 return r.json();
 },
 async post(p, body, timeoutMs) {
 const r = await _fetchWithTimeout("/api" + p, {
 method: "POST",
 headers: {"Content-Type": "application/json"},
 body: body ? JSON.stringify(body) : null,
 }, timeoutMs ?? _DEFAULT_TIMEOUT_MS);
 if (!r.ok) throw new Error(await r.text());
 return r.json();
 },
 // v1.19.26: 补全 PUT/DELETE (之前只有 get/post, 调 API.put/del 会 TypeError)
 async put(p, body) {
 const r = await _fetchWithTimeout("/api" + p, {
 method: "PUT",
 headers: {"Content-Type": "application/json"},
 body: body ? JSON.stringify(body) : null,
 });
 if (!r.ok) throw new Error(await r.text());
 return r.json();
 },
 async del(p) {
 const r = await _fetchWithTimeout("/api" + p, { method: "DELETE" });
 if (!r.ok) throw new Error(await r.text());
 return r.json();
 },
};

const ESC = (s) => s == null ? "" : String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");


// =================== Toast 通知（右上角滑入，2.5s 自动消失）===================
let _toastContainer = null;
function _ensureToastContainer() {
 if (_toastContainer) return _toastContainer;
 _toastContainer = document.createElement("div");
 _toastContainer.className = "toast-container";
 document.body.appendChild(_toastContainer);
 return _toastContainer;
}
/**
 * 显示 toast 通知。
 * @param msg 文本（支持简单 emoji）
 * @param kind success | error | info | warning（决定颜色图标）
 * @param duration 毫秒，默认 2500
 */
function showToast(msg, kind = "success", duration = 2500) {
 const c = _ensureToastContainer();
 // 堆叠上限 4 条: 批量操作连续触发时移除最旧, 防止 toast 刷屏遮挡界面
 while (c.children.length >= 4) c.firstElementChild.remove();
 const icons = { success: "✓", error: "✕", info: "i", warning: "!" };
 const t = document.createElement("div");
 t.className = `toast toast-${kind}`;
 t.innerHTML = `<span class="toast-icon">${icons[kind] || "✓"}</span><span class="toast-msg">${ESC(msg)}</span>`;
 c.appendChild(t);
 // 触发入场动画
 requestAnimationFrame(() => t.classList.add("toast-show"));
 setTimeout(() => {
 t.classList.remove("toast-show");
 setTimeout(() => t.remove(), 300); // 等退场动画
 }, duration);
}

/** 统一错误提示：提供上下文 + 原因 + 建议操作 */
function toastError(ctx, e) {
 let reason = e?.message || String(e);
 // 常见错误的友好翻译
 let hint = "";
 if (reason.includes("timeout") || reason.includes("超时")) {
  hint = "（请求超时，可能是 AI 模型响应慢，请重试）";
 } else if (reason.includes("401") || reason.includes("Authentication") || reason.includes("API key")) {
  hint = "（API Key 无效或过期，请检查 .env 配置）";
 } else if (reason.includes("429") || reason.includes("rate limit")) {
  hint = "（请求频率过高，请等几秒后重试）";
 } else if (reason.includes("Failed to fetch") || reason.includes("Connection")) {
  hint = "（网络连接问题，请检查服务器是否在运行）";
 } else if (reason.includes("500") || reason.includes("Internal Server Error")) {
  hint = "（服务器内部错误，请查看日志了解详情）";
 } else if (reason.includes("404")) {
  hint = "（请求的资源不存在，可能已被删除）";
 }
 showToast(`${ctx}：${reason}${hint}`, "error", 6000);
 addLog("error", `[error] ${ctx} — ${reason}${hint}`);
}


// =================== 焦点管理（弹窗打开时捕获焦点，关闭时恢复到触发元素）===================
const _focusStack = []; // 栈：每打开一个弹窗 push 当前 activeElement，关闭时 pop 并 focus

/** 弹窗打开时调用：记录当前焦点元素（之后关闭时可恢复）。*/
function pushFocus() {
 const el = document.activeElement;
 if (el && el !== document.body) {
 _focusStack.push(el);
 } else {
 _focusStack.push(null);
 }
}

/** 弹窗关闭时调用：恢复到打开前的焦点元素。*/
function popFocus() {
 const el = _focusStack.pop();
 if (el && el.focus) {
 try { el.focus(); } catch (_) {}
 }
}

/** 通用的弹窗关闭：隐藏 + 恢复焦点。传入要隐藏的元素。*/
function closeOverlay(el) {
 if (!el) return;
 el.classList.add("hidden");
 popFocus();
}


// =================== 异步确认弹窗（替代原生 confirm，PyWebView 友好）===================
let _confirmResolve = null;
function _ensureConfirmDialog() {
 let dlg = document.getElementById("app-confirm-dialog");
 if (dlg) return dlg;
 dlg = document.createElement("div");
 dlg.id = "app-confirm-dialog";
 dlg.className = "modal hidden";
 dlg.innerHTML = `
 <div class="modal-mask"></div>
 <div class="modal-content modal-sm">
 <div class="modal-header">
 <h2 id="app-confirm-title">确认</h2>
 </div>
 <div class="modal-body" style="padding:20px 24px">
 <p id="app-confirm-msg" style="font-size:14px;line-height:1.6;color:var(--fg);white-space:pre-wrap"></p>
 </div>
 <div style="display:flex;gap:8px;justify-content:flex-end;padding:0 24px 20px">
 <button class="btn" id="app-confirm-cancel">取消</button>
 <button class="btn primary" id="app-confirm-ok">确定</button>
 </div>
 </div>
 `;
 document.body.appendChild(dlg);
 const mask = dlg.querySelector(".modal-mask");
 const cancelBtn = dlg.querySelector("#app-confirm-cancel");
 const okBtn = dlg.querySelector("#app-confirm-ok");
 const close = (val) => {
 dlg.classList.add("hidden");
 popFocus(); // 恢复焦点到触发元素
 if (_confirmResolve) { _confirmResolve(val); _confirmResolve = null; }
 };
 mask.onclick = () => close(false);
 cancelBtn.onclick = () => close(false);
 okBtn.onclick = () => close(true);
 dlg.addEventListener("keydown", (e) => {
 if (e.key === "Escape") { e.preventDefault(); close(false); }
 if (e.key === "Enter") { e.preventDefault(); close(true); }
 });
 return dlg;
}
/**
 * 异步确认弹窗（替代原生 confirm）。
 * @param msg 确认信息
 * @param title 标题（可选，默认"确认"）
 * @returns Promise<boolean> true=确定, false=取消
 */
function showConfirm(msg, title = "确认") {
 return new Promise(resolve => {
 const dlg = _ensureConfirmDialog();
 dlg.querySelector("#app-confirm-msg").textContent = msg;
 dlg.querySelector("#app-confirm-title").textContent = title;
 _confirmResolve = resolve;
 pushFocus(); // 记住打开前的焦点
 dlg.classList.remove("hidden");
 const okBtn = dlg.querySelector("#app-confirm-ok");
 setTimeout(() => okBtn.focus(), 30);
 });
}


// =================== 工具函数 ===================
function formatRelativeTime(iso) {
 if (!iso) return "";
 const t = new Date(iso);
 if (isNaN(t.getTime())) return "";
 const now = new Date();
 const diffMs = now - t;
 const min = Math.floor(diffMs / 60000);
 if (min < 1) return "刚刚";
 if (min < 60) return `${min} 分钟前`;
 const hr = Math.floor(min / 60);
 if (hr < 24) return `${hr} 小时前`;
 const day = Math.floor(hr / 24);
 if (day < 30) return `${day} 天前`;
 const mon = Math.floor(day / 30);
 if (mon < 12) return `${mon} 个月前`;
 return `${Math.floor(mon / 12)} 年前`;
}


// =================== 状态持久化 ===================
const LS_KEYS = {
 view: "novelai-last-view", // { target, params }
 chapter: "novelai-last-chapter", // { idx, scroll }
};

function saveView(target, params = {}) {
 try { localStorage.setItem(LS_KEYS.view, JSON.stringify({ target, params, t: Date.now() })); } catch (e) {}
}
function loadView() {
 try {
 const raw = localStorage.getItem(LS_KEYS.view);
 if (!raw) return null;
 const obj = JSON.parse(raw);
 // 超过 7 天就回到仪表盘
 if (Date.now() - (obj.t || 0) > 7 * 86400 * 1000) return null;
 return obj;
 } catch (e) { return null; }
}

// v2: per-idx 滚动位置记忆（单槽只能记 1 章，per-idx 能记多章）
// 格式：{ idx: scrollTop, ... }（最近 50 章，LRU 淘汰）
const _SCROLL_LS_KEY = "novelai-scroll-positions";
function saveChapterScroll(idx, scrollTop) {
 try {
 const map = _loadScrollMap();
 map[idx] = scrollTop;
 // LRU：只保留最近 50 章
 const keys = Object.keys(map);
 if (keys.length > 50) delete map[keys[0]];
 localStorage.setItem(_SCROLL_LS_KEY, JSON.stringify(map));
 } catch (e) {}
}
function loadChapterScroll(idx) {
 try {
 const map = _loadScrollMap();
 return map[idx] || 0;
 } catch (e) { return 0; }
}
function _loadScrollMap() {
 try {
 const raw = localStorage.getItem(_SCROLL_LS_KEY);
 return raw ? JSON.parse(raw) : {};
 } catch (e) { return {}; }
}

function saveLastChapter(idx, scrollTop = 0) {
 try { localStorage.setItem(LS_KEYS.chapter, JSON.stringify({ idx, scrollTop, t: Date.now() })); } catch (e) {}
 saveChapterScroll(idx, scrollTop); // 同时存 per-idx
}
function loadLastChapter() {
 try {
 const raw = localStorage.getItem(LS_KEYS.chapter);
 if (!raw) return null;
 const obj = JSON.parse(raw);
 if (Date.now() - (obj.t || 0) > 30 * 86400 * 1000) return null; // 30 天
 return obj;
 } catch (e) { return null; }
}

// =================== 主题 & 专注模式 ===================
// B-新97: 读 CSS 变量值, 用于 echarts 等不能用 var() 的地方. 主题切换时 echarts 重渲会自动拿新值
function getCssVar(name) {
 const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
 // 玻璃态 token 是 rgba 半透明，ECharts/CSS 都用不透明等效色更稳定
 // 叠到深底 #1A1A2E = (26,26,46) 上做 alpha 混合
 if (v.startsWith("rgba")) {
  const m = v.match(/rgba?\(([^)]+)\)/);
  if (m) {
   const parts = m[1].split(",").map(s => parseFloat(s.trim()));
   const r = parts[0], g = parts[1], b = parts[2], a = parts[3] !== undefined ? parts[3] : 1;
   if (a < 1) {
    const isLight = document.documentElement.getAttribute("data-theme") === "light";
    const bg = isLight ? [242, 243, 247] : [26, 26, 46];
    const nr = Math.round(r * a + bg[0] * (1 - a));
    const ng = Math.round(g * a + bg[1] * (1 - a));
    const nb = Math.round(b * a + bg[2] * (1 - a));
    return `rgb(${nr},${ng},${nb})`;
   }
  }
 }
 return v;
}
function loadTheme() {
 const saved = localStorage.getItem("novelai-theme") || "dark";
 document.documentElement.setAttribute("data-theme", saved);
 return saved;
}
function toggleTheme() {
 const cur = document.documentElement.getAttribute("data-theme") || "dark";
 const next = cur === "dark" ? "light" : "dark";
 document.documentElement.setAttribute("data-theme", next);
 localStorage.setItem("novelai-theme", next);
 // B-新96: 切主题后, echarts 图表 inline 颜色不变, 需要重新渲染可见图表
 Object.values(_charts).forEach(c => c && c.resize());
 // 重新触发当前视图的图表渲染 (按 .view 名匹配)
 try {
 const curEl = document.querySelector(".view:not(.hidden)");
 if (!curEl) return;
 const id = curEl.id;
 if (id === "view-dashboard") { if (typeof renderDashboard === "function") renderDashboard(); }
 else if (id === "view-timeline") { if (typeof renderTimeline === "function") renderTimeline(); }
 else if (id === "view-chain") { if (typeof renderChain === "function") renderChain(); }
 else if (id === "view-rhythm") { if (typeof renderRhythm === "function") renderRhythm(); }
 else if (id === "view-network") { if (typeof renderNetwork === "function") renderNetwork(); }
 } catch (e) {
 addLog("warn", `[theme] 图表重渲失败: ${e.message || e}`);
 }
 addLog("info", `[theme] 切换到 ${next === "dark" ? "深色" : "浅色"} 主题`);
}

function toggleFocusMode() {
 const on = document.body.classList.toggle("focus-mode");
 localStorage.setItem("novelai-focus", on ? "1" : "0");
 if (on && CURRENT.target !== "editor") {
 goto("editor");
 }
 addLog("info", `[focus] 专注模式 ${on ? "开启" : "关闭"}`);
}

function loadFocusMode() {
 if (localStorage.getItem("novelai-focus") === "1") {
 document.body.classList.add("focus-mode");
 }
}

// v1.19.23: 左/右栏开合状态持久化 (用户的工作流习惯, 跨刷新保留)
function loadEditorPanels() {
 try {
 const left = localStorage.getItem("ed.leftOpen");
 const right = localStorage.getItem("ed.rightCollapsed");
 if (left === "1") document.body.classList.add("left-open");
 if (right === "1") document.body.classList.add("right-collapsed", "editor-compact");
 } catch (e) {}
}
function syncEditorPanelButtons() {
 // 把 body class 反映回顶条按钮的 active 视觉
 const tl = document.getElementById("ed-toggle-left");
 const tr = document.getElementById("ed-toggle-right");
 if (tl) tl.classList.toggle("active", document.body.classList.contains("left-open"));
 if (tr) tr.classList.toggle("active", !document.body.classList.contains("editor-compact"));
}

// =================== 路由 ===================
// target → 渲染函数
const ROUTES = {
 dashboard: renderDashboard,
 import: renderImport,
 "new-novel": renderNewNovel,
 chapters: renderChapters,
 characters: renderCharacters,
 mbti: renderMbti,
 events: renderEvents,
 "ai-extract": renderAIExtract,
 structure: renderStructure,
 pipeline: renderPipeline,
 editor: renderEditor,
 scan: renderScanAll,
 threadscan: renderThreadScan,
 logicscan: renderLogicScan,
 stylescan: renderStyleScan,
 driftscan: renderDriftScan,
 optimize: renderOptimizeView,
 "opt-all": renderOptAll,
 "opt-personality": renderOptPersonality,
 "opt-arc": renderOptArc,
 "opt-relationship": renderOptRelationship,
 timeline: renderTimeline,
 chain: renderChain,
 rhythm: renderRhythm,
 matrix: renderMatrix,
 arcs: renderArcs,
 relcurve: renderRelCurve,
 network: renderNetwork,
 knowledge_graph: renderKnowledgeGraph,
};

const CURRENT = { target: "dashboard", params: {} };

function goto(target, params = {}) {
 // v1.19.24: 切走非 editor 时, 取消进行中的 AI 流式读取 (省 token + 防止回 editor 时新内容污染)
 if (CURRENT.target === "editor" && target !== "editor" && window._aiEditAbortController) {
 try { window._aiEditAbortController.abort(); } catch (_) {}
 window._aiEditAbortController = null;
 _aiStreaming = false;
 addLog("info", "[ai] 切走编辑器, 已中断 AI 流");
 }
 // v1.19.26: 切走非 pipeline 时, 也停 pipeline 轮询 (否则切到 editor 还在每 3s 调一次 /pipeline/last)
 if (CURRENT.target === "pipeline" && target !== "pipeline" && typeof _pipelinePolling !== "undefined" && _pipelinePolling) {
 clearInterval(_pipelinePolling);
 _pipelinePolling = null;
 addLog("info", "[pipeline] 切走 pipeline 视图, 已停止轮询");
 }
 // 切走时隐藏选区浮动按钮
 if (typeof hideSelAIButton === "function") hideSelAIButton();

 CURRENT.target = target;
 CURRENT.params = params;
 $$(".wf-item").forEach(b => b.classList.toggle("wf-active", b.dataset.target === target));
 // B-新159: 顶栏工具 menu 同步高亮 + 自动关闭
 const toolsMenu = $("#tools-menu");
 if (toolsMenu) {
 $$(".topbar-menu-item", toolsMenu).forEach(item => {
 item.classList.toggle("topbar-menu-active", item.dataset.target === target);
 });
 }
 // 显示对应视图
 $$(".view").forEach(v => v.classList.add("hidden"));
 // v1.19.23: editor / tool 视图独占 #main, 隐藏 dashboard 的 sidebar + detail, 腾出空间
 const _detail = $("#detail");
 const _sidebar = $("#sidebar");
 const _mainEl = $("main");
 if (target === "dashboard") {
 $("#view-dashboard").classList.remove("hidden");
 if (_detail) _detail.style.display = "";
 if (_sidebar) _sidebar.style.display = "";
 if (_mainEl) _mainEl.style.gridTemplateColumns = "200px 1fr 240px";
 } else if (target === "editor") {
 $("#view-editor").classList.remove("hidden");
 if (_detail) _detail.style.display = "none";
 if (_sidebar) _sidebar.style.display = "none";
 if (_mainEl) _mainEl.style.gridTemplateColumns = "1fr";
 } else {
 $("#view-tool").classList.remove("hidden");
 if (_detail) _detail.style.display = "none";
 if (_sidebar) _sidebar.style.display = "none";
 if (_mainEl) _mainEl.style.gridTemplateColumns = "1fr";
 }
 // 调用渲染
 const fn = ROUTES[target];
 if (fn) {
 try {
 fn(params);
 } catch (e) {
 console.error(e);
 $("#tool-body").innerHTML = `<p class="placeholder">加载失败: ${ESC(e.message || e)}</p>`;
 }
 } else {
 // B-新155: 未知 target 给用户提示, 不静默
 $("#tool-body").innerHTML = `<p class="placeholder">未找到路由: ${ESC(target)}</p>`;
 addLog("warn", `[goto] 未找到路由: ${target}`);
 }
 // 持久化：跳过 editor 视图（避免和 last-chapter 冲突）
 if (target !== "editor") {
 saveView(target, params);
 }
}

// 顶部 / 底部 / onboarding
$$(".wf-item").forEach(btn => {
 btn.onclick = () => {
 const target = btn.dataset.target;
 if (target) goto(target);
 };
 // 箭头键导航：Up/Down 在 wf-item 间移动焦点（键盘可访问性，交互审计 #6）
 btn.addEventListener("keydown", (e) => {
 const items = Array.from($$(".wf-item"));
 const cur = items.indexOf(btn);
 if (cur < 0) return;
 if (e.key === "ArrowDown") {
 e.preventDefault();
 const next = items[(cur + 1) % items.length];
 if (next) next.focus();
 } else if (e.key === "ArrowUp") {
 e.preventDefault();
 const prev = items[(cur - 1 + items.length) % items.length];
 if (prev) prev.focus();
 }
 });
});

// 折叠"所有功能"区 (B-新166: 已移除, 21 项其它工具移到顶栏 菜单)
const wfToggle = $("#wf-toggle-others");
if (wfToggle) {
 // 兼容旧 localStorage 状态, 清掉不再用
 try { localStorage.removeItem("novelai-wf-others-open"); } catch (e) {}
}

// =================== 顶部 ===================
function renderTopbar() {
 const d = STATE.dashboard;
 if (!d) return;
 $("#project-title").textContent = d.project.title || "(无标题)";
 // B-新187: story_time_unit 兜底, 防 null 显示
 $("#project-meta").textContent = `${d.project.pov_mode || "限知"} · ${d.project.volumes}卷 · 时间单位：${d.project.story_time_unit || "回"}`;
 const k = d.kpis;
 // B-优1: kpi-total / kpi-written 已删除, 只剩 kpi-words
 const wEl = $("#kpi-words");
 if (wEl) wEl.textContent = (k.words_total || 0).toLocaleString();
 // 健康度
 const statusEl = $("#kpi-status");
 if (statusEl) {
 statusEl.classList.remove("green", "yellow", "red");
 const healthClass = d.health.overall;
 if (healthClass) statusEl.classList.add(healthClass);
 }
 const healthText = {
 green: "健康",
 yellow: "注意",
 red: "严重",
 }[d.health.overall] || "检测中";
 if (statusEl) {
 const valEl = statusEl.querySelector(".kpi-value");
 if (valEl) valEl.textContent = healthText;
 statusEl.onclick = () => goto("pipeline"); // B-优2: 健康度点击直跳 pipeline
 }
 // 仪表盘 tag
 const tag = $("#wf-tag-dashboard");
 if (d.health.high_issues > 0) {
 tag.textContent = `${d.health.high_issues} 个高优问题`;
 tag.style.display = "";
 } else {
 tag.style.display = "none";
 }
}

function setStatus(text, kind) {
 const el = $("#kpi-status");
 if (!el) return;
 el.classList.remove("busy", "error");
 if (kind === "busy") el.classList.add("busy");
 if (kind === "error") el.classList.add("error");
 const v = el.querySelector(".kpi-value");
 if (v) v.textContent = text;
}

// =================== 仪表盘 ===================
async function renderDashboard(opts = {}) {
 // silent 模式（15s 轮询 / WS 触发的后台刷新）: 不闪"加载中", 保留滚动位置,
 // 不劫持右栏详情面板（用户可能正在看别的人物/章节）。opts.data 可复用已拉取的数据, 避免重复请求。
 const silent = !!opts.silent;
 const cont = $("#dashboard-content");
 if (!cont) return;
 const view = $("#view-dashboard");
 const prevScroll = silent && view ? view.scrollTop : 0;
 if (!silent) cont.innerHTML = '<p class="placeholder loading">加载中…</p>';
 try {
 const d = opts.data || await API.get("/dashboard");
 STATE.dashboard = d;
 renderTopbar();
 // P3-G: 后端漏返 kpis / health 不再抛
 const k = d.kpis || {};
 const h = d.health || {};
 const healthClass = h.overall;
 const healthLabel = {green:"健康", yellow:"注意", red:"严重"}[h.overall];
 let html = "";

 if (!d.onboarding_done) {
 // 新用户：欢迎卡
 html += `
 <div class="welcome-card">
 <h2> 欢迎来到作者工作台</h2>
 <p>你的项目还没数据。先做下面 4 步：</p>
 <div class="welcome-steps">
 <div class="welcome-step" onclick="goto('import')">
 <div class="ws-num">第 1 步</div>
 <div class="ws-title"> 导入手稿</div>
 <div class="ws-desc">把你的 .md 文件导入数据库</div>
 </div>
 <div class="welcome-step" onclick="goto('mbti')">
 <div class="ws-num">第 2 步</div>
 <div class="ws-title"> 标注 MBTI</div>
 <div class="ws-desc">给主要人物标 16 型人格</div>
 </div>
 <div class="welcome-step" onclick="goto('scan')">
 <div class="ws-num">第 3 步</div>
 <div class="ws-title"> 跑全本扫描</div>
 <div class="ws-desc">找出伏笔 / 逻辑 / 文风问题</div>
 </div>
 <div class="welcome-step" onclick="goto('opt-all')">
 <div class="ws-num">第 4 步</div>
 <div class="ws-title"> AI 优化建议</div>
 <div class="ws-desc">让 LLM 给出可执行修改方向</div>
 </div>
 </div>
 <div style="margin-top:18px;display:flex;gap:10px;justify-content:center;flex-wrap:wrap">
 <button class="btn primary" onclick="goto('new-novel')" style="background:linear-gradient(135deg, var(--accent), color-mix(in srgb, var(--accent) 70%, var(--bg-base)));color:var(--accent-text);font-weight:600;padding:10px 24px;font-size:14px">
 开始写新小说（AI 辅助创作）
 </button>
 <button class="btn" onclick="loadSampleProject()"> 加载示例项目</button>
 <button class="btn" onclick="goto('import')"> 导入 .md 手稿</button>
 </div>
 <div style="margin-top:10px;font-size:11px;color:var(--fg-dim)"> 示例项目：3 章"长安遗事"+ 4 角色 + 5 事件 + 已跑一致性</div>
 </div>
 `;
 } else {
 // ===== 老用户：杂志型仪表盘 (v1.19.20) =====

 // 1. Banner 大封面 (项目名 + POV + 元数据 + 主CTA)
 const last = d.recent_chapters?.[0];
 const updatedAgo = last ? formatRelativeTime(last.updated_at) : "";
 html += `
 <div class="dash-banner">
 <div class="dash-banner-grad"></div>
 <div class="dash-banner-body">
 <div class="dash-banner-meta">${ESC(d.project.pov_mode || "限知")} · ${d.project.volumes || 0}卷 · ${d.project.story_time_unit || "回"}</div>
 <h1 class="dash-banner-title">${ESC(d.project.title || "(无标题)")}</h1>
 ${d.project.synopsis ? `<div class="dash-banner-synopsis">${ESC(d.project.synopsis.slice(0, 140))}${d.project.synopsis.length > 140 ? "…" : ""}</div>` : ""}
 ${d.project.style ? `<div class="dash-banner-style">文风 · ${ESC(d.project.style)}</div>` : ""}
 <div class="dash-banner-actions">
 ${last ? `<button class="btn primary dash-banner-cta" id="btn-continue-edit"> 继续编辑第 ${last.idx} 回 · ${ESC(last.title || "")}</button>` : ""}
 <button class="btn primary" onclick="goto('new-novel')" style="background:var(--accent);color:var(--accent-text)">+ 写新小说</button>
 <button class="btn" onclick="goto('scan')"> 跑全本扫描</button>
 <button class="btn" onclick="goto('chapters')"> 项目页</button>
 </div>
 </div>
 </div>
 `;

 // 2. KPI 一行 (粘滞条风格, 6 个 chip)
 html += `
 <div class="dash-strip">
 <div class="dash-strip-item"><span class="dsi-ico"></span><span class="dsi-num">${k.chapters_written ?? 0}<small>/${k.chapters_total ?? 0}</small></span><span class="dsi-label">章</span></div>
 <div class="dash-strip-sep"></div>
 <div class="dash-strip-item"><span class="dsi-ico"></span><span class="dsi-num">${(k.words_total || 0).toLocaleString()}</span><span class="dsi-label">字</span></div>
 <div class="dash-strip-sep"></div>
 <div class="dash-strip-item"><span class="dsi-ico"></span><span class="dsi-num">${k.characters ?? 0}</span><span class="dsi-label">人物</span></div>
 <div class="dash-strip-sep"></div>
 <div class="dash-strip-item"><span class="dsi-ico"></span><span class="dsi-num">${k.events ?? 0}</span><span class="dsi-label">事件</span></div>
 <div class="dash-strip-sep"></div>
 <div class="dash-strip-item"><span class="dsi-ico"></span><span class="dsi-num">${k.threads_total ?? 0}</span><span class="dsi-label">伏笔</span></div>
 <div class="dash-strip-sep"></div>
 <div class="dash-strip-item"><span class="dsi-ico"></span><span class="dsi-num">${k.volumes ?? 0}</span><span class="dsi-label">卷</span></div>
 </div>
 `;

 // 3. 三栏: 待处理 / 健康度 / 进度
 const totalIssues = (h.high_issues || 0) + (h.thread_issues || 0) + (h.logic_issues || 0) + (h.style_issues || 0) + (h.drift_signals || 0);
 const progressPct = Math.min(100, Math.round(((k.words_total || 0) / BOOK_TARGET) * 100));
 html += `<div class="dash-trio">`;

 html += `<div class="dash-card dash-mini">
 <div class="card-title">! 待处理</div>
 <div class="dash-mini-num ${h.high_issues > 0 ? "red" : ""}">${totalIssues}<small> 个问题</small></div>
 <div class="dash-mini-break">
 ${h.thread_issues > 0 ? `<span class="dm-chip yellow">${h.thread_issues}</span>` : ""}
 ${h.logic_issues > 0 ? `<span class="dm-chip yellow">${h.logic_issues}</span>` : ""}
 ${h.style_issues > 0 ? `<span class="dm-chip yellow">${h.style_issues}</span>` : ""}
 ${h.drift_signals > 0 ? `<span class="dm-chip yellow">${h.drift_signals}</span>` : ""}
 ${totalIssues === 0 ? `<span class="dm-chip green">✓ 无</span>` : ""}
 </div>
 <button class="btn small dash-mini-go" onclick="goto('scan')">跳扫描问题 →</button>
 </div>`;

 html += `<div class="dash-card dash-mini">
 <div class="card-title"> 健康度</div>
 <div class="dash-mini-num ${healthClass}">${healthLabel}</div>
 <div class="dash-mini-break" style="font-size:11px;color:var(--fg-muted);line-height:1.5">
 ${h.high_issues > 0 ? `${h.high_issues} 个高优需处理` : "无高优阻塞"}
 ${h.logic_issues > 0 ? `<br>逻辑链 ${h.logic_issues} 处待查` : ""}
 </div>
 <button class="btn small dash-mini-go" onclick="goto('pipeline')">跳修改流水线 →</button>
 </div>`;

 html += `<div class="dash-card dash-mini">
 <div class="card-title"> 总进度</div>
 <div class="dash-progress-bar"><div class="dash-progress-fill" style="width:${progressPct}%"></div></div>
 <div class="dash-mini-break" style="font-size:11px;color:var(--fg-muted)">
 ${(k.words_total || 0).toLocaleString()} / ${BOOK_TARGET.toLocaleString()} 字 · ${progressPct}%
 </div>
 <button class="btn small dash-mini-go" onclick="showReviewModal()">跳审稿看板 →</button>
 </div>`;

 html += `</div>`;

 // 4. 双栏 footer: 最近改过 (5 行) + 活跃伏笔 (top 5)
 const recent = (d.recent_chapters || []).slice(0, 5);
 html += `<div class="dash-pair">`;

 html += `<div class="dash-card">
 <div class="card-title" style="display:flex;justify-content:space-between;align-items:center">
 <span> 最近改过</span>
 <div style="display:flex;gap:6px">
 <button class="btn small" id="btn-export-all" title="导出 Word 完整版"> .docx</button>
 <button class="btn small" id="btn-export-all-md" title="导出 Markdown 备份"> .md</button>
 </div>
 </div>
 <div class="recent-list">`;
 if (!recent.length) {
 html += `<div class="todo-empty">还没有任何章节</div>`;
 } else {
 for (const c of recent) {
 html += `<div class="recent-item" data-chapter-idx="${c.idx}">
 <span class="ri-idx">第 ${c.idx} 回</span>
 <span class="ri-title">${ESC(c.title || "")}</span>
 <span class="ri-meta">${(c.word_count || 0).toLocaleString()} 字 · ${formatRelativeTime(c.updated_at)}</span>
 </div>`;
 }
 }
 html += `</div></div>`;

 // 4b. 活跃伏笔 top5
 let activeThreads = [];
 try {
 const tData = await API.get("/scan/threads");
 activeThreads = (tData.issues || tData.threads || []).slice(0, 5);
 } catch (e) { /* ignore */ }
 html += `<div class="dash-card">
 <div class="card-title"> 活跃伏笔 · Top 5</div>
 <div class="thread-list">`;
 if (!activeThreads.length) {
 html += `<div class="todo-empty">暂无伏笔数据 · 跑扫描查看</div>`;
 } else {
 for (const t of activeThreads) {
 html += `<div class="thread-item">
 <span class="ti-tag ${t.severity || ""}">${ESC(t.status || "active")}</span>
 <span class="ti-title">${ESC(t.title || t.description || t.summary || "(无描述)")}</span>
 </div>`;
 }
 }
 html += `</div></div>`;

 html += `</div>`;
 }

 cont.innerHTML = html;
 if (silent && view) view.scrollTop = prevScroll; // 后台刷新不打扰阅读位置

 // 绑定"继续修改"和"最近章节"点击
 const continueBtn = $("#btn-continue-edit");
 if (continueBtn) {
 continueBtn.onclick = () => {
 const last = STATE.dashboard?.recent_chapters?.[0];
 if (last) {
 const lidx = last.idx ?? last.chapter_idx ?? last.id;
 STATE_EDITOR.chapterIdx = lidx;
 if (lidx) saveLastChapter(lidx, 0);
 goto("editor");
 }
 };
 }
 $$(".recent-item").forEach(el => {
 el.onclick = () => {
 const idx = parseInt(el.dataset.chapterIdx, 10);
 if (!isNaN(idx)) {
 STATE_EDITOR.chapterIdx = idx; // v1.19.26
 saveLastChapter(idx, 0);
 goto("editor");
 }
 };
 });

 // 健康度 KPI 可点击（仅首次绑定，避免 15s 轮询重复绑定）
 const statusKpi = $("#kpi-status");
 if (statusKpi && !statusKpi._kpiBound) {
 statusKpi._kpiBound = true;
 statusKpi.onclick = () => goto("pipeline");
 statusKpi.style.cursor = "pointer";
 }

 // "全本导出"按钮在重建的 DOM 里, 每次渲染都要重新绑定
 const exportBtn = $("#btn-export-all");
 if (exportBtn) exportBtn.onclick = exportAllDocx;
 const exportMdBtn = $("#btn-export-all-md");
 if (exportMdBtn) exportMdBtn.onclick = exportAllMd;

 // 自动显示"最近章节"详情到右侧（让用户立即看到详情面板的价值）
 // silent 后台刷新时跳过: 用户可能正在右栏看别的人物/章节, 不能劫持
 if (!silent) {
 if (d.recent_chapters && d.recent_chapters.length > 0) {
 const lastIdx = d.recent_chapters[0].idx;
 showChapterDetail(lastIdx);
 } else {
 // 没数据时给一个有用的引导
 $("#detail-body").innerHTML = `
 <div class="placeholder" style="padding:30px 16px;text-align:center">
 <div style="font-size:32px;margin-bottom:10px"></div>
 <div style="font-size:13px;color:var(--fg-muted);line-height:1.7">
 点击左侧 <b>章节 / 人物</b> 列表的任一行，<br>
 详细信息会显示在这里。
 </div>
 </div>
 `;
 }
 }
 } catch (e) {
 if (silent) {
 // 后台刷新失败: 保留旧内容, 只记日志（下次轮询会再试）
 addLog("warn", `[dashboard] 静默刷新失败: ${e.message || e}`);
 } else {
 cont.innerHTML = `<p class="placeholder">加载失败: ${ESC(e.message || e)}</p>`;
 }
 }
}

// =================== 通用工具：tool-header ===================
function setToolHeader(title, desc) {
 $("#tool-header").innerHTML = `
 <button class="tool-back-btn" onclick="goto('dashboard')" title="返回仪表盘">
 <span class="tbb-arrow">‹</span> 返回
 </button>
 <div class="tool-title">${title}</div>
 <div class="tool-desc">${desc}</div>
 `;
}
function setToolBody(html) {
 $("#tool-body").innerHTML = html;
}

// =================== 新建小说向导（AI 辅助创作） ===================
const STYLE_PRESETS = {
 "玄幻": "热血升级、金手指、境界突破、宗门争斗；节奏明快，爽点密集，章末留悬念",
 "都市": "现代背景、商战/职场/情感纠葛；对话生活化，节奏中等，贴近现实",
 "科幻": "硬科幻设定、科技冲突、文明博弈；理性冷静，逻辑严密，宏大叙事",
 "古言": "古风雅致、权谋宫斗、情感细腻；半文半白，节奏沉稳，氛围浓厚",
 "悬疑": "层层反转、线索铺排、心理博弈；节奏紧凑，伏笔密集，章末钩子",
 "自定义": "",
};

function renderNewNovel() {
 setToolHeader("开始写新小说", "AI 辅助创作：设项目 → 生成大纲 → 写正文。");
 const p = STATE.project || {};
 const curStyle = p.style || "";
 const presetMatch = Object.entries(STYLE_PRESETS).find(([k,v]) => v && v === curStyle);
 const presetSel = presetMatch ? presetMatch[0] : "自定义";
 const presetOpts = Object.keys(STYLE_PRESETS).map(k => `<option value="${k}" ${k===presetSel?"selected":""}>${k}</option>`).join("");
 const draft = _nnReadDraft(); // localStorage 草稿回填
 const targetCh = draft.chapters || "20";
 const wordsSel = _nnWordsSel(); // 当前每章目标字数档位(用于高亮)
 _nnStep = 1;
 _nnOutlineData = null; // 清上次残留，避免第 2 步面板空转
 setToolBody(`
 <div style="max-width:720px;margin:0 auto">
 <div class="nn-stepper" id="nn-stepper">
 <div class="nn-step" data-step="1"><span class="nn-step-num">1</span><span class="nn-step-label">项目设定</span></div>
 <div class="nn-step-sep"></div>
 <div class="nn-step" data-step="2"><span class="nn-step-num">2</span><span class="nn-step-label">生成大纲</span></div>
 <div class="nn-step-sep"></div>
 <div class="nn-step" data-step="3"><span class="nn-step-num">3</span><span class="nn-step-label">写正文</span></div>
 </div>

 <div class="nn-panel" id="nn-step-1">
 <div class="detail-section">
 <h4>项目设定</h4>
 <div class="nn-field">
 <label class="nn-label">书名</label>
 <input type="text" id="nn-title" class="nn-input" value="${ESC(draft.title ?? p.title ?? "")}" placeholder="给你的小说起个名字">
 </div>
 <div class="nn-field">
 <label class="nn-label">梗概（越详细 AI 生成越精准）</label>
 <textarea id="nn-synopsis" class="nn-input" rows="4" placeholder="一句话或几句话描述你的故事核心：主角是谁、想要什么、障碍是什么、世界观背景…">${ESC(draft.synopsis ?? p.synopsis ?? "")}</textarea>
 </div>
 <div style="display:flex;gap:12px;flex-wrap:wrap">
 <div class="nn-field" style="flex:1;min-width:160px">
 <label class="nn-label">文风预设</label>
 <select id="nn-preset" class="nn-input">${presetOpts}</select>
 </div>
 <div class="nn-field" style="flex:1;min-width:160px">
 <label class="nn-label">视角</label>
 <select id="nn-pov" class="nn-input">
 <option value="限知视角" ${(draft.pov ?? p.pov_mode) === "限知视角" ? "selected" : ""}>限知视角</option>
 <option value="全知视角" ${(draft.pov ?? p.pov_mode) === "全知视角" ? "selected" : ""}>全知视角</option>
 </select>
 </div>
 <div class="nn-field" style="flex:1;min-width:120px">
 <label class="nn-label">时间单位</label>
 <select id="nn-unit" class="nn-input">
 ${["回","章","日","月","年","节"].map(u => `<option value="${u}" ${(draft.unit ?? p.story_time_unit) === u ? "selected" : ""}>${u}</option>`).join("")}
 </select>
 </div>
 </div>
 <div id="nn-style-wrap" class="nn-field" ${(draft.preset ?? presetSel) === "自定义" ? "" : "style='display:none'"}>
 <label class="nn-label">自定义文风描述</label>
 <textarea id="nn-style" class="nn-input" rows="2" placeholder="描述你想要的文风…">${ESC(draft.style ?? curStyle)}</textarea>
 </div>
 <div class="nn-field">
 <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:var(--fg-muted)">
 <input type="checkbox" id="nn-reset" style="accent-color:var(--accent)"> 清除当前所有数据，从零开始写新书（保留人物设定）
 </label>
 </div>
 <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
 <button class="btn primary" id="nn-save-project">保存并下一步 →</button>
 <span id="nn-save-status" style="color:var(--fg-dim);font-size:11px"></span>
 </div>
 </div>
 </div>

 <div class="nn-panel" id="nn-step-2">
 <div class="detail-section">
 <h4>生成大纲</h4>
 <div class="nn-field">
 <label class="nn-label">预计规模</label>
 <div class="nn-seg-group" id="nn-scale-group">
 ${[[10,"短篇"],[50,"中篇"],[100,"长篇"],[200,"超长篇"]].map(([n,label]) => `<button type="button" class="nn-seg${parseInt(targetCh)===n?" nn-on":""}" data-chapters="${n}">${label} ${n}章</button>`).join("")}
 </div>
 </div>
 <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
 <label class="nn-label" style="margin-bottom:0">目标章节数</label>
 <input type="number" id="nn-target-chapters" class="nn-input" value="${ESC(targetCh)}" min="3" max="500" style="width:90px;margin-bottom:0">
 <button class="btn primary" id="nn-gen-outline">生成大纲</button>
 <span id="nn-outline-status" style="color:var(--fg-dim);font-size:11px"></span>
 </div>
 <div id="nn-outline-result" style="margin-top:12px"></div>
 </div>
 </div>

 <div class="nn-panel" id="nn-step-3">
 <div class="detail-section">
 <h4>写正文</h4>
 <div class="nn-field">
 <label class="nn-label">每章目标字数</label>
 <div class="nn-seg-group" id="nn-words-group">
 ${[[2000,"2千"],[5000,"5千"],[10000,"1万"],[20000,"2万"]].map(([n,label]) => `<button type="button" class="nn-seg${wordsSel===n?" nn-on":""}" data-words="${n}">${label}字</button>`).join("")}
 <span style="font-size:11px;color:var(--fg-dim);align-self:center">写章时自动生效，也可在编辑器里单独改</span>
 </div>
 </div>
 <div id="nn-write-section" style="color:var(--fg-dim);font-size:12px">先完成第 1、2 步（保存项目 + 生成大纲）。</div>
 </div>
 </div>
 </div>
 `);

 _nnRenderStep();
 // 步骤条：点击已完成步骤回看
 document.querySelectorAll("#nn-stepper .nn-step").forEach(step => {
 step.onclick = () => {
 const s = parseInt(step.dataset.step);
 if (s < _nnStep) { _nnStep = s; _nnRenderStep(); }
 };
 });
 // 文风预设切换
 $("#nn-preset").onchange = (e) => {
 $("#nn-style-wrap").style.display = e.target.value === "自定义" ? "" : "none";
 _nnScheduleDraft();
 };
 // 草稿：所有 nn-* 输入变更即存
 ["nn-title","nn-synopsis","nn-pov","nn-unit","nn-style","nn-target-chapters"].forEach(id => {
 const el = document.getElementById(id);
 if (el) el.addEventListener("input", _nnScheduleDraft);
 });
 $("#nn-reset").onchange = _nnScheduleDraft;
 // 规模分段：点击填章节数
 document.querySelectorAll("#nn-scale-group .nn-seg").forEach(seg => {
 seg.onclick = () => {
 document.querySelectorAll("#nn-scale-group .nn-seg").forEach(s => s.classList.remove("nn-on"));
 seg.classList.add("nn-on");
 $("#nn-target-chapters").value = seg.dataset.chapters;
 _nnScheduleDraft();
 };
 });
 // 字数分段：点击写全局默认目标字数
 document.querySelectorAll("#nn-words-group .nn-seg").forEach(seg => {
 seg.onclick = () => {
 document.querySelectorAll("#nn-words-group .nn-seg").forEach(s => s.classList.remove("nn-on"));
 seg.classList.add("nn-on");
 const w = parseInt(seg.dataset.words);
 localStorage.setItem("novelai:default-target-words", String(w));
 showToast(`每章目标字数设为 ${(w/1000).toFixed(w%1000?1:0)} 千字`, "info");
 };
 });
 // 保存项目
 $("#nn-save-project").onclick = async () => {
 const preset = $("#nn-preset").value;
 const style = preset === "自定义" ? ($("#nn-style").value || "") : preset;
 const doReset = $("#nn-reset").checked;
 if (doReset && !(await showConfirm("确定清除当前所有章节/事件/伏笔数据，重新开始？\n（人物设定会保留）", "! 重置项目"))) {
 $("#nn-reset").checked = false;
 return;
 }
 try {
 $("#nn-save-status").textContent = "保存中…";
 await API.post("/project/setup", {
 title: $("#nn-title").value, synopsis: $("#nn-synopsis").value,
 style: style, pov_mode: $("#nn-pov").value, story_time_unit: $("#nn-unit").value,
 reset: doReset,
 });
 $("#nn-save-status").textContent = "已保存";
 showToast(doReset ? "已清除旧数据，项目已重置" : "项目设定已保存");
 STATE.project = {...(STATE.project||{}), title: $("#nn-title").value, synopsis: $("#nn-synopsis").value, style, pov_mode: $("#nn-pov").value, story_time_unit: $("#nn-unit").value};
 STATE.chapters = []; // 清空前端缓存
 localStorage.removeItem(NN_DRAFT_LS); // 已落库，清草稿
 _nnStep = 2;
 _nnRenderStep();
 _nnRefreshExistingOutline();
 } catch (e) { toastError("保存失败", e); $("#nn-save-status").textContent = ""; }
 };
 // 生成大纲
 $("#nn-gen-outline").onclick = async () => {
 const target = parseInt($("#nn-target-chapters").value) || 20;
 const hasExisting = !!_nnOutlineData || document.querySelector("#nn-outline-result .nn-card") || document.querySelector("#nn-has-outline");
 if (hasExisting && !(await showConfirm(`将重新生成整本大纲（${target} 章），覆盖当前大纲。确定继续？`, "! 重新生成"))) return;
 try {
 $("#nn-outline-status").textContent = "AI 生成中（可能需 30-60 秒）…";
 $("#nn-gen-outline").disabled = true;
 const result = await API.post("/outline/generate", {target_chapters: target}, LLM_TIMEOUT_MS);
 _nnOutlineData = result;
 renderOutlineResult(result);
 $("#nn-outline-status").textContent = `生成 ${result.count} 章`;
 _nnStep = 3;
 _nnRenderStep();
 } catch (e) {
 toastError("大纲生成失败", e);
 $("#nn-outline-status").textContent = "失败";
 } finally {
 $("#nn-gen-outline").disabled = false;
 }
 };
 _nnRefreshExistingOutline();
 _nnLoadProjectIntoForm(); // 刷新后从后端回填已保存的项目设定
}

// 刷新后 STATE.project 丢失，从 /dashboard 拉回已保存的项目设定回填表单
function _nnLoadProjectIntoForm() {
 if (STATE.project && Object.keys(STATE.project).length > 0) return; // 已有数据不覆盖
 API.get("/dashboard").then(d => {
 const proj = d && d.project;
 if (!proj) return;
 const draft = _nnReadDraft(); // 草稿优先：用户在填的新书
 const setVal = (id, v) => { const el = document.getElementById(id); if (el && v != null) el.value = v; };
 if (!(draft.title ?? "")) setVal("nn-title", proj.title || "");
 if (!(draft.synopsis ?? "")) setVal("nn-synopsis", proj.synopsis || "");
 if (!(draft.pov ?? "")) setVal("nn-pov", proj.pov_mode || "限知视角");
 if (!(draft.unit ?? "")) setVal("nn-unit", proj.story_time_unit || "日");
 if (!(draft.style ?? "")) {
 const sel = Object.entries(STYLE_PRESETS).find(([k,v]) => v && v === proj.style);
 if (sel) { setVal("nn-preset", sel[0]); $("#nn-style-wrap").style.display = "none"; }
 else if (proj.style) { setVal("nn-preset", "自定义"); $("#nn-style-wrap").style.display = ""; setVal("nn-style", proj.style); }
 }
 STATE.project = proj; // 缓存，避免重复拉取
 }).catch(() => {});
}

// ---- 新建小说：步骤与草稿 ----
const NN_DRAFT_LS = "novelai:nn-draft";
let _nnStep = 1;
let _nnOutlineData = null;
let _nnDraftTimer = null;

function _nnReadDraft() {
 try { return JSON.parse(localStorage.getItem(NN_DRAFT_LS) || "{}"); } catch (_) { return {}; }
}
function _nnScheduleDraft() {
 clearTimeout(_nnDraftTimer);
 _nnDraftTimer = setTimeout(() => {
 const pick = id => { const el = document.getElementById(id); return el ? el.value : ""; };
 localStorage.setItem(NN_DRAFT_LS, JSON.stringify({
 title: pick("nn-title"), synopsis: pick("nn-synopsis"),
 preset: pick("nn-preset"), pov: pick("nn-pov"), unit: pick("nn-unit"),
 style: pick("nn-style"), chapters: pick("nn-target-chapters"),
 }));
 }, 300);
}
function _nnWordsSel() {
 const v = parseInt(localStorage.getItem("novelai:default-target-words") || "0");
 return [2000, 5000, 10000, 20000].includes(v) ? v : 0;
}
function _nnRenderStep() {
 document.querySelectorAll("#nn-stepper .nn-step").forEach(step => {
 const s = parseInt(step.dataset.step);
 step.classList.toggle("nn-active", s === _nnStep);
 step.classList.toggle("nn-done", s < _nnStep);
 });
 document.querySelectorAll(".nn-panel").forEach(panel => {
 panel.classList.remove("nn-active");
 });
 const cur = document.getElementById("nn-step-" + _nnStep);
 if (cur) cur.classList.add("nn-active");
}
function _nnRefreshExistingOutline() {
 // 已存在大纲：展示摘要，生成按钮变"重新生成"
 API.get("/chapters").then(chapters => {
 if (chapters && chapters.length > 0) {
 const written = chapters.filter(c => c.word_count > 0).length;
 if (!_nnOutlineData) {
 $("#nn-outline-result").innerHTML = `<div id="nn-has-outline" style="padding:10px;color:var(--fg-muted);font-size:12px">已有 ${chapters.length} 章大纲 · ${written} 章已写正文。可点击"重新生成"覆盖，或直接进入下一步。</div>`;
 }
 $("#nn-outline-status").textContent = `已有 ${chapters.length} 章`;
 const btn = document.getElementById("nn-gen-outline");
 if (btn) btn.textContent = "重新生成";
 if (_nnStep < 2) { _nnStep = 2; _nnRenderStep(); }
 updateWriteSection(); // 已有大纲 → 第 3 步写正文入口可用
 }
 }).catch(() => {});
}

function renderOutlineResult(result) {
 const chapters = result.chapters || [];
 if (!chapters.length) { $("#nn-outline-result").innerHTML = '<p class="placeholder">大纲为空</p>'; return; }
 _nnOutlineData = result;
 let html = `<div style="display:flex;justify-content:flex-end;margin-bottom:6px">
 <button class="btn small" id="nn-toggle-expand"> 全部展开</button>
 </div>
 <div style="max-height:420px;overflow:auto">`;
 for (const ch of chapters) {
 const hook = ch.hook ? `<span class="badge" style="background:color-mix(in srgb, var(--warning) 15%, transparent);color:var(--warning);font-size:11px;margin-left:6px">钩子</span>` : "";
 html += `<div class="nn-card" data-idx="${ch.idx}">
 <div class="nn-card-head">
 <span class="nn-caret">▸</span>
 <span class="nn-card-title">第${ch.idx}章 · ${ESC(ch.title || "")}</span>
 ${hook}
 </div>
 <div class="nn-card-body">
 <div class="nn-card-sec"><div class="nn-card-sec-label">摘要</div>${ESC(ch.summary || "")}</div>
 ${ch.hook ? `<div class="nn-card-sec"><div class="nn-card-sec-label">钩子</div>${ESC(ch.hook)}</div>` : ""}
 ${ch.causal_link ? `<div class="nn-card-sec"><div class="nn-card-sec-label">承接</div>${ESC(ch.causal_link)}</div>` : ""}
 <div class="nn-card-sec"><div class="nn-card-sec-label">元信息</div>POV：${ESC(ch.pov_character || "-")} · 时间：${ESC(ch.story_time ?? "-")} · 地点：${ESC(ch.location || "-")}</div>
 </div>
 </div>`;
 }
 html += `</div>`;
 if (result.structural_notes) {
 html += `<div class="nn-notes"><div class="nn-notes-label">结构备注</div>${ESC(result.structural_notes)}</div>`;
 }
 $("#nn-outline-result").innerHTML = html;
 document.querySelectorAll("#nn-outline-result .nn-card").forEach(card => {
 card.querySelector(".nn-card-head").onclick = () => card.classList.toggle("nn-open");
 });
 const expandBtn = document.getElementById("nn-toggle-expand");
 let allOpen = false;
 expandBtn.onclick = () => {
 allOpen = !allOpen;
 document.querySelectorAll("#nn-outline-result .nn-card").forEach(c => c.classList.toggle("nn-open", allOpen));
 expandBtn.textContent = allOpen ? " 全部收起" : " 全部展开";
 };
 const btn = document.getElementById("nn-gen-outline");
 if (btn) btn.textContent = "重新生成";
 updateWriteSection();
}

function updateWriteSection() {
 const hasOutline = _nnOutlineData || document.querySelector("#nn-outline-result .nn-card") || document.querySelector("#nn-has-outline");
 if (!hasOutline) {
 $("#nn-write-section").innerHTML = '先完成第 1、2 步（保存项目 + 生成大纲）。';
 return;
 }
 const target = parseInt(localStorage.getItem("novelai:default-target-words") || "0") || 0;
 const wordsTip = target ? `（每章目标 ${(target/1000).toFixed(target%1000?1:0)} 千字）` : "";
 $("#nn-write-section").innerHTML = `
 <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
 <button class="btn primary" onclick="writeFirstChapter()">AI 写第 1 章 ${wordsTip}</button>
 <span style="color:var(--fg-muted);font-size:11px">单章约 30-90 秒，完成后自动跳转编辑器</span>
 </div>
 <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
 <span style="font-size:12px;color:var(--fg-muted)">或批量生成：</span>
 <input type="number" id="nn-batch-from" value="1" min="1" class="nn-input" style="width:60px">
 <span style="font-size:12px">到</span>
 <input type="number" id="nn-batch-to" value="5" min="1" class="nn-input" style="width:60px">
 <button class="btn" onclick="batchGenerate()">批量生成</button>
 </div>
 `;
}


async function writeFirstChapter() {
 try {
 showToast("开始写第 1 章，实时展示中…");
 await streamWriteChapter(1);
 } catch (e) { toastError("启动写章失败", e); }
}

async function batchGenerate() {
 const fromIdx = parseInt($("#nn-batch-from").value) || 1;
 const toIdx = parseInt($("#nn-batch-to").value) || 5;
 try {
 showToast(`开始批量生成第 ${fromIdx}-${toIdx} 章…`);
 await API.post("/book/generate", {from_idx: fromIdx, to_idx: toIdx, target_words: _getChapterTargetWords(fromIdx)});
 goto("pipeline");
 } catch (e) { toastError("批量生成失败", e); }
}

// =================== 导入 ===================
function renderImport() {
 setToolHeader("导入 Markdown 手稿", "把你的 .md 文件导入数据库。格式：<code># 第一卷 ...</code> + <code>## 第一回 ...</code> + 正文。");
 setToolBody(`
 <div style="max-width:600px">
 <div class="form-row">
 <label>文件/目录路径</label>
 <input type="text" id="imp-path" placeholder="如: C:\\Users\\me\\novel.md 或 C:\\Users\\me\\mybook\\">
 </div>
 <div class="form-help">单文件 / 目录自动识别。文件按"# 第N卷" / "## 第N回"切分。</div>
 <div class="form-row">
 <label>书名（可选）</label>
 <input type="text" id="imp-title" placeholder="不填则用文件名">
 </div>
 <div class="form-row">
 <label>时间单位</label>
 <select id="imp-unit">
 <option value="回">回</option>
 <option value="日">日</option>
 <option value="小时">小时</option>
 <option value="章">章</option>
 <option value="不定">不定</option>
 </select>
 </div>
 <div style="margin-top:16px">
 <button class="btn primary" id="btn-imp-go">开始导入</button>
 <span id="imp-status" style="margin-left:12px;color:var(--fg-muted)"></span>
 </div>
 <div id="imp-result" style="margin-top:16px"></div>
 </div>
 `);
 $("#btn-imp-go").onclick = async () => {
 const path = $("#imp-path").value.trim();
 if (!path) { showToast("请填路径", "warning"); return; }
 // 如果已有章节，警告导入会覆盖同编号章节（importer 按 idx upsert）
 try {
 const chs = await API.get("/chapters");
 const existing = Array.isArray(chs) ? chs : (chs.chapters || []);
 if (existing && existing.length > 0) {
 const ok = await showConfirm(
 `当前已有 ${existing.length} 章数据。\n导入新文件会按回目编号覆盖同编号的章节内容（不会删除其他章节）。\n\n确定继续导入？`,
 "! 导入将覆盖"
 );
 if (!ok) return;
 }
 } catch (_) { /* 查询失败不阻塞导入 */ }
 $("#imp-status").textContent = "导入中…";
 $("#imp-result").innerHTML = "";
 try {
 const r = await API.post("/import", {
 path: path,
 title: $("#imp-title").value.trim() || null,
 story_time_unit: $("#imp-unit").value,
 });
 $("#imp-status").textContent = "完成";
 $("#imp-result").innerHTML = `<div class="dash-card" style="background:color-mix(in srgb, var(--success) 10%, transparent);border-color:var(--success)">
 ✓ 导入完成：${r.chapters} 章，${r.words} 字，${r.volumes} 卷
 </div>`;
 addLog("done", `[import] ${r.chapters} 章 / ${r.words} 字 / ${r.volumes} 卷`);
 showToast(`导入完成 · ${r.chapters} 章 ${r.words} 字`, "success");
 await refreshAll();
 // 清掉旧章节指针, 否则跳编辑器会加载旧书的章节（与拖拽导入路径行为一致）
 STATE_EDITOR.chapterIdx = null;
 try { localStorage.removeItem("novelai-last-chapter"); } catch (_) {}
 let firstIdx = 1;
 try {
 const chs = await API.get("/chapters");
 if (chs && chs.length) firstIdx = chs[0].idx;
 } catch (_) {}
 // 导入成功后跳转到编辑器（修 #7：之前留在导入页用户以为没成功）
 setTimeout(() => gotoEditorAndLoad(firstIdx), 1200);
 } catch (e) {
 $("#imp-status").textContent = "失败";
 $("#imp-result").innerHTML = `<div class="dash-card" style="background:color-mix(in srgb, var(--danger) 10%, transparent);border-color:var(--danger)">✕ ${ESC(e.message || e)}</div>`;
 }
 };
}

// =================== 章节 ===================
async function renderChapters() {
 setToolHeader("章节 / 卷", "查看所有章节，点击右侧查看详情。");
 setToolBody(`<p class="placeholder loading">加载中…</p>`);
 try {
 const chapters = await API.get("/chapters");
 if (!chapters.length) {
 setToolBody(emptyStateHTML({
 icon: "",
 title: "还没有章节",
 desc: "把你的 Markdown 手稿导入进来，自动按《第N回》切分章节。",
 cta: { label: " 立即导入 MD", onclick: "goto('import')" },
 extra: "<div style='margin-top:14px;color:var(--fg-dim);font-size:11px'> 提示：也可以直接把 .md 文件拖到窗口</div>",
 }));
 return;
 }
 const volumes = await API.get("/volumes");
 const volMap = {};
 volumes.forEach(v => volMap[v.idx] = v.title);
 STATE.chapters = chapters;
 let html = `<div style="margin-bottom:12px;color:var(--fg-muted);font-size:11px">共 ${chapters.length} 章</div><div class="list-card">`;
 for (const c of chapters) {
 const volName = c.volume_idx && volMap[c.volume_idx] ? `卷${c.volume_idx} ${volMap[c.volume_idx]}` : "未分配卷";
 const hasText = c.final_text || c.draft;
 const status = hasText ? (c.final_text ? "✓" : "") : "";
 html += `<div class="list-row" data-idx="${c.idx}">
 <div class="lr-title">${status} 第 ${c.idx} 章 ${ESC(c.title || "")}</div>
 <div class="lr-meta">${volName} · ${(c.word_count || 0).toLocaleString()} 字${c.location ? ' · ' + ESC(c.location) : ''}</div>
 </div>`;
 }
 html += `</div>`;
 setToolBody(html);
 document.querySelectorAll(".list-row[data-idx]").forEach(el => {
 el.onclick = () => showChapterDetail(parseInt(el.dataset.idx));
 });
 } catch (e) {
 setToolBody(`<p class="placeholder">加载失败: ${ESC(e.message || e)}</p>`);
 }
}

async function showChapterDetail(idx) {
 try {
 const r = await API.get(`/editor/chapter/${idx}`);
 const chapter = r.chapter || r;
 const events = r.events || [];
 // 如果 #detail 被隐藏（在 tool/editor 视图中），回退到在 tool-body 中显示
 const detail = $("#detail");
 const body = (detail && detail.style.display !== "none") ? $("#detail-body") : $("#tool-body");
 if (body) {
 body.innerHTML = `
 <div class="detail-section"><h4>第 ${chapter.idx} 章</h4><div class="v" style="font-weight:600">${ESC(chapter.title || "(无标题)")}</div></div>
 <div class="detail-section"><h4>字数 / 状态</h4><div class="v">${chapter.word_count || 0} 字 · ${chapter.final_text ? "已终稿" : (chapter.draft ? "草稿" : "未生成")}</div></div>
 <div class="detail-section"><h4>故事内时间</h4><div class="v">${chapter.story_time_start ?? "—"} ~ ${chapter.story_time_end ?? "—"} ${STATE.dashboard?.project?.story_time_unit || ""}</div></div>
 <div class="detail-section"><h4>地点</h4><div class="v">${ESC(chapter.location || "—")}</div></div>
 ${chapter.outline ? `<div class="detail-section"><h4>大纲</h4><div class="v">${ESC(chapter.outline)}</div></div>` : ""}
 ${chapter.summary ? `<div class="detail-section"><h4>摘要</h4><div class="v">${ESC(chapter.summary)}</div></div>` : ""}
 ${events.length ? `<div class="detail-section"><h4>本章事件 (${events.length})</h4><div class="v">${events.map((e, i) => {
 const causes = e.cause_event_ids && e.cause_event_ids.length ? ` ← 因果: #${e.cause_event_ids.join(', #')}` : '';
 return `· <span class="badge ${e.event_type}">${e.event_type}</span> ${ESC(e.title)}${causes} — ${ESC(e.summary || "")}`;
 }).join("<br>")}</div></div>` : ""}
 <div class="detail-section" style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
 <button class="btn primary small" onclick="goto('editor'); setTimeout(()=>loadEditorChapter(${idx}),300)">编辑这章</button>
 ${!chapter.final_text ? `<button class="btn small" onclick="writeChapterFromDetail(${idx})">AI 写这章</button>` : ""}
 <button class="btn small" onclick="if(confirm('确定删除第${idx}章？')) deleteChapter(${idx})">删除</button>
 </div>
 `;
 // 绑定 AI 写章
 window.writeChapterFromDetail = async (chIdx) => {
 try { await streamWriteChapter(chIdx); }
 catch (e) { toastError("写章失败", e); }
 };
 window.deleteChapter = async (chIdx) => {
 try { await API.del(`/chapter/${chIdx}`); showToast("已删除"); renderChapters(); }
 catch (e) { toastError("删除失败", e); }
 };
 }
 } catch (e) {
 const errBody = $("#detail-body") || $("#tool-body");
 if (errBody) errBody.innerHTML = `<p class="placeholder" style="padding:20px;text-align:center;color:var(--fg-muted)">! 加载失败: ${ESC(e.message || e)}<br><span style="font-size:11px">可能是第 ${idx} 章不存在</span></p>`;
 }
}

// =================== 人物 ===================
async function renderCharacters() {
 setToolHeader("人物档案", "点击人物右侧查看详情。");
 setToolBody(`<p class="placeholder loading">加载中…</p>`);
 try {
 const chars = await API.get("/characters");
 STATE.characters = chars;
 renderCharactersList(chars);
 } catch (e) {
 setToolBody(`<p class="placeholder">加载失败: ${ESC(e.message || e)}</p>`);
 }
}

function renderCharactersList(chars) {
 // 分组定义（按重要度排序，minor 默认折叠）
 const GROUPS = [
 {role: "protagonist", label: "主角", collapsed: false},
 {role: "antagonist", label: "反派", collapsed: false},
 {role: "major", label: "重要配角", collapsed: false},
 {role: "supporting", label: "常规配角", collapsed: false},
 {role: "minor", label: "次要人物", collapsed: true}, // minor 默认折叠（200+时避免 DOM 爆炸）
 {role: "_other", label: "未分类", collapsed: false},
 ];
 // 按 role 分组
 const grouped = {};
 for (const c of chars) {
 const r = c.role || "_other";
 if (!grouped[r]) grouped[r] = [];
 grouped[r].push(c);
 }
 // 顶部：添加按钮 + 计数 + 搜索框
 let html = `<div style="margin-bottom:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
 <button class="btn primary" id="btn-add-char">+ 添加人物</button>
 <span style="color:var(--fg-muted);font-size:11px">共 ${chars.length} 人</span>
 <input type="text" id="char-search" placeholder="搜索名字/别名/MBTI…" style="flex:1;min-width:160px;padding:6px 10px;background:var(--bg-card);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur-webkit);border:1px solid var(--border-glass);border-radius:var(--radius);box-shadow:var(--shadow-inner);color:var(--fg);font-size:13px;outline:none">
 </div>`;
 // 渲染分组
 html += `<div class="list-card">`;
 for (const g of GROUPS) {
 const items = grouped[g.role] || [];
 if (!items.length) continue;
 const isCollapsed = g.collapsed && items.length > 10; // minor 且超 10 个才折叠
 html += `<details class="char-group" data-role="${g.role}" ${isCollapsed ? "" : "open"} style="margin-bottom:4px">
 <summary style="cursor:pointer;user-select:none;padding:6px 10px;font-size:12px;font-weight:600;color:var(--fg-muted);border-radius:var(--radius-sm);background:var(--bg-card)">
 ${g.label} <span style="color:var(--fg-dim);font-weight:400">(${items.length})</span>
 </summary>
 <div class="char-group-body" style="padding:4px 0 4px 4px">`;
 for (const c of items) {
 const row = renderCharRow(c);
 html += row;
 }
 html += `</div></details>`;
 }
 html += `</div>`;
 setToolBody(html);
 // 绑定点击
 document.querySelectorAll(".list-row[data-id]").forEach(el => {
 el.onclick = () => showCharacterDetail(parseInt(el.dataset.id));
 });
 $("#btn-add-char").onclick = () => addCharacterDialog();
 // 搜索过滤
 const searchEl = $("#char-search");
 if (searchEl) {
 searchEl.oninput = () => {
 const q = searchEl.value.trim().toLowerCase();
 document.querySelectorAll(".char-group").forEach(g => {
 let visible = 0;
 g.querySelectorAll(".list-row[data-id]").forEach(row => {
 const c = STATE.characters.find(x => x.id === parseInt(row.dataset.id));
 if (!c) return;
 const haystack = [c.name, c.mbti || "", ...(c.aliases || [])].join(" ").toLowerCase();
 const match = !q || haystack.includes(q);
 row.style.display = match ? "" : "none";
 if (match) visible++;
 });
 // 搜索时自动展开所有分组
 if (q) g.setAttribute("open", "");
 // 隐藏无匹配的分组
 g.style.display = visible > 0 ? "" : "none";
 });
 };
 }
}

function renderCharRow(c) {
 const mbtiTag = c.mbti ? `<span class="opt-badge" style="background:var(--bg-elevated);color:var(--accent);margin-left:6px">${ESC(c.mbti)}</span>` : '<span style="color:var(--danger);font-size:11px;margin-left:6px">未标 MBTI</span>';
 let statusTag = "";
 if (c.status) {
 let sColor = "var(--fg-muted)";
 if (c.status.includes("死") || c.status.includes("亡")) sColor = "var(--danger)";
 else if (c.status.includes("失踪") || c.status.includes("消失")) sColor = "var(--warning)";
 else if (c.status.includes("伤")) sColor = "var(--warning)";
 statusTag = `<span class="badge" style="background:color-mix(in srgb, ${sColor} 15%, transparent);color:${sColor};margin-left:6px">${ESC(c.status)}</span>`;
 }
 // 出场次数（>0 时显示）
 const appTag = c.appearance_count > 0 ? `<span style="color:var(--fg-dim);font-size:11px;margin-left:6px">出场${c.appearance_count}次</span>` : "";
 return `<div class="list-row" data-id="${c.id}">
 <div class="lr-title">${ESC(c.name)} <span class="badge ${c.role || 'supporting'}">${c.role || '?'}</span>${mbtiTag}${statusTag}${appTag}</div>
 <div class="lr-meta">${ESC((c.basic_info || "").slice(0, 60))}</div>
 </div>`;
}

async function showCharacterDetail(id) {
 const c = STATE.characters.find(x => x.id === id);
 if (!c) return;
 // 先用基础档案占位，再异步加载完整小传
 $("#detail-body").innerHTML = `<div class="detail-section"><h4>${ESC(c.name)}</h4><p class="placeholder loading" style="text-align:left;padding:8px 0">加载小传…</p></div>`;
 try {
 const profile = await API.get(`/character/${id}/profile`);
 renderCharacterProfile(profile);
 } catch (e) {
 // 降级：仅显示基础档案
 $("#detail-body").innerHTML = `<div class="detail-section"><h4>${ESC(c.name)}</h4><div class="v">${ESC(e.message || e)}</div></div>`;
 }
}

function renderCharacterProfile(profile) {
 const c = profile.character;
 // status badge 配色
 let statusBadge = "";
 if (c.status) {
 let sColor = "var(--fg-muted)";
 if (c.status.includes("死") || c.status.includes("亡")) sColor = "var(--danger)";
 else if (c.status.includes("失踪") || c.status.includes("消失")) sColor = "var(--warning)";
 else if (c.status.includes("伤")) sColor = "var(--warning)";
 statusBadge = `<span class="badge" style="background:color-mix(in srgb, ${sColor} 15%, transparent);color:${sColor}">${ESC(c.status)}</span>`;
 }
 let html = `<div class="detail-section">
 <h4>${ESC(c.name)} ${statusBadge}</h4>
 <div class="v">
 <span class="badge ${c.role || 'supporting'}">${c.role || '?'}</span>
 ${c.mbti ? `<span class="opt-badge" style="background:var(--bg-elevated);color:var(--accent)">${c.mbti}</span>` : '<span style="color:var(--danger);font-size:11px">未标 MBTI</span>'}
 ${c.aliases?.length ? `<span style="color:var(--fg-muted);font-size:11px">别号：${c.aliases.map(ESC).join("、")}</span>` : ""}
 </div>
 </div>`;
 // 基础档案
 if (c.basic_info) html += `<div class="detail-section"><h4>基础</h4><div class="v">${ESC(c.basic_info)}</div></div>`;
 if (c.personality) html += `<div class="detail-section"><h4>性格</h4><div class="v">${ESC(c.personality)}</div></div>`;
 if (c.speech_style) html += `<div class="detail-section"><h4>说话风格</h4><div class="v">${ESC(c.speech_style)}</div></div>`;
 if (c.abilities) html += `<div class="detail-section"><h4>能力</h4><div class="v">${ESC(c.abilities)}</div></div>`;
 if (c.arc) html += `<div class="detail-section"><h4>弧光</h4><div class="v">${ESC(c.arc)}</div></div>`;
 // 成长里程碑
 if (profile.milestones?.length) {
 html += `<div class="detail-section"><h4>成长里程碑 (${profile.milestones.length})</h4><div class="v">`;
 for (const m of profile.milestones) {
 html += `<div style="margin:3px 0;font-size:12px">• 第${m.chapter_idx || "?"}章 <span style="color:var(--accent)">${ESC(m.milestone_type)}</span> ${ESC(m.description || "")}</div>`;
 }
 html += `</div></div>`;
 }
 // 事件时间线
 if (profile.events?.length) {
 html += `<div class="detail-section"><h4>事件时间线 (${profile.events.length})</h4><div class="v">`;
 for (const ev of profile.events) {
 const typeColor = ev.event_type === "death" ? "var(--danger)" :
   ev.event_type === "disappearance" ? "var(--warning)" :
   ev.event_type === "turning_point" ? "var(--accent)" : "var(--fg-muted)";
 html += `<div style="margin:4px 0;font-size:12px">第${ev.chapter_idx || "?"}章 <span class="badge" style="background:color-mix(in srgb, ${typeColor} 15%, transparent);color:${typeColor}">${ESC(ev.event_type)}</span> <span style="color:var(--fg)">${ESC(ev.title)}</span><br><span style="color:var(--fg-muted);font-size:11px;margin-left:8px">${ESC((ev.summary || "").slice(0, 80))}</span></div>`;
 }
 html += `</div></div>`;
 }
 // 关系演变
 if (profile.relationships?.length) {
 html += `<div class="detail-section"><h4>关系演变 (${profile.relationships.length})</h4><div class="v">`;
 for (const r of profile.relationships) {
 let nums = [];
 if (r.intimacy !== null && r.intimacy !== undefined) nums.push(`亲密度 ${Number(r.intimacy).toFixed(1)}`);
 if (r.trust !== null && r.trust !== undefined) nums.push(`信任 ${Number(r.trust).toFixed(1)}`);
 if (r.conflict !== null && r.conflict !== undefined) nums.push(`冲突 ${Number(r.conflict).toFixed(1)}`);
 html += `<div style="margin:4px 0;font-size:12px">↔ <span style="color:var(--accent)">${ESC(r.other_name)}</span>：${ESC(r.rel_type)}（${ESC(r.current_state || "未明确")}）${nums.length ? `<span style="color:var(--fg-muted)"> · ${nums.join(" / ")}</span>` : ""}</div>`;
 }
 html += `</div></div>`;
 }
 // 相关伏笔
 if (profile.threads?.length) {
 html += `<div class="detail-section"><h4>相关伏笔 (${profile.threads.length})</h4><div class="v">`;
 for (const t of profile.threads) {
 const sColor = t.status === "resolved" ? "var(--success)" : t.status === "payoff" ? "var(--accent)" : "var(--warning)";
 html += `<div style="margin:4px 0;font-size:12px"><span class="badge" style="background:color-mix(in srgb, ${sColor} 15%, transparent);color:${sColor}">${ESC(t.thread_type)}|${ESC(t.status)}</span> ${ESC(t.title)}</div>`;
 }
 html += `</div></div>`;
 }
 // 操作按钮
 html += `<div class="detail-section" style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">
 <button class="btn small" id="btn-edit-mbti-${c.id}">设置 MBTI</button>
 <button class="btn small" id="btn-set-status-${c.id}">修改状态</button>
 </div>`;
 $("#detail-body").innerHTML = html;
 const mbtiBtn = document.getElementById(`btn-edit-mbti-${c.id}`);
 if (mbtiBtn) mbtiBtn.onclick = () => setMbtiDialog(c);
 const statusBtn = document.getElementById(`btn-set-status-${c.id}`);
 if (statusBtn) statusBtn.onclick = () => setStatusDialog(c);
}

function setStatusDialog(c) {
 const opts = ["活", "已死", "失踪", "重伤", "存活", ""];
 const cur = c.status || "";
 const items = opts.map(s => {
 const label = s === "" ? "（清空）" : s;
 const sel = s === cur ? "primary" : "";
 return `<button class="btn small ${sel}" data-status="${ESC(s)}" style="margin:3px">${ESC(label)}</button>`;
 }).join("");
 showModal(`修改 ${ESC(c.name)} 的状态`, `<div style="display:flex;flex-wrap:wrap;gap:4px;margin:12px 0">${items}</div>
 <div style="color:var(--fg-muted);font-size:11px;margin-top:8px">状态影响死人复活检测：标记"已死"后，该角色在后续章节以活人姿态出现会被标记为高危问题。</div>`,
 null);
 document.querySelectorAll("[data-status]").forEach(btn => {
 btn.onclick = async () => {
 const newStatus = btn.dataset.status;
 try {
 await API.post("/character/set_status", {name: c.name, status: newStatus});
 c.status = newStatus;
 hideModal();
 showCharacterDetail(c.id); // 刷新
 renderCharacters(); // 刷新列表 badge
 showToast("状态已更新");
 } catch (e) { toastError("修改状态失败", e); }
 };
 });
}

function setMbtiDialog(c) {
 showModal("设置 MBTI", `
 <p style="color:var(--fg-muted)">为「${ESC(c.name)}」设置 16 型人格</p>
 <div class="form-row"><label>MBTI</label>
 <select id="mbti-sel">
 ${["INTJ","INTP","ENTJ","ENTP","INFJ","INFP","ENFJ","ENFP","ISTJ","ISFJ","ESTJ","ESFJ","ISTP","ISFP","ESTP","ESFP"].map(t => `<option value="${t}" ${c.mbti===t?"selected":""}>${t}</option>`).join("")}
 </select>
 </div>
 `, async () => {
 const mbti = $("#mbti-sel").value;
 try {
 await API.post("/character/set_mbti", {name: c.name, mbti});
 addLog("done", `[mbti] ${c.name} → ${mbti}`);
 renderCharacters();
 refreshAll();
 } catch (e) {
 toastError("操作失败", e);
 } finally {
 hideModal();
 }
 });
}


function addCharacterDialog() {
 showModal("添加人物", `
 <div class="form-row"><label>姓名 *</label><input type="text" id="ac-name"></div>
 <div class="form-row"><label>角色</label>
 <select id="ac-role"><option value="supporting">配角</option><option value="protagonist">主角</option><option value="antagonist">反派</option></select>
 </div>
 <div class="form-row"><label>基础信息</label><input type="text" id="ac-info" placeholder="年龄/性别/职业/出身"></div>
 <div class="form-row"><label>性格</label><textarea id="ac-personality" placeholder="性格关键词/价值观/恐惧/欲望"></textarea></div>
 <div class="form-row"><label>MBTI（可选）</label><input type="text" id="ac-mbti" placeholder="如 INTJ（留空稍后标）" maxlength="4"></div>
 `, async () => {
 const nameRaw = $("#ac-name").value.trim();
 if (!nameRaw) { showToast("姓名必填", "warning"); return; }
 const role = $("#ac-role").value;
 const info = $("#ac-info").value.trim();
 const personality = $("#ac-personality").value.trim();
 const mbti = $("#ac-mbti").value.trim().toUpperCase();
 try {
 const r = await API.post("/character/add", {
 name: nameRaw, role, basic_info: info, personality, mbti: mbti || null,
 });
 if (r.ok) {
 hideModal();
 showToast(`已添加人物：${nameRaw}`, "success");
 refreshAll();
 if (CURRENT.target === "characters") renderCharacters();
 }
 } catch (e) {
 toastError("添加失败", e);
 }
 });
}

// =================== MBTI 标注页 ===================
async function renderMbti() {
 setToolHeader("MBTI 标注", "给主要人物标 16 型人格。这是后续所有人物维度分析的基础。");
 setToolBody(`<p class="placeholder loading">加载中…</p>`);
 try {
 const chars = await API.get("/characters");
 STATE.characters = chars;
 const withMbti = chars.filter(c => c.mbti).length;
 let html = `<div style="margin-bottom:12px;padding:8px 12px;background:var(--bg-elevated);border-radius:6px">已标注 <b>${withMbti}</b> / ${chars.length} 人 ${withMbti < chars.length ? '<span style="color:var(--warning)">· 建议把主要人物都标了</span>' : '<span style="color:var(--success)">· ✓ 全部已标</span>'}</div>`;
 html += `<div class="list-card">`;
 for (const c of chars) {
 const mbtiTag = c.mbti ? `<span class="opt-badge" style="background:var(--bg-elevated);color:var(--accent)">${c.mbti}</span>` : '<span style="color:var(--danger);font-size:11px">! 未标</span>';
 html += `<div class="list-row">
 <div class="lr-title">${ESC(c.name)} <span class="badge ${c.role || 'supporting'}">${c.role || '?'}</span> ${mbtiTag}</div>
 <div class="lr-meta"><button class="btn small" data-set-mbti="${c.id}">设置 MBTI</button></div>
 </div>`;
 }
 html += `</div>`;
 setToolBody(html);
 document.querySelectorAll("[data-set-mbti]").forEach(b => {
 b.onclick = () => {
 const c = chars.find(x => x.id == b.dataset.setMbti);
 if (c) setMbtiDialog(c);
 };
 });
 } catch (e) {
 setToolBody(`<p class="placeholder">加载失败: ${ESC(e.message || e)}</p>`);
 }
}

// =================== 事件 / 伏笔 / 事实 ===================
async function renderEvents() {
 setToolHeader("事件 / 伏笔 / 事实", "查看已录入的事件、伏笔、事实。");
 setToolBody(`<p class="placeholder loading">加载中…</p>`);
 try {
 const events = await API.get("/events");
 const threads = await API.get("/threads");
 STATE.events = events;
 STATE.threads = threads;
 if (!events.length && !threads.length) {
 setToolBody(emptyStateHTML({
 icon: "",
 title: "事件 / 伏笔 / 事实 都还没录入",
 desc: "导入手稿后，让 AI 自动抽取每章的关键事件和伏笔。",
 cta: { label: " 去 AI 抽取", onclick: "goto('ai-extract')" },
 extra: "<div style='margin-top:12px;color:var(--fg-dim);font-size:11px'>需要先导入至少 1 章</div>",
 }));
 return;
 }
 let html = `<div class="dash-row" style="grid-template-columns: 1fr 1fr; gap:12px">`;
 html += `<div class="dash-card"><div class="card-title"> 事件 (${events.length})</div><div class="list-card" style="max-height:500px;overflow-y:auto">`;
 for (const e of events) {
 html += `<div class="list-row">
 <div class="lr-title">@${e.story_time} <span class="badge ${e.event_type}">${e.event_type}</span> ${ESC(e.title)}</div>
 <div class="lr-meta">${ESC(e.summary || "")}</div>
 </div>`;
 }
 html += `</div></div>`;
 html += `<div class="dash-card"><div class="card-title"> 伏笔 (${threads.length})</div><div class="list-card" style="max-height:500px;overflow-y:auto">`;
 for (const t of threads) {
 html += `<div class="list-row">
 <div class="lr-title"><span class="badge ${t.status}">${t.status}</span> ${ESC(t.title)}</div>
 <div class="lr-meta">${ESC(t.description || "")}</div>
 </div>`;
 }
 html += `</div></div></div>`;
 html += `<div style="margin-top:12px;color:var(--fg-dim);font-size:11px;text-align:center">事件和伏笔由 AI 在写章时自动抽取。你可以到「AI 抽取」手动补充，或在编辑器里写新章节后自动生成。</div>`;
 setToolBody(html);
 } catch (e) {
 setToolBody(`<p class="placeholder">加载失败: ${ESC(e.message || e)}</p>`);
 }
}

// =================== AI 编辑器 ===================
const STATE_EDITOR = {
 chapterIdx: null,
 text: "",
 savedText: "",
 lastAiText: "",
 prevIdx: null,
 nextIdx: null,
};

async function renderEditor() {
 // 优先用持久化的 lastChapter → 没有就用当前 chapterIdx → 没有用最新章节
 let idx = STATE_EDITOR.chapterIdx;
 if (!idx) {
 const last = loadLastChapter();
 if (last && last.idx) {
 idx = last.idx;
 } else {
 try {
 const chapters = await API.get("/chapters");
 if (chapters.length) {
 // /chapters 列表端点会 pop 掉 final_text/draft，改用 word_count>0 判断"已写"
 const written = chapters.filter(c => (c.word_count || 0) > 0);
 idx = (written.length ? written[written.length - 1] : chapters[chapters.length - 1]).idx;
 } else {
 idx = 1;
 }
 } catch (e) { idx = 1; }
 }
 }
 await loadEditorChapter(idx);
 // 恢复滚动位置（仅当记录的就是本章, 避免套上别章的 scrollTop）
 const last = loadLastChapter();
 if (last && last.idx === idx && last.scrollTop && $("#ed-text")) {
 $("#ed-text").scrollTop = last.scrollTop;
 }
}

// =================== 通用: 按钮防并发 helper (B2/B3) ===================
async function withButtonLock(btn, fn, opts = {}) {
 if (!btn) return fn();
 if (btn.disabled) { addLog("warn", opts.busyMsg || "[ui] 上一次还在跑, 请稍等"); return; }
 const origText = btn.textContent;
 btn.disabled = true;
 if (opts.runningText) btn.textContent = opts.runningText;
 try {
 return await fn();
 } finally {
 btn.disabled = false;
 btn.textContent = origText;
 }
}

// 全局: 扫描/AI 任务单例锁, 避免同视图多次点扫描并发 (B2/B3)
const _uiLocks = { scan: false, extract: false, optimize: false };
let _relCurveCharts = {}; // 关系曲线 ECharts 实例池
function acquireUiLock(kind) {
 if (_uiLocks[kind]) return false;
 _uiLocks[kind] = true;
 return true;
}
function releaseUiLock(kind) { _uiLocks[kind] = false; }

// 切章节并发保护: 每次新请求 id 自增, 老请求回来发现 id 已变 → 丢弃
let _loadChapterReqId = 0;

/** 跳到编辑器并加载指定章节。无脏数据时预设 chapterIdx 让 renderEditor 一次加载到位（避免二次请求）；
 * 有未保存内容时不预设——renderEditor 先加载旧章（不弹确认），250ms 后 loadEditorChapter 只弹一次确认。 */
function gotoEditorAndLoad(idx) {
 const ta = $("#ed-text");
 const dirty = ta && ta.value !== STATE_EDITOR.savedText && ta.value.trim().length > 0;
 if (!dirty) STATE_EDITOR.chapterIdx = idx;
 goto("editor");
 setTimeout(() => { if (STATE_EDITOR.chapterIdx !== idx) loadEditorChapter(idx); }, 250);
 // 已在编辑器且就是本章：不重载，等视图切换后直接注入待处理指令
 setTimeout(() => { if (STATE_EDITOR.chapterIdx === idx) _applyPendingAiPrompt(); }, 500);
}

async function loadEditorChapter(idx) {
 // 切换前检查未保存内容
 const ta = $("#ed-text");
 if (ta && idx !== STATE_EDITOR.chapterIdx && ta.value !== STATE_EDITOR.savedText && ta.value.trim().length > 0) {
 if (!(await showConfirm("当前章节有未保存的修改，确定放弃并切换？"))) return;
 }
 // 如果有未采纳的 AI 输出（段落卡/inline 卡仍在面板里），警告会丢失
 if (idx !== STATE_EDITOR.chapterIdx) {
 const aiCards = document.querySelectorAll("#ed-ai-stream .ed-paragraph-card.ed-para-pending, #ed-ai-stream .ed-inline-card.epc-modified, #ed-ai-stream .epc-suspicious");
 if (aiCards.length > 0) {
 if (!(await showConfirm(`有 ${aiCards.length} 个未采纳的 AI 改稿结果，切换章节后会丢失。确定切换？`, "! AI 输出未采纳"))) return;
 }
 }
 const myReqId = ++_loadChapterReqId; // 抢占: 后发先到者胜
 // 切章节时关闭版本/diff modal，避免旧章节的恢复按钮在新章节静默失效（跨章 vid 404）
 const vm = $("#version-modal"); if (vm) vm.classList.add("hidden");
 const dm = $("#diff-modal"); if (dm) dm.classList.add("hidden");
 // 切章节时关闭 plan 模式（避免跨章节残留）
 if (_planMode) {
 _planMode = false;
 const pt = document.getElementById("ed-plan-toggle");
 if (pt) pt.classList.remove("active");
 }
 // 取消上一次未完成的 AI 流式读取 (A2)
 if (window._aiEditAbortController) {
 try { window._aiEditAbortController.abort(); } catch (_) {}
 window._aiEditAbortController = null;
 }
 STATE_EDITOR.chapterIdx = idx;
 // 切章节前：保存【当前章节】的 scrollTop（而非新 idx）。
 // 旧代码 saveLastChapter(idx, 0) 会清掉新 idx 的历史位置——那是我们要恢复的。
 const _oldIdx = STATE_EDITOR.prevLoadIdx;
 const _ta0 = $("#ed-text");
 if (_oldIdx && _oldIdx !== idx && _ta0) {
 saveChapterScroll(_oldIdx, _ta0.scrollTop);
 }
 STATE_EDITOR.prevLoadIdx = idx;
 try {
 // 加载态：清旧内容 + 标题写"加载中"，避免 await 期间显示旧章节
 const ta0 = $("#ed-text");
 if (ta0) ta0.value = "";
 const lab0 = $("#ed-chapter-label");
 if (lab0) lab0.textContent = `第 ${idx} 回 · 加载中…`;
 setEditorStatus(" 加载中…", false);
 const r = await API.get(`/editor/chapter/${idx}`);
 if (myReqId !== _loadChapterReqId) return; // 已被新请求取代, 丢弃
 const ch = r.chapter;
 STATE_EDITOR.text = r.text || "";
 STATE_EDITOR.savedText = r.text || "";
 await initVersionTracking(ch.idx, r.text || "");
 resetUndoStack();
 // 种子快照: undo 的终点 = 本章加载时的原文（否则最早一段编辑无法撤销回加载态）
 _undoStack.push({ value: r.text || "", scrollTop: 0, selectionStart: 0, selectionEnd: 0, label: "载入原文", t: Date.now() });
 updateUndoButton();
 STATE_EDITOR.lastAiText = "";
 STATE_EDITOR.prevIdx = r.prev_idx;
 STATE_EDITOR.nextIdx = r.next_idx;
 // 更新导航按钮状态
 const prevBtn = document.getElementById("ed-prev");
 const nextBtn = document.getElementById("ed-next");
 if (prevBtn) { prevBtn.disabled = !r.prev_idx; prevBtn.style.opacity = r.prev_idx ? "1" : "0.4"; }
 // "→" 在最后一章时不禁用：改为引导"新建并写下一章"
 if (nextBtn) {
  nextBtn.disabled = false;
  nextBtn.style.opacity = "1";
  if (!r.next_idx) {
   nextBtn.title = `写第 ${(ch.idx||1)+1} 回（新建）`;
   nextBtn.textContent = "+";
  } else {
   nextBtn.title = "下一回 (Alt+→)";
   nextBtn.textContent = "→";
  }
 }
 // P0-#72: 切章节清 AI 统计
 try { resetAiStats(); } catch (_) {}
 // v1.19.23: 标题写到 ed-chapter-label (顶条); 旧版 ed-title 兼容
 const _titleEl = $("#ed-title") || $("#ed-chapter-label");
 if (_titleEl) _titleEl.textContent = `第 ${ch.idx} 回 · ${ch.title || ""}`;
 $("#ed-meta").textContent = `${(ch.word_count || 0).toLocaleString()} 字 · ${ch.location || "(无)"} · 状态: ${r.consistency ? (r.consistency.passed ? "✓ 一致性通过" : "! 有问题") : "未扫描"}`;
 $("#ed-text").value = r.text || "";
 // * 顺带修 #10：placeholder 跟随当前章节
 $("#ed-text").placeholder = `开始写第 ${ch.idx} 回 · ${ch.title || ""}…`;
 // * #7：切章节时恢复上次滚动位置（per-idx 记忆，能记多章）
 // 用 rAF 等 DOM 渲染完再设置，否则 scrollTop 会被重置
 const _savedScroll = loadChapterScroll(idx);
 requestAnimationFrame(() => {
 const ta = $("#ed-text");
 if (ta && _savedScroll > 0) {
 ta.scrollTop = _savedScroll;
 }
 });
 $("#ed-chapter-label").textContent = `第 ${ch.idx} 回 · ${ch.title || ""}`;
 const _curChap = $("#ed-cur-chap");
 if (_curChap) _curChap.textContent = ch.idx;
 updateEditorStats();
 setEditorStatus("● 已保存", false);
 _applyPendingAiPrompt(); // 评审转修改：正文就绪后再注入指令框
 // B-优22: 重渲染左侧章列表 (高亮当前章节)
 renderEditorChapterList();
 // 清诊断 + 重渲染
 const issuesEl = $("#ed-issues");
 issuesEl.innerHTML = "";
 if (r.consistency && r.consistency.issues && r.consistency.issues.length) {
 issuesEl.innerHTML = `<div style="margin-bottom:6px;color:var(--fg-muted);font-size:11px">上次扫描: ${r.consistency.issues.length} 个问题</div>`;
 r.consistency.issues.forEach(it => {
 issuesEl.insertAdjacentHTML("beforeend",
 `<div class="ed-issue-mini ${it.severity||'low'}">
 <div class="eim-title">${ESC(it.explanation ? it.explanation.slice(0, 50) : it.category || '问题')}</div>
 <div class="eim-meta">[${it.severity||'low'}] ${ESC(it.category || '')}</div>
 </div>`);
 });
 } else if (r.threads && r.threads.length) {
 issuesEl.innerHTML = `<div style="margin-bottom:6px;color:var(--fg-muted);font-size:11px">本章衔接 ${r.threads.length} 条线</div>`;
 } else {
 issuesEl.innerHTML = '<p style="color:var(--fg-dim);font-size:11px">未扫描。点"扫描本章"运行。</p>';
 }
 renderOutline(r.outline, r.text);
 STATE_EDITOR.outline = r.outline;
 renderChapterComments(idx);
 $("#ed-ai-stream").innerHTML = '<div style="padding:20px;text-align:center"><p style="color:var(--fg-muted);font-size:13px;margin-bottom:8px">在下方输入框告诉 AI 怎么改</p><p style="color:var(--fg-dim);font-size:11px;line-height:1.6">例如：把第三段对话改得更口语化<br>或选中一段文字后点浮动按钮<br>或用快捷命令：润色 / 一致 / 紧凑 / 心理</p></div>';
 $("#ed-ai-actions").style.display = "none";
 addLog("info", `[editor] 加载第 ${ch.idx} 回 (${(r.text || '').length} 字)`);
 } catch (e) {
 if (myReqId !== _loadChapterReqId) return; // 老请求失败也不响
 // 切章节失败: 清旧章节所有残留 (A3)
 STATE_EDITOR.text = "";
 STATE_EDITOR.savedText = "";
 await initVersionTracking(idx, "");
 resetUndoStack();
 $("#ed-text").value = "";
 const _titleEl2 = $("#ed-title") || $("#ed-chapter-label");
 if (_titleEl2) _titleEl2.textContent = `第 ${idx} 回 · 加载失败`;
 $("#ed-meta").textContent = String(e.message || e);
 $("#ed-ai-stream").innerHTML = '<p class="placeholder" style="font-size:11px">章节加载失败, AI 不可用</p>';
 $("#ed-ai-actions").style.display = "none";
 const issuesEl = $("#ed-issues");
 issuesEl.innerHTML = `<p class="placeholder" style="color:var(--danger);font-size:11px">! ${ESC(e.message || e)}</p>`;
 addLog("error", `[editor] 加载第 ${idx} 回失败: ${e.message || e}`);
 }
}

// B-优22: 渲染左侧章列表 (高亮当前章, 一点直接切)
function renderEditorChapterList() {
 const el = document.getElementById("ed-chapter-list");
 if (!el) return;
 const chs = STATE.chapters || [];
 if (!chs.length) {
 el.innerHTML = '<p class="placeholder" style="font-size:11px;color:var(--fg-muted)">还没章节</p>';
 return;
 }
 const cur = STATE_EDITOR.chapterIdx;
 // 新建下一章按钮（基于最大 idx + 1）
 const maxIdx = Math.max(...chs.map(c => c.idx));
 const nextIdx = maxIdx + 1;
 el.innerHTML = chs.map(c => {
 const active = c.idx === cur ? " active" : "";
 return `<div class="ed-cl-item${active}" data-idx="${c.idx}">
 <span class="ed-cl-idx">第 ${c.idx} 回</span>
 <span class="ed-cl-title">${ESC(c.title || "")}</span>
 <span class="ed-cl-meta">${(c.word_count || 0).toLocaleString()}</span>
 </div>`;
 }).join("") + `<div class="ed-cl-item ed-cl-new" data-new-idx="${nextIdx}" style="border-style:dashed;color:var(--accent);justify-content:center;font-size:12px">+ 写第 ${nextIdx} 回</div>`;
 el.querySelectorAll(".ed-cl-item").forEach(it => {
 it.onclick = () => {
 const idx = parseInt(it.dataset.idx, 10);
 if (!isNaN(idx)) { loadEditorChapter(idx); return; }
 const newIdx = parseInt(it.dataset.newIdx, 10);
 if (!isNaN(newIdx)) writeNextChapter(newIdx);
 };
 });
}

async function writeNextChapter(newIdx) {
 if (!confirm(`AI 写第 ${newIdx} 章？\n将用完整管线生成正文（约 30-90 秒），你能实时看到 AI 写的文字。`)) return;
 try {
 // 先新建空章节（若大纲已有则跳过）
 await API.post("/chapter/new", {idx: newIdx, title: `第${newIdx}章`}).catch(() => {});
 await streamWriteChapter(newIdx);
 } catch (e) { toastError("写章失败", e); }
}

// SSE 流式写章：在编辑器 AI 面板实时展示 AI 正在写的正文
async function streamWriteChapter(idx) {
 // 确保在编辑器视图
 if (CURRENT.target !== "editor") goto("editor");
 // 切到 AI tab
 const aiTab = Array.from(document.querySelectorAll(".ed-tab")).find(t => t.textContent.includes("AI"));
 if (aiTab) aiTab.click();
 const stream = $("#ed-ai-stream");
 if (!stream) { showToast("AI 面板未找到"); return; }

 stream.innerHTML = "";
 // 取消按钮
 const cancelRow = document.createElement("div");
 cancelRow.style.cssText = "display:flex;justify-content:flex-end;margin-bottom:4px";
 const cancelBtn = document.createElement("button");
 cancelBtn.className = "btn small";
 cancelBtn.textContent = "取消";
 cancelBtn.style.display = "none";
 let writeAborted = false;
 cancelBtn.onclick = () => { writeAborted = true; prog.textContent = "已取消"; cancelBtn.style.display = "none"; };
 cancelRow.appendChild(cancelBtn);
 stream.appendChild(cancelRow);
 // 进度气泡
 const prog = document.createElement("div");
 prog.className = "ed-bubble ed-bubble-tool";
 prog.style.cssText = "font-size:12px;color:var(--fg-muted);margin-bottom:6px";
 prog.textContent = "准备写章…";
 stream.appendChild(prog);
 // 正文气泡
 const body = document.createElement("div");
 body.className = "ed-bubble ed-bubble-ai";
 body.style.cssText = "white-space:pre-wrap;font-size:14px;line-height:1.8;max-height:60vh;overflow-y:auto;font-family:var(--font-serif)";
 stream.appendChild(body);

 let fullText = "";
 let charCount = 0;
 cancelBtn.style.display = "";

 try {
 const resp = await fetch(`/api/chapter/${idx}/write`, {
 method: "POST",
 headers: {"Content-Type": "application/json"},
 body: JSON.stringify({target_words: _getChapterTargetWords(idx), auto_fix: true}),
 });
 const reader = resp.body.getReader();
 // 启动取消按钮
 const decoder = new TextDecoder();
 let buffer = "";
 let parseErrors = 0;
 writeAborted = false;

 while (true) {
 if (writeAborted) { try { reader.cancel(); } catch(_){} break; }
 const {done, value} = await reader.read();
 if (done) break;
 buffer += decoder.decode(value, {stream: true});
 const lines = buffer.split("\n");
 buffer = lines.pop() || "";
 for (const line of lines) {
 if (!line.startsWith("data:")) continue;
 const dataStr = line.slice(5).trim();
 if (!dataStr) continue;
 try {
 const evt = JSON.parse(dataStr);
 if (evt.phase) {
 prog.textContent = evt.msg || evt.phase;
 } else if (evt.chunk) {
 fullText += evt.chunk;
 charCount += evt.chunk.length;
 body.textContent = fullText;
 body.scrollTop = body.scrollHeight;
 prog.textContent = `AI 正在写… ${charCount} / ${_getChapterTargetWords(idx)} 字`;
 } else if (evt.done) {
 cancelBtn.style.display = "none";
 prog.textContent = `完成！${evt.word_count} 字，一致性重试 ${evt.retries || 0} 次`;
 prog.style.color = "var(--success)";
 showToast(`第 ${idx} 章写完了（${evt.word_count} 字）`);
 // 完成后显示操作按钮
 const actions = document.createElement("div");
 actions.style.cssText = "margin-top:8px;display:flex;gap:8px;flex-wrap:wrap";
 actions.innerHTML = `<button class="btn small primary" onclick="loadEditorChapter(${idx});document.getElementById('ed-tab-ai')?.click()">查看本章</button>`;
 stream.appendChild(actions);
 // 自动刷新编辑器加载新内容
 setTimeout(() => loadEditorChapter(idx), 800);
 } else if (evt.error) {
 prog.innerHTML = `<span class="spinner" style="border-top-color:var(--danger)"></span> <span style="color:var(--danger)">错误：${ESC(evt.error)}</span>`;
 // 错误时显示重试按钮
 const retryBtn = document.createElement("button");
 retryBtn.className = "btn small";
 retryBtn.style.marginTop = "8px";
 retryBtn.textContent = "重试";
 retryBtn.onclick = () => streamWriteChapter(idx);
 stream.appendChild(retryBtn);
 }
 } catch (e) {
 if (++parseErrors < 3) addLog("warn", `[write-stream] 解析失败`);
 }
 }
 }
 } catch (e) {
 prog.textContent = `错误：${e.message || e}`;
 prog.style.color = "var(--danger)";
 }
}

function updateEditorStats() {
 const txt = $("#ed-text").value;
 // 精确统计：CJK 字 + 英文词（正确处理 emoji/生僻字代理对）
 const cur = countCharsAccurate(txt);
 const saved = countCharsAccurate(STATE_EDITOR.savedText);
 const len = cur.total;
 const delta = len - saved.total;
 // 显示：纯中文只显 "N 字"，含英文时追加 "+M 词"
 const enPart = cur.enWords > 0 ? ` · ${cur.enWords} 词` : "";
 $("#ed-words").textContent = `${cur.cjk.toLocaleString()} 字${enPart}`;
 $("#ed-words-delta").textContent = delta === 0 ? "" : (delta > 0 ? `+${delta}` : `${delta}`);
 setEditorStatus(delta === 0 ? "● 已保存" : "● 未保存", delta !== 0);
 updateTargetDisplay();
 // 实时检查要点覆盖率（每次改稿后跑一次，轻量：字符串 substring）
 if (STATE_EDITOR.outline) updateOutlineCoverage(STATE_EDITOR.outline, txt);
}

/* ===== 编辑器右侧：本章要点 ===== */

/**
 * 渲染要点面板
 * outline = { outline, summary, location, pov_character, related_characters, key_events, volume }
 */
function renderOutline(outline, currentText) {
 const el = $("#ed-outline-content");
 if (!el) return;
 if (!outline || typeof outline !== "object") {
 // P1-G: outline 是 undefined / null / 字符串都走安全路径
 el.innerHTML = '<p class="placeholder" style="font-size:11px;color:var(--fg-dim)">本章暂无要点数据。<br>先做一次"提取事件"扫描即可自动生成。</p>';
 updateOutlineCoverage(null, currentText);
 return;
 }
 const html = [];
 // 卷标签
 if (outline.volume && outline.volume.title) {
 html.push(`<div class="outline-vol"> ${ESC(outline.volume.title)}</div>`);
 }
 // 视角
 if (outline.pov_character) {
 html.push(`
 <div class="outline-section">
 <div class="outline-h">视角</div>
 <span class="outline-pov">${ESC(outline.pov_character.name)}</span>
 ${outline.pov_character.role ? `<div class="outline-text" style="font-size:11px;color:var(--fg-dim);margin-top:4px">${ESC(outline.pov_character.role)}</div>` : ""}
 </div>
 `);
 }
 // 地点
 if (outline.location) {
 html.push(`
 <div class="outline-section">
 <div class="outline-h">地点</div>
 <div class="outline-text"> ${ESC(outline.location)}</div>
 </div>
 `);
 }
 // 大纲/概要
 if (outline.outline) {
 html.push(`
 <div class="outline-section">
 <div class="outline-h">大纲</div>
 <div class="outline-text">${ESC(outline.outline)}</div>
 </div>
 `);
 } else if (!outline.summary) {
 html.push(`
 <div class="outline-section">
 <div class="outline-h">大纲</div>
 <div class="outline-text empty">未填写</div>
 </div>
 `);
 }
 if (outline.summary) {
 html.push(`
 <div class="outline-section">
 <div class="outline-h">概要</div>
 <div class="outline-text">${ESC(outline.summary)}</div>
 </div>
 `);
 }
 // 涉及人物
 if (outline.related_characters && outline.related_characters.length) {
 const charChips = outline.related_characters.map(c => {
 const mentioned = _nameInText(c.name, c.aliases, currentText);
 return `<span class="outline-char${mentioned ? " mentioned" : " missing"}" data-name="${ESC(c.name)}" title="${mentioned ? "✓ 文中已提及" : "! 文中未提及"}">${mentioned ? "✓ " : "! "}${ESC(c.name)}</span>`;
 }).join("");
 html.push(`
 <div class="outline-section">
 <div class="outline-h">涉及人物（${outline.related_characters.length}）</div>
 <div class="outline-char-list">${charChips}</div>
 </div>
 `);
 }
 // 关键事件
 if (outline.key_events && outline.key_events.length) {
 const evItems = outline.key_events.map(e => {
 const mentioned = _nameInText(e.title, [], currentText);
 return `
 <div class="outline-event${mentioned ? " mentioned" : " missing"}" data-title="${ESC(e.title)}" title="${mentioned ? "✓ 文中已提及" : "! 文中未提及"}">
 <div class="outline-event-title">${mentioned ? "✓ " : "! "}${ESC(e.title)}</div>
 ${e.summary ? `<div class="outline-event-summary">${ESC(e.summary)}</div>` : ""}
 <div class="outline-event-meta">${e.event_type || ""}${e.location ? " · " + e.location : ""}${e.importance ? " · 重要度 " + e.importance : ""}</div>
 </div>
 `;
 }).join("");
 html.push(`
 <div class="outline-section">
 <div class="outline-h">关键事件（${outline.key_events.length}）</div>
 ${evItems}
 </div>
 `);
 }
 el.innerHTML = html.join("") || '<p class="placeholder" style="font-size:11px;color:var(--fg-dim)">本章暂无要点数据</p>';
 updateOutlineCoverage(outline, currentText);
}

/**
 * 名字是否出现在文本里（轻量 substring + 别名）
 */
function _nameInText(name, aliases, text) {
 if (!name || !text) return false;
 if (text.includes(name)) return true;
 if (aliases && aliases.length) {
 for (const a of aliases) {
 if (a && text.includes(a)) return true;
 }
 }
 return false;
}

/**
 * 实时覆盖率：数 (人物 + 事件) 中"已提及"的占比
 */
function updateOutlineCoverage(outline, text) {
 const el = $("#ed-outline-coverage");
 if (!el) return;
 if (!outline) {
 el.textContent = "—";
 el.className = "ed-outline-coverage";
 return;
 }
 let total = 0, hit = 0;
 for (const c of (outline.related_characters || [])) {
 total++;
 if (_nameInText(c.name, c.aliases, text)) hit++;
 }
 for (const e of (outline.key_events || [])) {
 total++;
 if (_nameInText(e.title, [], text)) hit++;
 }
 if (total === 0) {
 el.textContent = "无要点";
 el.className = "ed-outline-coverage";
 return;
 }
 const pct = Math.round(hit / total * 100);
 el.textContent = `${hit}/${total} ✓`;
 el.title = `本章文本里命中了 ${hit}/${total} 个要点（${pct}%）`;
 el.className = "ed-outline-coverage " + (pct >= 80 ? "cov-full" : pct >= 40 ? "cov-mid" : "cov-low");
}

/* ===== 章节目标字数 ===== */
const STATE_TARGETS = {
 // key = chapter idx (number) ; val = number 目标字数
 byChapter: {},
 loadFromStorage() {
 try {
 const raw = localStorage.getItem("novelai:chapter-targets");
 if (raw) this.byChapter = JSON.parse(raw);
 } catch (e) { this.byChapter = {}; }
 },
 saveToStorage() {
 try {
 localStorage.setItem("novelai:chapter-targets", JSON.stringify(this.byChapter));
 } catch (e) {}
 },
 get(idx) { return this.byChapter[idx] || null; },
 set(idx, val) {
 if (val && val > 0) this.byChapter[idx] = val;
 else delete this.byChapter[idx];
 this.saveToStorage();
 },
};

function updateTargetDisplay() {
 const idx = STATE_EDITOR.chapterIdx;
 const target = idx != null ? STATE_TARGETS.get(idx) : null;
 const len = ($("#ed-text").value || "").length;
 const pct = target ? Math.min(100, Math.round(len / target * 100)) : 0;

 // v1.19.23: 始终更新顶条目标 mini 控件（这是实际可见的 UI）
 const miniFill = document.getElementById("ed-target-mini-fill");
 const miniText = document.getElementById("ed-target-mini-text");
 if (miniFill) miniFill.style.width = (target ? pct : 0) + "%";
 if (miniText) miniText.textContent = target ? `${pct}%` : "未设";

 // 旧版全宽目标控件（可能不存在于新版 HTML）
 const wrap = $("#ed-target-wrap");
 const txt = $("#ed-target-text");
 const fill = $("#ed-target-fill");
 if (!wrap || !txt || !fill) return;
 if (!target) {
 txt.textContent = " 未设目标";
 fill.style.width = "0%";
 fill.className = "";
 wrap.className = "ed-target-wrap";
 wrap.title = "点击设置本章目标字数";
 return;
 }
 fill.style.width = pct + "%";
 fill.className = pct < 50 ? "t-low" : pct < 100 ? "t-mid" : "t-good";
 wrap.className = "ed-target-wrap" + (pct >= 100 ? " t-done" : (pct < 30 && len > 0 ? " t-warn" : ""));
 if (pct >= 100) {
 txt.textContent = ` ${len.toLocaleString()}/${target.toLocaleString()} (${pct}%) ✓`;
 } else {
 txt.textContent = ` ${len.toLocaleString()}/${target.toLocaleString()} (${pct}%)`;
 }
 wrap.title = `本章目标 ${target.toLocaleString()} 字 · 当前 ${len.toLocaleString()} 字 (${pct}%)\n点击修改目标`;
}

function showTargetDialog() {
 const idx = STATE_EDITOR.chapterIdx;
 if (idx == null) return;
 const cur = STATE_TARGETS.get(idx) || "";
 const globalDefault = localStorage.getItem("novelai:default-target-words") || CHAPTER_TARGET_WORDS;
 showModal(`设置第 ${idx} 回目标字数`, `
 <p style="color:var(--fg-muted);margin-bottom:8px">目标字数会影响 AI 写章时生成的篇幅。留空表示使用默认值。</p>
 <div class="form-row"><label>本章目标字数</label>
 <input type="number" id="target-input" min="0" step="500" value="${cur}" placeholder="如 10000（留空=默认）" style="padding:8px 11px;background:var(--bg-card);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur-webkit);border:1px solid var(--border-glass);border-radius:var(--radius);color:var(--fg);font-size:14px">
 </div>
 <div class="form-row" style="margin-top:8px"><label>全局默认字数（新章节）</label>
 <input type="number" id="target-default" min="0" step="500" value="${globalDefault}" placeholder="如 10000" style="padding:8px 11px;background:var(--bg-card);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur-webkit);border:1px solid var(--border-glass);border-radius:var(--radius);color:var(--fg);font-size:14px">
 </div>
 <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">
 <button class="btn small" data-quick="5000">5千</button>
 <button class="btn small" data-quick="10000">1万</button>
 <button class="btn small" data-quick="15000">1.5万</button>
 <button class="btn small" data-quick="20000">2万</button>
 </div>
 `, () => {
 const input = $("#target-input").value;
 const defInput = $("#target-default").value;
 // 全局默认
 if (defInput) {
 const dn = parseInt(defInput, 10);
 if (dn > 0) {
 localStorage.setItem("novelai:default-target-words", String(dn));
 addLog("done", `[target] 全局默认字数设为 ${dn.toLocaleString()}`);
 }
 }
 // 本章目标
 const n = parseInt(input, 10);
 if (isNaN(n) || n <= 0) {
 STATE_TARGETS.set(idx, 0);
 addLog("info", `[target] 第 ${idx} 回目标已清除（使用默认）`);
 } else {
 STATE_TARGETS.set(idx, n);
 addLog("done", `[target] 第 ${idx} 回目标设为 ${n.toLocaleString()} 字`);
 }
 updateTargetDisplay();
 hideModal();
 });
 // 快捷按钮绑定（不用 inline onclick，兼容 PyWebView）
 document.querySelectorAll("#cmd-body [data-quick]").forEach(btn => {
 btn.addEventListener("click", () => {
 const inp = document.getElementById("target-input");
 if (inp) inp.value = btn.dataset.quick;
 });
 });
}

function setEditorStatus(text, dirty) {
 const el = $("#ed-status");
 if (el) {
 el.textContent = text;
 el.classList.toggle("dirty", !!dirty);
 }
 // 同步到工具栏状态指示区（脉冲点 + 文字）
 const tb = $("#ed-tb-status");
 if (tb) {
 const txt = tb.querySelector(".status-text");
 if (txt) txt.textContent = text;
 tb.classList.toggle("dirty", !!dirty);
 }
}

/** 工具栏状态切"忙"（AI 生成中）。busy=true 加 .busy（蓝点脉冲），false 移除。 */
function setToolbarBusy(busy) {
 const tb = $("#ed-tb-status");
 if (tb) tb.classList.toggle("busy", !!busy);
}

let _savingInFlight = false; // 防并发保存覆盖
let _analyzingInFlight = false; // v1.19.24: 防并发扫描 (避免多次点 触发后端过载)
async function editorSave(opts = {}) {
 const idx = STATE_EDITOR.chapterIdx;
 if (!idx || idx < 1 || isNaN(idx)) return;
 if (_savingInFlight) { addLog("warn", "[editor] 保存中, 请稍等"); return; }
 const text = $("#ed-text").value;
 _savingInFlight = true;
 const saveBtn = document.getElementById("ed-btn-save");
 if (saveBtn) { saveBtn.disabled = true; saveBtn.style.opacity = "0.6"; }
 setEditorStatus(" 保存中…", true);
 try {
 await API.post(`/editor/chapter/${idx}/save`, {text});
 // 注：save 端点已自动建一版（source=save），无需前端再 recordVersion。
 // 版本列表以"打开历史弹窗时重拉"为准（见 refreshVersionListFromServer），避免本地/后端不同步。
 STATE_EDITOR.savedText = text;
 STATE_EDITOR.lastSavedText = text; // 用作未保存检测
 // 刷新本地版本列表（badge 计数需要同步）
 try { await refreshVersionListFromServer(); } catch (_) {}
 setEditorStatus("● 已保存", false);
 addLog("done", `[editor] 第 ${idx} 回已保存（${text.length} 字）`);
 // 自动保存(quiet)不弹 toast: 写作中每 30s 弹一次很打扰, 状态栏已有反馈; 手动保存才弹
 if (!opts.quiet) showToast(`已保存 · 第 ${idx} 回 ${text.length} 字`, "success");
 const sb = document.getElementById("ed-btn-save");
 if (sb) { sb.disabled = false; sb.style.opacity = "1"; }
 } catch (e) {
 addLog("error", `[editor] 保存失败: ${e.message || e}`);
 toastError("保存失败", e);
 } finally {
 _savingInFlight = false;
 const saveBtn = document.getElementById("ed-btn-save");
 if (saveBtn) { saveBtn.disabled = false; saveBtn.style.opacity = "1"; }
 }
}

async function editorAnalyze() {
 const idx = STATE_EDITOR.chapterIdx;
 if (!idx) return;
 if (_analyzingInFlight) { addLog("warn", "[editor] 扫描中, 请稍等"); return; }
 _analyzingInFlight = true;
 const text = $("#ed-text").value;
 const issuesEl = $("#ed-issues");
 issuesEl.innerHTML = '<p style="color:var(--fg-muted);font-size:11px">扫描中…</p>';
 try {
 const r = await API.post(`/editor/chapter/${idx}/analyze`, {text});
 const sev = r.by_severity || {}; // E1: 后端漏返 by_severity 不再抛
 const high = sev.high || 0, med = sev.medium || 0, low = sev.low || 0;
 issuesEl.innerHTML = `<div style="margin-bottom:6px;color:var(--fg-muted);font-size:11px">扫描结果：●${high} ●${med} ●${low}</div>`;
 if (!r.issues.length) {
 issuesEl.innerHTML += '<p style="color:var(--success);font-size:11px">✓ 无问题</p>';
 } else {
 r.issues.forEach(it => {
 issuesEl.insertAdjacentHTML("beforeend",
 `<div class="ed-issue-mini ${it.severity||'low'}">
 <div class="eim-title">${ESC((it.explanation || '').slice(0, 50))}</div>
 <div class="eim-meta">[${it.severity||'low'}] ${ESC(it.category || '')}</div>
 </div>`);
 });
 }
 } catch (e) {
 issuesEl.innerHTML = `<p style="color:var(--danger);font-size:11px">扫描失败: ${ESC(e.message || e)}</p>`;
 } finally {
 _analyzingInFlight = false;
 }
}

// ============== 每日编辑简报 (Phase 1) ==============
// 打开时一次性返: 硬关联 + LLM 建议 + 自动疑点
// 缓存 10 分钟 (后端); 用户可点 "↻ 重算" 跳过缓存
let _briefInFlight = false;

function showBriefModal() {
 $("#brief-modal").classList.remove("hidden");
 $("#brief-loading").style.display = "";
 $("#brief-content").style.display = "none";
 $("#brief-cache-tag").style.display = "none";
}

function hideBriefModal() {
 $("#brief-modal").classList.add("hidden");
}

function renderBrief(payload) {
 $("#brief-loading").style.display = "none";
 $("#brief-content").style.display = "";
 const tag = $("#brief-cache-tag");
 if (payload.cached) {
 tag.style.display = "";
 tag.textContent = `缓存 (${payload.cache_age_seconds}s 前)`;
 } else {
 tag.style.display = "none";
 }

 // 1) 硬关联
 const hard = payload.hard_context || {};
 let hardHTML = "";
 // 上一章未完成动作
 if (hard.prev_unfinished_action) {
 hardHTML += `<div style="margin-bottom:10px;padding:8px 10px;background:var(--bg-elevated);border-left:3px solid var(--warning);border-radius:4px">
 <div style="font-size:11px;color:var(--fg-dim);margin-bottom:2px"> 上一章未完成动作</div>
 <div style="font-size:12px;line-height:1.6">${ESC(hard.prev_unfinished_action)}</div>
 </div>`;
 } else {
 hardHTML += `<div style="margin-bottom:10px;color:var(--fg-dim);font-size:11px"> 上一章无未完成动作 (或第 1 章)</div>`;
 }
 // 本章时间线位置
 const tp = hard.timeline_position || {};
 if (tp.chapter_idx) {
 hardHTML += `<div style="margin-bottom:10px;padding:8px 10px;background:var(--bg-elevated);border-left:3px solid var(--accent);border-radius:4px">
 <div style="font-size:11px;color:var(--fg-dim);margin-bottom:2px"> 本章时间线位置</div>
 <div style="font-size:12px">第 <b>${tp.chapter_idx}</b> 回 / 共 ${tp.total} 回 (<b>${tp.pct}%</b> 进度)</div>
 <div style="font-size:11px;color:var(--fg-muted);margin-top:2px">故事内时间: ${tp.story_time_range ? `[${tp.story_time_range[0]}, ${tp.story_time_range[1]}]` : "未设"}</div>
 </div>`;
 }
 // 涉及人物
 const chars = hard.related_characters || [];
 if (chars.length) {
 hardHTML += `<div style="margin-bottom:10px">
 <div style="font-size:11px;color:var(--fg-dim);margin-bottom:4px"> 涉及人物 (${chars.length})</div>`;
 chars.forEach(c => {
 const recent = (c.recent_appearances || []).slice(0, 3).map(r => `第${r.chapter_idx}回`).join(", ") || "无最近出场";
 hardHTML += `<div style="padding:4px 8px;background:var(--bg-elevated);border-radius:4px;margin-bottom:4px;font-size:12px">
 <span style="color:var(--accent)">${ESC(c.name)}</span>
 <span style="color:var(--fg-dim);font-size:11px"> · ${ESC(c.role || "—")} · ${ESC(c.mbti || "—")}</span>
 <span style="color:var(--fg-dim);font-size:11px;margin-left:6px">最近: ${ESC(recent)}</span>
 </div>`;
 });
 hardHTML += `</div>`;
 }
 // 关联伏笔
 const threads = hard.related_threads || [];
 if (threads.length) {
 hardHTML += `<div>
 <div style="font-size:11px;color:var(--fg-dim);margin-bottom:4px"> 关联伏笔 (${threads.length})</div>`;
 threads.forEach(t => {
 const relBadge = {planted: "● 新种", payoff: "● 揭晓", other: "○ 关联"}[t.relation] || "○";
 hardHTML += `<div style="padding:4px 8px;background:var(--bg-elevated);border-radius:4px;margin-bottom:4px;font-size:12px">
 <span class="badge ${ESC(t.status)}" style="font-size:11px">${ESC(t.status)}</span>
 ${relBadge} <span style="color:var(--accent)">${ESC(t.title)}</span>
 <div style="color:var(--fg-muted);font-size:11px;margin-top:2px">${ESC(t.description || "")}</div>
 </div>`;
 });
 hardHTML += `</div>`;
 }
 $("#brief-hard").innerHTML = hardHTML || '<p class="placeholder">无</p>';

 // 2) LLM 建议
 const llm = payload.llm_suggestions || [];
 let llmHTML = "";
 if (!llm.length) {
 llmHTML = '<p class="placeholder" style="font-size:12px">（LLM 暂不可用 — 可能未配 API key，或调用失败）</p>';
 } else {
 const typeIcon = {pacing: "", character: "", dialogue: "", consistency: "", "_meta": "!"};
 llm.forEach(s => {
 const t = s.type || "其他";
 const icon = typeIcon[t] || "";
 llmHTML += `<div style="padding:8px 10px;background:var(--bg-elevated);border-left:3px solid var(--accent-soft);border-radius:4px;margin-bottom:6px;font-size:12px;line-height:1.6">
 <span style="font-size:11px;color:var(--fg-dim);margin-right:6px">${icon} ${ESC(t)}</span>
 ${ESC(s.suggestion || "")}
 </div>`;
 });
 }
 $("#brief-llm").innerHTML = llmHTML;

 // 3) 自动疑点
 const issues = payload.issues || {items: [], n_total: 0, by_severity: {}};
 let issHTML = "";
 const sev = issues.by_severity || {};
 if (!issues.n_total) {
 issHTML = '<p class="placeholder" style="font-size:12px;color:var(--success)">✓ 规则引擎未发现疑点</p>';
 } else {
 issHTML = `<div style="margin-bottom:8px;font-size:11px;color:var(--fg-dim)">
 总计 <b>${issues.n_total}</b> 条 · ●${sev.high||0} ●${sev.medium||0} ●${sev.low||0}
 </div>`;
 issues.items.forEach(it => {
 const cls = it.severity || "low";
 const fix = it.fix_suggestion ? `<div style="color:var(--fg-muted);font-size:11px;margin-top:2px"> ${ESC(it.fix_suggestion)}</div>` : "";
 issHTML += `<div style="padding:6px 10px;background:var(--bg-elevated);border-left:3px solid var(--${cls==='high'?'danger':cls==='medium'?'warning':'success'});border-radius:4px;margin-bottom:6px;font-size:12px">
 <span style="font-size:11px;color:var(--fg-dim)">[${ESC(cls)}] ${ESC(it.category || '')}</span>
 <div style="margin-top:2px;line-height:1.5">${ESC(it.explanation || '')}</div>
 ${fix}
 </div>`;
 });
 }
 $("#brief-issues").innerHTML = issHTML;

 // 耗时
 const ms = payload.elapsed_ms || {};
 $("#brief-elapsed").textContent = `耗时: 硬关联 ${ms.hard ?? "?"}ms · 疑点 ${ms.issues ?? "?"}ms · LLM ${ms.llm ?? "?"}ms`;
}

async function editorDailyBrief(refresh = false) {
 const idx = STATE_EDITOR.chapterIdx;
 if (!idx) { addLog("warn", "[brief] 当前未在编辑任何章节"); return; }
 if (_briefInFlight) { addLog("warn", "[brief] 已在加载, 请稍等"); return; }
 _briefInFlight = true;
 showBriefModal();
 try {
 const url = `/editor/chapter/${idx}/daily-brief${refresh ? "?refresh=1" : ""}`;
 const r = await API.get(url, LLM_TIMEOUT_MS); // LLM 生成简报, 30s 默认超时不够
 renderBrief(r);
 addLog("done", `[brief] 第 ${idx} 章简报已生成 (缓存=${r.cached}, LLM=${r.llm_suggestions?.length || 0}条, 疑点=${r.issues?.n_total || 0})`);
 } catch (e) {
 $("#brief-loading").style.display = "none";
 $("#brief-content").style.display = "";
 $("#brief-hard").innerHTML = `<p class="placeholder" style="color:var(--danger)">✕ 加载失败: ${ESC(e.message || e)}</p>`;
 addLog("error", "[brief] 加载失败: " + (e.message || e));
 } finally {
 _briefInFlight = false;
 }
}

let _aiStreaming = false; // 防 AI 流并发 (多次点改/回车导致面板混乱)
let _lastAiInstruction = null; // 上一次 AI 指令（供"重试"按钮复用）
let _lastAiSelection = null; // 上一次是否 inline 模式（重试时复用选区）
let _planMode = false; // Plan 模式开关：开启后发送的指令走 /ai-plan（先规划后执行）
async function sendEditInstruction(instruction) {
 const idx = STATE_EDITOR.chapterIdx;
 if (!idx) return false;
 if (_aiStreaming) {
 addLog("warn", "[ai] 上一次 AI 改稿还在跑, 请等完成或刷新");
 showToast("AI 正在运行，请等完成", "warning");
 return false;
 }
 // Plan 模式分流：走 /ai-plan 端点（先规划后执行），不走 ai-edit
 if (_planMode) {
 return await sendPlanRequest(instruction);
 }
 const currentText = $("#ed-text").value;
 if (!currentText.trim()) {
 // 正文为空：不是"改稿"而是"写稿"——走 streamWriteChapter 让 AI 从零撰写
 addLog("info", "[ai] 正文为空，切换到 AI 写稿模式");
 showToast("正文为空，AI 将根据章节大纲从零撰写全文");
 await streamWriteChapter(idx);
 return true;
 }
 // 消费 inline 选区（来自" AI 改这段"浮动按钮/Ctrl+Enter）；chip 命令/普通触发时为 null → 整章模式
 const selection = _inlineSelection;
 _inlineSelection = null;
 // 校验 selection 的 offset 仍指向同一文本（防止用户选中后又改了正文导致 offset 失效）
 let useInline = !!selection;
 if (useInline) {
 const cur = $("#ed-text").value;
 if (selection.end > cur.length || cur.substring(selection.start, selection.end) !== selection.text) {
 addLog("warn", "[ai] 选区已失效（正文被改动），退化为整章模式");
 useInline = false;
 }
 }
 const effectiveSelection = useInline ? selection : null;
 _lastAiInstruction = instruction; // 记住本次指令，供"重试"按钮复用
 _lastAiSelection = effectiveSelection; // 记住是否 inline 模式
 _aiStreaming = true;
 setToolbarBusy(true); // 工具栏状态区切蓝点脉冲
 // 显示用户指令气泡
 const stream = $("#ed-ai-stream");
 stream.insertAdjacentHTML("beforeend", `<div class="ed-bubble ed-bubble-user">${ESC(instruction)}</div>`);
 const aiBubble = document.createElement("div");
 aiBubble.className = "ed-bubble ed-bubble-ai";
 aiBubble.innerHTML = '<span class="spinner"></span> 等待 AI...';
 stream.appendChild(aiBubble);
 // P0-#71: 显示取消按钮
 const cancelBtn = $("#ed-ai-cancel");
 if (cancelBtn) {
 cancelBtn.style.display = "";
 cancelBtn.onclick = () => {
 if (window._aiEditAbortController) {
 window._aiEditAbortController.abort();
 window._aiEditAbortController = null;
 }
 cancelBtn.style.display = "none";
 aiBubble.innerHTML = '<span style="color:var(--fg-dim);font-size:11px"> 已取消</span>';
 _aiStreaming = false;
 setToolbarBusy(false);
 };
 }
 // 诊断由后端 Harness 在 SSE 流中完成，不再单独调用 /analyze
 stream.insertAdjacentHTML("beforeend", `<div class="ed-bubble ed-bubble-tool" id="ed-progress-bubble"> 即将开始…</div>`);
 // Phase 进度显示（由 SSE phase 事件驱动，不再用 setInterval 动画）
 const phaseLabels = {
 analyze: " 扫描章节问题…",
 analyze_done: "",  // 用后端 msg 显示具体问题类型
 context: " 收集上下文…",
 context_done: "",  // 用后端 msg 显示加载了什么
 tool_call: " AI 正在查询知识库…",
 generate: " AI 正在修改…",
 validate: " 验证修改结果…",
 self_check: "",  // 用后端 msg 显示具体问题
 retry_generate: " 正在重新生成修正版…",
 };
 stream.scrollTop = stream.scrollHeight;
 $("#ed-ai-actions").style.display = "none";

 let aiText = "";
 // AbortController: 切章节时主动取消, 避免污染新章节 (A2)
 const ac = new AbortController();
 window._aiEditAbortController = ac;
 try {
 const reqBody = {
 instruction,
 current_text: currentText,
 };
 if (effectiveSelection) {
 reqBody.selection = effectiveSelection; // 触发后端 inline 模式
 }
 const resp = await fetch(`/api/editor/chapter/${idx}/ai-edit`, {
 method: "POST",
 headers: {"Content-Type": "application/json"},
 body: JSON.stringify(reqBody),
 signal: ac.signal,
 });
 if (!resp.ok) {
 const err = await resp.text();
 aiBubble.innerHTML = `<span style="color:var(--danger)">✕ ${ESC(err.slice(0, 200))}</span>`;
 return;
 }
 const reader = resp.body.getReader();
 const decoder = new TextDecoder();
 let buffer = "";
 while (true) {
 const {done, value} = await reader.read();
 if (done) break;
 buffer += decoder.decode(value, {stream: true});
 // 按行解析 SSE
 const lines = buffer.split("\n");
 buffer = lines.pop() || "";
 for (const line of lines) {
 if (!line.startsWith("data: ")) continue;
 try {
 const data = JSON.parse(line.slice(6));
 // Harness phase 事件
 if (data.phase) {
 const label = phaseLabels[data.phase] || data.msg || data.phase;
 if (label) {
 const prog = document.getElementById("ed-progress-bubble");
 if (prog) prog.textContent = label;
 }
 if (data.phase === "analyze_done" && data.pre_analysis) {
 const pa = data.pre_analysis;
 const highs = pa.by_severity?.high || 0;
 const prog = document.getElementById("ed-progress-bubble");
 if (prog) prog.textContent = `发现 ${pa.n_total} 个问题 (${highs} 高优先) · 准备修改…`;
 }
 continue;
 }
 if (data.chunk) {
 aiText += data.chunk;
 aiBubble.textContent = aiText;
 stream.scrollTop = stream.scrollHeight;
 const prog = document.getElementById("ed-progress-bubble");
 if (prog) prog.textContent = ` 接收中… ${aiText.length} 字符`;
 } else if (data.done) {
 aiText = data.text || aiText;
 aiBubble.textContent = aiText;
 const prog = document.getElementById("ed-progress-bubble");
 if (prog) prog.remove();
 // 显示验证报告
 if (data.report && data.report.delta) {
 const r = data.report;
 const delta = r.delta;
 let reportHTML = '<div class="ed-bubble ed-bubble-tool" style="margin-top:4px;font-size:11px">';
 reportHTML += '<b> 验证结果</b>';
 if (r.retries > 0) reportHTML += ` <span style="color:var(--accent)">(经 ${r.retries} 次自校验)</span>`;
 reportHTML += '<br>';
 reportHTML += `修改前: ${r.before.n_issues} 个问题 · `;
 reportHTML += `修改后: ${r.after.n_issues} 个问题<br>`;
 if (delta.improvement > 0) {
 reportHTML += `<span style="color:var(--success)">✓ 修复了 ${delta.fixed} 个问题</span>`;
 if (delta.introduced > 0) reportHTML += ` · <span style="color:var(--warning)">! 引入了 ${delta.introduced} 个新问题</span>`;
 } else if (delta.improvement < 0) {
 reportHTML += `<span style="color:var(--danger)">! 问题增加了 ${-delta.improvement} 个</span>`;
 } else {
 reportHTML += '<span style="color:var(--fg-dim)">问题数量不变</span>';
 }
 reportHTML += '</div>';
 stream.insertAdjacentHTML("beforeend", reportHTML);
 }
 // 透明度面板：展示 AI 用到的上下文（可折叠，默认收起）
 if (data.report && data.report.context_summary) {
 const cs = data.report.context_summary;
 const ctxItems = [];
 if (cs.pov_mode) ctxItems.push(`<b>视角</b>: ${ESC(cs.pov_mode)}`);
 if (cs.characters && cs.characters !== "—") ctxItems.push(`<b>人物</b>: <pre style="margin:2px 0;white-space:pre-wrap;font-family:inherit">${ESC(cs.characters)}</pre>`);
 if (cs.facts && cs.facts !== "—") ctxItems.push(`<b>信息边界(POV已知)</b>: <pre style="margin:2px 0;white-space:pre-wrap;font-family:inherit">${ESC(cs.facts)}</pre>`);
 if (cs.relationships && cs.relationships !== "—") ctxItems.push(`<b>关系</b>: <pre style="margin:2px 0;white-space:pre-wrap;font-family:inherit">${ESC(cs.relationships)}</pre>`);
 if (cs.threads && cs.threads !== "—") ctxItems.push(`<b>相关伏笔</b>: <pre style="margin:2px 0;white-space:pre-wrap;font-family:inherit">${ESC(cs.threads)}</pre>`);
 if (cs.prev_unfinished && cs.prev_unfinished !== "—" && cs.prev_unfinished !== "（无）") ctxItems.push(`<b>上章承接</b>: ${ESC(cs.prev_unfinished)}`);
 if (cs.recent_events && cs.recent_events !== "—" && cs.recent_events !== "（无）") ctxItems.push(`<b>近章事件</b>: <pre style="margin:2px 0;white-space:pre-wrap;font-family:inherit">${ESC(cs.recent_events)}</pre>`);
 if (cs.style_rules) ctxItems.push(`<b>风格规则</b>: ${ESC(cs.style_rules)}`);
 if (ctxItems.length) {
 const ctxHTML = `<details class="ed-bubble ed-bubble-tool" style="margin-top:4px;font-size:11px"><summary style="cursor:pointer;user-select:none;color:var(--fg-muted)"> AI 看到的上下文 (${ctxItems.length} 项)</summary><div style="margin-top:8px;display:flex;flex-direction:column;gap:6px">${ctxItems.join("")}</div></details>`;
 stream.insertAdjacentHTML("beforeend", ctxHTML);
 }
 }
 // ===== 渲染：inline 模式走单 diff 卡；整章模式走段落卡 =====
 if (effectiveSelection && data.report && data.report.mode === "inline") {
 await renderInlineEditCard(effectiveSelection, aiText, data.report);
 STATE_EDITOR.lastAiText = aiText;
 } else {
 await renderAiParagraphs(aiText);
 // renderAiParagraphs 内部已设 lastAiText，但 inline 模式没走它，需补
 STATE_EDITOR.lastAiText = aiText;
 }
 $("#ed-ai-actions").style.display = "flex";
 const logMsg = data.report
 ? `修复 ${data.report.delta.fixed} 个 · ${data.report.delta.introduced > 0 ? '引入 ' + data.report.delta.introduced + ' 个' : ''}`
 : `完成`;
 addLog("done", `[editor] AI 修改完成（${aiText.length} 字）· ${logMsg}`);
 } else if (data.error) {
 aiBubble.innerHTML = `<span style="color:var(--danger)">✕ ${ESC(data.error)}</span>`;
 const prog = document.getElementById("ed-progress-bubble");
 if (prog) {
 prog.innerHTML = `<span style="color:var(--danger)">✕ ${ESC(data.error)}</span>`;
 // 错误时显示重试按钮
 const retryBtn = document.createElement("button");
 retryBtn.className = "btn small";
 retryBtn.style.marginTop = "6px";
 retryBtn.textContent = "重试";
 retryBtn.onclick = () => { if (_lastAiInstruction) sendEditInstruction(_lastAiInstruction); };
 prog.appendChild(document.createElement("br"));
 prog.appendChild(retryBtn);
 }
 addLog("error", `[ai] ${data.error.slice(0, 120)}`);
 }
 } catch (parseErr) {
 // 静默跳过单行解析错误，但记录以便排查
 if (!window._sseParseErrCount) window._sseParseErrCount = 0;
 if (window._sseParseErrCount < 2) {
 addLog("warn", `[editor] SSE 解析异常: ${(line || "").slice(0, 100)}`);
 window._sseParseErrCount++;
 }
 }
 }
 }
 } catch (e) {
 if (e.name === "AbortError") {
 // 切章节主动取消, 不显示错误 (静默)
 aiBubble.innerHTML = '<span style="color:var(--fg-dim);font-size:11px"> 已取消 (切章节或主动停止)</span>';
 } else {
 aiBubble.innerHTML = `<span style="color:var(--danger)">✕ ${ESC(e.message || e)}</span>`;
 }
 } finally {
 _aiStreaming = false;
 setToolbarBusy(false); // 工具栏状态区恢复绿点
 if (window._aiEditAbortController === ac) window._aiEditAbortController = null;
 const cancelBtn = $("#ed-ai-cancel");
 if (cancelBtn) cancelBtn.style.display = "none";
 }
 return true; // 成功完成（供 Plan 项 approve handler 检查）
}



/**
 * 精确字数统计：CJK 字符按 1 个字计，英文按词计，正确处理代理对（emoji/生僻字）。
 * 返回 { cjk, enWords, total }。total = cjk + enWords（混合中英文的标准统计）。
 * 对纯中文：total ≈ 字符数（和旧 .length 一致）；对含 emoji 的也准确。
 */
function countCharsAccurate(text) {
 if (!text) return { cjk: 0, enWords: 0, total: 0 };
 // 用 spread/array-from 按 Unicode 码点拆分（正确处理代理对，1 emoji = 1 项）
 const chars = Array.from(text);
 let cjk = 0;
 // CJK 统一表意 + 扩展A + 兼容表意 + 全角标点（中文小说的"字"）
 const cjkRe = /[\u3400-\u9FFF\uF900-\uFAFF\u3000-\u303F\uFF00-\uFFEF]/;
 let nonCjk = "";
 for (const ch of chars) {
 if (cjkRe.test(ch)) {
 cjk++;
 } else {
 nonCjk += ch;
 }
 }
 // 英文词数（空白分隔的非 CJK 段）
 const enWords = (nonCjk.replace(/[^\s\w]/g, " ").trim().split(/\s+/).filter(w => /\w/.test(w))).length;
 return { cjk, enWords, total: cjk + enWords };
}

// =================== 文本工具 ===================
/**
 * 净化粘贴文本：清理 Word/网页带来的不可见垃圾字符。
 * 处理：零宽字符（ZWSP/ZWJ/ZWNJ/BOM）、NBSP、\r、智能引号统一、连续空白归一。
 * 这些字符会破坏段落切分（splitParagraphs）、AI 改稿 splice、字数统计。
 */
function sanitizePastedText(text) {
 if (!text) return text;
 let t = text;
 // 零宽字符 + BOM（最隐蔽，会污染所有后续处理）
 t = t.replace(/[\u200B-\u200F\u202A-\u202E\u2060\uFEFF]/g, "");
 // \r\n / \r → \n（Windows 换行归一）
 t = t.replace(/\r\n?/g, "\n");
 // NBSP → 普通空格
 t = t.replace(/[\u00A0]/g, " ");
 // 连续空格归一（但保留全角空格缩进 \u3000）
 t = t.replace(/(?!^) {2,}(?!\s*$)/g, " ");
 // 末尾空白
 t = t.replace(/[ \t]+$/gm, "");
 return t;
}

// =================== 编辑器内查找/替换（Ctrl+F / Ctrl+H）===================
let _frBar = null; // 浮层 DOM
let _frMatchPos = -1; // 当前匹配位置（高亮）
let _frMatches = []; // 所有匹配位置 [{start, end}]

/** 打开查找/替换浮层。mode = "find" | "replace"。 */
function openFindReplaceBar(mode) {
 const ta = $("#ed-text");
 if (!ta) return;
 ensureFindReplaceBar();
 pushFocus();
 _frBar.classList.remove("hidden");
 // 显示/隐藏替换行
 const repRow = _frBar.querySelector(".fr-replace-row");
 if (repRow) repRow.style.display = (mode === "replace") ? "flex" : "none";
 // 预填选中文本
 const findInput = _frBar.querySelector(".fr-find");
 const sel = ta.value.substring(ta.selectionStart, ta.selectionEnd);
 if (sel && sel.length < 200) findInput.value = sel;
 findInput.focus();
 findInput.select();
 runFind();
}

function ensureFindReplaceBar() {
 if (_frBar) return;
 const bar = document.createElement("div");
 bar.className = "find-replace-bar hidden";
 bar.innerHTML = `
 <div class="fr-find-row">
 <input type="text" class="fr-find" placeholder="查找…" />
 <span class="fr-count" id="fr-count">0/0</span>
 <button class="btn small fr-prev" title="上一个 (Shift+Enter)">↑</button>
 <button class="btn small fr-next" title="下一个 (Enter)">↓</button>
 <label class="fr-opt"><input type="checkbox" class="fr-case" /> 区分大小写</label>
 <button class="btn small fr-close" title="关闭 (Esc)">×</button>
 </div>
 <div class="fr-replace-row" style="display:none">
 <input type="text" class="fr-replace" placeholder="替换为…" />
 <button class="btn small fr-replace-one">替换</button>
 <button class="btn small fr-replace-all">全部替换</button>
 </div>
 `;
 document.body.appendChild(bar);
 _frBar = bar;

 const findInput = bar.querySelector(".fr-find");
 const repInput = bar.querySelector(".fr-replace");
 const countEl = bar.querySelector("#fr-count");

 findInput.addEventListener("input", runFind);
 findInput.addEventListener("keydown", (e) => {
 if (e.key === "Enter") { e.preventDefault(); e.shiftKey ? findPrev() : findNext(); }
 if (e.key === "Escape") { e.preventDefault(); closeFindReplaceBar(); }
 });
 repInput.addEventListener("keydown", (e) => {
 if (e.key === "Escape") { e.preventDefault(); closeFindReplaceBar(); }
 });
 bar.querySelector(".fr-next").onclick = findNext;
 bar.querySelector(".fr-prev").onclick = findPrev;
 bar.querySelector(".fr-close").onclick = closeFindReplaceBar;
 bar.querySelector(".fr-replace-one").onclick = replaceOne;
 bar.querySelector(".fr-replace-all").onclick = replaceAll;
 bar.querySelector(".fr-case").addEventListener("change", runFind);
}

function getFindQuery() {
 const findInput = _frBar.querySelector(".fr-find");
 const caseSensitive = _frBar.querySelector(".fr-case").checked;
 return { text: findInput.value || "", caseSensitive };
}

/** 重新扫描所有匹配。 */
function runFind() {
 const ta = $("#ed-text");
 if (!ta || !_frBar) return;
 const { text: q, caseSensitive } = getFindQuery();
 const countEl = _frBar.querySelector("#fr-count");
 _frMatches = [];
 _frMatchPos = -1;
 if (!q) { countEl.textContent = "0/0"; return; }
 const v = ta.value;
 const hay = caseSensitive ? v : v.toLowerCase();
 const needle = caseSensitive ? q : q.toLowerCase();
 let from = 0;
 while (true) {
 const idx = hay.indexOf(needle, from);
 if (idx === -1) break;
 _frMatches.push({ start: idx, end: idx + q.length });
 from = idx + q.length;
 }
 if (_frMatches.length > 0) {
 _frMatchPos = 0;
 highlightMatch(0);
 }
 countEl.textContent = `${_frMatches.length > 0 ? _frMatchPos + 1 : 0}/${_frMatches.length}`;
}

function highlightMatch(i) {
 const ta = $("#ed-text");
 if (!ta || i < 0 || i >= _frMatches.length) return;
 const m = _frMatches[i];
 ta.focus();
 ta.setSelectionRange(m.start, m.end);
 // 让选中区域可见（textarea 会自动滚动到 selection）
 const countEl = _frBar.querySelector("#fr-count");
 if (countEl) countEl.textContent = `${i + 1}/${_frMatches.length}`;
}

function findNext() {
 if (_frMatches.length === 0) return;
 _frMatchPos = (_frMatchPos + 1) % _frMatches.length;
 highlightMatch(_frMatchPos);
}

function findPrev() {
 if (_frMatches.length === 0) return;
 _frMatchPos = (_frMatchPos - 1 + _frMatches.length) % _frMatches.length;
 highlightMatch(_frMatchPos);
}

function replaceOne() {
 const ta = $("#ed-text");
 if (!ta || _frMatchPos < 0 || _frMatchPos >= _frMatches.length) return;
 const repInput = _frBar.querySelector(".fr-replace");
 const rep = repInput.value;
 const m = _frMatches[_frMatchPos];
 pushUndoSnapshot("before-replace", true);
 ta.value = ta.value.slice(0, m.start) + rep + ta.value.slice(m.end);
 updateEditorStats();
 pushUndoOnEdit(ta.value); // 手动 splice 需更新 undo 边界
 // 后续匹配位置整体平移
 const delta = rep.length - (m.end - m.start);
 _frMatches.splice(_frMatchPos, 1);
 for (let i = _frMatchPos; i < _frMatches.length; i++) {
 _frMatches[i].start += delta;
 _frMatches[i].end += delta;
 }
 if (_frMatches.length > 0) {
 if (_frMatchPos >= _frMatches.length) _frMatchPos = 0;
 highlightMatch(_frMatchPos);
 } else {
 _frMatchPos = -1;
 _frBar.querySelector("#fr-count").textContent = "0/0";
 }
}

function replaceAll() {
 const ta = $("#ed-text");
 if (!ta) return;
 const { text: q, caseSensitive } = getFindQuery();
 const repInput = _frBar.querySelector(".fr-replace");
 const rep = repInput.value;
 if (!q) return;
 pushUndoSnapshot("before-replace-all", true);
 const v = ta.value;
 let newV, count;
 if (caseSensitive) {
 const parts = v.split(q);
 count = parts.length - 1;
 newV = parts.join(rep);
 } else {
 // 大小写不敏感：用正则（转义特殊字符）
 const esc = q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
 const re = new RegExp(esc, "gi");
 count = (v.match(re) || []).length;
 newV = v.replace(re, () => rep); // 用 callback 绕过 $&/$1 等模式注入
 }
 ta.value = newV;
 updateEditorStats();
 pushUndoOnEdit(ta.value); // 手动 splice 需更新 undo 边界
 addLog("done", `[find] 已替换 ${count} 处`);
 runFind(); // 重新扫描
}

function closeFindReplaceBar() {
 if (_frBar) _frBar.classList.add("hidden");
 popFocus(); // 恢复焦点到编辑器
}

// =================== Ctrl+G 快速跳转章节（长篇小说导航）===================
let _gotoDialog = null;
function openChapterGotoDialog() {
 const chs = STATE.chapters || [];
 if (!chs.length) { showToast("还没有章节可跳转", "warning"); return; }
 if (!_gotoDialog) {
 _gotoDialog = document.createElement("div");
 _gotoDialog.className = "goto-dialog";
 _gotoDialog.innerHTML = `
 <div class="gd-header">
 <span> 跳转到章节</span>
 <button class="gd-close">×</button>
 </div>
 <input type="number" class="gd-input" placeholder="输入章节号，如 5" min="1" />
 <div class="gd-list"></div>
 `;
 document.body.appendChild(_gotoDialog);
 _gotoDialog.querySelector(".gd-close").onclick = () => { _gotoDialog.classList.add("hidden"); popFocus(); }
 _gotoDialog.querySelector(".gd-input").addEventListener("input", renderGotoList);
 _gotoDialog.querySelector(".gd-input").addEventListener("keydown", (e) => {
 if (e.key === "Enter") {
 const v = parseInt(e.target.value, 10);
 if (!isNaN(v)) { doGotoChapter(v); }
 } else if (e.key === "Escape") {
 _gotoDialog.classList.add("hidden");
 }
 });
 _gotoDialog.addEventListener("click", (e) => {
 if (e.target === _gotoDialog) { _gotoDialog.classList.add("hidden"); popFocus(); }
 });
 }
 // 刷新列表
 renderGotoList();
 pushFocus();
 _gotoDialog.classList.remove("hidden");
 const inp = _gotoDialog.querySelector(".gd-input");
 inp.value = "";
 setTimeout(() => inp.focus(), 30);
}

function renderGotoList() {
 if (!_gotoDialog) return;
 const filter = parseInt(_gotoDialog.querySelector(".gd-input").value, 10);
 const chs = STATE.chapters || [];
 const list = _gotoDialog.querySelector(".gd-list");
 const cur = STATE_EDITOR.chapterIdx;
 const items = chs
 .filter(c => isNaN(filter) || c.idx === filter)
 .slice(0, 50) // 最多显示 50 条
 .map(c => `
 <div class="gd-item${c.idx === cur ? ' active' : ''}" data-idx="${c.idx}">
 <span class="gd-idx">第 ${c.idx} 回</span>
 <span class="gd-title">${ESC((c.title || "").slice(0, 30))}</span>
 <span class="gd-meta">${(c.word_count || 0).toLocaleString()} 字</span>
 </div>
 `).join("");
 list.innerHTML = items || '<div class="gd-empty">无匹配章节</div>';
 list.querySelectorAll(".gd-item").forEach(el => {
 el.onclick = () => doGotoChapter(parseInt(el.dataset.idx, 10));
 });
}

function doGotoChapter(idx) {
 goto("editor");
 setTimeout(() => loadEditorChapter(idx), 50);
 if (_gotoDialog) _gotoDialog.classList.add("hidden");
 showToast(`跳转到第 ${idx} 回`, "info", 1500);
}

// =================== Plan 模式（Cline 风格：先规划后逐项执行）===================

/** 发送 plan 请求：走 /ai-plan 端点，AI 输出结构化计划（不改正文）。 */
async function sendPlanRequest(instruction) {
 const idx = STATE_EDITOR.chapterIdx;
 if (!idx) return;
 const currentText = $("#ed-text").value;
 if (!currentText.trim()) {
 showToast("章节文本为空", "warning");
 return;
 }
 _aiStreaming = true;
 setToolbarBusy(true);
 _lastAiInstruction = instruction;

 const stream = $("#ed-ai-stream");
 stream.insertAdjacentHTML("beforeend", `<div class="ed-bubble ed-bubble-user"> ${ESC(instruction)}</div>`);
 stream.insertAdjacentHTML("beforeend", `<div class="ed-bubble ed-bubble-tool" id="ed-plan-progress"> 正在制定计划…</div>`);
 $("#ed-ai-actions").style.display = "none";

 const phaseLabels = {
 analyze: " 扫描章节问题…",
 context: " 收集上下文…",
 plan: " AI 制定修改计划…",
 };

 const ac = new AbortController();
 window._aiEditAbortController = ac;
 try {
 const resp = await fetch(`/api/editor/chapter/${idx}/ai-plan`, {
 method: "POST",
 headers: {"Content-Type": "application/json"},
 body: JSON.stringify({instruction, current_text: currentText}),
 signal: ac.signal,
 });
 if (!resp.ok) {
 const err = await resp.text();
 const prog = $("#ed-plan-progress");
 if (prog) prog.innerHTML = `<span style="color:var(--danger)">✕ ${ESC(err.slice(0,200))}</span>`;
 return;
 }
 const reader = resp.body.getReader();
 const decoder = new TextDecoder();
 let buffer = "";
 while (true) {
 const {done, value} = await reader.read();
 if (done) break;
 buffer += decoder.decode(value, {stream: true});
 const lines = buffer.split("\n");
 buffer = lines.pop() || "";
 for (const line of lines) {
 if (!line.startsWith("data: ")) continue;
 try {
 const data = JSON.parse(line.slice(6));
 if (data.phase) {
 const label = phaseLabels[data.phase] || data.msg || data.phase;
 const prog = $("#ed-plan-progress");
 if (prog && label) prog.textContent = label;
 if (data.phase === "analyze_done" && data.pre_analysis) {
 const prog2 = $("#ed-plan-progress");
 if (prog2) prog2.textContent = ` 发现 ${data.pre_analysis.n_total} 个问题 · 制定计划中…`;
 }
 } else if (data.done && data.plan) {
 const prog = $("#ed-plan-progress");
 if (prog) prog.remove();
 renderPlanCard(data.plan);
 } else if (data.error) {
 const prog = $("#ed-plan-progress");
 if (prog) prog.innerHTML = `<span style="color:var(--danger)">✕ ${ESC(data.error)}</span>`;
 }
 } catch (_) {}
 }
 }
 } catch (e) {
 if (e.name !== "AbortError") {
 const prog = $("#ed-plan-progress");
 if (prog) prog.innerHTML = `<span style="color:var(--danger)">✕ ${ESC(e.message || e)}</span>`;
 }
 } finally {
 _aiStreaming = false;
 setToolbarBusy(false);
 if (window._aiEditAbortController === ac) window._aiEditAbortController = null;
 }
}

/** 渲染 plan 卡片：每项含 what/why/where/severity + 批准/跳过按钮。 */
function renderPlanCard(plan) {
 const stream = $("#ed-ai-stream");
 stream.querySelectorAll(".ed-paragraph-card, .ed-inline-card, .ed-plan-card").forEach(n => n.remove());

 const items = (plan && plan.items) || [];
 if (items.length === 0) {
 stream.insertAdjacentHTML("beforeend", `
 <div class="ed-plan-card ed-plan-empty">
 <div class="epc-head"><span class="epc-num"> 修改计划</span></div>
 <div class="epc-body" style="text-align:center;padding:20px;color:var(--success)">
 ✓ AI 认为本章无需修改（或没有发现问题）
 </div>
 </div>
 `);
 stream.scrollTop = stream.scrollHeight;
 return;
 }

 const sevColors = {high: "var(--danger)", medium: "var(--warning)", low: "var(--fg-muted)"};
 const sevLabels = {high: "● 高", medium: "● 中", low: "○ 低"};

 // 计划卡主体（OpenCode 风：进度计数 + 进度条 + 全部批准）
 const cardId = `ed-plan-${Date.now()}`;
 let html = `<div class="ed-plan-card" id="${cardId}">
 <div class="epc-head">
 <span class="epc-num"> 修改计划（${items.length} 项）</span>
 <span class="epc-progress">
 <span class="epc-progress-count">0/${items.length}</span>
 <span class="epc-progress-bar"><span class="epc-progress-fill" style="width:0%"></span></span>
 </span>
 </div>
 <div class="epc-batch">
 <button class="btn small primary epc-approve-all">✓ 全部批准并依次执行</button>
 <button class="btn small epc-skip-all">全部跳过</button>
 </div>`;
 items.forEach((item, i) => {
 const sev = item.severity || "medium";
 html += `
 <div class="plan-item plan-item-pending sev-${sev}" data-pid="${i}">
 <div class="pi-head">
 <span class="pi-sev" style="color:${sevColors[sev]}">${sevLabels[sev] || "● 中"}</span>
 <span class="pi-what">${ESC(item.what || "")}</span>
 </div>
 ${item.why ? `<div class="pi-why"><span class="pi-label">原因</span>${ESC(item.why)}</div>` : ""}
 ${item.where ? `<div class="pi-where"><span class="pi-label">位置</span><code>${ESC(item.where)}</code></div>` : ""}
 ${item.context_refs ? `<div class="pi-why" style="color:var(--fg-dim)"><span class="pi-label">依据</span>${ESC(item.context_refs)}</div>` : ""}
 <div class="pi-actions">
 <button class="btn small primary pi-approve" data-pid="${i}">✓ 批准并执行</button>
 <button class="btn small pi-skip" data-pid="${i}">✕ 跳过</button>
 </div>
 </div>`;
 });
 html += `</div>`;
 stream.insertAdjacentHTML("beforeend", html);
 stream.scrollTop = stream.scrollHeight;

 const card = document.getElementById(cardId);

 // 绑定逐项 批准/跳过
 card.querySelectorAll(".pi-approve").forEach(btn => {
 btn.onclick = async () => {
 const pid = parseInt(btn.dataset.pid, 10);
 const item = items[pid];
 if (!item) return;
 const itemEl = card.querySelector(`.plan-item[data-pid="${pid}"]`);
 // 构造精确指令，临时关闭 plan mode 走正常 ai-edit
 const preciseInstr = item.what + (item.where ? `（位置：${item.where}）` : "");
 // 尝试把 where 匹配为 inline 选区
 // 先清掉上次残留的选区: where 匹配不上时若保留旧选区, 修改会被应用到错误位置
 _inlineSelection = null;
 if (item.where && item.where.length > 5 && item.where.length < 500) {
 const ta = $("#ed-text");
 const pos = ta.value.indexOf(item.where);
 if (pos >= 0) {
 _inlineSelection = {start: pos, end: pos + item.where.length, text: item.where};
 }
 }
 itemEl.classList.add("executing");
 itemEl.querySelectorAll(".pi-approve, .pi-skip").forEach(b => { b.disabled = true; b.style.opacity = "0.5"; });
 const wasPlan = _planMode;
 _planMode = false; // 临时关闭，让 sendEditInstruction 走 ai-edit
 try {
 const ok = await sendEditInstruction(preciseInstr);
 if (!ok) {
 // 被 _aiStreaming 拦截（上一个还在跑），恢复按钮不标"已执行"
 itemEl.classList.remove("executing");
 itemEl.classList.add("plan-item-pending");
 itemEl.querySelectorAll(".pi-approve, .pi-skip").forEach(b => { b.disabled = false; b.style.opacity = "1"; });
 return;
 }
 itemEl.classList.remove("plan-item-pending", "executing");
 itemEl.classList.add("plan-item-done");
 itemEl.querySelector(".pi-actions").innerHTML = '<span class="pi-done-tag">✓ 已执行</span>';
 } catch (e) {
 itemEl.classList.remove("executing");
 itemEl.classList.add("plan-item-pending");
 itemEl.querySelectorAll(".pi-approve, .pi-skip").forEach(b => { b.disabled = false; b.style.opacity = "1"; });
 showToast("执行失败: " + (e.message || e), "error");
 } finally {
 _planMode = wasPlan; // 恢复
 _updatePlanProgress(card, items.length);
 }
 };
 });

 card.querySelectorAll(".pi-skip").forEach(btn => {
 btn.onclick = () => {
 const pid = parseInt(btn.dataset.pid, 10);
 const itemEl = card.querySelector(`.plan-item[data-pid="${pid}"]`);
 itemEl.classList.remove("plan-item-pending");
 itemEl.classList.add("plan-item-skipped");
 itemEl.querySelector(".pi-actions").innerHTML = '<span class="pi-skip-tag"> 已跳过</span>';
 _updatePlanProgress(card, items.length);
 };
 });

 // 全部跳过
 card.querySelector(".epc-skip-all")?.addEventListener("click", () => {
 card.querySelectorAll(".plan-item-pending").forEach(el => {
 el.classList.remove("plan-item-pending");
 el.classList.add("plan-item-skipped");
 el.querySelector(".pi-actions").innerHTML = '<span class="pi-skip-tag"> 已跳过</span>';
 });
 _updatePlanProgress(card, items.length);
 });

 // 全部批准：依次执行所有 pending 项（串行，避免并发冲突）
 card.querySelector(".epc-approve-all")?.addEventListener("click", async () => {
 const btn = card.querySelector(".epc-approve-all");
 if (btn) { btn.disabled = true; btn.textContent = " 执行中…"; }
 for (let i = 0; i < items.length; i++) {
 const itemEl = card.querySelector(`.plan-item[data-pid="${i}"]`);
 if (!itemEl || !itemEl.classList.contains("plan-item-pending")) continue;
 const approveBtn = itemEl.querySelector(".pi-approve");
 if (approveBtn) {
 approveBtn.click(); // 复用单项批准逻辑（含 sendEditInstruction）
 // 等该项执行完（_aiStreaming 变 false）再下一项
 while (_aiStreaming) { await new Promise(r => setTimeout(r, 300)); }
 }
 }
 if (btn) { btn.disabled = false; btn.textContent = "✓ 全部批准并依次执行"; }
 });

 addLog("info", `[plan] 生成 ${items.length} 项修改计划`);
}

/** 更新计划卡顶部进度计数 + 进度条（OpenCode 风实时进度）。 */
function _updatePlanProgress(card, total) {
 if (!card || !total) return;
 const done = card.querySelectorAll(".plan-item-done, .plan-item-skipped").length;
 const countEl = card.querySelector(".epc-progress-count");
 const fillEl = card.querySelector(".epc-progress-fill");
 if (countEl) countEl.textContent = `${done}/${total}`;
 if (fillEl) fillEl.style.width = `${Math.round(done / total * 100)}%`;
}


// =================== AI 输出按段落处理 ===================
function splitParagraphs(text) {
 if (!text) return [];
 return text.split(/\n\s*\n+/).map(p => p.trim()).filter(p => p.length > 0);
}


// =================== diff Worker 调度层 ===================
// 把 O(n×m) LCS diff 计算移到 Worker 线程，避免长段落 diff 卡住主线程输入。
// Worker 不可用（旧浏览器/加载失败）时降级走下面的同步 charDiffHTML/countDiffChars。
let _diffWorker = null;
let _diffWorkerFailed = false; // 加载失败标记，避免反复尝试
let _diffReqId = 0;
const _diffPending = new Map(); // reqId -> resolve

function getDiffWorker() {
 if (_diffWorkerFailed) return null;
 if (_diffWorker) return _diffWorker;
 try {
 if (typeof Worker === "undefined") { _diffWorkerFailed = true; return null; }
 _diffWorker = new Worker("/static/diff-worker.js");
 _diffWorker.onmessage = (e) => {
 const { reqId, results } = e.data || {};
 const resolve = _diffPending.get(reqId);
 if (resolve) { resolve(results); _diffPending.delete(reqId); }
 };
 _diffWorker.onerror = (e) => {
 addLog("warn", `[diff-worker] 加载/运行失败，降级同步: ${e.message || e}`);
 // 唤醒所有等待中的 promise（用降级路径重算）
 for (const [rid, resolve] of _diffPending.entries()) {
 resolve(null);
 }
 _diffPending.clear();
 _diffWorker = null;
 _diffWorkerFailed = true;
 };
 } catch (e) {
 addLog("warn", `[diff-worker] 创建失败，降级同步: ${e.message || e}`);
 _diffWorkerFailed = true;
 return null;
 }
 return _diffWorker;
}

/** 批量算 diff。tasks=[{idx, oldText, newText}, ...]。
 * 返回 [{idx, html, del, ins, eq}, ...]，顺序与 tasks 一致。
 * Worker 不可用时降级同步（复用 charDiffHTML/countDiffChars，带 800 截断兜底）。 */
async function batchDiff(tasks) {
 const w = getDiffWorker();
 if (!w) {
 return tasks.map(t => ({
 idx: t.idx,
 html: charDiffHTML(t.oldText, t.newText),
 del: countDiffChars(t.oldText, t.newText, "del"),
 ins: countDiffChars(t.oldText, t.newText, "ins"),
 eq: countDiffChars(t.oldText, t.newText, "eq"),
 }));
 }
 const reqId = ++_diffReqId;
 const results = await new Promise(resolve => {
 _diffPending.set(reqId, resolve);
 try {
 w.postMessage({ reqId, tasks });
 } catch (e) {
 _diffPending.delete(reqId);
 resolve(null); // 降级
 }
 });
 if (results) return results;
 // Worker 返回 null（onerror 已标记失败）→ 降级同步
 return tasks.map(t => ({
 idx: t.idx,
 html: charDiffHTML(t.oldText, t.newText),
 del: countDiffChars(t.oldText, t.newText, "del"),
 ins: countDiffChars(t.oldText, t.newText, "ins"),
 eq: countDiffChars(t.oldText, t.newText, "eq"),
 }));
}

/**
 * 字符级 diff（用 Myers / LCS 算法）
 * 返回 HTML，标记删除（红色删除线）和新增（绿色背景）
 * 输入: oldText, newText
 * 输出: HTML string（已转义）
 *
 * 简化版：O(n*m) 动态规划，对中文 200 字以内足够快
 * 注：此同步版带 800 字截断兜底；Worker 版（diff-worker.js）取消截断。
 * batchDiff() 优先走 Worker，失败降级到此。
 */
function charDiffHTML(oldText, newText) {
 if (!oldText && !newText) return "";
 if (!oldText) return `<ins class="cd-ins">${ESC(newText)}</ins>`;
 if (!newText) return `<del class="cd-del">${ESC(oldText)}</del>`;

 // 字符级 LCS（限长以避免卡顿）
 const MAX = 800;
 if (oldText.length > MAX || newText.length > MAX) {
 // 过长 → 退化：只显示前 MAX 字
 return ESC(newText.slice(0, MAX)) + (newText.length > MAX ? "…" : "");
 }

 const a = oldText;
 const b = newText;
 const m = a.length;
 const n = b.length;
 // dp[i][j] = LCS 长度
 const dp = Array.from({length: m + 1}, () => new Uint32Array(n + 1));
 for (let i = 1; i <= m; i++) {
 for (let j = 1; j <= n; j++) {
 if (a[i - 1] === b[j - 1]) {
 dp[i][j] = dp[i - 1][j - 1] + 1;
 } else {
 dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
 }
 }
 }
 // 回溯
 const ops = []; // {type: 'eq'|'del'|'ins', ch: '字'}
 let i = m, j = n;
 while (i > 0 || j > 0) {
 if (i > 0 && j > 0 && a[i - 1] === b[j - 1]) {
 ops.push({type: "eq", ch: a[i - 1]});
 i--; j--;
 } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
 ops.push({type: "ins", ch: b[j - 1]});
 j--;
 } else {
 ops.push({type: "del", ch: a[i - 1]});
 i--;
 }
 }
 ops.reverse();
 // 合并连续相同类型 + 长串保留
 let html = "";
 let curType = null;
 let buf = "";
 const flush = () => {
 if (!buf) return;
 const t = ESC(buf);
 if (curType === "del") html += `<del class="cd-del">${t}</del>`;
 else if (curType === "ins") html += `<ins class="cd-ins">${t}</ins>`;
 else html += t;
 buf = "";
 };
 for (const op of ops) {
 if (op.type !== curType) {
 flush();
 curType = op.type;
 }
 buf += op.ch;
 }
 flush();
 return html;
}


// =================== AI 改稿接受率统计 ===================
// v2: 累计计数持久化到 localStorage（跨会话保留，可观测性需要长期数据）；
// recent 滑窗仍只保留最近 10 次（内存态，刷新即清，避免 localStorage 膨胀）。
const _AI_STATS_LS_KEY = "novelai-ai-stats";

function loadAiStats() {
 try {
 const raw = localStorage.getItem(_AI_STATS_LS_KEY);
 if (!raw) return;
 const s = JSON.parse(raw);
 STATE_AI_STATS.totalAiEdits = s.totalAiEdits || 0;
 STATE_AI_STATS.totalAiChars = s.totalAiChars || 0;
 STATE_AI_STATS.acceptedChars = s.acceptedChars || 0;
 STATE_AI_STATS.acceptedParagraphs = s.acceptedParagraphs || 0;
 STATE_AI_STATS.rejectedChars = s.rejectedChars || 0;
 STATE_AI_STATS.rejectedParagraphs = s.rejectedParagraphs || 0;
 } catch (_) {}
}

function persistAiStats() {
 try {
 localStorage.setItem(_AI_STATS_LS_KEY, JSON.stringify({
 totalAiEdits: STATE_AI_STATS.totalAiEdits,
 totalAiChars: STATE_AI_STATS.totalAiChars,
 acceptedChars: STATE_AI_STATS.acceptedChars,
 acceptedParagraphs: STATE_AI_STATS.acceptedParagraphs,
 rejectedChars: STATE_AI_STATS.rejectedChars,
 rejectedParagraphs: STATE_AI_STATS.rejectedParagraphs,
 }));
 } catch (_) {}
}

function resetAiStats() {
 // 切章节时只清 recent 滑窗，累计计数保留（可观测性需要长期数据，用户可在面板手动重置）
 STATE_AI_STATS.recent = [];
 try { updateAiStatsDisplay(); } catch (_) {}
}

function fullResetAiStats() {
 // 完整重置（面板"重置接受率"按钮调用）
 STATE_AI_STATS.totalAiEdits = 0;
 STATE_AI_STATS.totalAiChars = 0;
 STATE_AI_STATS.acceptedChars = 0;
 STATE_AI_STATS.acceptedParagraphs = 0;
 STATE_AI_STATS.rejectedChars = 0;
 STATE_AI_STATS.rejectedParagraphs = 0;
 STATE_AI_STATS.recent = [];
 STATE_AI_STATS.history = [];
 persistAiStats();
 try { updateAiStatsDisplay(); } catch (_) {}
 refreshAiStatsbar();
}

const STATE_AI_STATS = {
 // 每次 ai-edit 完成 → {text, charCount, instructions: [...]}
 totalAiEdits: 0,
 totalAiChars: 0,
 // 每次"接受段" / "替换段" / "插入到光标" → 接受的字数
 acceptedChars: 0,
 acceptedParagraphs: 0,
 // 每次"跳过" / "全部拒绝" → 跳过的字数
 rejectedChars: 0,
 rejectedParagraphs: 0,
 // 最近 10 次操作的滑窗（用于显示在状态栏）
 recent: [], // [{t, type: 'accept'|'reject', chars, paragraphIdx}]
 maxRecent: 10,
};
// 启动时恢复持久化的累计计数
loadAiStats();

function recordAiAction(type, chars, paragraphIdx) {
 if (type === "accept" || type === "replace") {
 STATE_AI_STATS.acceptedChars += chars;
 STATE_AI_STATS.acceptedParagraphs++;
 } else if (type === "reject") {
 STATE_AI_STATS.rejectedChars += chars;
 STATE_AI_STATS.rejectedParagraphs++;
 }
 STATE_AI_STATS.recent.push({t: Date.now(), type, chars, paragraphIdx});
 if (STATE_AI_STATS.recent.length > STATE_AI_STATS.maxRecent) {
 STATE_AI_STATS.recent.shift();
 }
 persistAiStats(); // 累计计数落盘
 updateAiStatsDisplay();
 refreshAiStatsbar(); // 同步统计条
}

function recordAiEdit(charCount) {
 STATE_AI_STATS.totalAiEdits++;
 STATE_AI_STATS.totalAiChars += charCount;
 persistAiStats();
 updateAiStatsDisplay();
 refreshAiStatsbar();
}

// =================== AI 可观测性统计条 ===================
let _aiStatsbarTimer = null;

function formatTokens(n) {
 if (n >= 10000) return (n / 10000).toFixed(1) + "万";
 return String(n);
}

function formatAcceptRate() {
 const acc = STATE_AI_STATS.acceptedChars;
 const rej = STATE_AI_STATS.rejectedChars;
 const total = acc + rej;
 if (total < 50) return "—"; // 样本太少
 return Math.round(acc / total * 100) + "%";
}

let _aiStatsbarInFlight = false; // 防慢服务器时请求堆叠

async function refreshAiStatsbar() {
 const bar = $("#ed-ai-statsbar");
 if (!bar) return;
 if (_aiStatsbarInFlight) return; // 上一次还没返回，跳过本次（避免堆叠）
 _aiStatsbarInFlight = true;
 let stats = null;
 try {
 stats = await API.get("/ai-stats");
 } catch (e) { _aiStatsbarInFlight = false; return; }
 finally { _aiStatsbarInFlight = false; }
 if (!stats) return;
 const today = stats.today || {};
 const callsEl = $("#asb-calls");
 const tokensEl = $("#asb-tokens");
 const latencyEl = $("#asb-latency");
 const rateEl = $("#asb-accept-rate");
 if (callsEl) callsEl.textContent = today.calls || 0;
 if (tokensEl) tokensEl.textContent = formatTokens(today.total_tokens || 0);
 if (latencyEl) latencyEl.textContent = today.avg_latency_ms ? Math.round(today.avg_latency_ms) : "—";
 if (rateEl) rateEl.textContent = formatAcceptRate();
 // 详情区（展开时才填充）
 const det = $("#ed-ai-statsbar-detail");
 if (det && !det.classList.contains("hidden")) {
 renderAiStatsbarDetail(stats);
 }
}

function renderAiStatsbarDetail(stats) {
 const epEl = $("#asbd-endpoints");
 const recEl = $("#asbd-recent");
 const all = stats.all || {};
 // 按 endpoint 分类的明细
 if (epEl) {
 const eps = all.by_endpoint || {};
 const items = Object.entries(eps).map(([ep, d]) =>
 `<div class="asbd-ep-row">
 <span class="asbd-ep-name">${ESC(ep)}</span>
 <span class="asbd-ep-calls">${d.calls} 次</span>
 <span class="asbd-ep-tokens">${formatTokens(d.tokens)} token</span>
 <span class="asbd-ep-latency">${d.avg_latency_ms || 0}ms</span>
 </div>`
 ).join("");
 epEl.innerHTML = items || '<div class="asbd-empty">暂无调用记录</div>';
 }
 // 最近调用列表
 if (recEl) {
 const recent = stats.recent || [];
 const rows = recent.map(r => {
 const t = new Date(r.ts * 1000);
 const tm = `${t.getHours().toString().padStart(2,'0')}:${t.getMinutes().toString().padStart(2,'0')}:${t.getSeconds().toString().padStart(2,'0')}`;
 const ok = r.success === 1 || r.success === true;
 return `<div class="asbd-rec-row ${ok ? '' : 'fail'}">
 <span class="asbd-rec-time">${tm}</span>
 <span class="asbd-rec-ep">${ESC(r.endpoint)}</span>
 <span class="asbd-rec-tokens">${formatTokens(r.total_tokens)} token</span>
 <span class="asbd-rec-latency">${r.latency_ms || 0}ms</span>
 <span class="asbd-rec-status">${ok ? '✓' : '✕'}</span>
 </div>`;
 }).join("");
 recEl.innerHTML = rows || '<div class="asbd-empty">暂无调用记录</div>';
 }
}

async function setupAiStatsbarToggle() {
 const bar = $("#ed-ai-statsbar");
 const det = $("#ed-ai-statsbar-detail");
 if (!bar || !det) return;
 bar.onclick = async () => {
 const hidden = det.classList.toggle("hidden");
 const toggle = $("#asb-toggle");
 if (toggle) toggle.textContent = hidden ? "▾" : "▴";
 if (!hidden) {
 // 展开时立即拉取并渲染详情
 try {
 const stats = await API.get("/ai-stats");
 renderAiStatsbarDetail(stats);
 } catch (_) {}
 }
 };
 const resetBtn = $("#asb-reset");
 if (resetBtn) {
 resetBtn.onclick = async (e) => {
 e.stopPropagation();
 if (await showConfirm("清空前端接受率统计？\n（后端 token 日志不受影响）")) {
 fullResetAiStats();
 }
 };
 }
}

function updateAiStatsDisplay() {
 // 接受率 = 接受 / (接受 + 拒绝)
 const total = STATE_AI_STATS.acceptedChars + STATE_AI_STATS.rejectedChars;
 const rate = total > 0 ? Math.round(STATE_AI_STATS.acceptedChars / total * 100) : 0;

 // 接受率反馈：低接受率提示
 let rateClass = "";
 let rateEmoji = "";
 let rateHint = "";
 if (total >= 200) { // 至少 200 字才判断（避免小样本误报）
 if (rate < 30) {
 rateClass = "ai-rate-bad";
 rateEmoji = "!";
 rateHint = "接受率低——AI 改的方向可能不对，建议换种指令或人工改";
 } else if (rate < 60) {
 rateClass = "ai-rate-mid";
 rateEmoji = "";
 rateHint = "接受率中等——可以接受部分段落";
 } else {
 rateClass = "ai-rate-good";
 rateEmoji = "✓";
 rateHint = "接受率高——AI 改的方向不错";
 }
 }

 const el = $("#ai-stats-display");
 if (!el) return; // 元素未渲染（HTML 中可能无此容器）
 el.className = "ai-stats " + rateClass;
 el.innerHTML = `
 <span title="本次 AI 输出总字数"> AI 输出: ${STATE_AI_STATS.totalAiChars.toLocaleString()} 字</span>
 <span title="已接受字数" style="color:var(--success)">✓ 采纳: ${STATE_AI_STATS.acceptedChars.toLocaleString()}</span>
 <span title="${ESC(rateHint)}" class="ai-rate-num" style="font-weight:600">${rateEmoji} 采纳率 ${rate}%</span>
 <span title="跳过字数" style="color:var(--fg-muted)">✕ 跳过: ${STATE_AI_STATS.rejectedChars.toLocaleString()}</span>
 ${rateHint && total >= 200 ? `<span class="ai-rate-hint" title="${ESC(rateHint)}">${ESC(rateHint)}</span>` : ""}
 `;
}


function countDiffChars(oldText, newText, type) {
 if (!oldText && !newText) return 0;
 if (!oldText) return newText.length; // 全部新增
 if (!newText) return oldText.length; // 全部删除
 const MAX = 800;
 if (oldText.length > MAX || newText.length > MAX) {
 // 过长退化为按行/段估算
 return type === "ins" ? newText.length : type === "del" ? oldText.length : 0;
 }
 const m = oldText.length;
 const n = newText.length;
 const dp = Array.from({length: m + 1}, () => new Uint32Array(n + 1));
 for (let i = 1; i <= m; i++) {
 for (let j = 1; j <= n; j++) {
 if (oldText[i - 1] === newText[j - 1]) {
 dp[i][j] = dp[i - 1][j - 1] + 1;
 } else {
 dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
 }
 }
 }
 let cnt = 0;
 let i = m, j = n;
 while (i > 0 || j > 0) {
 if (i > 0 && j > 0 && oldText[i - 1] === newText[j - 1]) {
 if (type === "eq") cnt++;
 i--; j--;
 } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
 if (type === "ins") cnt++;
 j--;
 } else {
 if (type === "del") cnt++;
 i--;
 }
 }
 return cnt;
}

/**
 * 把 AI 输出按段落渲染为可采纳/拒绝的卡片。
 * 每段一个段落卡，用户可以：
 * ✓ 采纳此段（替换到 textarea 当前光标位置）
 * ✕ 跳过
 * ⤴ 引用此段继续改
 * 顶部还有"复制全文到剪贴板"
 */
/**
 * inline 编辑结果卡：AI 只改了选中片段，这里展示选中→新版本的字符级 diff，
 * 并提供"替换选区"按钮（精确 splice，不动选区外的字）。
 */
async function renderInlineEditCard(selection, newText, report) {
 const stream = $("#ed-ai-stream");
 // 移除之前的 inline 卡 / 段落卡（保留指令气泡和报告）
 stream.querySelectorAll(".ed-paragraph-card, .ed-inline-card").forEach(n => n.remove());

 const oldText = selection.text;
 const ta = document.getElementById("ed-text");
 if (!ta) return;

 // 防御：AI 若输出了整章（远超选区长度），提示用户
 const lenRatio = oldText.length > 0 ? newText.length / oldText.length : 1;
 const suspicious = lenRatio > 3 && newText.length > oldText.length + 200;

 // * 字符级 diff 走 Worker（一次 batchDiff 算 html + del/ins）
 const myChapter = STATE_EDITOR.chapterIdx; // await 期间用户可能切章节
 const [dr] = await batchDiff([{idx: 0, oldText, newText}]);
 if (STATE_EDITOR.chapterIdx !== myChapter) return; // 防跨章节污染
 const diffHtmlStr = dr ? dr.html : "";
 const delN = dr ? dr.del : 0;
 const insN = dr ? dr.ins : 0;

 const cardId = `ed-inline-${Date.now()}`;
 stream.insertAdjacentHTML("beforeend", `
 <div class="ed-paragraph-card ed-inline-card ${suspicious ? "epc-suspicious" : "epc-modified"}" id="${cardId}">
 <div class="epc-head">
 <span class="epc-num"> inline 修改</span>
 <span class="epc-tag">选区 ${oldText.length} 字 → ${newText.length} 字</span>
 </div>
 ${suspicious ? `<div style="color:var(--warning);font-size:11px;margin-bottom:4px">! AI 输出比选区长很多（${lenRatio.toFixed(1)}x），可能输出了整章而非选区。仍可采纳，但建议检查。</div>` : ""}
 <div class="epc-diff">
 <div class="ed-tag">字符级变化</div>
 <div class="epc-diff-content">${diffHtmlStr || "<i>(无变化)</i>"}</div>
 <div class="epc-diff-stats">
 <span class="diff-del">− ${delN} 字</span>
 <span class="diff-ins">+ ${insN} 字</span>
 </div>
 </div>
 <div class="epc-actions">
 <button class="btn small primary epc-inline-accept">✓ 替换选区（仅改选中部分）</button>
 <button class="btn small epc-inline-reject">✕ 跳过</button>
 </div>
 </div>
 `);
 const card = document.getElementById(cardId);

 // ✓ 替换选区：精确 splice，不动选区外的字
 card.querySelector(".epc-inline-accept").onclick = () => {
 // 再次校验 offset 仍有效（用户可能在这期间改了正文）
 const cur = ta.value;
 if (selection.end > cur.length || cur.substring(selection.start, selection.end) !== oldText) {
 addLog("warn", "[ai-inline] 选区已失效，无法精确替换；请重新选中后再试");
 showToast("选区已失效，请重新选中文字", "warning");
 return;
 }
 pushUndoSnapshot("before-inline-replace", true);
 ta.value = cur.slice(0, selection.start) + newText + cur.slice(selection.end);
 // 把光标放到替换后的新文本末尾
 const newCursor = selection.start + newText.length;
 ta.focus();
 ta.setSelectionRange(newCursor, newCursor);
 updateEditorStats();
 setEditorStatus(`✓ 已替换选区（${oldText.length}→${newText.length} 字，按 Ctrl+Z 撤销 / 保存生效）`);
 recordVersion(`inline 改（${oldText.length}→${newText.length}字）`, {source: "ai"});
 card.classList.remove("epc-modified", "epc-suspicious");
 card.classList.add("ed-para-accepted");
 const diff = card.querySelector(".epc-diff");
 if (diff) diff.style.display = "none";
 card.querySelectorAll(".epc-inline-accept, .epc-inline-reject").forEach(b => { b.disabled = true; b.style.opacity = "0.5"; });
 addLog("done", `[ai-inline] 已替换选区（${oldText.length}→${newText.length} 字）`);
 };

 // ✕ 跳过
 card.querySelector(".epc-inline-reject").onclick = () => {
 card.classList.remove("epc-modified", "epc-suspicious");
 card.classList.add("ed-para-rejected");
 const diff = card.querySelector(".epc-diff");
 if (diff) diff.style.display = "none";
 card.querySelectorAll(".epc-inline-accept, .epc-inline-reject").forEach(b => { b.disabled = true; b.style.opacity = "0.5"; });
 };

 stream.scrollTop = stream.scrollHeight;
}


async function renderAiParagraphs(fullText) {
 // P2: 防重复调用 (data.done 触发时如果 reader 还在读, 同一 fullText 可能再调一次)
 if (STATE_EDITOR.lastAiText === fullText) {
 return; // 同一份内容, 跳过重复 recordAiEdit 和 DOM 重建
 }
 STATE_EDITOR.lastAiText = fullText;
 // 记录本次 AI 输出
 recordAiEdit(fullText.length);

 const stream = $("#ed-ai-stream");
 const paras = splitParagraphs(fullText);
 // 移除之前的段落卡（保留之前的指令气泡）
 stream.querySelectorAll(".ed-paragraph-card").forEach(n => n.remove());

 if (paras.length === 0) return;

 // 计算每段相对原章的变化（用段落按 index 1-1 对齐）
 const originalParas = splitParagraphs(STATE_EDITOR.savedText || "");
 const changeStats = { added: 0, modified: 0, same: 0 };
 paras.forEach((p, i) => {
 const op = originalParas[i] || "";
 if (!op) changeStats.added++;
 else if (op === p) changeStats.same++;
 else changeStats.modified++;
 });

 // * 批量算 diff（一次通信算完所有段，Worker 并行；失败降级同步）
 // 只对 hasDiff 的段算（未变/新增的不需要 diff）
 const diffTasks = paras.map((p, idx) => ({ idx, oldText: originalParas[idx] || "", newText: p }));
 const myChapter = STATE_EDITOR.chapterIdx; // await 期间用户可能切章节，事后校验
 const diffResults = await batchDiff(diffTasks);
 // 防跨章节污染：await 期间用户若切了章节，丢弃本次渲染（避免把旧章节 diff 卡插到新章节）
 if (STATE_EDITOR.chapterIdx !== myChapter) return;
 const diffMap = new Map(diffResults.map(r => [r.idx, r]));

 // 摘要条
 stream.insertAdjacentHTML("beforeend", `
 <div class="ed-paragraph-card ed-para-summary">
 <div class="eps-info">
 <span class="eps-total">共 ${paras.length} 段 · ${fullText.length} 字</span>
 ${changeStats.added > 0
 ? `<span class="eps-add" title="AI 改稿时新增的段落（你没有写过的）">+${changeStats.added} AI 新增</span>`
 : ""}
 <span class="eps-mod" title="改了原段">${changeStats.modified} 改了</span>
 <span class="eps-same" title="未变">${changeStats.same} 未变</span>
 </div>
 <div class="eps-actions">
 <button class="btn small" id="btn-filter-added" title="只看 AI 新增的段">只看新增</button>
 <button class="btn small" id="btn-copy-ai"> 复制全文</button>
 </div>
 </div>
 `);

 // 每段一个卡（diff 已在上方批量算好，这里只拼 HTML）
 paras.forEach((p, idx) => {
 const originalP = originalParas[idx] || "";
 const hasDiff = originalP && originalP !== p;
 const isNew = !originalP;
 const dr = diffMap.get(idx); // {html, del, ins, eq}
 const cardId = `ed-para-${Date.now()}-${idx}`;
 stream.insertAdjacentHTML("beforeend", `
 <div class="ed-paragraph-card ed-para-pending ${isNew ? "epc-new" : (hasDiff ? "epc-modified" : "epc-same")}" id="${cardId}" data-idx="${idx}">
 <div class="epc-head">
 <span class="epc-num">第 ${idx + 1} 段</span>
 <span class="epc-tag">${isNew ? "AI 新增" : (hasDiff ? "改了" : "未变")}</span>
 <span class="epc-len">${p.length} 字</span>
 </div>
 ${hasDiff ? `<div class="epc-diff">
 <div class="ed-tag">变化</div>
 <div class="epc-diff-content">${dr ? dr.html : ""}</div>
 <div class="epc-diff-stats">
 <span class="diff-del">− ${dr ? dr.del : 0} 字</span>
 <span class="diff-ins">+ ${dr ? dr.ins : 0} 字</span>
 <span class="diff-eq">= ${dr ? dr.eq : 0} 字未变</span>
 </div>
 </div>` : `<div class="epc-body">${ESC(p).slice(0, 600)}${p.length > 600 ? "…" : ""}</div>`}
 <div class="epc-actions">
 ${hasDiff ? `<button class="btn small primary epc-replace" data-idx="${idx}">✓ 用新版替换原段</button>` : ""}
 <button class="btn small epc-accept">✓ 插入到光标</button>
 <button class="btn small epc-quote">基于此段再改</button>
 <button class="btn small epc-reject">✕ 跳过</button>
 </div>
 </div>
 `);
 const card = document.getElementById(cardId);
 // 替换原段（最实用：精确修改）
 if (hasDiff) {
 card.querySelector(".epc-replace").onclick = () => {
 replaceOriginalParagraph(idx, p);
 recordAiAction("replace", p.length, idx);
 card.classList.remove("ed-para-pending");
 card.classList.add("ed-para-accepted");
 // P0-#66: 替换后隐藏 diff 区域, 避免"已采纳但还显示红绿"
 const diff = card.querySelector(".epc-diff");
 if (diff) diff.style.display = "none";
 // 替换后禁用 3 个按钮 (避免重复替换)
 card.querySelectorAll(".epc-replace, .epc-accept, .epc-reject, .epc-quote").forEach(b => { b.disabled = true; b.style.opacity = "0.5"; });
 addLog("done", `[ai] 第 ${idx + 1} 段已用 AI 新版替换`);
 };
 }
 card.querySelector(".epc-accept").onclick = () => {
 const ta = $("#ed-text");
 pushUndoSnapshot("before-insert-para-" + (idx + 1), true);
 const pos = ta.selectionStart ?? ta.value.length;
 const before = ta.value.slice(0, pos);
 const after = ta.value.slice(pos);
 // 插入：光标位置 + 段落 + 换行
 ta.value = before + (before.endsWith("\n\n") ? "" : (before.length > 0 ? "\n\n" : "")) + p + (after.startsWith("\n\n") ? "" : "\n\n") + after;
 updateEditorStats();
 setEditorStatus("已插入到光标 (按 Ctrl+Z 撤销 / 按保存生效)");
 recordVersion(`插入第 ${idx + 1} 段`, {source: "insert"});
 recordAiAction("accept", p.length, idx);
 card.classList.remove("ed-para-pending");
 card.classList.add("ed-para-accepted");
 // P0-#66: 隐藏 diff (已采纳就不该再显示红绿)
 const diff = card.querySelector(".epc-diff");
 if (diff) diff.style.display = "none";
 card.querySelectorAll(".epc-replace, .epc-accept, .epc-reject, .epc-quote").forEach(b => { b.disabled = true; b.style.opacity = "0.5"; });
 };
 card.querySelector(".epc-quote").onclick = () => {
 const instr = $("#ed-input");
 instr.value = `基于「${p.slice(0, 60)}${p.length > 60 ? "…" : ""}」继续改：`;
 instr.focus();
 instr.setSelectionRange(instr.value.length, instr.value.length);
 };
 card.querySelector(".epc-reject").onclick = () => {
 recordAiAction("reject", p.length, idx);
 card.classList.remove("ed-para-pending");
 card.classList.add("ed-para-rejected");
 // 拒绝后同样隐藏 diff + 禁用按钮
 const diff = card.querySelector(".epc-diff");
 if (diff) diff.style.display = "none";
 card.querySelectorAll(".epc-replace, .epc-accept, .epc-reject, .epc-quote").forEach(b => { b.disabled = true; b.style.opacity = "0.5"; });
 };
 });

 // 复制全文
 const copyBtn = document.getElementById("btn-copy-ai");
 if (copyBtn) {
 copyBtn.onclick = async () => {
 try {
 await navigator.clipboard.writeText(fullText);
 copyBtn.textContent = "✓ 已复制";
 showToast("已复制全文到剪贴板", "info");
 setTimeout(() => { copyBtn.textContent = " 复制全文"; }, 2000);
 } catch (e) {
 const ta = document.createElement("textarea");
 ta.value = fullText;
 document.body.appendChild(ta);
 ta.select();
 document.execCommand("copy");
 document.body.removeChild(ta);
 copyBtn.textContent = "✓ 已复制（兼容模式）";
 }
 };
 }

 // "只看新增" 过滤
 const filterBtn = document.getElementById("btn-filter-added");
 if (filterBtn) {
 let showOnlyAdded = false;
 filterBtn.onclick = () => {
 showOnlyAdded = !showOnlyAdded;
 filterBtn.classList.toggle("active", showOnlyAdded);
 filterBtn.textContent = showOnlyAdded ? "全部显示" : "只看新增";
 stream.querySelectorAll(".ed-paragraph-card[data-idx]").forEach(c => {
 if (showOnlyAdded) {
 c.style.display = c.classList.contains("epc-new") ? "" : "none";
 } else {
 c.style.display = "";
 }
 });
 };
 }

 stream.scrollTop = stream.scrollHeight;
}

// =================== 修改流水线 ===================
let _lastRoadmap = [];

async function renderPipeline() {
 setToolHeader("修改流水线", "4 套扫描 + 结构分析 + LLM 优化。阶段 1 秒级；完整流水线含 LLM 需 5-15 分钟。");
 setToolBody(`
 <div style="display:flex;gap:8px;margin-bottom:12px">
 <button class="btn primary" id="btn-pipeline-quick">快速诊断（秒级）</button>
 <button class="btn" id="btn-pipeline-full">完整流水线（含 LLM）</button>
 </div>
 <div id="pipeline-summary" style="margin:8px 0;color:var(--fg-muted);font-size:12px">尚未运行。点击上方按钮开始——快速诊断秒级出结果，完整流水线含 LLM 优化需 5-15 分钟。</div>
 <div id="pipeline-results"></div>
 `);
 $("#btn-pipeline-quick").onclick = runPipelineQuick;
 $("#btn-pipeline-full").onclick = runPipelineFull;
 // 如果内存里有上次结果，自动展示
 API.get("/pipeline/last").then(r => {
 if (r && !r.empty) renderPipelineResults(r);
 }).catch(() => {});
}

async function runPipelineQuick() {
 const cont = $("#pipeline-results");
 cont.innerHTML = '<p class="placeholder"><span class="spinner"></span> 跑 5 个扫描器 + 结构分析…</p>';
 addLog("info", "[pipeline] 开始快速诊断");
 try {
 const r = await API.get("/pipeline/quick");
 addLog("done", `[pipeline] quick 完成，${Object.values(r.issues_by_category).reduce((s, v) => s + v.count, 0)} 个问题`);
 renderQuickReport(r);
 // 立即拉路线图
 const rm = await API.get("/pipeline/roadmap?limit=20");
 _lastRoadmap = rm.roadmap || [];
 renderRoadmap(_lastRoadmap, rm.total);
 } catch (e) {
 cont.innerHTML = `<p class="placeholder">失败: ${ESC(e.message || e)}</p>`;
 addLog("error", `[pipeline] 失败: ${e.message || e}`);
 }
}

let _pipelinePolling = null; // 流水线轮询单例, 防止多次启动轮询
let _pipelineErrCount = 0; // 轮询连续出错计数
async function runPipelineFull() {
 if (!(await showConfirm("完整流水线要 5-15 分钟, 期间会调用 LLM 多次, 建议在网络稳定时跑。\n继续?"))) return;
 if (_pipelinePolling) { addLog("warn", "[pipeline] 已有轮询在跑, 不重启"); return; }
 const cont = $("#pipeline-results");
 cont.innerHTML = '<p class="placeholder"> 完整流水线已启动, 在底部日志看进度…</p>';
 addLog("info", "[pipeline] 启动完整流水线");
 try {
 await API.post("/pipeline/full", {});
 } catch (e) { addLog("error", `[pipeline] 启动失败: ${e.message || e}`); return; }
 // 单例轮询: 用 _pipelinePolling 引用, 任何 finally 都要清 (B1)
 const POLL_MAX = 100; // 最多 100 * 3s = 5 分钟, 超时强停
 let pollCount = 0;
 _pipelinePolling = setInterval(async () => {
 pollCount++;
 if (pollCount > POLL_MAX) {
 clearInterval(_pipelinePolling);
 _pipelinePolling = null;
 addLog("warn", "[pipeline] 轮询超时 5 分钟, 强停");
 return;
 }
 try {
 const r = await API.get("/pipeline/last");
 if (r && !r.empty && r.summary) {
 _pipelineErrCount = 0;
 clearInterval(_pipelinePolling);
 _pipelinePolling = null;
 addLog("done", `[pipeline] 完成: ${r.summary.roadmap_items} 个路线图 / ${r.summary.llm_suggestions} 个 LLM 建议`);
 renderPipelineResults(r);
 const rm = await API.get("/pipeline/roadmap?limit=30");
 _lastRoadmap = rm.roadmap || [];
 renderRoadmap(_lastRoadmap, rm.total);
 }
 } catch (e) {
 _pipelineErrCount = (_pipelineErrCount || 0) + 1;
 addLog("warn", `[pipeline] 轮询出错: ${e.message || e}`);
 // 连续 3 次错才清掉
 if (_pipelineErrCount >= 3) {
 clearInterval(_pipelinePolling);
 _pipelinePolling = null;
 _pipelineErrCount = 0;
 addLog("error", "[pipeline] 连续 3 次出错, 已停止轮询");
 }
 }
 }, POLL_INTERVAL_MS);
}

function renderQuickReport(r) {
 const cont = $("#pipeline-results");
 const cats = [
 ["thread", " 伏笔"],
 ["logic", " 逻辑链"],
 ["style", " 文风"],
 ["personality", " 性格"],
 ["structure", " 结构"],
 ];
 let html = `<div class="dash-row" style="grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin-bottom:12px">`;
 const ibc = r.issues_by_category || {};
 for (const [k, label] of cats) {
 const info = ibc[k] || { count: 0, high: 0 };
 const color = info.count === 0 ? "var(--success)" : info.high > 0 ? "var(--danger)" : info.count > 5 ? "var(--danger)" : getCssVar("--warning");
 html += `<div class="dash-card" style="padding:10px">
 <div class="card-title" style="font-size:11px">${label}</div>
 <div class="big-num" style="font-size:24px;color:${color}">${info.count}</div>
 <div class="sub">${info.high ? '● '+info.high+' 高优' : (info.count>0?'有问题':'✓ 通过')}</div>
 </div>`;
 }
 html += `</div>`;
 // 项目概况
 const h = r.health || {};
 html += `<div class="dash-card" style="margin-bottom:12px">
 <div class="card-title"> 项目概况</div>
 <div>${h.n_chapters ?? 0} 章 / ${h.n_events ?? 0} 事件 / ${h.n_threads ?? 0} 伏笔 / ${h.n_characters ?? 0} 人物（${h.n_characters_with_mbti ?? 0} 已标 MBTI）/ ${(h.total_words || 0).toLocaleString()} 字</div>
 <div style="color:var(--fg-muted);font-size:11px;margin-top:4px">️ 扫描耗时: ${r.elapsed_seconds ?? "?"}s</div>
 </div>`;
 cont.innerHTML = html;
 $("#pipeline-summary").textContent = `✓ 快速诊断完成（${r.elapsed_seconds ?? "?"}s），${Object.values(ibc).reduce((s, v) => s + (v ? v.count : 0), 0)} 个问题`;
}

function renderPipelineResults(r) {
 if (!r || !r.quick) return;
 renderQuickReport(r.quick);
 // 路线图
 if (r.roadmap) {
 renderRoadmap(r.roadmap, r.roadmap.length);
 }
 // LLM 汇总
 if (r.llm_suggestions_count !== undefined) {
 $("#pipeline-summary").innerHTML = `
 ✓ 完整流水线完成 · 扫描 ${r.summary.total_scanner_issues} 个问题（H=${r.summary.high_issues}）·
 LLM ${r.llm_suggestions_count} 条建议 · 路线图 ${r.summary.roadmap_items} 项 ·
 总耗时 ${(r.elapsed_total_seconds_full || r.summary.elapsed_total_seconds || 0).toFixed(1)}s
 `;
 }
}

function renderRoadmap(items, total) {
 const cont = $("#pipeline-results");
 if (!cont) return;
 // 幂等: 先移除旧的路线图片段, 避免重复调用时追加两份
 cont.querySelector("#roadmap-card")?.remove();
 if (!items || !items.length) {
 cont.insertAdjacentHTML("beforeend", `<div class="dash-card" id="roadmap-card" style="text-align:center;color:var(--success);padding:20px">✓ 路线图为空（无问题）</div>`);
 return;
 }
 let html = `<div class="dash-card" id="roadmap-card" style="margin-top:12px">
 <div class="card-title"> 修改路线图（${total || items.length} 项，按优先级排序）</div>
 <div style="font-size:11px;color:var(--fg-muted);margin-bottom:8px">
 ● high 紧急修 · ● medium 应改 · ● low 可润色
 </div>
 `;
 for (const it of items) {
 const sevMark = {high: "●", medium: "●", low: "●"}[it.severity] || "○";
 const chRef = it.chapter_ref ? `第 ${it.chapter_ref} 章` : "全局";
 html += `<div class="issue-card ${it.severity}">
 <div class="head">
 <div class="left">#${it.rank} ${sevMark} <span style="color:var(--fg-muted);font-size:11px">[${chRef}]</span> ${ESC(it.category)} · ${ESC(it.type)}</div>
 <div class="meta">score=${it.score}</div>
 </div>
 <div class="body" style="font-weight:600;color:var(--fg);margin-bottom:4px">${ESC(it.title)}</div>
 ${it.context ? `<div class="body">${ESC(it.context.slice(0, 200))}${it.context.length > 200 ? "…" : ""}</div>` : ""}
 ${it.fix_suggestion && it.fix_suggestion !== it.context ? `<div class="body" style="color:var(--fg-muted);margin-top:4px">建议: ${ESC(it.fix_suggestion.slice(0, 200))}</div>` : ""}
 </div>`;
 }
 html += `</div>`;
 cont.insertAdjacentHTML("beforeend", html);
}

// =================== 叙事结构 ===================
async function renderStructure() {
 setToolHeader("叙事结构（起承转合）", "三层分析：全篇 / 全卷 / 章回 + 8 大结构问题检测 + LLM 优化建议。");
 setToolBody(`<p class="placeholder loading">加载中…</p>`);
 try {
 const full = await API.get("/structure/full");
 if (full.error) { setToolBody(`<p class="placeholder">${ESC(full.error)}</p>`); return; }
 let html = `
 <div class="opt-quick-row">
 <select id="str-level" class="opt-select">
 <option value="full">全篇</option>
 <option value="volume">全卷</option>
 <option value="chapter">章回</option>
 </select>
 <button class="btn primary" id="btn-str-analyze">重新分析</button>
 <button class="btn primary" id="btn-str-optimize"> LLM 优化建议</button>
 <span style="color:var(--fg-muted);font-size:11px;margin-left:8px">点击 LLM 优化将根据所选层级生成建议（需配置 NOVELAI_API_KEY）</span>
 </div>
 <div id="str-curves" style="height:300px;margin-bottom:12px;background:var(--bg-elevated);border:1px solid var(--border);border-radius:6px;padding:8px"></div>
 <div class="dash-card">
 <div class="card-title"> 4 段事件分布（起/承/转/合）</div>
 <div id="str-phases" style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:8px"></div>
 </div>
 <div class="dash-card" style="margin-top:10px">
 <div class="card-title"> 3 幕结构</div>
 <div id="str-acts"></div>
 </div>
 <div class="dash-card" style="margin-top:10px">
 <div class="card-title">! 结构问题（${full.issues?.length || 0}）</div>
 <div id="str-issues"></div>
 </div>
 `;
 setToolBody(html);

 // 画重要性曲线 + 4 段区域
 const chartEl = $("#str-curves");
 if (_charts.structure) _charts.structure.dispose();
 _charts.structure = echarts.init(chartEl);
 const data = (full.intensity_curve || []).map(c => [c.position, c.intensity, c.chapter_idx, c.n_events, c.n_turning]);
 _charts.structure.setOption({
 backgroundColor: "transparent",
 title: {text: "全篇重要性曲线（横轴 0~1 = 故事内位置）", textStyle: {color: getCssVar("--fg-muted"), fontSize: 12}, left: 0, top: 0},
 tooltip: {trigger: "item", formatter: p => `第${p.value[2]}章 pos=${p.value[0].toFixed(2)} int=${p.value[1].toFixed(2)}<br/>${p.value[3]} 事件 ${p.value[4]>0?'':''}`},
 grid: {left: 50, right: 20, top: 30, bottom: 30},
 xAxis: {type: "value", min: 0, max: 1, name: "位置", nameTextStyle: {color: getCssVar("--fg-muted")}, axisLine: {lineStyle: {color: getCssVar("--border-strong")}}, axisLabel: {color: getCssVar("--fg-muted")}, splitLine: {lineStyle: {color: getCssVar("--border")}}},
 yAxis: {type: "value", min: 0, name: "重要性", nameTextStyle: {color: getCssVar("--fg-muted")}, axisLine: {lineStyle: {color: getCssVar("--border-strong")}}, axisLabel: {color: getCssVar("--fg-muted")}, splitLine: {lineStyle: {color: getCssVar("--border")}}},
 series: [
 {type: "line", data: data, smooth: true, itemStyle: {color: getCssVar("--accent")}, lineStyle: {color: getCssVar("--accent"), width: 2}, areaStyle: {color: getCssVar("--accent-soft")}, markPoint: {data: data.filter(d => d[4] > 0).map(d => ({value: d[1], coord: [d[0], d[1]]})), itemStyle: {color: getCssVar("--danger")}, label: {formatter: ""}}},
 // 4 段背景区（Nord 色板：info / success / warning / accent）
 {type: "line", markArea: {itemStyle: {color: "transparent"}, data: [
 [{xAxis: 0, itemStyle: {color: "rgba(94,129,172,0.06)"}}, {xAxis: 0.15}],
 [{xAxis: 0.15, itemStyle: {color: "rgba(163,190,140,0.06)"}}, {xAxis: 0.60}],
 [{xAxis: 0.60, itemStyle: {color: "rgba(235,203,139,0.08)"}}, {xAxis: 0.80}],
 [{xAxis: 0.80, itemStyle: {color: "rgba(136,192,208,0.06)"}}, {xAxis: 1.0}],
 ]}},
 ],
 });
 // 4 段分布
 const ph = $("#str-phases");
 const phaseNames = ["setup", "development", "climax", "resolution"];
 // 用 color-mix 生成主题色弱底（跟随 dark/light 切换；修复旧 "var(--success)44" 无效拼接）
 const phaseColors = [
 "color-mix(in srgb, var(--accent) 10%, transparent)",
 "color-mix(in srgb, var(--success) 10%, transparent)",
 "color-mix(in srgb, var(--warning) 12%, transparent)",
 "color-mix(in srgb, var(--info) 10%, transparent)",
 ];
 const phaseIcons = ["", "", "", ""];
 const pb = full.phase_breakdown || {};
 for (let i = 0; i < phaseNames.length; i++) {
 const p = phaseNames[i];
 const info = pb[p];
 if (!info) continue; // 后端缺该阶段则跳过，不崩
 ph.innerHTML += `<div style="background:${phaseColors[i]};border:1px solid var(--border);border-radius:6px;padding:10px">
 <div style="font-size:11px;color:var(--fg-muted);text-transform:uppercase">${phaseIcons[i]} ${ESC(info.label || p)}</div>
 <div style="font-size:20px;color:var(--fg);font-weight:600;margin-top:2px">${info.n_events ?? 0}</div>
 <div style="font-size:11px;color:var(--fg-muted)">事件 · imp ${info.importance_avg ?? "-"}</div>
 <div style="font-size:11px;color:var(--fg-muted)">pos ${(info.position_range || [0,0]).join("-")}</div>
 <div style="font-size:11px;color:var(--fg-dim);margin-top:4px">埋 ${info.n_threads_planted ?? 0} · 揭 ${info.n_threads_payoff ?? 0}</div>
 </div>`;
 }
 // 3 幕结构
 $("#str-acts").innerHTML = (full.act_breakdown || []).map(ab => {
 const cr = ab.chapter_range || [0, 0];
 return `<div style="display:flex;justify-content:space-between;padding:8px 12px;background:var(--bg-elevated);border-radius:4px;margin-top:4px">
 <span><b>${ESC(ab.label || "")}</b> · 第 ${cr[0]}-${cr[1]} 章</span>
 <span style="color:var(--fg-muted)">${ab.n_chapters} 章 · ${ab.word_count.toLocaleString()} 字 · ${ab.n_events} 事件</span>
 </div>`;
 }).join("");
 // 问题
 const issues = full.issues || [];
 if (!issues.length) {
 $("#str-issues").innerHTML = '<p style="color:var(--success);padding:8px">✓ 未发现明显结构问题</p>';
 } else {
 $("#str-issues").innerHTML = issues.map(it =>
 `<div class="issue-card ${it.severity}">
 <div class="head"><div class="left">[${it.severity}] ${ESC(it.type)}</div></div>
 <div class="body">${ESC(it.context)}</div>
 </div>`
 ).join("");
 }

 $("#btn-str-analyze").onclick = renderStructure;
 $("#btn-str-optimize").onclick = async () => {
 const level = $("#str-level").value;
 addLog("info", `[optimize-structure] 生成 ${level} 建议...`);
 try {
 const r = await API.post("/optimize/structure", {level}, LLM_TIMEOUT_MS);
 addLog("done", `[optimize-structure] ${r.count} 条建议已入库`);
 showToast(`${r.count} 条建议已生成`, "success");
 } catch (e) { addLog("error", `[optimize-structure] 失败: ${e}`); }
 };
 } catch (e) {
 setToolBody(`<p class="placeholder">加载失败: ${ESC(e.message || e)}</p>`);
 }
}

// =================== AI 自动抽取 ===================
async function renderAIExtract() {
 setToolHeader("AI 自动抽取", "用 LLM 把已导入的章节正文抽取成结构化事件和伏笔，直接入库。每章 1 次事件抽取 + 1 次伏笔抽取 = 2 次 LLM 调用。");
 setToolBody(`<p class="placeholder loading">加载中…</p>`);
 try {
 const chapters = await API.get("/chapters");
 const eventsN = (await API.get("/events")).length;
 const threadsN = (await API.get("/threads")).length;
 let html = `
 <div class="dash-card">
 <div class="card-title"> 当前状态</div>
 <div>已导入 <b>${chapters.length}</b> 章 · 已抽取 <b>${eventsN}</b> 个事件 · <b>${threadsN}</b> 个伏笔</div>
 </div>
 <div class="dash-card" style="margin-top:12px">
 <div class="card-title"> 抽取事件</div>
 <div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0">
 <button class="btn primary" id="btn-extract-events-all"> 一键抽全本事件</button>
 </div>
 <div style="color:var(--fg-muted);font-size:11px">每章 1 次 LLM 调用 → 提取 2-6 个事件入库</div>
 </div>
 <div class="dash-card" style="margin-top:12px">
 <div class="card-title"> 抽取伏笔</div>
 <div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0">
 <button class="btn primary" id="btn-extract-threads-all"> 一键抽全本伏笔</button>
 </div>
 <div style="color:var(--fg-muted);font-size:11px">每章 1 次 LLM 调用 → 识别 planted/payoff/developing，自动关联到已存在的伏笔</div>
 </div>
 <div class="dash-card" style="margin-top:12px;background:linear-gradient(135deg, color-mix(in srgb, var(--accent) 12%, var(--bg-card)) 0%, var(--bg-card) 100%);border-color:var(--border)">
 <div class="card-title"> 一键抽取：事件 + 伏笔</div>
 <div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0">
 <button class="btn primary" id="btn-extract-all"> 一键抽全本（事件+伏笔）</button>
 </div>
 <div style="color:var(--fg-dim);font-size:11px">预计 ${chapters.length * 2} 次 LLM 调用 · ${chapters.length} 章约 ${Math.max(1, Math.round(chapters.length / 4))}-${Math.max(2, Math.round(chapters.length / 2))} 分钟</div>
 </div>
 <div class="dash-card" style="margin-top:12px">
 <div class="card-title"> 各章已抽取进度</div>
 <div id="extract-progress" style="max-height:300px;overflow-y:auto"></div>
 </div>
 `;
 setToolBody(html);

 // 进度概览：每章已抽多少事件/伏笔
 // 注：后端 /chapters 不返正文，事件/伏笔各取一次列表后在内存里按 chapter_id 过滤
 // （旧代码用 `/chapter/${idx}` 这个不存在的端点会 404，且在 map 里重复拉 N 次 /threads）
 const [allEvents, allThreads] = await Promise.all([API.get("/events"), API.get("/threads")]);
 const progHtml = chapters.map(c => {
 const evs = (allEvents || []).filter(e => e.chapter_id === c.id);
 const ths = (allThreads || []).filter(t => t.planted_chapter_id === c.id || t.resolved_chapter_id === c.id || t.payoff_chapter_id === c.id);
 return `<div class="list-row">
 <div class="lr-title">第 ${c.idx} 回 ${ESC(c.title || "")}</div>
 <div class="lr-meta"> ${evs.length} 事件 · ${ths.length} 伏笔</div>
 </div>`;
 });
 $("#extract-progress").innerHTML = progHtml.join("");

 $("#btn-extract-events-all").onclick = () => runExtractAll("events");
 $("#btn-extract-threads-all").onclick = () => runExtractAll("threads");
 $("#btn-extract-all").onclick = () => runExtractAll("all");
 } catch (e) {
 setToolBody(`<p class="placeholder">加载失败: ${ESC(e.message || e)}</p>`);
 }
}

async function runExtractAll(kind) {
 const label = {events: "事件", threads: "伏笔", all: "事件+伏笔"}[kind];
 if (!(await showConfirm(`开始抽取全本 ${label}? 约 1-2 分钟 (每章 1-2 次 LLM 调用). 建议在网络稳定时跑.`))) return;
 if (!acquireUiLock("extract")) { addLog("warn", "[extract] 已有抽取任务在跑, 请稍等"); return; }
 // P0-#86: 抽取中禁用 3 个按钮
 ["#btn-extract-events-all", "#btn-extract-threads-all", "#btn-extract-all"].forEach(sel => {
 const b = $(sel); if (b) b.disabled = true;
 });
 const prog = $("#extract-progress");
 prog.innerHTML = '<p class="placeholder"><span class="spinner"></span> LLM 抽取中...</p>';
 addLog("info", `[extract-${kind}] 开始抽取全本...`);
 try {
 const endpoint = {events: "/extract/events-all", threads: "/extract/threads-all", all: "/extract/all"}[kind];
 const r = await API.post(endpoint, {}, LLM_TIMEOUT_MS); // 全本 LLM 抽取按章串行, 远超 30s 默认超时
 addLog("done", `[extract-${kind}] 完成: ${JSON.stringify(r.events || r.threads || r).slice(0,200)}`);
 await renderAIExtract();
 refreshAll();
 } catch (e) {
 prog.innerHTML = `<p class="placeholder">失败: ${ESC(e.message || e)}</p>`;
 addLog("error", `[extract-${kind}] 失败: ${e.message || e}`);
 } finally {
 releaseUiLock("extract");
 ["#btn-extract-events-all", "#btn-extract-threads-all", "#btn-extract-all"].forEach(sel => {
 const b = $(sel); if (b) b.disabled = false;
 });
 }
}

// =================== 扫描 ===================
async function renderScanAll() {
 setToolHeader("全本扫描", "4 个扫描器一次性跑，列出所有问题。点击下方按钮运行。");
 setToolBody(`
 <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">
 <button class="btn primary" id="btn-run-scan-all">一键全扫</button>
 <button class="btn" id="btn-run-threads">仅伏笔</button>
 <button class="btn" id="btn-run-logic">仅逻辑链</button>
 <button class="btn" id="btn-run-style">仅文风</button>
 </div>
 <div id="scan-result"><p class="placeholder" style="padding:30px">点击上方按钮开始扫描。扫描完成后，问题列表会显示在这里。</p></div>
 `);
 $("#btn-run-scan-all").onclick = () => runFullScan();
 $("#btn-run-threads").onclick = () => runOneScan("threads");
 $("#btn-run-logic").onclick = () => runOneScan("logic");
 $("#btn-run-style").onclick = () => runOneScan("style");
}

async function runOneScan(kind) {
 if (!acquireUiLock("scan")) { addLog("warn", `[scan] 已有扫描在跑, 请稍等`); return; }
 // P0-#85: 扫描中禁用 4 个按钮, 视觉上明显
 ["#btn-run-scan-all", "#btn-run-threads", "#btn-run-logic", "#btn-run-style"].forEach(sel => {
 const b = $(sel); if (b) b.disabled = true;
 });
 const cont = $("#scan-result");
 cont.innerHTML = '<p class="placeholder"><span class="spinner"></span> 扫描中…</p>';
 try {
 if (kind === "threads") {
 const data = await API.get("/scan/threads");
 renderThreadScanResult(data.issues || []);
 } else if (kind === "logic") {
 const data = await API.get("/scan/logic");
 renderLogicScanResult(data);
 } else if (kind === "style") {
 const data = await API.get("/scan/style");
 renderStyleScanResult(data);
 }
 } catch (e) {
 cont.innerHTML = `<p class="placeholder">失败: ${ESC(e.message || e)}</p>`;
 } finally {
 releaseUiLock("scan");
 ["#btn-run-scan-all", "#btn-run-threads", "#btn-run-logic", "#btn-run-style"].forEach(sel => {
 const b = $(sel); if (b) b.disabled = false;
 });
 }
}

async function runFullScan() {
 if (!acquireUiLock("scan")) { addLog("warn", "[scan] 已有扫描在跑, 请稍等"); return; }
 // P0-#85: 全扫时禁用 4 个按钮
 ["#btn-run-scan-all", "#btn-run-threads", "#btn-run-logic", "#btn-run-style"].forEach(sel => {
 const b = $(sel); if (b) b.disabled = true;
 });
 const cont = $("#scan-result");
 cont.innerHTML = '<p class="placeholder"><span class="spinner"></span> 扫描中 (4 个扫描器并行)…</p>';
 try {
 const [t, l, s, p] = await Promise.all([
 API.get("/scan/threads"),
 API.get("/scan/logic"),
 API.get("/scan/style"),
 API.get("/personality_drift").catch(() => null), // 可能无 MBTI 人物，容错
 ]);
 let html = "";
 html += renderThreadScanResultHTML(t.issues || []);
 html += renderLogicScanResultHTML(l);
 html += renderStyleScanResultHTML(s);
 // 第 4 个扫描器：性格漂移（之前漏跑，修 #4）
 if (p && p.results) {
 const driftCount = p.results.filter(r => r.drift_signals && r.drift_signals.length).length;
 if (driftCount > 0) {
 html += `<div class="dash-card"><div class="card-title"> 性格漂移</div><div style="color:var(--warning);padding:8px 0">发现 ${driftCount} 处人物性格偏差，<a href="#" onclick="goto('driftscan');return false" style="color:var(--accent)">查看详情 →</a></div></div>`;
 } else {
 html += `<div class="dash-card"><div class="card-title"> 性格漂移</div><div style="color:var(--success);padding:8px 0">✓ 人物性格无明显漂移</div></div>`;
 }
 }
 cont.innerHTML = html || emptyStateHTML({
 icon: "",
 title: "全部通过!",
 desc: "伏笔 / 逻辑链 / 文风 / 性格漂移 4 个扫描器都没有发现问题。",
 });
 } catch (e) {
 cont.innerHTML = errorStateHTML(e.message || e, "renderScanAll()");
 } finally {
 releaseUiLock("scan");
 ["#btn-run-scan-all", "#btn-run-threads", "#btn-run-logic", "#btn-run-style"].forEach(sel => {
 const b = $(sel); if (b) b.disabled = false;
 });
 }
}

function renderThreadScanResult(issues) {
 const cont = $("#scan-result");
 // P1-E: 防御 issues undefined / null
 const list = issues || [];
 if (!list.length) { cont.innerHTML = '<p class="placeholder">✓ 伏笔扫描通过</p>'; return; }
 cont.innerHTML = renderThreadScanResultHTML(list);
}
function renderThreadScanResultHTML(issues) {
 if (!issues || !issues.length) return '<div class="dash-card">✓ 伏笔无问题</div>';
 let html = '<div class="dash-card"><div class="card-title"> 伏笔问题 (' + issues.length + ')</div>';
 for (const it of issues) {
 const chIdx = it.chapter_idx || it.planted_chapter || it.resolved_chapter;
 const clickable = chIdx ? `issue-card-clickable` : '';
 const dataAttr = chIdx ? `data-chapter-idx="${chIdx}"` : '';
 html += `<div class="issue-card ${it.severity} ${clickable}" ${dataAttr}>
 <div class="head">
 <div class="left">${ESC(it.title || "")}</div>
 <div class="meta">[${it.severity}] ${it.issue_type}${chIdx ? ` · <span class="ch-link">第 ${chIdx} 章 →</span>` : ""}</div>
 </div>
 <div class="body">${ESC(it.context || "")}</div>
 ${it.fix_suggestion ? `<div class="body" style="color:var(--fg-muted);margin-top:4px">建议: ${ESC(it.fix_suggestion)}</div>` : ""}
 </div>`;
 }
 return html + "</div>";
}

function renderLogicScanResult(data) {
 const cont = $("#scan-result");
 cont.innerHTML = renderLogicScanResultHTML(data);
}
function renderLogicScanResultHTML(data) {
 // P1-E: 防御 data 是 null/undefined 或后端漏返 summary
 if (!data || typeof data !== "object") return '<p class="placeholder">无扫描结果</p>';
 const sum = data.summary || {};
 const sections = [
 ["dead_appears", " 死人复活"],
 ["location_clash", " 地点冲突"],
 ["causality_reversed", " 因果倒置"],
 ["info_leak", " 信息泄漏"],
 ["chain_break", " 事件链断裂"],
 ];
 let total = 0;
 for (const [k] of sections) total += (data[k] || []).length;
 if (total === 0) return '<div class="dash-card">✓ 逻辑链无问题</div>';
 let html = `<div class="dash-card"><div class="card-title"> 逻辑链问题 (${total}, high=${sum.by_severity?.high || 0})</div>`;
 for (const [k, label] of sections) {
 const arr = data[k] || [];
 if (!arr.length) continue;
 html += `<div style="margin-top:8px;font-size:11px;color:var(--fg-muted)">${label} (${arr.length})</div>`;
 for (const it of arr) {
 const chIdx = it.chapter_idx;
 const clickable = chIdx ? `issue-card-clickable` : '';
 const dataAttr = chIdx ? `data-chapter-idx="${chIdx}"` : '';
 html += `<div class="issue-card ${it.severity} ${clickable}" ${dataAttr}>
 <div class="head">
 <div class="left">${label}</div>
 <div class="meta">[${it.severity}]${chIdx ? ` · <span class="ch-link">第 ${chIdx} 章 →</span>` : ""}</div>
 </div>
 <div class="body">${ESC(it.context || "")}</div>
 ${it.fix_suggestion ? `<div class="body" style="color:var(--fg-muted);margin-top:4px">建议: ${ESC(it.fix_suggestion)}</div>` : ""}
 </div>`;
 }
 }
 return html + "</div>";
}

function renderStyleScanResult(data) {
 const cont = $("#scan-result");
 cont.innerHTML = renderStyleScanResultHTML(data);
}
function renderStyleScanResultHTML(data) {
 const issues = data.drift_issues || [];
 const curve = data.overall_drift_curve || [];
 if (!issues.length && !curve.length) return '<div class="dash-card">✓ 文风无问题</div>';
 let html = `<div class="dash-card"><div class="card-title"> 文风漂移</div>`;
 if (curve.length) {
 const maxV = Math.max(...curve.map(c => c.distance), 0.1);
 html += `<div style="display:flex;gap:4px;align-items:flex-end;height:60px;margin:8px 0">`;
 for (const c of curve) {
 const h = (c.distance / maxV * 50) + 4;
 const col = c.distance > 2 ? "var(--danger)" : c.distance > 1.2 ? getCssVar("--warning") : "var(--success)";
 html += `<div style="flex:1;height:${h}px;background:${col};border-radius:2px 2px 0 0" title="第${c.idx}章: ${c.distance}"></div>`;
 }
 html += `</div><div style="display:flex;gap:4px;font-size:11px;color:var(--fg-dim)">`;
 for (const c of curve) html += `<div style="flex:1;text-align:center">${c.idx}</div>`;
 html += `</div>`;
 }
 for (const it of issues) {
 const chIdx = it.chapter_idx;
 const clickable = chIdx ? `issue-card-clickable` : '';
 const dataAttr = chIdx ? `data-chapter-idx="${chIdx}"` : '';
 html += `<div class="issue-card ${it.severity} ${clickable}" ${dataAttr}>
 <div class="head">
 <div class="left">第${chIdx}章 ${ESC(it.dimension || "")}</div>
 <div class="meta">[${it.severity}] z=${it.z_score}${chIdx ? ` · <span class="ch-link">→ 编辑器</span>` : ""}</div>
 </div>
 <div class="body">${ESC(it.context || "")}</div>
 ${it.fix_suggestion ? `<div class="body" style="color:var(--fg-muted);margin-top:4px">建议: ${ESC(it.fix_suggestion)}</div>` : ""}
 </div>`;
 }
 return html + "</div>";
}

async function renderThreadScan() { setToolHeader("伏笔深度扫描", "扫描伏笔烂尾、超期、提前揭晓。"); setToolBody(`<div style="margin-bottom:12px"><button class="btn primary" id="btn-run">开始扫描</button></div><div id="scan-result"></div>`); $("#btn-run").onclick = () => runOneScan("threads"); }
async function renderLogicScan() { setToolHeader("逻辑链深度扫描", "死人复活 / 因果倒置 / 地点冲突 / 信息泄漏 / 链断裂。"); setToolBody(`<div style="margin-bottom:12px"><button class="btn primary" id="btn-run">开始扫描</button></div><div id="scan-result"></div>`); $("#btn-run").onclick = () => runOneScan("logic"); }
async function renderStyleScan() { setToolHeader("文风漂移扫描", "句长 / 独白 / 描写密度 的章节间差异。"); setToolBody(`<div style="margin-bottom:12px"><button class="btn primary" id="btn-run">开始扫描</button></div><div id="scan-result"></div>`); $("#btn-run").onclick = () => runOneScan("style"); }
async function renderDriftScan() { setToolHeader("性格漂移扫描", "检查人物 MBTI 与实际表现的偏差。"); setToolBody(`<p class="placeholder loading">加载中…</p>`);
 try {
 const data = await API.get("/personality_drift");
 const results = data.results || [];
 if (!results.length) {
 setToolBody(emptyStateHTML({
 icon: "",
 title: "还没有 MBTI 标注人物",
 desc: "先给主要 5-8 个人物设置 16 型人格（INTJ、INFP 等），性格漂移扫描才有意义。",
 cta: { label: " 去标 MBTI", onclick: "goto('mbti')" },
 }));
 return;
 }
 const byChar = {};
 for (const r of results) (byChar[r.char_id] = byChar[r.char_id] || []).push(r);
 let html = '';
 for (const cid in byChar) {
 const rows = byChar[cid];
 const sigRows = rows.filter(r => r.drift_signals && r.drift_signals.length);
 if (!sigRows.length) continue;
 html += `<div class="dash-card"><div class="card-title">${ESC(rows[0].char_name)} (${ESC(rows[0].mbti_baseline)})</div>`;
 for (const r of sigRows) {
 html += `<div class="issue-card medium">
 <div class="head"><div class="left">第 ${r.chapter_idx} 章 ${ESC(r.chapter_title || "")}</div><div class="meta">baseline 重叠 ${(r.baseline_overlap || 0).toFixed(2)}</div></div>
 ${r.drift_signals.map(s => `<div class="body">! ${ESC(s)}</div>`).join("")}
 </div>`;
 }
 html += `</div>`;
 }
 if (!html) html = emptyStateHTML({
 icon: "✓",
 title: "全本性格稳定",
 desc: "所有标注 MBTI 的人物在所有章节里都没出现明显性格漂移。",
 });
 setToolBody(html);
 } catch (e) { setToolBody(errorStateHTML(e.message || e, "location.reload()")); }
}

// =================== 优化 ===================
async function renderOptimizeView() {
 setToolHeader("优化建议总览", "所有 AI 生成的修改建议。可以按类型/状态过滤。");
 setToolBody(`
 <div class="opt-quick-row">
 <select id="opt-type" class="opt-select"><option value="all">全部类型</option><option value="personality">性格</option><option value="arc">成长线</option><option value="relationship">交会</option><option value="global">全局</option></select>
 <select id="opt-status" class="opt-select"><option value="open">待处理</option><option value="applied">已应用</option><option value="dismissed">已忽略</option><option value="">全部</option></select>
 <span id="opt-count" style="color:var(--fg-muted);font-size:11px"></span>
 </div>
 <div id="opt-list"></div>
 `);
 $("#opt-type").onchange = loadSugList;
 $("#opt-status").onchange = loadSugList;
 await loadSugList();
}

async function loadSugList() {
 const t = $("#opt-type").value;
 const s = $("#opt-status").value;
 const params = new URLSearchParams();
 if (t !== "all") params.set("target_type", t);
 if (s) params.set("status", s);
 try {
 const data = await API.get("/suggestions?" + params.toString());
 $("#opt-count").textContent = `共 ${data.length} 条`;
 const cont = $("#opt-list");
 if (!data.length) {
 cont.innerHTML = emptyStateHTML({
 icon: "",
 title: "还没有建议",
 desc: s === "open" ? "先用左侧「扫描」找出问题，然后让 AI 生成可执行修改建议。" : "当前过滤条件下没有建议。",
 cta: s === "open" ? { label: " 跑全本扫描", onclick: "goto('scan')" } : null,
 });
 return;
 }
 cont.innerHTML = "";
 for (const s of data) cont.appendChild(_sugCard(s));
 } catch (e) { $("#opt-list").innerHTML = errorStateHTML(e.message || e, "loadSugList()"); }
}

function _sugCard(s) {
 const card = document.createElement("div");
 card.className = `opt-card ${s.priority} ${s.status}`;
 const typeLabel = {personality:" 性格", arc:" 成长线", relationship:" 交会", global:" 全局"}[s.target_type] || s.target_type;
 const statusLabel = {open:"待处理", applied:"✓ 已应用", dismissed:"✕ 已忽略"}[s.status] || s.status;
 card.innerHTML = `
 <div class="opt-head">
 <div class="opt-title">${ESC(s.title || "")}</div>
 <div class="opt-meta">
 <span class="opt-badge">${typeLabel}</span>
 <span class="opt-badge">${s.priority}</span>
 <span class="opt-badge">${statusLabel}</span>
 <span>#${s.id}</span>
 </div>
 </div>
 <div style="color:var(--fg-muted);font-size:11px;margin-bottom:4px">${s.target_label ? "目标: " + ESC(s.target_label) : ""} ${s.chapter_focus ? " · 范围: " + ESC(s.chapter_focus) : ""}</div>
 <div class="opt-content">${ESC(s.content || "")}</div>
 ${s.evidence ? `<div class="opt-evidence"><b>依据：</b>${ESC(s.evidence)}</div>` : ""}
 ${s.status === "open" ? `<div class="opt-actions"><button class="btn small primary" data-act="apply" data-id="${s.id}">✓ 已应用</button><button class="btn small" data-act="dismiss" data-id="${s.id}">✕ 忽略</button></div>` : ""}
 `;
 card.querySelectorAll("[data-act]").forEach(btn => {
 btn.onclick = async () => {
 try {
 await API.post(`/suggestion/${btn.dataset.act}/${btn.dataset.id}`);
 loadSugList();
 } catch (e) {
 addLog("error", `[suggestion] 操作失败: ${e.message || e}`);
 toastError("操作失败", e);
 }
 };
 });
 return card;
}

async function renderOptAll() {
 setToolHeader("全局优化", "整体扫描后给综合建议。会考虑人物分布、伏笔、关系、节奏。");
 setToolBody(`
 <div class="form-row"><label>说明</label><div style="color:var(--fg-muted);font-size:12px;line-height:1.6">基于项目整体 briefing（人物 + 关系 + 伏笔 + 扫描 + 漂移热点）生成 3-5 条综合洞察。</div></div>
 <div style="margin-top:8px"><button class="btn primary" id="btn-go"> 开始生成</button> <span style="color:var(--fg-muted);font-size:11px;margin-left:8px">首次调用 LLM 需要 10-30 秒</span></div>
 <div id="opt-result" style="margin-top:16px"><p class="placeholder" style="padding:20px">点击"开始生成"后，AI 的综合优化建议会显示在这里。</p></div>
 `);
 $("#btn-go").onclick = async () => {
 if (!acquireUiLock("optimize")) { addLog("warn", "[optimize] 上一次还在跑, 请稍等"); return; }
 await withButtonLock($("#btn-go"), async () => {
 $("#opt-result").innerHTML = '<p class="placeholder"><span class="spinner"></span> LLM 生成中…</p>';
 try {
 const r = await API.post("/optimize/all", {}, LLM_TIMEOUT_MS);
 $("#opt-result").innerHTML = `<p style="color:var(--success);margin-bottom:8px">✓ 生成 ${r.count} 条建议 (已入库)</p>` + r.suggestions.map((s, i) => _sugHTML(s, i)).join("");
 } catch (e) { $("#opt-result").innerHTML = `<p class="placeholder">${ESC(e.message || e)}</p>`; }
 }, { runningText: " 生成中…" });
 releaseUiLock("optimize");
 };
}

function _sugHTML(s, i) {
 return `<div class="opt-card ${s.priority}">
 <div class="opt-head"><div class="opt-title">#${i+1} ${ESC(s.title || "")}</div><div class="opt-meta"><span class="opt-badge">${s.priority}</span></div></div>
 <div style="color:var(--fg-muted);font-size:11px;margin-bottom:4px">${s.chapter_focus ? "范围: " + ESC(s.chapter_focus) : ""}</div>
 <div class="opt-content">${ESC(s.content || "")}</div>
 ${s.evidence ? `<div class="opt-evidence"><b>依据：</b>${ESC(s.evidence)}</div>` : ""}
 </div>`;
}

function renderOptPersonality() {
 setToolHeader("性格优化", "针对单个人物：基于 MBTI baseline + 漂移数据 + milestone，让 LLM 给出性格修改建议。");
 setToolBody(`
 <div class="form-row"><label>人物名</label><input type="text" id="op-name" placeholder="如 沈青砚"></div>
 <div style="margin-top:8px"><button class="btn primary" id="btn-go"> 生成性格建议</button></div>
 <div id="opt-result" style="margin-top:16px"></div>
 `);
 $("#btn-go").onclick = async () => {
 const name = $("#op-name").value.trim();
 if (!name) { showToast("请填人物名", "warning"); return; }
 if (!acquireUiLock("optimize")) { addLog("warn", "[optimize] 上一次还在跑, 请稍等"); return; }
 await withButtonLock($("#btn-go"), async () => {
 $("#opt-result").innerHTML = '<p class="placeholder"><span class="spinner"></span> LLM 生成中…</p>';
 try {
 const r = await API.post("/optimize/personality", {name}, LLM_TIMEOUT_MS);
 $("#opt-result").innerHTML = `<p style="color:var(--success);margin-bottom:8px">✓ 生成 ${r.count} 条建议 (已入库)</p>` + r.suggestions.map((s, i) => _sugHTML(s, i)).join("");
 } catch (e) { $("#opt-result").innerHTML = `<p class="placeholder">${ESC(e.message || e)}</p>`; }
 }, { runningText: " 生成中…" });
 releaseUiLock("optimize");
 };
}

function renderOptArc() {
 setToolHeader("成长线优化", "针对单个人物：基于 arc_type + 进度 + milestone，判断弧光是否合理。");
 setToolBody(`
 <div class="form-row"><label>人物名</label><input type="text" id="op-name"></div>
 <div style="margin-top:8px"><button class="btn primary" id="btn-go"> 生成成长线建议</button></div>
 <div id="opt-result" style="margin-top:16px"></div>
 `);
 $("#btn-go").onclick = async () => {
 const name = $("#op-name").value.trim();
 if (!name) { showToast("请填人物名", "warning"); return; }
 if (!acquireUiLock("optimize")) { addLog("warn", "[optimize] 上一次还在跑, 请稍等"); return; }
 await withButtonLock($("#btn-go"), async () => {
 $("#opt-result").innerHTML = '<p class="placeholder"><span class="spinner"></span> LLM 生成中…</p>';
 try {
 const r = await API.post("/optimize/arc", {name}, LLM_TIMEOUT_MS);
 $("#opt-result").innerHTML = `<p style="color:var(--success);margin-bottom:8px">✓ ${r.count} 条建议</p>` + r.suggestions.map((s, i) => _sugHTML(s, i)).join("");
 } catch (e) { $("#opt-result").innerHTML = `<p class="placeholder">${ESC(e.message || e)}</p>`; }
 }, { runningText: " 生成中…" });
 releaseUiLock("optimize");
 };
}

function renderOptRelationship() {
 setToolHeader("人物交会优化", "针对一对人物：基于 MBTI 兼容性 + 关系演变曲线，给出关系深化建议。");
 setToolBody(`
 <div class="form-row"><label>A 人物</label><input type="text" id="op-a"></div>
 <div class="form-row"><label>B 人物</label><input type="text" id="op-b"></div>
 <div style="margin-top:8px"><button class="btn primary" id="btn-go"> 生成交会建议</button></div>
 <div id="opt-result" style="margin-top:16px"></div>
 `);
 $("#btn-go").onclick = async () => {
 const a = $("#op-a").value.trim();
 const b = $("#op-b").value.trim();
 if (!a || !b) { showToast("请填 A 和 B", "warning"); return; }
 if (!acquireUiLock("optimize")) { addLog("warn", "[optimize] 上一次还在跑, 请稍等"); return; }
 await withButtonLock($("#btn-go"), async () => {
 $("#opt-result").innerHTML = '<p class="placeholder"><span class="spinner"></span> LLM 生成中…</p>';
 try {
 const r = await API.post("/optimize/relationship", {a, b}, LLM_TIMEOUT_MS);
 $("#opt-result").innerHTML = `<p style="color:var(--success);margin-bottom:8px">✓ ${r.count} 条建议</p>` + r.suggestions.map((s, i) => _sugHTML(s, i)).join("");
 } catch (e) { $("#opt-result").innerHTML = `<p class="placeholder">${ESC(e.message || e)}</p>`; }
 }, { runningText: " 生成中…" });
 releaseUiLock("optimize");
 };
}

// =================== 可视化视图（保留） ===================
const _charts = {};
function renderTimeline() {
 setToolHeader("时间线", "故事内时间轴：章节条 + 事件点 + 伏笔标记。");
 setToolBody(`<div id="chart-timeline" class="chart"></div>`);
 API.get("/timeline").then(tl => {
 const el = document.getElementById("chart-timeline");
 if (!el) return; // 用户已切走视图, DOM 已被替换
 // 空数据占位
 if (!tl.chapter_ranges?.length && !tl.event_points?.length) {
 el.innerHTML = '<p class="placeholder" style="padding:40px">暂无事件数据。请先在"AI 抽取"或"写章"流程中抽取事件。</p>';
 return;
 }
 if (_charts.timeline) _charts.timeline.dispose();
 const chart = echarts.init(el);
 _charts.timeline = chart;
 const unit = STATE.dashboard?.project?.story_time_unit || "日";
 const ch1 = tl.chapter_ranges.map(r => ({name: r.name, value: r.value, itemStyle:{color:getCssVar("--bg-elevated"), borderColor:getCssVar("--accent"), borderWidth:1}}));
 const ev1 = tl.event_points.map(p => ({name: p.name, value: p.value, symbolSize: 8 + p.value[3] * 2, itemStyle:{color: typeColor(p.value[4])}}));
 chart.setOption({
 backgroundColor: "transparent", tooltip: {trigger: "item"},
 legend: {textStyle:{color:getCssVar("--fg-muted")}, top: 0, data:["章节","事件"]},
 grid: {left: 60, right: 30, top: 40, bottom: 30},
 xAxis: {type: "value", name: "故事内时间（" + unit + "）", nameTextStyle: {color: getCssVar("--fg-muted")}, axisLine: {lineStyle: {color: getCssVar("--border-strong")}}, axisLabel: {color: getCssVar("--fg-muted")}, splitLine: {lineStyle: {color: getCssVar("--border")}}},
 yAxis: {type: "value", min: 0, max: 3, interval: 1, axisLine: {lineStyle: {color: getCssVar("--border-strong")}}, axisLabel: {color: getCssVar("--fg-muted"), formatter: v => ({0:"揭晓",1:"事件",2:"伏笔",3:"章节"})[v] || ""}, splitLine: {lineStyle: {color: getCssVar("--border")}}},
 series: [
 {name: "章节", type: "custom", renderItem: function (params, api) {
 const x1 = api.value(0), x2 = api.value(1), y = api.value(3);
 const start = api.coord([x1, y]); const end = api.coord([x2, y]);
 return {type: "rect", shape: {x: start[0], y: start[1] - 9, width: Math.max(end[0] - start[0], 2), height: 18, r: 3}, style: {fill: getCssVar("--bg-elevated"), stroke: getCssVar("--accent"), lineWidth: 1}};
 }, encode: {x: [0, 1], y: 3}, data: ch1, z: 1},
 {name: "事件", type: "scatter", data: ev1, z: 3, label: {show: true, position: "right", color: getCssVar("--fg"), fontSize: 10, formatter: p => p.name}},
 ],
 });
 }).catch(() => {});
}

function typeColor(t) { return ({action:getCssVar("--accent-hover"),dialogue:getCssVar("--fg-muted"),revelation:getCssVar("--accent-soft"),turning_point:getCssVar("--danger"),decision:getCssVar("--warning"),discovery:getCssVar("--success")})[t] || getCssVar("--accent-hover"); }

function renderChain() {
 setToolHeader("事件因果链", "节点=事件，边=cause_event_ids 关系。");
 setToolBody(`<div id="chart-chain" class="chart"></div>`);
 API.get("/events").then(events => {
 const el = document.getElementById("chart-chain");
 if (!el) return; // 用户已切走视图
 if (!events || !events.length) {
 el.innerHTML = emptyStateHTML({
 icon: "", title: "还没有事件数据",
 desc: "先在「事件 / 伏笔 / 事实」里录入事件，或用 AI 自动抽取，因果链图会在这里呈现。",
 });
 return;
 }
 if (_charts.chain) _charts.chain.dispose();
 _charts.chain = echarts.init(el);
 // name 用事件 id 保证唯一: ECharts graph 以 name 作节点 id, 同名事件会被合并
 const nodes = events.map(e => ({
 name: String(e.id), symbolSize: 12 + (e.importance || 3) * 3, value: e,
 category: e.event_type || "action",
 label: {show: true, formatter: e.title, position: "right", fontSize: 10, color: getCssVar("--fg")},
 itemStyle: {color: typeColor(e.event_type)},
 }));
 const links = [];
 for (const e of events) for (const cid of (e.cause_event_ids || [])) {
 const cause = events.find(x => x.id === cid);
 if (cause) links.push({source: String(cause.id), target: String(e.id), lineStyle: {color: getCssVar("--accent"), width: 1.2, curveness: 0.15}});
 }
 _charts.chain.setOption({
 backgroundColor: "transparent", tooltip: {trigger: "item"},
 series: [{type: "graph", layout: "force", roam: true, data: nodes, links: links,
 categories: [
 {name:"action", itemStyle:{color:typeColor("action")}},
 {name:"dialogue", itemStyle:{color:typeColor("dialogue")}},
 {name:"revelation", itemStyle:{color:typeColor("revelation")}},
 {name:"turning_point", itemStyle:{color:typeColor("turning_point")}},
 {name:"decision", itemStyle:{color:typeColor("decision")}},
 {name:"discovery", itemStyle:{color:typeColor("discovery")}},
 ],
 force: {repulsion: 220, edgeLength: 80, gravity: 0.05},
 emphasis: {focus: "adjacency", lineStyle: {width: 3}},
 }],
 });
 }).catch(() => {});
}

function renderRhythm() {
 setToolHeader("节奏曲线", "每章字数、事件数、伏笔推进、一致性 high 问题。");
 setToolBody(`<div id="chart-rhythm" class="chart"></div>`);
 API.get("/rhythm").then(r => {
 const el = document.getElementById("chart-rhythm");
 if (!el) return; // 用户已切走视图
 if (!r.idx?.length) { el.innerHTML = '<p class="placeholder">暂无数据</p>'; return; }
 if (_charts.rhythm) _charts.rhythm.dispose();
 _charts.rhythm = echarts.init(el);
 const xData = r.idx.map(i => `第 ${i} 章`);
 _charts.rhythm.setOption({
 backgroundColor: "transparent", tooltip: {trigger: "axis"}, legend: {textStyle:{color:getCssVar("--fg-muted")}, top: 0},
 grid: {left: 60, right: 60, top: 40, bottom: 30},
 xAxis: {type: "category", data: xData, axisLine:{lineStyle:{color:getCssVar("--border-strong")}}, axisLabel:{color:getCssVar("--fg-muted")}},
 yAxis: [
 {type: "value", name: "字数", position: "left", axisLine:{lineStyle:{color:getCssVar("--border-strong")}}, axisLabel:{color:getCssVar("--fg-muted")}, splitLine:{lineStyle:{color:getCssVar("--border")}}},
 {type: "value", name: "数量", position: "right", axisLine:{lineStyle:{color:getCssVar("--border-strong")}}, axisLabel:{color:getCssVar("--fg-muted")}, splitLine:{show:false}},
 ],
 series: [
 {name:"字数", type:"bar", data:r.words, itemStyle:{color:getCssVar("--bg-elevated")}},
 {name:"事件数", type:"line", data:r.event_count, smooth:true, itemStyle:{color:getCssVar("--accent-hover")}, yAxisIndex: 1},
 {name:"事件平均重要度", type:"line", data:r.event_importance_avg, smooth:true, itemStyle:{color:getCssVar("--accent-soft")}, yAxisIndex: 1},
 {name:"新增伏笔", type:"bar", data:r.threads_new, itemStyle:{color:getCssVar("--warning")}, yAxisIndex: 1},
 {name:"未解决伏笔", type:"line", data:r.threads_unresolved, smooth:true, itemStyle:{color:getCssVar("--warning")}, yAxisIndex: 1},
 {name:"一致性 high", type:"scatter", data:r.consistency_high, symbolSize: 14, itemStyle:{color:getCssVar("--danger")}, yAxisIndex: 1},
 ],
 });
 }).catch(() => {});
}

function renderMatrix() {
 setToolHeader("人物 MBTI 矩阵", "性格对照 + 兼容性热力图。");
 setToolBody(`<div id="matrix-container" class="chart"></div>`);
 API.get("/character_matrix").then(data => {
 const chars = data.characters || [];
 const matrix = data.matrix || {};
 const container = document.getElementById("matrix-container");
 if (!container) return; // 用户已切走视图
 if (!chars.length) { container.innerHTML = '<p class="placeholder">还没有 MBTI 标注人物。左侧"建库 → MBTI 标注"设置</p>'; return; }
 container.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;margin-bottom:16px">${
 chars.map(c => `<div style="background:var(--bg-elevated);border:1px solid var(--border);border-radius:6px;padding:10px">
 <div style="display:flex;justify-content:space-between;align-items:center">
 <div style="font-weight:600;color:var(--fg)">${ESC(c.name)}</div>
 <div style="font-family:monospace;background:var(--bg-elevated);padding:2px 6px;border-radius:3px;color:var(--accent)">${ESC(c.mbti)}</div>
 </div>
 <div style="color:var(--fg-muted);font-size:11px;margin-top:4px">${c.stack_str || ""}</div>
 <div style="color:var(--fg-muted);font-size:11px">${c.arc_type || "未设"} · 进度 ${((c.arc_progress || 0)*100).toFixed(0)}%</div>
 <div style="height:4px;background:var(--border);border-radius:2px;margin-top:4px;overflow:hidden">
 <div style="height:100%;width:${((c.arc_progress || 0)*100).toFixed(0)}%;background:var(--accent)"></div>
 </div>
 </div>`).join("")
 }</div>
 <div id="matrix-heatmap" style="height:${Math.max(220, 80 + chars.length * 30)}px;background:var(--bg-elevated);border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:16px"></div>
 <table class="simple-table">
 <thead><tr><th>A</th><th>B</th><th>分</th><th>共享</th><th>解读</th></tr></thead>
 <tbody>${(() => {
 const names = chars.map(c => c.name);
 const seen = new Set();
 let rows = "";
 for (let i = 0; i < names.length; i++) for (let j = i + 1; j < names.length; j++) {
 const d = matrix[names[i]]?.[names[j]];
 if (!d) continue;
 const c = d.score >= 0.75 ? "var(--success)" : d.score >= 0.5 ? "var(--accent)" : d.score >= 0.3 ? getCssVar("--warning") : "var(--danger)";
 rows += `<tr><td>${ESC(names[i])}</td><td>${ESC(names[j])}</td><td style="color:${c};font-weight:600">${d.score.toFixed(2)}</td><td style="color:var(--accent)">${d.shared_functions || 0}/4</td><td>${ESC(d.interpretation || "")}</td></tr>`;
 }
 return rows;
 })()}</tbody>
 </table>`;
 const names = chars.map(c => c.name);
 const cellData = [];
 for (let i = 0; i < names.length; i++) for (let j = 0; j < names.length; j++) {
 if (i === j) continue;
 cellData.push([j, i, matrix[names[i]]?.[names[j]]?.score || 0]);
 }
 if (_charts.matrix) _charts.matrix.dispose(); // P1-F: 防止反复 init 累积
 const chart = echarts.init(document.getElementById("matrix-heatmap"));
 _charts.matrix = chart;
 chart.setOption({
 backgroundColor: "transparent",
 tooltip: {position: "top", formatter: p => `${names[p.value[1]]} ↔ ${names[p.value[0]]}<br/>兼容性: ${p.value[2].toFixed(2)}`},
 grid: {left: 80, right: 20, top: 20, bottom: 60},
 xAxis: {type: "category", data: names, axisLabel: {color: getCssVar("--fg-muted"), rotate: 30, fontSize: 11}, splitArea: {show: true}},
 yAxis: {type: "category", data: names, axisLabel: {color: getCssVar("--fg-muted"), fontSize: 11}, splitArea: {show: true}},
 visualMap: {min: 0, max: 1, calculable: true, orient: "horizontal", left: "center", bottom: 0,
 inRange: {color: [getCssVar("--danger"), getCssVar("--warning"), getCssVar("--success"), getCssVar("--accent")]}, textStyle: {color: getCssVar("--fg-muted")}},
 series: [{type: "heatmap", data: cellData, label: {show: true, formatter: p => p.value[2].toFixed(2), color: getCssVar("--fg"), fontSize: 10}}],
 });
 }).catch(() => {});
}

function renderArcs() {
 setToolHeader("人物成长线", "每个有 MBTI 的人物：弧光进度 + 里程碑。");
 setToolBody(`<div id="arcs-container"></div>`);
 API.get("/character_arcs").then(data => {
 const cont = $("#arcs-container");
 if (!cont) return; // 用户已切走视图
 const chars = data.characters || [];
 if (!chars.length) { cont.innerHTML = '<p class="placeholder">没有人物</p>'; return; }
 const withMbti = chars.filter(c => c.mbti);
 if (!withMbti.length) { cont.innerHTML = '<p class="placeholder">还没有 MBTI 标注人物</p>'; return; }
 let html = "";
 for (const c of withMbti) {
 const ms = c.milestones || [];
 const prog = c.arc_progress || 0;
 const bar = "█".repeat(Math.round(prog * 30)) + "░".repeat(30 - Math.round(prog * 30));
 html += `<div class="dash-card">
 <div style="display:flex;justify-content:space-between;align-items:center">
 <div><span style="font-weight:600;color:var(--fg);font-size:14px">${ESC(c.name)}</span> <span style="font-family:monospace;background:var(--bg-elevated);padding:2px 6px;border-radius:3px;color:var(--accent);margin-left:6px">${ESC(c.mbti)}</span> <span style="color:var(--fg-muted);margin-left:8px;font-size:11px">${ESC(c.arc_type || "未设")}</span></div>
 <div style="font-size:11px;color:var(--fg-muted)">${ms.length} 个里程碑</div>
 </div>
 <div style="margin-top:6px;font-size:11px;color:var(--fg-muted)">弧光进度 [${bar}] ${(prog*100).toFixed(0)}%</div>
 ${ms.length ? `<div style="margin-top:8px;border-top:1px solid var(--border);padding-top:8px">${ms.map(m => `
 <div style="padding:4px 0;border-bottom:1px dashed var(--border);font-size:12px">
 <span class="badge" style="background:var(--bg-elevated);color:var(--accent)">${ESC(m.milestone_type)}</span>
 ${m.dimension ? `<span class="badge" style="background:var(--accent-soft);color:var(--accent)">${ESC(m.dimension)}</span>` : ""}
 <div style="margin-top:3px;color:var(--fg-dim)">${ESC(m.description)}</div>
 ${(m.before_state || m.after_state) ? `<div style="color:var(--fg-muted);font-size:11px;margin-top:2px">${ESC(m.before_state || "?")} → ${ESC(m.after_state || "?")}</div>` : ""}
 </div>`).join("")}</div>` : ""}
 </div>`;
 }
 cont.innerHTML = html;
 }).catch(() => {});
}

function renderRelCurve() {
 setToolHeader("亲密度曲线", "每对人物：亲密度/信任/冲突 三轴时间线。");
 setToolBody(`<div id="relcurve-container"></div>`);
 API.get("/relationship_evolution").then(data => {
 const cont = $("#relcurve-container");
 if (!cont) return; // 用户已切走视图
 const series = data.series || [];
 if (!series.length) { cont.innerHTML = '<p class="placeholder" style="padding:30px">还没有关系演变数据。<br><span style="font-size:11px;color:var(--fg-dim)">关系演变会在 AI 写章时自动记录——人物之间的亲密度、信任、冲突如何随章节变化。</span></p>'; return; }
 let html = "";
 for (const s of series) {
 const evs = s.evolutions || [];
 html += `<div class="dash-card">
 <div style="font-weight:600;color:var(--fg)">${ESC(s.a)} <span style="color:var(--fg-muted)">↔</span> ${ESC(s.b)}</div>
 <div style="color:var(--fg-muted);font-size:11px;margin-top:2px">${ESC(s.rel_type || "—")} · ${ESC(s.current_state || "")}</div>
 <div id="rc-${s.relationship_id}" style="height:200px;margin-top:8px"></div>
 <div style="margin-top:8px;display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:6px">${evs.map(e => `
 <div style="background:var(--bg-elevated);border:1px solid var(--border);border-radius:4px;padding:6px;font-size:11px">
 <div style="color:var(--fg-muted)">ch ${e.chapter_id}</div>
 <div style="color:var(--fg-dim)">亲密 ${e.intimacy != null ? (e.intimacy > 0 ? "+" : "") + e.intimacy.toFixed(2) : "—"}</div>
 <div style="color:var(--fg-dim)">信任 ${e.trust != null ? (e.trust > 0 ? "+" : "") + e.trust.toFixed(2) : "—"}</div>
 ${e.conflict != null ? `<div style="color:var(--fg-dim)">冲突 ${e.conflict.toFixed(2)}</div>` : ""}
 ${e.dynamics ? `<div style="color:var(--accent);margin-top:2px">${ESC(e.dynamics)}</div>` : ""}
 </div>`).join("")}</div>
 </div>`;
 }
 cont.innerHTML = html;
 // 清理旧的 ECharts 实例防止内存泄漏
 for (const k in _relCurveCharts) { _relCurveCharts[k].dispose(); }
 _relCurveCharts = {};
 for (const s of series) {
 const el = document.getElementById(`rc-${s.relationship_id}`);
 if (!el) continue;
 const evs = s.evolutions || [];
 if (!evs.length) continue;
 if (_relCurveCharts[s.relationship_id]) _relCurveCharts[s.relationship_id].dispose();
 const chart = echarts.init(el);
 _relCurveCharts[s.relationship_id] = chart;
 chart.setOption({
 backgroundColor: "transparent", tooltip: {trigger: "axis"},
 legend: {textStyle:{color:getCssVar("--fg-muted")}, top: 0, data:["亲密度","信任","冲突"]},
 grid: {left: 40, right: 20, top: 30, bottom: 30},
 xAxis: {type: "category", data: evs.map(e => `ch${e.chapter_id}`), axisLabel:{color:getCssVar("--fg-muted"), fontSize: 10}},
 yAxis: {type: "value", min: -1, max: 1, axisLabel:{color:getCssVar("--fg-muted")}, splitLine:{lineStyle:{color:getCssVar("--border")}}},
 series: [
 {name:"亲密度", type:"line", smooth:true, data:evs.map(e => e.intimacy), itemStyle:{color:getCssVar("--danger")}, lineStyle:{color:getCssVar("--danger")}},
 {name:"信任", type:"line", smooth:true, data:evs.map(e => e.trust), itemStyle:{color:getCssVar("--accent-hover")}, lineStyle:{color:getCssVar("--accent-hover")}},
 {name:"冲突", type:"line", smooth:true, data:evs.map(e => e.conflict ?? 0), itemStyle:{color:getCssVar("--warning")}, lineStyle:{color:getCssVar("--warning")}},
 ],
 });
 }
 }).catch(() => {});
}

function renderNetwork() {
 setToolHeader("人物关系网", "边宽=亲密度·颜色=信任·虚线=冲突·流动=强关系");
 setToolBody(`<div id="net-chart" class="chart"></div>`);
 if (_charts.network) _charts.network.dispose();
 API.get("/relationship_network").then(({nodes, edges}) => {
 const el = document.getElementById("net-chart");
 if (!el) return; // 用户已切走视图
 if (!nodes?.length) {
 el.innerHTML = '<p class="placeholder" style="padding:40px">暂无人物数据。请先添加人物。</p>';
 return;
 }
 if (!edges?.length) {
 // 有人物但无关系——仍渲染节点图（只是没边）
 }
 _charts.network = echarts.init(el);
 // 借鉴 StoryForge 视觉编码：边宽=intimacy、颜色=trust、虚线=conflict、流动=强关系
 // 我们三维比 StoryForge 标量 strength 更细
 const acc = getCssVar("--accent"), dang = getCssVar("--danger"), warn = getCssVar("--warning");
 const processedEdges = edges.map(e => {
 const intim = e.intimacy !== null && e.intimacy !== undefined ? e.intimacy : 0;
 const trust = e.trust !== null && e.trust !== undefined ? e.trust : 0;
 const conf = e.conflict !== null && e.conflict !== undefined ? e.conflict : 0;
 const absIntim = Math.abs(intim);
 // 边宽：1~5，按 |intimacy| 缩放
 const width = 1 + absIntim * 4;
 // 边颜色：trust>0 → 冰蓝(accent)，trust<0 → 红(danger)，中性 → fg-muted
 const color = trust > 0.2 ? acc : trust < -0.2 ? dang : getCssVar("--fg-muted");
 // conflict 高 → 虚线
 const lineType = conf > 0.4 ? "dashed" : "solid";
 // 强关系（|intimacy|>0.6）加流动光点（借鉴 StoryForge strength>0.7 的动画）
 const showEffect = absIntim > 0.6;
 // 敌对关系（intimacy<0）标签用红
 const labelColor = intim < -0.3 ? dang : getCssVar("--fg-muted");
 return {
 source: e.source, target: e.target,
 value: `${e.rel_type}（亲密度${intim.toFixed(1)} 信任${trust.toFixed(1)} 冲突${conf.toFixed(1)}）`,
 lineStyle: {width, color, type: lineType, curveness: 0.2, opacity: 0.8},
 label: {show: true, formatter: e.rel_type, fontSize: 10, color: labelColor},
 symbolSize: [6, 6],
 ...(showEffect ? {lineStyle: {...{width, color, type: lineType, curveness: 0.2}, opacity: 0.9}} : {}),
 };
 });
 _charts.network.setOption({
 backgroundColor: "transparent",
 tooltip: {
 trigger: "edge",
 formatter: p => p.data && p.data.value ? `${p.data.source} ↔ ${p.data.target}<br/>${p.data.value}` : "",
 },
 series: [{
 type: "graph", layout: "force", roam: true,
 data: nodes,
 links: processedEdges,
 categories: [
 {name: "protagonist", itemStyle: {color: acc}},
 {name: "antagonist", itemStyle: {color: dang}},
 {name: "major", itemStyle: {color: getCssVar("--info")}},
 {name: "supporting", itemStyle: {color: getCssVar("--fg-muted")}},
 {name: "minor", itemStyle: {color: getCssVar("--fg-dim")}},
 ],
 label: {show: true, position: "right", color: getCssVar("--fg"), fontSize: 12},
 force: {repulsion: 320, edgeLength: [80, 200], gravity: 0.04},
 emphasis: {focus: "adjacency", lineStyle: {width: 6}},
 lineStyle: {color: getCssVar("--fg-muted"), width: 1.5},
 }],
 });
 }).catch(() => {});
}

// =================== 知识图谱（统一异构图）===================
let _kgFilter = {character: true, event: true, thread: true, fact: true, world: true};
let _kgData = null;

function renderKnowledgeGraph() {
 setToolHeader("知识图谱", "人物 · 事件 · 伏笔 · 事实 · 世界观 的统一关系图。拖拽节点、滚轮缩放、点击查看详情。");
 setToolBody(`
 <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px" id="kg-filters">
  <label style="display:flex;align-items:center;gap:3px;cursor:pointer;font-size:11px"><input type="checkbox" data-type="character" checked> <span style="color:var(--accent)">●</span> 人物</label>
  <label style="display:flex;align-items:center;gap:3px;cursor:pointer;font-size:11px"><input type="checkbox" data-type="event" checked> <span style="color:#5E81AC">●</span> 事件</label>
  <label style="display:flex;align-items:center;gap:3px;cursor:pointer;font-size:11px"><input type="checkbox" data-type="thread" checked> <span style="color:var(--warning)">●</span> 伏笔</label>
  <label style="display:flex;align-items:center;gap:3px;cursor:pointer;font-size:11px"><input type="checkbox" data-type="fact" checked> <span style="color:#A3BE8C">●</span> 事实</label>
  <label style="display:flex;align-items:center;gap:3px;cursor:pointer;font-size:11px"><input type="checkbox" data-type="world" checked> <span style="color:#88C0D0">●</span> 世界观</label>
  <span id="kg-stats" style="color:var(--fg-dim);font-size:11px;margin-left:auto"></span>
 </div>
 <div id="kg-chart" class="chart"><p class="placeholder" style="padding:40px"><span class="spinner"></span> 正在构建知识图谱…</p></div>
 `);
 document.querySelectorAll('#kg-filters input[type="checkbox"]').forEach(cb => {
 cb.onchange = () => { _kgFilter[cb.dataset.type] = cb.checked; _renderKGChart(); };
 });
 if (_charts.kg) _charts.kg.dispose();
 API.get("/knowledge_graph").then(data => { _kgData = data; _renderKGChart(); })
 .catch(() => { $("#kg-chart").innerHTML = '<p class="placeholder" style="padding:40px">加载知识图谱失败。</p>'; });
}

function _renderKGChart() {
 if (!_kgData) return;
 const el = document.getElementById("kg-chart");
 if (!el) return;
 const typeColors = {character: getCssVar("--accent"), event: "#5E81AC", thread: getCssVar("--warning"), fact: "#A3BE8C", world: "#88C0D0"};
 const visibleTypes = new Set(Object.entries(_kgFilter).filter(([k,v]) => v).map(([k]) => k));
 const nodeIds = new Set();
 const nodes = [];
 for (const n of (_kgData.nodes || [])) {
 if (!visibleTypes.has(n.nodeType)) continue;
 nodeIds.add(n.id);
 nodes.push({id: n.id, name: n.name, category: n.category, symbolSize: n.symbolSize,
 itemStyle: {color: typeColors[n.nodeType] || "#888"},
 label: {show: n.symbolSize >= 16, fontSize: 10, color: getCssVar("--fg")}, _info: n.info});
 }
 const edgeStyles = {
 relationship: {width: 2, color: getCssVar("--accent"), opacity: 0.5},
 participates: {width: 1, color: getCssVar("--fg-muted"), opacity: 0.2},
 causes: {width: 1.5, color: getCssVar("--danger"), type: "dashed", opacity: 0.4},
 thread_char: {width: 1, color: getCssVar("--warning"), type: "dotted", opacity: 0.3},
 thread_event: {width: 1, color: getCssVar("--warning"), type: "dotted", opacity: 0.3},
 knows: {width: 1, color: "#A3BE8C", opacity: 0.25},
 };
 const links = [];
 for (const l of (_kgData.links || [])) {
 if (!nodeIds.has(l.source) || !nodeIds.has(l.target)) continue;
 links.push({source: l.source, target: l.target, lineStyle: edgeStyles[l.edgeType] || edgeStyles.participates, _edgeType: l.edgeType});
 }
 // 大图截断（>150 节点）
 let displayNodes = nodes, displayLinks = links;
 if (nodes.length > 150) {
 nodes.sort((a, b) => (b.symbolSize || 0) - (a.symbolSize || 0));
 displayNodes = nodes.slice(0, 150);
 const topIds = new Set(displayNodes.map(n => n.id));
 displayLinks = links.filter(l => topIds.has(l.source) && topIds.has(l.target));
 $("#kg-stats").textContent = `${displayNodes.length}/${_kgData.nodes.length} 节点（截断）· ${displayLinks.length} 边`;
 } else {
 $("#kg-stats").textContent = `${nodes.length} 节点 · ${links.length} 边`;
 }
 if (_charts.kg) _charts.kg.dispose();
 _charts.kg = echarts.init(el);
 _charts.kg.setOption({
 backgroundColor: "transparent",
 tooltip: {trigger: "item", formatter: p => p.dataType === "node" ? (p.data._info || p.data.name) : (p.data._edgeType || "关联")},
 series: [{
 type: "graph", layout: "force", roam: true,
 data: displayNodes, links: displayLinks,
 categories: [
 {name: "人物", itemStyle: {color: typeColors.character}},
 {name: "事件", itemStyle: {color: typeColors.event}},
 {name: "伏笔", itemStyle: {color: typeColors.thread}},
 {name: "事实", itemStyle: {color: typeColors.fact}},
 {name: "世界观", itemStyle: {color: typeColors.world}},
 ],
 force: {repulsion: 200, edgeLength: [60, 180], gravity: 0.06},
 emphasis: {focus: "adjacency", lineStyle: {width: 3, opacity: 0.8}},
 lineStyle: {color: getCssVar("--fg-muted"), width: 1, opacity: 0.3, curveness: 0.1},
 }],
 });
}

// =================== Modal ===================
function showModal(title, bodyHTML, onOK) {
 $("#cmd-title").textContent = title;
 $("#cmd-body").innerHTML = bodyHTML;
 $("#cmd-modal").classList.remove("hidden");
 $("#cmd-ok").onclick = onOK;
 $("#cmd-cancel").onclick = hideModal;
 $("#cmd-close").onclick = hideModal;
 // B-新148: 加 mask click 兜底, 跟其他 modal 一致
 $("#cmd-modal .modal-mask").onclick = hideModal;
}
function hideModal() { $("#cmd-modal").classList.add("hidden"); }


// =================== 拖拽导入 ===================
let _dropCounter = 0; // 防误触发：进入子元素也会触发 dragenter
let _dropAutoHideTimer = null; // 兜底定时器

function _showDropOverlay() {
 const overlay = $("#drop-overlay");
 if (!overlay) return;
 overlay.classList.remove("hidden");
 // 多重兜底: 4 秒没新事件自动收 (PyWebView 在 Windows 上 dragleave 经常漏触发)
 if (_dropAutoHideTimer) clearTimeout(_dropAutoHideTimer);
 _dropAutoHideTimer = setTimeout(_hideDropOverlay, 4000);
}

function _hideDropOverlay() {
 _dropCounter = 0;
 if (_dropAutoHideTimer) { clearTimeout(_dropAutoHideTimer); _dropAutoHideTimer = null; }
 const overlay = $("#drop-overlay");
 if (overlay) overlay.classList.add("hidden");
}

function setupDragDrop() {
 const overlay = $("#drop-overlay");
 const cancelBtn = $("#drop-cancel");

 // 取消按钮（pointer-events:auto 让小框可点）
 if (cancelBtn) cancelBtn.addEventListener("click", (e) => {
 e.stopPropagation();
 _hideDropOverlay();
 });

 // 8 个兜底关闭路径, 防止 dragleave 漏触发导致 overlay 卡死
 window.addEventListener("blur", _hideDropOverlay);
 window.addEventListener("pointerup", _hideDropOverlay);
 window.addEventListener("mouseup", _hideDropOverlay);
 window.addEventListener("wheel", _hideDropOverlay, { passive: true });
 window.addEventListener("contextmenu", _hideDropOverlay);
 window.addEventListener("resize", _hideDropOverlay);
 window.addEventListener("popstate", _hideDropOverlay);
 // 任何 keydown 也能关掉 (用户敲键盘就说明没在拖了)
 document.addEventListener("keydown", (e) => {
 if (!overlay.classList.contains("hidden")) _hideDropOverlay();
 });

 // 用 capture 模式: 即使 textarea/modal 阻止冒泡, 也能在最顶层收事件
 window.addEventListener("dragenter", (e) => {
 e.preventDefault();
 _dropCounter++;
 if (e.dataTransfer && Array.from(e.dataTransfer.items || []).some(it => it.kind === "file")) {
 _showDropOverlay();
 }
 }, true);
 window.addEventListener("dragover", (e) => {
 e.preventDefault();
 if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
 // dragover 持续触发 = 持续刷新计时器, 用户真在拖时不会自动隐藏
 if (_dropAutoHideTimer) { clearTimeout(_dropAutoHideTimer); _dropAutoHideTimer = setTimeout(_hideDropOverlay, 4000); }
 }, true);
 window.addEventListener("dragleave", (e) => {
 e.preventDefault();
 _dropCounter = Math.max(0, _dropCounter - 1);
 if (_dropCounter <= 0) {
 _dropCounter = 0;
 _hideDropOverlay();
 }
 }, true);
 // dragend: 用户拖回桌面/拖到外部 → 关
 window.addEventListener("dragend", _hideDropOverlay, true);
 window.addEventListener("drop", async (e) => {
 e.preventDefault();
 _hideDropOverlay();
 const files = Array.from(e.dataTransfer.files || []);
 if (!files.length) return;
 await handleDroppedFiles(files);
 }, true);
}

async function handleDroppedFiles(files) {
 // 过滤 .md / .markdown
 const mdFiles = files.filter(f => /\.(md|markdown|txt)$/i.test(f.name));
 if (!mdFiles.length) {
 addLog("error", "[drop] 没有 .md / .markdown 文件");
 showToast("只支持 .md / .markdown 文件", "warning");
 return;
 }
 // 显示进度
 showImportProgress(0, mdFiles.length, mdFiles[0].name);
 try {
 if (mdFiles.length === 1) {
 const f = mdFiles[0];
 // B-新146: 预检大小, 防止 text() 读 100MB 卡死浏览器
 if (f.size > 50 * 1024 * 1024) {
 finishImportProgress(`✕ 文件过大 (${(f.size / 1024 / 1024).toFixed(1)} MB > 50MB)`);
 addLog("error", `[drop] 文件过大: ${f.name} (${(f.size / 1024 / 1024).toFixed(1)} MB)`);
 return;
 }
 const text = await f.text();
 updateImportProgress(1, 1, f.name, "上传中…");
 const result = await API.post("/import-content", {
 filename: f.name, content: text,
 story_time_unit: "回",
 });
 finishImportProgress(`✓ 导入完成: ${result.chapters} 章, ${(result.words || 0).toLocaleString()} 字`);
 addLog("done", `[drop] 导入完成 ${f.name}: ${result.chapters} 章`);
 await refreshAll();
 // P0-#61: 跳到第 1 章 (导入的新内容, 不是之前可能不存在的旧章节)
 if (result.chapters > 0) {
 // 清掉旧 chapterIdx + lastChapter, 让 renderEditor 重新从 chapters 列表选
 STATE_EDITOR.chapterIdx = null;
 try { localStorage.removeItem("novelai-last-chapter"); } catch (_) {}
 // 等 refreshAll 完, 拿最新的 chapters 列表里第一个
 let firstIdx = 1;
 try {
 const chs = await API.get("/chapters");
 if (chs && chs.length) firstIdx = chs[0].idx;
 } catch (_) {}
 setTimeout(() => {
 gotoEditorAndLoad(firstIdx);
 }, 1200);
 }
 } else {
 // 多文件 = 目录模式
 const payload = [];
 for (let i = 0; i < mdFiles.length; i++) {
 const f = mdFiles[i];
 const text = await f.text();
 payload.push({ filename: f.name, content: text });
 updateImportProgress(i + 1, mdFiles.length, f.name, `读取中 (${i+1}/${mdFiles.length})`);
 }
 updateImportProgress(mdFiles.length, mdFiles.length, "", "上传中…");
 const result = await API.post("/import-directory", {
 files: payload, story_time_unit: "回",
 });
 finishImportProgress(`✓ 导入完成: ${result.chapters} 章, ${(result.words || 0).toLocaleString()} 字`);
 addLog("done", `[drop] 批量导入完成: ${result.chapters} 章`);
 await refreshAll();
 // P0-#61: 同上, 跳到第 1 章
 STATE_EDITOR.chapterIdx = null;
 try { localStorage.removeItem("novelai-last-chapter"); } catch (_) {}
 let firstIdx = 1;
 try {
 const chs = await API.get("/chapters");
 if (chs && chs.length) firstIdx = chs[0].idx;
 } catch (_) {}
 setTimeout(() => {
 gotoEditorAndLoad(firstIdx);
 }, 1200);
 }
 } catch (e) {
 finishImportProgress(`✕ 失败: ${e.message || e}`, true);
 addLog("error", `[drop] ${e.message || e}`);
 }
}

function showImportProgress(done, total, filename) {
 $("#import-modal").classList.remove("hidden");
 $(".ip-file").textContent = filename ? `文件：${filename}` : `共 ${total} 个文件`;
 $("#ip-status").textContent = `进度 0 / ${total}`;
 $("#ip-bar-fill").style.width = "0%";
}
function updateImportProgress(done, total, filename, status) {
 $(".ip-file").textContent = filename || `共 ${total} 个文件`;
 $("#ip-status").textContent = `${status || "处理中…"}（${done}/${total}）`;
 $("#ip-bar-fill").style.width = `${(done/total)*100}%`;
}
function finishImportProgress(msg, isError) {
 $("#ip-status").textContent = msg;
 $("#ip-bar-fill").style.width = isError ? "0%" : "100%";
 if (isError) {
 $("#ip-bar-fill").style.background = "var(--danger)";
 } else {
 $("#ip-bar-fill").style.background = "var(--success, var(--success))";
 }
 setTimeout(() => $("#import-modal").classList.add("hidden"), 2200);
}


// =================== 空状态 / 错误状态 ===================
/**
 * 通用空状态 HTML
 * opts: { icon, title, desc, cta: {label, onclick}, extra (HTML) }
 */
function emptyStateHTML(opts) {
 const { icon = "", title = "暂无数据", desc = "", cta = null, extra = "", actions = null } = opts;
 let html = `<div class="empty-state">
 <div class="empty-icon">${icon}</div>
 <div class="empty-title">${ESC(title)}</div>
 ${desc ? `<div class="empty-desc">${ESC(desc)}</div>` : ""}
 ${cta ? `<button class="btn primary empty-cta" onclick="${ESC(cta.onclick)}">${ESC(cta.label)}</button>` : ""}
 ${actions ? `<div class="empty-actions" style="display:flex;gap:8px;justify-content:center;margin-top:14px;flex-wrap:wrap">${actions}</div>` : ""}
 ${extra}
 </div>`;
 return html;
}

function errorStateHTML(msg, retryFn) {
 return `<div class="empty-state error-state">
 <div class="empty-icon">!</div>
 <div class="empty-title">出错了</div>
 <div class="empty-desc">${ESC(msg || "未知错误")}</div>
 ${retryFn ? `<button class="btn empty-cta" onclick="${ESC(retryFn)}"> 重试</button>` : ""}
 </div>`;
}


// =================== 隐藏的 file input（点 "导入" 按钮用） ===================
let _fileInput = null;
function setupFileInput() {
 // P1-D: 防重复创建
 if (_fileInput) return;
 _fileInput = document.createElement("input");
 _fileInput.type = "file";
 _fileInput.multiple = true;
 _fileInput.accept = ".md,.markdown,.txt";
 _fileInput.style.display = "none";
 _fileInput.onchange = async (e) => {
 const files = Array.from(e.target.files || []);
 try {
 if (files.length) await handleDroppedFiles(files);
 } catch (err) {
 addLog("error", `[import] 文件选择器异常: ${err.message || err}`);
 } finally {
 // 无论如何清空 value, 让用户能再选同一文件
 e.target.value = "";
 }
 };
 document.body.appendChild(_fileInput);
 // 顶部 " 导入" 按钮 (B-新156: 顶栏已移除, 但兼容旧引用)
 const importBtn = $("#btn-import");
 if (importBtn) {
 importBtn.onclick = () => _fileInput.click();
 }
}


// =================== 首次启动 5 秒教学浮层 ===================
function showOnboardingToast() {
 try {
 if (localStorage.getItem("novelai-toast-shown")) return; // 只显示 1 次
 } catch (e) {}
 const toast = $("#onboarding-toast");
 if (!toast) return;
 setTimeout(() => {
 toast.classList.remove("hidden");
 const hide = () => {
 toast.classList.add("fadeout");
 setTimeout(() => toast.classList.add("hidden"), 350);
 };
 // 6 秒后自动消失
 setTimeout(hide, 6000);
 // 点击任何位置立即消失
 toast.onclick = hide;
 }, 1500); // 给首屏加载留 1.5 秒
 try { localStorage.setItem("novelai-toast-shown", "1"); } catch (e) {}
}


// =================== 选中文字 → AI 改这段 ===================
let _selAIBtn = null;
let _selCommentBtn = null;
// inline 模式选区缓存：用户用" AI 改这段"/Ctrl+Enter 触发时记录 {start, end, text}，
// sendEditInstruction 消费后清空。chip 命令/无选区触发时为 null → 走整章模式。
let _inlineSelection = null;
function setupSelectionAIButton() {
 const ta = document.getElementById("ed-text");
 if (!ta) return;
 // 创建浮动按钮（一次）
 if (!_selAIBtn) {
 _selAIBtn = document.createElement("button");
 _selAIBtn.className = "sel-ai-btn";
 _selAIBtn.innerHTML = " AI 改这段";
 _selAIBtn.title = "Ctrl+Enter 也可触发";
 _selAIBtn.onclick = (e) => {
 e.preventDefault();
 e.stopPropagation();
 const start = ta.selectionStart, end = ta.selectionEnd;
 const sel = ta.value.substring(start, end);
 if (!sel) return;
 // 记录选区 offset，供 sendEditInstruction 走 inline 模式
 _inlineSelection = {start, end, text: sel};
 fillEditorInputWithSelection(sel);
 hideSelAIButton();
 ta.focus();
 };
 document.body.appendChild(_selAIBtn);
 }
 // 批注按钮（与 AI 改这段并列）
 if (!_selCommentBtn) {
 _selCommentBtn = document.createElement("button");
 _selCommentBtn.className = "sel-comment-btn";
 _selCommentBtn.innerHTML = " 加批注";
 _selCommentBtn.title = "对选中文本加一条编辑批注";
 _selCommentBtn.onclick = (e) => {
 e.preventDefault();
 e.stopPropagation();
 const start = ta.selectionStart, end = ta.selectionEnd;
 const sel = ta.value.substring(start, end);
 if (!sel || !sel.trim()) return;
 openCommentComposer(sel, start, end);
 hideSelAIButton();
 };
 document.body.appendChild(_selCommentBtn);
 }
 // P0-#32: 防监听器叠加. 用 setupUndoStack._bound 同款 flag
 if (ta._selListenersBound) return; // 已经绑过, 直接返回
 ta._selListenersBound = true;
 ta.addEventListener("mouseup", updateSelAIButtonPos);
 ta.addEventListener("keyup", updateSelAIButtonPos);
 ta.addEventListener("select", updateSelAIButtonPos);
 ta.addEventListener("blur", () => setTimeout(hideSelAIButton, 200));
 // textarea 增强键：Ctrl+Enter (AI inline) / Tab (缩进) / Enter (自动缩进) / Ctrl+F (查找)
 ta.addEventListener("keydown", (e) => {
 // Ctrl+Enter：选中文字时触发 AI inline 改
 if (e.ctrlKey && e.key === "Enter") {
 const start = ta.selectionStart, end = ta.selectionEnd;
 const sel = ta.value.substring(start, end);
 if (sel && sel.trim()) {
 e.preventDefault();
 _inlineSelection = {start, end, text: sel};
 fillEditorInputWithSelection(sel);
 hideSelAIButton();
 }
 return;
 }
 // Ctrl+F / Ctrl+H：编辑器内查找/替换
 if (e.ctrlKey && (e.key === "f" || e.key === "F" || e.key === "h" || e.key === "H")) {
 e.preventDefault();
 openFindReplaceBar(e.key === "h" || e.key === "H" ? "replace" : "find");
 return;
 }
 // Tab：插入两个全角空格（中文缩进习惯），不跳焦点
 if (e.key === "Tab" && !e.ctrlKey && !e.metaKey && !e.altKey) {
 e.preventDefault();
 const s = ta.selectionStart, en = ta.selectionEnd;
 pushUndoSnapshot("before-tab", true);
 const indent = "\u3000\u3000"; // 两个全角空格
 ta.value = ta.value.slice(0, s) + indent + ta.value.slice(en);
 ta.setSelectionRange(s + indent.length, s + indent.length);
 updateEditorStats();
 pushUndoOnEdit(ta.value); // 手动 splice 需更新 undo 边界
 return;
 }
 // Enter（无修饰键）：自动继承上一行的全角缩进（中文小说段落首行缩进）
 if (e.key === "Enter" && !e.ctrlKey && !e.metaKey && !e.altKey) {
 // Shift+Enter：保持原生（插入纯 \n，不自动缩进）
 if (e.shiftKey) return;
 const s = ta.selectionStart;
 const v = ta.value;
 // 找当前行起点，提取行首的全角空格/半角空格作为缩进
 const lineStart = v.lastIndexOf("\n", s - 1) + 1;
 const curLine = v.slice(lineStart, s);
 const m = curLine.match(/^[\u3000 \t]*/);
 const indent = m ? m[0] : "";
 if (indent) {
 e.preventDefault();
 pushUndoSnapshot("before-enter-indent", true);
 // 插入换行 + 继承的缩进
 const insert = "\n" + indent;
 const en = ta.selectionEnd;
 ta.value = v.slice(0, s) + insert + v.slice(en);
 const pos = s + insert.length;
 ta.setSelectionRange(pos, pos);
 updateEditorStats();
 pushUndoOnEdit(ta.value); // 手动 splice 需更新 undo 边界
 }
 // 无缩进则走原生 Enter
 }
 });
}
function updateSelAIButtonPos() {
 const ta = document.getElementById("ed-text");
 if (!ta || !_selAIBtn) return;
 const sel = ta.value.substring(ta.selectionStart, ta.selectionEnd);
 // * 选区字数反馈（Bear/VS Code 风格：选中即显示"选中 N 字"）
 const selCountEl = document.getElementById("ed-sel-count");
 if (selCountEl) {
 if (sel && sel.trim()) {
 const c = countCharsAccurate(sel);
 const enPart = c.enWords > 0 ? ` · ${c.enWords} 词` : "";
 selCountEl.textContent = `｜选中 ${c.cjk.toLocaleString()} 字${enPart}`;
 } else {
 selCountEl.textContent = "";
 }
 }
 if (!sel || !sel.trim()) { hideSelAIButton(); return; }
 // 把选区坐标转成窗口坐标
 // 注意：textarea 不像 contentEditable，简单的选择坐标是字符偏移而非像素
 // 我们用一个近似：按钮放在 textarea 右下角
 const rect = ta.getBoundingClientRect();
 // B-新76: 截断到视口内 (textarea 在屏幕底/右时按钮不外溢)
 const W = window.innerWidth, H = window.innerHeight;
 const aiLeft = Math.max(8, Math.min(W - 200, rect.right - 180));
 const aiTop = Math.max(8, Math.min(H - 60, rect.bottom - 50));
 const cmLeft = Math.max(8, Math.min(W - 320, rect.right - 300));
 _selAIBtn.style.left = aiLeft + "px";
 _selAIBtn.style.top = aiTop + "px";
 _selAIBtn.classList.add("visible");
 // 批注按钮放在 AI 按钮左边
 if (_selCommentBtn) {
 _selCommentBtn.style.left = cmLeft + "px";
 _selCommentBtn.style.top = aiTop + "px";
 _selCommentBtn.classList.add("visible");
 }
}
function hideSelAIButton() {
 if (_selAIBtn) _selAIBtn.classList.remove("visible");
 if (_selCommentBtn) _selCommentBtn.classList.remove("visible");
}
function fillEditorInputWithSelection(sel) {
 const instr = $("#ed-input");
 if (!instr) return;
 // 截断过长（>200 字只取前 200）
 const snippet = sel.length > 200 ? sel.slice(0, 200) + "…" : sel;
 // 有 _inlineSelection（来自浮动按钮/Ctrl+Enter）时提示进入 inline 模式
 const inlineHint = _inlineSelection ? "（将只改选中部分，不重写整章）" : "";
 instr.value = `改这段（${sel.length} 字）${inlineHint}："${snippet}"\n请改得更`;
 instr.focus();
 instr.setSelectionRange(instr.value.length, instr.value.length);
 // 滚动到底部 input
 instr.scrollIntoView({ block: "nearest", behavior: "smooth" });
 addLog("info", `[editor] 选区 ${sel.length} 字已填到指令框`);
}


/**
 * 替换 textarea 中的第 N 段为新内容（精确段落级修改）
 * 按 \n\n 切段，定位第 N 段，替换它
 */
function replaceOriginalParagraph(idx, newText) {
 const ta = $("#ed-text");
 if (!ta) return;
 pushUndoSnapshot("before-replace-para-" + (idx + 1), true);
 // 用统一的 splitParagraphs（trim+filter），避免空段落导致索引偏移
 const currentParas = splitParagraphs(ta.value);
 if (idx >= currentParas.length) {
 // 超出范围 → 追加到末尾
 ta.value = ta.value + (ta.value.endsWith("\n\n") ? "" : "\n\n") + newText;
 } else {
 currentParas[idx] = newText;
 ta.value = currentParas.join("\n\n");
 }
 updateEditorStats();
 setEditorStatus(`已替换第 ${idx + 1} 段（按 Ctrl+Z 撤销 / 按保存生效）`);
 recordVersion(`替换第 ${idx + 1} 段`, {source: "replace"});
 addLog("done", `[editor] 替换第 ${idx + 1} 段：${newText.length} 字`);
}


// =================== 版本快照 ===================
// 持久化版本树：history 项来自后端 chapter_version 表（增量 patch 重建）。
// 每项字段：{id, seq, source, label, name, wordCount, acceptCount, rejectCount, t, text}
// text 默认 null（懒加载：点恢复/对比时才 GET 单版正文）。
const STATE_VERSIONS = {
 baseline: null, // 当前章节从 db 加载时的原文（"基线"，纯字符串）
 history: [], // 历史版本（来自后端，最新在前）
 chapterIdx: null, // 当前章节号（落库用）
};

async function initVersionTracking(idx, baselineText) {
 STATE_VERSIONS.baseline = baselineText;
 STATE_VERSIONS.history = [];
 STATE_VERSIONS.chapterIdx = idx;
 await refreshVersionListFromServer();
 updateVersionBadge();
}

/** 从后端重新拉取当前章节的版本列表（save/建版后同步本地用）。失败静默。 */
async function refreshVersionListFromServer() {
 const idx = STATE_VERSIONS.chapterIdx;
 if (!idx) return;
 try {
 const versions = await API.get(`/editor/chapter/${idx}/versions`);
 STATE_VERSIONS.history = (versions || []).map(v => ({
 id: v.id,
 seq: v.seq,
 source: v.source,
 label: v.label,
 name: v.name,
 wordCount: v.word_count,
 acceptCount: v.accept_count || 0,
 rejectCount: v.reject_count || 0,
 t: (v.created_at || 0) * 1000,
 text: null, // 懒加载
 }));
 updateVersionBadge();
 } catch (e) {
 // 静默：版本不可用不阻塞编辑
 }
}

async function recordVersion(label, opts = {}) {
 const ta = document.getElementById("ed-text");
 if (!ta) return;
 const text = ta.value;
 const idx = STATE_VERSIONS.chapterIdx;
 if (!idx) return; // 没加载章节，无法落库
 const source = opts.source || "auto";
 // 去重：与最新版（history[0]）字数 + label 相同则跳过
 // （text 是懒加载可能为 null，用 wordCount 近似；后端还会做精确 text 比对兜底）
 const latest = STATE_VERSIONS.history[0];
 if (latest && latest.wordCount === text.length && latest.label === (opts.name || label)) {
 return;
 }
 try {
 const body = {
 source,
 label: opts.name || label,
 current_text: text,
 accept_count: opts.acceptCount ?? STATE_AI_STATS.acceptedParagraphs,
 reject_count: opts.rejectCount ?? STATE_AI_STATS.rejectedParagraphs,
 };
 if (opts.name) body.name = opts.name;
 const r = await API.post(`/editor/chapter/${idx}/versions`, body);
 if (r && r.version_id && !r.skipped) {
 // 插入 history 头部（最新在前）
 STATE_VERSIONS.history.unshift({
 id: r.version_id,
 seq: (latest?.seq ?? -1) + 1,
 source,
 label: opts.name || label,
 name: opts.name || null,
 wordCount: text.length,
 acceptCount: body.accept_count,
 rejectCount: body.reject_count,
 t: Date.now(),
 text, // 刚建的，正文已知
 });
 updateVersionBadge();
 addLog("info", `[version] ${opts.name || label}（共 ${STATE_VERSIONS.history.length} 版）`);
 }
 } catch (e) {
 // 版本落库失败绝不阻塞写作
 addLog("warn", `[version] 落库失败（不影响编辑）: ${e.message || e}`);
 }
}

/**
 * 手动起名存档（"v1 终稿"、"v2 修对话"）
 */
function showSnapshotNameDialog() {
 const ta = document.getElementById("ed-text");
 if (!ta) return;
 const modal = $("#snapshot-name-modal");
 if (!modal) return;
 $("#sn-input").value = "";
 $("#sn-current-words").textContent = ta.value.length.toLocaleString();
 modal.classList.remove("hidden");
 setTimeout(() => $("#sn-input").focus(), 50);
 $("#sn-cancel").onclick = () => modal.classList.add("hidden");
 $("#sn-close").onclick = () => modal.classList.add("hidden");
 $("#snapshot-name-modal .modal-mask").onclick = () => modal.classList.add("hidden");
 $("#sn-input").onkeydown = (e) => {
 if (e.key === "Enter") $("#sn-ok").click();
 };
 $("#sn-ok").onclick = async () => {
 const name = $("#sn-input").value.trim();
 if (!name) {
 showToast("请输入版本名", "warning");
 return;
 }
 const text = ta.value;
 if (text === STATE_VERSIONS.baseline) {
 showToast("当前内容和原始版一样，不需要存档", "info");
 return;
 }
 const okBtn = $("#sn-ok");
 okBtn.disabled = true;
 okBtn.textContent = "存档中…";
 try {
 const r = await API.post(`/editor/chapter/${STATE_VERSIONS.chapterIdx}/versions`, {
 source: "named",
 name,
 current_text: text,
 });
 if (r.skipped) {
 addLog("info", `[version] 与最新版相同，未新建命名版`);
 showToast("与最新版相同，未新建", "info");
 } else {
 addLog("done", `[version] * 已命名存档："${name}"`);
 showToast(`* 已存档："${name}"`, "success");
 }
 await refreshVersionListFromServer();
 modal.classList.add("hidden");
 renderVersionList();
 } catch (e) {
 toastError("命名存档失败", e);
 } finally {
 okBtn.disabled = false;
 okBtn.textContent = " 存档";
 }
 };
}

async function clearVersionHistory() {
 if (STATE_VERSIONS.history.length === 0) {
 showToast("历史已经是空的", "info");
 return;
 }
 const namedCount = STATE_VERSIONS.history.filter(v => v.source === "named").length;
 const autoCount = STATE_VERSIONS.history.length - namedCount;
 const msg = namedCount > 0
 ? `清空所有 ${autoCount} 个自动版本？\n（保留 ${namedCount} 个命名版本。基线版永远保留。）`
 : `清空所有 ${autoCount} 个自动版本？\n（基线版永远保留。）`;
 if (!(await showConfirm(msg))) return;
 try {
 const r = await API.post(`/editor/chapter/${STATE_VERSIONS.chapterIdx}/versions/clear`, {keep_named: true});
 addLog("info", `[version] 已清空 ${r.deleted || 0} 个自动版本`);
 await refreshVersionListFromServer();
 renderVersionList();
 } catch (e) {
 toastError("清空失败", e);
 }
}

function updateVersionBadge() {
 const btn = $("#btn-version-history");
 const badge = $("#vh-badge");
 if (!btn) return;
 const n = STATE_VERSIONS.history.length;
 const hasNamed = STATE_VERSIONS.history.some(v => v.source === "named");
 if (n === 0) {
 btn.disabled = true;
 btn.innerHTML = " 历史";
 } else {
 btn.disabled = false;
 btn.innerHTML = ` 历史 (${n})${hasNamed ? ' <span class="vh-badge">*</span>' : ''}`;
 }
 if (badge) badge.style.display = hasNamed ? "inline-block" : "none";
}

/** 把编辑器正文替换为指定版本文本（保留 redo 撤销点）。 */
function restoreVersionText(targetText, label) {
 const ta = document.getElementById("ed-text");
 if (!ta || targetText == null) return;
 if (ta.value === targetText) {
 addLog("info", `[version] 当前已是${label}，无需恢复`);
 return;
 }
 // 把恢复前的当前文本推进 redo 栈，Ctrl+Shift+Z 可撤销恢复
 _redoStack.push({
 value: ta.value,
 scrollTop: 0,
 selectionStart: 0,
 selectionEnd: 0,
 label: `恢复到${label}`,
 t: Date.now(),
 });
 _undoSuppressMs = Date.now() + 1000;
 ta.value = targetText;
 ta.scrollTop = 0;
 updateEditorStats();
 setEditorStatus(`↶ 已恢复${label}（Ctrl+Shift+Z 可再回去，需手动保存才入新版）`);
 addLog("info", `[version] 恢复${label}（undo ${_undoStack.length} / redo ${_redoStack.length}）`);
 showToast(`↶ 已恢复${label}`, "info");
 updateRedoButton();
}

async function showVersionHistory() {
 const modal = $("#version-modal");
 if (!modal) return;
 modal.classList.remove("hidden");
 // 打开时先显示加载态，再从后端重拉最新列表（保证 save/建版后看到的是最新）
 $("#version-list").innerHTML = '<div class="version-empty">加载中…</div>';
 await refreshVersionListFromServer();
 renderVersionList();
 $("#version-close").onclick = () => modal.classList.add("hidden");
 $("#version-modal .modal-mask").onclick = () => modal.classList.add("hidden");
 $("#btn-snapshot-named").onclick = () => showSnapshotNameDialog();
 $("#btn-clear-history").onclick = () => clearVersionHistory();
}

function renderVersionList() {
 const list = $("#version-list");
 if (!list) return;
 const items = [];
 // 基线版 = history 里 seq=0 的那一版（若没有，用内存 baseline 兜底显示）
 const baselineEntry = STATE_VERSIONS.history.find(v => v.seq === 0);
 const baselineText = STATE_VERSIONS.baseline || "";
 const baselineTime = baselineEntry ? baselineEntry.t : null;
 items.push(`
 <div class="version-item version-baseline" data-vid="${baselineEntry ? baselineEntry.id : -1}">
 <div class="vi-time">${formatTime(baselineTime)} <span class="vi-tag vi-tag-base"> 基线</span></div>
 <div class="vi-label">原始版（首次入库）</div>
 <div class="vi-meta">${baselineText.length} 字</div>
 <div class="vi-preview">${previewText(baselineText)}</div>
 <div class="vi-actions">
 <button class="btn small vi-restore">↶ 恢复此版</button>
 <button class="btn small vi-diff"> 对比当前</button>
 </div>
 </div>
 `);
 // 当前（正在编辑的）
 const ta = document.getElementById("ed-text");
 const current = ta ? ta.value : "";
 const diffFromBase = baselineText ? `${current.length - baselineText.length >= 0 ? "+" : ""}${current.length - baselineText.length} 字` : "—";
 items.push(`
 <div class="version-item version-current">
 <div class="vi-time">现在</div>
 <div class="vi-label"> 当前正在编辑</div>
 <div class="vi-meta">${current.length} 字 · 相对原始版 ${diffFromBase}</div>
 <div class="vi-preview">${previewText(current)}</div>
 </div>
 `);
 // 历史（STATE_VERSIONS.history 已是最新在前；跳过基线版单独显示过了）
 STATE_VERSIONS.history.forEach((v, idx) => {
 if (v.seq === 0) return; // 基线版已在上面单独显示
 const sourceTag = v.source === "named" ? '<span class="vi-tag vi-tag-named">* 命名</span>'
 : v.source === "save" ? '<span class="vi-tag vi-tag-save"> 保存</span>'
 : v.source === "replace" ? '<span class="vi-tag vi-tag-rep"> 替换</span>'
 : v.source === "insert" ? '<span class="vi-tag vi-tag-ins">+ 插入</span>'
 : v.source === "ai" ? '<span class="vi-tag vi-tag-ai"> AI</span>'
 : '<span class="vi-tag vi-tag-auto"> 自动</span>';
 const arBadge = (v.acceptCount || v.rejectCount)
 ? `<span class="vi-stats">✓${v.acceptCount || 0} ✕${v.rejectCount || 0}</span>`
 : "";
 // text 懒加载：null 时 preview 显示提示，点恢复/对比时才加载
 const preview = v.text != null ? previewText(v.text) : `<span style="color:var(--fg-dim);font-size:11px">（点"恢复/对比"加载正文）</span>`;
 items.push(`
 <div class="version-item" data-idx="${idx}" data-vid="${v.id}">
 <div class="vi-time">${formatTime(v.t)} ${sourceTag} ${arBadge}</div>
 <div class="vi-label">${ESC(v.label || "")}</div>
 <div class="vi-meta">${v.wordCount || 0} 字</div>
 <div class="vi-preview">${preview}</div>
 <div class="vi-actions">
 <button class="btn small vi-restore">↶ 恢复此版</button>
 <button class="btn small vi-diff"> 对比当前</button>
 </div>
 </div>
 `);
 });
 if (STATE_VERSIONS.history.length === 0) {
 items.push(`
 <div class="version-empty">
 还没有任何版本。<br>
 <span style="color:var(--fg-dim);font-size:11px">点上方" 给当前编辑起名存档"手动存一版，或保存修改后会自动留版。</span>
 </div>
 `);
 }
 list.innerHTML = items.join("");
 // 绑定按钮（基线 data-vid=-1 用 baseline；其余按 data-idx 取 history 项）
 $$(".version-item .vi-restore").forEach(btn => {
 btn.onclick = async (e) => {
 const item = e.target.closest(".version-item");
 const vid = parseInt(item.dataset.vid, 10);
 $("#version-modal").classList.add("hidden");
 if (vid === -1) {
 restoreVersionText(STATE_VERSIONS.baseline || "", "原始版");
 } else {
 const text = await loadVersionText(vid);
 const v = STATE_VERSIONS.history.find(x => x.id === vid);
 if (text != null) restoreVersionText(text, v?.name ? `* ${v.name}` : (v?.label || `版本#${vid}`));
 }
 };
 });
 $$(".version-item .vi-diff").forEach(btn => {
 btn.onclick = async (e) => {
 const item = e.target.closest(".version-item");
 const vid = parseInt(item.dataset.vid, 10);
 const cur = document.getElementById("ed-text")?.value || "";
 let target;
 if (vid === -1) {
 target = STATE_VERSIONS.baseline || "";
 } else {
 target = await loadVersionText(vid);
 }
 if (target != null) await showVersionDiff(target, cur);
 };
 });
}

/** 懒加载某版本的正文（已缓存则直接返回）。返回 null 表示加载失败。 */
async function loadVersionText(versionId) {
 const cached = STATE_VERSIONS.history.find(v => v.id === versionId);
 if (cached && cached.text != null) return cached.text;
 try {
 const v = await API.get(`/editor/chapter/${STATE_VERSIONS.chapterIdx}/versions/${versionId}`);
 if (cached) cached.text = v.text; // 缓存
 return v.text;
 } catch (e) {
 addLog("warn", `[version] 加载版本 ${versionId} 正文失败: ${e.message || e}`);
 return null;
 }
}

function formatTime(t) {
 if (!t) return "—";
 const d = new Date(t);
 return d.toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit", second: "2-digit"});
}

function previewText(text) {
 if (!text) return "<i>(空)</i>";
 const trimmed = text.replace(/\s+/g, " ").slice(0, 100);
 return ESC(trimmed) + (text.length > 100 ? "…" : "");
}

async function showVersionDiff(target, current) {
 const modal = $("#diff-modal");
 if (!modal) return;
 // 先显示加载态（diff 可能要算一会儿）
 $("#diff-body").innerHTML = `<div class="diff-loading">计算差异中…</div>`;
 modal.classList.remove("hidden");
 // * diff 走 Worker（一次 batchDiff 算 html + del/ins/eq）
 const [dr] = await batchDiff([{idx: 0, oldText: target || "", newText: current || ""}]);
 const html = dr ? dr.html : "";
 const delN = dr ? dr.del : 0;
 const insN = dr ? dr.ins : 0;
 const eqN = dr ? dr.eq : 0;
 $("#diff-body").innerHTML = `
 <div class="diff-summary">
 <span class="diff-del">删 -${delN}</span>
 <span class="diff-ins">增 +${insN}</span>
 <span class="diff-eq">未变 =${eqN}</span>
 </div>
 <div class="diff-content">${html || "<i>(无差异)</i>"}</div>
 `;
 $("#diff-close").onclick = () => modal.classList.add("hidden");
 $("#diff-modal .modal-mask").onclick = () => modal.classList.add("hidden");
}
// =================== 撤销栈 (Ctrl+Z) + 重做栈 (Ctrl+Shift+Z) ===================
let _undoStack = []; // [{value, scrollTop, selectionStart, selectionEnd, label, t}, ...]
let _redoStack = []; // 重做栈
let _undoMax = 50; // 最多 50 步
let _undoLastPushTime = 0;
let _undoSuppressMs = 0; // 撤销后这段时间内不自动 push

function setupUndoStack() {
 _undoStack = [];
 _redoStack = [];
 _undoLastPushTime = Date.now();
 updateUndoButton();
 updateRedoButton();
 // 绑定 Ctrl+Z / Cmd+Z 和 Ctrl+Shift+Z / Ctrl+Y / Ctrl+Shift+C（加批注）
 // (只在 init 调用一次，避免重绑)
 if (setupUndoStack._bound) return;
 setupUndoStack._bound = true;
 document.addEventListener("keydown", (e) => {
 const ta = document.getElementById("ed-text");
 if (!ta) return;
 if (document.activeElement !== ta && document.activeElement !== document.getElementById("ed-input")) return;
 if (!(e.ctrlKey || e.metaKey)) return;
 if (e.key === "z" || e.key === "Z") {
 e.preventDefault();
 if (e.shiftKey) redoLast();
 else undoLast();
 } else if (e.key === "y" || e.key === "Y") {
 e.preventDefault();
 redoLast();
 } else if (e.shiftKey && (e.key === "c" || e.key === "C")) {
 // Ctrl+Shift+C: 加批注
 e.preventDefault();
 const sel = ta.value.substring(ta.selectionStart, ta.selectionEnd);
 if (sel && sel.trim()) {
 openCommentComposer(sel, ta.selectionStart, ta.selectionEnd);
 } else {
 addLog("info", "[comment] 请先在正文中选一段文本");
 }
 }
 });
}

/** 切章节时重置撤销/重做栈（避免跨章节污染） */
function resetUndoStack() {
 _undoStack = [];
 _redoStack = [];
 _undoLastPushTime = Date.now();
 _undoSuppressMs = 0; // 切章节后清掉 suppress 标志, 新章节第一秒也能 push (A4)
 updateUndoButton();
 updateRedoButton();
}

function pushUndoSnapshot(label = "edit", force = false) {
 const ta = document.getElementById("ed-text");
 if (!ta) return;
 const now = Date.now();
 // force=true 时绕过 300ms debounce 和 undo/redo 后的 1s suppress
 // （手动操作如粘贴/Tab/回车/查找替换的 before-* 快照必须可靠入栈，否则不可撤销）
 if (!force) {
 if (now - _undoLastPushTime < 300 && _undoStack.length > 0) return;
 if (now < _undoSuppressMs) return;
 }
 _undoLastPushTime = now;
 const snap = {
 value: ta.value,
 scrollTop: ta.scrollTop,
 selectionStart: ta.selectionStart,
 selectionEnd: ta.selectionEnd,
 label,
 t: now,
 };
 _undoStack.push(snap);
 if (_undoStack.length > _undoMax) _undoStack.shift();
 // 标准 undo 行为：任何新操作清空 redo 栈
 _redoStack = [];
 updateUndoButton();
 updateRedoButton();
}

function pushUndoOnEdit(newValue) {
 if (_undoStack.length === 0 || _undoStack[_undoStack.length - 1].value !== newValue) {
 // 任何新编辑都作废 redo——即使快照被 debounce/suppress 吞掉,
 // 否则 undo 后 1s 内打字, 旧 redo 会把"未来"套到已分叉的文本上
 if (_redoStack.length) { _redoStack = []; updateRedoButton(); }
 pushUndoSnapshot("edit");
 }
}

function _applySnap(snap) {
 const ta = document.getElementById("ed-text");
 if (!ta || !snap) return;
 ta.value = snap.value;
 ta.scrollTop = snap.scrollTop;
 if (snap.selectionStart != null) {
 try { ta.setSelectionRange(snap.selectionStart, snap.selectionEnd); } catch (e) {}
 }
 ta.focus();
 updateEditorStats();
}

function undoLast() {
 const ta = document.getElementById("ed-text");
 if (!ta) return;
 // 快照在编辑"之后"入栈, 栈顶常等于当前文本——先剥掉冗余顶部快照,
 // 否则第一次 Ctrl+Z 应用的就是当前态, 看似没反应（off-by-one）
 while (_undoStack.length && _undoStack[_undoStack.length - 1].value === ta.value) _undoStack.pop();
 if (_undoStack.length === 0) {
 addLog("info", "[undo] 没有可撤销的操作");
 updateUndoButton();
 return;
 }
 const snap = _undoStack.pop();
 // 当前态 → redo 栈
 _redoStack.push({
 value: ta.value,
 scrollTop: ta.scrollTop,
 selectionStart: ta.selectionStart,
 selectionEnd: ta.selectionEnd,
 label: snap.label,
 t: Date.now(),
 });
 if (_redoStack.length > _undoMax) _redoStack.shift();
 _applySnap(snap);
 setEditorStatus(`↶ 撤销：${snap.label}`);
 addLog("info", `[undo] 撤销：${snap.label}（undo ${_undoStack.length} / redo ${_redoStack.length}）`);
 _undoSuppressMs = Date.now() + 1000;
 updateUndoButton();
 updateRedoButton();
}

function redoLast() {
 const ta = document.getElementById("ed-text");
 if (!ta) return;
 // 对称处理: redo 栈顶若等于当前文本也是冗余（undo 时已把当前态入 redo）
 while (_redoStack.length && _redoStack[_redoStack.length - 1].value === ta.value) _redoStack.pop();
 if (_redoStack.length === 0) {
 addLog("info", "[redo] 没有可重做的操作");
 updateRedoButton();
 return;
 }
 const snap = _redoStack.pop();
 // 当前态 → undo 栈
 _undoStack.push({
 value: ta.value,
 scrollTop: ta.scrollTop,
 selectionStart: ta.selectionStart,
 selectionEnd: ta.selectionEnd,
 label: snap.label,
 t: Date.now(),
 });
 if (_undoStack.length > _undoMax) _undoStack.shift();
 _applySnap(snap);
 setEditorStatus(`↷ 重做：${snap.label}`);
 addLog("info", `[redo] 重做：${snap.label}（undo ${_undoStack.length} / redo ${_redoStack.length}）`);
 _undoSuppressMs = Date.now() + 1000;
 updateUndoButton();
 updateRedoButton();
}

function updateUndoButton() {
 const btn = document.getElementById("ed-btn-undo");
 if (!btn) return;
 const n = _undoStack.length;
 if (n === 0) {
 btn.disabled = true;
 btn.textContent = "↶ 撤销";
 } else {
 btn.disabled = false;
 btn.textContent = `↶ 撤销 (${n})`;
 }
}
function updateRedoButton() {
 const btn = document.getElementById("ed-btn-redo");
 if (!btn) return;
 const n = _redoStack.length;
 if (n === 0) {
 btn.disabled = true;
 btn.textContent = "↷ 重做";
 } else {
 btn.disabled = false;
 btn.textContent = `↷ 重做 (${n})`;
 }
}


// =================== 导出 .docx ===================
async function exportCurrentChapterDocx() {
 const idx = STATE_EDITOR.chapterIdx;
 if (!idx) {
 addLog("warn", "[export] 未打开章节, 取消导出");
 showToast("请先打开一个章节", "warning");
 return;
 }
 addLog("info", `[export] 导出第 ${idx} 章为 .docx ...`);
 try {
 const r = await fetch(`/api/export/chapter/${idx}.docx`);
 if (!r.ok) {
 const err = await r.text();
 addLog("error", `[export] HTTP ${r.status}: ${err.slice(0, 200)}`);
 toastError("导出失败", err);
 return;
 }
 // 从 Content-Disposition 拿文件名
 const disp = r.headers.get("Content-Disposition") || "";
 const m = disp.match(/filename\*=UTF-8''([^;]+)/);
 const filename = m ? decodeURIComponent(m[1]) : `chapter_${idx}.docx`;
 const blob = await r.blob();
 const url = URL.createObjectURL(blob);
 const a = document.createElement("a");
 a.href = url;
 a.download = filename;
 document.body.appendChild(a);
 a.click();
 setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1000);
 addLog("done", `[export] 已下载 ${filename} (${(blob.size/1024).toFixed(1)} KB)`);
 } catch (e) {
 addLog("error", `[export] ${e.message || e}`);
 toastError("导出失败", e);
 }
}

async function exportAllDocx() {
 if (!(await showConfirm("导出全本为 .docx? 可能需要几秒。"))) return;
 addLog("info", "[export] 导出全本为 .docx ...");
 try {
 const r = await fetch(`/api/export/all.docx`);
 if (!r.ok) {
 const err = await r.text();
 addLog("error", `[export] HTTP ${r.status}: ${err.slice(0, 200)}`);
 toastError("导出失败", err);
 return;
 }
 const disp = r.headers.get("Content-Disposition") || "";
 const m = disp.match(/filename\*=UTF-8''([^;]+)/);
 const filename = m ? decodeURIComponent(m[1]) : "novel.docx";
 const blob = await r.blob();
 const url = URL.createObjectURL(blob);
 const a = document.createElement("a");
 a.href = url;
 a.download = filename;
 document.body.appendChild(a);
 a.click();
 setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1000);
 addLog("done", `[export] 已下载 ${filename} (${(blob.size/1024).toFixed(1)} KB)`);
 } catch (e) {
 addLog("error", `[export] ${e.message || e}`);
 toastError("导出失败", e);
 }
}


// =================== 导出 .md 备份 ===================
async function _downloadFile(url, logLabel) {
 const r = await fetch(url);
 if (!r.ok) {
 const err = await r.text();
 addLog("error", `${logLabel} HTTP ${r.status}: ${err.slice(0, 200)}`);
 toastError("导出失败", err);
 return;
 }
 const disp = r.headers.get("Content-Disposition") || "";
 const m = disp.match(/filename\*=UTF-8''([^;]+)/);
 const filename = m ? decodeURIComponent(m[1]) : "export";
 const blob = await r.blob();
 const dlUrl = URL.createObjectURL(blob);
 const a = document.createElement("a");
 a.href = dlUrl;
 a.download = filename;
 document.body.appendChild(a);
 a.click();
 setTimeout(() => { URL.revokeObjectURL(dlUrl); a.remove(); }, 1000);
 addLog("done", `${logLabel} 已下载 ${filename} (${(blob.size/1024).toFixed(1)} KB)`);
}

async function exportCurrentChapterMd() {
 const idx = STATE_EDITOR.chapterIdx;
 if (!idx) {
 addLog("warn", "[export] 未打开章节, 取消导出");
 showToast("请先打开一个章节", "warning");
 return;
 }
 addLog("info", `[export] 导出第 ${idx} 章为 .md ...`);
 try {
 await _downloadFile(`/api/export/chapter/${idx}.md`, `[export]`);
 } catch (e) {
 addLog("error", `[export] ${e.message || e}`);
 toastError("导出失败", e);
 }
}

async function exportAllMd() {
 if (!(await showConfirm("导出全本为 .md (纯文本备份)?\n(如果手稿较大, 可能需要几秒)"))) return;
 addLog("info", "[export] 导出全本为 .md ...");
 try {
 await _downloadFile(`/api/export/all.md`, "[export]");
 } catch (e) {
 addLog("error", `[export] ${e.message || e}`);
 toastError("导出失败", e);
 }
}


// =================== 快捷键面板 ===================
function showShortcutPanel() {
 $("#shortcut-modal").classList.remove("hidden");
 $("#sc-close").onclick = () => $("#shortcut-modal").classList.add("hidden");
 $("#shortcut-modal .modal-mask").onclick = () => $("#shortcut-modal").classList.add("hidden");
}


// =================== AI 配置（首次启动） ===================
let _systemInfo = null;

async function checkAISetupOnBoot() {
 try {
 const r = await API.get("/system/info");
 _systemInfo = r;
 updateAISetupStatus();
 if (!r.ai.ready) {
 showAISetupModal();
 }
 } catch (e) {
 console.error("checkAISetupOnBoot failed", e);
 }
}

function updateAISetupStatus() {
 // 顶栏显示 AI 状态 (B-优3: 圆点 + 文字 pill 形态, 颜色随状态) (B-优19: ai-ok→ready, ai-fail→config)
 const el = $("#ai-status-badge");
 if (!el || !_systemInfo) return;
 if (_systemInfo.ai.ready) {
 el.className = "ai-pill ready";
 el.innerHTML = `<span class="ai-pill-dot"></span><span class="ai-pill-text">${ESC(_systemInfo.ai.model || _systemInfo.ai.provider)}</span>`;
 el.title = `API Key 已配置\nProvider: ${_systemInfo.ai.provider}\nModel: ${_systemInfo.ai.model}\n点此重新配置`;
 el.style.cursor = "pointer";
 el.onclick = () => showAISetupModal();
 } else {
 el.className = "ai-pill config";
 el.innerHTML = `<span class="ai-pill-dot"></span><span class="ai-pill-text">AI 未配置</span>`;
 el.title = "AI 未配置, 点此配置";
 el.style.cursor = "pointer";
 el.onclick = () => showAISetupModal();
 }
}

async function showAISetupModal() {
 const modal = $("#ai-setup-modal");
 if (!modal) return;
 // 加载现有配置
 if (!_systemInfo) {
 try { _systemInfo = await API.get("/system/info"); } catch (e) { _systemInfo = null; }
 }
 // 预填表单
 if (_systemInfo) {
 $("#ai-setup-provider").value = _systemInfo.ai.provider || "openai_compatible";
 $("#ai-setup-base-url").value = _systemInfo.ai.base_url || "https://api.deepseek.com/v1";
 $("#ai-setup-model").value = _systemInfo.ai.model || "deepseek-chat";
 } else {
 $("#ai-setup-provider").value = "openai_compatible";
 $("#ai-setup-base-url").value = "https://api.deepseek.com/v1";
 $("#ai-setup-model").value = "deepseek-chat";
 }
 $("#ai-setup-key").value = "";
 $("#ai-setup-msg").textContent = "";
 $("#ai-setup-skip").style.display = _systemInfo && _systemInfo.ai.ready ? "inline-block" : "none";
 modal.classList.remove("hidden");
 // 绑定
 $("#ai-setup-cancel").onclick = () => modal.classList.add("hidden");
 $("#ai-setup-close").onclick = () => modal.classList.add("hidden");
 $("#ai-setup-skip").onclick = () => modal.classList.add("hidden");
 $("#ai-setup-modal .modal-mask").onclick = () => modal.classList.add("hidden");
 $("#ai-setup-save").onclick = saveAISetup;
 $("#ai-setup-preset-deepseek").onclick = () => applyAIPreset("deepseek");
 $("#ai-setup-preset-openai").onclick = () => applyAIPreset("openai");
 setTimeout(() => $("#ai-setup-key").focus(), 50);
}

function applyAIPreset(kind) {
 if (kind === "deepseek") {
 $("#ai-setup-provider").value = "openai_compatible";
 $("#ai-setup-base-url").value = "https://api.deepseek.com/v1";
 $("#ai-setup-model").value = "deepseek-chat";
 } else if (kind === "openai") {
 $("#ai-setup-provider").value = "openai";
 $("#ai-setup-base-url").value = "https://api.openai.com/v1";
 $("#ai-setup-model").value = "gpt-4o-mini";
 }
}

let _aiSetupSaving = false; // C2: 防 800ms 内可重复保存覆盖配置
async function saveAISetup() {
 const api_key = $("#ai-setup-key").value.trim();
 const provider = $("#ai-setup-provider").value;
 const base_url = $("#ai-setup-base-url").value.trim();
 const model = $("#ai-setup-model").value.trim();
 if (!api_key) {
 $("#ai-setup-msg").innerHTML = '<span style="color:var(--danger)">请填 API key</span>';
 return;
 }
 if (_aiSetupSaving) { addLog("warn", "[ai-setup] 上一次还在保存, 请稍等"); return; }
 _aiSetupSaving = true;
 $("#ai-setup-msg").textContent = "保存中…";
 $("#ai-setup-save").disabled = true;
 try {
 await API.post("/system/setup-ai", { api_key, provider, base_url, model });
 $("#ai-setup-msg").innerHTML = '<span style="color:var(--success)">✓ 已保存!</span>';
 addLog("done", "[ai-setup] API key 已保存");
 _systemInfo = await API.get("/system/info");
 updateAISetupStatus();
 // 关 modal 前清掉 _aiSetupSaving, 防止下次再点开时按钮永远 disabled
 setTimeout(() => {
 $("#ai-setup-modal").classList.add("hidden");
 _aiSetupSaving = false;
 }, 800);
 } catch (e) {
 $("#ai-setup-msg").innerHTML = `<span style="color:var(--danger)">保存失败: ${ESC(e.message || e)}</span>`;
 _aiSetupSaving = false; // 失败立刻释放, 让用户能重试
 } finally {
 $("#ai-setup-save").disabled = false;
 }
}


// =================== 跨设备数据迁移 ===================
function showMigrateModal() {
 const modal = $("#migrate-modal");
 if (!modal) return;
 $("#mg-msg").textContent = "";
 $("#mg-file").value = "";
 modal.classList.remove("hidden");
 $("#mg-close").onclick = () => modal.classList.add("hidden");
 $("#migrate-modal .modal-mask").onclick = () => modal.classList.add("hidden");
 $("#mg-export").onclick = exportNovelpack;
 $("#mg-import").onclick = importNovelpack;
}

async function exportNovelpack() {
 $("#mg-msg").innerHTML = '<span style="color:var(--fg-muted)">打包中…（含数据库 + .env）</span>';
 $("#mg-export").disabled = true;
 try {
 // 直接下载 — fetch + blob
 const r = await fetch("/api/system/export-pack");
 if (!r.ok) {
 const err = await r.text();
 $("#mg-msg").innerHTML = `<span style="color:var(--danger)">导出失败: ${ESC(err)}</span>`;
 return;
 }
 const disp = r.headers.get("Content-Disposition") || "";
 const m = disp.match(/filename\*=UTF-8''([^;]+)/);
 const filename = m ? decodeURIComponent(m[1]) : "novelai-writer.novelpack";
 const blob = await r.blob();
 const url = URL.createObjectURL(blob);
 const a = document.createElement("a");
 a.href = url;
 a.download = filename;
 document.body.appendChild(a);
 a.click();
 setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1000);
 $("#mg-msg").innerHTML = `<span style="color:var(--success)">✓ 已下载 ${filename}</span><br><span style="color:var(--fg-dim);font-size:11px">把它拷到另一台电脑，在那里打开应用 → 顶栏点 → 选这个文件 → 导入</span>`;
 addLog("done", `[migrate] 导出 ${filename}（${(blob.size/1024).toFixed(1)} KB）`);
 } catch (e) {
 addLog("error", `[migrate] 导出失败: ${e.message || e}`);
 $("#mg-msg").innerHTML = `<span style="color:var(--danger)">导出失败: ${ESC(e.message || e)}</span>`;
 } finally {
 $("#mg-export").disabled = false;
 }
}

let _importingPack = false; // C1: 防 3s 内可重复导入 (数据被覆盖)
async function importNovelpack() {
 if (_importingPack) { addLog("warn", "[migrate] 上一次导入还在跑, 请等 3 秒页面刷新"); return; }
 const fileInput = $("#mg-file");
 const file = fileInput.files[0];
 if (!file) {
 $("#mg-msg").innerHTML = '<span style="color:var(--warning)">请先选一个 .novelpack 文件</span>';
 return;
 }
 if (!(await showConfirm(`导入 "${file.name}"?\n当前数据库和 .env 会自动备份到 data/backups/, 再覆盖.\n\n确定继续?`))) {
 return;
 }
 _importingPack = true;
 $("#mg-msg").innerHTML = '<span style="color:var(--fg-muted)">上传中…</span>';
 $("#mg-import").disabled = true;
 try {
 const form = new FormData();
 form.append("file", file);
 const r = await fetch("/api/system/import-pack", { method: "POST", body: form });
 if (!r.ok) {
 const err = await r.text();
 $("#mg-msg").innerHTML = `<span style="color:var(--danger)">导入失败: ${ESC(err)}</span>`;
 $("#mg-import").disabled = false;
 _importingPack = false;
 return;
 }
 const j = await r.json();
 let html = `<span style="color:var(--success)">✓ 导入成功</span><br>`;
 html += `· 数据库: ${j.db_imported ? "✓" : "—"}<br>`;
 html += `· .env: ${j.env_imported ? "✓" : "—"}<br>`;
 if (j.manifest) {
 const m = j.manifest;
 html += `· 项目: ${ESC(m.project_title || "")}<br>`;
 html += `· ${m.chapters_count} 章 / ${m.characters_count} 角色 / ${m.events_count} 事件 / ${m.total_words?.toLocaleString() || 0} 字<br>`;
 }
 if (j.backup && j.backup.length) {
 html += `<span style="color:var(--fg-dim);font-size:11px">备份: ${j.backup.length} 个文件在 data/backups/</span><br>`;
 }
 html += `<br><span style="color:var(--fg-dim);font-size:11px">3 秒后自动刷新页面…</span>`;
 $("#mg-msg").innerHTML = html;
 addLog("done", `[migrate] 导入成功 (${j.manifest?.chapters_count || 0} 章)`);
 setTimeout(async () => {
 try { _systemInfo = await API.get("/system/info"); updateAISetupStatus(); } catch (e) {}
 refreshAll();
 }, 500);
 setTimeout(() => location.reload(), 3000);
 // 安全兜底: 若 10s 后仍未 reload (用户取消了/浏览器阻止), 恢复导入按钮
 setTimeout(() => {
 _importingPack = false;
 $("#mg-import").disabled = false;
 }, 10000);
 } catch (e) {
 $("#mg-msg").innerHTML = `<span style="color:var(--danger)">导入失败: ${ESC(e.message || e)}</span>`;
 $("#mg-import").disabled = false;
 _importingPack = false;
 }
}


// =================== 一键加载示例项目 ===================
let _loadingSample = false; // B-新93: 防用户连点导致多次 POST /system/load-sample (后端已二次拒, 但避免冗余请求)
async function loadSampleProject() {
 if (_loadingSample) return; // 已有在跑
 if (!(await showConfirm('加载示例项目《长安遗事》(3 章 + 4 角色 + 5 事件)?\n\n这会立即填充数据库, 让你不用准备手稿就能看到完整编辑界面.\n\n如果项目已有章节, 会拒绝执行 (先清理数据再试).'))) return;
 _loadingSample = true;
 addLog("info", "[sample] 加载示例项目…");
 try {
 const r = await API.post("/system/load-sample", {});
 // C3: API.post 已 fetch 完, r 就是 JSON. 后端 ok=true 时是 {ok:true, created:{...}, first_chapter:1}
 // 不是 HTTP Response, 不要 r.ok (HTTP 字段)
 if (!r || r.ok !== true) {
 showToast("加载失败: " + ((r && (r.error || r.message)) || "未知错误"), "error", 4000);
 addLog("error", `[sample] ${(r && r.error) || "后端返回非 ok"}`);
 return;
 }
 addLog("done", `[sample] 示例项目已加载 (${r.created.chapters} 章 / ${r.created.characters} 角色 / ${r.created.events} 事件)`);
 try { localStorage.setItem("novelai:onboarding-done", "1"); } catch (e) {}
 await refreshAll();
 setTimeout(() => {
 gotoEditorAndLoad(r.first_chapter || 1);
 }, 500);
 } catch (e) {
 toastError("加载失败", e);
 addLog("error", `[sample] ${e.message || e}`);
 } finally {
 _loadingSample = false;
 }
}


// =================== 跨章节用词一致性 ===================
async function showVocabModal() {
 const modal = $("#vocab-modal");
 if (!modal) return;
 $("#vocab-summary").innerHTML = '<p style="color:var(--fg-muted);font-size:12px">扫描中…</p>';
 $("#vocab-honorifics").innerHTML = "";
 $("#vocab-roles").innerHTML = "";
 modal.classList.remove("hidden");
 $("#vocab-close").onclick = () => modal.classList.add("hidden");
 $("#vocab-modal .modal-mask").onclick = () => modal.classList.add("hidden");
 try {
 const r = await API.get("/vocab-consistency");
 renderVocabData(r);
 } catch (e) {
 $("#vocab-summary").innerHTML = `<p style="color:var(--danger)">扫描失败: ${ESC(e.message || e)}</p>`;
 }
}

function renderVocabData(r) {
 // 顶部汇总 (P0-#39: aliases 防御 undefined)
 const honCount = (r.honorifics || []).length;
 $("#vocab-summary").innerHTML = `
 <div class="vocab-stat"> 共 <span class="vocab-stat-num">${r.chapters_total}</span> 回</div>
 <div class="vocab-stat"> <span class="vocab-stat-num">${r.characters_total}</span> 个角色</div>
 <div class="vocab-stat">! <span class="vocab-stat-num ${honCount > 0 ? 'warn' : ''}">${honCount}</span> 个敬称异常</div>
 <div class="vocab-stat"> 已用 <span class="vocab-stat-num">${r.role_usage.filter(u => u.total_count > 0).length}/${r.characters_total}</span> 角色</div>
 `;
 // 敬称聚合
 if (r.honorifics.length) {
 $("#vocab-honorifics").innerHTML = `
 <div class="vocab-section">
 <div class="vocab-section-h">! 同人不同敬称（${r.honorifics.length}）</div>
 ${r.honorifics.map(h => `
 <div class="vocab-hon-block">
 <div class="vocab-hon-surname">"${ESC(h.surname)}" 在不同章节被叫了 ${h.variant_count} 种：</div>
 <div class="vocab-hon-variants">
 ${h.variants.map(v => `
 <span class="vocab-hon-variant" title="第 ${v.chapters.join(', ')} 回">
 <span class="vname">${ESC(v.name)}</span>
 <span class="vcount">×${v.count}</span>
 <span class="vchapters">[${v.chapters.join(',')}]</span>
 </span>
 `).join("")}
 </div>
 </div>
 `).join("")}
 </div>
 `;
 } else {
 $("#vocab-honorifics").innerHTML = '<div class="vocab-section" style="color:var(--fg-dim);font-size:12px">✓ 没发现同人不同敬称的情况</div>';
 }
 // 角色出场
 if (r.role_usage.length) {
 $("#vocab-roles").innerHTML = `
 <div class="vocab-section">
 <div class="vocab-section-h"> 角色出场分布（按总次数降序）</div>
 ${r.role_usage.map(u => {
 const knownNames = new Set([u.name, ...(u.aliases || [])]);
 const hitChips = [];
 for (const c of u.chapters) {
 for (const [n, cnt] of Object.entries(c.names)) {
 hitChips.push({ ch: c.chapter_idx, chTitle: c.chapter_title, name: n, cnt });
 }
 }
 if (hitChips.length === 0) {
 return `
 <div class="vocab-role-block" style="opacity:0.55">
 <div class="vocab-role-head">
 <span class="vocab-role-name">${ESC(u.name)}</span>
 <span class="vocab-role-count">0 次</span>
 <span class="vocab-role-chaps">· 未出场</span>
 </div>
 </div>
 `;
 }
 return `
 <div class="vocab-role-block">
 <div class="vocab-role-head">
 <span class="vocab-role-name">${ESC(u.name)}</span>
 <span class="vocab-role-count">${u.total_count} 次</span>
 <span class="vocab-role-chaps">· ${u.chapter_count}/${r.chapters_total} 回</span>
 ${(u.aliases || []).length ? `<span class="vocab-role-chaps" style="color:var(--fg-dim)">· 别名: ${(u.aliases || []).map(ESC).join('、')}</span>` : ""}
 </div>
 <div class="vocab-role-hits">
 ${hitChips.map(h => `
 <span class="vocab-role-hit${knownNames.has(h.name) ? '' : ' unknown'}"
 onclick="gotoEditorAndLoad(${h.ch})"
 title="第 ${h.ch} 回${knownNames.has(h.name) ? '' : ' ! 此叫法不在别名表里，可能漏登记'}">
 ${ESC(h.name)} ×${h.cnt}<span class="vch">[${h.ch}]</span>
 </span>
 `).join("")}
 </div>
 </div>
 `;
 }).join("")}
 </div>
 `;
 } else {
 $("#vocab-roles").innerHTML = '<div class="vocab-section" style="color:var(--fg-dim);font-size:12px">没有角色数据</div>';
 }
}


// =================== 编辑批注 ===================
let _currentComment = { snippet: "", start: 0, end: 0 };

function openCommentComposer(snippet, start, end) {
 const idx = STATE_EDITOR.chapterIdx;
 if (!idx) return;
 _currentComment = { snippet, start, end };
 const modal = $("#comment-modal");
 if (!modal) return;
 $("#cm-snippet").textContent = snippet.length > 200 ? snippet.slice(0, 200) + "…" : snippet;
 $("#cm-body").value = "";
 $("#cm-pos").textContent = `${start}–${end}（${end - start} 字）`;
 modal.classList.remove("hidden");
 setTimeout(() => $("#cm-body").focus(), 50);
 $("#cm-cancel").onclick = () => modal.classList.add("hidden");
 $("#cm-close").onclick = () => modal.classList.add("hidden");
 // B-新129: 删掉重复的 $("#cm-modal-mask") 兜底 (id 不存在), 保留 $("#comment-modal .modal-mask")
 $("#comment-modal .modal-mask").onclick = () => modal.classList.add("hidden");
 $("#cm-body").onkeydown = (e) => {
 if (e.ctrlKey && e.key === "Enter") $("#cm-ok").click();
 };
 $("#cm-ok").onclick = async () => {
 const body = $("#cm-body").value.trim();
 if (!body) { showToast("请填批注内容", "warning"); return; }
 try {
 await API.post(`/editor/chapter/${idx}/comments`, {
 body,
 snippet: _currentComment.snippet,
 anchor_start: _currentComment.start,
 anchor_end: _currentComment.end,
 });
 addLog("done", `[comment] 第 ${idx} 回已加批注（${_currentComment.end - _currentComment.start} 字）`);
 modal.classList.add("hidden");
 renderChapterComments(idx);
 } catch (e) {
 toastError("提交失败", e);
 }
 };
}

async function renderChapterComments(chapterIdx) {
 const el = $("#ed-comments-list");
 const cnt = $("#ed-comment-count");
 if (!el) return;
 if (!chapterIdx) {
 el.innerHTML = '<p class="placeholder" style="font-size:11px;color:var(--fg-dim)">还没打开章节</p>';
 if (cnt) cnt.textContent = "0";
 return;
 }
 try {
 const r = await API.get(`/editor/chapter/${chapterIdx}/comments`);
 const list = r.comments || [];
 if (cnt) cnt.textContent = list.length;
 if (list.length === 0) {
 el.innerHTML = '<p class="placeholder" style="font-size:11px;color:var(--fg-dim)">还没批注。<br>在正文中选一段文本，右上角会浮出" 加批注"按钮。</p>';
 return;
 }
 el.innerHTML = list.map(c => {
 const resolved = c.status === "resolved";
 const snip = c.snippet || ""; // 老数据可能缺 snippet
 const snippet = snip.length > 100 ? snip.slice(0, 100) + "…" : snip;
 return `
 <div class="ec-item ${resolved ? 'resolved' : ''}" data-id="${c.id}" data-anchor-start="${c.anchor_start}" data-anchor-end="${c.anchor_end}">
 <div class="ec-snippet">"${ESC(snippet)}"</div>
 <div class="ec-body">${ESC(c.body)}</div>
 <div class="ec-meta">
 <span>${c.author || "editor"}</span>
 <span>·</span>
 <span>${resolved ? "✓ 已解决" : "○ 待解决"}</span>
 <button class="ec-toggle" data-id="${c.id}" data-status="${c.status}">${resolved ? "↺ 重开" : "✓ 解决"}</button>
 <button class="ec-del del" data-id="${c.id}"></button>
 </div>
 </div>
 `;
 }).join("");
 // 点击跳转
 $$("#ed-comments-list .ec-item").forEach(it => {
 it.onclick = (e) => {
 if (e.target.closest("button")) return; // 按钮不触发
 const start = parseInt(it.dataset.anchorStart, 10);
 const ta = $("#ed-text");
 if (ta && !isNaN(start)) {
 ta.focus();
 try { ta.setSelectionRange(start, start + 10); } catch (e) {}
 }
 };
 });
 // 切换状态
 $$("#ed-comments-list .ec-toggle").forEach(btn => {
 btn.onclick = async (e) => {
 e.stopPropagation();
 const id = e.target.dataset.id;
 const newStatus = e.target.dataset.status === "resolved" ? "open" : "resolved";
 await API.put(`/editor/comments/${id}`, { status: newStatus });
 renderChapterComments(chapterIdx);
 };
 });
 // 删除
 $$("#ed-comments-list .ec-del").forEach(btn => {
 btn.onclick = async (e) => {
 e.stopPropagation();
 const id = e.target.dataset.id;
 if (!(await showConfirm("删除这条批注？"))) return;
 await API.del(`/editor/comments/${id}`);
 renderChapterComments(chapterIdx);
 };
 });
 } catch (e) {
 el.innerHTML = `<p style="color:var(--danger);font-size:11px">加载失败: ${ESC(e.message || e)}</p>`;
 }
}


// =================== 角色声音分析 ===================
// =================== 审稿进度看板 ===================
async function showReviewModal() {
 const modal = $("#review-modal");
 if (!modal) return;
 modal.classList.remove("hidden");
 $("#review-close").onclick = () => modal.classList.add("hidden");
 $("#review-modal .modal-mask").onclick = () => modal.classList.add("hidden");
 $("#review-grid").innerHTML = '<p style="color:var(--fg-muted);font-size:12px">加载中…</p>';
 try {
 const r = await API.get("/editor/review-status");
 renderReviewData(r);
 } catch (e) {
 $("#review-grid").innerHTML = `<p style="color:var(--danger)">加载失败: ${ESC(e.message || e)}</p>`;
 }
}

const REVIEW_STATUS_LABEL = {
 untouched: "○ 未改",
 editor_reviewed: "● 已审",
 final_approved: "✓ 终审",
 rejected: "● 退改",
};
const REVIEW_STATUS_LABEL_SHORT = {
 untouched: "未改",
 editor_reviewed: "已审",
 final_approved: "终审",
 rejected: "退改",
};

function renderReviewData(r) {
 // 顶部统计
 const order = ["untouched", "editor_reviewed", "final_approved", "rejected"];
 $("#review-summary").innerHTML = order.map(s => `
 <div class="review-stat st-${s}">
 <div class="review-stat-num">${r.summary[s] || 0}</div>
 <div class="review-stat-label">${REVIEW_STATUS_LABEL[s]}</div>
 </div>
 `).join("") + `
 <div class="review-stat">
 <div class="review-stat-num">${r.summary.total}</div>
 <div class="review-stat-label"> 总章节</div>
 </div>
 `;
 // 进度条（终审通过占比）
 const finPct = r.summary.total > 0 ? Math.round((r.summary.final_approved || 0) / r.summary.total * 100) : 0;
 $("#review-summary").insertAdjacentHTML("beforeend", `
 <div class="review-stat">
 <div class="review-stat-num">${finPct}%</div>
 <div class="review-stat-label">✓ 终审通过率</div>
 </div>
 `);
 // AI 评审均值
 const reviewed = r.chapters.filter(c => c.ai_review_score != null);
 const avgScore = reviewed.length > 0 ? Math.round(reviewed.reduce((s, c) => s + c.ai_review_score, 0) / reviewed.length) : null;
 $("#review-summary").insertAdjacentHTML("beforeend", `
 <div class="review-stat st-review">
 <div class="review-stat-num">${avgScore != null ? avgScore + "分" : "—"}</div>
 <div class="review-stat-label"> AI 评审均分 · ${reviewed.length}/${r.chapters.length} 章</div>
 </div>
 `);
 // legend
 $("#review-legend").innerHTML = order.map(s => `
 <span class="review-legend-item"><span class="review-legend-dot st-${s}"></span>${REVIEW_STATUS_LABEL[s]}</span>
 `).join("");
 // 网格
 $("#review-grid").innerHTML = r.chapters.map(c => {
 const badges = [];
 if (c.open_comments > 0) badges.push(`<span class="badge open">${c.open_comments} 待解决</span>`);
 if (c.resolved_comments > 0) badges.push(`<span class="badge resolved">${c.resolved_comments} 已解决</span>`);
 if (c.consistency_passed === true) badges.push(`<span class="badge cons-pass">✓ 一致</span>`);
 if (c.consistency_passed === false) badges.push(`<span class="badge cons-fail">✕ 一致</span>`);
 if (c.ai_review_score != null) {
 const sc = Math.round(c.ai_review_score);
 const cls = sc >= 80 ? "rev-good" : (sc >= 60 ? "rev-mid" : "rev-bad");
 badges.push(`<span class="badge rev-score ${cls}" title="AI 评审均分 ${sc} 分${c.ai_review_high ? ` · ${c.ai_review_high} 个高严重度问题` : ""}"> AI ${sc}分</span>`);
 }
 return `
 <div class="review-cell st-${c.status}" onclick="gotoEditorAndLoad(${c.idx})">
 <div class="review-cell-idx">第 ${c.idx} 回 · ${REVIEW_STATUS_LABEL_SHORT[c.status]}</div>
 <div class="review-cell-title" title="${ESC(c.title)}">${ESC(c.title || '')}</div>
 <div class="review-cell-meta">${c.word_count.toLocaleString()} 字 · ${badges.join("")}</div>
 <div class="review-cell-actions">
 <button class="btn small" onclick="event.stopPropagation();openReviewDetail(${c.idx})" title="AI 多维度评审本章"> AI 评审</button>
 </div>
 </div>
 `;
 }).join("");
}

// =================== AI 评审详情（审稿看板） ===================
let REVD_CURRENT_IDX = null;
let REVD_CURRENT_REV = null; // 当前显示的评审数据（供"用 AI 修改选中项"拼指令）
let _pendingAiPrompt = null; // 评审转修改：跳转编辑器后待注入的 AI 指令

async function openReviewDetail(idx) {
 REVD_CURRENT_IDX = idx;
 const modal = $("#review-detail-modal");
 if (!modal) return;
 modal.classList.remove("hidden");
 $("#revd-close").onclick = () => modal.classList.add("hidden");
 $("#review-detail-modal .modal-mask").onclick = () => modal.classList.add("hidden");
 $("#revd-goto").onclick = () => {
 modal.classList.add("hidden");
 gotoEditorAndLoad(idx);
 };
 $("#revd-again").onclick = () => runAiReview(idx);
 $("#revd-fix").onclick = () => {
 // 把勾选的问题/建议拼成一条 AI 修改指令，跳转编辑器预填
 const instr = collectReviewFixInstruction();
 if (!instr) {
 showToast("请先勾选要修改的问题或建议", "warning");
 return;
 }
 modal.classList.add("hidden");
 _pendingAiPrompt = instr; // 编辑器加载完成后注入指令框
 gotoEditorAndLoad(idx);
 };
 $("#revd-title").textContent = `AI 评审 · 第 ${idx} 回`;
 $("#revd-body").innerHTML = '<p style="color:var(--fg-muted);font-size:12px">加载中…</p>';
 try {
 const r = await API.get(`/editor/chapter/${idx}/ai-review`);
 if (r.review) {
 renderReviewDetail(r.review);
 } else {
 REVD_CURRENT_REV = null;
 $("#revd-body").innerHTML = `
 <div class="revd-empty">
 <p style="color:var(--fg-muted);font-size:13px">本章还没有 AI 评审记录。</p>
 <p style="color:var(--fg-dim);font-size:12px">点击下方"AI 评审"按钮，让 AI 从文笔 / 逻辑 / 人物 / 节奏 / 伏笔五个维度评审本章。</p>
 </div>`;
 }
 } catch (e) {
 $("#revd-body").innerHTML = `<p style="color:var(--danger)">加载失败: ${ESC(e.message || e)}</p>`;
 }
}

/** 把评审勾选的问题/建议拼成一条 AI 修改指令（问题在前，建议在后） */
function collectReviewFixInstruction() {
 const rev = REVD_CURRENT_REV;
 if (!rev) return "";
 const issues = (rev.issues || []).map((it, i) => ({it, i})).filter(o => {
 const cb = document.querySelector(`.revd-pick[data-kind="issue"][data-i="${o.i}"]`);
 return cb && cb.checked;
 });
 const sugs = (rev.suggestions || []).map((s, i) => ({s, i})).filter(o => {
 const cb = document.querySelector(`.revd-pick[data-kind="suggestion"][data-i="${o.i}"]`);
 return cb && cb.checked;
 });
 if (!issues.length && !sugs.length) return "";
 const lines = [];
 if (issues.length) {
 lines.push("根据 AI 评审发现的问题逐一修改：");
 issues.forEach((o, k) => {
 const it = o.it;
 const tag = it.severity === "high" ? "严重" : (it.severity === "medium" ? "中等" : "轻微");
 lines.push(`${k + 1}. [${tag}] ${it.text}`);
 });
 }
 if (sugs.length) {
 if (issues.length) lines.push("");
 lines.push("优先参考以下修改建议（与问题对应，可灵活调整）：");
 sugs.forEach((o, k) => lines.push(`${k + 1}. ${o.s}`));
 }
 lines.push("");
 lines.push("请逐项处理，直接输出修改后的全文（或按我的确认方式给出修改）。");
 return lines.join("\n");
}

// 编辑器加载完成后，若有待注入的评审指令则填入指令框并聚焦
function _applyPendingAiPrompt() {
 if (!_pendingAiPrompt) return;
 const inp = document.getElementById("ed-input");
 if (!inp) return;
 inp.value = _pendingAiPrompt;
 _pendingAiPrompt = null;
 // 切到 AI tab 并聚焦
 const aiTab = document.getElementById("ed-tab-ai");
 if (aiTab && !aiTab.classList.contains("active")) aiTab.click();
 setTimeout(() => { inp.focus(); inp.select(); }, 60);
 showToast("评审意见已填入指令框，确认后发送（Ctrl+Enter）", "info", 4000);
}

function renderReviewDetail(rev) {
 const score = Math.round(rev.overall_score);
 const scoreCls = score >= 80 ? "rev-good" : (score >= 60 ? "rev-mid" : "rev-bad");
 const dims = (rev.dimensions || []).map(d => {
 const s = Math.round(d.score);
 const pct = Math.max(4, Math.round(d.score * 10));
 const cls = d.score >= 8 ? "rev-good" : (d.score >= 6 ? "rev-mid" : "rev-bad");
 return `
 <div class="revd-dim">
 <div class="revd-dim-head"><span>${ESC(d.name || "")}</span><span class="revd-dim-score ${cls}">${s}分</span></div>
 <div class="revd-dim-bar"><div class="revd-dim-fill ${cls}" style="width:${pct}%"></div></div>
 <div class="revd-dim-comment">${ESC(d.comment || "")}</div>
 </div>`;
 }).join("");
 const strengths = (rev.strengths || []).map(s => `<li>${ESC(s)}</li>`).join("");
 // 问题带勾选框（默认全选，可取消不想改的）
 const issues = (rev.issues || []).map((it, i) => {
 const cls = it.severity === "high" ? "issue-high" : (it.severity === "medium" ? "issue-mid" : "issue-low");
 const tag = it.severity === "high" ? "高" : (it.severity === "medium" ? "中" : "低");
 return `<li class="revd-issue ${cls}"><label class="revd-pick-row"><input type="checkbox" class="revd-pick" data-kind="issue" data-i="${i}" checked><span class="revd-issue-tag">${tag}</span><span>${ESC(it.text || "")}</span></label></li>`;
 }).join("");
 // 建议带勾选框（默认全选）
 const suggestions = (rev.suggestions || []).map((s, i) => `<li><label class="revd-pick-row"><input type="checkbox" class="revd-pick" data-kind="suggestion" data-i="${i}" checked><span>${ESC(s)}</span></label></li>`).join("");
 const when = rev.created_at ? new Date(rev.created_at * 1000).toLocaleString() : "";
 REVD_CURRENT_REV = rev; // 供"用 AI 修改选中项"拼指令
 $("#revd-body").innerHTML = `
 <div class="revd-score-row">
 <div class="revd-big-score ${scoreCls}">${score}<span>分</span></div>
 <div class="revd-overall">
 <div class="revd-overall-comment">${ESC(rev.overall_comment || "")}</div>
 ${when ? `<div class="revd-time">评审时间 ${ESC(when)}</div>` : ""}
 </div>
 </div>
 ${dims ? `<div class="revd-section"><h4> 维度评分</h4>${dims}</div>` : ""}
 ${strengths ? `<div class="revd-section"><h4> 亮点</h4><ul class="revd-list revd-strengths">${strengths}</ul></div>` : ""}
 ${issues ? `<div class="revd-section"><h4> 问题</h4><ul class="revd-list">${issues}</ul></div>` : ""}
 ${suggestions ? `<div class="revd-section"><h4> 修改建议</h4><ul class="revd-list revd-suggestions">${suggestions}</ul></div>` : ""}
 `;
}

async function runAiReview(idx) {
 const body = $("#revd-body");
 const prev = body.innerHTML;
 body.innerHTML = '<p style="color:var(--info);font-size:12px">AI 正在评审本章（需要 10~30 秒）…</p>';
 try {
 const r = await API.post(`/editor/chapter/${idx}/ai-review`, null, LLM_TIMEOUT_MS);
 renderReviewDetail(r.review);
 if (typeof showToast === "function") showToast(`第 ${idx} 回评审完成: ${Math.round(r.review.overall_score)} 分`);
 } catch (e) {
 body.innerHTML = `<p style="color:var(--danger);font-size:12px">评审失败: ${ESC(e.message || e)}</p>
 <div style="margin-top:10px">
 <button class="btn small" onclick="runAiReview(${idx})"> 重试</button>
 <button class="btn small" onclick="REVD_CURRENT_IDX != null && (openReviewDetail(REVD_CURRENT_IDX))"> 返回</button>
 </div>`;
 }
 // 刷新看板徽章（后台重新拉，失败不阻塞）
 try {
 const r = await API.get("/editor/review-status");
 if ($("#review-grid")) renderReviewData(r);
 } catch (e) { /* 忽略：看板未开时无需刷新 */ }
}

// =================== 命令面板 (Ctrl+K) ===================
const PALETTE_COMMANDS = [
 { label: " 仪表盘", target: "dashboard", keys: "G D" },
 { label: " AI 编辑器", target: "editor", keys: "G E" },
 { label: " 扫描问题", target: "scan", keys: "G S" },
 { label: " AI 修改建议", target: "opt-all", keys: "G O" },
 { label: " 导入 MD", target: "import", keys: "G I" },
 { label: " 修改流水线", target: "pipeline", keys: "G P" },
 { label: " 章节 / 卷", target: "chapters" },
 { label: " 人物档案", target: "characters" },
 { label: " MBTI 标注", target: "mbti" },
 { label: " 事件 / 伏笔", target: "events" },
 { label: " AI 自动抽取", target: "ai-extract" },
 { label: " 叙事结构", target: "structure" },
 { label: " 伏笔深度扫描", target: "threadscan" },
 { label: " 逻辑链扫描", target: "logicscan" },
 { label: " 文风漂移", target: "stylescan" },
 { label: " 性格漂移", target: "driftscan" },
 { label: " 建议总览", target: "optimize" },
 { label: " 全局优化", target: "opt-all" },
 { label: " 性格优化", target: "opt-personality" },
 { label: " 成长线优化", target: "opt-arc" },
 { label: " 人物交会优化", target: "opt-relationship" },
 { label: " 时间线", target: "timeline" },
 { label: " 事件因果链", target: "chain" },
 { label: " 节奏曲线", target: "rhythm" },
 { label: " MBTI 矩阵", target: "matrix" },
 { label: " 成长线", target: "arcs" },
 { label: " 亲密度曲线", target: "relcurve" },
 { label: " 关系网", target: "network" },
 // Actions
 { label: " 切换主题", action: "toggleTheme" },
 { label: " 切换专注模式", action: "toggleFocusMode" },
 { label: " 刷新仪表盘", action: "refreshAll" },
 { label: "? 快捷键帮助", action: "showShortcutPanel" },
];

function showCommandPalette() {
 const modal = $("#palette-modal");
 modal.classList.remove("hidden");
 const input = $("#palette-input");
 input.value = "";
 input.focus();
 renderPaletteResults("");
 // B-新133: 加 mask click 兜底, 跟其他 modal 风格一致
 $("#palette-modal .modal-mask").onclick = () => modal.classList.add("hidden");
 $("#palette-input").oninput = (e) => renderPaletteResults(e.target.value);
 $("#palette-input").onkeydown = (e) => {
 if (e.key === "Enter") {
 const first = document.querySelector(".palette-item.active");
 if (first) first.click();
 } else if (e.key === "Escape") {
 modal.classList.add("hidden");
 } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
 e.preventDefault();
 const items = Array.from($$(".palette-item"));
 const idx = items.indexOf(document.querySelector(".palette-item.active"));
 let next = e.key === "ArrowDown" ? idx + 1 : idx - 1;
 if (next < 0) next = items.length - 1;
 if (next >= items.length) next = 0;
 items.forEach(i => i.classList.remove("active"));
 if (items[next]) items[next].classList.add("active");
 items[next]?.scrollIntoView({ block: "nearest" });
 }
 };
}

function renderPaletteResults(query) {
 const q = (query || "").toLowerCase().trim();
 const results = PALETTE_COMMANDS.filter(c => !q || c.label.toLowerCase().includes(q));
 const cont = $("#palette-results");
 if (!results.length) {
 cont.innerHTML = '<div class="palette-empty">无匹配命令</div>';
 return;
 }
 cont.innerHTML = results.slice(0, 12).map((c, i) => `
 <div class="palette-item ${i === 0 ? 'active' : ''}" data-idx="${i}">
 <span class="pi-label">${ESC(c.label)}</span>
 ${c.keys ? `<span class="pi-keys">${ESC(c.keys)}</span>` : ''}
 </div>
 `).join("");
 cont.querySelectorAll(".palette-item").forEach((el, i) => {
 el.onclick = () => {
 const cmd = results[i];
 $("#palette-modal").classList.add("hidden");
 if (cmd.target) goto(cmd.target);
 else if (cmd.action === "toggleTheme") toggleTheme();
 else if (cmd.action === "toggleFocusMode") toggleFocusMode();
 else if (cmd.action === "refreshAll") refreshAll();
 else if (cmd.action === "showShortcutPanel") showShortcutPanel();
 };
 });
}

// =================== Onboarding ===================
let _onbStep = 1;
function showOnboarding() {
 _onbStep = 1;
 $("#onboarding-modal").classList.remove("hidden");
 updateOnbStep();
}
function updateOnbStep() {
 $$(".onb-step").forEach(el => el.classList.toggle("onb-active", parseInt(el.dataset.step) === _onbStep));
 $("#onb-step-indicator").textContent = `${_onbStep} / 5`;
 $("#onb-prev").style.display = _onbStep === 1 ? "none" : "";
 $("#onb-next").textContent = _onbStep === 5 ? "开始使用 →" : "下一步 →";
}
function hideOnboarding() {
 $("#onboarding-modal").classList.add("hidden");
 // B-新135: 用户走完 5 步到"开始使用 →" 才存 dismissed; ESC 关闭不算
 // 这里只 ESC 关闭不存, onb-next 走到 5 步由 help 按钮不会重看 — backend 已有 onboarding_done = chapters > 0 兜底
}

// =================== 日志 ===================
// P3-B: addLog 合批, 高频日志不每条都 reflow
let _logQueue = [];
let _logFlushScheduled = false;
function addLog(stage, msg) {
 _logQueue.push({stage, msg, t: Date.now()});
 if (_logFlushScheduled) return;
 _logFlushScheduled = true;
 requestAnimationFrame(() => {
 _logFlushScheduled = false;
 const el = $("#log-content");
 if (!el) { _logQueue = []; return; }
 // 第一次有日志时清掉占位
 if (!el._placeholderCleared) {
 const ph = el.querySelector(".log-placeholder");
 if (ph) ph.remove();
 el._placeholderCleared = true;
 }
 // 一次性 append, 减少 reflow
 const frag = document.createDocumentFragment();
 for (const item of _logQueue) {
 const line = document.createElement("div");
 line.className = "line" + (item.stage === "error" ? " error" : (item.stage === "done" ? " done" : ""));
 const t = new Date(item.t).toLocaleTimeString();
 line.innerHTML = `<span class="t">${t}</span><span class="stage">${ESC(item.stage)}</span><span class="msg">${ESC(item.msg)}</span>`;
 frag.appendChild(line);
 }
 _logQueue = [];
 el.appendChild(frag);
 el.scrollTop = el.scrollHeight;
 while (el.children.length > 200) el.removeChild(el.firstChild);
 });
}

let _ws = null;
let _wsReconnectTimer = null; // P0-#7: 重连定时器单例, 防止多次 setTimeout 叠加
let _wsConnecting = false; // P0-#7: 防止重连连一半又被新 onclose 触发
let _wsReconnectAttempt = 0; // B-新92: 指数退避 (1s, 2s, 4s, 8s, ... 上限 30s) 避免网络抖动时重连雪崩
function connectWS() {
 // 已连或正在连, 直接返回
 if (_ws && (_ws.readyState === WebSocket.OPEN || _ws.readyState === WebSocket.CONNECTING)) return;
 if (_wsConnecting) return;
 _wsConnecting = true;
 const proto = location.protocol === "https:" ? "wss:" : "ws:";
 try {
 _ws = new WebSocket(`${proto}//${location.host}/api/ws`);
 } catch (e) {
 _wsConnecting = false;
 _scheduleWSReconnect();
 return;
 }
 _ws.onopen = () => {
 _wsConnecting = false;
 _wsReconnectAttempt = 0; // 成功连接, 重置退避计数
 };
 _ws.onmessage = (e) => {
 try {
 const msg = JSON.parse(e.data);
 if (msg.type === "ping") return; // B-新100: server 周期心跳, 客户端不需处理
 if (msg.type === "log") addLog(msg.data.stage, msg.data.msg);
 else if (msg.type === "status") {
 // B-优21: 进度文字改前缀符号 "⟳"/"✓"/"✕", idle 不覆盖健康度文字 (让 health "严重"/"注意"/"健康" 显示)
 if (msg.data.running) setStatus(`${msg.data.stage}…`, "busy");
 else if (msg.data.stage === "done") { setStatus("完成", ""); setTimeout(refreshAll, 500); }
 else if (msg.data.stage === "error") setStatus("错误", "error");
 // idle: 不改文字, 保留 healthText (严重/注意/健康)
 }
 } catch (e) {
 // P3-I: 之前静默吞错, 至少记一次 warn (高频错的话只看一次)
 if (!window._wsParseErrCount) window._wsParseErrCount = 0;
 if (window._wsParseErrCount < 3) {
 addLog("warn", `[ws] 消息解析失败 (e.data=${String(e.data).slice(0, 100)})`);
 }
 window._wsParseErrCount++;
 }
 };
 _ws.onclose = () => {
 _wsConnecting = false;
 _scheduleWSReconnect();
 };
 _ws.onerror = (e) => {
 addLog("warn", `[ws] 错误事件, 将自动重连`);
 try { _ws.close(); } catch (_) {}
 };
}

function _scheduleWSReconnect() {
 if (_wsReconnectTimer) return; // 已计划重连, 不重排
 // B-新92: 指数退避: 1s, 2s, 4s, 8s, 16s, 30s (封顶)
 const delay = Math.min(1000 * Math.pow(2, _wsReconnectAttempt), 30000);
 _wsReconnectAttempt++;
 _wsReconnectTimer = setTimeout(() => {
 _wsReconnectTimer = null;
 connectWS();
 }, delay);
}

// =================== 刷新 ===================
let _refreshInFlight = false; // P2: refreshAll 并发保护 (ws done 500ms + 15s 轮询可能撞)
async function refreshAll() {
 if (_refreshInFlight) return; // 已有在跑, 跳过
 _refreshInFlight = true;
 try {
 const dash = await API.get("/dashboard");
 STATE.dashboard = dash;
 renderTopbar();
 // 如果当前是仪表盘视图，静默重渲染（不闪加载态/保滚动/不动右栏; 复用已拉的数据避免重复请求）
 if (CURRENT.target === "dashboard") renderDashboard({ silent: true, data: dash });
 // 全局缓存
 try {
 STATE.chapters = await API.get("/chapters");
 STATE.characters = await API.get("/characters");
 } catch (e) {
 addLog("warn", "[refresh] 缓存 chapters/characters 失败: " + (e.message || e));
 }
 } catch (e) {
 addLog("error", "[refresh] dashboard 拉取失败: " + (e.message || e));
 } finally {
 _refreshInFlight = false;
 }
}

async function regenerateCurrentChapter() {
 if (!STATE.dashboard?.kpis?.current_chapter_idx) {
 showToast("当前没有已写章节可重新生成", "warning");
 return;
 }
 const idx = STATE.dashboard.kpis.current_chapter_idx;
 if (!(await showConfirm(`重新生成第 ${idx} 章？将覆盖现有正文。`))) return;
 try {
 // 后端 target_words 是 Query 参数（不是 body 字段），必须放进 URL query string
 const r = await API.post(`/regenerate/${idx}?target_words=${CHAPTER_TARGET_WORDS}`);
 // 后端在已有任务运行时返回 {started:false, error}: 此时不该进入轮询傻等
 if (r && r.started === false) {
 showToast(r.error || "已有任务在运行，请稍后再试", "warning");
 return;
 }
 addLog("info", `[regen] 已请求生成第 ${idx} 章`);
 setEditorStatus(" AI 生成中…", true);
 // 轮询等待完成（timeoutId 先声明, 完成/出错时都要清掉, 否则超时回调会误杀已结束的状态）
 let timeoutId = null;
 const pollId = setInterval(async () => {
 try {
 const r = await API.get("/progress_live");
 if (!r.running) {
 clearInterval(pollId);
 if (timeoutId) clearTimeout(timeoutId);
 // BUG 修复：只在用户仍在该章时才自动重载；否则不强行切章覆盖用户当前编辑
 if (STATE_EDITOR.chapterIdx === idx) {
 await loadEditorChapter(idx);
 addLog("done", `[regen] 第 ${idx} 章已重新生成`);
 } else {
 // 用户已切到别的章——刷新数据但不强行跳转，提示去查看
 refreshAll();
 showToast(`第 ${idx} 章已重新生成（点此查看）`, "success", 6000);
 addLog("done", `[regen] 第 ${idx} 章已重新生成（用户已切走，未自动跳转）`);
 }
 setEditorStatus("● 就绪", false);
 } else if (r.log && r.log.length > 0) {
 const last = r.log[r.log.length - 1];
 setEditorStatus(` ${last.stage || '生成中'}…`, true);
 }
 } catch (e) {
 clearInterval(pollId);
 if (timeoutId) clearTimeout(timeoutId);
 setEditorStatus("● 就绪", false);
 }
 }, 2000);
 timeoutId = setTimeout(() => { clearInterval(pollId); setEditorStatus("● 就绪", false); }, REGEN_TIMEOUT_MS); // 超时强停
 } catch (e) { toastError("启动失败", e); }
}

// =================== 初始化 ===================
window.addEventListener("DOMContentLoaded", () => {
 // 顶部按钮 (B-新156: 简化为 4 个: refresh / migrate / theme / focus / help)
 $("#btn-refresh").onclick = refreshAll;
 // B-新156: 兼容旧 #btn-regenerate / #btn-import 引用, 但已从顶栏移除
 const oldRegen = $("#btn-regenerate");
 if (oldRegen) oldRegen.onclick = regenerateCurrentChapter;
 const oldImport = $("#btn-import");
 if (oldImport) oldImport.onclick = () => goto("import");
 $("#btn-help").onclick = showOnboarding;
 $("#btn-theme").onclick = toggleTheme;
 $("#btn-focus").onclick = toggleFocusMode;

 // B-新158: 顶栏工具菜单 toggle
 const toolsBtn = $("#btn-tools");
 const toolsMenu = $("#tools-menu");
 if (toolsBtn && toolsMenu) {
 toolsBtn.onclick = (e) => {
 e.stopPropagation();
 const willShow = toolsMenu.style.display === "none";
 if (willShow) {
 // body 级浮层：按按钮位置弹出，右缘对齐按钮，顶部对齐按钮下方
 const r = toolsBtn.getBoundingClientRect();
 const viewW = window.innerWidth;
 const right = Math.max(12, Math.round(viewW - r.right)); // 视口内右缘，至少留 12px
 const top = Math.round(r.bottom + 6);
 toolsMenu.style.top = top + "px";
 toolsMenu.style.right = right + "px";
 toolsMenu.style.display = "grid";
 highlightActiveMenuItem();
 } else {
 toolsMenu.style.display = "none";
 }
 };
 // 点击 menu item
 toolsMenu.addEventListener("click", (e) => {
 const item = e.target.closest(".topbar-menu-item");
 if (!item) return;
 const action = item.dataset.action;
 const target = item.dataset.target;
 toolsMenu.style.display = "none"; // 关闭 menu
 if (action === "goto") {
 if (target === "vocab-show") { showVocabModal(); return; }
 if (target === "review-show") { showReviewModal(); return; }
 goto(target);
 } else if (action === "migrate-show") {
 showMigrateModal();
 }
 });
 // 点击页面其它位置关闭 menu
 document.addEventListener("click", (e) => {
 if (!toolsMenu.contains(e.target) && e.target !== toolsBtn) {
 toolsMenu.style.display = "none";
 }
 });
 }
 // 切换视图时自动关闭 menu + 高亮
 function highlightActiveMenuItem() {
 if (!toolsMenu) return;
 const cur = CURRENT.target;
 $$(".topbar-menu-item", toolsMenu).forEach(item => {
 const active = item.dataset.target === cur || (item.dataset.action === "migrate-show" && false);
 item.classList.toggle("topbar-menu-active", active);
 });
 }

 // 任何 modal 打开时自动隐藏浮按钮（"AI 改这段"/"加批注"），避免它们浮在 modal 上
 $$(".modal").forEach(m => {
 new MutationObserver(() => {
 if (!m.classList.contains("hidden")) {
 if (typeof hideSelAIButton === "function") hideSelAIButton();
 }
 }).observe(m, { attributes: true, attributeFilter: ["class"] });
 });
 // 全局快捷键
 // 二段式导航：先按 G 进入"go mode"，再按目标字母跳
 let _gPending = false;
 let _gPendingTimer = null;
 document.addEventListener("keydown", (e) => {
 // 在输入框 / contenteditable 时不响应（避免误触除 ESC 外的快捷键）
 const tag = (e.target.tagName || "").toLowerCase();
 const inEditable = tag === "input" || tag === "textarea" || e.target.isContentEditable;

 // Esc 退专注模式 / 关闭最上层弹窗（输入框内也允许）
 if (e.key === "Escape") {
 e.preventDefault();
 if (document.body.classList.contains("focus-mode")) { toggleFocusMode(); return; }
 // 关闭最上层 visible 的 modal
 const openModals = Array.from($$(".modal"))
 .filter(m => !m.classList.contains("hidden"))
 .reverse(); // 后渲染的在上层
 if (openModals.length > 0) {
 openModals[0].classList.add("hidden");
 hideSelAIButton(); // 顺手隐藏浮按钮
 return;
 }
 // 没 modal 就 hide 浮按钮 + 退出 focus
 hideSelAIButton();
 return;
 }

 // Ctrl/Cmd + S 保存——必须在 inEditable 拦截之前: 写作时光标就在 textarea 里,
 // 放在下面会让最常用的保存快捷键在写作场景中静默失效
 if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) {
 e.preventDefault();
 if (STATE_EDITOR.chapterIdx) { editorSave(); }
 return;
 }

 // 下面这些快捷键在输入框内不响应
 if (inEditable) return;

 // F 键切专注模式
 if (e.key === "f" && !e.ctrlKey && !e.metaKey) {
 e.preventDefault();
 toggleFocusMode();
 return;
 }
 // v1.19.23: Ctrl/Cmd + [ 切左栏, Ctrl/Cmd + ] 切右栏
 if ((e.ctrlKey || e.metaKey) && e.key === "[") {
 e.preventDefault();
 const b = document.getElementById("ed-toggle-left");
 if (b) b.click();
 return;
 }
 if ((e.ctrlKey || e.metaKey) && e.key === "]") {
 e.preventDefault();
 const b = document.getElementById("ed-toggle-right");
 if (b) b.click();
 return;
 }
 // Ctrl/Cmd + . 切主题
 if ((e.ctrlKey || e.metaKey) && e.key === ".") {
 e.preventDefault();
 toggleTheme();
 return;
 }
 // Ctrl/Cmd + K 命令面板 (B-新153: 输入框内不拦截, 让 Ctrl+K 删行可工作)
 if ((e.ctrlKey || e.metaKey) && e.key === "k" && !inEditable) {
 e.preventDefault();
 showCommandPalette();
 return;
 }
 // Ctrl/Cmd + G 快速跳转章节（长篇小说导航）
 if ((e.ctrlKey || e.metaKey) && (e.key === "g" || e.key === "G")) {
 e.preventDefault();
 openChapterGotoDialog();
 return;
 }
 // ? 快捷键面板（需要 shift + /）
 if (e.key === "?" || (e.shiftKey && e.key === "/")) {
 e.preventDefault();
 showShortcutPanel();
 return;
 }

 // 二段式：G + 字母
 if (_gPending) {
 const map = { d: "dashboard", e: "editor", s: "scan", o: "opt-all", i: "import", p: "pipeline" };
 const target = map[e.key.toLowerCase()];
 if (target) {
 e.preventDefault();
 goto(target);
 addLog("info", `[key] G ${e.key.toUpperCase()} → ${target}`);
 }
 _gPending = false;
 if (_gPendingTimer) clearTimeout(_gPendingTimer);
 return;
 }
 if (e.key.toLowerCase() === "g" && !e.ctrlKey && !e.metaKey && !e.altKey) {
 _gPending = true;
 _gPendingTimer = setTimeout(() => { _gPending = false; }, 1200);
 // 可视提示（subtle）
 addLog("info", "[key] 等待下一个键（D/E/S/O/I/P）");
 }
 });
 // log 折叠
 $("#btn-toggle-log").onclick = () => {
 $("#logbar").classList.toggle("collapsed");
 $("#btn-toggle-log").textContent = $("#logbar").classList.contains("collapsed") ? "展开" : "收起";
 };
 // Onboarding 控件
 $("#onb-prev").onclick = () => { if (_onbStep > 1) { _onbStep--; updateOnbStep(); } };
 $("#onb-next").onclick = () => { if (_onbStep < 5) { _onbStep++; updateOnbStep(); } else hideOnboarding(); };
 $("#onb-close").onclick = hideOnboarding;
 // 编辑器按钮
 const edBack = document.getElementById("ed-back");
 if (edBack) edBack.onclick = () => goto("dashboard");
 const edPrev = document.getElementById("ed-prev");
 if (edPrev) edPrev.onclick = () => STATE_EDITOR.prevIdx && loadEditorChapter(STATE_EDITOR.prevIdx);
 const edNext = document.getElementById("ed-next");
 if (edNext) edNext.onclick = () => {
  if (STATE_EDITOR.nextIdx) {
   loadEditorChapter(STATE_EDITOR.nextIdx);
  } else {
   // 最后一章：引导新建并写下一章
   const curIdx = STATE_EDITOR.chapterIdx || 1;
   writeNextChapter(curIdx + 1);
  }
 };
 const edSave = document.getElementById("ed-btn-save");
 if (edSave) edSave.onclick = editorSave;
 const edAnalyze = document.getElementById("ed-btn-analyze");
 if (edAnalyze) edAnalyze.onclick = editorAnalyze;
 // Phase 1: 每日简报按钮
 const edBrief = document.getElementById("ed-btn-brief");
 if (edBrief) edBrief.onclick = () => editorDailyBrief(false);
 const briefRefresh = document.getElementById("brief-refresh");
 if (briefRefresh) briefRefresh.onclick = () => editorDailyBrief(true);
 const briefClose = document.getElementById("brief-close");
 if (briefClose) briefClose.onclick = hideBriefModal;
 const briefModal = document.getElementById("brief-modal");
 if (briefModal) {
 const mask = briefModal.querySelector(".modal-mask");
 if (mask) mask.onclick = hideBriefModal;
 }
 const edClear = document.getElementById("ed-btn-clear");
 if (edClear) edClear.onclick = () => {
 $("#ed-ai-stream").innerHTML = "";
 $("#ed-ai-actions").style.display = "none";
 };
 // 查找/替换按钮（工具栏入口，Ctrl+F 也可触发）
 const edFind = document.getElementById("ed-btn-find");
 if (edFind) edFind.onclick = () => openFindReplaceBar("find");
 // 快速命名存档按钮（工具栏入口，避免每次都要先开版本历史）
 const edSnap = document.getElementById("ed-btn-snapshot");
 if (edSnap) edSnap.onclick = () => showSnapshotNameDialog();
 // AI 改 总入口：展开右栏 + 切到 AI tab + 聚焦输入框
 // （右栏已有完整 AI 输入区 + 5 快捷芯片，工具栏不重复，只做"打开+聚焦"）
 const edAiEdit = document.getElementById("ed-btn-ai-edit");
 if (edAiEdit) edAiEdit.onclick = () => {
 // 展开右栏（若处于 compact/收起状态）
 if (document.body.classList.contains("editor-compact") || document.body.classList.contains("right-collapsed")) {
 document.body.classList.remove("editor-compact", "right-collapsed");
 const tr = document.getElementById("ed-toggle-right");
 if (tr) tr.classList.add("active");
 }
 // 切到 AI tab
 const aiTab = document.getElementById("ed-tab-ai");
 if (aiTab) aiTab.click();
 // 聚焦输入框（稍延迟等 tab 切换完成）
 setTimeout(() => {
 const inp = document.getElementById("ed-input");
 if (inp) { inp.focus(); inp.select(); }
 }, 50);
 };
 // B-新161: 导出 dropdown
 const edExport = document.getElementById("ed-btn-export");
 // v1.19.23: 顶条右侧栏切换 + 沉浸模式
 const edToggleRight = document.getElementById("ed-toggle-right");
 if (edToggleRight) {
 edToggleRight.onclick = () => {
 // editor-compact 模式 = 右栏收起; 用户点 ☰ 切右栏
 const compact = document.body.classList.toggle("editor-compact");
 // 同时同步 right-collapsed 标记 (新逻辑, 用于 grid 列控制)
 document.body.classList.toggle("right-collapsed", compact);
 edToggleRight.classList.toggle("active", !compact); // active = 右栏可见
 try { localStorage.setItem("ed.rightCollapsed", compact ? "1" : "0"); } catch (e) {}
 };
 }
 const edToggleFocus = document.getElementById("ed-toggle-focus");
 if (edToggleFocus) edToggleFocus.onclick = toggleFocusMode;
 // v1.19.23: 左侧栏切换 (顶条 按钮)
 const edToggleLeft = document.getElementById("ed-toggle-left");
 if (edToggleLeft) {
 edToggleLeft.onclick = () => {
 const open = document.body.classList.toggle("left-open");
 edToggleLeft.classList.toggle("active", open); // active = 左栏可见
 try { localStorage.setItem("ed.leftOpen", open ? "1" : "0"); } catch (e) {}
 };
 }
 // v1.19.23: 左侧 rail 按钮 ( 章节 / 诊断 / 要点)
 document.querySelectorAll(".ed-rail-btn").forEach((btn) => {
 btn.onclick = () => {
 const target = btn.dataset.rail;
 if (!target) return;
 // 切换 pane
 document.querySelectorAll(".editor-left .ed-section[data-pane]").forEach((p) => {
 p.style.display = p.dataset.pane === target ? "" : "none";
 });
 // 切换 active 样式
 document.querySelectorAll(".ed-rail-btn").forEach((b) => b.classList.remove("active"));
 btn.classList.add("active");
 // 顺便展开左栏
 if (!document.body.classList.contains("left-open")) {
 document.body.classList.add("left-open");
 if (edToggleLeft) edToggleLeft.classList.add("active");
 }
 };
 });
 // v1.19.23: 右侧 tabs
 document.querySelectorAll(".editor-right .ed-tab").forEach((tab) => {
 tab.onclick = () => {
 const target = tab.dataset.tab;
 if (!target) return;
 document.querySelectorAll(".editor-right .ed-tab").forEach((t) => t.classList.remove("active"));
 document.querySelectorAll(".editor-right .ed-right-pane").forEach((p) => {
 p.classList.toggle("active", p.dataset.pane === target);
 });
 tab.classList.add("active");
 };
 });
 // v1.19.23: 字号调节 (A- / A+)
 const FONT_TIERS = [14, 16, 18, 20, 22, 24, 28];
 const edFontSize = document.getElementById("ed-font-size");
 const applyFontTier = (idx) => {
 const tier = FONT_TIERS[Math.max(0, Math.min(FONT_TIERS.length - 1, idx))];
 FONT_TIERS.forEach((_, i) => document.body.classList.remove("font-tier-" + FONT_TIERS[i]));
 document.body.classList.add("font-tier-" + tier);
 if (edFontSize) edFontSize.textContent = String(tier);
 try { localStorage.setItem("ed.fontTier", String(idx)); } catch (e) {}
 };
 const edFontUp = document.getElementById("ed-font-up");
 const edFontDown = document.getElementById("ed-font-down");
 let curTier = 2; // 默认 18
 try { const saved = parseInt(localStorage.getItem("ed.fontTier") || "2", 10); if (!isNaN(saved)) curTier = saved; } catch (e) {}
 applyFontTier(curTier);
 if (edFontUp) edFontUp.onclick = () => { curTier = Math.min(FONT_TIERS.length - 1, curTier + 1); applyFontTier(curTier); };
 if (edFontDown) edFontDown.onclick = () => { curTier = Math.max(0, curTier - 1); applyFontTier(curTier); };
 // v1.19.23: 目标 mini 按钮 (顶条) — 点击弹出目标设定对话框
 const edTargetMini = document.getElementById("ed-target-mini");
 if (edTargetMini) edTargetMini.onclick = () => showTargetDialog();
 // v1.19.23: 左栏内的备用章节翻页按钮 (ed-prev-2 / ed-next-2)
 const edPrev2 = document.getElementById("ed-prev-2");
 const edNext2 = document.getElementById("ed-next-2");
 if (edPrev2) edPrev2.onclick = () => { const b = document.getElementById("ed-prev"); if (b) b.click(); };
 if (edNext2) edNext2.onclick = () => { const b = document.getElementById("ed-next"); if (b) b.click(); };
 const edAnalyzeMini = document.getElementById("ed-btn-analyze-mini");
 if (edAnalyzeMini) edAnalyzeMini.onclick = editorAnalyze;
 const edExportMenu = document.getElementById("ed-export-menu");
 if (edExport && edExportMenu) {
 edExport.onclick = (e) => {
 e.stopPropagation();
 edExportMenu.style.display = edExportMenu.style.display === "none" ? "block" : "none";
 };
 edExportMenu.addEventListener("click", (e) => {
 const item = e.target.closest(".ed-tools-menu-item");
 if (!item) return;
 const action = item.dataset.action;
 edExportMenu.style.display = "none";
 if (action === "export-docx") exportCurrentChapterDocx();
 else if (action === "export-md") exportCurrentChapterMd();
 });
 document.addEventListener("click", (e) => {
 if (!edExportMenu.contains(e.target) && e.target !== edExport) {
 edExportMenu.style.display = "none";
 }
 });
 }
 // 兼容旧 ed-btn-export-md 引用
 const edExportMd = document.getElementById("ed-btn-export-md");
 if (edExportMd) edExportMd.onclick = exportCurrentChapterMd;
 // 「更多」下拉菜单：触发对应隐藏按钮的功能
 const edMoreBtn = document.getElementById("ed-btn-more");
 const edMoreMenu = document.getElementById("ed-more-menu");
 if (edMoreBtn && edMoreMenu) {
   edMoreBtn.onclick = (e) => {
     e.stopPropagation();
     edMoreMenu.style.display = edMoreMenu.style.display === "none" ? "block" : "none";
   };
   edMoreMenu.addEventListener("click", (e) => {
     const item = e.target.closest(".ed-tools-menu-item");
     if (!item) return;
     const action = item.dataset.action;
     edMoreMenu.style.display = "none";
     const click = (id) => { const b = document.getElementById(id); if (b) b.click(); };
     switch (action) {
       case "undo": click("ed-btn-undo"); break;
       case "redo": click("ed-btn-redo"); break;
       case "find": click("ed-btn-find"); break;
       case "clear": click("ed-btn-clear"); break;
       case "history": click("btn-version-history"); break;
       case "snapshot": click("ed-btn-snapshot"); break;
       case "font-down": click("ed-font-down"); break;
       case "font-up": click("ed-font-up"); break;
       case "set-target": click("ed-target-mini"); break;
       case "export-docx": exportCurrentChapterDocx(); break;
       case "export-md": exportCurrentChapterMd(); break;
     }
   });
   document.addEventListener("click", (e) => {
     if (!edMoreMenu.contains(e.target) && e.target !== edMoreBtn) {
       edMoreMenu.style.display = "none";
     }
   });
 }
 // 兼容旧 ed-btn-regenerate 引用 (移到工具栏)
 const edRegenThis = document.getElementById("ed-btn-regenerate-this");
 if (edRegenThis) edRegenThis.onclick = regenerateCurrentChapter;
 const edUndo = document.getElementById("ed-btn-undo");
 if (edUndo) edUndo.onclick = undoLast;
 const edRedo = document.getElementById("ed-btn-redo");
 if (edRedo) edRedo.onclick = redoLast;
 const btnVerHist = document.getElementById("btn-version-history");
 if (btnVerHist) btnVerHist.onclick = showVersionHistory;
 const edInput = document.getElementById("ed-input");
 const edSend = document.getElementById("ed-send");
 if (edSend && edInput) {
 edSend.onclick = () => {
 const v = edInput.value.trim();
 if (v) {
 // 自动切到 AI tab（用户可能不在 AI 面板）
 const aiTab = document.getElementById("ed-tab-ai");
 if (aiTab && !aiTab.classList.contains("active")) aiTab.click();
 sendEditInstruction(v);
 edInput.value = "";
 }
 };
 edInput.onkeydown = (e) => {
 if (e.key === "Enter" && !e.shiftKey) {
 e.preventDefault();
 const v = edInput.value.trim();
 if (v) {
 const aiTab = document.getElementById("ed-tab-ai");
 if (aiTab && !aiTab.classList.contains("active")) aiTab.click();
 sendEditInstruction(v);
 edInput.value = "";
 }
 }
 };
 }

 // 选中文本 → 浮动按钮"AI 改这段"
 setupSelectionAIButton();
 // AI 可观测性统计条：启动即拉取 + 每 30 秒刷新 + 展开/重置按钮接线
 setupAiStatsbarToggle();
 refreshAiStatsbar();
 if (_aiStatsbarTimer) clearInterval(_aiStatsbarTimer);
 _aiStatsbarTimer = setInterval(refreshAiStatsbar, 30000);
 setupUndoStack();
 document.querySelectorAll(".ed-chip").forEach(chip => {
 // plan toggle 按钮单独绑定（不走 sendEditInstruction）
 if (chip.id === "ed-plan-toggle") return;
 // AI 撰写按钮单独绑定（检测空文本走 streamWriteChapter）
 if (chip.id === "ed-chip-write") {
 chip.onclick = () => {
 const aiTab = document.getElementById("ed-tab-ai");
 if (aiTab && !aiTab.classList.contains("active")) aiTab.click();
 const idx = STATE_EDITOR.chapterIdx;
 const text = $("#ed-text")?.value?.trim() || "";
 if (!text) {
 // 空文本：AI 从零撰写
 if (!idx) { showToast("请先加载一个章节"); return; }
 showToast("AI 将根据大纲撰写本章全文…");
 streamWriteChapter(idx);
 } else {
 // 有正文：走润色
 sendEditInstruction("润色本章的对话，让语言更自然有性格");
 }
 };
 return;
 }
 chip.onclick = () => {
 // 自动切到 AI tab
 const aiTab = document.getElementById("ed-tab-ai");
 if (aiTab && !aiTab.classList.contains("active")) aiTab.click();
 sendEditInstruction(chip.dataset.cmd);
 };
 });
 // 计划模式 toggle：切换 _planMode + 视觉态 + placeholder
 const planToggle = document.getElementById("ed-plan-toggle");
 if (planToggle) planToggle.onclick = () => {
 _planMode = !_planMode;
 planToggle.classList.toggle("active", _planMode);
 const inp = document.getElementById("ed-input");
 if (inp) {
 inp.placeholder = _planMode
 ? "描述修改方向，AI 会先列出计划供你批准…"
 : "指挥 AI：把第三段对话改得更口语化…";
 }
 if (_planMode) showToast("计划模式已开启 · AI 会先列出方案，你逐项批准后执行", "info");
 };
 const edText = document.getElementById("ed-text");
 if (edText) {
 // rAF 合并: 长章节每按键都全量统计会卡, 每帧最多算一次（程序化修改仍直接调 updateEditorStats 立即更新）
 let _statsRaf = null;
 edText.addEventListener("input", () => {
 if (_statsRaf) return;
 _statsRaf = requestAnimationFrame(() => {
 _statsRaf = null;
 updateEditorStats();
 pushUndoOnEdit(edText.value);
 });
 });
 // * 粘贴净化：清理 Word/网页带来的不可见垃圾字符
 // 防止 NBSP/ZWSP/ZWJ/零宽BOM/\\r 等污染正文（会破坏段落切分、AI 改稿、字数统计）
 edText.addEventListener("paste", (e) => {
 const cd = e.clipboardData || window.clipboardData;
 if (!cd) return;
 const raw = cd.getData("text/plain");
 if (!raw) return;
 const cleaned = sanitizePastedText(raw);
 if (cleaned === raw) return; // 无需清理，走默认
 e.preventDefault();
 // 手动插入清理后的文本（保留撤销栈）
 const s = edText.selectionStart, en = edText.selectionEnd;
 pushUndoSnapshot("before-paste", true);
 edText.value = edText.value.slice(0, s) + cleaned + edText.value.slice(en);
 const pos = s + cleaned.length;
 edText.setSelectionRange(pos, pos);
 updateEditorStats();
 pushUndoOnEdit(edText.value); // 手动 splice 不触发 input 事件，需手动更新 undo 栈边界
 setEditorStatus(`已粘贴（清理 ${raw.length - cleaned.length} 个垃圾字符）`);
 });
 // 滚动时持久化位置（用 rAF 防抖）
 let _scrollTimer = null;
 edText.addEventListener("scroll", () => {
 if (_scrollTimer) cancelAnimationFrame(_scrollTimer);
 _scrollTimer = requestAnimationFrame(() => {
 if (STATE_EDITOR.chapterIdx) {
 saveLastChapter(STATE_EDITOR.chapterIdx, edText.scrollTop);
 }
 });
 });
 // 自动保存：每 30 秒检查未保存内容
 let _autoSaveTimer = null;
 edText.addEventListener("input", () => {
 if (_autoSaveTimer) clearTimeout(_autoSaveTimer);
 _autoSaveTimer = setTimeout(async () => {
 if (STATE_EDITOR.chapterIdx && edText.value !== STATE_EDITOR.savedText && !_savingInFlight) {
 try {
 await editorSave({ quiet: true });
 addLog("info", "[editor] 已自动保存");
 } catch (e) { /* 静默，下次再试 */ }
 }
 }, 30000);
 });
 }
 const edAcceptAll = document.getElementById("ed-ai-accept-all");
 if (edAcceptAll) edAcceptAll.onclick = async () => {
 if (!STATE_EDITOR.lastAiText) return;
 if (!(await showConfirm(`用 AI 输出整章替换当前正文？\n\n（按段落部分采纳请逐段点 ✓）`))) return;
 pushUndoSnapshot("before-accept-all", true); // 保留撤销点
 $("#ed-text").value = STATE_EDITOR.lastAiText;
 updateEditorStats();
 editorSave();
 STATE_EDITOR.lastAiText = "";
 $("#ed-ai-actions").style.display = "none";
 addLog("done", "[editor] 已整章替换 AI 输出");
 };
 const edRejectAll = document.getElementById("ed-ai-reject-all");
 if (edRejectAll) edRejectAll.onclick = async () => {
 if (!(await showConfirm("清空所有 AI 输出（已插入到正文的段落会保留）？"))) return;
 STATE_EDITOR.lastAiText = "";
 $("#ed-ai-actions").style.display = "none";
 $$(".ed-paragraph-card").forEach(n => n.remove());
 $("#ed-ai-stream").insertAdjacentHTML("beforeend", '<div class="ed-bubble ed-bubble-tool">✕ 已清空</div>');
 };
 // 重试：用上一次指令重新生成（温度随机，结果不同）
 const edRetry = document.getElementById("ed-ai-retry");
 if (edRetry) edRetry.onclick = () => {
 if (_aiStreaming) { showToast("AI 正在运行，请等完成", "warning"); return; }
 if (!_lastAiInstruction) { showToast("没有可重试的指令", "warning"); return; }
 // 恢复 inline 选区（若上次是 inline 模式），重新发送
 _inlineSelection = _lastAiSelection;
 $("#ed-ai-actions").style.display = "none";
 sendEditInstruction(_lastAiInstruction);
 };
 // P0-#71: 流式输出过程中显示"取消"按钮, 点了直接 abort fetch
 const edCancel = document.getElementById("ed-ai-cancel");
 if (edCancel) edCancel.onclick = () => {
 if (window._aiEditAbortController) {
 try { window._aiEditAbortController.abort(); } catch (_) {}
 addLog("warn", "[ai] 用户主动取消");
 }
 };
 // 窗口 resize
 window.addEventListener("resize", () => Object.values(_charts).forEach(c => c && c.resize()));

 // 问题卡片点击 → 跳到对应章节编辑器
 document.addEventListener("click", (e) => {
 const card = e.target.closest(".issue-card-clickable");
 if (!card) return;
 const chIdx = parseInt(card.dataset.chapterIdx, 10);
 if (!isNaN(chIdx) && chIdx > 0) {
 e.preventDefault();
 e.stopPropagation();
 STATE_EDITOR.chapterIdx = chIdx; // v1.19.26: 统一字段 (之前用 idx, 导致 Ctrl+S 误判无 chapter)
 saveLastChapter(chIdx, 0);
 addLog("info", `[nav] 跳到第 ${chIdx} 章`);
 goto("editor");
 }
 });

 // 启动
 loadTheme();
 loadFocusMode();
 loadEditorPanels(); // v1.19.23: 恢复左/右栏开合状态
 syncEditorPanelButtons();
 STATE_TARGETS.loadFromStorage(); // 加载章节目标
 setupDragDrop(); // 拖拽导入
 setupFileInput(); // 顶部 " 导入" 按钮 → file picker
 refreshAll();
 connectWS();
 showOnboardingToast(); // 首次启动 5 秒快捷键教学

 // 跨设备：首次启动检查 AI 是否配置好，没配好弹窗让用户填
 checkAISetupOnBoot();

 // 章节目标字数按钮 (兼容旧元素; 新版走顶条 .ed-target-mini)
 const _tgtSet = $("#ed-target-set");
 if (_tgtSet) _tgtSet.onclick = showTargetDialog;
 const _tgtWrap = $("#ed-target-wrap");
 if (_tgtWrap) _tgtWrap.onclick = showTargetDialog;

 // 恢复上次的视图（用户用习惯：打开 app 接着上次的活干）
 const lastView = loadView();
 if (lastView && lastView.target && ROUTES[lastView.target]) {
 setTimeout(() => {
 if (lastView.target === "dashboard") {
 // dashboard 是默认，无需跳转（refreshAll 已经渲染）
 return;
 }
 goto(lastView.target, lastView.params || {});
 addLog("info", `[ui] 恢复上次视图：${lastView.target}`);
 }, 800);
 }
 // 检查是否需要 onboarding
 setTimeout(() => {
 // 必须等 dashboard 真的拉回来再判断: 慢机器上 refreshAll 还没完成时
 // STATE.dashboard 是 null, 老用户会被误弹新手引导
 if (STATE.dashboard && !STATE.dashboard.onboarding_done) {
 showOnboarding();
 }
 }, 1500);
 setInterval(refreshAll, 15000);
});
