"""SQLite 事务隔离实验室 —— Flask 后端。

每个实验对应一个独立的 WAL 数据库（单行整数值 100）。
页面左右两栏（甲 / 乙）各绑定一条长期独立连接，事务完全由显式 SQL 控制；
第三条连接只读已提交值。状态刷新不执行任何 SELECT，
避免在 DEFERRED 事务中提前建立读快照。

写操作同样遵守这一约束：事务尚未建立读快照时，「加上」的边界检查
基于 observer 连接看到的已提交值在 Python 中完成，UPDATE 本身再以
checked_add 函数做最终精确判定，绝不先在本连接上 SELECT —— 否则
首次写锁冲突（SQLITE_BUSY）后本事务会留下读快照，对方提交释放写锁后
重试即被误报为 SQLITE_BUSY_SNAPSHOT。

每栏另有一个独立的只读开关（默认关闭）：开启即在**该栏原长期连接**上
真实执行 PRAGMA query_only=1，不换连接、不结束当前事务，也不先读任何值
（不建立读快照）；关闭执行 PRAGMA query_only=0。开启后读值、BEGIN、
COMMIT、ROLLBACK 都照常（COMMIT 仍会提交此前写入，ROLLBACK 仍撤销），
加法按钮也保持可用，但事务内未越界的加法会被 SQLite 如实拒绝为
SQLITE_READONLY(8)，语句失败不撤销此前修改；关闭后可在原事务继续加法。
提交 / 回滚不重置开关，开关状态只保存在内存并随状态接口返回。两栏开关
互相独立，乙的写锁冲突仍如实报 SQLITE_BUSY(5)。

WAL 检查点（PASSIVE / TRUNCATE）不由甲、乙两栏的连接执行，而是每次
用一条全新的独立连接真实执行 PRAGMA wal_checkpoint，busy_timeout=0：
读事务阻止回收时立即返回 busy=1，不等待。各写连接均已关闭自动检查点
（PRAGMA wal_autocheckpoint=0），检查点时机完全由手动按钮 / HTTP 控制。
检查点绝不代替两栏提交、回滚，也不替换连接；状态刷新只读取内存中
最近一次检查点结果的缓存，不执行任何 SQL、不触发检查点。

提交时间线按事务汇总：仅「至少一次加法成功且 COMMIT 成功」的事务产生
一条记录，按实际提交顺序从 1 编号，含栏位、首次成功加法前的值、提交后
的值与按执行顺序排列的成功增量（沿用整数无损编码）。首次成功加法前的值
由 checked_add 在 UPDATE 语句内捕获（事务自身视图），提交后的值 =
该值 + 全部成功增量之和（首次成功加法已持有写锁直至提交，期间无其他
连接能修改该行），绝不事后读取别栏新值作为本次结果。失败加法不计入；
回滚、失败提交、纯读事务不记录；未提交内容不可见；提交重试成功只记
一次。COMMIT 与记录追加在同一临界区（timeline_lock）完成，编号顺序与
实际提交顺序严格一致。采集、查询（GET /api/experiments/<id>/timeline）
与轮询（/status 的 timeline 字段）都只读内存，不在两栏连接上额外读值、
不提前建立快照，也不改变最近一次显式读值。记录与值在同一事务写入
本实验库的 timeline 表（见下文「持久化与恢复」）。

已提交的加法不能用 ROLLBACK 撤回，改为对时间线末条普通提交执行补偿
（POST /api/experiments/<id>/timeline/compensate，body 为正整数
{"seq": N}）。仅当末条记录是普通提交且 seq 与其一致时才执行：过期目标、
补偿（system）记录或有效脚本一律 409，补偿记录不可再补偿。补偿用一条
全新的独立连接显式事务真实提交（UPDATE 与记录 INSERT 在同一事务），
把值恢复为原记录的 before；busy_timeout=0，两栏任一事务持有写锁时
立即返回 SQLITE_BUSY 错误名 / 错误码，失败时回滚本事务，不改库也不改
时间线。目标核对、事务提交与记录追加都在 timeline_lock 内完成，与普通
提交的「COMMIT → 追加」互斥：交错时重新核对目标，同一 seq 并发最多
成功一次。成功按提交顺序追加一条 side=system 的记录：before/after
对调、deltas 逆序取负、compensates 记原 seq；原记录保留，普通记录
字段不变。全程不触碰两栏长期连接与 observer：两栏事务、只读模式、
最近显式读值与读快照都不受影响（已有读快照仍见旧值），也不在两栏
连接上额外读值。

删除实验（DELETE /api/experiments/<id>）先在内存门控上标记删除开始，
并等待此前已进入的请求全部正常完成；删除开始后该实验的操作、状态、
已提交值与检查点新请求一律返回 410。等待按实验独立，不阻塞其他实验。
之后才回滚两栏未提交事务、关闭甲、乙长期连接与 observer，再移除
数据库及 -wal、-shm 文件——此时不存在在途请求或在途检查点连接，
检查点不可能在删除后重新创建文件。本次运行中已删除 ID 仍返回 410，
未知 ID 返回 404；重启后内存墓碑不保留，但已删除实验的库文件早已
移除，不会复活，按未知 ID 返回 404。

持久化与恢复：实验库（kv 值与 timeline 表同库）保存在 LAB_DATA_DIR
（Compose 以命名卷挂载），启动时不再清空目录，而是扫描留存的
exp_*.db 重新加载，实验在服务重启或进程被强制终止后仍能按原 ID
访问：已提交值就在 kv 表中，提交时间线从 timeline 表完整恢复
（含补偿关联 compensates 与序号 seq），编号接续，末条普通记录仍可
补偿；两栏重建为无事务、只读关闭、最近读值为空，脚本、操作日志与
检查点缓存不恢复（全新内存状态）。普通提交与补偿都把「值 + 时间线
记录」放在同一事务提交：进程在任意时刻被终止（含 kill -9），重启后
只能恢复到二者都生效或都未生效的状态，绝不依赖退出清理。失败提交
保留原事务与重试能力：记录 INSERT 随 SAVEPOINT 回滚撤销，重试时
重新写入，绝不重复记录。旧版库（无 timeline 表）首次加载保留现值，
以空时间线为基线。

脚本逐步执行：页面与 HTTP 可装载 1～50 步的 JSON 数组（POST
/api/experiments/<id>/script），每步含 side（a/b）与 op
（begin/read/add/commit/rollback），add 另含 value，沿用与手工加法
完全相同的整数校验（parse_int、±2⁶² 上限、大整数十进制字符串）。
整份校验、非法整份拒绝（400），任何已存在脚本都不受影响。仅当两栏
均无事务且当前没有有效（active）脚本时才能装载——装载先在 script_lock
内等待已准入的在途手工请求收尾（等待经 wait() 释放锁，在途请求与
另一栏的新请求都不被阻塞），再同时持两栏 conn_lock 复查
in_transaction，与手工 BEGIN 严格互斥：手工 BEGIN 先成功则装载 409，
装载先成功则手工请求 409，不会出现「装载成功后手工 BEGIN 才开启
事务」。手工操作 / 只读切换只在 script_lock 内做 active 复查与在途
计数，随即释放锁在本栏原连接上独立执行：同栏请求仍由本栏 conn_lock
串行，两栏互不等待。脚本只保存在内存，生成独立脚本 ID；查询（GET
…/script 及 /status 的 script 字段）只读内存，返回脚本 ID、从 1
开始的当前步号、全部步骤与逐次执行记录，绝不在两栏连接上额外读值。

脚本有效（active）期间，后端拒绝两栏的全部手工事务操作与只读切换
（409）；查询、已提交值、时间线、检查点与删除仍照常可用。推进
（…/script/advance）在该步指定栏的**原长期连接**上调用同一个 run_op
真实执行：成功才把步号前移；失败不代为回滚、步号停在原处，可重试或
跳过（…/script/skip）。每次尝试都追加一条记录：步号、栏位、操作、
增量、success/failure 结果与原始 SQLite 错误名 / 错误码（拒绝类错误
名 / 码为 null）；跳过单独记录 skipped，不执行任何 SQL。脚本走完全部
步骤置为 finished，或经 …/script/cancel 取消置为 cancelled 后，手工
操作立即恢复；系统既不自动提交也不自动回滚，脚本结束时仍开启的事务
原样保留（因此若要再装载脚本，需先手工提交 / 回滚）。

推进、跳过、取消都必须携带脚本 ID 与所见步号（{script_id, step}，
类型不符 400）；同一实验的脚本动作经 script_lock 串行核对并执行：
脚本 ID 与当前不符（旧脚本的过期请求，包括已被新脚本替换后）、脚本
已结束 / 取消、步号与当前步不一致，一律 409，不执行、不产生记录、
不影响新脚本。

只读 SQL 工作台：页面与 HTTP 可对实验库提交一条只读 SQL（POST
/api/experiments/<id>/query，body {"sql": "..."}），立即返回查询
ID（12 位十六进制）。SQL 限 1～4000 字符（按字符数，含空白），必须是
**单独一条** SELECT 或 WITH 读查询：允许结尾分号，禁止多条语句，写入
（INSERT/UPDATE/DELETE 等）、PRAGMA（含 pragma 函数表）、ATTACH/
DETACH、扩展加载（load_extension）以及 EXPLAIN 等非 SELECT 开头语句
一律 400。执行用**全新的独立只读连接**（URI mode=ro + PRAGMA
query_only=1 + set_authorizer 白名单三重限制），绝不触碰两栏长期
连接与 observer：两栏事务、只读开关、读快照、最近显式读值、时间线都
不受影响。提交前先用同类只读连接以 EXPLAIN 预编译做策略闸，非法 SQL
在提交当时即 400，绝不入队；合法但运行时失败（如无此表）则查询状态
为 failed 并附原始 SQLite 错误名 / 错误码。

每实验只保留**最新一条**查询：已有查询处于 running 时再次提交返回
409；上一条已终结（succeeded/failed/cancelled）时新提交直接替换它，
旧查询 ID 立即返回 404（不影响新查询、不留墓碑）。查询状态有 running /
succeeded / failed / cancelled 四种（GET …/query/<query_id>）：
运行中可轮询；成功返回列名、**前 100 行**与 truncated 截断标志（超过
100 行只取前 100 行并置 truncated=true）；整数沿用 json_int 无损约定
（超出 ±2⁵³−1 用十进制字符串），BLOB 一律转成 X'…' 形式的小写十六进制
文本。取消（POST …/query/<query_id>/cancel，空 JSON body 即可）必须
**中断真实执行的 SQL**（连接 interrupt，递归 CTE 等长查询立即
SQLITE_INTERRUPT）；**尚未开跑**（worker 尚未开始执行）的查询也必须
能取消——worker 在打开连接前、执行前都复查取消标志。取消先被确认则
绝不能成功：worker 在 SQL 返回后、**连接关闭之后**才发布终态，并再次
复查取消标志。**连接关闭后才公布终态**（成功 / 失败 / 取消皆然），
删除与取消请求等到的查询线程绝不可能再触碰数据库文件。对已终结查询
再次取消返回 200 与原终态，**不改变结果**；旧查询 ID 查询 / 取消返回
404。

删除实验（DELETE /api/experiments/<id>）除原有流程外，先取消在途
查询并等待其工作线程结束（连接已关闭、终态已发布），再回滚两栏
事务、关闭连接、删除文件；查询不阻塞其他实验，失败或被取消的查询
**不改值也不改时间线**。查询状态只保存在内存，服务重启后不恢复：
旧查询 ID 对恢复后的实验返回 404，与「旧 ID 不影响新查询」一致。
"""

import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import uuid

from flask import Flask, jsonify, render_template, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("LAB_DATA_DIR", os.path.join(BASE_DIR, "data"))
MAX_LOG_ENTRIES = 200
MAX_ADD_ABS = 2 ** 62  # 单次增量上限
INT64_MIN = -(2 ** 63)  # SQLite 整数下限，累加结果不得越界（含负向）
INT64_MAX = 2 ** 63 - 1  # SQLite 整数上限，越界会被 SQLite 静默转成 REAL
OVERFLOW_MESSAGE = "累加结果超出有符号 64 位整数范围，本次未修改"
JS_SAFE_INT_MAX = 2 ** 53 - 1  # Number.MAX_SAFE_INTEGER，超出则用十进制字符串传输

INT_PATTERN = re.compile(r"[+-]?[0-9]+")

VALID_OPS = {"begin", "read", "add", "commit", "rollback"}
MAX_SCRIPT_STEPS = 50  # 单个脚本的步数上限：1～50 步
OP_LABELS = {
    "begin": "BEGIN DEFERRED",
    "read": "读值",
    "commit": "COMMIT",
    "rollback": "ROLLBACK",
}

# 允许手动触发的检查点模式：SQL 片段直接拼入 PRAGMA，必须走白名单
VALID_CHECKPOINT_MODES = {"PASSIVE", "TRUNCATE"}
WAL_SUFFIX = "-wal"
SHM_SUFFIX = "-shm"
# 数据目录中实验库文件名：exp_<12 位十六进制>.db（与 uuid4().hex[:12] 一致）
EXP_DB_PATTERN = re.compile(r"exp_([0-9a-f]{12})\.db")

# ---- 只读 SQL 工作台 ----
MAX_SQL_LEN = 4000  # SQL 文本上限（按字符数，含空白）；下限 1（非空白）
MAX_QUERY_ROWS = 100  # 成功查询最多返回前 100 行，超出置 truncated=true
# sqlite3 authorizer 动作码（sqlite3.h，Python 仅暴露 OK/DENY/IGNORE）：
# 只放行读相关动作，其余（写入 / PRAGMA / ATTACH / 函数等）一律拒绝
AUTH_READ = 20
AUTH_SELECT = 21
AUTH_FUNCTION = 31
AUTH_RECURSIVE = 33
# 一条语句唯一允许的起始关键字：SELECT，或定义 CTE 的 WITH。
# WITH 后的数据修改语句（WITH cte AS (...) DELETE/INSERT/UPDATE ...）
# 还要在 CTE 括号之后复查下一个关键字必须是 SELECT；其余一切以
# INSERT/UPDATE/DELETE/PRAGMA/ATTACH/EXPLAIN/... 开头的语句在词法层拒绝。
SQL_LEAD_WORDS = frozenset(("select", "with"))
# 任何位置都禁止出现的函数名：扩展加载与 PRAGMA 表值函数。
# authorizer 拒绝这两类函数时 SQLite 只给通用 SQLITE_ERROR(1)，无法与
# 「无此函数」等语义错误区分，因此在词法层显式拒绝；authorizer 仍作为
# 纵深防御（参数化 / 视图等路径同样拦得住）。
SQL_FORBIDDEN_FUNCS = frozenset(("load_extension",))
SQL_FORBIDDEN_FUNC_PREFIXES = ("pragma_",)

app = Flask(__name__)


def json_int(value):
    """JSON 无损编码整数：安全整数范围内返回数字，超出则用十进制字符串。"""
    if value is None:
        return None
    if -JS_SAFE_INT_MAX <= value <= JS_SAFE_INT_MAX:
        return value
    return str(value)


def parse_int(raw):
    """解析增量：接受 JSON 整数或十进制字符串（大整数以字符串传输），否则返回 None。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and INT_PATTERN.fullmatch(raw.strip()):
        return int(raw.strip())
    return None


def scan_single_read_sql(sql):
    """词法扫描 SQL，确认它是**单独一条** SELECT / WITH 读查询。

    只做词法层面的「条数 + 起始关键字」检查，不解析语义；策略的最终
    判定由只读连接上的 authorizer（prepare_query_gate）兜底。规则：

    - 空白 / 注释之外最多出现一个分号，且分号只能是整条语句的结尾
      （允许结尾分号；分号前后都不得再有非空白 token，即空语句、多条
      语句、分号后再写内容一律拒绝）；
    - 第一个词法 token 必须是 SELECT 或 WITH；WITH 定义 CTE 后，跳过
      平衡的 CTE 括号，真正语句的第一个关键字必须仍是 SELECT（据此在
      词法层拒绝 WITH cte AS (...) DELETE/INSERT/UPDATE，不必等
      authorizer）；
    - 字符串（'…'，'' 为转义）、双引号 / 反引号 / [..] 标识符、行注释
      （--）与块注释（/* … */，不嵌套）内的分号与关键字都不算数；
      未闭合的引号 / 注释拒绝。

    返回 None 表示通过；否则返回拒绝消息（调用方以 400 拒绝）。
    """
    tokens = []  # 引号 / 注释之外的词法 token：("kw", 小写词) / ("(", ) /
                # (")", ) / (";", ) / ("x", )（其它，如数字 / 运算符）
    n = len(sql)
    i = 0
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i + 2)
            i = n if j == -1 else j + 1
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            if j == -1:
                return "unclosed block comment"
            i = j + 2
            continue
        if ch in ("'", '"', "`"):
            close = ch
            doubled = close + close
            j = i + 1
            while True:
                k = sql.find(close, j)
                if k == -1:
                    return "unclosed string or identifier literal"
                if sql[k:k + 2] == doubled:
                    j = k + 2
                    continue
                break
            tokens.append(("x",))
            i = k + 1
            continue
        if ch == "[":
            j = sql.find("]", i + 1)
            if j == -1:
                return "unclosed bracketed identifier"
            tokens.append(("x",))
            i = j + 1
            continue
        if ch == ";":
            if tokens and tokens[-1][0] == ";":
                return "only a single SELECT/WITH statement is allowed"
            tokens.append((";",))
            i += 1
            continue
        if ch == "(":
            tokens.append(("(",))
            i += 1
            continue
        if ch == ")":
            tokens.append((")",))
            i += 1
            continue
        # 普通词：标识符 / 关键字（字母、下划线、$ 开头）或数字 / 运算符
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in "_$"):
                j += 1
            tokens.append(("kw", sql[i:j].lower()))
            i = j
            continue
        # 数字、运算符、句点、逗号等：记录为不透明 token（分号 / 括号 /
        # 引号 / 注释已在前面单独处理），用于判定「分号前是否有语句」
        tokens.append(("x",))
        i += 1

    # 分号只能有一个且必须在末尾：删掉结尾分号后不应再出现分号，
    # 且分号前后都必须有真正的语句 token
    while tokens and tokens[-1][0] == ";":
        tokens.pop()
        if tokens and tokens[-1][0] == ";":
            return "only a single SELECT/WITH statement is allowed"
    if any(tok[0] == ";" for tok in tokens):
        return "only a single SELECT/WITH statement is allowed"
    words = [tok[1] for tok in tokens if tok[0] == "kw"]
    if not words:
        return "SQL must be a single SELECT/WITH statement"
    # 禁用函数（load_extension / pragma_* 表值函数）在词法层即拒绝：
    # 字符串 / 引号标识符内的同名文本不会被扫描成关键字，故不会误伤
    # 普通文本字面量
    for word in words:
        if word in SQL_FORBIDDEN_FUNCS or any(
            word.startswith(prefix) for prefix in SQL_FORBIDDEN_FUNC_PREFIXES
        ):
            return "function is not allowed in read-only queries"
    if words[0] not in SQL_LEAD_WORDS:
        return "only a single SELECT/WITH statement is allowed"
    if words[0] == "select":
        return None
    # WITH：跳过一个或多个 CTE 定义。每个 CTE 形如
    # 名称 [(列…)] AS (主体)，多个 CTE 以逗号分隔；主体括号内（可嵌套
    # 括号）的一切关键字都忽略。所有 CTE 之后的第一个词法关键字必须
    # 仍是 SELECT——WITH cte AS (...) DELETE/INSERT/UPDATE 在词法层
    # 即拒绝，不必等 authorizer。
    idx = 0
    length = len(tokens)
    while True:
        # 定位本 CTE 的 AS
        while idx < length and not (tokens[idx][0] == "kw" and tokens[idx][1] == "as"):
            idx += 1
        if idx >= length:
            return "WITH must be followed by a SELECT statement"
        idx += 1
        # 定位 AS 之后的主体起始括号
        while idx < length and tokens[idx][0] != "(":
            idx += 1
        if idx >= length:
            return "WITH must be followed by a SELECT statement"
        depth = 0
        while idx < length:
            kind = tokens[idx][0]
            if kind == "(":
                depth += 1
            elif kind == ")":
                depth -= 1
                if depth == 0:
                    idx += 1
                    break
            idx += 1
        # 主体结束后：跳过逗号（被扫描器记为不透明 token "x"）与空白；
        # 见到下一个关键字即做最终判定。
        while idx < length and tokens[idx][0] == "x":
            idx += 1
        if idx >= length:
            return "WITH must be followed by a SELECT statement"
        if tokens[idx][0] != "kw":
            return "WITH must be followed by a SELECT statement"
        if tokens[idx][1] == "select":
            return None
        # 形如「, cte2 AS (...)」的下一个 CTE：当前关键字是 CTE 名，
        # 继续循环；其余关键字（delete/insert/update/...）一律拒绝
        probe = idx + 1
        while probe < length and not (
            tokens[probe][0] == "kw" and tokens[probe][1] == "as"
        ):
            probe += 1
        if probe < length:
            idx += 1  # 越过 CTE 名，下一轮从它的 AS 解析
            continue
        return "only a single SELECT/WITH statement is allowed"


def open_query_connection(db_path):
    """打开一条**只读**查询连接（三重只读限制）。

    1. URI mode=ro：从文件层禁止写，连接本身不可能修改主库 / WAL；
    2. PRAGMA query_only=1：再由 SQLite 连接层拒绝任何写语句
       （须在 set_authorizer 之前执行——PRAGMA 也受 authorizer 约束）；
    3. set_authorizer 白名单：只允许读（READ）、SELECT 准备、递归 CTE
       与普通函数；PRAGMA / ATTACH / 事务 / 一切写入动作返回 DENY，
       load_extension 与 pragma_* 表值函数在函数动作上单独拒绝。
    busy_timeout=0：查询遇到写锁 / 检查点冲突立即报错，不等待两栏。
    """
    uri = "file:%s?mode=ro" % urllib.parse.quote(os.path.abspath(db_path))
    conn = sqlite3.connect(
        uri, uri=True, timeout=0, isolation_level=None, check_same_thread=False
    )
    conn.execute("PRAGMA busy_timeout=0")
    conn.execute("PRAGMA query_only=1")

    def query_authorizer(action, arg1, arg2, db_name, trigger):
        if action in (AUTH_READ, AUTH_SELECT, AUTH_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == AUTH_FUNCTION:
            # SQLITE_FUNCTION 回调：arg1 恒为 None，函数名在 arg2
            fname = (arg2 or "").lower()
            # 扩展加载绝不允许；pragma 函数表（pragma_*）等同 PRAGMA 拒绝。
            # 其余普通函数（abs / coalesce / json / ...）放行。
            if fname == "load_extension" or fname.startswith("pragma_"):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    conn.set_authorizer(query_authorizer)
    return conn


def prepare_query_gate(db_path, sql):
    """提交前策略闸：用只读连接以 EXPLAIN 预编译 SQL，不执行、不取行。

    词法扫描（scan_single_read_sql）通过后再做这一步，只拦截**策略
    违规**：authorizer 拒绝（SQLITE_AUTH，写入 / PRAGMA / ATTACH /
    load_extension / 数据修改型 CTE）与 sqlite3 的多条语句 Warning 返回
    拒绝消息（HTTP 400）。其余编译期错误（如「无此表 / 无此列」）**不
    在此拒绝**——SQL 形式合法、只是运行时才失败的查询应当入队，worker
    执行后状态为 failed 并附原始 SQLite 错误名 / 错误码。预编译只生成
    执行计划，绝不运行用户语句，因此不产生结果、不建读快照。
    """
    try:
        gate = open_query_connection(db_path)
    except sqlite3.Error as exc:
        return "cannot open read-only query connection: %s" % exc
    try:
        try:
            gate.execute("EXPLAIN " + sql).close()
        except sqlite3.Warning:
            return "only a single SELECT/WITH statement is allowed"
        except sqlite3.Error as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            if code == 23:  # SQLITE_AUTH：authorizer 策略拒绝
                return "SQL is not allowed: only a single read-only SELECT/WITH query"
            # 语法 / 对象不存在等：放行给 worker，运行时如实记 failed
    finally:
        gate.close()
    return None


def timeline_public_view(record):
    """时间线记录的对外视图：整数无损编码（大整数用十进制字符串）。

    普通记录字段固定为 seq / side / before / after / deltas；补偿记录
    （side=system）额外携带 compensates（被补偿记录的原 seq）。
    """
    view = {
        "seq": record["seq"],
        "side": record["side"],
        "before": json_int(record["before"]),
        "after": json_int(record["after"]),
        "deltas": [json_int(delta) for delta in record["deltas"]],
    }
    if record["side"] == "system":
        view["compensates"] = record["compensates"]
    return view


class PanelState:
    """某一栏的展示状态。last_read / log 记录在内存中，刷新状态不触碰 SQLite。"""

    def __init__(self):
        self.last_read = None
        self.log = []
        # 当前事务是否已建立读快照：显式读值后为 True，
        # 成功写入后也为 True；BEGIN 与 COMMIT/ROLLBACK 后重置。
        self.tx_has_snapshot = False
        # 本栏只读开关（PRAGMA query_only）：默认关闭，只保存在内存。
        # 开关在原长期连接上生效，不换连接、不结束事务；
        # COMMIT / ROLLBACK 不重置它，两栏各自独立。
        self.readonly = False
        # 当前事务内已成功、尚未提交的加法：[(checked_add 捕获的行值, 增量), ...]
        # 仅供提交时间线使用：COMMIT 成功才生成记录；ROLLBACK / 隐式回滚丢弃；
        # 不随状态接口返回（未提交内容不可见）。
        self.tx_adds = []

    def add_log(self, text):
        self.log.append("%s  %s" % (time.strftime("%H:%M:%S"), text))
        if len(self.log) > MAX_LOG_ENTRIES:
            del self.log[: len(self.log) - MAX_LOG_ENTRIES]


class Experiment:
    def __init__(self, exp_id):
        self.id = exp_id
        self.db_path = os.path.join(DATA_DIR, "exp_%s.db" % exp_id)
        # 建库 / 打开已有库，并从 timeline 表加载已持久化的提交时间线
        # （新库与旧版库为空时间线基线；kv 现值保留在库中，无需读出）
        self.timeline = self._init_db()
        # 两栏各自的长期连接：isolation_level=None 进入自动提交模式，
        # 事务完全由显式 BEGIN/COMMIT/ROLLBACK 控制；
        # busy_timeout=0 让写锁冲突立即报错而不是等待。
        # 两个写连接均关闭自动检查点（wal_autocheckpoint=0），
        # 使 WAL 回收只由手动 PASSIVE / TRUNCATE 检查点触发。
        # overflow_flags 供连接上的 checked_add 函数在判定 64 位溢出时置位，
        # 以便把溢出与普通 SQLite 错误区分开，沿用既有的拒绝响应。
        # add_base_slots 供 checked_add 在 UPDATE 语句内暂存本次成功加法
        # 基于的行值（事务自身视图），run_op 在语句成功后读槽记入本事务
        # 待提交增量，供提交时间线使用；全程不执行任何额外读取。
        self.overflow_flags = {"a": [False], "b": [False]}
        self.add_base_slots = {"a": [None], "b": [None]}
        self.conns = {
            "a": self._open_conn(
                self.overflow_flags["a"], self.add_base_slots["a"], autockpt=False
            ),
            "b": self._open_conn(
                self.overflow_flags["b"], self.add_base_slots["b"], autockpt=False
            ),
        }
        self.conn_locks = {"a": threading.Lock(), "b": threading.Lock()}
        # 第三条连接：只读已提交值
        self.observer = self._open_conn()
        self.observer_lock = threading.Lock()
        self.panels = {"a": PanelState(), "b": PanelState()}
        # 检查点状态只保存在内存：状态刷新只读这份缓存，不触发检查点
        self.checkpoint_lock = threading.Lock()
        self.checkpoint_last = None
        self.checkpoint_log = []
        # 删除准入控制：gate 同时保护 active_calls / deleting / deleted。
        # 删除开始时先置 deleting 并等待已准入请求（active_calls）全部完成，
        # 此后新请求一律 410；拆除连接与文件期间不可能有请求触碰它们。
        # 门控按实验独立，等待删除绝不阻塞其他实验。
        self.gate = threading.Condition(threading.Lock())
        self.active_calls = 0
        self.deleting = False
        self.deleted = False
        # 提交时间线：与 kv 值同库持久化（timeline 表），构造时由
        # _init_db 从库中加载（新库 / 旧版库以空时间线为基线）。普通提交
        # 与补偿都把「值 + 记录」放在同一事务提交，进程任意时刻终止后
        # 只能恢复二者都生效或都未生效的状态，不依赖退出清理。
        # timeline_lock 保证「COMMIT 成功 → 追加记录」在同一临界区完成，
        # 记录编号顺序与实际提交顺序严格一致；查询只读内存快照，
        # 不触碰甲、乙长期连接与 observer。
        self.timeline_lock = threading.Lock()
        # 脚本逐步执行状态：只保存在内存，无需跨重启保留。
        # script_lock 串行化「装载 / 推进 / 跳过 / 取消」与手工操作的
        # active 互斥复查；同实验的脚本动作先在该锁上核对脚本 ID 与步号，
        # 再真实执行，过期请求一律 409，不会触碰到新脚本。
        # 手工操作 / 只读切换路由只在该锁内做「复查 + 在途计数」，
        # 计数后即释放锁再执行，两栏手工请求互不等待（同栏请求仍由本栏
        # conn_lock 串行）。锁内读标志只能用 _script_active_locked()——
        # threading.Lock 不可重入，锁内再次获取会让请求永久卡住，
        # 并占住准入名额，使随后的脚本查询、推进与删除一并阻塞。
        self.script_lock = threading.Lock()
        # 在途手工请求计数（按栏）：手工路由在 script_lock 内通过 active
        # 复查后 +1，执行结束（含失败 / 异常）在同一把锁内 -1 并唤醒
        # manual_cond。装载在复查两栏 in_transaction 之前，于 script_lock
        # 内等待两栏计数归零：已通过复查的在途手工 BEGIN 必然先执行完，
        # 装载不会抢在它前面成功，杜绝「装载成功后手工 BEGIN 才开启事务」。
        # 等待经 wait() 释放 script_lock，在途请求照常执行并归还计数，
        # 另一栏新的手工请求在此期间也不被阻塞。
        self.manual_cond = threading.Condition(self.script_lock)
        self.manual_active = {"a": 0, "b": 0}
        self.script = None  # None 或 {id, steps, cursor, records, status}
        # 只读 SQL 工作台：每实验只保留最新一条查询（只保存在内存，
        # 不跨重启恢复）。query_lock 同时保护 query_job 与 job 内取消
        # 标志；query_cond 供取消 / 删除请求等待终态（连接关闭后才发布）。
        # worker 用**全新的独立只读连接**执行，与两栏长期连接、observer、
        # 检查点 / 补偿连接都无关；running 时再提交返回 409，终结后新
        # 提交替换旧 job，旧查询 ID 一律 404。
        self.query_lock = threading.Lock()
        self.query_cond = threading.Condition(self.query_lock)
        self.query_job = None

    def _init_db(self):
        """建库 / 打开已有库，返回从 timeline 表加载的提交时间线。

        新库：建 kv 表并插入初始值 100、建空 timeline 表。已存在的库
        （含旧版无 timeline 表的库）：INSERT OR IGNORE 保留现值，
        CREATE TABLE IF NOT EXISTS 补齐 timeline 表——旧版库首次加载
        保留现值，以空时间线为基线。deltas 以 JSON 文本存储，Python
        整数无损（大整数经十进制文本往返，精度不变）。
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kv (id INTEGER PRIMARY KEY"
                " CHECK (id = 1), val INTEGER NOT NULL)"
            )
            # 已存在的行（含旧版库的现值）保留；仅新库插入初始值 100
            conn.execute("INSERT OR IGNORE INTO kv (id, val) VALUES (1, 100)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS timeline ("
                " seq INTEGER PRIMARY KEY,"
                " side TEXT NOT NULL,"
                " before_val INTEGER NOT NULL,"
                " after_val INTEGER NOT NULL,"
                " deltas TEXT NOT NULL,"
                " compensates INTEGER)"
            )
            conn.commit()
            rows = conn.execute(
                "SELECT seq, side, before_val, after_val, deltas, compensates"
                " FROM timeline ORDER BY seq"
            ).fetchall()
        finally:
            conn.close()
        timeline = []
        for seq, side, before, after, deltas_json, compensates in rows:
            record = {
                "seq": seq,
                "side": side,
                "before": before,
                "after": after,
                "deltas": json.loads(deltas_json),
            }
            if side == "system":
                record["compensates"] = compensates
            timeline.append(record)
        return timeline

    def _open_conn(self, overflow_flag=None, add_base_slot=None, autockpt=True):
        conn = sqlite3.connect(
            self.db_path, timeout=0, isolation_level=None, check_same_thread=False
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=0")
        if not autockpt:
            # 关闭本连接提交时的自动检查点：检查点时机完全交由
            # 手动 PASSIVE / TRUNCATE 按钮（HTTP）控制。
            conn.execute("PRAGMA wal_autocheckpoint=0")
        if overflow_flag is not None:
            # checked_add 在 UPDATE 内部做最终的 64 位边界判定。
            # SQLite 对整数溢出不报错而会静默存成 REAL，必须在写入前拦下：
            # 溢出时置位并抛异常终止本次语句，行不会被修改，事务保持开启。
            # 函数只做纯计算，不执行任何读取，不会影响快照与加锁顺序。
            def checked_add(current, delta):
                result = current + delta
                if not INT64_MIN <= result <= INT64_MAX:
                    overflow_flag[0] = True
                    raise OverflowError("累加结果超出有符号 64 位整数范围")
                if add_base_slot is not None:
                    # 暂存本次成功加法基于的行值（UPDATE 语句内的事务
                    # 自身视图），供提交时间线记录「首次成功加法前的值」；
                    # 纯计算不执行任何读取，不影响快照与加锁顺序。
                    add_base_slot[0] = current
                return result

            conn.create_function("checked_add", 2, checked_add)
        return conn

    def panel_state(self, side):
        # 只读取内存状态与连接标志位，绝不执行 SELECT，
        # 避免为 DEFERRED 事务提前建立读快照。
        panel = self.panels[side]
        return {
            "in_transaction": self.conns[side].in_transaction,
            "readonly": panel.readonly,
            "last_read": json_int(panel.last_read),
            "log": list(panel.log),
        }

    def committed_value(self):
        with self.observer_lock:
            row = self.observer.execute(
                "SELECT val FROM kv WHERE id = 1"
            ).fetchone()
            return row[0]

    def build_timeline_record(self, side, tx_adds):
        """由本事务成功增量构造时间线记录（尚未持久化、未入内存）。

        before 取自本事务首次成功加法时 checked_add 在 UPDATE 语句内捕获的
        行值（事务自身视图）；after = before + 全部成功增量之和——首次成功
        加法已持有写锁直至提交，期间无其他连接能修改该行，因此提交后的值
        无需、也绝不允许事后读取别栏新值得到。seq 为下一条编号，
        调用方必须持有 timeline_lock。
        """
        before = tx_adds[0][0]
        after = before
        deltas = []
        for _base, delta in tx_adds:
            after += delta
            deltas.append(delta)
        return {
            "seq": len(self.timeline) + 1,
            "side": side,
            "before": before,
            "after": after,
            "deltas": deltas,
        }

    @staticmethod
    def insert_timeline_record(conn, record):
        """把记录写入本实验库的 timeline 表。

        必须在提交 kv 修改的**同一事务**内执行（普通提交在两栏连接上、
        补偿在独立连接的显式事务中），使值与记录一同生效或一同回滚——
        进程任意时刻终止都不出现半生效状态。deltas 以 JSON 文本存储，
        Python 整数无损。
        """
        conn.execute(
            "INSERT INTO timeline (seq, side, before_val, after_val, deltas,"
            " compensates) VALUES (?, ?, ?, ?, ?, ?)",
            (
                record["seq"],
                record["side"],
                record["before"],
                record["after"],
                json.dumps(record["deltas"]),
                record.get("compensates"),
            ),
        )

    def _timeline_view_locked(self):
        # 时间线对外视图；调用方必须持有 timeline_lock
        return [timeline_public_view(record) for record in self.timeline]

    def timeline_records(self):
        # 只读内存中的时间线：不执行任何 SQL、不触碰两栏连接与 observer，
        # 不提前建立快照，也不改变最近一次显式读值
        with self.timeline_lock:
            return self._timeline_view_locked()

    @staticmethod
    def validate_steps(raw):
        """整份校验脚本：必须是 1～50 步的 JSON 数组。

        每步必须是对象，含 side ∈ {a,b} 与 op ∈ VALID_OPS；add 另含
        合法整数 value（沿用手工加法的 parse_int 与 ±2⁶² 上限，大整数
        以十进制字符串接受）。任何不合法都返回错误消息，调用方以 400
        整份拒绝——绝不部分装载。
        """
        if not isinstance(raw, list):
            return "body must be a JSON array of steps"
        if not raw:
            return "script must contain 1..%d steps" % MAX_SCRIPT_STEPS
        if len(raw) > MAX_SCRIPT_STEPS:
            return "script must contain at most %d steps" % MAX_SCRIPT_STEPS
        for index, step in enumerate(raw):
            where = "step %d" % (index + 1)
            if not isinstance(step, dict):
                return "%s must be an object" % where
            side = step.get("side")
            if side not in ("a", "b"):
                return "%s: side must be 'a' or 'b'" % where
            op = step.get("op")
            if op not in VALID_OPS:
                return "%s: op must be one of begin/read/add/commit/rollback" % where
            if op == "add":
                value = parse_int(step.get("value"))
                if value is None:
                    return "%s: value must be an integer" % where
                if abs(value) > MAX_ADD_ABS:
                    return "%s: value too large" % where
        return None

    def load_script(self, raw):
        """装载新脚本。调用方已完成整份校验。

        仅当两栏均无事务且没有 active 脚本时才允许；在 script_lock 内先
        等待已准入的在途手工请求收尾（manual_active 归零），再同时持两栏
        conn_lock 复查 in_transaction，与手工 BEGIN 严格互斥：不会出现
        「检查时无事务、装载时已 BEGIN」，也不会出现「装载成功后手工
        BEGIN 才开启事务」。成功后旧脚本的查看记录被新脚本替换，旧脚本的
        在途动作因 ID 不符一律 409。
        """
        with self.script_lock:
            # 等待已通过 active 复查的在途手工请求执行完：它们可能即将
            # BEGIN。wait() 释放 script_lock，在途请求照常推进并在结束时
            # 归还计数、唤醒本等待；另一栏新的手工请求在此期间也不被阻塞。
            # 唤醒后必须重查 active：等待期间另一装载请求可能已先激活脚本。
            while True:
                if self.script is not None and self.script["status"] == "active":
                    return None, {
                        "error": "an active script already exists; cancel it first"
                    }
                if not self.manual_active["a"] and not self.manual_active["b"]:
                    break
                self.manual_cond.wait()
            with self.conn_locks["a"], self.conn_locks["b"]:
                if self.conns["a"].in_transaction or self.conns["b"].in_transaction:
                    return None, {
                        "error": "both connections must be out of transaction "
                        "before loading a script"
                    }
                self.script = {
                    "id": uuid.uuid4().hex[:12],
                    # add 的 value 规范化为 Python int（大整数十进制字符串
                    # 同样解析），保证 run_op 拿到的与手工接口完全一致
                    "steps": [
                        dict(step, value=parse_int(step["value"]))
                        if step["op"] == "add"
                        else dict(step)
                        for step in raw
                    ],
                    "cursor": 0,  # 尚未执行的步骤下标（从 0 开始；步号 = cursor + 1）
                    "records": [],
                    "status": "active",  # active / finished / cancelled
                }
                return self.script_view(self.script), None

    def _script_record(self, step_no, step, result, skipped=False):
        """构造一条执行记录。result 为 run_op 的返回（跳过则为 None）。

        value 仅 add 出现，沿用 json_int 无损编码；失败如实记录 run_op
        给出的原始错误名 / 错误码（拒绝类为 null）；跳过不执行任何 SQL。
        """
        record = {
            "step": step_no,
            "side": step["side"],
            "op": step["op"],
        }
        if step["op"] == "add":
            record["value"] = json_int(step["value"])
        if skipped:
            record["result"] = "skipped"
            record["error"] = None
        else:
            record["result"] = "success" if result["ok"] else "failure"
            record["error"] = None if result["ok"] else dict(result["error"])
        return record

    def _script_step_public(self, step_no, step):
        item = {"step": step_no, "side": step["side"], "op": step["op"]}
        if step["op"] == "add":
            item["value"] = json_int(step["value"])
        return item

    def script_view(self, script):
        """脚本视图（只读内存，绝不执行任何 SQL、不在两栏连接上读值）。

        active 时 step 为从 1 开始的当前（下一步）步号；finished 全部
        走完，step 为 None；cancelled 在取消的步号处停止（该步未执行）。
        """
        if script is None:
            return None
        total = len(script["steps"])
        if script["status"] == "active":
            step = script["cursor"] + 1
        elif script["status"] == "cancelled":
            step = min(script["cursor"] + 1, total)
        else:
            step = None
        return {
            "id": script["id"],
            "status": script["status"],
            "step": step,
            "total": total,
            "steps": [
                self._script_step_public(index + 1, step)
                for index, step in enumerate(script["steps"])
            ],
            "records": [dict(record) for record in script["records"]],
        }

    def script_state(self):
        with self.script_lock:
            return self.script_view(self.script)

    def _check_script_action(self, script, script_id, step_no):
        """在 script_lock 内核对推进 / 跳过 / 取消请求。

        无脚本、脚本 ID 与当前不符（旧脚本的过期请求，含已被新脚本替换）、
        脚本已结束 / 取消、步号不是当前步，一律视为过期 / 无效请求；
        返回错误字符串（HTTP 409），不执行、不记录、不影响新脚本。
        """
        if script is None or script["id"] != script_id:
            return "script id is stale; current script differs"
        if script["status"] != "active":
            return "script is %s; no more actions accepted" % script["status"]
        if step_no != script["cursor"] + 1:
            return "stale step: current step is %d" % (script["cursor"] + 1)
        return None

    def advance_script(self, script_id, step_no):
        """在原连接真实执行当前步。成功才前移；失败停在原步，可重试 / 跳过。"""
        with self.script_lock:
            script = self.script
            stale = self._check_script_action(script, script_id, step_no)
            if stale is not None:
                return self.script_view(script), False, {"error": stale}
            step = script["steps"][script["cursor"]]
            # run_op 只取该栏自己的 conn_lock 与 timeline_lock，
            # 与已持有的 script_lock 不构成环；真实在该栏原长期连接执行。
            result = run_op(self, step["side"], step["op"], step.get("value"))
            script["records"].append(
                self._script_record(step_no, step, result)
            )
            if result["ok"]:
                # 成功才前移；失败不代为回滚，事务与步号都停在原处
                script["cursor"] += 1
                if script["cursor"] >= len(script["steps"]):
                    script["status"] = "finished"
            # 失败：原样保留错误名 / 码，不自动提交或回滚
            return self.script_view(script), result["ok"], None

    def skip_script(self, script_id, step_no):
        """跳过当前步：只记 skipped，不执行任何 SQL、不改变事务状态。"""
        with self.script_lock:
            script = self.script
            stale = self._check_script_action(script, script_id, step_no)
            if stale is not None:
                return self.script_view(script), False, {"error": stale}
            step = script["steps"][script["cursor"]]
            script["records"].append(
                self._script_record(step_no, step, None, skipped=True)
            )
            script["cursor"] += 1
            if script["cursor"] >= len(script["steps"]):
                script["status"] = "finished"
            return self.script_view(script), True, None

    def cancel_script(self, script_id, step_no):
        """取消脚本：取消的步号处停止；不提交、不回滚，手工操作随即恢复。"""
        with self.script_lock:
            script = self.script
            stale = self._check_script_action(script, script_id, step_no)
            if stale is not None:
                return self.script_view(script), False, {"error": stale}
            script["status"] = "cancelled"
            return self.script_view(script), True, None

    def script_active(self):
        # 手工事务操作与只读切换的准入检查（script_lock 内读标志）
        with self.script_lock:
            return self._script_active_locked()

    def _script_active_locked(self):
        # 调用方必须已持有 script_lock。手工操作 / 只读切换路由在
        # 「复查 + 在途计数」临界区持有同一把锁（见路由注释），这里绝不能
        # 再获取：threading.Lock 不可重入，重复获取会让请求永久卡住，
        # 占住准入名额并阻塞随后的脚本查询、推进与删除。
        return self.script is not None and self.script["status"] == "active"

    def checkpoint_state(self):
        # 只读内存缓存：不执行任何 SQL、不触发检查点
        return {"last": self.checkpoint_last, "log": list(self.checkpoint_log)}

    def add_checkpoint_log(self, text):
        self.checkpoint_log.append("%s  %s" % (time.strftime("%H:%M:%S"), text))
        if len(self.checkpoint_log) > MAX_LOG_ENTRIES:
            del self.checkpoint_log[: len(self.checkpoint_log) - MAX_LOG_ENTRIES]

    def wal_size(self):
        # 操作后读取 WAL 文件字节数；文件不存在时记 0
        try:
            return os.path.getsize(self.db_path + WAL_SUFFIX)
        except OSError:
            return 0

    # ================= 只读 SQL 工作台 =================

    QUERY_TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")

    @staticmethod
    def encode_query_cell(value):
        """查询结果单元格编码：整数沿用 json_int 无损约定；BLOB 转成
        X'…' 形式的小写十六进制文本（空 BLOB 为 X''）；None / float /
        str 原样返回（JSON 无整数精度问题，REAL 为浮点数）。"""
        if isinstance(value, bool):  # 防御：SQLite 不返回 bool
            return value
        if isinstance(value, int):
            return json_int(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "X'%s'" % bytes(value).hex()
        return value

    def _query_view_locked(self, job):
        """查询对外视图。调用方必须持有 query_lock。

        running 只回 id/status/sql；succeeded 附 columns / rows /
        truncated（前 100 行）；failed / cancelled 附 error（取消时
        error 为 null）。终态字段只有在 worker 关闭连接之后才写入。
        """
        view = {"id": job["id"], "status": job["status"], "sql": job["sql"]}
        if job["status"] == "succeeded":
            view["columns"] = list(job["columns"])
            view["rows"] = [[self.encode_query_cell(v) for v in row]
                            for row in job["rows"]]
            view["truncated"] = job["truncated"]
        if job["status"] in ("failed", "cancelled"):
            view["error"] = job.get("error")
        return view

    def submit_query(self, sql):
        """提交一条只读查询。调用方已完成长度与策略校验。

        每实验只保留最新一条：已有 running 查询返回 409 语义（不新建、
        不影响在途查询）；无查询或上一条已终结则新建 job 并启动 worker，
        旧 job（及其查询 ID）立即不可见——旧 ID 查询 / 取消返回 404。
        返回 (view, rejected)：rejected 非 None 时调用方返回 409。
        """
        with self.query_lock:
            current = self.query_job
            if current is not None and current["status"] == "running":
                return None, {
                    "error": "a query is already running; cancel it or wait"
                }
            job = {
                "id": uuid.uuid4().hex[:12],
                "sql": sql,
                "status": "running",  # running/succeeded/failed/cancelled
                "cancel": False,      # 取消标志（取消请求在锁内置位）
                "conn": None,         # worker 打开的只读连接（供 interrupt）
                "error": None,
            }
            self.query_job = job
            thread = threading.Thread(
                target=self._run_query_job, args=(job,), daemon=True
            )
            job["thread"] = thread
            thread.start()
            return self._query_view_locked(job), None

    def get_query(self, query_id):
        """按 ID 取查询视图；不是当前最新查询（从未存在 / 已被新查询
        替换 / 重启后内存清空）一律返回 None，调用方以 404 回应。"""
        with self.query_lock:
            job = self.query_job
            if job is None or job["id"] != query_id:
                return None
            return self._query_view_locked(job)

    def cancel_query(self, query_id):
        """取消查询并等其进入终态（连接已关闭）。

        - 旧 / 未知查询 ID：返回 ("missing", None)，调用方以 404 回应；
        - 已终结（succeeded/failed/cancelled）：返回 ("terminal", view)，
          不改变结果（取消先被确认则不可能走到这里——worker 发布终态前
          会再查取消标志）；
        - running：锁内置取消标志并对 worker 连接 interrupt（连接尚未
          打开则只置标志，worker 在开连接 / 执行前自行复查），随后等待
          终态发布，返回 ("terminal", cancelled 视图)。
        """
        with self.query_lock:
            job = self.query_job
            if job is None or job["id"] != query_id:
                return "missing", None
            if job["status"] in self.QUERY_TERMINAL_STATUSES:
                return "terminal", self._query_view_locked(job)
            job["cancel"] = True
            conn = job.get("conn")
            if conn is not None:
                # 中断真实执行中的 SQL：长查询（如无界递归 CTE）立即
                # 收到 SQLITE_INTERRUPT(9)
                try:
                    conn.interrupt()
                except sqlite3.Error:
                    pass
            # 必须等到**本 job** 进入终态（worker 关闭连接后才发布）：
            # 谓词同时要求 id 一致，避免其他查询的 notify（如上一条查询
            # 终结、唤醒后被新提交替换）让等待提前返回。
            current_id = job["id"]
            self.query_cond.wait_for(
                lambda: (
                    self.query_job is not None
                    and self.query_job["id"] == current_id
                    and self.query_job["status"]
                    in self.QUERY_TERMINAL_STATUSES
                )
                or self.query_job is not job
            )
            # 等待期间理论上不会被新提交替换（替换只发生在终结之后），
            # 稳妥起见：若 job 已不是当前 job，按旧 ID 404 语义处理
            if self.query_job is not job:
                return "missing", None
            return "terminal", self._query_view_locked(job)

    def cancel_inflight_queries(self):
        """删除拆除前调用：取消在途查询并等其工作线程结束。

        先在锁内置取消标志、interrupt 真实 SQL，再在锁外 join，避免
        worker 终态发布（需要取 query_lock）与本等待形成阻塞。必须在
        关闭两栏连接与删除文件**之前**完成：worker 发布任何终态前都已
        关闭自己的只读连接，因此 join 返回后绝不可能再触碰数据库文件。
        """
        with self.query_lock:
            job = self.query_job
            if job is None or job["status"] != "running":
                return
            job["cancel"] = True
            conn = job.get("conn")
            if conn is not None:
                try:
                    conn.interrupt()
                except sqlite3.Error:
                    pass
            thread = job.get("thread")
        if thread is not None:
            thread.join()

    def _publish_terminal(self, job, status, **fields):
        """终态发布的唯一入口：调用方必须**已关闭查询连接**。

        连接关闭后才在锁内公布终态并唤醒取消 / 删除等待者；取消标志在
        最后一刻再复查一次——SQL 恰好跑完、但取消请求已先被确认时，
        发布的终态仍是 cancelled（取消先被确认则绝不能成功）。
        """
        with self.query_lock:
            if job["status"] != "running":
                # 理论不可达：终态只发布一次；防御性直接返回现有终态
                return
            if job["cancel"] and status == "succeeded":
                status = "cancelled"
                fields = {}
            job["status"] = status
            for key, value in fields.items():
                job[key] = value
            self.query_cond.notify_all()

    def _run_query_job(self, job):
        """查询工作线程：独立只读连接执行，终态在连接关闭后才发布。

        取消在三个时点生效：打开连接前、执行 SQL 前（尚未开跑也能
        取消）、以及真实执行中（连接 interrupt 令长查询立即中断）。
        无论成功 / 失败 / 取消都不改实验库的值与时间线。
        """
        sql = job["sql"]
        if job["cancel"]:
            self._publish_terminal(job, "cancelled", error=None)
            return
        conn = None
        published = False
        try:
            conn = open_query_connection(self.db_path)
            with self.query_lock:
                job["conn"] = conn
                # 打开连接期间取消可能已到（interrupt 时连接尚未登记，
                # 错过 interrupt 的那次调用）：执行前再复查一次
                cancelled = job["cancel"]
            if cancelled:
                # 尚未开跑就被取消：先关闭连接，关闭后才公布终态
                conn.close()
                self._publish_terminal(job, "cancelled", error=None)
                published = True
                return
            cursor = conn.execute(sql)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchmany(MAX_QUERY_ROWS + 1)
            truncated = len(rows) > MAX_QUERY_ROWS
            if truncated:
                rows = rows[:MAX_QUERY_ROWS]
            conn.close()
            conn = None
            self._publish_terminal(
                job,
                "succeeded",
                columns=columns,
                rows=rows,
                truncated=truncated,
            )
            published = True
        except sqlite3.Error as exc:
            # 连接须先关闭，关闭后才公布终态（成功路径同理）
            if conn is not None:
                conn.close()
            # 取消先被确认（worker 执行期间取消请求置位并 interrupt）：
            # 即使 SQLite 返回的不是中断错误码，终态也必须是 cancelled；
            # interrupt 引发的 SQLITE_INTERRUPT(9) 同样记 cancelled。
            # 其余 SQLite 错误（无此表、被 authorizer 拒绝等）记 failed，
            # 并如实附原始错误名 / 错误码。
            code = getattr(exc, "sqlite_errorcode", None)
            if job["cancel"] or code == 9:
                self._publish_terminal(job, "cancelled", error=None)
            else:
                name = getattr(exc, "sqlite_errorname", None) or "SQLITE_ERROR"
                self._publish_terminal(
                    job,
                    "failed",
                    error={"name": name, "code": code, "message": str(exc)},
                )
            published = True
        finally:
            # 防御性兜底：任何未发布终态的退出（如非 sqlite 异常路径）
            # 都必须先关掉连接、给出终态，避免取消 / 删除等待者永久阻塞。
            # 不预设 status：_publish_terminal 仍会据取消标志做最终裁决。
            if not published:
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
                self._publish_terminal(job, "failed", error=None)

    def admit_call(self):
        """请求准入：实验存活则计入 active_calls 并返回 True。

        删除开始后（deleting/deleted）所有新请求一律拒绝，由调用方返回 410。
        """
        with self.gate:
            if self.deleting or self.deleted:
                return False
            self.active_calls += 1
            return True

    def finish_call(self):
        # 请求结束（含异常）：归还名额并唤醒删除者。调用方在 finally 中调用，
        # 此时已退出 run_op / run_checkpoint 的资源临界区（conn_lock、
        # observer_lock、checkpoint_lock 均已释放），因此这里只触碰门控，
        # 不会与 teardown 取资源锁形成锁序环。
        with self.gate:
            self.active_calls -= 1
            if self.active_calls <= 0:
                self.gate.notify_all()

    def begin_delete(self):
        """等待此前已进入的请求全部正常完成，并把删除标记为已完成。

        必须在 claim_or_wait_delete 返回 True 之后调用。等待期间其他实验的
        请求完全不受影响（各自持有自己的 gate）。结束时必须 notify_all，
        唤醒所有在 claim 之后、完成之前进入等待的并发 DELETE。
        """
        with self.gate:
            while self.active_calls > 0:
                self.gate.wait()
            self.deleted = True
            self.gate.notify_all()

    def claim_or_wait_delete(self):
        """在同一段门控临界区内争取删除权或等待删除完成。

        必须原子完成「检查 deleting → 标记 / 进入等待」，否则失败者在
        claim 与 wait 之间释放门控，获胜者可能已收尾并 notify_all，
        迟到的等待者将永久阻塞。

        返回 True 表示本请求获得删除执行权（已标记 deleting）；
        返回 False 表示删除已结束（或在等待期间结束），调用方应返回 410。
        """
        with self.gate:
            if not self.deleting:
                self.deleting = True
                self.gate.notify_all()
                return True
            while not self.deleted:
                self.gate.wait()
            return False

    def teardown(self):
        """删除的拆除阶段：必须在 begin_delete 之后调用。

        先取消在途只读查询并等待其工作线程结束（worker 发布任何终态前
        都已关闭自己的只读连接），再回滚两栏未提交事务，依次关闭甲、乙
        长期连接与 observer；全部关闭后才移除数据库及 WAL、SHM 文件。
        此时不可能有在途请求、在途查询或在途检查点连接，因此检查点 /
        查询都不会在删除后重新创建文件。
        """
        self.cancel_inflight_queries()
        for side in ("a", "b"):
            with self.conn_locks[side]:
                conn = self.conns[side]
                try:
                    if conn.in_transaction:
                        # 系统只在此时代为回滚未提交事务
                        conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                conn.close()
        with self.observer_lock:
            self.observer.close()
        for suffix in ("", WAL_SUFFIX, SHM_SUFFIX):
            path = self.db_path + suffix
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


def run_op(exp, side, op, value):
    """在指定栏的长期连接上真实执行一个操作，返回 {ok, error, state}。"""
    panel = exp.panels[side]
    conn = exp.conns[side]
    label = "加上 %s" % value if op == "add" else OP_LABELS.get(op, op)

    def reject(message):
        panel.add_log("%s → 拒绝：%s" % (label, message))
        return {
            "ok": False,
            "error": {"name": None, "code": None, "message": message},
            "state": exp.panel_state(side),
        }

    with exp.conn_locks[side]:
        try:
            if op == "begin":
                if conn.in_transaction:
                    return reject("事务已开启，不能重复 BEGIN")
                conn.execute("BEGIN DEFERRED")
                # 新事务从空的待记录增量开始（正常已为空，防御性重置）
                panel.tx_adds = []
                panel.tx_has_snapshot = False
                panel.add_log("BEGIN DEFERRED → 成功，事务已开启")
            elif op == "read":
                row = conn.execute("SELECT val FROM kv WHERE id = 1").fetchone()
                panel.last_read = row[0]
                panel.tx_has_snapshot = conn.in_transaction
                scope = "事务内" if conn.in_transaction else "事务外（自动提交）"
                panel.add_log("读值（%s）→ %s" % (scope, row[0]))
            elif op == "add":
                if not conn.in_transaction:
                    return reject("禁止事务外修改：请先 BEGIN DEFERRED")
                overflow_message = OVERFLOW_MESSAGE
                if panel.tx_has_snapshot:
                    # 本事务已有读快照（显式读值或此前写过）：
                    # 直接在事务内读当前值做精确边界检查，行为与单事务内
                    # 可见性完全一致。快照过时导致的写冲突仍由 SQLite
                    # 如实报 SQLITE_BUSY_SNAPSHOT，不做任何自动处理。
                    current = conn.execute(
                        "SELECT val FROM kv WHERE id = 1"
                    ).fetchone()[0]
                    if not INT64_MIN <= current + value <= INT64_MAX:
                        return reject(overflow_message)
                else:
                    # 关键路径：事务尚无读快照（BEGIN 后第一次操作就是写）。
                    # 绝不能先在本连接 SELECT —— 那会建立读快照：若对方正
                    # 持有写锁，本次 UPDATE 拿到 SQLITE_BUSY 后快照已留下，
                    # 对方提交后重试就会被误报为 SQLITE_BUSY_SNAPSHOT。
                    # 这里用 observer 看到的已提交值做无锁预判：仅当
                    # 「已提交值 + 增量」本身就越界时直接拒绝，从而保持
                    # 溢出拒绝不获取写锁、不影响另一栏的原有行为。
                    committed = exp.committed_value()
                    if not INT64_MIN <= committed + value <= INT64_MAX:
                        return reject(overflow_message)
                # 最终精确判定放在 UPDATE 内的 checked_add：
                # 无快照预判与真实写入之间若对方恰好提交了别的值，
                # 仍以语句内计算为准，保证永远不会静默写成 REAL。
                # 纯写语句在写锁被占时返回 SQLITE_BUSY 且不建立读快照，
                # 因此对方提交/回滚释放写锁后可在同一事务直接重试成功。
                exp.overflow_flags[side][0] = False
                exp.add_base_slots[side][0] = None
                conn.execute(
                    "UPDATE kv SET val = checked_add(val, ?) WHERE id = 1",
                    (value,),
                )
                # checked_add 已在 UPDATE 语句内捕获本次加法基于的行值
                # （事务自身视图），记入本事务待提交的增量列表，供提交
                # 时间线使用；不执行任何额外读取，不影响快照与加锁顺序。
                base = exp.add_base_slots[side][0]
                if base is not None:
                    panel.tx_adds.append((base, value))
                panel.tx_has_snapshot = True
                panel.add_log("加上 %s → 成功（尚未提交）" % value)
            elif op == "commit":
                if not conn.in_transaction:
                    return reject("当前没有开启的事务")
                if panel.tx_adds:
                    # 本事务有成功加法：值与记录在同一事务提交——先在本连接
                    # INSERT 时间线记录（SAVEPOINT 保护），再 COMMIT。进程在
                    # 任意时刻终止，恢复后只能是「值与记录都生效」（COMMIT
                    # 落盘）或「都未生效」（事务回滚），绝不出现半生效状态。
                    # COMMIT 失败时异常直接抛出：事务仍开启则记录 INSERT 随
                    # SAVEPOINT 回滚撤销（kv 修改保留），tx_adds 保留，之后
                    # 重试会重新 INSERT 再 COMMIT，成功只记录一次。只读开关
                    # 开启时 INSERT 会被 SQLITE_READONLY 拒绝：提交期间临时
                    # 在本连接放开 query_only，无论成败都在 finally 恢复——
                    # 开关语义不变（提交不重置开关）。
                    with exp.timeline_lock:
                        record = exp.build_timeline_record(side, panel.tx_adds)
                        if panel.readonly:
                            conn.execute("PRAGMA query_only=0")
                        try:
                            conn.execute("SAVEPOINT timeline_record")
                            try:
                                exp.insert_timeline_record(conn, record)
                                conn.execute("COMMIT")
                            except sqlite3.Error:
                                if conn.in_transaction:
                                    # 事务仍开启：只撤销本次 INSERT，保留
                                    # 事务与此前的 kv 修改，重试时重新写入
                                    conn.execute(
                                        "ROLLBACK TO timeline_record"
                                    )
                                    conn.execute("RELEASE timeline_record")
                                raise
                            # COMMIT 已落盘：值与记录都生效，内存追加同一记录
                            exp.timeline.append(record)
                        finally:
                            if panel.readonly:
                                conn.execute("PRAGMA query_only=1")
                    panel.tx_adds = []
                else:
                    # 纯读事务（或只有失败加法）：照常提交，不产生记录
                    conn.execute("COMMIT")
                panel.tx_has_snapshot = False
                panel.add_log("COMMIT → 成功，事务已提交")
            elif op == "rollback":
                if not conn.in_transaction:
                    return reject("当前没有开启的事务")
                conn.execute("ROLLBACK")
                # 回滚不产生时间线记录，丢弃本事务待记录的增量
                panel.tx_adds = []
                panel.tx_has_snapshot = False
                panel.add_log("ROLLBACK → 成功，事务已回滚")
            else:
                return reject("未知操作：%s" % op)
        except sqlite3.Error as exc:
            # 个别错误会让 SQLite 隐式回滚整个事务：若事务已不存在，
            # 丢弃待记录的增量，已被撤销的写入绝不进入时间线
            if not conn.in_transaction:
                panel.tx_adds = []
            # checked_add 置位溢出标志时，这不是锁冲突而是越界拒绝：
            # 语句已终止、行未修改、事务保持开启，沿用既有拒绝响应。
            if exp.overflow_flags[side][0]:
                exp.overflow_flags[side][0] = False
                return reject(overflow_message)
            # 写锁冲突等 SQLite 错误：如实上报错误名与错误码，
            # 不做任何自动回滚，失败的事务保持开启。
            name = getattr(exc, "sqlite_errorname", None) or "SQLITE_ERROR"
            code = getattr(exc, "sqlite_errorcode", None)
            panel.add_log("%s → 失败 [%s (%s)] %s" % (label, name, code, exc))
            return {
                "ok": False,
                "error": {"name": name, "code": code, "message": str(exc)},
                "state": exp.panel_state(side),
            }
    return {"ok": True, "error": None, "state": exp.panel_state(side)}


def run_set_readonly(exp, side, enabled):
    """在该栏**原长期连接**上真实切换 PRAGMA query_only。

    不更换连接、不 COMMIT / ROLLBACK 当前事务，也不执行任何读取
    （不会为 DEFERRED 事务建立读快照）；开关状态只保存在 PanelState。
    开启后 SQLite 对写语句返回 SQLITE_READONLY(8)，此前写入与未提交
    事务都原样保留；COMMIT / ROLLBACK 不重置开关。
    """
    panel = exp.panels[side]
    conn = exp.conns[side]
    label = "只读模式：开启" if enabled else "只读模式：关闭"
    with exp.conn_locks[side]:
        try:
            conn.execute("PRAGMA query_only=%d" % (1 if enabled else 0))
        except sqlite3.Error as exc:
            name = getattr(exc, "sqlite_errorname", None) or "SQLITE_ERROR"
            code = getattr(exc, "sqlite_errorcode", None)
            panel.add_log("%s → 失败 [%s (%s)] %s" % (label, name, code, exc))
            return {
                "ok": False,
                "error": {"name": name, "code": code, "message": str(exc)},
                "state": exp.panel_state(side),
            }
        panel.readonly = enabled
        panel.add_log(
            "%s → 成功（原连接、当前事务不变；提交 / 回滚不重置本开关）" % label
        )
    return {"ok": True, "error": None, "state": exp.panel_state(side)}


def run_checkpoint(exp, mode):
    """用全新的独立连接真实执行 PRAGMA wal_checkpoint(<mode>)。

    busy_timeout=0：读事务阻止回收时立即返回 busy=1，绝不等待。
    检查点不替代两栏提交 / 回滚，也不触碰甲、乙的长期连接。
    """
    mode = (mode or "").upper()
    if mode not in VALID_CHECKPOINT_MODES:
        return {
            "ok": False,
            "error": {"name": None, "code": None, "message": "unknown checkpoint mode"},
            "state": exp.checkpoint_state(),
        }
    with exp.checkpoint_lock:
        # 每次一条独立连接：不用甲、乙或 observer，避免把检查点和
        # 两栏的事务状态、读快照绑定在一起。该连接不是最后一个连接，
        # 关闭时不会触发默认检查点；busy_timeout=0 使 busy 立即返回。
        ckpt_conn = sqlite3.connect(exp.db_path, timeout=0, isolation_level=None)
        try:
            ckpt_conn.execute("PRAGMA busy_timeout=0")
            try:
                row = ckpt_conn.execute(
                    "PRAGMA wal_checkpoint(%s)" % mode
                ).fetchone()
            except sqlite3.Error as exc:
                name = getattr(exc, "sqlite_errorname", None) or "SQLITE_ERROR"
                code = getattr(exc, "sqlite_errorcode", None)
                result = {
                    "mode": mode,
                    "busy": None,
                    "log_frames": None,
                    "checkpointed": None,
                    "wal_size": exp.wal_size(),
                }
                exp.checkpoint_last = result
                exp.add_checkpoint_log(
                    "%s → 失败 [%s (%s)] %s" % (mode, name, code, exc)
                )
                return {
                    "ok": False,
                    "error": {"name": name, "code": code, "message": str(exc)},
                    "state": exp.checkpoint_state(),
                }
        finally:
            ckpt_conn.close()
        # SQLite 原样返回三项：busy、WAL 总帧数 log、已回写帧数 checkpointed
        busy, log_frames, checkpointed = row[0], row[1], row[2]
        result = {
            "mode": mode,
            "busy": busy,
            "log_frames": log_frames,
            "checkpointed": checkpointed,
            "wal_size": exp.wal_size(),
        }
        exp.checkpoint_last = result
        if busy:
            tail = "检查点被并发读事务阻止（busy=1）"
        elif checkpointed < log_frames:
            tail = "回写未完成（%d/%d 帧）" % (checkpointed, log_frames)
        else:
            tail = "回写完成（%d/%d 帧）" % (checkpointed, log_frames)
        exp.add_checkpoint_log(
            "%s → busy=%d，WAL 总帧数=%d，已回写帧数=%d，WAL=%d 字节，%s"
            % (mode, busy, log_frames, checkpointed, result["wal_size"], tail)
        )
    return {"ok": True, "error": None, "state": exp.checkpoint_state(), **result}


def run_compensate(exp, seq):
    """补偿时间线末条普通提交，把已提交值恢复为该记录的 before。

    仅当末条记录是普通提交且 seq 与其一致时才执行；过期目标、补偿
    （system）记录与有效脚本一律由调用方返回 409，补偿记录不可再补偿。
    用一条全新的独立连接显式事务真实提交（UPDATE 与记录 INSERT 在同一
    事务，busy_timeout=0）：两栏任一事务持有写锁时立即返回 SQLITE_BUSY
    的错误名 / 错误码，失败时回滚本事务，不改库也不改时间线；进程任意
    时刻终止，恢复后只能是值与记录都生效或都未生效。目标核对、事务提交
    与记录追加都在 timeline_lock 内完成，与普通提交的「COMMIT → 追加」
    互斥：交错时重新核对目标，同一 seq 并发最多成功一次。全程不触碰两栏
    长期连接与 observer：两栏事务、只读模式、最近显式读值与读快照都不受
    影响（已有读快照仍见旧值），也不在两栏连接上额外读值。

    返回 (result, conflict)：conflict 非 None 时调用方返回 409。
    """
    # 锁序与 advance_script 一致（script_lock → timeline_lock），绝不反向：
    # advance_script 持 script_lock 经 run_op 的 COMMIT 进入 timeline_lock。
    with exp.script_lock:
        if exp._script_active_locked():
            return None, {
                "error": "an active script exists; "
                "finish or cancel it before compensating"
            }
        with exp.timeline_lock:
            target = exp.timeline[-1] if exp.timeline else None
            if target is None or target["seq"] != seq:
                return None, {
                    "error": "stale target: only the last normal timeline "
                    "record can be compensated"
                }
            if target["side"] == "system":
                return None, {
                    "error": "compensation records cannot be compensated"
                }
            # 补偿记录：before/after 对调（末条记录的 after 即当前已提交
            # 值——值的一切变化都有记录，且本临界区与普通提交互斥），
            # deltas 逆序取负，compensates 记原 seq；原记录保留，
            # 普通记录字段不变。
            record = {
                "seq": len(exp.timeline) + 1,
                "side": "system",
                "before": target["after"],
                "after": target["before"],
                "deltas": [-delta for delta in reversed(target["deltas"])],
                "compensates": target["seq"],
            }
            # 独立连接显式事务真实提交：不用两栏长期连接与 observer，避免
            # 把补偿与两栏事务、读快照绑定；busy_timeout=0 使写锁冲突立即
            # 报错。UPDATE 与记录 INSERT 同一事务 COMMIT：进程任意时刻
            # 终止，恢复后只能是二者都生效或都未生效。
            comp_conn = sqlite3.connect(
                exp.db_path, timeout=0, isolation_level=None
            )
            try:
                comp_conn.execute("PRAGMA busy_timeout=0")
                try:
                    comp_conn.execute("BEGIN")
                    comp_conn.execute(
                        "UPDATE kv SET val = ? WHERE id = 1", (target["before"],)
                    )
                    exp.insert_timeline_record(comp_conn, record)
                    comp_conn.execute("COMMIT")
                except sqlite3.Error:
                    # 失败：回滚本事务（若仍开启），库与时间线都保持原样
                    if comp_conn.in_transaction:
                        comp_conn.execute("ROLLBACK")
                    raise
            except sqlite3.Error as exc:
                # 写锁冲突等：如实返回 SQLite 错误名 / 错误码；
                # 语句未生效，库与时间线都保持原样
                name = getattr(exc, "sqlite_errorname", None) or "SQLITE_ERROR"
                code = getattr(exc, "sqlite_errorcode", None)
                return {
                    "ok": False,
                    "error": {"name": name, "code": code, "message": str(exc)},
                    "timeline": exp._timeline_view_locked(),
                }, None
            finally:
                comp_conn.close()
            # 成功：按提交顺序追加补偿记录（与库中同事务生效的记录一致）
            exp.timeline.append(record)
            return {
                "ok": True,
                "error": None,
                "record": timeline_public_view(record),
                "timeline": exp._timeline_view_locked(),
            }, None


# 实验注册表：内存索引，启动时由 restore_experiments() 按数据目录中
# 留存的 exp_*.db 重建（重启 / 强制终止后按原 ID 恢复访问）。
# 删除后的对象不从这里摘除，而是以墓碑（deleted=True）保留：
# 任何准入请求都返回 410，并供并发 DELETE 等待者等待收尾结束。
experiments = {}
experiments_lock = threading.Lock()
# 本次运行中已经删除的实验 ID 集合（与注册表墓碑保持一致）；
# 重启后墓碑不保留，但已删除实验的库文件已移除，不会复活（404）
deleted_experiments = set()


def admit_experiment(exp_id):
    """解析请求目标：

    返回 (exp, response)。存活并准入成功时 response 为 None；
    本次运行中删除过的 ID 返回 410；未知 ID 返回 404。
    准入成功的请求必须在 finally 中调用 exp.finish_call()。
    """
    with experiments_lock:
        exp = experiments.get(exp_id)
        was_deleted = exp_id in deleted_experiments
    if exp is not None:
        # active_calls 在独立的实验门控上自增，不持有 experiments_lock
        if exp.admit_call():
            return exp, None
        return None, (jsonify({"error": "experiment gone"}), 410)
    if was_deleted:
        return None, (jsonify({"error": "experiment gone"}), 410)
    return None, (jsonify({"error": "experiment not found"}), 404)


def restore_experiments():
    """按数据目录重建注册表：每个留存的 exp_*.db 恢复为一个实验。

    已提交值就在 kv 表中；提交时间线从 timeline 表完整加载（含补偿
    关联与序号），编号接续；两栏重建为无事务、只读关闭、最近读值为空；
    脚本、操作日志与检查点缓存不恢复（全新内存状态）。已删除实验的
    库文件已移除，不会复活。损坏的库文件跳过，不影响其他实验恢复。
    已在注册表中的 ID 跳过（幂等，可重复调用）。
    """
    for name in sorted(os.listdir(DATA_DIR)):
        match = EXP_DB_PATTERN.fullmatch(name)
        if not match:
            continue
        exp_id = match.group(1)
        with experiments_lock:
            if exp_id in experiments:
                continue
        try:
            exp = Experiment(exp_id)
        except (OSError, sqlite3.Error):
            continue
        with experiments_lock:
            experiments.setdefault(exp_id, exp)
            deleted_experiments.discard(exp_id)


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/experiments")
def create_experiment():
    exp_id = uuid.uuid4().hex[:12]
    exp = Experiment(exp_id)
    with experiments_lock:
        experiments[exp_id] = exp
        # 全新 ID，不会与已删除集合冲突；保险起见若被复用则移除旧墓碑
        deleted_experiments.discard(exp_id)
    return jsonify({"id": exp_id})


@app.delete("/api/experiments/<exp_id>")
def delete_experiment(exp_id):
    # 争取删除执行权与立墓碑在同一段注册表临界区内原子完成：
    # 并发请求不可能落入「未标记」窗口而拿到 404。
    # 删除后的实验对象保留在注册表中（墓碑），等待者可等待其收尾。
    with experiments_lock:
        exp = experiments.get(exp_id)
        if exp is None:
            if exp_id in deleted_experiments:
                # 本次运行中已删除的 ID：再次删除仍返回 410
                return jsonify({"error": "experiment gone"}), 410
            return jsonify({"error": "experiment not found"}), 404
        deleted_experiments.add(exp_id)

    # 原子地争取删除权，或等待另一请求的删除收尾结束（随后返回 410）。
    # 检查与等待在同一门控临界区内完成，不会漏掉获胜者的 notify。
    if not exp.claim_or_wait_delete():
        return jsonify({"error": "experiment gone"}), 410

    # 等待此前已进入的请求全部正常完成；
    # 删除开始后该实验的操作、状态、已提交值及检查点新请求均在准入处返回 410。
    exp.begin_delete()
    # 回滚未提交事务、关闭长期及临时连接、移除数据库与 WAL/SHM 文件。
    # 删除等待与拆除都按实验独立，不会阻塞其他实验。
    exp.teardown()
    # 注意：不从注册表摘除——删除后的对象以墓碑形式保留（准入一律 410），
    # 同时保证并发 DELETE 等待者持有的对象引用始终有效。
    return jsonify({"id": exp_id, "deleted": True})


@app.get("/api/experiments/<exp_id>/status")
def experiment_status(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        # 两栏状态、检查点状态与提交时间线均来自内存缓存，
        # 不执行任何 SQL、不触发检查点、不在两栏连接上读值
        return jsonify(
            {
                "id": exp.id,
                "a": exp.panel_state("a"),
                "b": exp.panel_state("b"),
                "checkpoint": exp.checkpoint_state(),
                "timeline": exp.timeline_records(),
                "script": exp.script_state(),
            }
        )
    finally:
        exp.finish_call()


@app.get("/api/experiments/<exp_id>/timeline")
def experiment_timeline(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        # 只读内存中的提交时间线：不执行任何 SQL、不触碰两栏连接
        return jsonify({"timeline": exp.timeline_records()})
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/timeline/compensate")
def experiment_timeline_compensate(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        data = request.get_json(silent=True)
        # seq 必须是正整数（布尔不算整数；非 JSON body 同样拒绝）
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("seq"), int)
            or isinstance(data.get("seq"), bool)
            or data["seq"] < 1
        ):
            return jsonify({"error": "seq must be a positive integer"}), 400
        # 过期目标 / 补偿记录 / 有效脚本：409；写锁冲突等 SQLite 错误：
        # 200 + ok:false 并如实携带错误名 / 错误码，库与时间线不变
        result, conflict = run_compensate(exp, data["seq"])
        if conflict is not None:
            return jsonify(conflict), 409
        return jsonify(result)
    finally:
        exp.finish_call()


def parse_script_action(data):
    """解析推进 / 跳过 / 取消请求体：必须且只能给出正确类型的
    script_id（字符串）与 step（整数，布尔不算整数）。"""
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("script_id"), str)
        or not isinstance(data.get("step"), int)
        or isinstance(data.get("step"), bool)
    ):
        return None, (jsonify({"error": "script_id (string) and step (integer) required"}), 400)
    return (data["script_id"], data["step"]), None


@app.get("/api/experiments/<exp_id>/script")
def experiment_script_get(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        # 只读内存中的脚本状态与执行记录：不执行任何 SQL、不额外读两栏值
        return jsonify({"script": exp.script_state()})
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/script")
def experiment_script_load(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        # 整份校验，非法整份拒绝（400），任何已有脚本都不受影响
        data = request.get_json(silent=True)
        invalid = Experiment.validate_steps(data if data is not None else None)
        if invalid is not None:
            return jsonify({"error": invalid}), 400
        view, rejected = exp.load_script(data)
        if rejected is not None:
            return jsonify(rejected), 409
        return jsonify({"ok": True, "script": view})
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/script/advance")
def experiment_script_advance(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        parsed, bad = parse_script_action(request.get_json(silent=True))
        if bad is not None:
            return bad
        script_id, step_no = parsed
        # 同实验串行核对并执行；过期请求（ID / 步号不符、已结束）409，
        # 不执行、不记录、不影响新脚本。
        view, ok, rejected = exp.advance_script(script_id, step_no)
        if rejected is not None:
            return jsonify(rejected), 409
        return jsonify({"ok": ok, "script": view})
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/script/skip")
def experiment_script_skip(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        parsed, bad = parse_script_action(request.get_json(silent=True))
        if bad is not None:
            return bad
        script_id, step_no = parsed
        view, ok, rejected = exp.skip_script(script_id, step_no)
        if rejected is not None:
            return jsonify(rejected), 409
        return jsonify({"ok": ok, "script": view})
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/script/cancel")
def experiment_script_cancel(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        parsed, bad = parse_script_action(request.get_json(silent=True))
        if bad is not None:
            return bad
        script_id, step_no = parsed
        view, ok, rejected = exp.cancel_script(script_id, step_no)
        if rejected is not None:
            return jsonify(rejected), 409
        return jsonify({"ok": ok, "script": view})
    finally:
        exp.finish_call()


@app.get("/api/experiments/<exp_id>/committed")
def experiment_committed(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        return jsonify({"committed": json_int(exp.committed_value())})
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/query")
def experiment_query_submit(exp_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        data = request.get_json(silent=True)
        # body 必须是含字符串 sql 的 JSON 对象；按字符数限 1～4000（非空白）
        if not isinstance(data, dict) or not isinstance(data.get("sql"), str):
            return jsonify({"error": "sql must be a string"}), 400
        sql = data["sql"]
        if not sql.strip():
            return jsonify({"error": "sql must be 1..%d characters" % MAX_SQL_LEN}), 400
        if len(sql) > MAX_SQL_LEN:
            return (
                jsonify({"error": "sql must be at most %d characters" % MAX_SQL_LEN}),
                400,
            )
        # 词法层：必须单独一条 SELECT / WITH 读查询（写入 / PRAGMA /
        # ATTACH / 多条语句等在此 400，绝不入队）
        invalid = scan_single_read_sql(sql)
        if invalid is not None:
            return jsonify({"error": invalid}), 400
        # 策略闸：只读连接 EXPLAIN 预编译，authorizer 拒绝写入 / PRAGMA /
        # ATTACH / 扩展加载，数据修改型 CTE 在此以 SQLITE_AUTH 失败
        rejected = prepare_query_gate(exp.db_path, sql)
        if rejected is not None:
            return jsonify({"error": rejected}), 400
        # running 中再提交：409；上一条已终结则替换，旧 ID 随即 404
        view, conflict = exp.submit_query(sql)
        if conflict is not None:
            return jsonify(conflict), 409
        return jsonify({"query": view})
    finally:
        exp.finish_call()


@app.get("/api/experiments/<exp_id>/query/<query_id>")
def experiment_query_get(exp_id, query_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        view = exp.get_query(query_id)
        if view is None:
            # 旧查询 ID（已被新查询替换 / 重启后内存清空 / 从未存在）：404
            return jsonify({"error": "query not found"}), 404
        return jsonify({"query": view})
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/query/<query_id>/cancel")
def experiment_query_cancel(exp_id, query_id):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        outcome, view = exp.cancel_query(query_id)
        if outcome == "missing":
            return jsonify({"error": "query not found"}), 404
        # 已终结：返回原终态（200），不改变成功 / 失败 / 取消结果；
        # running：已中断真实 SQL 并等待连接关闭、终态发布后返回
        return jsonify({"query": view})
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/checkpoint")
@app.post("/api/experiments/<exp_id>/checkpoint/<mode>")
def experiment_checkpoint(exp_id, mode=None):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        if mode is None:
            data = request.get_json(silent=True) or {}
            mode = data.get("mode")
        mode = (mode or "").upper()
        if mode not in VALID_CHECKPOINT_MODES:
            return jsonify({"error": "mode must be PASSIVE or TRUNCATE"}), 400
        return jsonify(run_checkpoint(exp, mode))
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/conn/<side>/op")
def experiment_op(exp_id, side):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        if side not in ("a", "b"):
            return jsonify({"error": "unknown side"}), 400
        data = request.get_json(silent=True) or {}
        op = data.get("op")
        if op not in VALID_OPS:
            return jsonify({"error": "unknown op"}), 400
        # 脚本有效期间拒绝手工事务操作（查询 / 检查点 / 删除不受影响）。
        # active 复查与在途计数在同一段 script_lock 临界区内完成，计数后
        # 即释放锁再执行：两栏手工请求互不等待（同栏请求仍由本栏
        # conn_lock 串行），甲的在途读值不再阻塞乙的 BEGIN / 只读切换。
        # 与装载的互斥不变：装载会在同一把锁内等待在途计数归零后才复查
        # 两栏 in_transaction，因此不会留下「复查通过后、执行前被装载
        # 抢先」的 TOCTOU 窗口，也不会出现「装载成功后手工 BEGIN 才开启
        # 事务」。锁内读标志只能用 _script_active_locked()：script_lock
        # 不可重入，重复获取会让请求永久卡住并占住准入名额。
        value = None
        if op == "add":
            value = parse_int(data.get("value"))
            if value is None:
                return jsonify({"error": "value must be an integer"}), 400
            if abs(value) > MAX_ADD_ABS:
                return jsonify({"error": "value too large"}), 400
        with exp.script_lock:
            if exp._script_active_locked():
                return (
                    jsonify({"error": "manual operations are locked while a script is active"}),
                    409,
                )
            exp.manual_active[side] += 1
        try:
            return jsonify(run_op(exp, side, op, value))
        finally:
            # 执行结束（含失败 / 异常）归还本栏在途计数并唤醒等待中的
            # 装载；准入名额由外层 finally 的 finish_call 归还。
            with exp.script_lock:
                exp.manual_active[side] -= 1
                exp.manual_cond.notify_all()
    finally:
        exp.finish_call()


@app.post("/api/experiments/<exp_id>/conn/<side>/readonly")
def experiment_set_readonly(exp_id, side):
    exp, response = admit_experiment(exp_id)
    if exp is None:
        return response
    try:
        if side not in ("a", "b"):
            return jsonify({"error": "unknown side"}), 400
        data = request.get_json(silent=True)
        # 严格只接受 JSON 布尔：1/0、"true"、None 等一律拒绝，
        # get_json 解析失败（body 不是 JSON）时 data 为 None 同样拒绝。
        if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
            return jsonify({"error": "enabled must be a boolean"}), 400
        # 脚本有效期间只读切换同样锁定（脚本必须沿两栏既定连接语义执行）。
        # 与手工操作一样只在 script_lock 内做「复查 + 在途计数」，随即释放
        # 锁在本栏原连接上执行：两栏互不等待；装载等待在途计数归零后才
        # 复查两栏事务，避免「检查后、执行前恰好装载」。锁内同样只能用
        # 不重复取锁的 _script_active_locked() 读标志。
        with exp.script_lock:
            if exp._script_active_locked():
                return (
                    jsonify({"error": "readonly switch is locked while a script is active"}),
                    409,
                )
            exp.manual_active[side] += 1
        try:
            return jsonify(run_set_readonly(exp, side, data["enabled"]))
        finally:
            # 执行结束（含失败 / 异常）归还本栏在途计数并唤醒等待中的
            # 装载；准入名额由外层 finally 的 finish_call 归还。
            with exp.script_lock:
                exp.manual_active[side] -= 1
                exp.manual_cond.notify_all()
    finally:
        exp.finish_call()


def prepare_data_dir():
    # 实验跨重启保留：只确保数据目录存在，绝不清空；
    # 留存的 exp_*.db 由 restore_experiments() 重新加载
    os.makedirs(DATA_DIR, exist_ok=True)


prepare_data_dir()
restore_experiments()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
