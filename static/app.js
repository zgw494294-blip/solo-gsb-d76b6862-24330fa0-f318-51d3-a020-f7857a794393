"use strict";

let expId = null;
// expGen：每次新建 / 冻结实验自增，用于作废在途（迟到）响应，
// 使旧实验的迟到响应不可能恢复旧展示。从 1 开始，init 的首次加载即第 1 代。
let expGen = 1;
// expFrozen：当前实验已删除，旧实验操作全部禁用，轮询停止；
// 只有「新建实验」可以解除。
let expFrozen = false;
// creating：「新建实验」请求进行中。期间暂停旧轮询、禁用一切旧实验操作
// 入口（定时器已清除，creating 再拦下已入队的迟到回调）；成功后只为新 ID
// 启用控件与轮询，失败则按创建前状态回退（冻结页仍冻结、存活页恢复轮询）。
let creating = false;
let statusTimer = null;
let committedTimer = null;
// 404（服务端重启）后多个刷新可能同时到达：用单一在途 Promise 去重，
// 保证同一时刻只会新建一个实验
let creatingPromise = null;
const lastRender = { a: "", b: "" };
// 最近一次被服务端确认的只读模式（初始 / 新建后为 false）。开关请求失败时
// 复选框与文案一律恢复到它：请求期间轮询若已带回新确认，就恢复成新值；
// 期间没有任何新确认，则恢复点击前的旧模式。乐观渲染只改 DOM，不动这里。
const confirmedReadonly = { a: false, b: false };
// 两栏的「手工请求在途」禁用（只禁用本栏，另一栏照常）。
// 两栏的最终可用性 = 非创建 / 非冻结 && 非脚本锁定 && 本栏无在途请求。
const panelBusy = { a: false, b: false };
// 最近一次状态轮询 / 脚本响应确认的脚本视图（null = 无脚本）。
// 脚本有效（active）期间两栏手工控件与只读开关由后端锁定。
let scriptState = null;
// 脚本请求（装载 / 推进 / 跳过 / 取消）在途：禁用全部脚本控件并锁住两栏。
let scriptBusy = false;
// 补偿请求在途：禁用时间线区块的补偿输入与按钮（不影响两栏与检查点）。
let compensateBusy = false;
// 只读 SQL 工作台：当前最新查询视图（null = 无查询）与请求在途标志。
// wbQuery.id 用于轮询 / 取消；新查询提交后旧 ID 的迟到响应一律丢弃
// （后端对旧 ID 回 404，前端亦不渲染）。查询轮询用独立定时器，与实验
// 状态轮询分开：进入终态即停止，creating / 冻结期间不发请求。
let wbQuery = null;
let wbBusy = false; // 提交 / 取消请求在途
let wbPollTimer = null;

const $ = (id) => document.getElementById(id);

// 实验删除开始后（或本次运行中已删除 ID 再次访问）：410，不自动新建
class ExpDeleted extends Error {}
// 未知实验（如服务端重启）：404，沿用旧语义自动新建
class ExpNotFound extends Error {}

async function fetchJson(path, options) {
  const resp = await fetch(path, options);
  if (resp.status === 410) {
    throw new ExpDeleted();
  }
  if (resp.status === 404) {
    throw new ExpNotFound();
  }
  if (!resp.ok) {
    throw new Error("HTTP " + resp.status);
  }
  return resp.json();
}

function stopPolling() {
  // 清除两组轮询定时器。注意 clearInterval 不会撤销已经入队、即将执行的
  // 回调，因此各回调入口还要再检查 creating / expFrozen。
  if (statusTimer !== null) {
    clearInterval(statusTimer);
    statusTimer = null;
  }
  if (committedTimer !== null) {
    clearInterval(committedTimer);
    committedTimer = null;
  }
}

function freezeExperiment(reason) {
  // 删除成功或收到 410：停止轮询并禁用全部旧实验操作。幂等。
  if (expFrozen) return;
  expFrozen = true;
  expGen++; // 作废一切在途响应，迟到响应不再渲染 / 不再恢复按钮
  stopPolling();
  setBusy("a", true);
  setBusy("b", true);
  setCheckpointBusy(true);
  setCompensateBusy(true);
  $("refresh-committed").disabled = true;
  $("delete-exp").disabled = true;
  $("panel-a").classList.add("exp-frozen");
  $("panel-b").classList.add("exp-frozen");
  $("checkpoint").classList.add("exp-frozen");
  $("timeline").classList.add("exp-frozen");
  $("script").classList.add("exp-frozen");
  $("sql-workbench").classList.add("exp-frozen");
  scriptState = null;
  scriptBusy = false;
  stopWbPolling();
  wbQuery = null;
  wbBusy = false;
  syncPanelControls();
  syncScriptControls();
  syncWbControls();
  const note = $("deleted-note");
  note.textContent = reason;
  note.hidden = false;
}

function newExperiment() {
  // 去重：并发的自动新建（404）共用同一个 Promise；手动点击同样复用
  if (creatingPromise) return creatingPromise;
  creatingPromise = createExperiment().finally(() => {
    creatingPromise = null;
  });
  return creatingPromise;
}

async function createExperiment() {
  const wasFrozen = expFrozen;
  const oldId = expId;
  const frozenNote = wasFrozen ? $("deleted-note").textContent : "";
  const gen = ++expGen;
  // 创建期间：暂停旧实验轮询（creating 同时拦下已入队的迟到回调），
  // 旧实验的操作入口全部保持禁用，创建成功前没有任何请求可以发到旧 ID。
  creating = true;
  stopPolling();
  // 与状态轮询一致：创建期间立即停掉旧实验的查询轮询（回调虽有 creating
  // 兜底，但定时器不再保留），创建成功后随新实验状态重新启动
  stopWbPolling();
  // 解除旧实验冻结状态；创建请求完成前先保持控件禁用，
  // 避免在途点击把操作发到旧 expId。
  expFrozen = false;
  $("deleted-note").hidden = true;
  $("panel-a").classList.remove("exp-frozen");
  $("panel-b").classList.remove("exp-frozen");
  $("checkpoint").classList.remove("exp-frozen");
  $("timeline").classList.remove("exp-frozen");
  $("script").classList.remove("exp-frozen");
  $("sql-workbench").classList.remove("exp-frozen");
  panelBusy.a = false;
  panelBusy.b = false;
  scriptState = null;
  scriptBusy = false;
  stopWbPolling();
  wbQuery = null;
  wbBusy = false;
  setBusy("a", true);
  setBusy("b", true);
  setCheckpointBusy(true);
  setCompensateBusy(true);
  syncScriptControls();
  // 创建请求在途：工作台一并禁用（创建成功后的复位在下方新 ID 段完成）
  wbBusy = false;
  syncWbControls();
  $("refresh-committed").disabled = true;
  $("delete-exp").disabled = true;
  $("exp-id").textContent = "…";
  $("committed").textContent = "…";

  const failCreate = (message) => {
    if (gen !== expGen) return; // 期间又触发了新建 / 冻结
    creating = false;
    showError("exp", { name: null, code: null, message: "新建实验失败：" + message });
    if (wasFrozen) {
      // 从已删除页面发起的新建失败：旧操作仍禁用、轮询停止，
      // 保持冻结页面，等待手动重试。
      $("exp-id").textContent = oldId;
      $("committed").textContent = "—";
      freezeExperiment(frozenNote);
    } else {
      // 存活页面创建失败：恢复旧实验控件与轮询，错误显示在顶部。
      if (oldId) {
        expId = oldId;
        history.replaceState(null, "", "?exp=" + expId);
      } else {
        history.replaceState(null, "", location.pathname);
      }
      $("exp-id").textContent = expId || "…";
      $("committed").textContent = "—";
      setBusy("a", false);
      setBusy("b", false);
      setCheckpointBusy(false);
      setCompensateBusy(false);
      syncScriptControls();
      syncWbControls();
      $("refresh-committed").disabled = false;
      $("delete-exp").disabled = false;
      // 立即拉一次旧实验状态，随后恢复轮询
      if (oldId) {
        refreshStatus(gen).catch(() => {});
        refreshCommitted(gen).catch(() => {});
        startPolling();
      }
    }
  };

  let data;
  try {
    const resp = await fetch("/api/experiments", { method: "POST" });
    if (!resp.ok) {
      throw new Error("HTTP " + resp.status);
    }
    data = await resp.json();
  } catch (e) {
    failCreate(e.message);
    return;
  }
  if (gen !== expGen) return; // 期间又触发了新建 / 冻结
  if (!data || !data.id) {
    failCreate("响应缺少实验 ID");
    return;
  }

  // 创建成功：从此刻起一切只围绕新 ID。creating 先复位，旧 ID 的迟到响应
  // 因 gen 不匹配早已全部作废，不可能再影响下面的渲染 / 轮询。
  creating = false;
  expId = data.id;
  history.replaceState(null, "", "?exp=" + expId);
  $("exp-id").textContent = expId;
  for (const side of ["a", "b"]) {
    lastRender[side] = "";
    confirmedReadonly[side] = false;
    $(`tx-${side}`).textContent = "…";
    $(`dot-${side}`).classList.remove("on");
    $(`read-${side}`).textContent = "—";
    $(`ro-${side}`).textContent = "…";
    $(`ro-${side}`).classList.remove("on");
    $(`ro-toggle-${side}`).checked = false;
    $(`log-${side}`).innerHTML = "";
    clearError(side);
  }
  $("ckpt-result").textContent = "尚未执行过检查点";
  $("ckpt-result").classList.remove("busy");
  $("log-ckpt").innerHTML = "";
  $("timeline-list").innerHTML = "";
  $("compensate-seq").value = "";
  clearError("timeline");
  clearError("ckpt");
  clearError("exp");
  // 新实验无脚本：清空脚本区并恢复「装载」可用
  scriptState = null;
  scriptBusy = false;
  $("script-input").value = "";
  $("script-state").textContent = "尚未装载脚本";
  $("script-state").className = "script-state";
  $("script-records").innerHTML = "";
  clearError("script");
  // 新实验工作台：清空查询 / 结果，恢复「提交」可用（SQL 文本框保留，
  // 方便在不同实验间复用同一条只读查询）
  wbQuery = null;
  wbBusy = false;
  stopWbPolling();
  $("wb-state").textContent = "尚未提交查询";
  $("wb-state").className = "wb-state";
  $("wb-count").textContent = "";
  $("wb-result").hidden = true;
  $("wb-result").innerHTML = "";
  clearError("wb");
  syncWbControls();

  setBusy("a", false);
  setBusy("b", false);
  setCheckpointBusy(false);
  setCompensateBusy(false);
  syncScriptControls();
  $("refresh-committed").disabled = false;
  $("delete-exp").disabled = false;

  // 新建后的首次刷新绝不允许再触发自动新建：否则 404 会递归 newExperiment，
  // 令刚建好的实验被孤立；这里 404 当作瞬时错误，等待下一轮轮询恢复。
  await refreshStatus(gen, { allowAutoCreate: false });
  if (gen !== expGen || expFrozen) return;
  await refreshCommitted(gen, { allowAutoCreate: false });
  if (gen !== expGen || expFrozen) return;
  startPolling();
}

function startPolling() {
  // 先清理旧定时器，因此重复调用也只会保留一组轮询
  if (statusTimer !== null) clearInterval(statusTimer);
  if (committedTimer !== null) clearInterval(committedTimer);
  // 状态轮询只返回服务端内存状态，不执行任何 SELECT，不会提前建立快照
  statusTimer = setInterval(() => {
    const gen = expGen;
    if (creating || expFrozen) return;
    refreshStatus(gen).catch(() => {});
  }, 2000);
  committedTimer = setInterval(() => {
    const gen = expGen;
    if (creating || expFrozen) return;
    refreshCommitted(gen).catch(() => {});
  }, 2000);
}

async function refreshStatus(gen, opts = {}) {
  const allowAutoCreate = opts.allowAutoCreate !== false;
  if (!expId || creating || expFrozen) return;
  try {
    const data = await fetchJson(`/api/experiments/${expId}/status`);
    if (gen !== expGen || expFrozen) return;
    renderPanel("a", data.a);
    renderPanel("b", data.b);
    renderCheckpoint(data.checkpoint);
    renderTimeline(data.timeline);
    // 脚本状态来自内存：轮询不在两栏连接上额外读值
    renderScript(data.script === undefined ? null : data.script);
  } catch (e) {
    if (e instanceof ExpDeleted) {
      if (gen === expGen) {
        freezeExperiment(`实验 ${expId} 已删除（410）：轮询已停止，旧实验操作已禁用，可点击「新建实验」继续。`);
      }
      return;
    }
    if (e instanceof ExpNotFound) {
      // 服务端重启后旧实验已不存在：自动新建一个（仅处理当前这一代）
      if (allowAutoCreate && gen === expGen && !expFrozen) {
        await newExperiment();
      }
      return;
    }
    // 其他瞬时错误忽略，下一轮轮询会恢复
  }
}

async function refreshCommitted(gen, opts = {}) {
  const allowAutoCreate = opts.allowAutoCreate !== false;
  if (!expId || creating || expFrozen) return;
  try {
    const data = await fetchJson(`/api/experiments/${expId}/committed`);
    if (gen !== expGen || expFrozen) return;
    $("committed").textContent = data.committed;
  } catch (e) {
    if (e instanceof ExpDeleted) {
      if (gen === expGen) {
        freezeExperiment(`实验 ${expId} 已删除（410）：轮询已停止，旧实验操作已禁用，可点击「新建实验」继续。`);
      }
      return;
    }
    if (e instanceof ExpNotFound) {
      // 404（服务端重启）：与状态刷新共用同一个去重 Promise 自动新建
      if (allowAutoCreate && gen === expGen && !expFrozen) {
        await newExperiment();
      }
      return;
    }
    // 其他瞬时错误忽略，下一轮轮询会恢复
  }
}

function syncReadonlyDisplay(side, ro) {
  // 只按给定模式对齐复选框与文案，不碰“最近一次服务端确认”记录。
  // 程序化设置 checked 不会触发 change 事件，不会形成请求回环。
  ro = !!ro;
  const roEl = $("ro-" + side);
  roEl.textContent = ro ? "开启" : "关闭";
  roEl.classList.toggle("on", ro);
  const roToggle = $("ro-toggle-" + side);
  if (roToggle.checked !== ro) roToggle.checked = ro;
}

function renderPanel(side, state) {
  // 只读控件的同步必须先于渲染去重：即使状态 JSON 与上次完全相同，
  // 也要用服务端模式纠正本地偏差（例如开关请求失败后复选框与文案被
  // 本地回退、而轮询返回的仍是同一个模式）。
  const ro = !!state.readonly;
  confirmedReadonly[side] = ro;
  syncReadonlyDisplay(side, ro);

  const key = JSON.stringify(state);
  if (lastRender[side] === key) return;
  lastRender[side] = key;

  const inTx = state.in_transaction;
  $("tx-" + side).textContent = inTx ? "已开启" : "未开启";
  const dot = $("dot-" + side);
  dot.classList.toggle("on", inTx);
  dot.title = inTx ? "事务已开启" : "事务未开启";
  $("read-" + side).textContent = state.last_read === null ? "—" : state.last_read;

  const logEl = $("log-" + side);
  logEl.innerHTML = "";
  for (const entry of state.log) {
    const li = document.createElement("li");
    li.textContent = entry;
    if (entry.includes("→ 失败") || entry.includes("→ 拒绝")) {
      li.classList.add("log-error");
    }
    logEl.appendChild(li);
  }
  logEl.scrollTop = logEl.scrollHeight;
}

function renderCheckpoint(state) {
  // 只渲染服务端内存中的最近结果缓存：轮询本身不触发检查点
  const resultEl = $("ckpt-result");
  const last = state && state.last;
  if (!last) {
    resultEl.textContent = "尚未执行过检查点";
    resultEl.classList.remove("busy");
  } else {
    resultEl.textContent =
      `最近一次 ${last.mode}：busy=${last.busy}，` +
      `WAL 总帧数=${last.log_frames}，已回写帧数=${last.checkpointed}，` +
      `WAL 文件大小=${last.wal_size} 字节`;
    resultEl.classList.toggle("busy", last.busy === 1);
  }

  const logEl = $("log-ckpt");
  logEl.innerHTML = "";
  for (const entry of (state && state.log) || []) {
    const li = document.createElement("li");
    li.textContent = entry;
    if (entry.includes("失败") || entry.includes("busy=1")) {
      li.classList.add("log-error");
    }
    logEl.appendChild(li);
  }
  logEl.scrollTop = logEl.scrollHeight;
}

const TIMELINE_SIDE_LABELS = { a: "甲（A）", b: "乙（B）", system: "系统" };

function renderTimeline(records) {
  // 只渲染服务端内存中的提交时间线：轮询本身不读库、不触碰两栏连接
  const listEl = $("timeline-list");
  listEl.innerHTML = "";
  const items = Array.isArray(records) ? records : [];
  if (items.length === 0) {
    const li = document.createElement("li");
    li.textContent = "暂无已提交的写入事务";
    li.classList.add("timeline-empty");
    listEl.appendChild(li);
    return;
  }
  for (const rec of items) {
    const li = document.createElement("li");
    const side = TIMELINE_SIDE_LABELS[rec.side] || rec.side;
    const deltas = (rec.deltas || []).join(", ");
    li.textContent =
      `#${rec.seq}  ${side}  ${rec.before} → ${rec.after}  增量 [${deltas}]`;
    if (rec.side === "system") {
      // 补偿记录：附被补偿记录的原 seq
      li.textContent += `  补偿 #${rec.compensates}`;
      li.classList.add("timeline-system");
    }
    listEl.appendChild(li);
  }
  listEl.scrollTop = listEl.scrollHeight;
}

const SCRIPT_OP_LABELS = {
  begin: "BEGIN DEFERRED",
  read: "读值",
  add: "加上",
  commit: "COMMIT",
  rollback: "ROLLBACK",
};
const SCRIPT_STATUS_LABELS = {
  active: "执行中",
  finished: "已完成",
  cancelled: "已取消",
};

function describeScriptStep(step) {
  // 第 N 步（甲/乙）：操作 [增量]
  const side = TIMELINE_SIDE_LABELS[step.side] || step.side;
  const label = SCRIPT_OP_LABELS[step.op] || step.op;
  return step.op === "add"
    ? `第 ${step.step} 步（${side}）：${label} ${step.value}`
    : `第 ${step.step} 步（${side}）：${label}`;
}

function renderScript(state) {
  // 只渲染服务端内存中的脚本视图：轮询本身不额外读两栏值、不触发任何 SQL。
  // scriptState 更新后统一同步两栏锁定与脚本按钮可用性。
  scriptState = state || null;
  const stateEl = $("script-state");
  const listEl = $("script-records");
  listEl.innerHTML = "";
  if (!scriptState) {
    stateEl.textContent = "尚未装载脚本";
    stateEl.className = "script-state";
    const li = document.createElement("li");
    li.textContent = "两栏均无事务且无有效脚本时可装载 1～50 步 JSON";
    li.classList.add("script-empty");
    listEl.appendChild(li);
    syncPanelControls();
    syncScriptControls();
    return;
  }
  const statusLabel = SCRIPT_STATUS_LABELS[scriptState.status] || scriptState.status;
  if (scriptState.status === "active") {
    stateEl.textContent =
      `脚本 ${scriptState.id}（${statusLabel}）：当前第 ${scriptState.step} / ` +
      `${scriptState.total} 步`;
  } else {
    stateEl.textContent =
      `脚本 ${scriptState.id}（${statusLabel}）：共 ${scriptState.total} 步` +
      (scriptState.status === "cancelled" ? `，停于第 ${scriptState.step} 步` : "");
  }
  stateEl.classList.remove("finished", "cancelled");
  if (scriptState.status !== "active") {
    stateEl.classList.add(scriptState.status);
  }

  const records = Array.isArray(scriptState.records) ? scriptState.records : [];
  if (records.length === 0) {
    const li = document.createElement("li");
    li.textContent = "尚无执行记录";
    li.classList.add("script-empty");
    listEl.appendChild(li);
  }
  const stepByIdx = new Map();
  for (const step of scriptState.steps || []) stepByIdx.set(step.step, step);
  for (const rec of records) {
    const li = document.createElement("li");
    const step = stepByIdx.get(rec.step) || { step: rec.step, side: rec.side, op: rec.op };
    const head = describeScriptStep(step);
    let tail;
    if (rec.result === "skipped") {
      tail = "→ 跳过";
    } else if (rec.result === "success") {
      tail = "→ 成功";
    } else {
      const err = rec.error || {};
      tail = err.name
        ? `→ 失败：${err.name}（错误码 ${err.code}） ${err.message || ""}`
        : `→ 失败：${err.message || ""}`;
    }
    li.textContent = head + "  " + tail;
    if (rec.result === "failure") li.classList.add("log-error");
    if (rec.result === "skipped") li.classList.add("script-skipped");
    listEl.appendChild(li);
  }
  listEl.scrollTop = listEl.scrollHeight;
  syncPanelControls();
  syncScriptControls();
}

function setBusy(side, busy) {
  // 记录本栏手工请求是否在途，再按「创建 / 冻结 / 脚本锁定 / 在途」
  // 统一计算两栏控件的最终可用性（另一栏的在途请求不影响本栏）。
  panelBusy[side] = !!busy;
  syncPanelControls();
}

function scriptActive() {
  return !!(scriptState && scriptState.status === "active");
}

function syncPanelControls() {
  // 两栏控件的唯一出口：创建中 / 冻结 / 脚本有效或脚本请求在途时全部禁用；
  // 否则仅禁用各自有在途手工请求的那一栏。脚本锁定不由前端按钮自行解除，
  // 一切以后端状态（轮询 / 动作响应）为准。
  const lockedAll = creating || expFrozen || scriptBusy || scriptActive();
  for (const side of ["a", "b"]) {
    const disabled = lockedAll || panelBusy[side];
    for (const el of document.querySelectorAll(
      `#panel-${side} button, #panel-${side} input`
    )) {
      el.disabled = disabled;
    }
  }
}

function setScriptBusy(busy) {
  scriptBusy = !!busy;
  syncScriptControls();
  syncPanelControls();
}

function syncScriptControls() {
  // 装载 / 推进 / 跳过 / 取消按钮的可用性全部以后端脚本状态为准：
  // 创建中 / 冻结 / 脚本请求在途一律禁用；active 时只能推进 / 跳过 / 取消，
  // 非 active（无脚本 / 已完成 / 已取消）时只能装载。
  const frozen = creating || expFrozen || scriptBusy;
  const active = scriptActive();
  $("script-load").disabled = frozen || active;
  $("script-example").disabled = frozen || active;
  $("script-input").disabled = frozen || active;
  $("script-advance").disabled = frozen || !active;
  $("script-skip").disabled = frozen || !active;
  $("script-cancel").disabled = frozen || !active;
}

function showError(side, error) {
  const box = $("error-" + side);
  box.textContent = error.name
    ? `${error.name}（错误码 ${error.code}）：${error.message}`
    : error.message;
  box.hidden = false;
}

function clearError(side) {
  $("error-" + side).hidden = true;
}

async function doOp(side, op) {
  if (creating || expFrozen) return;
  // 脚本有效期间手工事务操作由后端锁定（409）；前端直接不发请求，
  // 控件可用性以轮询 / 脚本响应确认的 scriptState 为准。
  if (scriptActive()) return;
  const body = { op };
  if (op === "add") {
    const raw = $(`add-input-${side}`).value.trim();
    if (!/^[+-]?\d+$/.test(raw)) {
      showError(side, { name: null, code: null, message: "请输入一个整数" });
      return;
    }
    // 大整数以十进制字符串传输避免精度丢失；安全整数范围内仍发送数字
    const big = BigInt(raw);
    body.value =
      big >= BigInt(Number.MIN_SAFE_INTEGER) &&
      big <= BigInt(Number.MAX_SAFE_INTEGER)
        ? Number(big)
        : big.toString();
  }
  const gen = expGen;
  setBusy(side, true);
  clearError(side);
  try {
    const data = await fetchJson(`/api/experiments/${expId}/conn/${side}/op`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (gen !== expGen) return;
    renderPanel(side, data.state);
    if (!data.ok && data.error) {
      showError(side, data.error);
    }
    refreshCommitted(gen);
  } catch (e) {
    if (e instanceof ExpDeleted) {
      if (gen === expGen) {
        freezeExperiment(`实验 ${expId} 已删除（410）：操作未执行，旧实验操作已禁用，可点击「新建实验」继续。`);
      }
      return;
    }
    if (e instanceof ExpNotFound) {
      if (gen === expGen && !expFrozen) {
        await newExperiment();
      }
      return;
    }
    if (gen === expGen && !expFrozen) {
      showError(side, { name: null, code: null, message: "请求失败：" + e.message });
    }
  } finally {
    // 冻结 / 已切换到新实验后，迟到响应不得改动当前按钮状态
    if (gen === expGen && !expFrozen) {
      setBusy(side, false);
    }
  }
}

function bindPanel(side) {
  for (const btn of document.querySelectorAll(`#panel-${side} button[data-op]`)) {
    btn.addEventListener("click", () => doOp(side, btn.dataset.op));
  }
  // 只读开关：每栏独立，遵守与该栏操作相同的「请求期间禁用本栏」规则。
  // 点击时复选框已是新状态；失败 / 410 / 冻结 / 迟到都按服务端状态回退。
  $("ro-toggle-" + side).addEventListener("change", (ev) => {
    doSetReadonly(side, ev.target.checked).catch(() => {});
  });
}

async function doSetReadonly(side, enabled) {
  if (creating || expFrozen) return;
  // 脚本有效期间只读切换同样被后端锁定：不发请求
  if (scriptActive()) {
    syncReadonlyDisplay(side, confirmedReadonly[side]);
    return;
  }
  const gen = expGen;
  // 与操作按钮相同：请求未完成时禁用本栏全部控件（含复选框），另一栏不受影响
  setBusy(side, true);
  clearError(side);
  // 点击后复选框已是新状态（浏览器原生行为）；文案同步到新值，保证请求期间
  // 勾选与文案一致。这只是乐观渲染，不更新 confirmedReadonly。
  syncReadonlyDisplay(side, enabled);
  try {
    const data = await fetchJson(
      `/api/experiments/${expId}/conn/${side}/readonly`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      }
    );
    if (gen !== expGen) return;
    renderPanel(side, data.state);
    if (!data.ok && data.error) {
      // 应用层拒绝（模式未变更，状态已由 renderPanel 同步）：错误显示在本栏。
      showError(side, data.error);
    }
  } catch (e) {
    if (e instanceof ExpDeleted) {
      if (gen === expGen) {
        freezeExperiment(`实验 ${expId} 已删除（410）：只读开关未变更，旧实验操作已禁用，可点击「新建实验」继续。`);
      }
      return;
    }
    if (e instanceof ExpNotFound) {
      if (gen === expGen && !expFrozen) {
        await newExperiment();
      }
      return;
    }
    if (gen === expGen && !expFrozen) {
      // 瞬时失败：按最近一次服务端确认的模式同时恢复复选框与文案
      // （取当前记录而非请求开始时的快照：期间轮询若已确认新模式，
      // 这里就是新模式；没有任何新确认则是点击前模式），错误显示在
      // 本栏；之后相同模式的轮询也会再次执行同样的纠正。
      syncReadonlyDisplay(side, confirmedReadonly[side]);
      showError(side, { name: null, code: null, message: "只读开关请求失败：" + e.message });
    }
  } finally {
    // 冻结 / 已切换到新实验后，迟到响应不得改动当前控件状态
    if (gen === expGen && !expFrozen) {
      setBusy(side, false);
    }
  }
}

// 脚本区 409（互斥 / 过期）：不自动新建，提示并以下一次轮询的服务端
// 状态为准重新核对；这里立即拉一次状态，使新脚本 / 步号尽快同步。
// 该 409 感知的 fetch 也被补偿请求复用（过期序号 / 补偿记录 / 脚本有效）。
class ScriptConflict extends Error {}

async function fetchScriptJson(path, options) {
  const resp = await fetch(path, options);
  if (resp.status === 410) throw new ExpDeleted();
  if (resp.status === 404) throw new ExpNotFound();
  let body = null;
  try {
    body = await resp.json();
  } catch (e) {
    body = {};
  }
  if (resp.status === 409) throw new ScriptConflict((body && body.error) || "conflict");
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  return body;
}

// 「乙遇写锁后跳过、甲提交、乙再次加法」的 README 示例脚本
function exampleScriptText() {
  const steps = [
    { side: "a", op: "begin" },
    { side: "a", op: "add", value: 5 },
    { side: "b", op: "begin" },
    { side: "b", op: "add", value: 7 },
    // 第 5 步：乙撞写锁后「跳过本步」（由人在页面点跳过，不占脚本条目）
    { side: "a", op: "commit" },
    { side: "b", op: "add", value: 7 },
    { side: "b", op: "commit" },
  ];
  return JSON.stringify(steps, null, 2);
}

async function doScriptLoad() {
  if (creating || expFrozen) return;
  const raw = $("script-input").value.trim();
  let steps;
  if (!raw) {
    showError("script", { name: null, code: null, message: "请粘贴 1～50 步的 JSON 数组" });
    return;
  }
  try {
    steps = JSON.parse(raw);
  } catch (e) {
    showError("script", { name: null, code: null, message: "JSON 解析失败（整份未装载）：" + e.message });
    return;
  }
  if (!Array.isArray(steps) || steps.length < 1 || steps.length > 50) {
    showError("script", { name: null, code: null, message: "脚本必须是 1～50 步的 JSON 数组（整份未装载）" });
    return;
  }
  const gen = expGen;
  setScriptBusy(true);
  clearError("script");
  try {
    // 直接发送原文，避免前端 JSON 数字精度丢失；整数校验完全沿用后端规则
    const data = await fetchScriptJson(`/api/experiments/${expId}/script`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: raw,
    });
    if (gen !== expGen) return;
    renderScript(data.script);
  } catch (e) {
    await handleScriptError(e, gen, "装载脚本失败：");
  } finally {
    if (gen === expGen && !expFrozen) setScriptBusy(false);
  }
}

async function doScriptAction(kind) {
  if (creating || expFrozen) return;
  // 推进 / 跳过 / 取消必须带上所见脚本 ID 与步号；本地无 active 视图时不发
  if (!scriptActive()) return;
  const payload = { script_id: scriptState.id, step: scriptState.step };
  const gen = expGen;
  setScriptBusy(true);
  clearError("script");
  try {
    const data = await fetchScriptJson(
      `/api/experiments/${expId}/script/${kind}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }
    );
    if (gen !== expGen) return;
    renderScript(data.script);
    // 推进真实执行了两栏操作：顺带刷新已提交值（查询本身不在两栏连接读值）
    if (kind === "advance") {
      refreshCommitted(gen, { allowAutoCreate: false }).catch(() => {});
    }
    // 推进一步但该步失败（ok=false）：脚本仍停在原步，记录里已有原始
    // 错误名 / 码；不在顶部弹错，错误以脚本记录与该栏日志为准。
  } catch (e) {
    await handleScriptError(
      e,
      gen,
      kind === "advance" ? "推进失败：" : kind === "skip" ? "跳过失败：" : "取消失败："
    );
  } finally {
    if (gen === expGen && !expFrozen) setScriptBusy(false);
  }
}

async function handleScriptError(e, gen, prefix) {
  if (e instanceof ExpDeleted) {
    if (gen === expGen) {
      freezeExperiment(`实验 ${expId} 已删除（410）：脚本操作未执行，旧实验操作已禁用，可点击「新建实验」继续。`);
    }
    return;
  }
  if (e instanceof ExpNotFound) {
    if (gen === expGen && !expFrozen) {
      await newExperiment();
    }
    return;
  }
  if (gen !== expGen || expFrozen) return;
  if (e instanceof ScriptConflict) {
    // 脚本 ID / 步号过期，或两栏存在事务 / 已有有效脚本：
    // 本地不猜测结果，立即以服务端状态为准重新核对
    showError("script", { name: null, code: null, message: prefix + e.message });
    refreshStatus(gen, { allowAutoCreate: false }).catch(() => {});
    return;
  }
  showError("script", { name: null, code: null, message: prefix + "请求失败：" + e.message });
}

function setCheckpointBusy(busy) {
  // 检查点请求进行中只禁用检查点按钮，甲、乙两栏照常可用
  $("ckpt-passive").disabled = busy;
  $("ckpt-truncate").disabled = busy;
}

function setCompensateBusy(busy) {
  compensateBusy = !!busy;
  syncCompensateControls();
}

function syncCompensateControls() {
  // 补偿控件：创建中 / 冻结 / 补偿请求在途一律禁用；
  // 脚本有效期间不锁（后端会以 409 如实拒绝，与检查点同理）
  const disabled = creating || expFrozen || compensateBusy;
  $("compensate-seq").disabled = disabled;
  $("compensate-btn").disabled = disabled;
}

async function doCompensate() {
  if (creating || expFrozen) return;
  const raw = $("compensate-seq").value.trim();
  if (!/^[0-9]+$/.test(raw)) {
    showError("timeline", { name: null, code: null, message: "请输入正整数记录序号" });
    return;
  }
  // seq 是时间线编号（小整数）；超出安全整数范围的输入本地拒绝，不做精度截断
  const seqBig = BigInt(raw);
  if (seqBig < 1n || seqBig > BigInt(Number.MAX_SAFE_INTEGER)) {
    showError("timeline", { name: null, code: null, message: "序号必须是正整数且在安全整数范围内" });
    return;
  }
  const gen = expGen;
  setCompensateBusy(true);
  clearError("timeline");
  try {
    const data = await fetchScriptJson(
      `/api/experiments/${expId}/timeline/compensate`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seq: Number(seqBig) }),
      }
    );
    if (gen !== expGen) return;
    // 无论成功失败都以响应中的时间线为准重渲染（失败时时间线未变）
    if (data.timeline) renderTimeline(data.timeline);
    if (!data.ok && data.error) {
      // 写锁冲突等 SQLite 错误：如实显示错误名 / 错误码，库与时间线未变
      showError("timeline", data.error);
    } else {
      $("compensate-seq").value = "";
      refreshCommitted(gen, { allowAutoCreate: false }).catch(() => {});
    }
  } catch (e) {
    if (e instanceof ExpDeleted) {
      if (gen === expGen) {
        freezeExperiment(`实验 ${expId} 已删除（410）：补偿未执行，旧实验操作已禁用，可点击「新建实验」继续。`);
      }
      return;
    }
    if (e instanceof ExpNotFound) {
      if (gen === expGen && !expFrozen) {
        await newExperiment();
      }
      return;
    }
    if (gen !== expGen || expFrozen) return;
    if (e instanceof ScriptConflict) {
      // 过期序号 / 补偿记录 / 脚本有效：本地不猜测，立即以服务端状态重核对
      showError("timeline", { name: null, code: null, message: "补偿被拒绝：" + e.message });
      refreshStatus(gen, { allowAutoCreate: false }).catch(() => {});
      return;
    }
    showError("timeline", { name: null, code: null, message: "补偿请求失败：" + e.message });
  } finally {
    // 冻结 / 已切换到新实验后，迟到响应不得改动当前控件状态
    if (gen === expGen && !expFrozen) {
      setCompensateBusy(false);
    }
  }
}

async function doCheckpoint(mode) {
  if (creating || expFrozen) return;
  const gen = expGen;
  setCheckpointBusy(true);
  clearError("ckpt");
  try {
    const data = await fetchJson(`/api/experiments/${expId}/checkpoint/${mode}`, {
      method: "POST",
    });
    if (gen !== expGen) return;
    renderCheckpoint(data.state);
    if (!data.ok && data.error) {
      showError("ckpt", data.error);
    }
  } catch (e) {
    if (e instanceof ExpDeleted) {
      if (gen === expGen) {
        freezeExperiment(`实验 ${expId} 已删除（410）：检查点未执行，旧实验操作已禁用，可点击「新建实验」继续。`);
      }
      return;
    }
    if (e instanceof ExpNotFound) {
      if (gen === expGen && !expFrozen) {
        await newExperiment();
      }
      return;
    }
    if (gen === expGen && !expFrozen) {
      showError("ckpt", { name: null, code: null, message: "请求失败：" + e.message });
    }
  } finally {
    if (gen === expGen && !expFrozen) {
      setCheckpointBusy(false);
    }
  }
}

async function deleteCurrentExperiment() {
  if (!expId || creating || expFrozen) return;
  const id = expId;
  const ok = window.confirm(
    `确认删除当前实验 ${id} ？\n\n` +
    "将等待进行中的请求完成，随后回滚未提交事务、关闭全部连接，" +
    "并删除数据库及 WAL、SHM 文件。\n" +
    "删除后本页旧实验操作将被禁用并停止轮询，可随时手动新建实验。"
  );
  if (!ok) return;
  const gen = expGen;
  $("delete-exp").disabled = true;
  clearError("exp");
  try {
    await fetchJson(`/api/experiments/${id}`, { method: "DELETE" });
    if (gen !== expGen) return;
    freezeExperiment(`实验 ${id} 已删除：在途请求已完成，未提交事务已回滚，连接已关闭，数据库与 WAL/SHM 文件已移除。可点击「新建实验」继续。`);
  } catch (e) {
    if (e instanceof ExpDeleted) {
      if (gen === expGen) {
        freezeExperiment(`实验 ${id} 已删除（410）：旧实验操作已禁用，可点击「新建实验」继续。`);
      }
      return;
    }
    if (e instanceof ExpNotFound) {
      if (gen === expGen && !expFrozen) {
        await newExperiment();
      }
      return;
    }
    if (gen === expGen && !expFrozen) {
      $("delete-exp").disabled = false;
      showError("exp", { name: null, code: null, message: "删除失败：" + e.message });
    }
  }
}

// 只读 SQL 工作台的状态文案
const WB_STATUS_LABELS = {
  running: "运行中",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
};

// 查询接口的 404 有两种含义：实验本身不存在（服务端重启，仅发生在不带
// 查询 ID 的提交接口）应沿用自动新建；带查询 ID 的查询 / 取消接口 404
// 表示查询 ID 已过期（被新查询替换 / 重启后内存清空），不应自动新建。
// expScoped=true 的调用（提交）把 404 当实验缺失；其余按旧查询处理。
class QueryNotFound extends Error {}

async function fetchWbJson(path, options, expScoped = false) {
  const resp = await fetch(path, options);
  if (resp.status === 410) throw new ExpDeleted();
  if (resp.status === 404) {
    throw expScoped ? new ExpNotFound() : new QueryNotFound();
  }
  let body = null;
  try {
    body = await resp.json();
  } catch (e) {
    body = {};
  }
  if (resp.status === 409) throw new ScriptConflict((body && body.error) || "conflict");
  if (!resp.ok) throw new Error("HTTP " + resp.status + (body && body.error ? "：" + body.error : ""));
  return body;
}

function stopWbPolling() {
  if (wbPollTimer !== null) {
    clearInterval(wbPollTimer);
    wbPollTimer = null;
  }
}

function syncWbControls() {
  // 创建中 / 冻结：全部禁用；有查询在途（running）时只能取消；
  // 请求在途（提交 / 取消）时两个按钮都禁用。无查询或已终结时可提交。
  const frozen = creating || expFrozen;
  const running = !!(wbQuery && wbQuery.status === "running");
  $("wb-run").disabled = frozen || wbBusy || running;
  $("wb-cancel").disabled = frozen || wbBusy || !running;
  $("wb-sql").disabled = frozen || wbBusy || running;
}

function renderWbResult(query) {
  const resultEl = $("wb-result");
  resultEl.innerHTML = "";
  if (query.status === "succeeded") {
    const table = document.createElement("table");
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const col of query.columns || []) {
      const th = document.createElement("th");
      th.textContent = col;
      headRow.appendChild(th);
    }
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    for (const row of query.rows || []) {
      const tr = document.createElement("tr");
      for (const cell of row) {
        const td = document.createElement("td");
        if (cell === null) {
          td.textContent = "NULL";
          td.classList.add("wb-null");
        } else {
          // 数字 / 字符串 / X'…' 十六进制 BLOB 均按文本无损渲染
          td.textContent = String(cell);
        }
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    resultEl.appendChild(table);
    if ((query.rows || []).length === 0) {
      const div = document.createElement("div");
      div.className = "wb-count";
      div.textContent = "结果集为空（0 行）";
      resultEl.appendChild(div);
    }
    if (query.truncated) {
      const div = document.createElement("div");
      div.className = "wb-truncated";
      div.textContent = "结果超过 100 行：仅显示前 100 行（truncated=true）。";
      resultEl.appendChild(div);
    }
    resultEl.hidden = false;
  } else {
    resultEl.hidden = true;
  }
}

function renderWb(query, adopt = false) {
  // 渲染工作台状态与结果。默认（轮询 / 取消）只接受当前最新查询，旧 ID
  // 的迟到响应丢弃；adopt=true 用于刚提交返回的查询——它本身就是新的
  // 当前查询（此时本地仍持有上一条查询），必须接受并替换。
  if (!query) return;
  if (!adopt && wbQuery && query.id !== wbQuery.id) return;
  wbQuery = query;
  const stateEl = $("wb-state");
  const label = WB_STATUS_LABELS[query.status] || query.status;
  stateEl.textContent = `查询 ${query.id}：${label}`;
  stateEl.className = "wb-state " + query.status;
  const countEl = $("wb-count");
  if (query.status === "succeeded") {
    const n = (query.rows || []).length;
    countEl.textContent = `列 ${(query.columns || []).length} · 返回 ${n} 行` +
      (query.truncated ? "（已截断）" : "");
  } else if (query.status === "failed" && query.error) {
    countEl.textContent = query.error.name
      ? `${query.error.name}（错误码 ${query.error.code}）`
      : "";
  } else {
    countEl.textContent = "";
  }
  renderWbResult(query);
  if (query.status === "running") {
    startWbPolling();
  } else {
    stopWbPolling();
    if (query.status === "failed" && query.error) {
      showError("wb", query.error);
    } else {
      clearError("wb");
    }
  }
  syncWbControls();
}

function startWbPolling() {
  // 单一查询轮询定时器（700ms）；终态 / 新提交 / 冻结时停止
  if (wbPollTimer !== null) return;
  wbPollTimer = setInterval(() => {
    if (creating || expFrozen || !wbQuery || wbQuery.status !== "running") {
      stopWbPolling();
      return;
    }
    pollWbQuery().catch(() => {});
  }, 700);
}

async function pollWbQuery() {
  if (!expId || creating || expFrozen || !wbQuery) return;
  const id = wbQuery.id;
  const gen = expGen;
  try {
    const data = await fetchWbJson(`/api/experiments/${expId}/query/${id}`);
    if (gen !== expGen || expFrozen) return;
    renderWb(data.query);
  } catch (e) {
    await handleWbError(e, gen, "");
  }
}

async function doWbSubmit() {
  if (creating || expFrozen) return;
  const sql = $("wb-sql").value;
  if (!sql.trim()) {
    showError("wb", { name: null, code: null, message: "请输入 1～4000 字符的 SELECT / WITH 只读查询" });
    return;
  }
  if (sql.length > 4000) {
    showError("wb", { name: null, code: null, message: `SQL 长度 ${sql.length} 超过 4000 字符上限` });
    return;
  }
  const gen = expGen;
  stopWbPolling();
  wbBusy = true;
  clearError("wb");
  syncWbControls();
  try {
    const data = await fetchWbJson(`/api/experiments/${expId}/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql }),
    }, true);
    if (gen !== expGen || expFrozen) return;
    renderWb(data.query, true); // 新提交：adopt 为最新查询，running → 启动轮询
  } catch (e) {
    await handleWbError(e, gen, "提交查询失败：");
  } finally {
    if (gen === expGen && !expFrozen) {
      wbBusy = false;
      syncWbControls();
    }
  }
}

async function doWbCancel() {
  if (creating || expFrozen || !wbQuery) return;
  if (wbQuery.status !== "running") return;
  const id = wbQuery.id;
  const gen = expGen;
  wbBusy = true;
  clearError("wb");
  syncWbControls();
  try {
    // 取消请求在服务端等到连接关闭、终态发布后才返回；返回即终态
    const data = await fetchWbJson(`/api/experiments/${expId}/query/${id}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (gen !== expGen || expFrozen) return;
    stopWbPolling();
    renderWb(data.query);
  } catch (e) {
    await handleWbError(e, gen, "取消查询失败：");
  } finally {
    if (gen === expGen && !expFrozen) {
      wbBusy = false;
      syncWbControls();
    }
  }
}

async function handleWbError(e, gen, prefix) {
  if (e instanceof ExpDeleted) {
    if (gen === expGen) {
      freezeExperiment(`实验 ${expId} 已删除（410）：SQL 工作台已冻结，可点击「新建实验」继续。`);
    }
    return;
  }
  if (e instanceof ExpNotFound) {
    // 实验本身不存在（服务端重启）：沿用统一自动新建；新实验工作台为空
    if (gen === expGen && !expFrozen) {
      await newExperiment();
    }
    return;
  }
  if (gen !== expGen || expFrozen) return;
  if (e instanceof QueryNotFound) {
    // 查询 ID 已过期（被新查询替换 / 重启后内存清空）：停止轮询，
    // 按「当前无查询」复位，绝不自动新建实验，也不影响新查询
    stopWbPolling();
    wbQuery = null;
    wbBusy = false;
    $("wb-state").textContent = "当前查询已失效（旧查询 ID，可能已被新查询替换）";
    $("wb-state").className = "wb-state cancelled";
    $("wb-count").textContent = "";
    $("wb-result").hidden = true;
    syncWbControls();
    return;
  }
  if (e instanceof ScriptConflict) {
    // running 中再提交：409，提示并继续轮询原查询（若在运行中）
    showError("wb", { name: null, code: null, message: prefix + e.message });
    if (wbQuery && wbQuery.status === "running") startWbPolling();
    return;
  }
  wbBusy = false;
  syncWbControls();
  showError("wb", { name: null, code: null, message: prefix + "请求失败：" + e.message });
}

async function init() {
  bindPanel("a");
  bindPanel("b");
  $("new-exp").addEventListener("click", () => {
    newExperiment().catch(() => {});
  });
  $("delete-exp").addEventListener("click", () => {
    deleteCurrentExperiment().catch(() => {});
  });
  $("refresh-committed").addEventListener("click", () => {
    if (creating || expFrozen) return;
    refreshCommitted(expGen).catch(() => {});
  });
  $("ckpt-passive").addEventListener("click", () => {
    doCheckpoint("PASSIVE").catch(() => {});
  });
  $("ckpt-truncate").addEventListener("click", () => {
    doCheckpoint("TRUNCATE").catch(() => {});
  });
  $("compensate-btn").addEventListener("click", () => {
    doCompensate().catch(() => {});
  });
  $("script-load").addEventListener("click", () => {
    doScriptLoad().catch(() => {});
  });
  $("script-example").addEventListener("click", () => {
    if (creating || expFrozen || scriptActive()) return;
    $("script-input").value = exampleScriptText();
    clearError("script");
  });
  $("script-advance").addEventListener("click", () => {
    doScriptAction("advance").catch(() => {});
  });
  $("script-skip").addEventListener("click", () => {
    doScriptAction("skip").catch(() => {});
  });
  $("script-cancel").addEventListener("click", () => {
    doScriptAction("cancel").catch(() => {});
  });
  $("wb-run").addEventListener("click", () => {
    doWbSubmit().catch(() => {});
  });
  $("wb-cancel").addEventListener("click", () => {
    doWbCancel().catch(() => {});
  });
  // 初始（?exp= 或自动新建前）控件可用性：无查询时只可提交、不可取消
  syncWbControls();

  const params = new URLSearchParams(location.search);
  const existing = params.get("exp");
  if (existing) {
    const gen = expGen;
    expId = existing;
    $("exp-id").textContent = existing;
    // 404 时 refreshStatus 会自动新建（expId 改变并已自行启动轮询）；
    // 410 时会冻结视图。这两种情况都不再执行后面的初始化。
    await refreshStatus(gen);
    if (expFrozen || expId !== existing) return;
    await refreshCommitted(gen);
    if (expFrozen || expId !== existing) return;
    startPolling();
  } else {
    await newExperiment();
  }
}

init();
