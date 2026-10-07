"use strict";

// 验证「补偿末条提交」的前端行为：
// 正整数序号校验（非法输入不发请求）、请求在途禁用、成功后渲染系统补偿记录
// 并刷新已提交值、SQLite 错误如实显示、409（过期 / 补偿记录 / 脚本有效）
// 本地不猜测并立即以服务端状态重核对、新建进行中禁用、删除后冻结、
// 旧实验迟到响应不影响新实验、410 冻结。
// 响应顺序完全可控，不依赖真实时间与网络。
// 运行：node tests/compensate.test.js

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
    throw new Error(`${message}（期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)}）`);
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

const COMPENSATE_OK = {
  ok: true,
  error: null,
  record: { seq: 2, side: "system", before: 112, after: 100, deltas: [-7, -5], compensates: 1 },
  timeline: [
    { seq: 1, side: "a", before: 100, after: 112, deltas: [5, 7] },
    { seq: 2, side: "system", before: 112, after: 100, deltas: [-7, -5], compensates: 1 },
  ],
};

function timelineTexts(ctx) {
  return ctx.el("timeline-list")._children.map((c) => c.textContent);
}

async function main() {
  await test("存活页控件可用；非法序号不发请求并显示错误", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    assertEq(ctx.el("compensate-seq").disabled, false, "序号输入可用");
    assertEq(ctx.el("compensate-btn").disabled, false, "补偿按钮可用");

    for (const bad of ["", "abc", "0", "-1", "1.5", "99999999999999999999"]) {
      const before = ctx.calls.length;
      ctx.el("compensate-seq").value = bad;
      ctx.el("compensate-btn").click();
      await flush();
      assertEq(ctx.calls.length, before, `非法输入 ${JSON.stringify(bad)} 不得发请求`);
      assertEq(ctx.el("error-timeline").hidden, false, `非法输入 ${JSON.stringify(bad)} 显示错误`);
    }
  });

  await test("补偿成功：请求体正整数、在途禁用、渲染系统记录并刷新已提交值", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("compensate-seq").value = "1";
    ctx.el("compensate-btn").click();
    await flush();

    const call = ctx.findCall("/OLD/timeline/compensate", "POST");
    assert(!!call, "应发出补偿请求");
    assertEq(call.body, '{"seq":1}', "请求体为正整数 seq");

    // 在途：输入与按钮禁用
    assertEq(ctx.el("compensate-seq").disabled, true, "在途时序号输入禁用");
    assertEq(ctx.el("compensate-btn").disabled, true, "在途时补偿按钮禁用");

    call.settled = true;
    call.resolve({ status: 200, ok: true, json: async () => COMPENSATE_OK });
    await flush();

    const texts = timelineTexts(ctx);
    assertEq(texts.length, 2, "时间线渲染两条记录");
    assertEq(texts[0], "#1  甲（A）  100 → 112  增量 [5, 7]", "原记录保留且字段不变");
    assertEq(texts[1], "#2  系统  112 → 100  增量 [-7, -5]  补偿 #1", "系统补偿记录含原 seq");
    assertEq(ctx.el("compensate-seq").value, "", "成功后清空序号输入");
    assertEq(ctx.el("error-timeline").hidden, true, "成功不显示错误");

    // 成功后刷新已提交值
    const committedCall = ctx.findCall("/OLD/committed", "GET");
    assert(!!committedCall, "成功后应刷新已提交值");
    committedCall.settled = true;
    committedCall.resolve({ status: 200, ok: true, json: async () => ({ committed: 100 }) });
    await flush();
    assertEq(String(ctx.el("committed").textContent), "100", "已提交值更新为 100");

    assertEq(ctx.el("compensate-seq").disabled, false, "完成后序号输入恢复");
    assertEq(ctx.el("compensate-btn").disabled, false, "完成后补偿按钮恢复");
  });

  await test("写锁冲突：如实显示 SQLite 错误名 / 错误码", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("compensate-seq").value = "1";
    ctx.el("compensate-btn").click();
    await flush();
    ctx.settle("/OLD/timeline/compensate", 200, {
      ok: false,
      error: { name: "SQLITE_BUSY", code: 5, message: "database is locked" },
      timeline: [{ seq: 1, side: "a", before: 100, after: 105, deltas: [5] }],
    }, "POST");
    await flush();
    assert(
      ctx.el("error-timeline").textContent.includes("SQLITE_BUSY（错误码 5）"),
      "错误框显示错误名与错误码"
    );
    assertEq(timelineTexts(ctx).length, 1, "失败时按响应时间线渲染（未变）");
    assertEq(ctx.el("compensate-btn").disabled, false, "失败后按钮恢复");
  });

  await test("409（过期 / 补偿记录 / 脚本有效）：显示并以服务端状态重核对", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("compensate-seq").value = "1";
    ctx.el("compensate-btn").click();
    await flush();
    ctx.settle("/OLD/timeline/compensate", 409, {
      error: "stale target: only the last normal timeline record can be compensated",
    }, "POST");
    await flush();
    assert(
      ctx.el("error-timeline").textContent.includes("补偿被拒绝：stale target"),
      "409 显示拒绝原因"
    );
    // 立即重新拉取状态重核对
    const statusCall = ctx.findCall("/OLD/status", "GET");
    assert(!!statusCall, "409 后应立即重新拉取状态");
    statusCall.settled = true;
    statusCall.resolve({
      status: 200,
      ok: true,
      json: async () => ({
        id: "OLD",
        a: { in_transaction: false, readonly: false, last_read: null, log: [] },
        b: { in_transaction: false, readonly: false, last_read: null, log: [] },
        checkpoint: { last: null, log: [] },
        timeline: [
          { seq: 1, side: "a", before: 100, after: 112, deltas: [5, 7] },
          { seq: 2, side: "b", before: 112, after: 115, deltas: [3] },
        ],
        script: null,
      }),
    });
    await flush();
    const texts = timelineTexts(ctx);
    assertEq(texts.length, 2, "重核对后按服务端时间线渲染");
    assertEq(texts[1], "#2  乙（B）  112 → 115  增量 [3]", "服务端末条已是新普通提交");
    assertEq(ctx.el("compensate-btn").disabled, false, "409 后按钮恢复");
  });

  await test("新建进行中禁用补偿控件，成功后恢复", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("compensate-seq").value = "1";
    ctx.el("new-exp").click();
    await flush();
    assertEq(ctx.el("compensate-seq").disabled, true, "新建中序号输入禁用");
    assertEq(ctx.el("compensate-btn").disabled, true, "新建中补偿按钮禁用");
    const before = ctx.calls.length;
    ctx.el("compensate-btn").click();
    await flush();
    assertEq(ctx.calls.length, before, "新建中点击不发请求");

    await settleNewCreated(ctx);
    assertEq(ctx.el("compensate-seq").disabled, false, "新建成功后序号输入恢复");
    assertEq(ctx.el("compensate-btn").disabled, false, "新建成功后补偿按钮恢复");
    assertEq(ctx.el("compensate-seq").value, "", "新建后序号输入已清空");
  });

  await test("删除后冻结：补偿控件禁用且点击不发请求", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("delete-exp").click();
    await flush();
    ctx.settle("/api/experiments/OLD", 200, { id: "OLD", deleted: true }, "DELETE");
    await flush();
    assert(ctx.app().expFrozen, "删除后应冻结");
    assertEq(ctx.el("compensate-seq").disabled, true, "冻结后序号输入禁用");
    assertEq(ctx.el("compensate-btn").disabled, true, "冻结后补偿按钮禁用");
    const before = ctx.calls.length;
    ctx.el("compensate-btn").click();
    await flush();
    assertEq(ctx.calls.length, before, "冻结后点击不发请求");
  });

  await test("旧实验的迟到补偿响应不影响新实验", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("compensate-seq").value = "1";
    ctx.el("compensate-btn").click();
    await flush();
    assert(!!ctx.findCall("/OLD/timeline/compensate", "POST"), "OLD 补偿请求在途");

    // 补偿请求在途期间手动新建 NEW 并完成
    ctx.el("new-exp").click();
    await flush();
    await settleNewCreated(ctx);
    assertEq(ctx.app().expId, "NEW", "已切换到新实验");

    // 迟到放行 OLD 的补偿响应：不得污染 NEW 的展示与控件
    ctx.settle("/OLD/timeline/compensate", 200, COMPENSATE_OK, "POST");
    await flush();
    const texts = timelineTexts(ctx);
    assertEq(texts.length, 1, "NEW 时间线不被旧响应污染");
    assertEq(texts[0], "暂无已提交的写入事务", "NEW 时间线保持空态");
    assertEq(ctx.el("error-timeline").hidden, true, "旧响应不显示错误");
    assertEq(ctx.el("compensate-btn").disabled, false, "NEW 补偿按钮保持可用");
    assertEq(ctx.el("compensate-seq").disabled, false, "NEW 序号输入保持可用");
  });

  await test("补偿请求返回 410：冻结且不自动新建", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("compensate-seq").value = "1";
    ctx.el("compensate-btn").click();
    await flush();
    ctx.settle("/OLD/timeline/compensate", 410, { error: "experiment gone" }, "POST");
    await flush();
    assert(ctx.app().expFrozen, "410 后应冻结");
    assert(!ctx.findCall("/api/experiments", "POST"), "410 不自动新建");
    assertEq(ctx.el("compensate-btn").disabled, true, "410 后补偿按钮禁用");
  });

  console.log(`\n${passed} 通过，${failed} 失败`);
  if (failed > 0) process.exit(1);
}

main();
