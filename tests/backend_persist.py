"""后端持久化与重启 / 强制终止恢复验证（无需 Flask）：用最小 stub 导入 app.py。

通过 stub 的最小路由分发驱动 **app.py 注册的实际路由**（真实经过
admit_experiment 准入门控与全部实验级锁，不另写模拟准入逻辑），并用
「关闭并丢弃内存对象 + 重新扫描数据目录」模拟进程终止（含 kill -9：
未提交事务随连接关闭回滚，已提交事务由 WAL 恢复），覆盖：

- 提交 + 补偿后重启：已提交值与完整时间线（含 compensates 与序号）恢复；
- 恢复后两栏无事务、只读关闭、最近读值为空；日志 / 脚本 / 检查点缓存不恢复；
- 恢复后继续编号，末条普通记录仍可补偿；
- 成功删除的实验重启后不复活（404）；
- 旧版库（无 timeline 表）首次加载保留现值、以空时间线为基线；
- 值与记录同一事务提交：COMMIT 前「崩溃」两者都未生效，
  COMMIT 后「崩溃」两者都生效；
- 失败提交保留原事务与重试能力，重试不重复记录（注入 INSERT 后失败）；
- 只读开启下提交仍成功并产生记录，开关保持开启；重启后开关恢复关闭；
- 失败加法 / 纯读提交 / 回滚不产生记录（重启后依然）；
- 未提交事务随「崩溃」回滚；大整数无损恢复；恢复幂等。
"""
import sys, types, json, os, re, tempfile, threading, sqlite3

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


def restart(*exp_ids):
    """模拟进程终止（含 kill -9）+ 重启：关闭并丢弃内存对象（未提交事务
    随连接关闭回滚，已提交事务由 WAL 恢复），清空内存注册表与删除墓碑，
    重新扫描数据目录恢复。恢复不依赖任何退出清理。"""
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


def create_exp():
    resp = client.post("/api/experiments")
    assert resp.status_code == 200
    return resp.get_json()["id"]


def op(exp_id, side, body):
    resp = client.post("/api/experiments/%s/conn/%s/op" % (exp_id, side), body)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def committed(exp_id):
    resp = client.get("/api/experiments/%s/committed" % exp_id)
    assert resp.status_code == 200
    return resp.get_json()["committed"]


def timeline(exp_id):
    resp = client.get("/api/experiments/%s/timeline" % exp_id)
    assert resp.status_code == 200
    return resp.get_json()["timeline"]


def status(exp_id):
    resp = client.get("/api/experiments/%s/status" % exp_id)
    assert resp.status_code == 200
    return resp.get_json()


def compensate(exp_id, seq):
    return client.post("/api/experiments/%s/timeline/compensate" % exp_id,
                       {"seq": seq})


def commit_adds(exp_id, side, values):
    """开事务、逐个加法、提交；返回时间线末条记录。"""
    op(exp_id, side, {"op": "begin"})
    for v in values:
        body = op(exp_id, side, {"op": "add", "value": v})
        assert body["ok"] is True, body
    body = op(exp_id, side, {"op": "commit"})
    assert body["ok"] is True, body
    return timeline(exp_id)[-1]


# ================= 基本持久化：提交 + 补偿后重启完整恢复 =================
exp1 = create_exp()
rec1 = commit_adds(exp1, "a", [5, 7])   # #1: 100→112 [5,7]
rec2 = commit_adds(exp1, "b", [3])      # #2: 112→115 [3]
resp = compensate(exp1, 2)              # #3: system 115→112 [-3] 补偿#2
check("基本·重启前补偿成功", resp.status_code == 200 and resp.get_json()["ok"] is True)
rec3 = resp.get_json()["record"]
check("基本·重启前已提交值112", committed(exp1) == 112)

# 制造一批「不恢复」的内存状态：甲事务开启 + 显式读值 + 只读开启 + 日志，
# 检查点缓存，有效脚本（另一实验上演示，见后）
op(exp1, "a", {"op": "begin"})
body = op(exp1, "a", {"op": "read"})
check("基本·重启前甲读值112", body["state"]["last_read"] == 112)
resp = client.post("/api/experiments/%s/conn/a/readonly" % exp1, {"enabled": True})
check("基本·重启前甲只读开启", resp.get_json()["ok"] is True)
resp = client.post("/api/experiments/%s/checkpoint/PASSIVE" % exp1)
check("基本·重启前检查点已执行", resp.get_json()["ok"] is True)
st = status(exp1)
check("基本·重启前内存状态就位",
      st["a"]["in_transaction"] is True and st["a"]["readonly"] is True
      and st["a"]["last_read"] == 112 and len(st["a"]["log"]) > 0
      and st["checkpoint"]["last"] is not None)

restart(exp1)

st = status(exp1)
check("恢复·已提交值112", committed(exp1) == 112)
tl = timeline(exp1)
check("恢复·完整时间线3条（含补偿关联与序号）",
      tl == [rec1, rec2, rec3] and tl[2]["compensates"] == 2
      and tl[2]["side"] == "system" and tl[2]["seq"] == 3, str(tl))
check("恢复·状态接口时间线一致", st["timeline"] == tl)
check("恢复·两栏无事务",
      st["a"]["in_transaction"] is False and st["b"]["in_transaction"] is False)
check("恢复·两栏只读关闭",
      st["a"]["readonly"] is False and st["b"]["readonly"] is False)
check("恢复·最近读值为空",
      st["a"]["last_read"] is None and st["b"]["last_read"] is None)
check("恢复·操作日志不恢复", st["a"]["log"] == [] and st["b"]["log"] == [])
check("恢复·检查点缓存不恢复",
      st["checkpoint"]["last"] is None and st["checkpoint"]["log"] == [])
check("恢复·脚本不恢复", st["script"] is None)

# ================= 恢复后继续编号，末条普通记录仍可补偿 =================
rec4 = commit_adds(exp1, "a", [1])      # #4: 112→113
check("续号·恢复后新提交编号接续为4", rec4["seq"] == 4, str(rec4))
check("续号·已提交值113", committed(exp1) == 113)
resp = compensate(exp1, 4)
check("续号·末条普通记录仍可补偿", resp.status_code == 200
      and resp.get_json()["ok"] is True)
rec5 = resp.get_json()["record"]
check("续号·补偿记录 #5 system 113→112 [-1] 原seq=4",
      rec5 == {"seq": 5, "side": "system", "before": 113, "after": 112,
               "deltas": [-1], "compensates": 4}, str(rec5))
check("续号·补偿后已提交值112", committed(exp1) == 112)
resp = compensate(exp1, 5)
check("续号·补偿记录不可再补偿409", resp.status_code == 409)
# 再次重启：续号与补偿同样持久
restart(exp1)
tl = timeline(exp1)
check("续号·再次重启后时间线5条且末条为补偿记录",
      len(tl) == 5 and tl[-1] == rec5, str(tl[-1]))
check("续号·再次重启后已提交值112", committed(exp1) == 112)

# ================= 成功删除的实验重启后不复活 =================
exp2 = create_exp()
commit_adds(exp2, "a", [9])
resp = client.delete("/api/experiments/%s" % exp2)
check("删除·删除成功", resp.status_code == 200 and resp.get_json()["deleted"] is True)
resp = client.get("/api/experiments/%s/status" % exp2)
check("删除·本次运行中410", resp.status_code == 410)
restart(exp2)
resp = client.get("/api/experiments/%s/status" % exp2)
check("删除·重启后不复活（404）", resp.status_code == 404)
check("删除·库文件已移除",
      not os.path.exists(os.path.join(_tmp, "exp_%s.db" % exp2)))

# ================= 旧版库：保留现值，以空时间线为基线 =================
legacy_id = "a1b2c3d4e5f6"
legacy_path = os.path.join(_tmp, "exp_%s.db" % legacy_id)
legacy_conn = sqlite3.connect(legacy_path)
legacy_conn.execute("PRAGMA journal_mode=WAL")
legacy_conn.execute(
    "CREATE TABLE kv (id INTEGER PRIMARY KEY CHECK (id = 1),"
    " val INTEGER NOT NULL)"
)
legacy_conn.execute("INSERT INTO kv (id, val) VALUES (1, 777)")
legacy_conn.commit()
legacy_conn.close()
A.restore_experiments()
check("旧版·按原ID恢复", committed(legacy_id) == 777)
check("旧版·空时间线为基线", timeline(legacy_id) == [])
rec = commit_adds(legacy_id, "a", [3])
check("旧版·首条记录从1编号 777→780 [3]",
      rec == {"seq": 1, "side": "a", "before": 777, "after": 780, "deltas": [3]},
      str(rec))
restart(legacy_id)
check("旧版·重启后现值780", committed(legacy_id) == 780)
check("旧版·重启后时间线1条", timeline(legacy_id) == [rec])

# ================= 崩溃原子性：值与记录只能都生效或都未生效 =================
exp3 = create_exp()
# COMMIT 前「崩溃」：UPDATE 与 INSERT 都已执行但未 COMMIT，
# 进程终止 → 事务回滚，两者都未生效
exp_obj = A.experiments[exp3]
crash_conn = exp_obj.conns["a"]
crash_conn.execute("BEGIN DEFERRED")
crash_conn.execute("UPDATE kv SET val = val + 5 WHERE id = 1")
A.Experiment.insert_timeline_record(
    crash_conn,
    {"seq": 1, "side": "a", "before": 100, "after": 105, "deltas": [5]},
)
# 不 COMMIT，直接「终止进程」（连接随对象丢弃而关闭，事务回滚）
restart(exp3)
check("崩溃·COMMIT前终止：值未生效", committed(exp3) == 100)
check("崩溃·COMMIT前终止：记录未生效", timeline(exp3) == [])
# COMMIT 后「崩溃」：两者都生效
commit_adds(exp3, "a", [5])
restart(exp3)
check("崩溃·COMMIT后终止：值生效", committed(exp3) == 105)
check("崩溃·COMMIT后终止：记录生效",
      timeline(exp3) == [{"seq": 1, "side": "a", "before": 100,
                          "after": 105, "deltas": [5]}])
# 补偿同事务：补偿后「崩溃」，值与补偿记录都生效
resp = compensate(exp3, 1)
check("崩溃·补偿成功", resp.status_code == 200 and resp.get_json()["ok"] is True)
restart(exp3)
check("崩溃·补偿后终止：值与补偿记录都生效",
      committed(exp3) == 100 and len(timeline(exp3)) == 2
      and timeline(exp3)[1]["side"] == "system"
      and timeline(exp3)[1]["compensates"] == 1)

# ================= 失败提交：保留原事务与重试能力，重试不重复记录 =================
exp4 = create_exp()
op(exp4, "a", {"op": "begin"})
body = op(exp4, "a", {"op": "add", "value": 5})
assert body["ok"] is True
# 注入「INSERT 已执行、随后失败」的提交故障（模拟 COMMIT 落盘前出错）：
# 第一次提交失败但事务保留；重试重新 INSERT 再 COMMIT，只记录一次
orig_insert = A.Experiment.__dict__["insert_timeline_record"]
inject = {"calls": 0}


def flaky_insert(conn, record):
    inject["calls"] += 1
    if inject["calls"] == 1:
        orig_insert(conn, record)  # INSERT 已写入本事务
        raise sqlite3.Error("injected failure after INSERT")
    return orig_insert(conn, record)


A.Experiment.insert_timeline_record = staticmethod(flaky_insert)
try:
    body = op(exp4, "a", {"op": "commit"})
    check("失败提交·首次提交失败", body["ok"] is False and body["error"] is not None)
finally:
    A.Experiment.insert_timeline_record = orig_insert
st = status(exp4)
check("失败提交·原事务保留", st["a"]["in_transaction"] is True)
check("失败提交·未产生记录", timeline(exp4) == [])
check("失败提交·值未生效", committed(exp4) == 100)
body = op(exp4, "a", {"op": "commit"})
check("失败提交·重试成功", body["ok"] is True)
tl = timeline(exp4)
check("失败提交·重试只记录一次",
      tl == [{"seq": 1, "side": "a", "before": 100, "after": 105,
              "deltas": [5]}], str(tl))
check("失败提交·重试后值105", committed(exp4) == 105)
restart(exp4)
check("失败提交·重启后仍只有1条记录", len(timeline(exp4)) == 1)
check("失败提交·重启后值105", committed(exp4) == 105)

# ================= 只读开启下提交：记录产生，开关保持；重启后开关关闭 =================
exp5 = create_exp()
op(exp5, "a", {"op": "begin"})
body = op(exp5, "a", {"op": "add", "value": 5})
assert body["ok"] is True
resp = client.post("/api/experiments/%s/conn/a/readonly" % exp5, {"enabled": True})
check("只读提交·只读开启", resp.get_json()["ok"] is True)
body = op(exp5, "a", {"op": "commit"})
check("只读提交·只读下提交成功", body["ok"] is True)
check("只读提交·产生记录",
      timeline(exp5) == [{"seq": 1, "side": "a", "before": 100,
                          "after": 105, "deltas": [5]}])
st = status(exp5)
check("只读提交·提交不重置开关", st["a"]["readonly"] is True)
check("只读提交·已提交值105", committed(exp5) == 105)
restart(exp5)
st = status(exp5)
check("只读提交·重启后记录与值恢复",
      committed(exp5) == 105 and len(timeline(exp5)) == 1)
check("只读提交·重启后开关恢复关闭", st["a"]["readonly"] is False)

# ================= 失败加法 / 纯读提交 / 回滚：不产生记录（重启后依然） =================
exp6 = create_exp()
op(exp6, "a", {"op": "begin"})
op(exp6, "a", {"op": "read"})
body = op(exp6, "a", {"op": "commit"})
check("无记录·纯读提交成功", body["ok"] is True)
op(exp6, "a", {"op": "begin"})
body = op(exp6, "a", {"op": "add", "value": 2 ** 62})
assert body["ok"] is True  # 100 + 2^62 未越界：成功（回滚时不记录）
body = op(exp6, "a", {"op": "add", "value": 2 ** 62})
check("无记录·越界加法被拒绝", body["ok"] is False)  # 累加结果越出 int64
body = op(exp6, "a", {"op": "rollback"})
check("无记录·回滚成功", body["ok"] is True)
op(exp6, "b", {"op": "begin"})
body = op(exp6, "b", {"op": "add", "value": 5})
assert body["ok"] is True
op(exp6, "b", {"op": "rollback"})  # 成功加法但回滚：不记录
check("无记录·时间线为空", timeline(exp6) == [])
check("无记录·已提交值100", committed(exp6) == 100)
restart(exp6)
check("无记录·重启后时间线仍为空", timeline(exp6) == [])
check("无记录·重启后已提交值100", committed(exp6) == 100)

# ================= 未提交事务随「崩溃」回滚 =================
exp7 = create_exp()
op(exp7, "a", {"op": "begin"})
body = op(exp7, "a", {"op": "add", "value": 5})
assert body["ok"] is True  # 甲持写锁，未提交
restart(exp7)
st = status(exp7)
check("未提交·崩溃后值未生效", committed(exp7) == 100)
check("未提交·崩溃后无记录", timeline(exp7) == [])
check("未提交·崩溃后无事务", st["a"]["in_transaction"] is False)

# ================= 有效脚本不恢复 =================
exp8 = create_exp()
resp = client.post("/api/experiments/%s/script" % exp8,
                   [{"side": "a", "op": "begin"},
                    {"side": "a", "op": "read"}])
sid8 = resp.get_json()["script"]["id"]
resp = client.post("/api/experiments/%s/script/advance" % exp8,
                   {"script_id": sid8, "step": 1})
check("脚本·重启前有效脚本就位", resp.get_json()["ok"] is True)
restart(exp8)
st = status(exp8)
check("脚本·重启后脚本不恢复", st["script"] is None)
check("脚本·重启后事务不恢复", st["a"]["in_transaction"] is False)
# 手工操作立即可用（无脚本锁定）
body = op(exp8, "a", {"op": "begin"})
check("脚本·重启后手工操作立即可用", body["ok"] is True)
op(exp8, "a", {"op": "rollback"})

# ================= 大整数无损恢复 =================
exp9 = create_exp()
big = 9007199254740993  # 超出 JS 安全整数范围
rec1 = commit_adds(exp9, "a", [str(big)])
check("大整数·#1 after 字符串无损", rec1["after"] == "9007199254741093")
resp = compensate(exp9, 1)
rec2 = resp.get_json()["record"]
check("大整数·补偿记录无损",
      rec2["before"] == "9007199254741093" and rec2["after"] == 100
      and rec2["deltas"] == ["-9007199254740993"])
restart(exp9)
tl = timeline(exp9)
check("大整数·重启后时间线无损", tl == [rec1, rec2], str(tl))
check("大整数·重启后已提交值100", committed(exp9) == 100)

# ================= 恢复幂等：重复扫描不重复加载 =================
before_ids = set(A.experiments)
before_obj = A.experiments[exp9]
A.restore_experiments()
check("幂等·重复恢复不新增实验", set(A.experiments) == before_ids)
check("幂等·已加载对象不被替换", A.experiments[exp9] is before_obj)

# ================= 损坏的库文件：跳过，不影响其他实验恢复 =================
bad_id = "badbadbadbad"
with open(os.path.join(_tmp, "exp_%s.db" % bad_id), "wb") as f:
    f.write(b"this is not a sqlite database")
A.restore_experiments()
resp = client.get("/api/experiments/%s/status" % bad_id)
check("损坏·坏库文件被跳过（404）", resp.status_code == 404)
check("损坏·其他实验不受影响", committed(exp9) == 100)

# ================= 多实验并存：互不影响地恢复 =================
check("多实验·全部按原ID可访问",
      committed(exp1) == 112 and committed(exp3) == 100
      and committed(exp4) == 105 and committed(exp5) == 105
      and committed(legacy_id) == 780)

# 收尾：删除全部存活实验，确认没有请求占住准入名额
clean_ok = True
for e in (exp1, exp3, exp4, exp5, exp6, exp7, exp8, exp9, legacy_id):
    resp = client.delete("/api/experiments/%s" % e)
    clean_ok = clean_ok and resp.status_code == 200
check("收尾·全部实验删除及时完成（名额均已归还）", clean_ok)

print()
print(f"{len(fails)} 失败" if fails else "后端持久化与恢复测试全部通过")
sys.exit(1 if fails else 0)
