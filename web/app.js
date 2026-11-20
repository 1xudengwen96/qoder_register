/* ═════════════ QODER OPS · 指挥舱前端逻辑 ═════════════ */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  config: {},
  status: {},
  offset: 0,
  pollTimer: null,
  elapsedTimer: null,
  runStart: 0,
  latHist: [],
  lineCount: 0,
  filter: "all",
};

/* ---------- 基础工具 ---------- */
async function api(path, opts) {
  const r = await fetch(path, opts);
  return r.json();
}

function toast(msg, kind = "ok") {
  const t = $("toast");
  $("toastText").textContent = msg;
  t.className = "show" + (kind === "ok" ? "" : " " + kind);
  clearTimeout(t._tm);
  t._tm = setTimeout(() => (t.className = ""), 3000);
}

/* 自定义确认弹窗（替代原生 confirm） */
function askConfirm(title, text) {
  return new Promise((resolve) => {
    const mask = $("modalMask");
    $("modalTitle").textContent = title;
    $("modalText").textContent = text;
    mask.hidden = false;
    const done = (v) => {
      mask.hidden = true;
      $("modalOk").onclick = $("modalCancel").onclick = mask.onclick = null;
      resolve(v);
    };
    $("modalOk").onclick = () => done(true);
    $("modalCancel").onclick = () => done(false);
    mask.onclick = (e) => { if (e.target === mask) done(false); };
  });
}

/* 数字滚动动画 */
function countUp(el, target, decimals = 0) {
  const from = el._val ?? 0;
  el._val = target;
  if (from === target) { el.textContent = target.toFixed(decimals); return; }
  const t0 = performance.now(), dur = 650;
  cancelAnimationFrame(el._raf);
  const step = (t) => {
    const p = Math.min(1, (t - t0) / dur);
    const e = 1 - Math.pow(1 - p, 3);
    el.textContent = (from + (target - from) * e).toFixed(decimals);
    if (p < 1) el._raf = requestAnimationFrame(step);
  };
  el._raf = requestAnimationFrame(step);
}

/* 指挥台参数（并发/目标）存服务端：刷新不丢，页面/API 启动共用 */
function clampParams() {
  const w = Math.min(8, Math.max(1, parseInt($("inWorkers").value || "1", 10)));
  const t = Math.min(999, Math.max(1, parseInt($("inTarget").value || "10", 10)));
  $("inWorkers").value = w;
  $("inTarget").value = t;
  return { workers: w, target: t };
}
async function saveUiPrefs(explicit) {
  const p = clampParams();
  const r = await api("/api/run/prefs", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(p) });
  if (!r || r.error || !r.ok) {
    if (explicit) toast("参数保存失败 · " + ((r && r.error) || "服务不可用"), "bad");
    return;
  }
  if (explicit) toast(`参数已保存 · 并发 ×${r.workers} · 目标 ${r.target}`);
}
async function loadUiPrefs() {
  const p = await api("/api/run/prefs");
  if (p && !p.error) {
    if (p.workers >= 1 && p.workers <= 8) $("inWorkers").value = p.workers;
    if (p.target >= 1 && p.target <= 999) $("inTarget").value = p.target;
  }
}

/* ---------- 时钟 ---------- */
function tickClock() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  $("clockTime").textContent = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  const wk = "日一二三四五六"[d.getDay()];
  $("clockDate").textContent = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} 周${wk}`;
}

/* ---------- 分段 / 卡片选择器 ---------- */
function segInit(el, onPick) {
  el.querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => {
      el.querySelectorAll("button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      onPick(b);
    });
  });
}
function segSet(el, value) {
  el.querySelectorAll("button").forEach((b) =>
    b.classList.toggle("active", b.dataset.v === value));
}
function segActive(el) {
  const b = el.querySelector("button.active");
  return b ? b.dataset.v : "";
}

/* ---------- 配置 ---------- */
async function loadConfig() {
  state.config = await api("/api/config");
  renderConfig();
}

/* 网络出口开关的 UI 同步（渲染/点击切换共用） */
function applyProxyUI(on) {
  $("tgProxy").classList.toggle("on", on);
  $("tgProxy").setAttribute("aria-checked", on);
  $("tgProxyMain").textContent = on ? "走代理 · ipdeep 池" : "直连 · 本机出口";
  $("tgProxySub").textContent = on
    ? "注册窗口经代理池出口，一条一用"
    : "注册窗口用本机网络出口，不消耗代理 IP";
  $("tagProxy").textContent = on ? "代理开" : "直连";
  $("tagProxy").className = "tag " + (on ? "on" : "off");
  $("lmReg").textContent = on
    ? `ipdeep · ${String(state.config.region ?? "sg").toUpperCase()}` : "本机直连";
}

function renderConfig() {
  const c = state.config;
  segSet($("segProvider"), String(c["mail.provider"] ?? ""));
  segSet($("segRegion"), String(c.region ?? "sg"));
  $("inHost").value = c["imap.host"] ?? "";
  $("inPort").value = c["imap.port"] ?? 993;
  $("inFolder").value = c["imap.folder"] ?? "INBOX";
  $("inUser").value = c["imap.user"] ?? "";
  $("inPass").value = c["imap.password"] ?? "";

  const proxyOn = !!c["proxy.enabled"];
  applyProxyUI(proxyOn);

  const route = c["mail.imap_proxy"] ? "proxy" : "";
  segSet($("segCaptchaRoute"), route);
  $("inImapProxy").value = c["mail.imap_proxy"] ?? "";

  $("tagProvider").textContent = c["mail.provider"] || "自动";
  $("tagProvider").className = "tag " + (c["mail.provider"] ? "on" : "off");

  // 设备身份模式（random/browser，点击即保存）
  const idm = String(c["device.identity_mode"] ?? "random") === "browser" ? "browser" : "random";
  segSet($("segIdentity"), idm);
  $("tagIdentity").textContent = idm === "browser" ? "匹配" : "随机";
  $("tagIdentity").className = "tag " + (idm === "browser" ? "on" : "off");
  $("identityNote").textContent = idm === "browser"
    ? "同 seed 派生 · 注册与领取服务端看来同一台设备"
    : "每轮重置一台新设备 · 与注册环境无关联";

  // IMAP 预设高亮
  $("segImapPreset").querySelectorAll(".preset").forEach((b) => {
    const host = b.dataset.host;
    const on = host ? host === c["imap.host"]
                    : !["imap.qq.com", "imap.gmail.com"].includes(c["imap.host"]);
    b.classList.toggle("active", on);
  });

  // 链路形态图
  $("lmCap").textContent = c["mail.imap_proxy"] ? "本机隧道" : "本机直连";
}

/* ---------- 状态 / KPI 可视化 ---------- */
async function refreshStatus() {
  try {
    state.status = await api("/api/status");
  } catch { return; }
  const s = state.status;

  countUp($("kpiAccounts"), s.accounts_total ?? 0);
  countUp($("kpiRegistered"), s.accounts_registered ?? 0);
  countUp($("kpiMail"), s.mail_pool ?? 0);
  countUp($("kpiIp"), s.ip_file_remaining ?? 0);
  const bal = s.captcha && s.captcha.balance != null ? s.captcha.balance : 0;
  countUp($("kpiBalance"), bal, 2);

  // 注册占比环形
  const total = s.accounts_total || 0, reg = s.accounts_registered || 0;
  const pct = total ? Math.round((reg / total) * 100) : 0;
  $("donutReg").setAttribute("stroke-dasharray", `${pct} 100`);
  $("chipRegShare").textContent = `注册占比 ${pct}%`;
  $("meterReg").style.width = pct + "%";

  // 邮箱池堆叠条
  const pool = s.mail_pool || 0, pend = s.mail_pending || 0, used = s.mail_used || 0;
  const sum = pool + pend + used || 1;
  $("sbPool").style.width = (pool / sum * 100) + "%";
  $("sbPend").style.width = (pend / sum * 100) + "%";
  $("sbUsed").style.width = (used / sum * 100) + "%";

  // 代理库存电池格
  const ip = s.ip_file_remaining || 0;
  const bat = $("battery");
  if (!bat.children.length) for (let i = 0; i < 10; i++) bat.appendChild(document.createElement("i"));
  const filled = Math.min(10, ip);
  [...bat.children].forEach((cell, i) => {
    cell.className = i < filled ? (ip <= 3 ? "full low" : "full") : "";
  });

  // 余额仪表（$5 满刻度）+ 延迟趋势
  $("gaugeBal").setAttribute("stroke-dasharray", `${Math.min(100, bal / 5 * 100)} 100`);
  if (s.captcha && s.captcha.latency != null) {
    state.latHist.push(s.captcha.latency);
    if (state.latHist.length > 40) state.latHist.shift();
    drawSpark();
    $("chipLatency").textContent = `延迟 ${s.captcha.latency}s`;
  }

  // 健康灯
  setPill("lt-bit", s.bitbrowser && s.bitbrowser.ok ? "ok" : "bad",
          s.bitbrowser && s.bitbrowser.ok ? "比特浏览器" : "比特未就绪");
  setPill("lt-captcha", s.captcha && s.captcha.ok ? "ok" : "bad",
          s.captcha && s.captcha.ok ? `打码 $${bal.toFixed(2)}` : "打码不通");
  setPill("lt-mail", pool > 3 ? "ok" : pool > 0 ? "warn" : "bad", `邮箱池 ${pool}`);
  const proxyOn = !!state.config["proxy.enabled"];
  setPill("lt-proxy", proxyOn ? "ok" : "",
          proxyOn ? `代理 · ${String(state.config.region ?? "sg").toUpperCase()}` : "直连模式");
}

function setPill(id, cls, label) {
  const el = $(id);
  el.className = "pill" + (cls ? " " + cls : "");
  el.querySelector(".pill-label").textContent = label;
}

function drawSpark() {
  const h = state.latHist;
  if (h.length < 2) { $("sparkLine").setAttribute("points", ""); return; }
  const max = Math.max(...h, 1), min = Math.min(...h, 0);
  const span = max - min || 1;
  const pts = h.map((v, i) => {
    const x = (i / (h.length - 1)) * 118 + 1;
    const y = 23 - ((v - min) / span) * 20;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  $("sparkLine").setAttribute("points", pts);
}

/* ---------- 保存配置 ---------- */
async function saveConfig() {
  const patch = {
    "region": segActive($("segRegion")),
    "mail.provider": segActive($("segProvider")),
    "mail.imap_proxy": $("inImapProxy").value.trim(),
    "proxy.enabled": $("tgProxy").classList.contains("on"),
    "imap.host": $("inHost").value.trim(),
    "imap.port": parseInt($("inPort").value || "993", 10),
    "imap.user": $("inUser").value.trim(),
    "imap.password": $("inPass").value,
    "imap.folder": $("inFolder").value.trim() || "INBOX",
  };
  const res = await api("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (res.error) { toast("保存失败：" + res.error, "bad"); return; }
  const n = (res.applied || []).length;
  $("saveNote").textContent = res.backup
    ? `已更新 ${n} 项 · 备份 ${res.backup.split(/[\\/]/).pop()}`
    : "没有需要更新的字段";
  toast(n ? `配置已保存（${n} 项）` : "配置无变化", n ? "ok" : "warn");
  await loadConfig();
  await refreshStatus();
}

/* ---------- 运行控制 ---------- */
async function launch() {
  const s = state.status;
  if (s.captcha && !s.captcha.ok) {
    if (!(await askConfirm("打码通道自检失败", "2Captcha 当前不可达（网络 / 余额 / Key）。验证码环节大概率失败，仍要启动吗？"))) return;
  }
  if (s.bitbrowser && !s.bitbrowser.ok) {
    if (!(await askConfirm("比特浏览器未就绪", "本地指纹浏览器 API 没有响应，注册窗口无法创建。仍要启动吗？"))) return;
  }
  if (s.mail_pool === 0) { toast("邮箱池已空，先补充邮箱再启动", "bad"); return; }

  const workers = Math.min(8, Math.max(1, parseInt($("inWorkers").value || "3", 10)));
  const target = Math.min(999, Math.max(1, parseInt($("inTarget").value || "10", 10)));
  $("inWorkers").value = workers;
  $("inTarget").value = target;
  saveUiPrefs(false);   // 启动即固化本次参数
  // 本次任务的网络模式：直连→预创建的池窗口不配 IP；代理→取用时写入
  const proxyOn = $("tgProxy").classList.contains("on");
  const res = await api("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workers, target, proxy_enabled: proxyOn }),
  });
  if (!res.ok) { toast(res.error || "启动失败", "bad"); return; }

  state.config["proxy.enabled"] = proxyOn;
  toast(`机器人已启动 · ${res.mode} · PID ${res.pid}`);
  $("term").textContent = "";
  state.lineCount = 0;
  $("termCount").textContent = "0 行";
  state.offset = 0;
  state.runStart = Date.now();
  $("rmPid").textContent = res.pid;
  $("rmMode").textContent = res.mode;
  $("runProg").hidden = false;
  $("pgTarget").textContent = res.target;
  startPolling();
  startElapsed();
}

async function stopRun() {
  const res = await api("/api/run/stop", { method: "POST" });
  toast(res.ok ? "已停止运行" : "停止失败：" + (res.error || ""), res.ok ? "warn" : "bad");
}

function startElapsed() {
  clearInterval(state.elapsedTimer);
  state.elapsedTimer = setInterval(() => {
    const sec = Math.floor((Date.now() - state.runStart) / 1000);
    const m = Math.floor(sec / 60), s = sec % 60;
    $("rmElapsed").textContent = `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }, 1000);
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(pollTail, 2000);
  pollTail();
}
function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

async function pollTail() {
  let res;
  try { res = await api(`/api/run/tail?offset=${state.offset}`); } catch { return; }
  if (res.data) {
    appendTerm(res.data);
    state.offset = res.offset;
  }
  if (res.progress) renderProgress(res.progress);
  const tag = $("tagRun"), term = $("term");
  if (res.running) {
    tag.className = "run-state run";
    tag.innerHTML = '<i class="rs-dot"></i>运行中';
    $("btnStop").disabled = false;
    $("btnLaunch").disabled = true;
    $("btnLaunch").classList.add("running");
    $("launchLabel").textContent = "运行中…";
    term.classList.add("live");
    $("rmPid").textContent = res.pid ?? "–";
    $("rmMode").textContent = res.mode || "–";
  } else {
    const exited = res.exit_code != null;
    tag.className = "run-state " + (exited ? (res.exit_code === 0 ? "done" : "fail") : "idle");
    tag.innerHTML = '<i class="rs-dot"></i>' + (exited
      ? (res.exit_code === 0 ? "已完成" : `退出码 ${res.exit_code}`)
      : "待命");
    $("btnStop").disabled = true;
    $("btnLaunch").disabled = false;
    $("btnLaunch").classList.remove("running");
    $("launchLabel").textContent = "启动机器人";
    term.classList.remove("live");
    if (exited) {
      $("rmMode").textContent = (res.mode || "–") + " · 已退出";
      clearInterval(state.elapsedTimer);
      stopPolling();
      loadBbPool();  // 收尾关窗后立刻刷新池状态
    }
  }
}

// ── BitBrowser 连接 + 窗口池（固定 N 窗，每用重配指纹+IP） ─────────────
const BP_STATE = { busy: ["占用", "busy"], hold: ["待领", "hold"], running: ["运行", "running"], idle: ["空闲", ""] };
function bpMsg(text, cls) {
  const m = $("bpMsg");
  m.textContent = text || "";
  m.className = "bp-msg" + (cls ? " " + cls : "");
}
async function loadBbPool() {
  let p; try { p = await api("/api/bb-pool"); } catch { return; }
  const grid = $("bpGrid");
  if (!grid || !p) return;
  // 连接徽标 + 端口回填（输入框正在打字时不打扰）
  $("bpLink").className = "bp-link " + (p.online ? "on" : "off");
  $("bpPort").textContent = p.port ? ":" + p.port : "–";
  const inp = $("bpPortIn");
  if (document.activeElement !== inp && p.port) inp.value = p.port;
  // KPI：总窗口 / 运行中 / 池复用
  $("bpTotal").textContent = p.online ? p.env_total : "–";
  $("bpRunN").textContent = p.online ? p.running_total : "–";
  // 运行中窗口清单（池窗绿·非池窗灰）
  const rl = $("bpRunList");
  rl.innerHTML = "";
  if (p.online) {
    const rs = p.running_envs || [];
    if (!rs.length) rl.innerHTML = '<span class="bp-idle">无运行中窗口</span>';
    for (const r of rs) {
      const chip = document.createElement("span");
      chip.className = "bp-runchip" + (r.pool ? "" : " ext");
      chip.title = r.name;
      const dot = document.createElement("i");
      const nm = document.createElement("span"); nm.textContent = r.name;
      chip.append(dot, nm);
      if (r.pool) { const t = document.createElement("small"); t.textContent = "池"; chip.appendChild(t); }
      rl.appendChild(chip);
    }
  }
  // 池区
  if (p.enabled === false) {
    $("bpSum").textContent = "关";
    grid.innerHTML = '<div class="bp-note">池已关闭 · 每号新建窗口（旧逻辑）</div>';
    return;
  }
  if (!p.online) {
    $("bpSum").textContent = "–";
    grid.innerHTML = '<div class="bp-note">比特未连接 · 检查端口或启动客户端</div>';
    return;
  }
  const rows = p.pool || [];
  $("bpSum").textContent = `${rows.length}/${p.size}`;
  const sizeIn = $("bpSizeIn");
  if (sizeIn && document.activeElement !== sizeIn && p.size) sizeIn.value = p.size;
  grid.innerHTML = "";
  if (!rows.length) {
    grid.innerHTML = '<div class="bp-note">暂无池窗口 · 上方「直连预创建」或首次启动自动补齐</div>';
    return;
  }
  for (const w of rows) {
    const key = w.busy ? (w.hold ? "hold" : "busy") : (w.running ? "running" : "idle");
    const [label, cls] = BP_STATE[key];
    const chip = document.createElement("div");
    chip.className = "bp-chip" + (cls ? " " + cls : "");
    chip.title = `${w.name}${w.tag ? " · " + w.tag : ""}`;
    const dot = document.createElement("i"); dot.className = "bp-dot";
    const nm = document.createElement("span"); nm.textContent = w.name.replace(/^qpool_/, "W");
    const st = document.createElement("small"); st.textContent = label;
    chip.append(dot, nm, st);
    grid.appendChild(chip);
  }
}
$("bpTest").addEventListener("click", async () => {
  const port = parseInt($("bpPortIn").value, 10);
  if (!(port >= 1 && port <= 65535)) { bpMsg("端口须 1-65535", "bad"); return; }
  bpMsg("测试中…");
  const r = await api("/api/bb-check", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ port }) });
  if (r.ok) bpMsg(`通 · ${r.env_total} 窗 · ${r.latency}s`, "ok");
  else bpMsg("连不上 · " + (r.error || "检查客户端/端口"), "bad");
});
$("bpApply").addEventListener("click", async () => {
  const port = parseInt($("bpPortIn").value, 10);
  if (!(port >= 1 && port <= 65535)) { bpMsg("端口须 1-65535", "bad"); return; }
  const r = await api("/api/config", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ "bb.port": port }) });
  if (r.error || !(r.applied || []).includes("bb.port")) {
    bpMsg("保存失败 · " + ((r.skipped || [])[0] || r.error || ""), "bad");
    return;
  }
  bpMsg(`已保存 · :${port}`, "ok");
  await loadBbPool();
  refreshStatus();
});
/* 预创建：开工前建好 N 个直连指纹窗口，后续任务按模式取用 */
function prepMsg(text, cls) {
  const m = $("bpPrepMsg");
  m.textContent = text || "";
  m.className = "bp-msg" + (cls ? " " + cls : "");
}
$("bpPrepare").addEventListener("click", async () => {
  const size = parseInt($("bpSizeIn").value, 10);
  if (!(size >= 1 && size <= 500)) { prepMsg("窗口数须 1-500", "bad"); return; }
  const btn = $("bpPrepare");
  btn.disabled = true;
  prepMsg(`正在预创建 ${size} 个直连窗口（含指纹）…`
    + (size > 20 ? ` 大批量约需 ${Math.ceil(size * 1.5 / 60)} 分钟，请勿关闭页面` : ""));
  let r;
  try {
    r = await api("/api/bb-pool/prepare", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ size }) });
  } catch { r = { ok: false, error: "请求失败" }; }
  btn.disabled = false;
  if (!r.ok) { prepMsg("预创建失败 · " + (r.error || ""), "bad"); return; }
  prepMsg(`✓ 就绪 ${r.total} 个直连窗口（新建 ${r.created}）· 指纹已建好`, "ok");
  toast(`窗口池预创建完成 · 共 ${r.total} 个（新建 ${r.created}）`);
  await loadBbPool();
  await loadConfig();
});

/* ---------- 并发进度面板 ---------- */
const RS_STOP_REASON = {
  target: "✓ 目标达成 · 自动停止",
  mail_empty: "邮箱池已空 · 自动停止",
  stopped: "■ 手动停止",
  exhausted: "worker 全部退出 · 未达目标",
};
const WK_PHASE = { registering: "注册中", idle: "待命", exit: "已退出" };

function renderProgress(p) {
  $("runProg").hidden = false;
  countUp($("pgReg"), p.registered || 0);
  $("pgTarget").textContent = p.target || 0;
  $("pgClaimed").textContent = p.claimed || 0;
  $("pgPending").textContent = p.pending || 0;
  $("pgFail").textContent = p.register_failed || 0;
  const pct = p.target ? Math.min(100, ((p.registered || 0) / p.target) * 100) : 0;
  $("pgBar").style.width = pct.toFixed(1) + "%";
  const grid = $("wkGrid");
  grid.textContent = "";
  for (const w of p.worker_states || []) {
    const card = document.createElement("div");
    card.className = "wk-card" + (w.busy ? " busy" : w.phase === "exit" ? " exit" : "");
    const head = document.createElement("div");
    head.className = "wk-head";
    const dot = document.createElement("i"); dot.className = "wk-dot";
    const nm = document.createElement("b"); nm.textContent = `W${w.i + 1}`;
    const ph = document.createElement("span"); ph.className = "wk-phase";
    ph.textContent = WK_PHASE[w.phase] || w.phase;
    head.append(dot, nm, ph);
    const num = document.createElement("div");
    num.className = "wk-num";
    num.innerHTML = `<span class="ok">✓${w.ok || 0}</span> <span class="fail">✗${w.fail || 0}</span>`;
    const last = document.createElement("div");
    last.className = "wk-last"; last.textContent = w.last || "–";
    last.title = w.last || "";
    card.append(head, num, last);
    grid.appendChild(card);
  }
  const reason = $("pgReason");
  if (p.finished && p.stop_reason) {
    reason.hidden = false;
    reason.textContent = RS_STOP_REASON[p.stop_reason] || p.stop_reason;
  } else reason.hidden = true;
}

/* ---------- 日志终端 ---------- */
function appendTerm(text) {
  const term = $("term");
  text.split(/\r?\n/).forEach((line) => {
    if (!line) { term.appendChild(document.createTextNode("\n")); return; }
    const span = document.createElement("span");
    if (/ERROR|FAIL|Traceback|otp_failed|captcha_failed/i.test(line)) span.className = "err";
    else if (/PASS|成功|registered|ok=True/i.test(line)) span.className = "ok";
    else if (/WARN|warning|异常/i.test(line)) span.className = "warn";
    span.textContent = line + "\n";
    term.appendChild(span);
    state.lineCount++;
  });
  while (term.childNodes.length > 4000) {
    term.removeChild(term.firstChild);
    state.lineCount = Math.max(0, state.lineCount - 1);
  }
  $("termCount").textContent = `${state.lineCount} 行`;
  if ($("chkAuto").checked) term.scrollTop = term.scrollHeight;
}

/* ---------- 数据池批量管理 ---------- */
const POOL_META = {
  mail: { title: "邮箱清单", badge: "ptMail",
    fmt: "email----password----clientId----refreshToken 或 CSV：email,password,imap_host[,region]（可混贴，自动归一）",
    ph: "a@hotmail.com----pwd----clientId----refreshToken\nb@hotmail.com,pwd,outlook.office365.com,us" },
  relay: { title: "iCloud 转发地址", badge: "ptRelay",
    fmt: "一行一个隐藏转发地址（含 @）", ph: "hide1@icloud.com\nhide2@icloud.com" },
  proxy: { title: "代理池", badge: "ptProxy",
    fmt: "host:port:user:pass 或 http://user:pass@host:port#region（地区 us/sg）",
    ph: "1.2.3.4:8080:user:pass#sg\nhttp://u:p@5.6.7.8:3128#us" },
  ip: { title: "IP 池（消耗式）", badge: "ptIp",
    fmt: "ip:port:user:password（注册窗口取一条删一条）",
    ph: "1.2.3.4:8080:user:pass\n5.6.7.8:8080:user:pass" },
};
state.pool = { kind: "mail", mode: "append", rows: [], sel: new Set(), q: "" };

async function loadPoolSummary() {
  let s; try { s = await api("/api/pools"); } catch { return; }
  for (const k of Object.keys(POOL_META)) {
    const el = $(POOL_META[k].badge);
    if (el) el.textContent = (s[k] && s[k].count) || 0;
  }
  state.pool.summary = s;
}

function switchPool(kind) {
  state.pool.kind = kind;
  state.pool.sel.clear();
  $("poolTabs").querySelectorAll("button").forEach((b) =>
    b.classList.toggle("active", b.dataset.k === kind));
  const m = POOL_META[kind];
  $("piTitle").textContent = m.title;
  $("piFormat").textContent = "格式：" + m.fmt;
  $("poolText").placeholder = m.ph;
  $("piPath").textContent = (state.pool.summary[kind] || {}).path || "data/…";
  $("piResult").hidden = true;
  $("poolSearch").value = "";
  state.pool.q = "";
  loadPoolList();
}

async function loadPoolList() {
  const k = state.pool.kind;
  let r; try { r = await api(`/api/pools/list?kind=${k}&q=${encodeURIComponent(state.pool.q)}`); }
  catch { return; }
  state.pool.rows = r.rows || [];
  state.pool.sel.clear();
  $("plCount").textContent = `${r.total || 0} 条`;
  renderRows();
  syncSelUI();
}

function renderRows() {
  const box = $("plRows");
  box.textContent = "";
  if (!state.pool.rows.length) {
    box.innerHTML = '<div class="pl-empty">暂无数据</div>';
    return;
  }
  for (const row of state.pool.rows) {
    const el = document.createElement("div");
    el.className = "pl-row";
    el.dataset.key = row.key;
    const chk = document.createElement("label");
    chk.className = "pl-check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.addEventListener("change", () => {
      if (cb.checked) state.pool.sel.add(row.key); else state.pool.sel.delete(row.key);
      el.classList.toggle("sel", cb.checked);
      syncSelUI();
    });
    chk.appendChild(cb);
    const main = document.createElement("div");
    main.className = "pl-main";
    const t = document.createElement("span");
    t.className = "pl-title"; t.textContent = row.title; t.title = row.title;
    const meta = document.createElement("span");
    meta.className = "pl-meta"; meta.textContent = row.meta || "";
    main.append(t, meta);
    const tag = document.createElement("span");
    tag.className = "pl-tag" + (row.ok ? "" : " bad");
    tag.textContent = row.tag;
    const del = document.createElement("button");
    del.className = "pl-del"; del.title = "删除这条"; del.textContent = "×";
    del.addEventListener("click", async () => {
      if (!(await askConfirm("删除条目", `从「${POOL_META[state.pool.kind].title}」移除：\n${row.title}？`))) return;
      await api("/api/pools/delete", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: state.pool.kind, keys: [row.key] }) });
      toast("已删除 1 条", "warn");
      await afterPoolMutation();
    });
    el.append(chk, main, tag, del);
    box.appendChild(el);
  }
}

function syncSelUI() {
  const n = state.pool.sel.size;
  $("btnDelSel").disabled = n === 0;
  $("btnDelSel").textContent = n ? `删除所选 (${n})` : "删除所选";
  const all = $("chkAll");
  all.checked = n > 0 && n === state.pool.rows.length;
}

async function afterPoolMutation() {
  await loadPoolList();
  await loadPoolSummary();
  await refreshStatus();
}

async function importPool() {
  await doImport($("poolText").value, "");
}

/* 批量导入核心：粘贴文本或本机文件内容共用（只传内容，不存绝对路径） */
async function doImport(text, srcName) {
  if (!text.trim()) { toast("先粘贴要导入的内容，或选择文件", "warn"); return; }
  const mode = state.pool.mode;
  if (mode === "replace") {
    if (!(await askConfirm("覆盖导入", `将清空「${POOL_META[state.pool.kind].title}」现有条目，仅保留本次内容。确认覆盖？`))) return;
  }
  const r = await api("/api/pools/import", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind: state.pool.kind, text, mode }) });
  if (r.error) { toast("导入失败：" + r.error, "bad"); return; }
  const box = $("piResult");
  box.hidden = false;
  box.className = "pi-result" + (r.bad ? " warn" : "");
  let html = (srcName ? `文件 ${srcName} · ` : "") + `✓ 新增 ${r.added} 条 · 去重 ${r.dup} 条 · 现存 ${r.total} 条`;
  if (r.bad) html += `<br><span class="bad-lines">✗ ${r.bad} 行格式无效（行号 ${r.bad_lines.join(", ")}）已跳过</span>`;
  box.innerHTML = html;
  if (r.added && !srcName) $("poolText").value = "";
  updateLineMeter();
  toast(`导入完成：新增 ${r.added} · 去重 ${r.dup} · 无效 ${r.bad}`, r.bad ? "warn" : "ok");
  await afterPoolMutation();
}

function exportPool() {
  const a = document.createElement("a");
  a.href = `/api/pools/export?kind=${state.pool.kind}`;
  a.download = `${state.pool.kind}_pool.txt`;
  document.body.appendChild(a); a.click(); a.remove();
  toast("已导出原始清单", "ok");
}

async function clearPool() {
  const title = POOL_META[state.pool.kind].title;
  if (!(await askConfirm("清空数据池", `将清空「${title}」全部条目（自动备份到 data/webui）。确认清空？`))) return;
  await api("/api/pools/clear", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind: state.pool.kind }) });
  toast(`「${title}」已清空`, "warn");
  await afterPoolMutation();
}

function updateLineMeter() {
  const n = $("poolText").value.split(/\r?\n/).filter((l) => l.trim()).length;
  $("piLines").textContent = `${n} 行待导入`;
}

function bindPools() {
  $("poolTabs").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => switchPool(b.dataset.k)));
  segInit($("segImportMode"), (b) => {
    state.pool.mode = b.dataset.v;
    $("piMode").textContent = b.dataset.v === "replace" ? "覆盖模式" : "追加模式";
  });
  $("poolText").addEventListener("input", updateLineMeter);
  $("btnImport").addEventListener("click", importPool);
  $("btnPickFile").addEventListener("click", () => $("poolFile").click());
  $("poolFile").addEventListener("change", async (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = "";               // 同一文件可重复选择
    if (!f) return;
    let text;
    try { text = await f.text(); } catch { toast("文件读取失败", "bad"); return; }
    await doImport(text, f.name);
  });
  const pt = $("poolText");
  pt.addEventListener("dragover", (e) => { e.preventDefault(); pt.classList.add("drop"); });
  pt.addEventListener("dragleave", () => pt.classList.remove("drop"));
  pt.addEventListener("drop", async (e) => {
    e.preventDefault(); pt.classList.remove("drop");
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (!f) return;
    try { await doImport(await f.text(), f.name); } catch { toast("文件读取失败", "bad"); }
  });
  $("btnExport").addEventListener("click", exportPool);
  $("btnPoolClear").addEventListener("click", clearPool);
  let stm;
  $("poolSearch").addEventListener("input", (e) => {
    clearTimeout(stm);
    stm = setTimeout(() => { state.pool.q = e.target.value.trim(); loadPoolList(); }, 220);
  });
  $("chkAll").addEventListener("change", (e) => {
    const on = e.target.checked;
    state.pool.sel.clear();
    $("plRows").querySelectorAll(".pl-row").forEach((el) => {
      const cb = el.querySelector("input");
      cb.checked = on;
      el.classList.toggle("sel", on);
      if (on) state.pool.sel.add(el.dataset.key);
    });
    syncSelUI();
  });
  $("btnDelSel").addEventListener("click", async () => {
    const keys = [...state.pool.sel];
    if (!keys.length) return;
    if (!(await askConfirm("批量删除", `从「${POOL_META[state.pool.kind].title}」删除所选 ${keys.length} 条？`))) return;
    await api("/api/pools/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: state.pool.kind, keys }) });
    toast(`已删除 ${keys.length} 条`, "warn");
    await afterPoolMutation();
  });
}

/* ---------- 战果 · 成功成果 ---------- */
state.result = { fmt: "txt", rows: [], q: "" };
const RS_COLOR = { claimed: "var(--green)", stock: "var(--cyan)", registered: "var(--blue)",
  verified: "var(--blue)", sold: "var(--amber)", new: "var(--blue)",
  claiming: "var(--amber)", dead: "var(--red)" };
const RS_LABEL = { new: "待注册", registered: "已注册", verified: "已验证", claiming: "领取中",
  claimed: "已领取", stock: "库存", sold: "已售", dead: "已死" };

async function loadResults() {
  let r; try { r = await api("/api/results"); } catch { return; }
  countUp($("rsSuccess"), r.success || 0);
  countUp($("rsCredits"), r.credits || 0);
  countUp($("rsTokens"), r.tokens || 0);
  const st = r.states || {};
  const order = ["claimed", "stock", "registered", "verified", "sold", "claiming", "new", "dead"];
  const keys = order.filter((k) => st[k]).concat(Object.keys(st).filter((k) => !order.includes(k)));
  const sum = keys.reduce((a, k) => a + st[k], 0) || 1;
  $("rsBar").innerHTML = keys.map((k) =>
    `<i style="width:${(st[k] / sum) * 100}%;background:${RS_COLOR[k] || "var(--faint)"}" title="${k} ${st[k]}"></i>`).join("");
  $("rsLegend").innerHTML = keys.map((k) =>
    `<span><i style="background:${RS_COLOR[k] || "var(--faint)"}"></i>${RS_LABEL[k] || k} ${st[k]}</span>`).join("");
  state.result.rows = r.rows || [];
  renderRsRows();
}

function renderRsRows() {
  const q = state.result.q.toLowerCase();
  const rows = state.result.rows.filter((r) =>
    !q || r.email.toLowerCase().includes(q) || r.state.includes(q));
  $("rsCount").textContent = `${rows.length} 条`;
  const box = $("rsRows");
  box.textContent = "";
  if (!rows.length) { box.innerHTML = '<div class="pl-empty">暂无成功账号</div>'; return; }
  for (const r of rows) {
    const el = document.createElement("div");
    el.className = "pl-row rs-row";
    const main = document.createElement("div");
    main.className = "pl-main";
    const b = document.createElement("b"); b.textContent = r.email;
    const sp = document.createElement("span"); sp.textContent = `${r.meta} · ${r.created}`;
    main.append(b, sp);
    const tag = document.createElement("span");
    tag.className = "pl-tag" + (r.state === "stock" ? " cyan" : r.state === "sold" ? " amber" : "");
    tag.textContent = RS_LABEL[r.state] || r.state;
    el.append(main, tag);
    if (r.credits > 0) {
      const c = document.createElement("span");
      c.className = "rs-cred"; c.textContent = `+${r.credits}`;
      el.append(c);
    }
    if (r.has_token) {
      const t = document.createElement("span");
      t.className = "pl-tag amber"; t.textContent = "token";
      el.append(t);
    }
    box.appendChild(el);
  }
}

function copyText(t) {
  const done = () => toast("路径已复制", "ok");
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(t).then(done).catch(() => fallbackCopy(t, done));
  } else fallbackCopy(t, done);
}
function fallbackCopy(t, done) {
  const ta = document.createElement("textarea");
  ta.value = t; document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); done(); } catch { toast("复制失败，请手动选择路径", "warn"); }
  ta.remove();
}

function pathRow(p, url, fname) {
  const div = document.createElement("div");
  div.className = "rs-path";
  const code = document.createElement("code");
  code.textContent = p; code.title = p;
  const cp = document.createElement("button");
  cp.className = "btn tiny ghost"; cp.textContent = "复制";
  cp.addEventListener("click", () => copyText(p));
  div.append(code, cp);
  if (url) {
    const dl = document.createElement("a");
    dl.className = "btn tiny ghost"; dl.textContent = "下载";
    dl.href = url; dl.download = fname || "";
    div.append(dl);
  }
  return div;
}

async function exportResults() {
  const r = await api("/api/results/export", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fmt: state.result.fmt }) });
  if (r.error) { toast("导出失败：" + r.error, "bad"); return; }
  state.lastExport = r;
  const box = $("rsOut");
  box.hidden = false; box.textContent = "";
  const head = document.createElement("div");
  head.className = "ro-head";
  head.textContent = `✓ 已导出 ${r.count} 条 · ${r.bytes} B · ${r.rel}`;
  box.append(head, pathRow(r.path, r.url, r.filename));
  toast(`成果已导出 · ${r.rel}`, "ok");
}

async function exportAll() {
  const r = await api("/api/results/export_all", { method: "POST" });
  if (r.error) { toast("导出失败：" + r.error, "bad"); return; }
  const box = $("rsOut");
  box.hidden = false; box.textContent = "";
  const head = document.createElement("div");
  head.className = "ro-head";
  head.textContent = `✓ 三件套 · 账号 ${r.accounts} 条 · token ${r.tokens} 条`;
  box.append(head);
  for (const k of ["accounts", "tokens", "summary"]) box.append(pathRow(r.files[k]));
  toast("三件套已重写到 data/output", "ok");
}

function bindResults() {
  segInit($("segResultFmt"), (b) => { state.result.fmt = b.dataset.v; });
  $("btnExportResults").addEventListener("click", exportResults);
  $("btnExportAll").addEventListener("click", exportAll);
  let tm;
  $("rsSearch").addEventListener("input", (e) => {
    clearTimeout(tm);
    tm = setTimeout(() => { state.result.q = e.target.value.trim(); renderRsRows(); }, 200);
  });
}

/* ---------- 事件绑定 ---------- */
function bind() {
  $("btnRefresh").addEventListener("click", async () => {
    await refreshStatus();
    toast("状态已刷新");
  });
  $("btnSave").addEventListener("click", saveConfig);
  $("btnCheck").addEventListener("click", async () => {
    toast("正在自检 2Captcha 直连…", "warn");
    const r = await api("/api/check/captcha", { method: "POST" });
    toast(r.ok ? `2Captcha 通 · 余额 $${(r.balance || 0).toFixed(2)} · ${r.latency}s`
               : "2Captcha 直连失败（网络 / Key）", r.ok ? "ok" : "bad");
    await refreshStatus();
  });
  $("btnLaunch").addEventListener("click", launch);
  $("btnStop").addEventListener("click", stopRun);
  $("btnClear").addEventListener("click", () => {
    $("term").innerHTML = '<span class="dim">日志已清空</span>';
    state.lineCount = 0;
    $("termCount").textContent = "0 行";
  });

  segInit($("segProvider"), async (b) => {
    const v = b.dataset.v;
    const r = await api("/api/config", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ "mail.provider": v }) });
    if (r.error || !(r.applied || []).includes("mail.provider")) {
      toast("保存失败 · " + ((r.skipped || [])[0] || r.error || ""), "bad");
      segSet($("segProvider"), String(state.config["mail.provider"] ?? ""));
      return;
    }
    state.config["mail.provider"] = v;
    $("tagProvider").textContent = v || "自动";
    $("tagProvider").className = "tag " + (v ? "on" : "off");
    toast("邮箱源已保存 · " + (v || "自动"));
  });
  segInit($("segRegion"), async (b) => {
    const v = b.dataset.v;
    const r = await api("/api/config", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ "region": v }) });
    if (r.error || !(r.applied || []).includes("region")) {
      toast("保存失败 · " + ((r.skipped || [])[0] || r.error || ""), "bad");
      segSet($("segRegion"), String(state.config.region ?? "sg"));
      return;
    }
    state.config.region = v;
    applyProxyUI($("tgProxy").classList.contains("on"));
    toast("出口地区已保存 · " + v.toUpperCase());
  });
  segInit($("segIdentity"), async (b) => {
    const v = b.dataset.v;
    const r = await api("/api/config", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ "device.identity_mode": v }) });
    if (r.error || !(r.applied || []).includes("device.identity_mode")) {
      toast("保存失败 · " + ((r.skipped || [])[0] || r.error || ""), "bad");
      segSet($("segIdentity"), String(state.config["device.identity_mode"] ?? "random"));
      return;
    }
    state.config["device.identity_mode"] = v;
    renderConfig();
    toast(v === "browser" ? "设备身份：浏览器匹配 · 已保存" : "设备身份：随机新设备 · 已保存");
  });
  segInit($("segImapPreset"), (b) => {
    if (b.dataset.host) {
      $("inHost").value = b.dataset.host;
      $("inPort").value = b.dataset.port;
    } else {
      $("inHost").focus();
    }
  });
  segInit($("segCaptchaRoute"), (b) => {
    if (!b.dataset.v) {
      $("inImapProxy").value = "";
    } else if (!$("inImapProxy").value.trim()) {
      $("inImapProxy").value = "http://127.0.0.1:7897";
    }
    $("lmCap").textContent = b.dataset.v ? "本机隧道" : "本机直连";
  });

  const toggleProxy = async () => {
    const on = !$("tgProxy").classList.contains("on");
    applyProxyUI(on);   // 先切 UI，保存失败再回滚
    const r = await api("/api/config", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ "proxy.enabled": on }) });
    if (r.error || !(r.applied || []).includes("proxy.enabled")) {
      applyProxyUI(!on);
      toast("网络模式保存失败 · " + ((r.skipped || [])[0] || r.error || ""), "bad");
      return;
    }
    state.config["proxy.enabled"] = on;
    toast(on ? "网络出口：代理 IP · 已保存" : "网络出口：直连 · 已保存");
  };
  $("tgProxy").addEventListener("click", toggleProxy);
  $("tgProxy").addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleProxy(); }
  });

  const bindStepper = (minusId, inputId, plusId, min, max) => {
    const clamp = (v) => Math.min(max, Math.max(min, v));
    const setVal = (v) => {
      $(inputId).value = clamp(v);
      $(inputId).dispatchEvent(new Event("input"));  // 触发本地持久化
    };
    $(minusId).addEventListener("click", () => {
      setVal(parseInt($(inputId).value || String(min), 10) - 1);
    });
    $(plusId).addEventListener("click", () => {
      setVal(parseInt($(inputId).value || String(min), 10) + 1);
    });
    $(inputId).addEventListener("change", () => {
      setVal(parseInt($(inputId).value || String(min), 10));
    });
  };
  bindStepper("btnWMinus", "inWorkers", "btnWPlus", 1, 8);
  bindStepper("btnTMinus", "inTarget", "btnTPlus", 1, 999);
  // 并发/目标：改动即静默存服务端，「保存参数」按钮显式确认
  ["inWorkers", "inTarget"].forEach((id) =>
    $(id).addEventListener("input", () => saveUiPrefs(false)));
  $("btnSaveParams").addEventListener("click", () => saveUiPrefs(true));

  $("logFilters").querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => {
      $("logFilters").querySelectorAll("button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.filter = b.dataset.f;
      $("term").dataset.filter = state.filter;
    });
  });
}

/* ---------- 启动 ---------- */
$("svcAddr").textContent = location.host;   // 端口自动检测，不写死
loadUiPrefs();   // 恢复上次的并发/目标
bind();
bindPools();
bindResults();
tickClock();
setInterval(tickClock, 1000);
loadConfig().then(() => refreshStatus());
loadPoolSummary().then(() => switchPool("mail"));
loadResults();
pollTail();  // 恢复上次运行的进度面板
loadBbPool();
setInterval(() => { if (!document.hidden) loadBbPool(); }, 10000);
setInterval(() => { refreshStatus(); loadResults(); }, 15000);
