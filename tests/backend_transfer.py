"""后端跨实验整数转移（transfer）验证（无需 Flask）：用最小 stub 导入 app.py。

通过 stub 的最小路由分发驱动 **app.py 注册的实际路由**（真实经过
admit_experiment 准入门控、script_lock / timeline_lock 与真实 SQLite，
不另写模拟准入逻辑），覆盖 POST /api/transfers 的全部约定：

- 基本流程：来源减、目标加，返回两边新值与时间线序号；两边各追加
  side=transfer 记录（before/after、deltas、连续 seq 沿用原格式）；
- 转移记录不可补偿（409），重启后仍可查询且仍不可补偿；
- 任一实验有有效脚本 409；未知 / 已删除 ID 沿用 404 / 410；
- amount 正整数校验、±2⁶² 增量上限与无损传输（大整数十进制字符串）；
- 来源 / 目标持写锁立即返回 SQLITE_BUSY(5)，两库本次的值与记录都撤销；
- 任一结果越过 64 位整数范围：两库都不修改；
- 反向并发转移、与普通提交 / 补偿交错：记录对应真实提交顺序，不死锁；
- 两栏事务、只读模式、读快照与最近显式读值不受影响；
- 删除准入：已准入转移先收尾再删（TransferGate 确定性验证），
  删除等待不阻塞其他实验。
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


def transfer(source, target, amount):
    return r_post("/api/transfers", {"source": source, "target": target, "amount": amount})


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


def chains(tl):
    """时间线链一致：seq 从 1 连续，且每条 before == 前一条 after。"""
    return all(tl[i]["seq"] == i + 1 for i in range(len(tl))) and all(
        tl[i + 1]["before"] == tl[i]["after"] for i in range(len(tl) - 1)
    )


# ================= 基本流程：来源减、目标加，两边各记 transfer =================
exp1, exp2 = create_exp(), create_exp()
resp, exc, stuck = transfer(exp1, exp2, 30)
check("基本·转移及时返回200", not stuck and exc is None and resp.status_code == 200)
body = resp.get_json()
check("基本·ok:true 且 error:null", body["ok"] is True and body["error"] is None)
check("基本·响应字段约定", set(body) == {"ok", "error", "source", "target"}, str(set(body)))
check("基本·返回来源新值与序号",
      body["source"] == {"id": exp1, "value": 70, "seq": 1}, str(body["source"]))
check("基本·返回目标新值与序号",
      body["target"] == {"id": exp2, "value": 130, "seq": 1}, str(body["target"]))
check("基本·来源已提交值70", committed(exp1) == 70)
check("基本·目标已提交值130", committed(exp2) == 130)
tl1, tl2 = timeline(exp1), timeline(exp2)
check("基本·来源记录 #1 transfer 100→70 [-30]",
      tl1 == [{"seq": 1, "side": "transfer", "before": 100, "after": 70, "deltas": [-30]}],
      str(tl1))
check("基本·目标记录 #1 transfer 100→130 [30]",
      tl2 == [{"seq": 1, "side": "transfer", "before": 100, "after": 130, "deltas": [30]}],
      str(tl2))
check("基本·transfer 记录字段沿用原格式（无 compensates）",
      set(tl1[0]) == {"seq", "side", "before", "after", "deltas"})

# 与普通提交混合编号：普通提交接续 seq，转移再接续
rec = commit_adds(exp1, "a", [5])  # #2: 70→75
check("基本·转移后普通提交 #2 70→75 [5]",
      rec == {"seq": 2, "side": "a", "before": 70, "after": 75, "deltas": [5]}, str(rec))
resp, _, _ = transfer(exp2, exp1, 10)
body = resp.get_json()
check("基本·反向转移后来源序号接续",
      body["source"] == {"id": exp2, "value": 120, "seq": 2}
      and body["target"] == {"id": exp1, "value": 85, "seq": 3}, str(body))
check("基本·两边链一致", chains(timeline(exp1)) and chains(timeline(exp2)))

# ================= 转移记录不可补偿 =================
resp, exc, stuck = r_post("/api/experiments/%s/timeline/compensate" % exp1, {"seq": 3})
check("补偿·末条为转移记录时补偿409", not stuck and resp.status_code == 409)
check("补偿·409后来源值不变", committed(exp1) == 85)
check("补偿·409后时间线条数不变", len(timeline(exp1)) == 3)
# 普通记录仍可补偿（#2 之上又叠了 #3，故 #2 已过期；新提交一笔再补偿）
rec = commit_adds(exp2, "b", [2])  # exp2 #3: 120→122
resp, _, _ = r_post("/api/experiments/%s/timeline/compensate" % exp2, {"seq": 3})
check("补偿·转移不影响后续普通记录补偿", resp.status_code == 200 and resp.get_json()["ok"] is True)
check("补偿·补偿后值恢复120", committed(exp2) == 120)

# ================= 有效脚本：任一侧 409 =================
exp3, exp4 = create_exp(), create_exp()
resp, _, _ = r_post("/api/experiments/%s/script" % exp3, [{"side": "a", "op": "read"}])
sid3 = resp.get_json()["script"]["id"]
resp, exc, stuck = transfer(exp3, exp4, 5)
check("脚本·来源有有效脚本409", not stuck and resp.status_code == 409)
resp, _, _ = transfer(exp4, exp3, 5)
check("脚本·目标有有效脚本409", resp.status_code == 409)
check("脚本·409两库值不变", committed(exp3) == 100 and committed(exp4) == 100)
check("脚本·409两库时间线不变", len(timeline(exp3)) == 0 and len(timeline(exp4)) == 0)
r_post("/api/experiments/%s/script/cancel" % exp3, {"script_id": sid3, "step": 1})
resp, _, _ = transfer(exp3, exp4, 5)
check("脚本·取消后转移成功", resp.status_code == 200 and resp.get_json()["ok"] is True)

# ================= 未知 / 已删除 ID：404 / 410 =================
resp, exc, stuck = transfer("does-not-exist", exp4, 1)
check("删除·未知来源404", not stuck and resp.status_code == 404)
resp, _, _ = transfer(exp4, "does-not-exist", 1)
check("删除·未知目标404", resp.status_code == 404)
resp, _, _ = r_delete("/api/experiments/%s" % exp4)
check("删除·删除目标实验200", not stuck and resp.status_code == 200)
resp, _, _ = transfer(exp4, exp3, 1)
check("删除·已删除来源410", resp.status_code == 410)
resp, _, _ = transfer(exp3, exp4, 1)
check("删除·已删除目标410", resp.status_code == 410)
check("删除·410后另一库值不变", committed(exp3) == 95)

# ================= amount 与 body 校验：400 =================
exp5, exp6 = create_exp(), create_exp()
bad_bodies = [
    None, [], "x",                                          # 非 JSON 对象
    {},                                                     # 缺字段
    {"source": exp5, "target": exp6},                       # 缺 amount
    {"source": exp5, "target": exp6, "amount": 0},          # 非正
    {"source": exp5, "target": exp6, "amount": -1},         # 负
    {"source": exp5, "target": exp6, "amount": 1.5},        # 非整数
    {"source": exp5, "target": exp6, "amount": True},       # 布尔
    {"source": exp5, "target": exp6, "amount": "abc"},      # 非数字字符串
    {"source": exp5, "target": exp6, "amount": None},
    {"source": exp5, "target": exp6, "amount": 2 ** 62 + 1},    # 超增量上限
    {"source": exp5, "target": exp6, "amount": str(2 ** 62 + 1)},
    {"source": exp5, "target": exp5, "amount": 1},          # 来源目标相同
    {"source": 1, "target": exp6, "amount": 1},             # 非字符串 ID
    {"source": exp5, "target": None, "amount": 1},
]
all_400 = True
for bad in bad_bodies:
    resp, exc, stuck = r_post("/api/transfers", bad)
    if stuck or resp is None or resp.status_code != 400:
        all_400 = False
        break
check("非法·非法 body / amount / 相同 ID 一律400", all_400)
check("非法·400两库值不变", committed(exp5) == 100 and committed(exp6) == 100)
check("非法·400两库时间线不变", len(timeline(exp5)) == 0 and len(timeline(exp6)) == 0)
# 边界：恰好 ±2⁶² 上限的 amount 接受（字符串形式同样接受）
resp, _, _ = transfer(exp5, exp6, "25")
check("非法·字符串 amount 接受", resp.status_code == 200 and resp.get_json()["ok"] is True)
check("非法·字符串 amount 生效", committed(exp5) == 75 and committed(exp6) == 125)

# ================= 写锁冲突：SQLITE_BUSY(5)，两库本次都撤销 =================
exp7, exp8 = create_exp(), create_exp()
# 目标栏持写锁（未提交加法）
op(exp8, "a", {"op": "begin"})
body = op(exp8, "a", {"op": "add", "value": 7})
assert body["ok"] is True
resp, exc, stuck = transfer(exp7, exp8, 10)
check("写锁·目标持锁时转移及时返回200", not stuck and exc is None and resp.status_code == 200)
body = resp.get_json()
check("写锁·ok:false 且 SQLITE_BUSY(5)",
      body["ok"] is False and body["error"]["name"] == "SQLITE_BUSY"
      and body["error"]["code"] == 5, str(body.get("error")))
check("写锁·目标持锁两库值不变", committed(exp7) == 100 and committed(exp8) == 100)
check("写锁·目标持锁两库时间线不变",
      len(timeline(exp7)) == 0 and len(timeline(exp8)) == 0)
op(exp8, "a", {"op": "rollback"})
# 来源栏持写锁
op(exp7, "b", {"op": "begin"})
body = op(exp7, "b", {"op": "add", "value": 3})
assert body["ok"] is True
resp, _, _ = transfer(exp7, exp8, 10)
body = resp.get_json()
check("写锁·来源持锁 ok:false 且 SQLITE_BUSY(5)",
      body["ok"] is False and body["error"]["name"] == "SQLITE_BUSY"
      and body["error"]["code"] == 5, str(body.get("error")))
check("写锁·来源持锁两库值不变", committed(exp7) == 100 and committed(exp8) == 100)
check("写锁·来源持锁两库时间线不变",
      len(timeline(exp7)) == 0 and len(timeline(exp8)) == 0)
op(exp7, "b", {"op": "rollback"})
resp, _, _ = transfer(exp7, exp8, 10)
check("写锁·释放后转移成功", resp.status_code == 200 and resp.get_json()["ok"] is True)
check("写锁·释放后值生效", committed(exp7) == 90 and committed(exp8) == 110)

# ================= 64 位越界：两库都不修改 =================
exp9, exp10 = create_exp(), create_exp()
commit_adds(exp10, "a", [2 ** 62])  # 目标：4611686018427388004
resp, exc, stuck = transfer(exp9, exp10, 2 ** 62)
body = resp.get_json()
check("越界·目标上溢及时返回200", not stuck and resp.status_code == 200)
check("越界·目标上溢 ok:false 且错误名/码为 null",
      body["ok"] is False and body["error"]["name"] is None and body["error"]["code"] is None,
      str(body.get("error")))
check("越界·目标上溢两库值不变",
      committed(exp9) == 100 and committed(exp10) == "4611686018427388004")
check("越界·目标上溢两库时间线不变",
      len(timeline(exp9)) == 0 and len(timeline(exp10)) == 1)
commit_adds(exp9, "a", [-(2 ** 62), -(2 ** 62)])  # 来源：-9223372036854775708
resp, _, _ = transfer(exp9, exp10, 2 ** 62)
body = resp.get_json()
check("越界·来源下溢 ok:false",
      body["ok"] is False and body["error"]["name"] is None, str(body.get("error")))
check("越界·来源下溢两库值不变",
      committed(exp9) == "-9223372036854775708" and committed(exp10) == "4611686018427388004")
check("越界·越界后时间线无新增",
      len(timeline(exp9)) == 1 and len(timeline(exp10)) == 1)
resp, _, _ = transfer(exp9, exp10, 100)
check("越界·界内转移仍成功", resp.status_code == 200 and resp.get_json()["ok"] is True)

# ================= 反向并发转移：都成功、不死锁、链一致 =================
reverse_ok = True
for _ in range(20):
    ea, eb = create_exp(), create_exp()
    barrier = threading.Barrier(2)
    results = {}

    def t_ab():
        barrier.wait()
        results["ab"] = client.post("/api/transfers",
                                    {"source": ea, "target": eb, "amount": 3})

    def t_ba():
        barrier.wait()
        results["ba"] = client.post("/api/transfers",
                                    {"source": eb, "target": ea, "amount": 7})

    threads = [threading.Thread(target=t_ab, daemon=True),
               threading.Thread(target=t_ba, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(ROUTE_TIMEOUT)
    if any(t.is_alive() for t in threads):
        reverse_ok = False
        break
    if not (results["ab"].get_json().get("ok") is True
            and results["ba"].get_json().get("ok") is True):
        reverse_ok = False
        break
    # 净额：ea -3+7=104，eb +3-7=96；两边各两条 transfer 记录且链一致
    ta, tb = timeline(ea), timeline(eb)
    if not (committed(ea) == 104 and committed(eb) == 96
            and chains(ta) and chains(tb)
            and ta[-1]["after"] == 104 and tb[-1]["after"] == 96
            and all(r["side"] == "transfer" for r in ta + tb)):
        reverse_ok = False
        break
    client.delete("/api/experiments/%s" % ea)
    client.delete("/api/experiments/%s" % eb)
check("并发·反向转移不死锁且记录对应真实提交顺序(20轮)", reverse_ok)

# ================= 交错：转移与普通提交互斥，记录对应真实提交顺序 =================
interleave_ok = True
for _ in range(20):
    ea, eb = create_exp(), create_exp()
    commit_adds(ea, "a", [5])  # ea #1: 100→105
    barrier = threading.Barrier(2)
    results = {}

    def do_transfer():
        barrier.wait()
        results["t"] = client.post("/api/transfers",
                                   {"source": ea, "target": eb, "amount": 10})

    def do_commit():
        barrier.wait()
        client.post("/api/experiments/%s/conn/b/op" % ea, {"op": "begin"})
        client.post("/api/experiments/%s/conn/b/op" % ea,
                    {"op": "add", "value": 7})
        results["c"] = client.post("/api/experiments/%s/conn/b/op" % ea, {"op": "commit"})

    threads = [threading.Thread(target=do_transfer, daemon=True),
               threading.Thread(target=do_commit, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(ROUTE_TIMEOUT)
    if any(t.is_alive() for t in threads):
        interleave_ok = False
        break
    ta = timeline(ea)
    ok = chains(ta) and committed(ea) == ta[-1]["after"]
    if results["t"].get_json().get("ok") is True:
        # 转移成功：ea 上普通提交与转移记录都按真实提交顺序排列
        ok = ok and len(ta) == 3 and committed(eb) == 110
        ok = ok and timeline(eb)[-1]["after"] == 110
    else:
        # 转移撞上乙未提交的写锁：SQLITE_BUSY，ea 只有普通记录
        ok = ok and results["t"].get_json()["error"]["name"] == "SQLITE_BUSY"
        ok = ok and len(ta) == 2 and committed(eb) == 100 and len(timeline(eb)) == 0
    if not ok:
        interleave_ok = False
        break
    client.delete("/api/experiments/%s" % ea)
    client.delete("/api/experiments/%s" % eb)
check("交错·与普通提交互斥且记录对应真实提交顺序(20轮)", interleave_ok)

# ================= 交错：转移与补偿互斥 =================
comp_ok = True
for _ in range(20):
    ea, eb = create_exp(), create_exp()
    commit_adds(ea, "a", [5])  # ea #1: 100→105
    barrier = threading.Barrier(2)
    results = {}

    def do_transfer2():
        barrier.wait()
        results["t"] = client.post("/api/transfers",
                                   {"source": ea, "target": eb, "amount": 10})

    def do_comp():
        barrier.wait()
        results["c"] = client.post(
            "/api/experiments/%s/timeline/compensate" % ea, {"seq": 1})

    threads = [threading.Thread(target=do_transfer2, daemon=True),
               threading.Thread(target=do_comp, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(ROUTE_TIMEOUT)
    if any(t.is_alive() for t in threads):
        comp_ok = False
        break
    ta = timeline(ea)
    ok = chains(ta) and committed(ea) == ta[-1]["after"]
    c = results["c"]
    if c.status_code == 200 and c.get_json().get("ok") is True:
        # 补偿先完成：#2 system，随后转移记录为 #3
        ok = ok and len(ta) == 3 and ta[1]["side"] == "system" and ta[2]["side"] == "transfer"
    elif c.status_code == 409:
        # 转移先追加：末条是转移记录，不可补偿
        ok = ok and ta[-1]["side"] == "transfer"
    else:
        ok = False
    if not ok:
        comp_ok = False
        break
    client.delete("/api/experiments/%s" % ea)
    client.delete("/api/experiments/%s" % eb)
check("交错·与补偿互斥且结果一致(20轮)", comp_ok)

# ================= 大整数无损 =================
# 大整数无损：9007199254740993 超出 JS 安全整数范围（±2⁵³−1），
# 响应与时间线中超出范围的值用十进制字符串；来源新值 -9007199254740893
# 仍在安全整数范围内，按约定返回 JSON 数字
exp11, exp12 = create_exp(), create_exp()
big = 9007199254740993  # 超出 JS 安全整数范围
resp, _, _ = transfer(exp11, exp12, str(big))
body = resp.get_json()
check("大整数·转移成功", resp.status_code == 200 and body["ok"] is True)
check("大整数·响应新值无损（越界侧字符串、界内侧数字）",
      body["source"]["value"] == -9007199254740893
      and body["target"]["value"] == "9007199254741093", str(body))
tl11, tl12 = timeline(exp11), timeline(exp12)
check("大整数·来源记录 deltas 取负无损",
      tl11[0]["after"] == -9007199254740893 and tl11[0]["deltas"] == ["-9007199254740993"],
      str(tl11))
check("大整数·目标记录 deltas 无损",
      tl12[0]["after"] == "9007199254741093" and tl12[0]["deltas"] == ["9007199254740993"],
      str(tl12))
check("大整数·已提交值无损",
      committed(exp11) == -9007199254740893 and committed(exp12) == "9007199254741093")

# ================= 两栏事务 / 只读 / 快照 / 最近读值不受影响 =================
exp13, exp14 = create_exp(), create_exp()
op(exp13, "b", {"op": "begin"})
body = op(exp13, "b", {"op": "read"})
check("两栏·乙建立读快照100", body["state"]["last_read"] == 100)
resp, _, _ = r_post("/api/experiments/%s/conn/a/readonly" % exp13, {"enabled": True})
check("两栏·甲只读开启", resp.get_json()["ok"] is True)
resp, _, _ = transfer(exp14, exp13, 25)
check("两栏·转移成功", resp.get_json()["ok"] is True)
check("两栏·已提交值125", committed(exp13) == 125)
body = op(exp13, "b", {"op": "read"})
check("两栏·已有读快照仍见旧值100", body["state"]["last_read"] == 100)
st = status(exp13)
check("两栏·乙事务仍开启", st["b"]["in_transaction"] is True)
check("两栏·乙最近显式读值仍100", st["b"]["last_read"] == 100)
check("两栏·甲只读仍开启", st["a"]["readonly"] is True)
op(exp13, "b", {"op": "rollback"})
r_post("/api/experiments/%s/conn/a/readonly" % exp13, {"enabled": False})
op(exp13, "b", {"op": "begin"})
body = op(exp13, "b", {"op": "read"})
check("两栏·新事务读到125", body["state"]["last_read"] == 125)
op(exp13, "b", {"op": "rollback"})

# ================= 重启后转移记录仍可查询、仍不可补偿 =================
exp15, exp16 = create_exp(), create_exp()
resp, _, _ = transfer(exp15, exp16, 42)
assert resp.get_json()["ok"] is True
# 模拟进程终止（含 kill -9）+ 重启：关闭并丢弃内存对象，重新扫描数据目录
for eid in (exp15, exp16):
    with A.experiments_lock:
        exp = A.experiments.pop(eid, None)
        A.deleted_experiments.clear()
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
check("重启·来源转移记录恢复",
      timeline(exp15) == [{"seq": 1, "side": "transfer", "before": 100,
                           "after": 58, "deltas": [-42]}], str(timeline(exp15)))
check("重启·目标转移记录恢复",
      timeline(exp16) == [{"seq": 1, "side": "transfer", "before": 100,
                           "after": 142, "deltas": [42]}], str(timeline(exp16)))
check("重启·两库已提交值恢复", committed(exp15) == 58 and committed(exp16) == 142)
resp, _, _ = r_post("/api/experiments/%s/timeline/compensate" % exp15, {"seq": 1})
check("重启·转移记录仍不可补偿409", resp.status_code == 409)
resp, _, _ = transfer(exp15, exp16, 8)
check("重启·恢复后转移继续编号",
      resp.get_json()["source"]["seq"] == 2 and resp.get_json()["target"]["seq"] == 2)

# ================= 删除准入：已准入转移先收尾再删，不阻塞其他实验 =================
# TransferGate 把指定转移按在「已准入、尚未执行」状态，由测试手动放行，
# 确定性验证：删除等待在途转移收尾；等待期间其他实验照常操作。
real_execute = A._execute_transfer


class TransferGate:
    def __init__(self, src_id, dst_id):
        self.src_id = src_id
        self.dst_id = dst_id
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, src_exp, dst_exp, amount):
        if src_exp.id == self.src_id and dst_exp.id == self.dst_id:
            self.entered.set()
            # 限时等待：即使断言失败也不会永久卡住测试进程
            self.release.wait(ROUTE_TIMEOUT)
        return real_execute(src_exp, dst_exp, amount)


exp17, exp18, exp19 = create_exp(), create_exp(), create_exp()
gate = TransferGate(exp17, exp18)
A._execute_transfer = gate
try:
    box = {}

    def do_transfer3():
        box["t"] = client.post("/api/transfers",
                               {"source": exp17, "target": exp18, "amount": 9})

    def do_delete():
        box["d"] = client.delete("/api/experiments/%s" % exp17)

    t1 = threading.Thread(target=do_transfer3, daemon=True)
    t1.start()
    check("删除·转移已进入在途状态", gate.entered.wait(ROUTE_TIMEOUT))
    t2 = threading.Thread(target=do_delete, daemon=True)
    t2.start()
    t2.join(0.3)
    check("删除·在途转移未收尾时删除等待", t2.is_alive())
    # 删除等待期间其他实验照常操作（不阻塞其他实验）
    body = op(exp19, "a", {"op": "begin"})
    check("删除·删除等待不阻塞其他实验", body["ok"] is True)
    op(exp19, "a", {"op": "rollback"})
    gate.release.set()
    t1.join(ROUTE_TIMEOUT)
    t2.join(ROUTE_TIMEOUT)
    check("删除·转移先收尾成功",
          not t1.is_alive() and box["t"].get_json()["ok"] is True)
    check("删除·转移收尾后删除完成", not t2.is_alive() and box["d"].status_code == 200)
    resp, _, _ = transfer(exp17, exp19, 1)
    check("删除·删除后来源410", resp.status_code == 410)
    resp, _, _ = transfer(exp19, exp17, 1)
    check("删除·删除后目标410", resp.status_code == 410)
finally:
    A._execute_transfer = real_execute
check("删除·转移结果在删除前已落库（目标值109）", committed(exp18) == 109)

# 收尾：删除其余实验，确认没有请求占住准入名额
clean_ok = True
for e in (exp1, exp2, exp3, exp5, exp6, exp7, exp8, exp9, exp10,
          exp11, exp12, exp13, exp14, exp15, exp16, exp18, exp19):
    resp, exc, stuck = r_delete("/api/experiments/%s" % e)
    clean_ok = clean_ok and not stuck and resp.status_code == 200
check("收尾·全部实验删除及时完成（名额均已归还）", clean_ok)

print()
print(f"{len(fails)} 失败" if fails else "后端跨实验转移测试全部通过")
sys.exit(1 if fails else 0)
