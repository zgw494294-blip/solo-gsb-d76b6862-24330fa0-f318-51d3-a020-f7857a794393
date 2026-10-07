"""后端补偿（compensate）验证（无需 Flask）：用最小 stub 导入 app.py。

通过 stub 的最小路由分发驱动 **app.py 注册的实际路由**（真实经过
admit_experiment 准入门控、script_lock / timeline_lock 与真实 SQLite，
不另写模拟准入逻辑），覆盖补偿时间线末条普通提交的全部约定：

- 基本流程：甲 100 +5 +7 提交后补偿 #1，值恢复 100，追加
  side=system、112→100、增量 [-7,-5]、compensates=1 的记录；原记录保留；
- 已有读快照仍见旧值；两栏事务、只读模式、最近显式读值不受影响；
- 过期目标 / 补偿记录 / 空时间线 / 有效脚本一律 409；
- 写锁冲突立即返回 SQLITE_BUSY(5)，失败不改库也不改时间线；
- 并发同一 seq 最多成功一次；与普通提交交错时重新核对目标；
- 大整数无损；普通记录字段不变；删除准入（410）与未知实验（404）；
- 非法 seq 一律 400。
"""
import sys, types, json, os, re, tempfile, threading

# ---- 最小 Flask stub：路由注册 / 请求体注入 / 响应包装（仅 Web 框架管道） ----
flask = types.ModuleType("flask")


class _Resp:
    """仿 Flask 响应：视图返回 jsonify(...) 或 (jsonify(...), status)。"""

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def get_json(self):
        return self._payload


def _jsonify(*args, **kwargs):
    if args and kwargs:
        raise TypeError("jsonify: cannot mix positional and keyword")
    if len(args) == 1:
        payload = args[0]
    elif args:
        payload = list(args)
    else:
        payload = kwargs
    return _Resp(payload)


def _compile_path(path):
    # /api/experiments/<exp_id>/conn/<side>/op → 命名分组正则
    return re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", path) + "$")


class _App:
    def __init__(self, name):
        self.routes = []  # [(method, regex, view_fn)]，按注册顺序匹配

    def _reg(self, method, path):
        def deco(fn):
            self.routes.append((method, _compile_path(path), fn))
            return fn
        return deco

    def get(self, path, **k):
        return self._reg("GET", path)

    def post(self, path, **k):
        return self._reg("POST", path)

    def delete(self, path, **k):
        return self._reg("DELETE", path)

    def run(self, **k):
        pass


class _Req(threading.local):
    # 请求体按线程隔离：并发交错测试里各线程看到各自的 JSON body
    def __init__(self):
        self._json = None

    def get_json(self, silent=False):
        return self._json


flask.Flask = _App
flask.jsonify = _jsonify
flask.render_template = lambda *a, **k: "html"
flask.request = _Req()
sys.modules["flask"] = flask


class TestClient:
    """最小路由分发：直接调用 app.py 注册的实际视图函数。

    请求真实经过 admit_experiment 准入门控与全部实验级锁；
    这里只替代 Flask 的 URL 匹配与请求 / 响应包装，不另写模拟准入逻辑。
    """

    def __init__(self, app):
        self.app = app

    def open(self, method, path, json_body=None):
        flask.request._json = json_body
        try:
            for registered_method, rx, fn in self.app.routes:
                if registered_method != method:
                    continue
                match = rx.match(path)
                if match:
                    return self._normalize(fn(**match.groupdict()))
            raise AssertionError("no route: %s %s" % (method, path))
        finally:
            flask.request._json = None

    @staticmethod
    def _normalize(rv):
        if isinstance(rv, tuple):
            resp, status = rv[0], rv[1]
            resp.status_code = status
            return resp
        if isinstance(rv, _Resp):
            return rv
        return _Resp(rv)

    def get(self, path):
        return self.open("GET", path)

    def post(self, path, json=None):
        return self.open("POST", path, json)

    def delete(self, path):
        return self.open("DELETE", path)


_tmp = tempfile.mkdtemp()
os.environ["LAB_DATA_DIR"] = _tmp
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
import app as A

fails = []


def check(name, cond, extra=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


client = TestClient(A.app)
ROUTE_TIMEOUT = 5.0  # 正常路由调用为毫秒级；超时即视为「卡住」回归


def route_call(fn, *args, **kwargs):
    """在守护线程中执行路由调用并限时等待；卡住 / 异常变成明确的失败。"""
    box = {}

    def runner():
        try:
            box["resp"] = fn(*args, **kwargs)
        except Exception as exc:
            box["exc"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(ROUTE_TIMEOUT)
    if t.is_alive():
        return None, None, True
    if "exc" in box:
        return None, box["exc"], False
    return box["resp"], None, False


def r_post(path, json_body=None):
    return route_call(client.post, path, json_body)


def r_get(path):
    return route_call(client.get, path)


def r_delete(path):
    return route_call(client.delete, path)


def create_exp():
    resp, exc, stuck = r_post("/api/experiments")
    assert not stuck and exc is None and resp.status_code == 200
    return resp.get_json()["id"]


def op(exp_id, side, body):
    resp, exc, stuck = r_post("/api/experiments/%s/conn/%s/op" % (exp_id, side), body)
    assert not stuck and exc is None and resp.status_code == 200, (stuck, exc)
    return resp.get_json()


def compensate(exp_id, seq):
    return r_post("/api/experiments/%s/timeline/compensate" % exp_id, {"seq": seq})


def timeline(exp_id):
    resp, exc, stuck = r_get("/api/experiments/%s/timeline" % exp_id)
    assert not stuck and exc is None and resp.status_code == 200
    return resp.get_json()["timeline"]


def committed(exp_id):
    resp, exc, stuck = r_get("/api/experiments/%s/committed" % exp_id)
    assert not stuck and exc is None and resp.status_code == 200
    return resp.get_json()["committed"]


def status(exp_id):
    resp, exc, stuck = r_get("/api/experiments/%s/status" % exp_id)
    assert not stuck and exc is None and resp.status_code == 200
    return resp.get_json()


def commit_adds(exp_id, side, values):
    """开事务、逐个加法、提交；返回时间线末条记录。"""
    op(exp_id, side, {"op": "begin"})
    for v in values:
        body = op(exp_id, side, {"op": "add", "value": v})
        assert body["ok"] is True, body
    body = op(exp_id, side, {"op": "commit"})
    assert body["ok"] is True, body
    return timeline(exp_id)[-1]


# ================= 基本流程：甲 100 +5 +7 提交，补偿 #1 =================
exp1 = create_exp()
rec1 = commit_adds(exp1, "a", [5, 7])
check("基本·#1 普通记录 100→112 [5,7]",
      rec1 == {"seq": 1, "side": "a", "before": 100, "after": 112, "deltas": [5, 7]},
      str(rec1))
check("基本·普通记录字段不变（无 compensates）",
      set(rec1) == {"seq", "side", "before", "after", "deltas"})
check("基本·已提交值112", committed(exp1) == 112)

resp, exc, stuck = compensate(exp1, 1)
check("基本·补偿及时返回200", not stuck and exc is None and resp.status_code == 200)
body = resp.get_json()
check("基本·补偿 ok:true", body["ok"] is True and body["error"] is None)
check("基本·响应字段约定", set(body) == {"ok", "error", "record", "timeline"}, str(set(body)))
rec2 = body["record"]
check("基本·补偿记录 #2 system 112→100 [-7,-5] 原seq=1",
      rec2 == {"seq": 2, "side": "system", "before": 112, "after": 100,
               "deltas": [-7, -5], "compensates": 1},
      str(rec2))
check("基本·补偿后已提交值100", committed(exp1) == 100, str(committed(exp1)))
tl = timeline(exp1)
check("基本·时间线2条且原记录保留",
      len(tl) == 2 and tl[0] == rec1 and tl[1] == rec2, str(tl))
check("基本·响应内时间线与查询一致", body["timeline"] == tl)

# 补偿后继续普通提交：编号接续、字段不变
rec3 = commit_adds(exp1, "b", [3])
check("基本·补偿后普通提交 #3 100→103 [3]",
      rec3 == {"seq": 3, "side": "b", "before": 100, "after": 103, "deltas": [3]},
      str(rec3))
check("基本·已提交值103", committed(exp1) == 103)

# ================= 读快照 / 两栏状态不受补偿影响 =================
exp2 = create_exp()
commit_adds(exp2, "a", [5, 7])  # #1: 100→112
# 乙建立读快照（显式读值 112）；甲开启只读模式
op(exp2, "b", {"op": "begin"})
body = op(exp2, "b", {"op": "read"})
check("快照·乙读到112", body["state"]["last_read"] == 112)
resp, exc, stuck = r_post("/api/experiments/%s/conn/a/readonly" % exp2, {"enabled": True})
check("快照·甲只读开启", not stuck and resp.get_json()["ok"] is True)

resp, exc, stuck = compensate(exp2, 1)
check("快照·乙持读事务时补偿成功（读不阻塞写）",
      not stuck and resp.status_code == 200 and resp.get_json()["ok"] is True)
check("快照·已提交值100", committed(exp2) == 100)
# 已有读快照仍见旧值：乙事务内再读仍是 112
body = op(exp2, "b", {"op": "read"})
check("快照·已有读快照仍见旧值112", body["state"]["last_read"] == 112,
      str(body["state"]["last_read"]))
# 两栏事务、只读模式、最近显式读值均不变
st = status(exp2)
check("快照·乙事务仍开启", st["b"]["in_transaction"] is True)
check("快照·乙最近显式读值仍112", st["b"]["last_read"] == 112)
check("快照·甲只读仍开启", st["a"]["readonly"] is True)
op(exp2, "b", {"op": "rollback"})
r_post("/api/experiments/%s/conn/a/readonly" % exp2, {"enabled": False})
# 快照结束后新事务读到补偿后的值
op(exp2, "b", {"op": "begin"})
body = op(exp2, "b", {"op": "read"})
check("快照·新事务读到100", body["state"]["last_read"] == 100)
op(exp2, "b", {"op": "rollback"})

# ================= 过期目标 / 补偿记录 / 空时间线：409 =================
exp3 = create_exp()
resp, exc, stuck = compensate(exp3, 1)
check("过期·空时间线409", not stuck and resp.status_code == 409 and "error" in resp.get_json())
commit_adds(exp3, "a", [5])  # #1: 100→105
resp, _, _ = compensate(exp3, 2)
check("过期·末条之外的seq 409", resp.status_code == 409)
resp, _, _ = compensate(exp3, 99)
check("过期·不存在的seq 409", resp.status_code == 409)
resp, _, _ = compensate(exp3, 1)
check("过期·补偿末条成功", resp.status_code == 200 and resp.get_json()["ok"] is True)
resp, _, _ = compensate(exp3, 2)
check("过期·补偿记录不可再补偿409", resp.status_code == 409)
resp, _, _ = compensate(exp3, 1)
check("过期·已被补偿的记录成为过期目标409", resp.status_code == 409)
check("过期·多次409后已提交值仍100", committed(exp3) == 100)
check("过期·多次409后时间线仍2条", len(timeline(exp3)) == 2)

# ================= 有效脚本：409 =================
exp4 = create_exp()
commit_adds(exp4, "a", [5])  # #1
resp, exc, stuck = r_post("/api/experiments/%s/script" % exp4,
                          [{"side": "a", "op": "read"}])
sid4 = resp.get_json()["script"]["id"]
resp, exc, stuck = compensate(exp4, 1)
check("脚本·有效脚本期间补偿409", not stuck and resp.status_code == 409)
check("脚本·409不改库", committed(exp4) == 105)
check("脚本·409不改时间线", len(timeline(exp4)) == 1)
r_post("/api/experiments/%s/script/cancel" % exp4, {"script_id": sid4, "step": 1})
resp, _, _ = compensate(exp4, 1)
check("脚本·取消后补偿成功", resp.status_code == 200 and resp.get_json()["ok"] is True)
check("脚本·补偿后已提交值100", committed(exp4) == 100)

# ================= 写锁冲突：SQLITE_BUSY(5)，失败不改库 / 时间线 =================
exp5 = create_exp()
commit_adds(exp5, "a", [5])  # #1: 100→105
op(exp5, "a", {"op": "begin"})
body = op(exp5, "a", {"op": "add", "value": 7})
assert body["ok"] is True  # 甲持写锁（未提交）
resp, exc, stuck = compensate(exp5, 1)
check("写锁·补偿及时返回200", not stuck and exc is None and resp.status_code == 200)
body = resp.get_json()
check("写锁·ok:false 且 SQLITE_BUSY(5)",
      body["ok"] is False and body["error"]["name"] == "SQLITE_BUSY"
      and body["error"]["code"] == 5, str(body.get("error")))
check("写锁·失败不改库", committed(exp5) == 105)
check("写锁·失败不改时间线", len(timeline(exp5)) == 1)
check("写锁·失败响应仍携带时间线", body["timeline"] == timeline(exp5))
# 甲提交释放写锁后，补偿新末条成功
op(exp5, "a", {"op": "commit"})  # #2: 105→112
resp, _, _ = compensate(exp5, 2)
check("写锁·释放后补偿新末条成功", resp.status_code == 200 and resp.get_json()["ok"] is True)
check("写锁·补偿后已提交值105", committed(exp5) == 105)
tl = timeline(exp5)
check("写锁·#3 system 112→105 [-7] 原seq=2",
      tl[-1] == {"seq": 3, "side": "system", "before": 112, "after": 105,
                 "deltas": [-7], "compensates": 2}, str(tl[-1]))

# ================= 并发：同一 seq 最多成功一次 =================
race_ok = True
for _ in range(20):
    exp_r = create_exp()
    commit_adds(exp_r, "a", [5, 7])  # #1: 100→112
    barrier = threading.Barrier(2)
    results = {}

    def do_comp(tag):
        barrier.wait()
        results[tag] = client.post(
            "/api/experiments/%s/timeline/compensate" % exp_r, {"seq": 1})

    threads = [threading.Thread(target=do_comp, args=(i,), daemon=True) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(ROUTE_TIMEOUT)
    if any(t.is_alive() for t in threads):
        race_ok = False
        break
    r1, r2 = results[1], results[2]
    oks = [r for r in (r1, r2)
           if r.status_code == 200 and r.get_json().get("ok") is True]
    conflicts = [r for r in (r1, r2) if r.status_code == 409]
    if not (len(oks) == 1 and len(conflicts) == 1):
        race_ok = False
        break
    if committed(exp_r) != 100 or len(timeline(exp_r)) != 2:
        race_ok = False
        break
    if client.delete("/api/experiments/%s" % exp_r).status_code != 200:
        race_ok = False
        break
check("并发·同一seq最多成功一次(20轮)", race_ok)

# ================= 交错：与普通提交互斥，重新核对目标 =================
interleave_ok = True
for _ in range(20):
    exp_r = create_exp()
    commit_adds(exp_r, "a", [5])  # #1: 100→105
    barrier = threading.Barrier(2)
    results = {}

    def do_comp2():
        barrier.wait()
        results["comp"] = client.post(
            "/api/experiments/%s/timeline/compensate" % exp_r, {"seq": 1})

    def do_commit():
        barrier.wait()
        client.post("/api/experiments/%s/conn/b/op" % exp_r, {"op": "begin"})
        client.post("/api/experiments/%s/conn/b/op" % exp_r,
                    {"op": "add", "value": 7})
        results["commit"] = client.post(
            "/api/experiments/%s/conn/b/op" % exp_r, {"op": "commit"})

    threads = [threading.Thread(target=do_comp2, daemon=True),
               threading.Thread(target=do_commit, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(ROUTE_TIMEOUT)
    if any(t.is_alive() for t in threads):
        interleave_ok = False
        break
    comp = results["comp"]
    tl = timeline(exp_r)
    last = tl[-1]
    if comp.status_code == 200 and comp.get_json().get("ok") is True:
        # 补偿先完成：#2 system（105→100 [-5] 原seq=1），随后乙提交为 #3
        ok = (len(tl) == 3
              and tl[1] == {"seq": 2, "side": "system", "before": 105,
                            "after": 100, "deltas": [-5], "compensates": 1}
              and tl[2]["side"] == "b" and tl[2]["before"] == 100
              and tl[2]["after"] == 107 and tl[2]["deltas"] == [7])
    elif comp.status_code == 409:
        # 乙的提交先追加：目标已过期，时间线只有两条普通记录
        ok = (len(tl) == 2 and tl[1]["side"] == "b"
              and tl[1]["before"] == 105 and tl[1]["after"] == 112)
    elif comp.status_code == 200 and comp.get_json().get("ok") is False:
        # 补偿撞上乙未提交的写锁：SQLITE_BUSY，时间线随后只有乙的提交
        ok = (comp.get_json()["error"]["name"] == "SQLITE_BUSY"
              and len(tl) == 2 and tl[1]["side"] == "b")
    else:
        ok = False
    # 无论哪种交错：已提交值必须等于末条记录的 after
    ok = ok and committed(exp_r) == last["after"]
    if not ok:
        interleave_ok = False
        break
    client.delete("/api/experiments/%s" % exp_r)
check("交错·与普通提交互斥且结果一致(20轮)", interleave_ok)

# ================= 大整数无损 =================
exp6 = create_exp()
big = 9007199254740993  # 超出 JS 安全整数范围
rec1 = commit_adds(exp6, "a", [str(big)])
check("大整数·#1 after 字符串无损",
      rec1["after"] == "9007199254741093" and rec1["deltas"] == ["9007199254740993"],
      str(rec1))
resp, _, _ = compensate(exp6, 1)
body = resp.get_json()
check("大整数·补偿成功", resp.status_code == 200 and body["ok"] is True)
rec2 = body["record"]
check("大整数·补偿记录 before 字符串 / after 数字 / deltas 取负无损",
      rec2["before"] == "9007199254741093" and rec2["after"] == 100
      and rec2["deltas"] == ["-9007199254740993"] and rec2["compensates"] == 1,
      str(rec2))
check("大整数·补偿后已提交值100", committed(exp6) == 100)

# ================= 非法 seq：400 =================
exp7 = create_exp()
commit_adds(exp7, "a", [5])
bad_bodies = [None, {}, {"seq": 0}, {"seq": -1}, {"seq": 1.5},
              {"seq": True}, {"seq": "1"}, {"seq": None}, [1]]
all_400 = True
for bad in bad_bodies:
    resp, exc, stuck = r_post("/api/experiments/%s/timeline/compensate" % exp7, bad)
    if stuck or resp is None or resp.status_code != 400:
        all_400 = False
        break
check("非法·非正整数seq一律400", all_400)
check("非法·400不改库", committed(exp7) == 105)
check("非法·400不改时间线", len(timeline(exp7)) == 1)

# ================= 删除准入与未知实验 =================
resp, exc, stuck = r_delete("/api/experiments/%s" % exp7)
check("删除·及时完成", not stuck and resp.status_code == 200)
resp, exc, stuck = compensate(exp7, 1)
check("删除·已删除实验补偿410", not stuck and resp.status_code == 410)
resp, exc, stuck = compensate("does-not-exist", 1)
check("删除·未知实验补偿404", not stuck and resp.status_code == 404)

# 收尾：删除其余实验，确认没有请求占住准入名额
clean_ok = True
for e in (exp1, exp2, exp3, exp4, exp5, exp6):
    resp, exc, stuck = r_delete("/api/experiments/%s" % e)
    clean_ok = clean_ok and not stuck and resp.status_code == 200
check("收尾·全部实验删除及时完成（名额均已归还）", clean_ok)

print()
print(f"{len(fails)} 失败" if fails else "后端补偿测试全部通过")
sys.exit(1 if fails else 0)
