"""后端脚本状态机 + 实际路由验证（无需 Flask）：用最小 stub 导入 app.py。

第一部分直接在真实 SQLite 上驱动 Experiment 的装载 / 推进 / 跳过 / 取消 / 互斥。

第二部分通过 stub 的最小路由分发驱动 **app.py 注册的实际路由**（真实经过
admit_experiment 准入门控、script_lock / conn_lock 与真实 SQLite，不另写
模拟准入逻辑），覆盖手工操作 / 只读切换与脚本装载的互斥与交错：

- 无有效脚本时合法 BEGIN / 只读切换及时返回（修复前路由在持有 script_lock
  时再次获取同一把非可重入锁，请求永久卡住）；
- 脚本有效期间手工请求及时 409 且不执行；被拒请求归还准入名额，
  脚本查询 / 推进 / 删除不被阻塞；
- 完成 / 取消后恢复手工操作，系统不自动提交 / 回滚；
- BEGIN 先执行则装载 409，装载先成功则手工请求 409；
  并发交错下恰好一方成功；只读切换绝不在脚本有效期执行。

第三部分用 run_op 包装器（RunOpGate）把指定栏的指定操作按在「在途」
状态，由测试手动放行，确定性地控制实际路由的执行顺序，覆盖两栏独立
进展与装载交错（修复前两栏手工请求被整段持有的 script_lock 串行化，
甲的在途请求不结束，乙的 BEGIN / 只读切换就被卡住）：

- 无有效脚本时，甲的读值在途，乙的 BEGIN / 只读切换不等甲结束即完成；
  两栏互换同样适用；同栏请求仍串行；
- 装载等待在途手工请求收尾：在途 BEGIN 先成功则装载 409，
  绝不出现「装载成功后手工 BEGIN 才开启事务」；
- 装载等待期间另一栏手工请求照常推进（其 BEGIN 先成功则装载 409）；
  在途读值不留事务时，装载等其收尾后正常成功，此后手工请求 409。
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

check_eq = None

fails = []
def check(name, cond, extra=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond: fails.append(name)

# 校验
v = A.Experiment.validate_steps
check("非数组拒绝", v({"a":1}) is not None)
check("空数组拒绝", v([]) is not None)
check("51步拒绝", v([{"side":"a","op":"begin"}]*51) is not None)
check("50步通过", v([{"side":"a","op":"begin"}]*50) is None)
check("1步通过", v([{"side":"b","op":"read"}]) is None)
check("坏side", v([{"side":"c","op":"begin"}]) is not None)
check("坏op", v([{"side":"a","op":"nope"}]) is not None)
check("add缺value", v([{"side":"a","op":"add"}]) is not None)
check("add非整数", v([{"side":"a","op":"add","value":1.5}]) is not None)
check("add布尔拒绝", v([{"side":"a","op":"add","value":True}]) is not None)
check("add超2^62", v([{"side":"a","op":"add","value":2**62+1}]) is not None)
check("add=2^62通过", v([{"side":"a","op":"add","value":2**62}]) is None)
check("add大整数字符串通过", v([{"side":"a","op":"add","value":"9007199254740993"}]) is None)
check("非对象step拒绝", v(["begin"]) is not None)
check("begin带多余字段宽容", v([{"side":"a","op":"begin","value":1}]) is None)

exp = A.Experiment("test1")

# 装载 README 示例：甲 begin/+5, 乙 begin/+7(撞锁), 甲commit, 乙+7, 乙commit
steps = [
    {"side":"a","op":"begin"},
    {"side":"a","op":"add","value":5},
    {"side":"b","op":"begin"},
    {"side":"b","op":"add","value":7},
    {"side":"a","op":"commit"},
    {"side":"b","op":"add","value":7},
    {"side":"b","op":"commit"},
]
view, rej = exp.load_script(steps)
check("装载成功", rej is None)
sid = view["id"]
check("步号从1开始", view["step"] == 1 and view["status"] == "active", str(view["step"]))

# active 时手工互斥
check("active 标记手工锁定", exp.script_active() is True)

# 手工 run_op 不直接被 Experiment 拦（路由层拦），这里模拟路由检查：
# 直接调 run_op 甲 begin 应该报"事务已开启"——脚本第1步已 begin? 尚未推进，
# 所以应成功开启第二个 begin? 不，脚本还没执行，连接空闲；这只是状态机测试，
# 跳过手工 run_op，聚焦脚本流。

# 过期 ID / 错步号
_, ok, r = exp.advance_script("WRONG", 1)
check("错误脚本ID拒绝(409语义)", r is not None and ok is False, str(r))
_, ok, r = exp.advance_script(sid, 2)
check("错误步号拒绝", r is not None)
check("过期拒绝无记录", len(exp.script_state()["records"]) == 0)

def adv(step, expect_ok=True):
    vw, ok, r = exp.advance_script(sid, step)
    assert r is None, r
    return vw, ok

vw, ok = adv(1)  # 甲 begin
check("步1 甲begin成功", ok and vw["step"] == 2)
vw, ok = adv(2)  # 甲 +5
check("步2 甲+5成功", ok and vw["step"] == 3)
vw, ok = adv(3)  # 乙 begin
check("步3 乙begin成功", ok and vw["step"] == 4)
vw, ok = adv(4)  # 乙 +7 撞甲写锁
recs = vw["records"]
check("步4 乙+7失败", ok is False and vw["step"] == 4)
check("记录失败原始错误名SQLITE_BUSY", recs[-1]["error"] and recs[-1]["error"]["name"] == "SQLITE_BUSY", str(recs[-1]))
check("错误码5", recs[-1]["error"]["code"] == 5)
check("失败结果failure", recs[-1]["result"] == "failure")
# 甲仍未提交：乙事务保持开启，系统未代回滚
check("失败不代回滚：乙仍在事务", exp.conns["b"].in_transaction)
check("失败后甲仍持锁(在事务)", exp.conns["a"].in_transaction)

# 再次推进同一步号 4：仍撞锁（甲未提交）
vw, ok, r = exp.advance_script(sid, 4)
check("重试步4仍失败停住", r is None and ok is False and vw["step"] == 4)
check("重试也产生记录", len(vw["records"]) == 5)

# 跳过步4
vw, ok, r = exp.skip_script(sid, 4)
check("跳过步4成功", r is None and ok and vw["step"] == 5)
check("跳过记录skipped", vw["records"][-1]["result"] == "skipped" and vw["records"][-1]["error"] is None)

# 跳过错误步号
_, ok, r = exp.skip_script(sid, 4)
check("跳过错步号拒绝", r is not None)

vw, ok = adv(5)  # 甲 commit
check("步5 甲commit成功", ok and vw["step"] == 6)
check("甲提交落地=105", exp.committed_value() == 105, str(exp.committed_value()))
# 时间线记录一条
tl = exp.timeline_records()
check("时间线1条 100->105 [5]", len(tl) == 1 and tl[0]["before"] == 100 and tl[0]["after"] == 105 and tl[0]["deltas"] == [5], str(tl))

vw, ok = adv(6)  # 乙在原事务 +7（写锁已释放，无快照，应成功）
check("步6 乙原事务+7成功", ok and vw["step"] == 7)
vw, ok = adv(7)  # 乙 commit
check("步7 乙commit成功 脚本finished", ok and vw["status"] == "finished" and vw["step"] is None)
check("finished后不锁手工", exp.script_active() is False)
check("最终已提交112", exp.committed_value() == 112, str(exp.committed_value()))
tl = exp.timeline_records()
check("时间线2条 105->112 [7]", len(tl) == 2 and tl[1]["before"] == 105 and tl[1]["after"] == 112)

# 完成后过期动作
_, ok, r = exp.advance_script(sid, 8)
check("finished后推进拒绝", r is not None)
_, ok, r = exp.cancel_script(sid, 8)
check("finished后取消拒绝", r is not None)

# 完成后两栏均无事务：可以装载新脚本
view2, rej = exp.load_script([{"side":"a","op":"read"}])
check("完成且无事务:装载成功", rej is None)
sid2 = view2["id"]
check("新脚本ID不同于旧", sid2 != sid)
# 旧脚本的过期请求不得影响新脚本
_, ok, r = exp.advance_script(sid, 1)
check("旧ID动作不执行不影响新脚本", r is not None)
check("新脚本仍停在步1", exp.script_state()["step"] == 1)

# 取消：在第1步停止；不自动提交/回滚（read 不开写事务；另测 begin 后取消）
vw, ok, r = exp.cancel_script(sid2, 1)
check("取消成功", ok and vw["status"] == "cancelled" and vw["step"] == 1)
check("取消后手工恢复", exp.script_active() is False)

# 取消不自动回滚：begin 后取消，事务仍开着
v3, rej = exp.load_script([{"side":"a","op":"begin"},{"side":"a","op":"read"}])
check("装载begin脚本", rej is None)
exp.advance_script(v3["id"], 1)
exp.cancel_script(v3["id"], 2)
check("取消不自动回滚：甲事务仍开启", exp.conns["a"].in_transaction is True)
# 此时有未结事务：再装载被拒
v4, rej = exp.load_script([{"side":"a","op":"read"}])
check("有事务时装载拒绝", rej is not None)
# 手工提交（脚本已取消，手工解锁）提交后可装载
A.run_op(exp, "a", "commit", None)
v5, rej = exp.load_script([{"side":"a","op":"read"}])
check("手工收尾事务后可再装载", rej is None)

# ================= 实际路由：手工操作 / 只读切换 与脚本装载 =================
# 以下全部通过 TestClient 调用 app.py 注册的实际视图函数，
# 真实经过 admit_experiment 准入门控与全部实验级锁。
client = TestClient(A.app)
ROUTE_TIMEOUT = 5.0  # 正常路由调用为毫秒级；超时即视为「卡住」回归


class _NullDict(dict):
    """缺失键返回空 _NullDict：请求卡住时让后续断言自然失败而不是抛异常。"""

    def __missing__(self, key):
        return _NullDict()


class _StuckResp:
    """请求卡住 / 视图抛异常时的占位响应：status_code=0，JSON 体为空。"""

    status_code = 0

    def get_json(self):
        return _NullDict()


_STUCK = _StuckResp()


def route_call(fn, *args, **kwargs):
    """在守护线程中执行路由调用并限时等待。

    修复前手工操作 / 只读切换会在持有 script_lock 时再次获取同一把
    非可重入锁而永久卡住；这里把「卡住」变成明确的测试失败（占位响应
    让后续断言自然不通过），而不是让整个测试进程挂起或崩溃。
    返回 (response, exception, timed_out)。
    """
    box = {}

    def runner():
        try:
            box["resp"] = fn(*args, **kwargs)
        except Exception as exc:  # 视图抛错同样算失败，但不能卡住测试
            box["exc"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(ROUTE_TIMEOUT)
    if t.is_alive():
        return _STUCK, None, True
    if "exc" in box:
        return _STUCK, box["exc"], False
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


def op_url(exp_id, side):
    return "/api/experiments/%s/conn/%s/op" % (exp_id, side)


def ro_url(exp_id, side):
    return "/api/experiments/%s/conn/%s/readonly" % (exp_id, side)


def script_url(exp_id, action=""):
    return "/api/experiments/%s/script%s" % (exp_id, action)


# ---- 无有效脚本：合法 BEGIN / 只读切换按原事务规则及时执行 ----
exp1 = create_exp()
resp, exc, stuck = r_post(op_url(exp1, "a"), {"op": "begin"})
check("路由·无脚本：手工BEGIN及时返回200", not stuck and exc is None and resp.status_code == 200)
body = resp.get_json()
check("路由·无脚本：BEGIN成功开启事务", body["ok"] is True and body["state"]["in_transaction"] is True)
resp, exc, stuck = r_post(ro_url(exp1, "a"), {"enabled": True})
check("路由·无脚本：只读切换及时返回200", not stuck and exc is None and resp.status_code == 200)
body = resp.get_json()
check("路由·只读开启生效", body["ok"] is True and body["state"]["readonly"] is True)
# 只读在该栏原连接真实生效：事务内加法被 SQLITE_READONLY(8) 拒绝
resp, exc, stuck = r_post(op_url(exp1, "a"), {"op": "add", "value": 7})
body = resp.get_json()
check("路由·只读下事务内加法SQLITE_READONLY(8)",
      resp.status_code == 200 and body["ok"] is False and body["error"]["code"] == 8,
      str(body.get("error")))
resp, exc, stuck = r_post(ro_url(exp1, "a"), {"enabled": False})
check("路由·关闭只读及时返回200", not stuck and resp.status_code == 200 and resp.get_json()["ok"] is True)
resp, exc, stuck = r_post(op_url(exp1, "a"), {"op": "add", "value": 7})
check("路由·关闭后原事务加法成功", resp.get_json()["ok"] is True)
resp, exc, stuck = r_post(op_url(exp1, "a"), {"op": "commit"})
check("路由·提交成功", resp.get_json()["ok"] is True)
resp, exc, stuck = r_get("/api/experiments/%s/committed" % exp1)
check("路由·已提交值107", not stuck and resp.get_json()["committed"] == 107)

# ---- 脚本有效期间：手工请求及时 409 且不执行；查询 / 推进 / 删除不被阻塞 ----
exp2 = create_exp()
resp, exc, stuck = r_post(script_url(exp2),
                          [{"side": "a", "op": "begin"},
                           {"side": "a", "op": "add", "value": 5},
                           {"side": "a", "op": "commit"}])
check("路由·装载脚本200", not stuck and resp.status_code == 200)
sid_e2 = resp.get_json()["script"]["id"]
resp, exc, stuck = r_post(op_url(exp2, "a"), {"op": "begin"})
check("路由·active：手工BEGIN及时409", not stuck and exc is None and resp.status_code == 409)
resp, exc, stuck = r_post(op_url(exp2, "b"), {"op": "begin"})
check("路由·active：另一栏手工BEGIN也409", not stuck and resp.status_code == 409)
resp, exc, stuck = r_post(ro_url(exp2, "a"), {"enabled": True})
check("路由·active：只读切换及时409", not stuck and resp.status_code == 409)
resp, exc, stuck = r_get("/api/experiments/%s/status" % exp2)
st = resp.get_json()
check("路由·409未执行：只读仍关闭、乙无事务",
      st["a"]["readonly"] is False and st["b"]["in_transaction"] is False)
# 被拒请求已归还准入名额：脚本查询 / 推进照常
resp, exc, stuck = r_get(script_url(exp2))
check("路由·active：脚本查询不被阻塞",
      not stuck and resp.status_code == 200 and resp.get_json()["script"]["step"] == 1)
resp, exc, stuck = r_post(script_url(exp2, "/advance"), {"script_id": sid_e2, "step": 1})
check("路由·active：推进不被阻塞", not stuck and resp.status_code == 200 and resp.get_json()["ok"] is True)
# 删除照常完成：没有被卡住请求占住的名额，begin_delete 立即通过
resp, exc, stuck = r_delete("/api/experiments/%s" % exp2)
check("路由·active：删除及时完成", not stuck and resp.status_code == 200 and resp.get_json()["deleted"] is True)
resp, exc, stuck = r_get("/api/experiments/%s/status" % exp2)
check("路由·删除后410", resp.status_code == 410)

# ---- 脚本完成后恢复手工操作 ----
exp3 = create_exp()
resp, _, _ = r_post(script_url(exp3),
                    [{"side": "a", "op": "begin"},
                     {"side": "a", "op": "add", "value": 5},
                     {"side": "a", "op": "commit"}])
sid_e3 = resp.get_json()["script"]["id"]
for n in (1, 2, 3):
    resp, exc, stuck = r_post(script_url(exp3, "/advance"), {"script_id": sid_e3, "step": n})
    assert not stuck and resp.get_json()["ok"] is True
check("路由·脚本走完finished", resp.get_json()["script"]["status"] == "finished")
resp, exc, stuck = r_post(op_url(exp3, "b"), {"op": "begin"})
check("路由·finished后手工BEGIN恢复", not stuck and resp.status_code == 200 and resp.get_json()["ok"] is True)
r_post(op_url(exp3, "b"), {"op": "rollback"})
resp, exc, stuck = r_post(ro_url(exp3, "a"), {"enabled": True})
check("路由·finished后只读切换恢复", not stuck and resp.status_code == 200 and resp.get_json()["ok"] is True)
r_post(ro_url(exp3, "a"), {"enabled": False})
resp, _, _ = r_get("/api/experiments/%s/committed" % exp3)
check("路由·脚本提交落地105", resp.get_json()["committed"] == 105)

# ---- 取消后恢复手工操作；不自动提交 / 回滚 ----
exp4 = create_exp()
resp, _, _ = r_post(script_url(exp4),
                    [{"side": "a", "op": "begin"},
                     {"side": "a", "op": "add", "value": 9},
                     {"side": "a", "op": "commit"}])
sid_e4 = resp.get_json()["script"]["id"]
r_post(script_url(exp4, "/advance"), {"script_id": sid_e4, "step": 1})
r_post(script_url(exp4, "/advance"), {"script_id": sid_e4, "step": 2})
resp, exc, stuck = r_post(script_url(exp4, "/cancel"), {"script_id": sid_e4, "step": 3})
check("路由·取消成功", not stuck and resp.status_code == 200
      and resp.get_json()["script"]["status"] == "cancelled")
resp, _, _ = r_get("/api/experiments/%s/committed" % exp4)
check("路由·取消不自动提交", resp.get_json()["committed"] == 100)
resp, _, _ = r_get("/api/experiments/%s/status" % exp4)
check("路由·取消不自动回滚", resp.get_json()["a"]["in_transaction"] is True)
resp, exc, stuck = r_post(op_url(exp4, "a"), {"op": "commit"})
check("路由·取消后手工提交恢复", not stuck and resp.status_code == 200 and resp.get_json()["ok"] is True)
resp, _, _ = r_get("/api/experiments/%s/committed" % exp4)
check("路由·手工提交落地109", resp.get_json()["committed"] == 109)
resp, _, _ = r_post(script_url(exp4, "/advance"), {"script_id": sid_e4, "step": 3})
check("路由·已取消脚本推进409", resp.status_code == 409)

# ---- 互斥顺序：BEGIN 先执行则装载拒绝；装载先成功则手工请求拒绝 ----
exp5 = create_exp()
resp, _, _ = r_post(op_url(exp5, "a"), {"op": "begin"})
check("路由·先手工BEGIN 200", resp.status_code == 200 and resp.get_json()["ok"] is True)
resp, exc, stuck = r_post(script_url(exp5), [{"side": "b", "op": "begin"}])
check("路由·BEGIN在先：装载409", not stuck and resp.status_code == 409)
r_post(op_url(exp5, "a"), {"op": "rollback"})
resp, _, _ = r_post(script_url(exp5), [{"side": "b", "op": "begin"}])
check("路由·回滚后装载200", resp.status_code == 200)
sid_e5 = resp.get_json()["script"]["id"]
resp, exc, stuck = r_post(op_url(exp5, "b"), {"op": "begin"})
check("路由·装载在先：手工BEGIN 409", not stuck and resp.status_code == 409)
resp, exc, stuck = r_post(ro_url(exp5, "b"), {"enabled": True})
check("路由·装载在先：只读切换 409", not stuck and resp.status_code == 409)
r_post(script_url(exp5, "/cancel"), {"script_id": sid_e5, "step": 1})
resp, _, _ = r_post(op_url(exp5, "b"), {"op": "begin"})
check("路由·取消后手工恢复200", resp.status_code == 200 and resp.get_json()["ok"] is True)
r_post(op_url(exp5, "b"), {"op": "rollback"})

# ---- 并发交错：手工 BEGIN 与装载互斥，恰好一方成功，删除可完成 ----
race_ok = True
for _ in range(20):
    exp_r = create_exp()
    barrier = threading.Barrier(2)
    results = {}

    def do_begin():
        barrier.wait()
        results["begin"] = client.post(op_url(exp_r, "a"), {"op": "begin"})

    def do_load():
        barrier.wait()
        results["load"] = client.post(script_url(exp_r), [{"side": "b", "op": "begin"}])

    threads = [threading.Thread(target=do_begin, daemon=True),
               threading.Thread(target=do_load, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(ROUTE_TIMEOUT)
    if any(t.is_alive() for t in threads):
        race_ok = False
        break
    codes = sorted([results["begin"].status_code, results["load"].status_code])
    if codes != [200, 409]:
        race_ok = False
        break
    if results["begin"].status_code == 200:
        # BEGIN 先执行：装载被拒；手工回滚收尾
        client.post(op_url(exp_r, "a"), {"op": "rollback"})
    else:
        # 装载先成功：手工 BEGIN 被拒；取消脚本收尾
        sid_r = results["load"].get_json()["script"]["id"]
        client.post(script_url(exp_r, "/cancel"), {"script_id": sid_r, "step": 1})
    if client.delete("/api/experiments/%s" % exp_r).status_code != 200:
        race_ok = False
        break
check("路由·并发BEGIN/装载恰好一方成功且删除可完成(20轮)", race_ok)

# ---- 并发交错：只读切换绝不在脚本有效期执行 ----
ro_race_ok = True
for _ in range(20):
    exp_r = create_exp()
    barrier = threading.Barrier(2)
    results = {}

    def do_ro():
        barrier.wait()
        results["ro"] = client.post(ro_url(exp_r, "a"), {"enabled": True})

    def do_load2():
        barrier.wait()
        results["load"] = client.post(script_url(exp_r), [{"side": "b", "op": "begin"}])

    threads = [threading.Thread(target=do_ro, daemon=True),
               threading.Thread(target=do_load2, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(ROUTE_TIMEOUT)
    if any(t.is_alive() for t in threads):
        ro_race_ok = False
        break
    ro, ld = results["ro"], results["load"]
    st = client.get("/api/experiments/%s/status" % exp_r).get_json()
    if ld.status_code == 200 and ro.status_code == 409:
        # 装载先成功：切换被拒且未执行
        ro_race_ok = ro_race_ok and st["a"]["readonly"] is False
        sid_r = ld.get_json()["script"]["id"]
        client.post(script_url(exp_r, "/cancel"), {"script_id": sid_r, "step": 1})
    elif ro.status_code == 200 and ld.status_code == 200:
        # 切换先完成（不开事务、不妨碍装载）：随后装载成功，二者均有效
        ro_race_ok = ro_race_ok and st["a"]["readonly"] is True
        sid_r = ld.get_json()["script"]["id"]
        client.post(script_url(exp_r, "/cancel"), {"script_id": sid_r, "step": 1})
    else:
        # 其他组合不可能：ro 409 仅当脚本已 active（装载必已成功）
        ro_race_ok = False
    client.delete("/api/experiments/%s" % exp_r)
    if not ro_race_ok:
        break
check("路由·并发只读/装载：切换绝不在脚本有效期执行(20轮)", ro_race_ok)

# ================= 两栏手工请求互不等待 + 与装载交错 =================
# RunOpGate 把指定栏的指定操作按在「在途」状态（已准入、尚未执行），
# 由测试手动放行，从而确定性地控制实际路由的执行顺序。修复前手工路由在
# 「复查 + 执行」整段持有实验级 script_lock：甲的在途请求不结束，乙的
# BEGIN / 只读切换就被卡住（本节的及时性断言在修复前会失败）。
real_run_op = A.run_op


class RunOpGate:
    """按 (exp_id, side, op) 拦截 run_op：在本栏连接临界区内暂停，由测试放行。

    暂停期间与真实执行一样占用本栏 conn_lock——这正是「请求在途」的含义：
    同栏请求会被如实串行，另一栏与装载的后续行为则由被测代码决定。
    """

    def __init__(self, exp_id, side, op):
        self.exp_id = exp_id
        self.side = side
        self.op = op
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, exp, side, op, value):
        if exp.id == self.exp_id and side == self.side and op == self.op:
            with exp.conn_locks[side]:
                self.entered.set()
                # 限时等待：即使测试断言失败也不会永久卡住测试进程
                self.release.wait(ROUTE_TIMEOUT)
        return real_run_op(exp, side, op, value)


def gated(gate, body):
    """安装 gate 执行测试体，最后恢复真实 run_op。"""
    A.run_op = gate
    try:
        body()
    finally:
        A.run_op = real_run_op


def bg_call(fn, *args, **kwargs):
    """在守护线程中发起路由调用，立即返回 (thread, box) 由测试控制节奏。"""
    box = {}

    def runner():
        try:
            box["resp"] = fn(*args, **kwargs)
        except Exception as exc:  # 视图抛错：记录而不卡住测试
            box["exc"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    return t, box


def bg_done(t, box):
    return not t.is_alive() and "resp" in box


# ---- 甲读值在途：乙的 BEGIN / 只读切换不等甲结束即完成 ----
exp6 = create_exp()
gate = RunOpGate(exp6, "a", "read")


def two_panel_a_busy():
    t_rd, box_rd = bg_call(client.post, op_url(exp6, "a"), {"op": "read"})
    try:
        check("两栏·甲读值在途", gate.entered.wait(ROUTE_TIMEOUT))
        resp, exc, stuck = r_post(op_url(exp6, "b"), {"op": "begin"})
        check("两栏·甲读值在途：乙BEGIN及时200", not stuck and exc is None
              and resp.status_code == 200 and resp.get_json()["ok"] is True)
        resp, exc, stuck = r_post(ro_url(exp6, "b"), {"enabled": True})
        body = resp.get_json()
        check("两栏·甲读值在途：乙只读切换及时200", not stuck and exc is None
              and resp.status_code == 200 and body["ok"] is True
              and body["state"]["readonly"] is True)
        check("两栏·乙完成时甲读值仍在途", t_rd.is_alive())
    finally:
        gate.release.set()
        t_rd.join(ROUTE_TIMEOUT)
    check("两栏·放行后甲读值完成200", bg_done(t_rd, box_rd)
          and box_rd["resp"].status_code == 200
          and box_rd["resp"].get_json()["ok"] is True)
    r_post(op_url(exp6, "b"), {"op": "rollback"})
    r_post(ro_url(exp6, "b"), {"enabled": False})
    resp, exc, stuck = r_delete("/api/experiments/%s" % exp6)
    check("两栏·实验删除及时完成（名额已归还）",
          not stuck and resp.status_code == 200)


gated(gate, two_panel_a_busy)

# ---- 两栏互换：乙读值在途，甲的 BEGIN / 只读切换不等乙结束 ----
exp7 = create_exp()
gate = RunOpGate(exp7, "b", "read")


def two_panel_b_busy():
    t_rd, box_rd = bg_call(client.post, op_url(exp7, "b"), {"op": "read"})
    try:
        check("互换·乙读值在途", gate.entered.wait(ROUTE_TIMEOUT))
        resp, exc, stuck = r_post(op_url(exp7, "a"), {"op": "begin"})
        check("互换·乙读值在途：甲BEGIN及时200", not stuck and exc is None
              and resp.status_code == 200 and resp.get_json()["ok"] is True)
        resp, exc, stuck = r_post(ro_url(exp7, "a"), {"enabled": True})
        check("互换·乙读值在途：甲只读切换及时200", not stuck and exc is None
              and resp.status_code == 200 and resp.get_json()["ok"] is True)
        check("互换·甲完成时乙读值仍在途", t_rd.is_alive())
    finally:
        gate.release.set()
        t_rd.join(ROUTE_TIMEOUT)
    check("互换·放行后乙读值完成200", bg_done(t_rd, box_rd)
          and box_rd["resp"].status_code == 200
          and box_rd["resp"].get_json()["ok"] is True)
    r_post(op_url(exp7, "a"), {"op": "rollback"})
    r_post(ro_url(exp7, "a"), {"enabled": False})
    resp, exc, stuck = r_delete("/api/experiments/%s" % exp7)
    check("互换·实验删除及时完成（名额已归还）",
          not stuck and resp.status_code == 200)


gated(gate, two_panel_b_busy)

# ---- 同栏请求仍串行：甲读值在途时，甲的 BEGIN 等它收尾 ----
exp8 = create_exp()
gate = RunOpGate(exp8, "a", "read")


def same_panel_serial():
    t_rd, box_rd = bg_call(client.post, op_url(exp8, "a"), {"op": "read"})
    try:
        check("同栏·甲读值在途", gate.entered.wait(ROUTE_TIMEOUT))
        t_bg, box_bg = bg_call(client.post, op_url(exp8, "a"), {"op": "begin"})
        t_bg.join(0.5)
        check("同栏·读值在途时本栏BEGIN等待（仍串行）", t_bg.is_alive())
    finally:
        gate.release.set()
        t_rd.join(ROUTE_TIMEOUT)
        t_bg.join(ROUTE_TIMEOUT)
    check("同栏·两请求最终都完成", bg_done(t_rd, box_rd) and bg_done(t_bg, box_bg)
          and box_rd["resp"].status_code == 200
          and box_bg["resp"].status_code == 200
          and box_bg["resp"].get_json()["ok"] is True)
    r_post(op_url(exp8, "a"), {"op": "rollback"})
    r_delete("/api/experiments/%s" % exp8)


gated(gate, same_panel_serial)

# ---- 装载等待在途手工 BEGIN：BEGIN 先成功则装载 409 ----
# 关键不变量：绝不出现「装载成功后手工 BEGIN 才开启事务」。
exp9 = create_exp()
gate = RunOpGate(exp9, "a", "begin")


def load_waits_manual_begin():
    t_bg, box_bg = bg_call(client.post, op_url(exp9, "a"), {"op": "begin"})
    t_ld, box_ld = None, None
    try:
        check("装载交错·手工BEGIN在途", gate.entered.wait(ROUTE_TIMEOUT))
        t_ld, box_ld = bg_call(client.post, script_url(exp9),
                               [{"side": "b", "op": "begin"}])
        t_ld.join(0.5)
        check("装载交错·装载等待在途手工（未抢先成功）", t_ld.is_alive())
    finally:
        gate.release.set()
        t_bg.join(ROUTE_TIMEOUT)
        if t_ld is not None:
            t_ld.join(ROUTE_TIMEOUT)
    check("装载交错·在途BEGIN完成200", bg_done(t_bg, box_bg)
          and box_bg["resp"].status_code == 200
          and box_bg["resp"].get_json()["ok"] is True)
    check("装载交错·BEGIN先成功：装载409", bg_done(t_ld, box_ld)
          and box_ld["resp"].status_code == 409)
    resp, _, _ = r_get(script_url(exp9))
    check("装载交错·装载被拒后无有效脚本", resp.get_json()["script"] is None)
    r_post(op_url(exp9, "a"), {"op": "rollback"})
    resp, exc, stuck = r_delete("/api/experiments/%s" % exp9)
    check("装载交错·实验删除及时完成（名额已归还）",
          not stuck and resp.status_code == 200)


gated(gate, load_waits_manual_begin)

# ---- 装载等待期间另一栏照常推进：乙 BEGIN 先成功则装载 409 ----
exp10 = create_exp()
gate = RunOpGate(exp10, "a", "read")


def load_waits_other_panel_goes():
    t_rd, box_rd = bg_call(client.post, op_url(exp10, "a"), {"op": "read"})
    t_ld, box_ld = None, None
    try:
        check("装载等待·甲读值在途", gate.entered.wait(ROUTE_TIMEOUT))
        t_ld, box_ld = bg_call(client.post, script_url(exp10),
                               [{"side": "a", "op": "read"}])
        t_ld.join(0.5)
        check("装载等待·装载等待在途读值", t_ld.is_alive())
        # 装载等待期间，乙的手工 BEGIN 既不被甲的在途读值、
        # 也不被等待中的装载阻塞
        resp, exc, stuck = r_post(op_url(exp10, "b"), {"op": "begin"})
        check("装载等待·期间乙BEGIN及时200", not stuck and exc is None
              and resp.status_code == 200 and resp.get_json()["ok"] is True)
    finally:
        gate.release.set()
        t_rd.join(ROUTE_TIMEOUT)
        if t_ld is not None:
            t_ld.join(ROUTE_TIMEOUT)
    check("装载等待·乙BEGIN先成功：装载409", bg_done(t_ld, box_ld)
          and box_ld["resp"].status_code == 409)
    r_post(op_url(exp10, "b"), {"op": "rollback"})
    r_delete("/api/experiments/%s" % exp10)


gated(gate, load_waits_other_panel_goes)

# ---- 在途读值不留事务：装载等其收尾后成功，此后手工请求 409 ----
exp11 = create_exp()
gate = RunOpGate(exp11, "a", "read")


def load_after_manual_read():
    t_rd, box_rd = bg_call(client.post, op_url(exp11, "a"), {"op": "read"})
    t_ld, box_ld = None, None
    try:
        check("装载收尾·甲读值在途", gate.entered.wait(ROUTE_TIMEOUT))
        t_ld, box_ld = bg_call(client.post, script_url(exp11),
                               [{"side": "a", "op": "read"}])
        t_ld.join(0.5)
        check("装载收尾·装载等待在途读值", t_ld.is_alive())
    finally:
        gate.release.set()
        t_rd.join(ROUTE_TIMEOUT)
        if t_ld is not None:
            t_ld.join(ROUTE_TIMEOUT)
    check("装载收尾·读值不留事务：装载200", bg_done(t_ld, box_ld)
          and box_ld["resp"].status_code == 200)
    sid_e11 = box_ld["resp"].get_json()["script"]["id"]
    resp, exc, stuck = r_post(op_url(exp11, "b"), {"op": "begin"})
    check("装载收尾·装载成功后手工BEGIN 409", not stuck and resp.status_code == 409)
    r_post(script_url(exp11, "/cancel"), {"script_id": sid_e11, "step": 1})
    resp, exc, stuck = r_delete("/api/experiments/%s" % exp11)
    check("装载收尾·实验删除及时完成（名额已归还）",
          not stuck and resp.status_code == 200)


gated(gate, load_after_manual_read)

# ---- 既有字段与响应约定保持不变 ----
resp, _, _ = r_post(op_url("does-not-exist", "a"), {"op": "begin"})
check("路由·未知实验op 404", resp.status_code == 404)
resp, _, _ = r_post(ro_url("does-not-exist", "a"), {"enabled": True})
check("路由·未知实验只读 404", resp.status_code == 404)
resp, _, _ = r_post(op_url(exp5, "c"), {"op": "begin"})
check("路由·未知栏位400", resp.status_code == 400)
resp, _, _ = r_post(op_url(exp5, "a"), {"op": "nope"})
check("路由·未知op 400", resp.status_code == 400)
resp, _, _ = r_post(op_url(exp5, "a"), {"op": "add", "value": 1.5})
check("路由·add非整数400", resp.status_code == 400)
resp, _, _ = r_post(ro_url(exp5, "a"), {"enabled": 1})
check("路由·只读非布尔400", resp.status_code == 400)
resp, _, _ = r_post(ro_url(exp5, "a"), None)
check("路由·只读非JSON 400", resp.status_code == 400)
resp, _, _ = r_post(op_url(exp5, "a"), {"op": "read"})
body = resp.get_json()
check("路由·op响应字段约定",
      set(body) == {"ok", "error", "state"}
      and set(body["state"]) == {"in_transaction", "readonly", "last_read", "log"})
resp, _, _ = r_post(ro_url(exp5, "a"), {"enabled": True})
body = resp.get_json()
check("路由·只读响应字段约定",
      set(body) == {"ok", "error", "state"} and body["state"]["readonly"] is True)
r_post(ro_url(exp5, "a"), {"enabled": False})
# 409 响应体约定：{"error": ...}
resp, _, _ = r_post(script_url(exp5), [{"side": "a", "op": "read"}])
sid_e5b = resp.get_json()["script"]["id"]
resp, _, _ = r_post(op_url(exp5, "a"), {"op": "read"})
body = resp.get_json()
check("路由·active期间409响应含error字段", resp.status_code == 409 and "error" in body)
resp, _, _ = r_post(ro_url(exp5, "a"), {"enabled": True})
check("路由·active期间只读409响应含error字段", resp.status_code == 409 and "error" in resp.get_json())
r_post(script_url(exp5, "/cancel"), {"script_id": sid_e5b, "step": 1})

# 收尾：删除其余实验，确认没有请求占住准入名额
clean_ok = True
for e in (exp1, exp3, exp4, exp5):
    resp, exc, stuck = r_delete("/api/experiments/%s" % e)
    clean_ok = clean_ok and not stuck and resp.status_code == 200
check("路由·全部实验删除及时完成（名额均已归还）", clean_ok)

print()
print(f"{len(fails)} 失败" if fails else "后端脚本状态机与路由测试全部通过")
sys.exit(1 if fails else 0)
