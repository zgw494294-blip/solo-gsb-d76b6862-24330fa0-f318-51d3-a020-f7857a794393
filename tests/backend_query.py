"""后端只读 SQL 工作台验证（无需 Flask）：用最小 stub 导入 app.py。

通过 stub 的最小路由分发驱动 **app.py 注册的实际路由**（真实经过
admit_experiment 准入门控、query_lock/query_cond 与真实 SQLite，
不另写模拟准入逻辑），覆盖只读查询的全部约定：

- 校验：SQL 必须是 1～4000 字符的单独一条 SELECT/WITH 读查询；写入、
  PRAGMA、ATTACH/DETACH、load_extension / pragma_* 函数、多条语句、
  EXPLAIN、VALUES、非字符串 body 一律 400（非法绝不入队）；
- 合法但运行时失败（无此表等）入队执行，状态 failed 并附原始 SQLite
  错误名 / 错误码；
- 成功返回列名、前 100 行与 truncated 标志；整数无损；BLOB 转
  X'…' 十六进制；NULL/REAL/TEXT 原样；
- 每实验只保留最新查询：running 再提交 409；终结后新提交替换旧 job，
  旧查询 ID 查询 / 取消 404，不影响新查询；
- 取消：中断真实执行的长查询（无界递归 CTE）；尚未开跑（worker 卡在
  打开连接之前 / 连接已开未执行）也必须取消；取消先被确认则绝不成功；
  终态只在连接关闭后发布（取消请求等到的 worker 已不再触库）；
  已终结查询再次取消返回原终态、不改结果；
- 独立只读连接：不影响两栏事务、只读开关、最近读值与读快照，不改值与
  时间线；不阻塞其他实验；
- 删除：先取消在途查询、等工作线程结束（连接已关闭）再删文件，不阻塞
  其他实验；删除后查询接口 410、未知实验 404；
- 失败 / 取消不改值及时间线；查询不跨重启保留（旧 ID 404，实验可访问）。
"""
import sys, types, os, re, tempfile, threading, time

# ---- 最小 Flask stub（与 backend_compensate.py 同一套） ----
flask = types.ModuleType("flask")


class _Resp:
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
ROUTE_TIMEOUT = 5.0
LONG_SQL = (
    "with recursive c(x) as (select 1 union all select x+1 from c) "
    "select count(*) from c"
)


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


def r_get(path):
    return route_call(client.get, path)


def r_post(path, body=None):
    return route_call(lambda: client.post(path, body))


def r_delete(path):
    return route_call(client.delete, path)


def create_exp():
    resp, exc, stuck = r_post("/api/experiments")
    assert not stuck and exc is None and resp.status_code == 200
    return resp.get_json()["id"]


def base(exp_id):
    return "/api/experiments/%s" % exp_id


def submit(exp_id, sql):
    return r_post("%s/query" % base(exp_id), {"sql": sql})


def submit_raw(exp_id, body):
    return r_post("%s/query" % base(exp_id), body)


def qget(exp_id, qid):
    return r_get("%s/query/%s" % (base(exp_id), qid))


def cancel(exp_id, qid):
    return r_post("%s/query/%s/cancel" % (base(exp_id), qid), {})


def wait_terminal(exp_id, qid, timeout=3.0):
    end = time.time() + timeout
    while True:
        resp, exc, stuck = qget(exp_id, qid)
        assert not stuck and exc is None and resp.status_code == 200
        view = resp.get_json()["query"]
        if view["status"] != "running":
            return view
        if time.time() > end:
            raise AssertionError("query did not finish: %s" % qid)
        time.sleep(0.01)


def op(exp_id, side, body):
    resp, exc, stuck = r_post("%s/conn/%s/op" % (base(exp_id), side), body)
    assert not stuck and exc is None and resp.status_code == 200, resp
    return resp.get_json()


def committed(exp_id):
    resp, exc, stuck = r_get("%s/committed" % base(exp_id))
    assert not stuck and exc is None and resp.status_code == 200
    return resp.get_json()["committed"]


def timeline(exp_id):
    resp, exc, stuck = r_get("%s/timeline" % base(exp_id))
    assert not stuck and exc is None and resp.status_code == 200
    return resp.get_json()["timeline"]


# ================= 基本成功路径 =================
e1 = create_exp()
resp, exc, stuck = submit(e1, "select val from kv where id = 1")
check("基本·提交及时200", not stuck and exc is None and resp.status_code == 200)
q = resp.get_json()["query"]
qid = q["id"]
check("基本·立即返回查询ID与running", q["status"] == "running" and len(qid) == 12, str(q))
v = wait_terminal(e1, qid)
check("基本·成功状态/列/行",
      v["status"] == "succeeded" and v["columns"] == ["val"] and v["rows"] == [[100]]
      and v["truncated"] is False, str(v))
check("基本·成功视图字段", set(v) == {"id", "status", "sql", "columns", "rows", "truncated"},
      str(set(v)))

# 类型编码：整数 / 大整数 / NULL / REAL / TEXT / BLOB / 空 BLOB
big = 9007199254740993  # 超出 JS 安全整数
op(e1, "a", {"op": "begin"})
op(e1, "a", {"op": "add", "value": str(big)})
op(e1, "a", {"op": "commit"})
resp, _, _ = submit(
    e1,
    "select val, null as n, 1.5 as f, 'hi' as s, x'4142' as b, x'' as eb from kv",
)
v = wait_terminal(e1, resp.get_json()["query"]["id"])
check("编码·大整数无损字符串/NULL/REAL/TEXT/BLOB/空BLOB",
      v["rows"] == [[str(100 + big), None, 1.5, "hi", "X'4142'", "X''"]], str(v["rows"]))

# CTE / WITH RECURSIVE / 函数 / 多列
resp, _, _ = submit(
    e1,
    "with c(x) as (select 1), d(y) as (select 2) "
    "select x, y, x+y as s, abs(-7) as z from c, d",
)
v = wait_terminal(e1, resp.get_json()["query"]["id"])
check("CTE·多CTE与函数", v["rows"] == [[1, 2, 3, 7]], str(v["rows"]))

# 可查 timeline 表（只读）
resp, _, _ = submit(e1, "select seq, side, before_val, after_val from timeline order by seq")
v = wait_terminal(e1, resp.get_json()["query"]["id"])
check("查询·可读timeline表",
      v["rows"] == [[1, "a", 100, str(100 + big)]], str(v["rows"]))

# 空结果集
resp, _, _ = submit(e1, "select val from kv where id = 999")
v = wait_terminal(e1, resp.get_json()["query"]["id"])
check("空结果·0行不截断", v["rows"] == [] and v["truncated"] is False, str(v))

# 截断：150 行 → 前 100 行 + truncated
resp, _, _ = submit(
    e1,
    "with recursive c(x) as (select 1 union all select x+1 from c where x<150) "
    "select x from c order by x",
)
v = wait_terminal(e1, resp.get_json()["query"]["id"])
check("截断·100行且truncated",
      v["status"] == "succeeded" and len(v["rows"]) == 100 and v["truncated"] is True
      and v["rows"][0] == [1] and v["rows"][-1] == [100], str(len(v["rows"])))

# 恰 100 行不截断
resp, _, _ = submit(
    e1,
    "with recursive c(x) as (select 1 union all select x+1 from c where x<100) "
    "select x from c",
)
v = wait_terminal(e1, resp.get_json()["query"]["id"])
check("截断·恰100行不截断", len(v["rows"]) == 100 and v["truncated"] is False, str(len(v["rows"])))

# ================= 提交校验：400（绝不入队） =================
bad_sqls = [
    "", "   ", "\n\t ",
    "delete from kv", "DELETE FROM kv", "insert into kv values(2)",
    "update kv set val=1", "replace into kv values(1,1)",
    "pragma query_only=0", "PRAGMA table_info(kv)",
    "attach 'x.db' as z", "detach z",
    "explain select 1", "EXPLAIN QUERY PLAN select 1",
    "values(1)", "begin", "commit", "rollback",
    "create table t(x)", "drop table kv", "vacuum",
    "select 1; select 2", "select 1;select 2", "select 1;;",
    ";select 1", "  ;  ",
    "with c as (select 1) delete from kv",
    "with c as (select 1) insert into kv values(2)",
    "with c as (select 1) update kv set val=2",
    "select load_extension('x')", "SELECT LOAD_EXTENSION('x')",
    "select * from pragma_database_list()",
    "select 'unclosed", "/* unclosed comment select 1",
    "-- just a comment\n",
]
bad_ok = True
for bad in bad_sqls:
    resp, exc, stuck = submit(e1, bad)
    if stuck or exc is not None or resp.status_code != 400:
        bad_ok = False
        print("    非400:", repr(bad), stuck, exc,
              None if resp is None else resp.status_code)
        break
check("校验·非法SQL一律400（%d 例）" % len(bad_sqls), bad_ok)

# 长度边界：4000 通过，4001 拒绝
sql4000 = ("select 1" + " " * (4000 - len("select 1")))
sql4001 = ("select 1" + " " * (4001 - len("select 1")))
resp, _, _ = submit(e1, sql4000)
check("校验·4000字符接受", resp.status_code == 200)
wait_terminal(e1, resp.get_json()["query"]["id"])
resp, _, _ = submit(e1, sql4001)
check("校验·4001字符400", resp.status_code == 400)

# 允许结尾分号 / 前后空白 / 注释
for ok_sql in ["select 1;", "  SELECT 1  ;  ", "select /* c */ 1",
               "select 1 -- tail\n", "/*前*/ with c(x) as (select 1) select x from c;"]:
    resp, exc, stuck = submit(e1, ok_sql)
    good = (not stuck and exc is None and resp.status_code == 200)
    if good:
        wait_terminal(e1, resp.get_json()["query"]["id"])
    check("校验·合法变体接受 %r" % ok_sql, good)

# body 类型错误：400
for bad_body in [None, {}, {"sql": 123}, {"sql": True}, {"sql": None},
                 {"nope": "select 1"}, [], "string"]:
    resp, exc, stuck = submit_raw(e1, bad_body)
    check("校验·body非字符串400 %r" % (bad_body,),
          not stuck and resp is not None and resp.status_code == 400)

# 非法提交不改值 / 不留下查询（最新成功查询仍可查）
check("校验·非法提交不改值", committed(e1) == str(100 + big), str(committed(e1)))

# ================= 运行时失败：failed + 原始错误名/码 =================
resp, exc, stuck = submit(e1, "select * from no_such_table")
check("失败·合法形式提交200", not stuck and resp.status_code == 200)
qfail = resp.get_json()["query"]["id"]
v = wait_terminal(e1, qfail)
check("失败·状态failed附错误",
      v["status"] == "failed" and v["error"]["name"] == "SQLITE_ERROR"
      and isinstance(v["error"]["code"], int) and v["error"]["message"],
      str(v.get("error")))
check("失败·视图含error不含结果",
      "columns" not in v and "rows" not in v and "truncated" not in v, str(set(v)))
check("失败·不改值", committed(e1) == str(100 + big), str(committed(e1)))
check("失败·不改时间线", len(timeline(e1)) == 1)

# 运行时取消（中断真实 SQL）：无界递归 CTE
resp, _, _ = submit(e1, LONG_SQL)
qrun = resp.get_json()["query"]
check("中断·长查询running", qrun["status"] == "running")
time.sleep(0.15)
t0 = time.time()
resp, exc, stuck = cancel(e1, qrun["id"])
dt = time.time() - t0
check("中断·取消及时返回", not stuck and exc is None and resp.status_code == 200
      and dt < ROUTE_TIMEOUT, "%.2fs" % dt)
v = resp.get_json()["query"]
check("中断·终态cancelled", v["status"] == "cancelled" and v.get("error") is None, str(v))
# 轮询同一 ID 仍 cancelled
resp, _, _ = qget(e1, qrun["id"])
check("中断·查询仍cancelled", resp.get_json()["query"]["status"] == "cancelled")
# 值与时间线不变
check("中断·取消不改值", committed(e1) == str(100 + big), str(committed(e1)))
check("中断·取消不改时间线", len(timeline(e1)) == 1)

# ================= running 再提交：409 =================
resp, _, _ = submit(e1, LONG_SQL)
q1 = resp.get_json()["query"]
time.sleep(0.15)
resp2, exc, stuck = submit(e1, "select 1")
check("409·running中提交409", not stuck and resp2.status_code == 409
      and "error" in resp2.get_json(), str(None if resp2 is None else resp2.status_code))
# 在途查询不受影响，仍可取消
resp, _, _ = cancel(e1, q1["id"])
check("409·在途查询仍可取消", resp.get_json()["query"]["status"] == "cancelled")

# ================= 终结后替换：旧 ID 404，不影响新查询 =================
resp, _, _ = submit(e1, "select 1")
old_id = resp.get_json()["query"]["id"]
wait_terminal(e1, old_id)
resp, _, _ = submit(e1, "select 2 as v")
new_id = resp.get_json()["query"]["id"]
check("替换·新查询ID不同", old_id != new_id)
v = wait_terminal(e1, new_id)
check("替换·新查询成功", v["rows"] == [[2]], str(v["rows"]))
resp, _, _ = qget(e1, old_id)
check("替换·旧ID查询404", resp.status_code == 404)
resp, _, _ = cancel(e1, old_id)
check("替换·旧ID取消404", resp.status_code == 404)
# 全新的随机 ID 也 404
resp, _, _ = qget(e1, "deadbeefcafe")
check("替换·未知ID 404", resp.status_code == 404)
# 新查询不受旧 ID 操作影响
resp, _, _ = qget(e1, new_id)
check("替换·新查询仍成功", resp.get_json()["query"]["status"] == "succeeded")

# failed 终结后同样可替换；替换后旧 failed ID 404
resp, _, _ = submit(e1, "select * from nope")
fid = resp.get_json()["query"]["id"]
wait_terminal(e1, fid)
resp, _, _ = submit(e1, "select 3 as v")
nid2 = resp.get_json()["query"]["id"]
wait_terminal(e1, nid2)
resp, _, _ = qget(e1, fid)
check("替换·failed旧ID 404", resp.status_code == 404)

# ================= 已终结再取消：返回原终态、不改结果 =================
resp, _, _ = submit(e1, "select 4 as v")
sid = resp.get_json()["query"]["id"]
sv = wait_terminal(e1, sid)
resp, _, _ = cancel(e1, sid)
v = resp.get_json()["query"]
check("终结取消·成功查询仍成功", v["status"] == "succeeded" and v["rows"] == [[4]], str(v))
# failed 的再取消仍 failed 且错误保留
resp, _, _ = submit(e1, "select * from nope2")
fid2 = resp.get_json()["query"]["id"]
fv = wait_terminal(e1, fid2)
resp, _, _ = cancel(e1, fid2)
v = resp.get_json()["query"]
check("终结取消·失败查询仍failed", v["status"] == "failed" and v["error"] is not None)
# cancelled 的再取消仍 cancelled
resp, _, _ = submit(e1, LONG_SQL)
cid = resp.get_json()["query"]["id"]
time.sleep(0.1)
cancel(e1, cid)
resp, _, _ = cancel(e1, cid)
check("终结取消·取消查询仍cancelled",
      resp.get_json()["query"]["status"] == "cancelled")

# ================= 尚未开跑也能取消 =================
# 注入：只把 **worker**（_run_query_job 调用栈）挡在 open_query_connection
# 之前；提交路由内的策略闸（prepare_query_gate）也调用同一函数，绝不挡它。
real_open = A.open_query_connection
pre_gate = threading.Event()
pre_release = threading.Event()


def _called_from_worker():
    frame = sys._getframe()
    while frame is not None:
        if frame.f_code.co_name == "_run_query_job":
            return True
        frame = frame.f_back
    return False


def gated_before_open(path):
    if _called_from_worker():
        pre_gate.set()
        pre_release.wait(ROUTE_TIMEOUT)
    return real_open(path)


A.open_query_connection = gated_before_open
try:
    resp, exc, stuck = submit(e1, "select 1")
    pre_id = resp.get_json()["query"]["id"]
    check("未开跑·提交后running", resp.get_json()["query"]["status"] == "running")
    pre_gate.wait(ROUTE_TIMEOUT)  # worker 已停在打开连接之前
    # 在另一线程发取消：它必须等待终态（worker 尚未被放行）
    cancel_box = {}

    def do_cancel():
        cancel_box["resp"] = client.post(
            "%s/query/%s/cancel" % (base(e1), pre_id), {}
        )

    ct = threading.Thread(target=do_cancel, daemon=True)
    ct.start()
    time.sleep(0.2)
    check("未开跑·worker放行前取消在等待", "resp" not in cancel_box)
    pre_release.set()  # 放行 worker：它开连接前复查到取消标志 → cancelled
    ct.join(ROUTE_TIMEOUT)
    resp = cancel_box.get("resp")
    check("未开跑·放行后取消返回cancelled",
          resp is not None and resp.status_code == 200
          and resp.get_json()["query"]["status"] == "cancelled",
          str(None if resp is None else resp.get_json()))
    resp, _, _ = qget(e1, pre_id)
    check("未开跑·查询终态cancelled",
          resp.get_json()["query"]["status"] == "cancelled")
finally:
    pre_release.set()
    A.open_query_connection = real_open

# 连接已打开、SQL 尚未执行时取消。sqlite3.Connection 是内置不可变类型，
# 不能给实例加属性、也不能改类方法；改为包装 open_query_connection：
# worker 拿到的连接换成一个只代理 execute/close 的对象，其 execute 在
# 真正执行用户查询前先停在闸上（PRAGMA 与策略闸不经 worker，不受影响）。
exec_gate = threading.Event()
exec_release = threading.Event()


class HeldConnection:
    def __init__(self, real):
        self._real = real
        self._held = False

    def execute(self, sql, *args, **kwargs):
        if not self._held:
            # worker 在该连接上的第一次 execute 就是用户查询
            # （PRAGMA busy_timeout/query_only 已在 open_query_connection
            # 内部、包装之前完成）
            self._held = True
            exec_gate.set()
            exec_release.wait(ROUTE_TIMEOUT)
        return self._real.execute(sql, *args, **kwargs)

    def close(self):
        self._real.close()

    def interrupt(self):
        self._real.interrupt()


def gated_worker_open(path):
    if _called_from_worker():
        return HeldConnection(real_open(path))
    return real_open(path)


A.open_query_connection = gated_worker_open
try:
    resp, _, _ = submit(e1, "select 123 as v")
    mid_id = resp.get_json()["query"]["id"]
    exec_gate.wait(ROUTE_TIMEOUT)  # worker 已开连接、停在 execute 前
    cancel_box2 = {}

    def do_cancel2():
        cancel_box2["resp"] = client.post(
            "%s/query/%s/cancel" % (base(e1), mid_id), {}
        )

    ct2 = threading.Thread(target=do_cancel2, daemon=True)
    ct2.start()
    time.sleep(0.2)
    check("未执行·execute放行前取消在等待", "resp" not in cancel_box2)
    exec_release.set()
    ct2.join(ROUTE_TIMEOUT)
    resp = cancel_box2.get("resp")
    check("未执行·连接已开未跑取消cancelled",
          resp is not None and resp.get_json()["query"]["status"] == "cancelled",
          str(None if resp is None else resp.get_json()))
finally:
    exec_release.set()
    A.open_query_connection = real_open

# ================= 取消先被确认则绝不成功（高并发多轮） =================
race_ok = True
for i in range(10):
    resp, _, _ = submit(e1, LONG_SQL)
    rid = resp.get_json()["query"]["id"]
    time.sleep(0.01 + 0.005 * i)
    resp, exc, stuck = cancel(e1, rid)
    if stuck or resp.get_json()["query"]["status"] != "cancelled":
        race_ok = False
        break
    # 之后再查也不可能变成 succeeded
    v = wait_terminal(e1, rid)
    if v["status"] != "cancelled":
        race_ok = False
        break
check("竞态·取消先确认绝不成功(10轮)", race_ok)

# ================= 独立只读连接：不影响两栏事务 / 快照 / 只读开关 =================
e2 = create_exp()
op(e2, "a", {"op": "begin"})
op(e2, "a", {"op": "add", "value": 5})  # 甲未提交 +5，持写锁
op(e2, "b", {"op": "begin"})
rb = op(e2, "b", {"op": "read"})  # 乙读快照 100
check("隔离·乙最近读值100", rb["state"]["last_read"] == 100)
resp, _, _ = r_post("%s/conn/a/readonly" % base(e2), {"enabled": True})
check("隔离·甲只读开启", resp.get_json()["ok"] is True)
# 甲持写锁期间查询照常（读已提交值 100，不等写锁）
resp, _, _ = submit(e2, "select val from kv")
check("隔离·查询及时提交（不等写锁）", resp.status_code == 200)
v = wait_terminal(e2, resp.get_json()["query"]["id"])
check("隔离·查询见已提交值100而非未提交105", v["rows"] == [[100]], str(v["rows"]))
st = client.get("%s/status" % base(e2)).get_json()
check("隔离·甲事务仍开启", st["a"]["in_transaction"] is True)
check("隔离·甲只读仍开启", st["a"]["readonly"] is True)
check("隔离·乙事务仍开启且读值100",
      st["b"]["in_transaction"] is True and st["b"]["last_read"] == 100)
# 乙再读仍见 100（查询连接不改变乙的快照）
rb = op(e2, "b", {"op": "read"})
check("隔离·乙快照仍100", rb["state"]["last_read"] == 100)
# 查询不能写（即使绕过词法也不可能）：实验库值未变
check("隔离·未提交值不可见/未被改", committed(e2) == 100)
op(e2, "a", {"op": "rollback"})
op(e2, "b", {"op": "rollback"})
r_post("%s/conn/a/readonly" % base(e2), {"enabled": False})

# ================= 不阻塞其他实验 =================
e3 = create_exp()
resp, _, _ = submit(e3, LONG_SQL)
long_id = resp.get_json()["query"]["id"]
time.sleep(0.15)
# e2 在 e3 长查询期间完整写入流程不受影响
op(e2, "a", {"op": "begin"})
op(e2, "a", {"op": "add", "value": 9})
op(e2, "a", {"op": "commit"})
check("独立·其他实验写入109", committed(e2) == 109)
# e2 的查询也可并发
resp, _, _ = submit(e2, "select val from kv")
v = wait_terminal(e2, resp.get_json()["query"]["id"])
check("独立·其他实验查询109", v["rows"] == [[109]])
# e3 的长查询仍在跑（409），随后取消不受影响
resp, _, _ = submit(e3, "select 1")
check("独立·e3长查询仍409", resp.status_code == 409)
resp, _, _ = cancel(e3, long_id)
check("独立·e3长查询可取消", resp.get_json()["query"]["status"] == "cancelled")

# ================= 删除：先取消在途查询、等连接关闭再删文件 =================
e4 = create_exp()
resp, _, _ = submit(e4, LONG_SQL)
del_q = resp.get_json()["query"]["id"]
time.sleep(0.15)
del_box = {}


def do_delete():
    del_box["resp"] = client.delete(base(e4))


dt = threading.Thread(target=do_delete, daemon=True)
t0 = time.time()
dt.start()
time.sleep(0.2)
# 删除开始后新请求（含查询接口）410
resp, _, _ = qget(e4, del_q)
check("删除·等待期查询接口410", resp is not None and resp.status_code == 410)
resp, _, _ = submit(e4, "select 1")
check("删除·等待期提交410", resp.status_code == 410)
dt.join(ROUTE_TIMEOUT)
check("删除·及时完成", "resp" in del_box and del_box["resp"].status_code == 200
      and time.time() - t0 < ROUTE_TIMEOUT)
for suffix in ("", A.WAL_SUFFIX, A.SHM_SUFFIX):
    check("删除·文件已移除%s" % suffix,
          not os.path.exists(os.path.join(A.DATA_DIR, "exp_%s.db%s" % (e4, suffix))))
resp, _, _ = qget(e4, del_q)
check("删除·删除后查询410", resp.status_code == 410)
resp, _, _ = cancel(e4, del_q)
check("删除·删除后取消410", resp.status_code == 410)
# 删除不影响其他实验（e1/e2/e3 仍可查可查值）
check("删除·其他实验仍可查值", committed(e2) == 109)

# 未知实验：查询接口 404
resp, _, _ = qget("000000000000", "anything")
check("删除·未知实验查询404", resp.status_code == 404)
resp, _, _ = r_post("/api/experiments/000000000000/query/x/cancel", {})
check("删除·未知实验取消404", resp.status_code == 404)

# 删除在途查询时两栏有未提交事务也照常（回滚 + 查终态在文件删除前）
e5 = create_exp()
op(e5, "a", {"op": "begin"})
op(e5, "a", {"op": "add", "value": 11})  # 未提交
resp, _, _ = submit(e5, LONG_SQL)
q5 = resp.get_json()["query"]["id"]
time.sleep(0.15)
resp, exc, stuck = r_delete(base(e5))
check("删除·有未提交事务+在途查询时删除及时",
      not stuck and resp.status_code == 200)
check("删除·库文件已移除", not os.path.exists(os.path.join(A.DATA_DIR, "exp_%s.db" % e5)))

# ================= 查询不跨重启保留 =================
e6 = create_exp()
op(e6, "a", {"op": "begin"})
op(e6, "a", {"op": "add", "value": 3})
op(e6, "a", {"op": "commit"})  # 时间线 #1
resp, _, _ = submit(e6, "select val from kv")
old_q = resp.get_json()["query"]["id"]
wait_terminal(e6, old_q)
# 模拟重启：丢弃内存对象并重新扫描数据目录
with A.experiments_lock:
    exp_obj = A.experiments.pop(e6, None)
    A.deleted_experiments.clear()
if exp_obj is not None:
    for side in ("a", "b"):
        try:
            exp_obj.conns[side].close()
        except Exception:
            pass
    try:
        exp_obj.observer.close()
    except Exception:
        pass
A.restore_experiments()
resp, _, _ = r_get("%s/committed" % base(e6))
check("重启·实验按原ID可访问", resp.status_code == 200 and resp.get_json()["committed"] == 103)
resp, _, _ = qget(e6, old_q)
check("重启·旧查询ID 404（不跨重启保留）", resp.status_code == 404)
resp, _, _ = cancel(e6, old_q)
check("重启·旧取消404", resp.status_code == 404)
# 新查询照常
resp, _, _ = submit(e6, "select val from kv")
v = wait_terminal(e6, resp.get_json()["query"]["id"])
check("重启·新查询成功", v["rows"] == [[103]], str(v["rows"]))

# ================= 收尾：删除全部实验，名额均归还 =================
clean_ok = True
for e in (e1, e2, e3, e6):
    resp, exc, stuck = r_delete(base(e))
    clean_ok = clean_ok and not stuck and resp.status_code == 200
check("收尾·全部实验删除及时完成（查询线程均已收尾）", clean_ok)

print()
print(f"{len(fails)} 失败" if fails else "后端只读查询测试全部通过")
sys.exit(1 if fails else 0)
