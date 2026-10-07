"use strict";

// 验证「只读 SQL 工作台」的前端行为：
// 提交校验（空 / 超长不发请求）、提交在途禁用、立即渲染 running 并轮询、
// 成功渲染列名与前 100 行表格（NULL / 大整数文本 / 截断提示）、失败显示
// 原始错误名 / 码并恢复提交、取消中断后轮询停止、运行中只能取消、
// 查询 ID 404（旧 ID）不自动新建只复位、实验 404 沿用自动新建、
// 410 冻结、新建进行中禁用与新建后复位、删除冻结停止轮询、旧 ID 迟到
// 响应不覆盖新查询。响应顺序完全可控，不依赖真实时间。
// 运行：node tests/workbench.test.js

const {
  loadApp,
  flush,
  bootOldLive,
  settleNewCreated,
} = require("./harness");

let passed = 0;
let failed = 0;

function assert(cond, message) {
  if (!cond) throw new Error(message);
}
function assertEq(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(`${message}（期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)})`);
  }
}
async function test(name, fn) {
  try {
    await fn();
    passed++;
    console.log("  ✓ " + name);
  } catch (e) {
    failed++;
    console.error("  ✗ " + name);
    console.error("      " + e.message);
  }
}

function findExact(ctx, path, method) {
  return ctx.calls.find(
    (c) => !c.settled && c.path === path && (!method || c.method === method)
  );
}
function countTimers(ctx) {
  return ctx.intervalCallbacks().length;
}

async function submitQuery(ctx, sql) {
  ctx.el("wb-sql").value = sql;
  ctx.el("wb-run").click();
  await flush();
  return findExact(ctx, "/api/experiments/OLD/query", "POST");
}

const Q = (id, status, extra = {}) => ({ id, status, sql: "select 1", ...extra });

async function main() {
  await test("初始：可提交、不可取消；空 / 超长 SQL 不发请求", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    assertEq(ctx.el("wb-run").disabled, false, "提交可用");
    assertEq(ctx.el("wb-cancel").disabled, true, "无运行查询时取消禁用");
    assertEq(ctx.el("wb-sql").disabled, false, "SQL 框可用");

    const before = ctx.calls.length;
    ctx.el("wb-sql").value = "";
    ctx.el("wb-run").click();
    await flush();
    ctx.el("wb-sql").value = "   \n\t ";
    ctx.el("wb-run").click();
    await flush();
    ctx.el("wb-sql").value = "s".repeat(4001);
    ctx.el("wb-run").click();
    await flush();
    assertEq(ctx.calls.length, before, "非法 SQL 不发请求");
    assertEq(ctx.el("error-wb").hidden, false, "显示校验错误");
    assertEq(ctx.el("wb-run").disabled, false, "校验失败后仍可再提交");
  });

  await test("提交：请求体正确、在途禁用；running 立即渲染并启动轮询", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    const timers0 = countTimers(ctx);
    const call = await submitQuery(ctx, "select val from kv");
    assert(!!call, "应发出 POST /OLD/query");
    assertEq(call.body, JSON.stringify({ sql: "select val from kv" }), "请求体为 {sql}");
    assertEq(ctx.el("wb-run").disabled, true, "在途提交禁用");
    assertEq(ctx.el("wb-cancel").disabled, true, "在途取消禁用");
    assertEq(ctx.el("wb-sql").disabled, true, "在途 SQL 框禁用");

    call.settled = true;
    call.resolve({ status: 200, ok: true, json: async () => ({ query: Q("q1", "running") }) });
    await flush();
    assert(ctx.el("wb-state").textContent.includes("q1"), "状态含查询 ID");
    assert(ctx.el("wb-state").className.includes("running"), "running 样式");
    assertEq(ctx.el("wb-run").disabled, true, "running 时提交禁用");
    assertEq(ctx.el("wb-cancel").disabled, false, "running 时可取消");
    assertEq(countTimers(ctx), timers0 + 1, "启动一个查询轮询定时器");
  });

  await test("轮询成功：渲染列名 / 行 / NULL / 大整数 / 截断并停轮询", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    const call = await submitQuery(ctx, "select * from kv");
    call.settled = true;
    call.resolve({ status: 200, ok: true, json: async () => ({ query: Q("q1", "running") }) });
    await flush();

    ctx.tickTimers();
    const getCall = findExact(ctx, "/api/experiments/OLD/query/q1", "GET");
    assert(!!getCall, "轮询发出 GET 查询结果");
    getCall.settled = true;
    getCall.resolve({
      status: 200,
      ok: true,
      json: async () => ({
        query: Q("q1", "succeeded", {
          columns: ["val", "n", "big"],
          rows: [[100, null, "9007199254741093"]],
          truncated: false,
        }),
      }),
    });
    await flush();

    assert(ctx.el("wb-state").className.includes("succeeded"), "成功样式");
    assert(ctx.el("wb-count").textContent.includes("返回 1 行"), "行数统计");
    const result = ctx.el("wb-result");
    assertEq(result.hidden, false, "结果区显示");
    const table = result._children[0];
    const thead = table._children[0];
    const ths = thead._children[0]._children;
    assertEq(ths.length, 3, "三列表头");
    assertEq(ths.map((t) => t.textContent).join(","), "val,n,big", "列名");
    const tbody = table._children[1];
    const tds = tbody._children[0]._children;
    assertEq(tds.map((t) => t.textContent).join("|"), "100|NULL|9007199254741093", "单元格文本");
    assert(tds[1].classList.contains("wb-null"), "NULL 有样式类");
    assertEq(ctx.el("wb-run").disabled, false, "成功后可再提交");
    assertEq(ctx.el("wb-cancel").disabled, true, "成功后取消禁用");
    assertEq(ctx.el("error-wb").hidden, true, "成功无错误");

    // 终态后不再轮询
    const callsBefore = ctx.calls.length;
    ctx.tickTimers();
    await flush();
    assertEq(
      ctx.calls.filter((c) => c.path === "/api/experiments/OLD/query/q1" && !c.settled).length,
      0,
      "终态后不再发 GET"
    );
    assert(callsBefore >= 0);
  });

  await test("截断：超过 100 行显示提示", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    const call = await submitQuery(ctx, "select x from c");
    call.settled = true;
    call.resolve({ status: 200, ok: true, json: async () => ({ query: Q("q1", "running") }) });
    await flush();
    ctx.tickTimers();
    const getCall = findExact(ctx, "/api/experiments/OLD/query/q1", "GET");
    getCall.settled = true;
    getCall.resolve({
      status: 200,
      ok: true,
      json: async () => ({
        query: Q("q1", "succeeded", {
          columns: ["x"],
          rows: Array.from({ length: 100 }, (_, i) => [i + 1]),
          truncated: true,
        }),
      }),
    });
    await flush();
    assert(ctx.el("wb-count").textContent.includes("已截断"), "计数含截断");
    const result = ctx.el("wb-result");
    const truncatedNote = result._children.find(
      (el) => el.className === "wb-truncated"
    );
    assert(!!truncatedNote && truncatedNote.textContent.includes("前 100 行"), "显示截断提示");
  });

  await test("失败：显示原始错误名 / 码，恢复提交且不轮询", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    const call = await submitQuery(ctx, "select * from nope");
    call.settled = true;
    call.resolve({ status: 200, ok: true, json: async () => ({ query: Q("q1", "running") }) });
    await flush();
    ctx.tickTimers();
    const getCall = findExact(ctx, "/api/experiments/OLD/query/q1", "GET");
    getCall.settled = true;
    getCall.resolve({
      status: 200,
      ok: true,
      json: async () => ({
        query: Q("q1", "failed", {
          error: { name: "SQLITE_ERROR", code: 1, message: "no such table: nope" },
        }),
      }),
    });
    await flush();
    assert(ctx.el("wb-state").className.includes("failed"), "失败样式");
    assert(
      ctx.el("error-wb").textContent.includes("SQLITE_ERROR（错误码 1）"),
      "错误框显示错误名 / 码"
    );
    assertEq(ctx.el("wb-result").hidden, true, "失败不渲染结果表");
    assertEq(ctx.el("wb-run").disabled, false, "失败后可再提交");
    assertEq(ctx.el("wb-cancel").disabled, true, "失败后取消禁用");
  });

  await test("取消：中断后渲染已取消、停止轮询、恢复提交", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    const timers0 = countTimers(ctx);
    const call = await submitQuery(ctx, "with recursive ...");
    call.settled = true;
    call.resolve({ status: 200, ok: true, json: async () => ({ query: Q("q1", "running") }) });
    await flush();
    assertEq(countTimers(ctx), timers0 + 1, "轮询中");

    ctx.el("wb-cancel").click();
    await flush();
    const cancelCall = findExact(ctx, "/api/experiments/OLD/query/q1/cancel", "POST");
    assert(!!cancelCall, "发出取消请求");
    assertEq(cancelCall.body, "{}", "取消请求体为空 JSON");
    assertEq(ctx.el("wb-cancel").disabled, true, "取消在途时禁用");
    assertEq(ctx.el("wb-run").disabled, true, "取消在途时提交禁用");

    cancelCall.settled = true;
    cancelCall.resolve({
      status: 200,
      ok: true,
      json: async () => ({ query: Q("q1", "cancelled") }),
    });
    await flush();
    assert(ctx.el("wb-state").className.includes("cancelled"), "已取消样式");
    assertEq(ctx.el("wb-cancel").disabled, true, "取消后取消禁用");
    assertEq(ctx.el("wb-run").disabled, false, "取消后可再提交");
    assertEq(countTimers(ctx), timers0, "轮询已停止");
    assertEq(ctx.el("error-wb").hidden, true, "取消不是错误");
  });

  await test("查询 ID 404（旧 ID）：复位、不自动新建、不影响后续", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    const call = await submitQuery(ctx, "select 1");
    call.settled = true;
    call.resolve({ status: 200, ok: true, json: async () => ({ query: Q("old", "running") }) });
    await flush();
    ctx.tickTimers();
    const getCall = findExact(ctx, "/api/experiments/OLD/query/old", "GET");
    getCall.settled = true;
    getCall.resolve({ status: 404, ok: false, json: async () => ({ error: "query not found" }) });
    await flush();
    assert(ctx.el("wb-state").textContent.includes("已失效"), "提示查询已失效");
    assertEq(ctx.el("wb-run").disabled, false, "可再提交");
    assertEq(ctx.el("wb-cancel").disabled, true, "无运行查询");
    assertEq(
      ctx.calls.filter((c) => c.path === "/api/experiments" && c.method === "POST").length,
      0,
      "查询 404 不自动新建实验"
    );
  });

  await test("实验 404（服务端重启）：沿用自动新建", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    const call = await submitQuery(ctx, "select 1");
    call.settled = true;
    call.resolve({ status: 404, ok: false, json: async () => ({ error: "experiment not found" }) });
    await flush();
    const createCall = findExact(ctx, "/api/experiments", "POST");
    assert(!!createCall, "实验 404 触发自动新建");
    await settleNewCreated(ctx);
    assertEq(ctx.app().expId, "NEW", "切换到新实验");
    assertEq(ctx.el("wb-state").textContent, "尚未提交查询", "新实验工作台已复位");
    assertEq(ctx.el("wb-result").hidden, true, "新实验无旧结果");
    assertEq(ctx.el("wb-run").disabled, false, "新实验可提交");
  });

  await test("410：冻结工作台并停止轮询", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    const call = await submitQuery(ctx, "select 1");
    call.settled = true;
    call.resolve({ status: 200, ok: true, json: async () => ({ query: Q("q1", "running") }) });
    await flush();
    assertEq(countTimers(ctx), 3, "轮询启动（状态+已提交值+查询）");
    ctx.tickTimers();
    const getCall = findExact(ctx, "/api/experiments/OLD/query/q1", "GET");
    getCall.settled = true;
    getCall.resolve({ status: 410, ok: false, json: async () => ({ error: "gone" }) });
    await flush();
    assert(ctx.app().expFrozen, "410 后冻结");
    assertEq(ctx.el("wb-run").disabled, true, "冻结后提交禁用");
    assertEq(ctx.el("wb-cancel").disabled, true, "冻结后取消禁用");
    assertEq(ctx.el("wb-sql").disabled, true, "冻结后 SQL 框禁用");
    assertEq(countTimers(ctx), 0, "全部轮询停止");
  });

  await test("新建进行中禁用工作台；成功后复位（SQL 文本保留）", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("wb-sql").value = "select val from kv";
    ctx.el("new-exp").click();
    await flush();
    assertEq(ctx.el("wb-run").disabled, true, "新建中提交禁用");
    assertEq(ctx.el("wb-cancel").disabled, true, "新建中取消禁用");
    const before = ctx.calls.length;
    ctx.el("wb-run").click();
    await flush();
    assertEq(ctx.calls.length, before, "新建中点击不发请求");

    await settleNewCreated(ctx);
    assertEq(ctx.el("wb-run").disabled, false, "新建后可提交");
    assertEq(ctx.el("wb-state").textContent, "尚未提交查询", "状态复位");
    assertEq(ctx.el("wb-sql").value, "select val from kv", "SQL 文本保留以便复用");
  });

  await test("删除冻结：停止查询轮询并禁用控件", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    const call = await submitQuery(ctx, "select 1");
    call.settled = true;
    call.resolve({ status: 200, ok: true, json: async () => ({ query: Q("q1", "running") }) });
    await flush();
    ctx.el("delete-exp").click();
    await flush();
    ctx.settle("/api/experiments/OLD", 200, { id: "OLD", deleted: true }, "DELETE");
    await flush();
    assert(ctx.app().expFrozen, "删除后冻结");
    assertEq(ctx.el("wb-run").disabled, true, "删除后提交禁用");
    assertEq(ctx.el("wb-cancel").disabled, true, "删除后取消禁用");
    assertEq(countTimers(ctx), 0, "查询轮询随冻结停止");
  });

  await test("旧查询 ID 的迟到响应不覆盖新查询", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    // q1 running → 轮询
    let call = await submitQuery(ctx, "select 1");
    call.settled = true;
    call.resolve({ status: 200, ok: true, json: async () => ({ query: Q("q1", "running") }) });
    await flush();
    ctx.tickTimers();
    let getCall = findExact(ctx, "/api/experiments/OLD/query/q1", "GET");
    getCall.settled = true;
    getCall.resolve({ status: 200, ok: true, json: async () => ({ query: Q("q1", "succeeded", {
      columns: ["x"], rows: [[1]], truncated: false,
    }) }) });
    await flush();
    // q2 running
    call = await submitQuery(ctx, "select 2");
    call.settled = true;
    call.resolve({ status: 200, ok: true, json: async () => ({ query: Q("q2", "running") }) });
    await flush();
    ctx.tickTimers();
    getCall = findExact(ctx, "/api/experiments/OLD/query/q2", "GET");
    assert(!!getCall, "轮询新查询 q2");
    // 迟到 / 错配响应：内容是 q1 的成功结果，必须被丢弃
    getCall.settled = true;
    getCall.resolve({ status: 200, ok: true, json: async () => ({ query: Q("q1", "succeeded", {
      columns: ["old"], rows: [[999]], truncated: false,
    }) }) });
    await flush();
    assertEq(ctx.el("wb-state").textContent.includes("q2"), true, "仍显示 q2");
    assertEq(ctx.el("wb-cancel").disabled, false, "q2 仍在运行、可取消");
    // 下一轮仍轮询 q2
    ctx.tickTimers();
    assert(!!findExact(ctx, "/api/experiments/OLD/query/q2", "GET"), "错配响应后继续轮询 q2");
  });

  console.log(`\n${passed} 通过，${failed} 失败`);
  if (failed > 0) process.exit(1);
}

main();
