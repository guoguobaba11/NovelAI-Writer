/**
 * diff-worker.js —— 字符级 diff 计算 Worker
 *
 * 把 charDiffHTML / countDiffChars 的 O(n×m) LCS 计算移出主线程，
 * 避免长段落 diff 卡住编辑器输入。算法与 app.js 同步版完全一致，
 * 但取消 800 字截断（Worker 不怕卡），仅对极端规模保留兜底。
 *
 * 通信协议：
 *   入参 { reqId, tasks: [{idx, oldText, newText}, ...] }
 *   出参 { reqId, results: [{idx, html, del, ins, eq}, ...] }
 *   异常时回 { reqId, results: null }，主线程收到 null 会降级同步重算。
 *
 * 主线程通过 batchDiff() 调度（见 app.js），Worker 不可用时降级同步。
 */
"use strict";

// 内存兜底：LCS 的 DP 矩阵是 (m+1)×(n+1) 个 Uint32（4 字节/格）。
// 规模按格子数限制，而不是单边字数 —— 20000×20000 的矩阵要 1.6GB，必崩。
// 2500 万格 ≈ 100MB，Worker 可承受（如 5000×5000）。
const MAX_CELLS = 25_000_000;
// 单边极端上限：超过后连截断对比都不做，直接按"全文替换"处理
const HARD_MAX = 50000;

function ESC(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * 一次 LCS 同时产出 html + del/ins/eq 计数。
 * 旧版 diffHTML + 3× countChars 会把同一对文本的 DP 矩阵算 4 遍，
 * 这里合并为 1 次建表 + 1 次回溯，速度 4 倍、内存峰值相同。
 */
function diffOne(oldText, newText) {
  const a = oldText || "";
  const b = newText || "";
  if (!a && !b) return { html: "", del: 0, ins: 0, eq: 0 };
  if (!a) return { html: `<ins class="cd-ins">${ESC(b)}</ins>`, del: 0, ins: b.length, eq: 0 };
  if (!b) return { html: `<del class="cd-del">${ESC(a)}</del>`, del: a.length, ins: 0, eq: 0 };

  const m = a.length;
  const n = b.length;

  // 规模兜底：矩阵太大时不算 LCS，直接展示新文本（与旧版 >HARD_MAX 行为一致）
  if (m > HARD_MAX || n > HARD_MAX || m * n > MAX_CELLS) {
    const shown = b.slice(0, HARD_MAX);
    return {
      html: ESC(shown) + (b.length > HARD_MAX ? "…" : ""),
      del: m, ins: n, eq: 0,
    };
  }

  const dp = Array.from({ length: m + 1 }, () => new Uint32Array(n + 1));
  for (let i = 1; i <= m; i++) {
    const ai = a[i - 1];
    const row = dp[i], prev = dp[i - 1];
    for (let j = 1; j <= n; j++) {
      row[j] = ai === b[j - 1] ? prev[j - 1] + 1 : Math.max(prev[j], row[j - 1]);
    }
  }
  // 回溯
  const ops = [];
  let i = m, j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && a[i - 1] === b[j - 1]) {
      ops.push(0);  // eq
      i--; j--;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      ops.push(1);  // ins
      j--;
    } else {
      ops.push(2);  // del
      i--;
    }
  }
  ops.reverse();
  // 合并连续相同类型 → html + 计数
  let html = "";
  let del = 0, ins = 0, eq = 0;
  let curType = -1;
  let buf = "";
  const flush = () => {
    if (!buf) return;
    const t = ESC(buf);
    if (curType === 2) { html += `<del class="cd-del">${t}</del>`; del += buf.length; }
    else if (curType === 1) { html += `<ins class="cd-ins">${t}</ins>`; ins += buf.length; }
    else { html += t; eq += buf.length; }
    buf = "";
  };
  for (let k = 0; k < ops.length; k++) {
    const op = ops[k];
    if (op !== curType) {
      flush();
      curType = op;
    }
    // 字符归属: del 只消费 a；ins 只消费 b；eq 两侧都消费（a[i]==b[j]，取 a 即可）
    if (op === 1) { buf += b[_bi++]; }
    else { buf += a[_ai++]; if (op === 0) _bi++; }
  }
  flush();
  return { html, del, ins, eq };
}

// 字符游标（diffOne 重建用；模块级避免每次循环闭包开销）
let _ai = 0, _bi = 0;

function diffOneWrapped(oldText, newText) {
  _ai = 0; _bi = 0;
  return diffOne(oldText, newText);
}

self.onmessage = function(e) {
  const data = e.data || {};
  const reqId = data.reqId;
  try {
    const tasks = data.tasks || [];
    const results = tasks.map(function(t) {
      const r = diffOneWrapped(t.oldText, t.newText);
      return { idx: t.idx, html: r.html, del: r.del, ins: r.ins, eq: r.eq };
    });
    self.postMessage({ reqId: reqId, results: results });
  } catch (err) {
    // 任何异常（含大规模分配失败）都回 null，主线程收到 null 会降级同步重算，
    // 绝不能让请求悬着（主线程没有超时机制）。
    self.postMessage({ reqId: reqId, results: null, error: String(err && err.message || err) });
  }
};
