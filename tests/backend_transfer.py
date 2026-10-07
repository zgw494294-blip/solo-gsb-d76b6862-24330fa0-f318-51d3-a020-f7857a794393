"""后端跨实验转移（transfer）验证（无需 Flask）：用最小 stub 导入 app.py。

通过 stub 的最小路由分发驱动 **app.py 注册的实际路由**（真实经过两实验的
admit_experiment 准入门控、script_lock / timeline_lock 与真实 SQLite，
不另写模拟准入逻辑），覆盖跨实验整数转移的全部约定：

- 基本流程：来源减、目标加，两边 before/after、deltas、连续 seq 与响应
  字段；时间线追加 side=transfer 记录（无 compensates），编号各自连续，
  反向转移同样成立，总额守恒；
- 整数校验：非 JSON 对象、source/target 非字符串、source==target、
  amount 非正整数（0/负数/布尔/小数/字符串/None/超过 ±2⁶²）一律 400；
  大整数十进制字符串无损；
- 未知 ID 404、本次运行已删除 ID 410（来源 / 目标各一次）；
- 任一实验有有效脚本 409，两库都不改；取消后成功；
- 目标 / 来源持写锁时立即 SQLITE_BUSY(5)（含 failed_side），两库本次
  值与记录全部撤销；释放后成功；
- 两栏事务、只读模式、读快照（已有读快照仍见旧值）、最近显式读值都
  不受影响；
- 64 位上溢 / 下溢拒绝（名称 / 码为 null），两库不改、不记录；
- 转移记录不可补偿（末条为转移记录时 409），其后的普通提交仍可补偿；
- 重启后值与转移记录完整恢复、编号接续、仍不可补偿；
- 并发反向转移、与普通提交 / 补偿交错：不死锁、不卡住，重放时间线
  与已提交值严格一致，总额守恒；
- 删除准入：删除先开始则转移 410，已准入转移先收尾删除才完成，
  等待期间不阻塞其他实验；删除后其他实验不受影响；
- 大整数无损；转移记录字段集合与普通记录一致。
"""
import sys, types, json, os, re, tempfile, threading, random, sqlite3

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
    return re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", path) + "$")


class _App:
    def __init__(self, name):
        self.routes = []

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
    """最小路由分发：直接调用 app.py 注册的实际视图函数。"""

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
ROUTE_TIMEOUT = 10.0


def route_call(fn, *args, **kwargs):
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
    resp, exc, stuck = r_post(
        "/api/experiments/%s/conn/%s/op" % (exp_id, side), body)
    assert not stuck and exc is None and resp.status_code == 200, (stuck, exc)
    return resp.get_json()


def commit_adds(exp_id, side, values):
    op(exp_id, side, {"op": "begin"})
    for v in values:
        body = op(exp_id, side, {"op": "add", "value": v})
        assert body["ok"] is True, body
    body = op(exp_id, side, {"op": "commit"})
    assert body["ok"] is True, body


def transfer(source_id, target_id, amount):
    return r_post("/api/transfers",
                  {"source": source_id, "target": target_id, "amount": amount})


def timeline(exp_id):
    resp, exc, stuck = r_get("/api/experiments/%s/timeline" % exp_id)
    assert not stuck and exc is None and resp.status_code == 200
    return resp.get_json()["timeline"]


def committed(exp_id):
    resp, exc, stuck = r_get("/api/experiments/%s/committed" % exp_id)
    assert not stuck and exc is None and resp.status_code == 200
    return resp.get_json()["committed"]


def committed_int(exp_id):
    # /committed 沿用 json_int：超出 ±2⁵³−1 返回十进制字符串
    value = committed(exp_id)
    return int(value) if isinstance(value, str) else value


def status(exp_id):
    resp, exc, stuck = r_get("/api/experiments/%s/status" % exp_id)
    assert not stuck and exc is None and resp.status_code == 200
    return resp.get_json()


def compensate(exp_id, seq):
    return r_post("/api/experiments/%s/timeline/compensate" % exp_id,
                  {"seq": seq})


def restart(*exp_ids):
    """模拟进程终止 + 重启：丢弃内存对象（未提交事务回滚），重新扫描目录。"""
    for eid in exp_ids:
        with A.experiments_lock:
            exp = A.experiments.pop(eid, None)
            A.deleted_experiments.clear()
        if exp is not None:
            for side in ("a", "b"):
                try:
                    exp.conns[side].close()
                except Exception:
                    pass
            try:
                exp.observer.close()
            except Exception:
                pass
    A.restore_experiments()


def replay_consistent(exp_id):
    """重放时间线：每条 before==当前值，after==before+Σdeltas；
    末尾当前值等于已提交值，seq 从 1 连续。"""
    current = 100
    for index, rec in enumerate(timeline(exp_id), start=1):
        if rec["seq"] != index:
            return False
        if int(rec["before"]) != current:
            return False
        after = int(rec["before"]) + sum(int(d) for d in rec["deltas"])
        if int(rec["after"]) != after:
            return False
        current = after
    return committed_int(exp_id) == current


# ================= 基本流程：来源减 / 目标加，两边记录与序号 =================
e1 = create_exp()
e2 = create_exp()
commit_adds(e1, "a", [5, 7])    # e1: 100→112，时间线 #1
commit_adds(e2, "b", [3])       # e2: 100→103，时间线 #1
resp, exc, stuck = transfer(e1, e2, 10)
check("基本·转移及时200", not stuck and exc is None and resp.status_code == 200)
body = resp.get_json()
check("基本·ok:true", body["ok"] is True and body["error"] is None)
check("基本·顶层字段", set(body) == {"ok", "error", "source", "target", "timelines"},
      str(set(body)))
check("基本·来源 e1 112→102 [-10] seq=2",
      body["source"] == {"id": e1, "before": 112, "after": 102,
                         "deltas": [-10], "seq": 2}, str(body["source"]))
check("基本·目标 e2 103→113 [10] seq=2",
      body["target"] == {"id": e2, "before": 103, "after": 113,
                         "deltas": [10], "seq": 2}, str(body["target"]))
check("基本·来源已提交值102", committed_int(e1) == 102)
check("基本·目标已提交值113", committed_int(e2) == 113)
tl1, tl2 = timeline(e1), timeline(e2)
rec1, rec2 = tl1[-1], tl2[-1]
check("基本·来源追加 side=transfer 记录且字段同普通格式",
      rec1 == {"seq": 2, "side": "transfer", "before": 112,
               "after": 102, "deltas": [-10]}, str(rec1))
check("基本·目标追加 side=transfer 记录且字段同普通格式",
      rec2 == {"seq": 2, "side": "transfer", "before": 103,
               "after": 113, "deltas": [10]}, str(rec2))
check("基本·响应内两库时间线与查询一致",
      body["timelines"][e1] == tl1 and body["timelines"][e2] == tl2)

# 反向转移：编号各自连续，总额守恒
resp, _, _ = transfer(e2, e1, 4)
body = resp.get_json()
check("反向·目标(原来源) e1 102→106 [4] seq=3",
      body["target"] == {"id": e1, "before": 102, "after": 106,
                         "deltas": [4], "seq": 3}, str(body["target"]))
check("反向·来源(原目标) e2 113→109 [-4] seq=3",
      body["source"] == {"id": e2, "before": 113, "after": 109,
                         "deltas": [-4], "seq": 3}, str(body["source"]))
check("反向·总额守恒", committed_int(e1) + committed_int(e2) == 106 + 109 == 215)

# 全新两库首笔转移：seq 从 1 开始
n1, n2 = create_exp(), create_exp()
resp, _, _ = transfer(n1, n2, 10)
b = resp.get_json()
check("空时间线·来源 seq=1 100→90",
      b["source"] == {"id": n1, "before": 100, "after": 90,
                      "deltas": [-10], "seq": 1})
check("空时间线·目标 seq=1 100→110",
      b["target"] == {"id": n2, "before": 100, "after": 110,
                      "deltas": [10], "seq": 1})

# ================= 整数校验：400 =================
bad_bodies = [
    None, [], "x",
    {},
    {"target": e2, "amount": 1},                       # 缺 source
    {"source": e1, "amount": 1},                       # 缺 target
    {"source": 1, "target": e2, "amount": 1},          # source 非字符串
    {"source": e1, "target": 2, "amount": 1},          # target 非字符串
    {"source": e1, "target": e1, "amount": 1},         # source==target
    {"source": e1, "target": e2},                      # 缺 amount
    {"source": e1, "target": e2, "amount": 0},         # 0
    {"source": e1, "target": e2, "amount": -3},        # 负数
    {"source": e1, "target": e2, "amount": True},      # 布尔
    {"source": e1, "target": e2, "amount": 1.5},       # 小数
    {"source": e1, "target": e2, "amount": "abc"},     # 非数字字符串
    {"source": e1, "target": e2, "amount": None},      # null
    {"source": e1, "target": e2, "amount": 2 ** 62 + 1},  # 超 ±2⁶²
]
all_400 = True
for bad in bad_bodies:
    resp, exc, stuck = r_post("/api/transfers", bad)
    if stuck or exc is not None or resp is None or resp.status_code != 400:
        all_400 = False
        break
check("校验·非法 body 一律400（%d 例）" % len(bad_bodies), all_400)
check("校验·400 不改任何库",
      committed_int(e1) == 106 and committed_int(e2) == 109)

# ================= 未知 / 已删除：404 / 410 =================
resp, _, stuck = transfer("does-not-exist", e2, 1)
check("准入·未知来源404", not stuck and resp.status_code == 404)
resp, _, _ = transfer(e1, "does-not-exist", 1)
check("准入·未知目标404", resp.status_code == 404)
gone = create_exp()
resp, _, _ = r_delete("/api/experiments/%s" % gone)
assert resp.status_code == 200
resp, _, _ = transfer(gone, e2, 1)
check("准入·已删除来源410", resp.status_code == 410)
resp, _, _ = transfer(e1, gone, 1)
check("准入·已删除目标410", resp.status_code == 410)
restart(gone)
resp, _, _ = transfer(gone, e2, 1)
check("准入·重启后已删除不复活404", resp.status_code == 404)

# ================= 有效脚本：任一库 active 一律 409 =================
s1, s2 = create_exp(), create_exp()
resp, _, _ = r_post("/api/experiments/%s/script" % s1,
                    [{"side": "a", "op": "read"}])
sid_s1 = resp.get_json()["script"]["id"]
resp, _, _ = transfer(s1, s2, 1)
check("脚本·来源有效脚本409", resp.status_code == 409)
check("脚本·409 两库不改", committed_int(s1) == 100 and committed_int(s2) == 100)
check("脚本·409 两库不记录", timeline(s1) == [] and timeline(s2) == [])
resp, _, _ = r_post("/api/experiments/%s/script/cancel" % s1,
                    {"script_id": sid_s1, "step": 1})
assert resp.status_code == 200
resp, _, _ = r_post("/api/experiments/%s/script" % s2,
                    [{"side": "b", "op": "read"}])
sid_s2 = resp.get_json()["script"]["id"]
resp, _, _ = transfer(s1, s2, 1)
check("脚本·目标有效脚本409", resp.status_code == 409)
check("脚本·目标409 两库不改", committed_int(s1) == 100 and committed_int(s2) == 100)
r_post("/api/experiments/%s/script/cancel" % s2,
       {"script_id": sid_s2, "step": 1})
resp, _, _ = transfer(s1, s2, 1)
check("脚本·两库都取消后转移成功",
      resp.status_code == 200 and resp.get_json()["ok"] is True)
check("脚本·转移后99/101", committed_int(s1) == 99 and committed_int(s2) == 101)

# ================= 写锁冲突：立即 SQLITE_BUSY(5)，两库撤销 =================
w1, w2 = create_exp(), create_exp()
# 目标持写锁
op(w2, "a", {"op": "begin"})
b2 = op(w2, "a", {"op": "add", "value": 7})
assert b2["ok"] is True  # 目标甲持写锁（未提交）
resp, exc, stuck = transfer(w1, w2, 5)
check("写锁·目标持锁及时200", not stuck and exc is None and resp.status_code == 200)
body = resp.get_json()
check("写锁·ok:false 且 SQLITE_BUSY(5)",
      body["ok"] is False and body["error"]["name"] == "SQLITE_BUSY"
      and body["error"]["code"] == 5, str(body.get("error")))
check("写锁·failed_side 指向目标", body["error"]["failed_side"] == w2)
check("写锁·冲突两库值不变", committed_int(w1) == 100 and committed_int(w2) == 100)
check("写锁·冲突两库不记录", timeline(w1) == [] and timeline(w2) == [])
check("写锁·失败视图 seq=null 且 after==before",
      body["source"]["after"] == 100 and body["source"]["seq"] is None
      and body["target"]["after"] == 100 and body["target"]["seq"] is None
      and body["source"]["deltas"] == [] and body["target"]["deltas"] == [])
op(w2, "a", {"op": "rollback"})
resp, _, _ = transfer(w1, w2, 5)
check("写锁·目标释放后转移成功",
      resp.status_code == 200 and resp.get_json()["ok"] is True)
check("写锁·转移后95/105", committed_int(w1) == 95 and committed_int(w2) == 105)
# 来源持写锁
op(w1, "b", {"op": "begin"})
op(w1, "b", {"op": "add", "value": 2})
resp, _, _ = transfer(w1, w2, 5)
body = resp.get_json()
check("写锁·来源持锁 SQLITE_BUSY(5) 且 failed_side=来源",
      body["ok"] is False and body["error"]["name"] == "SQLITE_BUSY"
      and body["error"]["code"] == 5 and body["error"]["failed_side"] == w1,
      str(body.get("error")))
check("写锁·来源冲突两库值不变", committed_int(w1) == 95 and committed_int(w2) == 105)
check("写锁·来源冲突两库不追加",
      len(timeline(w1)) == 1 and len(timeline(w2)) == 1)
op(w1, "b", {"op": "rollback"})

# ================= 两栏事务 / 只读 / 读快照 / 最近读值不受影响 =================
p1, p2 = create_exp(), create_exp()
op(p2, "b", {"op": "begin"})
body = op(p2, "b", {"op": "read"})
check("不动两栏·乙读值100", body["state"]["last_read"] == 100)
resp, _, _ = r_post("/api/experiments/%s/conn/a/readonly" % p1,
                    {"enabled": True})
assert resp.get_json()["ok"] is True
resp, _, _ = transfer(p1, p2, 5)
check("不动两栏·转移成功", resp.get_json()["ok"] is True)  # p1 95, p2 105
body = op(p2, "b", {"op": "read"})
check("不动两栏·已有读快照仍见旧值100", body["state"]["last_read"] == 100)
st1, st2 = status(p1), status(p2)
check("不动两栏·乙事务仍开启", st2["b"]["in_transaction"] is True)
check("不动两栏·乙最近显式读值仍100", st2["b"]["last_read"] == 100)
check("不动两栏·甲只读仍开启", st1["a"]["readonly"] is True)
check("不动两栏·甲无事务", st1["a"]["in_transaction"] is False)
op(p2, "b", {"op": "rollback"})
op(p2, "b", {"op": "begin"})
body = op(p2, "b", {"op": "read"})
check("不动两栏·新事务读到105", body["state"]["last_read"] == 105)
op(p2, "b", {"op": "rollback"})
r_post("/api/experiments/%s/conn/a/readonly" % p1, {"enabled": False})

# ================= 64 位溢出：拒绝，两库不改不记录 =================
o1, o2 = create_exp(), create_exp()
# 目标累加到 INT64_MAX：+2^62 后再 +(2^62-103)
INT64_MAX = 2 ** 63 - 1
INT64_MIN = -(2 ** 63)
commit_adds(o2, "a", [2 ** 62, INT64_MAX - 100 - 2 ** 62])
check("溢出·目标预置到 INT64_MAX", committed_int(o2) == INT64_MAX,
      str(committed_int(o2)))
resp, _, _ = transfer(o1, o2, 1)
body = resp.get_json()
check("溢出·目标上溢拒绝",
      resp.status_code == 200 and body["ok"] is False
      and body["error"]["name"] is None and body["error"]["code"] is None)
check("溢出·上溢两库值不变",
      committed_int(o1) == 100 and committed_int(o2) == INT64_MAX)
check("溢出·上溢两库不记录", timeline(o1) == [] and timeline(o2) != [] and
      all(r["side"] != "transfer" for r in timeline(o2)))
# 来源下溢：预置到 INT64_MIN（-2^62, -2^62, -100）
u1, u2 = create_exp(), create_exp()
commit_adds(u1, "a", [-2 ** 62, -2 ** 62, -100])
check("溢出·来源预置到 INT64_MIN", committed_int(u1) == INT64_MIN,
      str(committed_int(u1)))
resp, _, _ = transfer(u1, u2, 1)
body = resp.get_json()
check("溢出·来源下溢拒绝",
      body["ok"] is False and body["error"]["name"] is None
      and body["error"]["code"] is None)
check("溢出·下溢两库值不变",
      committed_int(u1) == INT64_MIN and committed_int(u2) == 100)

# ================= 转移记录不可补偿 =================
c1, c2 = create_exp(), create_exp()
resp, _, _ = transfer(c1, c2, 5)
assert resp.get_json()["ok"] is True  # c1 95（#1 transfer），c2 105
resp, _, _ = compensate(c1, 1)
check("不可补偿·末条转移记录409", resp.status_code == 409)
resp, _, _ = compensate(c2, 1)
check("不可补偿·目标侧转移记录409", resp.status_code == 409)
check("不可补偿·409 不改值", committed_int(c1) == 95 and committed_int(c2) == 105)
# 转移之后再来一笔普通提交，该普通记录仍可补偿
commit_adds(c1, "a", [2])  # c1: 95→97 #2 normal
resp, _, _ = compensate(c1, 2)
check("不可补偿·转移后普通末条仍可补偿",
      resp.status_code == 200 and resp.get_json()["ok"] is True)
check("不可补偿·补偿后95", committed_int(c1) == 95)
tl = timeline(c1)
check("不可补偿·补偿记录 deltas 逆序取负且记录齐全",
      [(r["seq"], r["side"]) for r in tl] == [(1, "transfer"), (2, "a"),
                                              (3, "system")],
      str([(r["seq"], r["side"]) for r in tl]))

# ================= 重启恢复：值与转移记录完整、编号接续、仍不可补偿 =================
k1, k2 = create_exp(), create_exp()
resp, _, _ = transfer(k1, k2, 11)
assert resp.get_json()["ok"] is True  # k1 89 #1, k2 111 #1
commit_adds(k2, "a", [4])             # k2 111→115 #2 normal
restart(k1, k2)
check("恢复·来源值89", committed_int(k1) == 89)
check("恢复·目标值115", committed_int(k2) == 115)
tl1, tl2 = timeline(k1), timeline(k2)
check("恢复·来源转移记录恢复",
      tl1 == [{"seq": 1, "side": "transfer", "before": 100,
               "after": 89, "deltas": [-11]}], str(tl1))
check("恢复·目标转移+普通记录恢复",
      tl2[0] == {"seq": 1, "side": "transfer", "before": 100,
                 "after": 111, "deltas": [11]}
      and tl2[1] == {"seq": 2, "side": "a", "before": 111,
                     "after": 115, "deltas": [4]}, str(tl2))
resp, _, _ = transfer(k1, k2, 2)
check("恢复·转移编号接续（#2 / #3）",
      resp.get_json()["source"]["seq"] == 2
      and resp.get_json()["target"]["seq"] == 3)
resp, _, _ = compensate(k1, 2)
check("恢复·恢复后转移记录仍不可补偿409", resp.status_code == 409)
restart(k1, k2)
check("恢复·再次重启一致", replay_consistent(k1) and replay_consistent(k2))

# ================= 大整数无损 =================
g1, g2 = create_exp(), create_exp()
big = 9007199254740993
# 先把来源预置到 big+100（两笔普通加法，各 ≤2⁶²，时间线 #1）：
# 转移后来源恰好回到 100，目标变成 big+100，两边都出现大整数字符串
v1 = 2 ** 52
v2 = big - v1  # 100 + v1 + v2 = big + 100
commit_adds(g1, "a", [v1, v2])
check("大整数·来源预置 big+100", committed_int(g1) == big + 100)
resp, _, _ = transfer(g1, g2, str(big))  # 十进制字符串传输
body = resp.get_json()
check("大整数·成功", body["ok"] is True)
check("大整数·来源视图字符串无损",
      body["source"] == {"id": g1, "before": "9007199254741093",
                         "after": 100,
                         "deltas": ["-9007199254740993"], "seq": 2},
      str(body["source"]))
check("大整数·目标视图字符串无损",
      body["target"] == {"id": g2, "before": 100,
                         "after": "9007199254741093",
                         "deltas": ["9007199254740993"], "seq": 1},
      str(body["target"]))
check("大整数·存储值精确",
      committed_int(g1) == 100 and committed_int(g2) == 100 + big)

# ================= 并发：反向转移不死锁、总额守恒、时间线可重放 =================
stress_ok = True
x1, x2 = create_exp(), create_exp()
rng = random.Random(42)


def stress_round(round_no):
    a = rng.randrange(1, 20)
    b = rng.randrange(1, 20)
    barrier = threading.Barrier(2)
    results = {}

    def t12():
        barrier.wait()
        results["fwd"] = client.post(
            "/api/transfers",
            {"source": x1, "target": x2, "amount": a})

    def t21():
        barrier.wait()
        results["rev"] = client.post(
            "/api/transfers",
            {"source": x2, "target": x1, "amount": b})

    ts = [threading.Thread(target=t12, daemon=True),
          threading.Thread(target=t21, daemon=True)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(ROUTE_TIMEOUT)
    if any(t.is_alive() for t in ts):
        return False
    return all(r.status_code == 200 and r.get_json().get("ok")
               for r in results.values())


for rno in range(60):
    if not stress_round(rno):
        stress_ok = False
        break
check("并发·60轮反向并发转移均成功且不卡死", stress_ok)
check("并发·总额守恒200", committed_int(x1) + committed_int(x2) == 200,
      "%d+%d" % (committed_int(x1), committed_int(x2)))
tl1, tl2 = timeline(x1), timeline(x2)
check("并发·两库记录数相同（每笔成功转移各一条）", len(tl1) == len(tl2) == 120,
      "%d/%d" % (len(tl1), len(tl2)))
check("并发·两库时间线均可重放且 seq 连续",
      replay_consistent(x1) and replay_consistent(x2))
check("并发·全部为 transfer 记录",
      all(r["side"] == "transfer" for r in tl1 + tl2))

# ================= 交错：与普通提交并发不死锁，时间线可重放 =================
y1, y2 = create_exp(), create_exp()
rng2 = random.Random(7)
stop = {"v": False}


def transfer_worm():
    while not stop["v"]:
        amt = rng2.randrange(1, 9)
        if rng2.random() < 0.5:
            client.post("/api/transfers",
                        {"source": y1, "target": y2, "amount": amt})
        else:
            client.post("/api/transfers",
                        {"source": y2, "target": y1, "amount": amt})


def commit_worm(exp_id, side):
    for _ in range(40):
        amt = rng2.randrange(1, 9)
        client.post("/api/experiments/%s/conn/%s/op" % (exp_id, side),
                    {"op": "begin"})
        r = client.post("/api/experiments/%s/conn/%s/op" % (exp_id, side),
                        {"op": "add", "value": amt})
        if r.get_json()["ok"]:
            client.post("/api/experiments/%s/conn/%s/op" % (exp_id, side),
                        {"op": "commit"})
        else:
            client.post("/api/experiments/%s/conn/%s/op" % (exp_id, side),
                        {"op": "rollback"})


tw = threading.Thread(target=transfer_worm, daemon=True)
cw1 = threading.Thread(target=commit_worm, args=(y1, "a"), daemon=True)
cw2 = threading.Thread(target=commit_worm, args=(y2, "b"), daemon=True)
tw.start(); cw1.start(); cw2.start()
cw1.join(ROUTE_TIMEOUT); cw2.join(ROUTE_TIMEOUT)
stop["v"] = True
tw.join(ROUTE_TIMEOUT)
check("交错·转移与普通提交线程全部收尾",
      not tw.is_alive() and not cw1.is_alive() and not cw2.is_alive())
check("交错·两库时间线可重放且 seq 连续",
      replay_consistent(y1) and replay_consistent(y2))

# ================= 交错：与补偿并发，不死锁，结果一致 =================
inter_ok = True
for _ in range(30):
    z1, z2 = create_exp(), create_exp()
    commit_adds(z1, "a", [5])  # z1: 100→105 #1 normal
    barrier = threading.Barrier(2)
    results = {}

    def do_transfer():
        barrier.wait()
        results["tr"] = client.post(
            "/api/transfers", {"source": z1, "target": z2, "amount": 3})

    def do_comp():
        barrier.wait()
        results["comp"] = client.post(
            "/api/experiments/%s/timeline/compensate" % z1, {"seq": 1})

    ts = [threading.Thread(target=do_transfer, daemon=True),
          threading.Thread(target=do_comp, daemon=True)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(ROUTE_TIMEOUT)
    if any(t.is_alive() for t in ts):
        inter_ok = False
        break
    tr, comp = results["tr"], results["comp"]
    tl = timeline(z1)
    if comp.status_code == 200 and comp.get_json().get("ok") is True:
        # 补偿先成功：#2 system 105→100，转移随后基于 100（97→103 一侧）
        ok = (tr.status_code == 200 and tr.get_json().get("ok") is True
              and [(r["seq"], r["side"]) for r in tl]
              == [(1, "a"), (2, "system"), (3, "transfer")]
              and committed_int(z2) == 103)
    elif comp.status_code == 409:
        # 转移先成功：末条已是 transfer，补偿 409
        ok = (tr.status_code == 200 and tr.get_json().get("ok") is True
              and [(r["seq"], r["side"]) for r in tl]
              == [(1, "a"), (2, "transfer")])
    elif comp.status_code == 200 and comp.get_json().get("ok") is False:
        # 补偿撞上转移连接的写锁：SQLITE_BUSY，两库随转移成功
        ok = (comp.get_json()["error"]["name"] == "SQLITE_BUSY"
              and tr.get_json().get("ok") is True)
    else:
        ok = False
    ok = ok and replay_consistent(z1) and replay_consistent(z2)
    if not ok:
        inter_ok = False
        break
    client.delete("/api/experiments/%s" % z1)
    client.delete("/api/experiments/%s" % z2)
check("交错·转移与补偿并发结果一致(30轮)", inter_ok)

# ================= 删除准入：删除先开始410，已准入先收尾，不阻塞其他实验 =================
d1, d2, d3 = create_exp(), create_exp(), create_exp()
entered = threading.Event()
release = threading.Event()
orig_open = A.open_transfer_connection


def held_open(db_path):
    entered.set()
    release.wait(timeout=ROUTE_TIMEOUT)
    return orig_open(db_path)


A.open_transfer_connection = held_open
try:
    tr_result = {}

    def do_held_transfer():
        r = client.post("/api/transfers",
                        {"source": d1, "target": d2, "amount": 5})
        tr_result["resp"] = r

    ttr = threading.Thread(target=do_held_transfer, daemon=True)
    ttr.start()
    assert entered.wait(ROUTE_TIMEOUT)  # 转移已准入并持两库锁

    # 在转移在途时发起删除：claim 后等待在途名额，DELETE 不完成
    del_result = {}

    def do_delete():
        del_result["resp"] = client.delete("/api/experiments/%s" % d1)

    td = threading.Thread(target=do_delete, daemon=True)
    td.start()
    td.join(0.5)
    check("删除准入·删除等待已准入转移收尾", td.is_alive())

    # 删除已开始：新转移在准入处 410；其他实验不受影响。
    # 注意转移线程此时持两库 timeline_lock：用只走 observer 连接的
    # /committed 验证存活（/status 会取 timeline_lock，按约定等待收尾）。
    resp, _, _ = transfer(d1, d3, 1)
    check("删除准入·删除先开始则新转移410", resp.status_code == 410)
    resp, _, _ = r_get("/api/experiments/%s/committed" % d3)
    check("删除准入·等待不阻塞其他实验", resp.status_code == 200)
    resp, _, _ = r_get("/api/experiments/%s/committed" % d2)
    check("删除准入·目标实验在转移收尾前仍可用（observer 读已提交值）",
          resp.status_code == 200 and resp.get_json()["committed"] == 100)

    release.set()
    ttr.join(ROUTE_TIMEOUT)
    td.join(ROUTE_TIMEOUT)
    check("删除准入·已准入转移成功收尾",
          not ttr.is_alive() and tr_result["resp"].status_code == 200
          and tr_result["resp"].get_json()["ok"] is True)
    check("删除准入·转移收尾后删除完成",
          not td.is_alive() and del_result["resp"].status_code == 200)
finally:
    A.open_transfer_connection = orig_open

# 删除后：d1 410；d2 已被转移修改且仍可用
resp, _, _ = transfer(d1, d3, 1)
check("删除准入·删除后转移410", resp.status_code == 410)
resp, _, _ = r_get("/api/experiments/%s/status" % d2)
check("删除准入·目标实验存活（值已被转移改为105）",
      resp.status_code == 200 and committed_int(d2) == 105)
resp, _, _ = r_delete("/api/experiments/%s" % d2)
check("删除准入·目标实验随后可独立删除", resp.status_code == 200)
resp, _, _ = r_delete("/api/experiments/%s" % d3)
check("删除准入·无关实验可独立删除", resp.status_code == 200)

# ================= 收尾：删除全部存活实验，确认名额均已归还 =================
survivors = [e1, e2, n1, n2, s1, s2, w1, w2, p1, p2, o1, o2, u1, u2,
             c1, c2, k1, k2, g1, g2, x1, x2, y1, y2]
clean_ok = True
for e in survivors:
    resp, exc, stuck = r_delete("/api/experiments/%s" % e)
    clean_ok = clean_ok and not stuck and exc is None and resp.status_code == 200
check("收尾·全部实验删除及时完成（名额均已归还）", clean_ok)

print()
print(f"{len(fails)} 失败" if fails else "后端跨实验转移测试全部通过")
sys.exit(1 if fails else 0)
