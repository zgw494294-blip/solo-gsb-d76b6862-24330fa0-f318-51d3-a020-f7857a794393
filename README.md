# SQLite 事务隔离实验室

一个用于观察 SQLite（WAL 模式）事务隔离与写锁冲突行为的网页实验台。

- 后端：Python + Flask（直接调用标准库 `sqlite3`，真实执行 SQL）
- 前端：原生 HTML / CSS / JavaScript，无任何框架
- 每次「新建实验」都会得到一个独立的 WAL 数据库，内含单行整数值 `100`
- 页面左右两栏（甲 / 乙）各自绑定一条**长期独立连接**，可执行
  `BEGIN DEFERRED`、读值、加上输入整数、提交、回滚
- 第三条连接单独显示**已提交值**
- 状态刷新不执行任何 `SELECT`，不会为 DEFERRED 事务提前建立读快照
- 写锁冲突立即返回 SQLite 错误名与错误码（`busy_timeout=0`），
  不阻塞另一栏，也不会自动结束失败的事务
- 写锁冲突（`SQLITE_BUSY`）发生时事务尚未建立读快照：对方 `COMMIT` 或
  `ROLLBACK` 释放写锁后，**可在原事务直接重试写入**，无需回滚重开；
  一旦本事务显式读值（读快照建立），再与对方新提交冲突则报
  `SQLITE_BUSY_SNAPSHOT`，必须自行回滚重开（系统不代为回滚）
- 「加上」的内部边界检查不会在本连接提前建立读快照：无快照时基于
  observer 连接的已提交值预判，最终判定在 `UPDATE` 内的 `checked_add`
  函数中完成，因此不会把 `SQLITE_BUSY` 误报成 `SQLITE_BUSY_SNAPSHOT`
- 修改操作（加上）只允许在事务内进行，禁止事务外修改
- 每栏一个**独立只读开关**（默认关闭，也可 HTTP 调用）：开启即在**该栏原长期
  连接**上真实执行 `PRAGMA query_only=1`，不换连接、不结束当前事务，也不提前读值
  建立读快照；关闭执行 `PRAGMA query_only=0`。开启后仍可读值、`BEGIN`、`提交`、
  `回滚`（提交仍提交此前写入，回滚仍撤销），加法按钮保持可用，但事务内未越界的
  加法由 SQLite 如实返回 **SQLITE_READONLY（错误码 8）**，语句失败不撤销此前
  修改；关闭后可在**原事务**继续加法。`COMMIT` / `ROLLBACK` 不重置开关，两栏
  开关互相独立，另一栏的写锁冲突仍报 `SQLITE_BUSY（5）`；状态接口与页面均显示
  当前开关模式，开关请求只接受 JSON 布尔值（非布尔返回 `400`）。**开关请求失败
  回退规则**：请求期间复选框与文案先乐观保持一致（本栏禁用、另一栏照常）；请求
  失败（网络错误 / 非 2xx）时错误显示在**本栏**，勾选与文案一起恢复到**最近一次
  服务端确认的模式**——在途轮询若已先确认新模式则恢复为新模式，期间没有新确认
  则恢复点击前的旧模式；随后即使轮询返回完全相同的状态（只读字段、日志、读值均
  无变化），本地勾选 / 文案偏差也会被立即纠正，无须等待日志或读值变化
- 手动 **PASSIVE / TRUNCATE 检查点**按钮（也可 HTTP 调用）：每次用一条全新的
  独立连接真实执行 `PRAGMA wal_checkpoint`，`busy_timeout=0`，读事务阻止回收时
  立即返回 `busy=1`；各写连接已关闭自动检查点（`PRAGMA wal_autocheckpoint=0`）
- **只读 SQL 工作台**（页面区块与三个 HTTP 接口）：提交一条 1～4000 字符的
  **单独一条 SELECT / WITH 读查询**，立即返回查询 ID，可查询 **运行中 /
  成功 / 失败 / 取消**；写入、`PRAGMA`、`ATTACH`、扩展加载与多条语句一律
  `400`。查询在**全新的独立只读连接**（URI `mode=ro` + `query_only=1` +
  authorizer 白名单）上执行，**不碰两栏事务、读快照、最近读值与时间线**；
  返回列名、**前 100 行**与截断标志，整数无损、BLOB 转 `X'…'` 十六进制。
  每实验只保留最新查询：运行中再提交 `409`，新查询替换上一条后旧 ID 返回
  `404`；取消会**中断真实执行的 SQL**（尚未开跑也能取消），连接关闭后才
  公布终态，取消先确认绝不成功，已终结则不改结果。删除先取消在途查询、等
  连接关闭再删文件，不阻塞其他实验；失败 / 取消不改值及时间线；查询不跨
  重启保留。详见下文「只读 SQL 工作台」
- 检查点结果显示模式与 SQLite 原始三项（**busy、WAL 总帧数、已回写帧数**）及
  操作后 WAL 文件字节数（文件不存在记 0）；状态刷新只读内存缓存，不触发检查点
- 检查点不代替两栏提交 / 回滚，也不替换任何连接
- **提交时间线**（页面底部区块、`GET /api/experiments/<id>/timeline`，同时随
  `/status` 的 `timeline` 字段返回）：按事务汇总，按实际提交顺序从 1 编号。
  仅「至少一次加法成功且 COMMIT 成功」的事务产生一条记录，含栏位、首次成功
  加法前的值、提交后的值与按执行顺序排列的成功增量（沿用整数无损约定，大整数
  用十进制字符串）。失败加法不计入；回滚、失败提交与纯读事务不记录；未提交
  内容不可见；提交重试成功只记一次。首次成功加法前的值由 `UPDATE` 内的
  `checked_add` 在语句执行时捕获（事务自身视图），提交后的值 = 该值 + 全部
  成功增量之和，绝不事后读取别栏新值作为本次结果；采集、查询与轮询都不在
  两栏连接上额外读值、不提前建立快照，也不改变最近一次显式读值；`COMMIT`
  与记录追加在同一临界区完成，编号顺序与实际提交顺序严格一致；**记录与值
  在同一事务写入实验库的 `timeline` 表**，服务重启 / 进程被强制终止后按原
  实验 ID 完整恢复（含补偿关联与序号），脚本、操作日志与检查点缓存不恢复
- **补偿末条提交**（时间线区块输入序号，或
  `POST /api/experiments/<id>/timeline/compensate`，body 为正整数
  `{"seq": N}`）：已提交的加法不能用 `ROLLBACK` 撤回，改为对时间线
  **末条普通提交**执行补偿——后端用一条**全新的独立连接**显式事务真实提交
  （`UPDATE` 与记录 `INSERT` 在同一事务），把值
  恢复为该记录的 `before`，并按提交顺序追加一条 `side=system` 的补偿记录
  （`before`/`after` 对调、增量**逆序取负**、`compensates` 记原 seq；
  原记录保留，普通记录字段不变）。仅末条普通提交可补偿：**过期序号、
  补偿记录或脚本有效期间返回 `409`**，补偿记录不可再补偿；`busy_timeout=0`，
  两栏持写锁时立即返回 `SQLITE_BUSY` 错误名 / 错误码，**失败时回滚本事务，
  不改库也不改时间线**；目标核对与记录追加和普通提交的「COMMIT → 追加」
  在同一临界区互斥，交错时重新核对目标，**同一 seq 并发最多成功一次**。
  补偿不触碰两栏长期连接与 observer：两栏事务、只读模式、最近显式读值与
  读快照都不受影响（已有读快照仍见旧值），也不在两栏连接上额外读值；
  大整数沿用无损编码
- 页面提供**删除当前实验**按钮（也可 HTTP 调用）：确认后删除。删除开始即等待
  此前已进入的请求正常完成，期间该实验的操作、状态、已提交值与检查点**新请求
  一律返回 410 Gone**；等待按实验独立，不阻塞其他实验。随后回滚两栏未提交事务、
  关闭甲、乙长期连接与 observer 及一切临时检查点连接，再移除数据库与 `-wal`、
  `-shm` 文件，全部完成才报告删除成功，因此在途检查点不可能在删除后重新创建文件
- 本次运行中，已删除 ID 再次删除（或调用任何该实验接口）仍返回 **410**；
  未知 ID 返回 **404**。重启后内存墓碑不保留，但**成功删除的实验不会复活**
  （库文件已移除），按未知 ID 返回 **404**。页面收到删除成功或 410 后停止
  轮询、禁用旧实验的全部操作，
  迟到响应不会恢复旧展示也不会自动新建实验；「新建实验」始终可手动使用
- 手动「新建实验」进行期间：**立即暂停旧实验的两组轮询并保持旧实验全部操作禁用**
  （`creating` 同时拦截已入队、尚未执行的迟到轮询回调），创建成功前不会再有任何
  请求发往旧 ID；创建成功后只为**新 ID** 启用控件并启动轮询。旧实验的轮询、
  操作、检查点、删除请求，无论返回 2xx / 4xx / 5xx 还是在切换之后才收尾，
  都不会改动新实验的读值、已提交值、日志、错误框、按钮状态或冻结状态，也不会
  触发自动新建
- 已删除（冻结）页面上发起手动新建若失败（网络错误或非 2xx 响应）：顶部显示
  「新建实验失败」错误，旧实验操作**仍全部禁用、轮询保持停止**；手动重试成功后
  才解除冻结、为新 ID 启用控件并恢复轮询。存活页面上新建失败则恢复旧实验的
  控件与轮询，并立即重新拉取旧实验状态
- **实验跨重启保留**：已提交值与提交时间线同库存放在 `LAB_DATA_DIR`
  （Compose 以命名卷 `lab-data` 挂载 `/app/data`），**启动不再清空目录**，
  服务重启或进程被强制终止（`kill -9`）后按**原实验 ID** 即可继续访问：
  完整时间线（含补偿关联与序号）恢复、编号接续、末条普通记录仍可补偿；
  两栏重建为**无事务、只读关闭、最近读值为空**，脚本、操作日志与检查点
  缓存不恢复。普通提交与补偿都把「值 + 时间线记录」放在**同一事务**提交，
  进程任意时刻终止后只能恢复二者都生效或都未生效的状态，不依赖退出清理；
  旧版库（无 `timeline` 表）首次加载保留现值，以空时间线为基线。详见下文
  「跨重启与强制终止恢复」

## 脚本逐步执行

两栏实验可以按**录入的脚本**逐步执行，便于把固定的交错场景（如写锁冲突 → 跳过 → 对方提交 → 原事务重试）脚本化、可复现。

- 页面「脚本逐步执行」区块或 HTTP 可装载 **1～50 步的 JSON 数组**：
  每步是一个对象，含 `side`（`"a"` / `"b"`）与 `op`
  （`begin` / `read` / `add` / `commit` / `rollback`）；`add` 另含
  `value`，**沿用手工「加上」的整数校验**（JSON 整数或十进制字符串、
  单次 ±2⁶² 上限、大整数无损）。**整份校验、非法整份拒绝**（HTTP `400`），
  不会只装载其中一部分，已存在的脚本不受影响
- 仅当**两栏均无事务且没有有效（active）脚本**时才能装载：装载与手工
  `BEGIN` 在同一把实验级锁上严格互斥，不满足条件返回 `409`。每个脚本
  生成独立脚本 ID
- 查询（`GET /api/experiments/<id>/script`，也随 `/status` 的 `script`
  字段轮询返回）只读内存，返回**脚本 ID、从 1 开始的当前步号、全部步骤
  与逐次执行记录**；查询不在两栏连接上额外读值、不建立快照
- 脚本有效期间，后端**拒绝两栏全部手工事务操作与只读切换**（`409`）；
  查询、已提交值、时间线、**检查点、删除仍照常可用**
- 「推进一步」在该步指定栏的**原长期连接**上真实执行同一个操作：
  **成功才把步号前移**；失败**不代为回滚**，步号停在原处，可**重试**
  或**跳过**。每一次尝试都记录结果（`success` / `failure`）与
  **原始 SQLite 错误名 / 错误码**（如 `SQLITE_BUSY` / `5`；边界拒绝类
  错误名 / 码为 `null`）；跳过单独记 `skipped`，不执行任何 SQL
- 脚本走完全部步骤（`finished`）或被取消（`cancelled`）后**恢复手工
  操作**；系统**既不自动提交也不自动回滚**——结束时仍开启的事务原样
  保留，因此若要再装载脚本，需先手工 `提交` / `回滚`
- 推进、跳过、取消都必须携带**脚本 ID 与所见步号**
  （`{"script_id": "...", "step": N}`，类型不符 `400`）；同一实验的脚本
  动作**串行核对并执行**：脚本 ID 不符（旧脚本的过期请求，含已被新脚本
  替换）、脚本已完成 / 取消、步号与当前步不一致，一律 `409`，**不执行、
  不产生记录、不影响新脚本**
- 页面控件遵守：**新建实验进行中禁用**、**删除后冻结**、旧实验的迟到
  脚本响应不影响新实验；两栏手工控件在脚本有效期间禁用并随轮询自动
  锁定 / 恢复；原有手工接口、检查点、时间线、删除接口与整数无损约定
  全部保留不变

## 只读 SQL 工作台

页面中部的「只读 SQL 工作台」区块（与三个 HTTP 接口）可对实验库执行一条
**只读** SQL，用来在不动两栏事务的前提下自由观察 `kv` / `timeline` 等表的
已提交内容。

- **提交**：`POST /api/experiments/<id>/query`，body 为
  `{"sql": "..."}`。SQL 限 **1～4000 字符**（按字符数，含空白），必须是
  **单独一条** `SELECT` 或 `WITH` 读查询（允许一个结尾分号、前后空白与
  注释）；下列一律在提交当时返回 **400**，绝不入队：
  - 写入 / DDL / 事务语句：`INSERT` / `UPDATE` / `DELETE` / `REPLACE`、
    `CREATE` / `DROP` / `ALTER` / `VACUUM` / `REINDEX`、`BEGIN` /
    `COMMIT` / `ROLLBACK` / `SAVEPOINT` 等；
  - `WITH cte AS (...) DELETE/INSERT/UPDATE ...` 数据修改型 CTE；
  - 任何 `PRAGMA`（含 `select * from pragma_xxx()` 表值函数）、
    `ATTACH` / `DETACH`、`EXPLAIN`、`VALUES`、`load_extension(...)`；
  - 多条语句（`select 1; select 2`、空语句、分号后再写内容）、未闭合的
    引号 / 注释；
  - body 不是含字符串 `sql` 的 JSON 对象。
  合法但运行时才失败的 SQL（如查询不存在的表）**会入队**，状态为
  `failed` 并附原始 SQLite 错误名 / 错误码。
- 执行连接是**全新的独立只读连接**，三重只读限制：URI `file:…?mode=ro`
  （文件层只读）、`PRAGMA query_only=1`（连接层禁写）、
  `set_authorizer` 白名单（只放行读 / SELECT 准备 / 递归 CTE 与普通函数，
  拒绝 PRAGMA / ATTACH / 一切写入动作与 `load_extension`），
  `busy_timeout=0`。它**绝不触碰**甲、乙长期连接与 observer：两栏事务、
  只读开关、读快照、最近显式读值、时间线与检查点都不受影响；失败或取消的
  查询**不改值也不改时间线**。
- **每实验只保留最新一条查询**。提交立即返回
  `{"query": {"id": "<12 位十六进制>", "status": "running", "sql": ...}}`。
  - 已有查询处于 **running** 时再次提交 → **409**（在途查询照常运行 / 可取消）；
  - 上一条已终结（`succeeded` / `failed` / `cancelled`）时新提交直接
    **替换**它，旧查询 ID 立即返回 **404**，不影响新查询、不留墓碑。
- **查结果**：`GET /api/experiments/<id>/query/<query_id>`，返回
  `{"query": {...}}`，`status` 为 `running` / `succeeded` / `failed` /
  `cancelled`：
  - `succeeded`：`columns`（列名数组）、`rows`（**最多前 100 行**）、
    `truncated`（结果超过 100 行时为 `true`，只回前 100 行）；
    整数沿用全站无损约定（超出 ±2⁵³−1 用十进制字符串），`NULL` 为 `null`，
    REAL 为 JSON 数字，BLOB 一律编码成 `X'…'` 形式的**小写十六进制文本**
    （空 BLOB 为 `X''`）；
  - `failed`：附 `error = {name, code, message}`（原始 SQLite 错误名 /
    错误码，如 `SQLITE_ERROR(1)`）；
  - `cancelled`：`error` 为 `null`。
- **取消**：`POST /api/experiments/<id>/query/<query_id>/cancel`
  （空 JSON body `{}` 即可）。取消会**中断真实执行中的 SQL**（对工作连接
  调用 `interrupt`，无界递归 CTE 等长查询立即以 `SQLITE_INTERRUPT(9)`
  终止）；**尚未开跑**的查询也必须能取消（worker 在打开连接前、执行前都
  复查取消标志）。取消请求会等到查询线程**关闭连接之后**才返回，因此
  返回即终态、worker 绝不可能再触碰数据库文件。**取消先被确认则绝不能
  成功**：worker 在 SQL 返回后、连接关闭后才发布终态，并再查一次取消
  标志（取消与完成竞速时一律记 `cancelled`）。对**已终结**查询再次取消
  返回 **200 与原终态，不改变结果**；旧 / 未知查询 ID 的查询与取消返回
  **404**。
- 页面控件遵守统一规则：**新建实验进行中禁用**、**删除后冻结**（停止查询
  轮询、全部禁用）、旧实验 / 旧查询 ID 的迟到响应不影响新实验 / 新查询；
  运行中只能取消、不能再提交，终态后恢复提交。SQL 文本框内容在新建实验时
  保留，便于跨实验复用同一条查询。查询状态**只保存在内存**，服务重启后不
  恢复：实验本身按原 ID 恢复，但旧查询 ID 返回 **404**。

### HTTP 调用示例：提交 / 查结果 / 取消 / 删除交错

```bash
EXP=$(curl -s -X POST http://localhost:8000/api/experiments \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
BASE=http://localhost:8000/api/experiments/$EXP

# 1) 提交：立即返回查询 ID 与 running
QID=$(curl -s -X POST $BASE/query -H 'Content-Type: application/json' \
  -d '{"sql":"select seq, side, before_val, after_val from timeline order by seq"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["query"]["id"])')
echo $QID          # 12 位十六进制，如 7c31a08f9b21

# 2) 查结果：running → 终态（成功含列名 / 前 100 行 / truncated）
curl -s $BASE/query/$QID
# 新建实验 timeline 为空：
# {"query":{"id":"7c31...","status":"succeeded","sql":"select ...",
#   "columns":["seq","side","before_val","after_val"],
#   "rows":[],"truncated":false}}

# 类型编码：大整数无损字符串、NULL、REAL、BLOB 十六进制（BLOB 用 x'..' 字面量）
curl -s -X POST $BASE/query -H 'Content-Type: application/json' \
  --data-binary '{"sql":"select val, null as n, 1.5 as f, x'"'"'4142'"'"' as b from kv"}'

# 3) 非法 SQL：提交当时 400，绝不入队（不影响任何已有查询）
for sql in 'delete from kv' 'pragma query_only=0' 'attach '"'"'x'"'"' as z' \
           'select 1; select 2' 'select load_extension('"'"'x'"'"')' 'explain select 1'; do
  curl -s -o /dev/null -w "%{http_code} " -X POST $BASE/query \
    -H 'Content-Type: application/json' -d "{\"sql\":\"$sql\"}"
done
# 400 400 400 400 400 400

# 长度边界：> 4000 字符（含空白）返回 400；恰好 4000 字符接受
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/query \
  -H 'Content-Type: application/json' \
  --data-binary "$(python3 -c 'import json;print(json.dumps({"sql":"select 1" + " "*4000}))')"
# 400
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/query \
  -H 'Content-Type: application/json' \
  --data-binary "$(python3 -c 'import json;print(json.dumps({"sql":"select 1" + " "*3991}))')"
# 200（"select 1" 9 字符 + 3991 空格 = 4000）

# 4) 运行中再提交：409；在途查询不受影响
LONG='{"sql":"with recursive c(x) as (select 1 union all select x+1 from c) select count(*) from c"}'
LONG_ID=$(curl -s -X POST $BASE/query -H 'Content-Type: application/json' -d "$LONG" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["query"]["id"])')
echo $LONG_ID                  # 在途长查询的真实 ID
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/query \
  -H 'Content-Type: application/json' -d '{"sql":"select 1"}'        # 409

# 5) 取消：中断真实 SQL，等到连接关闭、终态发布后才返回
curl -s -X POST $BASE/query/$LONG_ID/cancel -H 'Content-Type: application/json' -d '{}'
# {"query":{"id":"<long_id>","status":"cancelled","sql":"...","error":null}}
# 已终结后再次取消：200 + 原终态，不改变结果
curl -s -X POST $BASE/query/$LONG_ID/cancel -H 'Content-Type: application/json' -d '{}'
# {"query":{...,"status":"cancelled",...}}
curl -s $BASE/query/$LONG_ID                                       # 仍 cancelled
curl -s -o /dev/null -w "%{http_code}\n" $BASE/query/deadbeefcafe  # 未知 ID：404

# 6) 成功 → 再提交一条：新查询替换旧查询，旧 ID 立即 404，不影响新查询
NEW_ID=$(curl -s -X POST $BASE/query -H 'Content-Type: application/json' \
  -d '{"sql":"select val from kv"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["query"]["id"])')
curl -s $BASE/query/$NEW_ID                                        # succeeded，100
curl -s -o /dev/null -w "%{http_code}\n" $BASE/query/$LONG_ID      # 404（已被替换）

# 7) 删除与在途查询交错：删除先取消在途查询、等其连接关闭，再回滚两栏
#    事务、关闭连接、删除 DB/WAL/SHM；删除开始后查询接口一律 410
curl -s -X POST $BASE/query -H 'Content-Type: application/json' -d "$LONG" >/dev/null  # running
curl -s -X DELETE $BASE                                            # 等查询收尾后 200
# {"id":"...","deleted":true}
curl -s -o /dev/null -w "%{http_code}\n" $BASE/query/$NEW_ID       # 410（实验已删除）
```

> 取消与完成几乎同时发生时（查询很短、取消恰好在结果返回前后到达）：终态
> 以**先被确认的取消**为准，worker 在连接关闭后的最终复查保证「取消先确认
> 绝不成功」；反过来，查询已终结后才到达的取消不会改写成功 / 失败结果。
> 查询不阻塞两栏与其他实验：甲实验的长查询运行期间，乙实验的两栏操作、检查
> 点、补偿、删除以及它自己的查询都照常进行。失败 / 取消的查询不产生任何写入，
> `kv` 值与 `timeline` 时间线保持原样。查询不跨重启保留：重启后实验与值、
> 时间线按原 ID 恢复，但旧查询 ID 返回 `404`，重新提交即可。

## 手工请求与脚本装载的互斥（后端卡死修复）

手工事务操作（`POST …/conn/<side>/op`）与只读切换
（`POST …/conn/<side>/readonly`）和脚本装载在同一把实验级 `script_lock`
上互斥：装载先成功则手工请求 `409`，手工 `BEGIN` 先执行则装载 `409`。
修复前，这两个路由在持有 `script_lock` 做 active 复查时**再次获取同一把
非可重入锁**，请求在锁内永久等待自己：

- 存活实验没有有效脚本时，合法 `BEGIN` / 只读切换也不返回；
- 脚本有效期间本应立即 `409` 的请求同样挂起，且不执行、不归还准入名额；
- 卡住的请求占住 `script_lock` 与准入名额，随后的脚本查询、推进与删除
  一并阻塞。

修复后：锁内改用不再重复取锁的 `_script_active_locked()` 读标志；请求
结束（含 `409`）在 `finally` 中归还准入名额，脚本查询、推进与删除照常
完成。两个接口的字段与响应约定不变。（该修复最初让路由在「复查 + 执行」
整段持有 `script_lock`，两栏手工请求因此被串行化；其后的改进见下节。）

### 复现（修复前）

```bash
EXP=$(curl -s -X POST http://localhost:8000/api/experiments \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
BASE=http://localhost:8000/api/experiments/$EXP

# 无有效脚本：合法 BEGIN 也不返回（3 秒超时被迫放弃）
curl -s -m 3 -X POST $BASE/conn/a/op \
  -H 'Content-Type: application/json' -d '{"op":"begin"}'
# curl: (28) Operation timed out after 3000 milliseconds

# 只读切换同样挂起
curl -s -m 3 -X POST $BASE/conn/a/readonly \
  -H 'Content-Type: application/json' -d '{"enabled":true}'
# curl: (28) Operation timed out after 3000 milliseconds

# 卡住的请求不归还名额：随后的脚本查询与删除也被阻塞
curl -s -m 3 $BASE/script     # 超时
curl -s -m 3 -X DELETE $BASE  # 超时
```

### 修复后调用

```bash
EXP=$(curl -s -X POST http://localhost:8000/api/experiments \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
BASE=http://localhost:8000/api/experiments/$EXP

# 无有效脚本：按原事务规则立即执行
curl -s -X POST $BASE/conn/a/op \
  -H 'Content-Type: application/json' -d '{"op":"begin"}'
# {"ok":true,"error":null,"state":{"in_transaction":true,"readonly":false,...}}
curl -s -X POST $BASE/conn/a/readonly \
  -H 'Content-Type: application/json' -d '{"enabled":true}'
# {"ok":true,"error":null,"state":{"in_transaction":true,"readonly":true,...}}
curl -s -X POST $BASE/conn/a/readonly \
  -H 'Content-Type: application/json' -d '{"enabled":false}'
curl -s -X POST $BASE/conn/a/op \
  -H 'Content-Type: application/json' -d '{"op":"rollback"}'

# 脚本有效期间：及时 409 且不执行；查询 / 取消 / 删除照常
SID=$(curl -s -X POST $BASE/script -H 'Content-Type: application/json' \
  -d '[{"side":"a","op":"begin"}]' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["script"]["id"])')
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/conn/b/op \
  -H 'Content-Type: application/json' -d '{"op":"begin"}'         # 409
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/conn/a/readonly \
  -H 'Content-Type: application/json' -d '{"enabled":true}'       # 409
curl -s $BASE/script                                              # 200，查询不阻塞
curl -s -X POST $BASE/script/cancel -H 'Content-Type: application/json' \
  -d "{\"script_id\":\"$SID\",\"step\":1}"                        # 200，取消后恢复手工
curl -s -X POST $BASE/conn/b/op \
  -H 'Content-Type: application/json' -d '{"op":"begin"}'         # 200
curl -s -X POST $BASE/conn/b/op \
  -H 'Content-Type: application/json' -d '{"op":"rollback"}'
curl -s -X DELETE $BASE                                           # {"deleted":true,...}
```

互斥顺序同样可用 HTTP 复现：手工 `BEGIN` 成功后 `POST …/script` 返回
`409`；先装载成功则手工 `BEGIN` / 只读切换返回 `409`。回归测试由实际
路由驱动（含并发交错），见下文「自动化交错测试」。

## 两栏手工请求的独立执行（串行化修复）

上一修复让手工路由在「复查 + 执行」整段持有实验级 `script_lock`：不再
卡死，但**两栏手工请求被串行化**——无有效脚本时，甲的读值（或任何手工
请求）尚未完成，乙的 `BEGIN` / 只读切换也要等甲结束才被处理；两栏互换
同理。页面上一栏的请求明明只该禁用本栏，另一栏的合法操作却被拖住。

修复后：手工操作（`POST …/conn/<side>/op`）与只读切换
（`POST …/conn/<side>/readonly`）只在 `script_lock` 内做「active 复查 +
在途计数」，随即释放锁在**本栏原连接**上执行——**同栏请求仍由本栏
`conn_lock` 串行，两栏互不等待**。与脚本装载的互斥保持不变：装载在同一把
锁内**等待在途计数归零**后，才持两栏 `conn_lock` 复查 `in_transaction`
——装载先成功则手工请求 `409` 且不执行，手工 `BEGIN` 先成功则装载
`409`，不会出现「装载成功后手工 `BEGIN` 才开启事务」；等待经 `wait()`
释放锁，在途请求与另一栏的新请求都不被阻塞。两个入口的请求字段与响应
约定不变；完成 / 取消脚本仍不代替提交、回滚；失败与被拒请求照常归还
准入名额，删除仍等待已准入请求收尾。

### 复现（修复前）

纯 HTTP 下每个请求都是毫秒级，串行化窗口即对方请求的在途时长，需把一侧
请求按住才能稳定观察。确定性复现由后端测试驱动（`RunOpGate` 把甲的读值
按在本栏连接临界区内，模拟「读值尚未完成」，再发乙的请求）：

```bash
python3 tests/backend_script.py
# 修复前：两栏·甲读值在途：乙BEGIN及时200          FAIL（乙等甲放行）
#         互换·乙读值在途：甲BEGIN及时200          FAIL
#         装载等待·期间乙BEGIN及时200              FAIL
# 修复后：全部通过
```

手工复现：在服务端对 `run_op` 下断点（或调试器暂停）按住甲栏请求，另一
终端 `curl` 乙栏 `BEGIN`——修复前乙等到断点放行才返回，修复后乙立即
返回。

### 修复后 HTTP 示例

```bash
EXP=$(curl -s -X POST http://localhost:8000/api/experiments \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
BASE=http://localhost:8000/api/experiments/$EXP

# 两栏独立：并发发出，各自立即返回，互不等待
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"begin"}' &
curl -s -X POST $BASE/conn/b/op -H 'Content-Type: application/json' \
  -d '{"op":"begin"}' &
wait
# 两个都立即 {"ok":true,...}

# 甲持写锁期间，乙的读值 / 只读切换照常立即返回（不等甲）；
# 只有乙的加法仍如实撞写锁 SQLITE_BUSY(5)——事务与锁约定不变
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"add","value":5}'
curl -s -X POST $BASE/conn/b/op -H 'Content-Type: application/json' \
  -d '{"op":"read"}'                # 立即 200
curl -s -X POST $BASE/conn/b/readonly -H 'Content-Type: application/json' \
  -d '{"enabled":true}'             # 立即 200
curl -s -X POST $BASE/conn/b/readonly -H 'Content-Type: application/json' \
  -d '{"enabled":false}'
curl -s -X POST $BASE/conn/b/op -H 'Content-Type: application/json' \
  -d '{"op":"add","value":7}'
# {"ok":false,"error":{"name":"SQLITE_BUSY","code":5,...},...}
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"commit"}'              # 已提交值 105
curl -s -X POST $BASE/conn/b/op -H 'Content-Type: application/json' \
  -d '{"op":"add","value":7}'       # 乙原事务重试成功
curl -s -X POST $BASE/conn/b/op -H 'Content-Type: application/json' \
  -d '{"op":"commit"}'              # 已提交值 112

# 与装载的互斥不变：手工 BEGIN 先成功则装载 409
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"begin"}'
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/script \
  -H 'Content-Type: application/json' -d '[{"side":"b","op":"begin"}]'
# 409
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"rollback"}'

# 装载先成功则手工请求 409 且不执行；取消后恢复
SID=$(curl -s -X POST $BASE/script -H 'Content-Type: application/json' \
  -d '[{"side":"a","op":"begin"}]' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["script"]["id"])')
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/conn/b/op \
  -H 'Content-Type: application/json' -d '{"op":"begin"}'          # 409
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/conn/a/readonly \
  -H 'Content-Type: application/json' -d '{"enabled":true}'        # 409
curl -s -X POST $BASE/script/cancel -H 'Content-Type: application/json' \
  -d "{\"script_id\":\"$SID\",\"step\":1}"                         # 200
curl -s -X DELETE $BASE                                            # {"deleted":true,...}
```

> 同栏请求仍串行：一栏的请求未完成时，该栏的下一个请求照常排队（页面
> 也会禁用本栏控件）；另一栏的请求不受任何影响。


## 数值约定

- 存储值为 SQLite 有符号 64 位整数（`-9223372036854775808` ～ `9223372036854775807`），
  正负边界均不会转成浮点数
- 单次增量上限为 ±2⁶²（`4611686018427387904`），超过即被 API 拒绝（`value too large`）
- 累加结果若超出 64 位整数范围，该次「加上」会被拒绝（SQLite 对整数溢出不会报错，
  而是静默存成 REAL，本实验在写入前做精确边界检查）；已开启的事务不受影响，
  之前的修改保留，仍可提交或回滚
- 传输编码：超出 JavaScript 安全整数范围（±2⁵³−1）的整数一律用十进制字符串传输，
  范围内仍返回 JSON 数字；「加上」请求的 `value` 字段两种形式都接受，
  旧的小整数数字请求照常可用
- 输入、操作响应、状态轮询、最近一次显式读值、日志与已提交值均无损显示完整十进制值

## 启动

```bash
docker compose up --build
```

访问：<http://localhost:8000>

实验数据（`LAB_DATA_DIR`，容器内 `/app/data`）由 Compose 命名卷
`lab-data` 持久化：`docker compose restart`、`docker compose down` 后
重新 `up`、甚至容器被强制终止，已有实验都按原 ID 恢复（删除卷才会清空：
`docker compose down -v`）。本地直接 `python3 app.py` 运行时数据在
`./data`，同样跨重启保留。

## 跨重启与强制终止恢复

每个实验的已提交值与提交时间线存放在**同一个 WAL 数据库**
（`exp_<id>.db` 的 `kv` 表与 `timeline` 表）。普通提交把「值 + 时间线
记录」放在**同一事务**提交（记录 `INSERT` 与本事务的 `COMMIT` 同生共死，
`SAVEPOINT` 保证失败提交撤销记录、重试不重复记录）；补偿用独立连接的
**显式事务**把 `UPDATE` 与记录 `INSERT` 一起提交。因此进程在任意时刻被
终止（含 `kill -9`），重启后只能恢复到**值与记录都生效或都未生效**的
状态——崩溃原子性由 SQLite 事务保证，不依赖任何退出清理。

启动时不再清空数据目录，而是扫描留存的 `exp_*.db` 重新加载：

- 已提交值就在 `kv` 表中，原样恢复；
- 提交时间线从 `timeline` 表完整恢复（含补偿关联 `compensates` 与序号
  `seq`），编号接续，末条普通记录仍可补偿；
- 两栏重建为**无事务、只读关闭、最近读值为空**；脚本、操作日志与检查点
  缓存不恢复（全新内存状态）；
- 未提交事务随进程死亡回滚，恢复后不可见；
- 成功删除的实验库文件已移除，**不会复活**（重启后按未知 ID 返回 404）；
- 旧版库（无 `timeline` 表）首次加载保留现值，以空时间线为基线。

### 可执行示例：重启后按原 ID 查询与补偿

```bash
# 启动（首次或重建）
docker compose up --build -d

# 新建实验并提交一笔：甲 100 +5 +7 → 112（时间线 #1）
EXP=$(curl -s -X POST http://localhost:8000/api/experiments \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
BASE=http://localhost:8000/api/experiments/$EXP
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"begin"}'
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"add","value":5}'
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"add","value":7}'
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"commit"}'
curl -s $BASE/committed                     # {"committed":112}

# 重启服务（容器重建，数据卷保留）
docker compose restart web && sleep 2

# 按原 ID 查询：已提交值与完整时间线都在
curl -s $BASE/committed                     # {"committed":112}
curl -s $BASE/timeline
# {"timeline":[{"seq":1,"side":"a","before":100,"after":112,"deltas":[5,7]}]}

# 恢复后末条普通记录仍可补偿：值恢复 100，追加 #2（system）
curl -s -X POST $BASE/timeline/compensate \
  -H 'Content-Type: application/json' -d '{"seq":1}'
# {"ok":true,...,"record":{"seq":2,"side":"system","before":112,"after":100,
#  "deltas":[-7,-5],"compensates":1},...}
curl -s $BASE/committed                     # {"committed":100}

# 恢复后继续编号：再提交一笔为 #3
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"begin"}'
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"add","value":3}'
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"commit"}'
curl -s $BASE/timeline
# [#1 a 100→112 [5,7], #2 system 112→100 [-7,-5] 补偿#1, #3 a 100→103 [3]]
```

### 可执行示例：强制终止（kill -9）后恢复

```bash
# 向运行中的容器直接发 SIGKILL（等价于进程崩溃，无任何退出清理）
EXP=<上面创建的实验 id>
BASE=http://localhost:8000/api/experiments/$EXP
curl -s -X POST $BASE/conn/b/op -H 'Content-Type: application/json' \
  -d '{"op":"begin"}'
curl -s -X POST $BASE/conn/b/op -H 'Content-Type: application/json' \
  -d '{"op":"add","value":10}'
curl -s -X POST $BASE/conn/b/op -H 'Content-Type: application/json' \
  -d '{"op":"commit"}'                  # 时间线 #4：103→113
docker compose kill web                 # 强制终止，不做任何清理
docker compose up -d web && sleep 2     # 重新拉起

# 按原 ID 访问：值与时间线（含此前的补偿关联）完整恢复
curl -s $BASE/committed                 # {"committed":113}
curl -s $BASE/timeline
# [#1 a 100→112 [5,7], #2 system 112→100 [-7,-5] compensates:1,
#  #3 a 100→103 [3], #4 b 103→113 [10]]

# 两栏已重建为无事务、只读关闭、最近读值为空；脚本 / 日志 / 检查点缓存为空
curl -s $BASE/status
# {"a":{"in_transaction":false,"readonly":false,"last_read":null,"log":[]},
#  "b":{...同样为空...},"checkpoint":{"last":null,"log":[]},
#  "script":null,"timeline":[...]}

# 末条普通记录仍可补偿
curl -s -X POST $BASE/timeline/compensate \
  -H 'Content-Type: application/json' -d '{"seq":4}'
curl -s $BASE/committed                 # {"committed":103}
```

> 若强制终止时某栏还有**未提交**事务，该事务随进程死亡回滚：重启后
> 已提交值与时间线都不含它，两栏均显示无事务。删除过的实验在重启后
> 不会复活（库文件已删除），按未知 ID 返回 `404`；本地
> `python3 app.py` 运行时把上面的 `docker compose kill web` 换成
> `kill -9 <pid>`、重新 `python3 app.py` 即可，效果相同。

## 前端交错问题的复现

修复前有两类前端串扰：

1. **新建期间旧轮询串入新页面**：手动点击「新建实验」时，新的代号
   （`expGen`）立即生效，但旧实验的轮询定时器仍在运行；一次 2 秒轮询恰好在
   创建请求进行中触发时，回调带着新代号向**旧 ID** 发出 `status` / `committed`
   请求。新建成功后该响应才返回：旧 **200** 会用旧实验的读值、日志、已提交值
   覆盖新页面；旧实验若正在删除，迟到的 **410** 会把**新实验**冻结。
2. **冻结页新建失败被误解冻**：已删除页面上手动新建若请求失败，旧代码会
   无条件重新启用两栏控件并放开删除 / 刷新按钮——旧实验其实已经不存在，
   页面却脱离了冻结态，下一次操作才又收到 410。
3. **只读开关失败后勾选与文案矛盾**：当前只读关闭，勾选「只读开关」后请求
   仍在途；在途期间一次状态轮询先返回 `readonly=true`（例如另一客户端 / HTTP
   已先开启，或服务端先于失败的开关响应被确认），文案变为「开启」。随后开关
   请求本身网络失败（或返回非 2xx），旧代码只把复选框机械退回点击前的
   `false`，文案却停留在「开启」；更糟的是之后每次轮询返回的状态 JSON 都与
   已渲染的完全相同，旧的渲染去重直接跳过，矛盾再也无法被相同状态纠正。

### 浏览器手工复现（修复前行为）

1. 打开页面，等实验 `OLD` 加载完成（轮询每 2 秒一次）。
2. 打开 DevTools → Network，勾选 **Throttling** 自定义慢速（或用断点 /
   `fetch` 拦截），让 `POST /api/experiments` 挂起 3 秒以上。
3. 点击「新建实验」。旧代码在这 3 秒内仍会对 `…/OLD/status`、
   `…/OLD/committed` 发出请求。
4. 另开终端删除旧实验：
   `curl -X DELETE http://localhost:8000/api/experiments/<OLD_ID>`，
   再放行挂起的新建请求：新页面出现后被旧轮询的 410 **冻结**；
   不删除旧实验直接放行则新读值被旧 **200** 覆盖。
5. 冻结页面上断网后点击「新建实验」：修复前失败提示出现的同时旧控件被
   重新启用（误解冻）。

修复后：创建期间无任何发往旧 ID 的请求；成功后只启用新 ID 的控件与轮询；
冻结页新建失败保持冻结并显示错误，手动重试成功才恢复。

只读开关矛盾的手工复现（DevTools 中用断点 / `fetch` 拦截控制响应顺序）：

1. 打开页面，等实验加载完成（两栏只读均为「关闭」，轮询每 2 秒一次）。
2. 在 Network 面板对 `conn/a/readonly` 请求设置 **断点 / 拦截**（或用慢速
   节流），勾选甲栏「只读开关」：请求挂起，本栏控件禁用、文案与复选框都先
   显示为「开启」（乐观一致）。
3. 等下一次 `status` 轮询发出后，先用 DevTools 的「重写响应 / Override
   content」（或拦截后直接构造）让该 `status` 返回
   `"a": {..., "readonly": true, "log": [...]}`（与随后各轮完全相同）：
   页面文案确认为「开启」、复选框勾选。
   （真实服务端场景：先用 HTTP 直接开启本栏只读，
   `curl -X POST .../conn/a/readonly -d '{"enabled":true}'`，再让页面自己的
   开关请求失败，效果相同。）
4. 放行挂起的开关请求为失败（DevTools 中 Block request URL，或直接断网后
   放行）：修复前复选框弹回「未勾选」，而文案仍为「开启」，本栏出现请求
   失败错误；之后每 2 秒返回的 `status` 都带同样的 `readonly=true`，修复前
   因渲染去重矛盾一直保留。
5. 修复后：失败瞬间复选框与文案一致停在最近一次服务端确认的「开启」并显示
   本栏错误；此后任何一次同状态轮询（甚至无需等日志 / 读值变化）都会再次把
   勾选纠正为「开启」。若请求在途期间没有任何轮询确认，则失败后勾选与文案
   一起恢复点击前的旧模式（如关闭）。乙栏规则完全相同；整个过程中甲栏请求
   只禁用甲栏、乙栏始终可操作，「新建实验」期间两栏开关均禁用，删除后冻结、
   旧实验的迟到开关响应不影响新实验。

### 自动化交错测试

`tests/` 下提供无第三方依赖的 Node 脚本，用可手动放行的假 `fetch`
（响应顺序完全可控）与假 `setInterval` 复现上述交错：

```bash
node tests/races.test.js
```

`tests/readonly.test.js` 用同一个假环境覆盖每栏只读开关：状态同步到文案 /
复选框、请求期间只禁用本栏（另一栏照常）、两栏独立、布尔请求体、网络失败回退、
**勾选开启后请求在途、轮询先确认 `readonly=true` 而开关请求失败（两栏各一次）：
失败显示本栏错误、勾选与文案一致恢复为最近一次服务端确认的开启、随后相同状态
轮询仍能纠正人为制造的本地偏差**、**期间没有新确认时失败恢复原已确认模式
（已确认开启→尝试关闭失败恢复开启；已确认关闭→尝试开启失败恢复关闭）**、
410 冻结、404 自动新建后开关重置、旧开关迟到响应不影响新实验、冻结页点击不发请求：

```bash
node tests/readonly.test.js
```

覆盖：创建期间旧轮询被暂停（含已入队迟到回调）、创建进行中触发的旧轮询
迟到 200 / 410 均不改新页面也不冻结、旧操作 / 检查点 / 删除的成功 / 失败 /
收尾响应不影响新实验、冻结页新建失败保持禁用且轮询停止（重试成功恢复）、
存活页新建失败恢复旧实验、404 自动新建且并发只建一次、410 冻结不自动新建；
只读开关的「轮询先确认 → 请求失败」交错回退、无新确认回退原模式、相同状态
轮询纠正本地偏差。

`tests/script.test.js` 覆盖脚本逐步执行的前端行为：原文装载（大整数不经
JS Number）、active 时两栏手工控件与只读开关锁定（检查点不锁）、推进 / 跳过 /
取消携带脚本 ID 与所见步号、步失败停步可重试且记录原始错误名 / 码、跳过记
`skipped`、409 过期 / 互斥请求本地不改动并立即以服务端状态重核对、完成 / 取消
后恢复手工操作、新建禁用与删除冻结、旧脚本迟到响应不影响新实验、新脚本 ID
替换旧脚本后动作按新 ID 发出：

```bash
node tests/script.test.js
```

`tests/backend_script.py` 不依赖 Flask，用最小 stub 导入 `app.py`，在真实
SQLite 上驱动脚本状态机：整份校验各分支、装载与手工 BEGIN 互斥、写锁冲突后
失败不回滚 / 重试 / 跳过 / 对方提交后原事务再加法、完成与取消的生命周期、
过期脚本 ID 与步号的拒绝、取消不自动回滚、有事务时拒绝再装载；并用同一
stub 的最小路由分发**直接驱动实际路由**（真实准入门控与实验级锁，不另写
模拟准入逻辑）：无有效脚本时手工 BEGIN / 只读切换及时返回、脚本有效期间
及时 409 且不执行、被拒请求归还名额后查询 / 推进 / 删除不阻塞、完成 /
取消后恢复手工操作且不自动提交 / 回滚、手工 BEGIN 与装载的并发交错恰好
一方成功、只读切换绝不在脚本有效期执行；再用 `RunOpGate` 把指定栏操作
按在在途状态、手动放行，**确定性控制实际路由执行顺序**：甲读值在途时乙的
BEGIN / 只读切换及时完成（两栏互换同理）、同栏请求仍串行、装载等待在途
手工收尾（在途 BEGIN 先成功则装载 409）、装载等待期间另一栏照常推进、
在途读值不留事务时装载等其收尾后成功：

```bash
python3 tests/backend_script.py
```

`tests/backend_compensate.py` 用同一个 stub 驱动实际路由，覆盖补偿末条提交：
甲 100 +5 +7 提交后补偿 #1（值恢复 100，追加 `system` 记录 112→100、
增量 [-7,-5]、`compensates=1`，原记录保留）、已有读快照仍见旧值、两栏事务 /
只读模式 / 最近显式读值不变、过期目标 / 补偿记录 / 空时间线 / 有效脚本一律
409、写锁冲突立即返回 `SQLITE_BUSY(5)` 且不改库与时间线、并发同一 seq 最多
成功一次（20 轮）、与普通提交交错时重新核对目标且结果一致（20 轮）、大整数
无损、非法 seq 一律 400、删除后 410 / 未知实验 404：

```bash
python3 tests/backend_compensate.py
```

`tests/backend_persist.py` 用同一个 stub 驱动实际路由，并用「关闭并丢弃
内存对象 + 重新扫描数据目录」模拟进程终止（含 kill -9：未提交事务随连接
关闭回滚，已提交事务由 WAL 恢复），覆盖跨重启 / 强制终止恢复：提交 + 补偿
后重启完整恢复已提交值与完整时间线（含 `compensates` 与序号）、两栏重建为
无事务 / 只读关闭 / 最近读值为空、日志 / 脚本 / 检查点缓存不恢复、恢复后
继续编号且末条普通记录仍可补偿、删除后不复活（404）、旧版库保留现值以空
时间线为基线、COMMIT 前后「崩溃」分别两者都未生效 / 都生效、失败提交保留
原事务且重试不重复记录（注入 INSERT 后失败）、只读开启下提交仍产生记录、
失败加法 / 纯读提交 / 回滚不产生记录、未提交事务随崩溃回滚、大整数无损、
恢复幂等：

```bash
python3 tests/backend_persist.py
```

`tests/compensate.test.js` 用假 `fetch` 环境覆盖补偿控件的前端行为：正整数
序号校验（非法输入不发请求）、请求在途禁用、成功后渲染系统补偿记录并刷新
已提交值、`SQLITE_BUSY` 如实显示、409 本地不猜测并立即以服务端状态重核对、
新建进行中禁用、删除后冻结（点击不发请求）、旧实验迟到响应不影响新实验、
410 冻结：

```bash
node tests/compensate.test.js
```

`tests/backend_query.py` 用同一个 stub 驱动实际路由，覆盖只读 SQL 工作台：
1～4000 字符与单独 SELECT/WITH 词法校验（写入 / PRAGMA / ATTACH /
load_extension / pragma 表值函数 / 多语句 / EXPLAIN / VALUES 等 35 例一律
400，非法绝不入队）、合法但运行时失败入队记 failed 并附原始错误名 / 码、
列名 / 前 100 行 / truncated 截断、整数无损与 BLOB 十六进制 / NULL / REAL /
TEXT 编码、running 再提交 409、终结后替换旧 ID 404 且不影响新查询、
interrupt 中断无界递归 CTE、尚未开跑（worker 挡在开连接前）与连接已开未执行
（挡在 execute 前）两种取消、取消先确认绝不成功（10 轮）、已终结再取消不改
结果、只读连接不触碰两栏事务 / 只读开关 / 快照 / 最近读值且不改值与时间线、
不阻塞其他实验、删除先取消在途查询并等连接关闭再删文件、查询不跨重启保留：

```bash
python3 tests/backend_query.py
```

`tests/workbench.test.js` 用假 `fetch` 环境覆盖工作台前端：空 / 超长本地
拒绝、请求体与在途禁用、running 立即渲染并轮询、成功表格（列名 / NULL /
大整数文本）与 100 行截断提示、failed 原始错误名 / 码、取消中断后停轮询、
查询 ID 404 只复位不自动新建、实验 404 沿用自动新建、410 冻结、新建进行中
禁用与新建后复位（SQL 文本保留）、删除冻结停止轮询、旧查询 ID 迟到响应不
覆盖新查询：

```bash
node tests/workbench.test.js
```

## 操作示例

### 示例一：快照隔离与快照冲突（SQLITE_BUSY_SNAPSHOT）

1. 甲：`BEGIN DEFERRED` → `读值` → 显示 **100**
2. 乙：`BEGIN DEFERRED` → 输入 `10` → `加上` → `提交`
   （顶部「已提交值」变为 **110**）
3. 甲：`读值` → 仍为 **100**（甲的读快照不受乙已提交的修改影响）
4. 甲：`加上` → 立即报错 **SQLITE_BUSY_SNAPSHOT（错误码 517）**，甲的事务仍然开启
5. 甲：`回滚` → `BEGIN DEFERRED` → `读值` → 显示 **110**

### 示例二：写锁冲突（SQLITE_BUSY）——提交释放写锁后在原事务重试

1. 甲：`BEGIN DEFERRED` → 输入 `5` → `加上`（先不提交，甲持有写锁）
2. 乙：`BEGIN DEFERRED`（不显式读值）→ 输入 `7` → `加上` →
   立即报错 **SQLITE_BUSY（错误码 5）**，乙的事务不会被自动结束
3. 甲：`提交`（释放写锁，已提交值变为 **105**）
4. 乙：**在原事务中**再次输入 `7` → `加上` → 成功（不会报
   SQLITE_BUSY_SNAPSHOT）→ `提交`（已提交值变为 **112**）

> 修复前，「加上」内部的边界检查会先执行一条 SELECT，提前为乙建立读快照：
> 第 2 步拿到 SQLITE_BUSY 后快照已经留下，第 4 步会被误报为
> SQLITE_BUSY_SNAPSHOT（517）。现在内部检查不在乙的连接上读值，
> 写锁冲突不建立读快照，原事务即可直接重试。

若第 3 步甲改为 `回滚`，第 4 步乙重试并提交后值为 **107**（甲的 +5 被撤销）。

### 示例二补充：显式读值后冲突 → 必须自行回滚重开（SQLITE_BUSY_SNAPSHOT）

1. 乙：`BEGIN DEFERRED` → `读值` → 显示 **100**（读快照建立）
2. 甲：`BEGIN DEFERRED` → 输入 `5` → `加上` → `提交`
3. 乙：输入 `7` → `加上` → 立即报错 **SQLITE_BUSY_SNAPSHOT（错误码 517）**，
   乙的事务仍开启，系统**不会**自动回滚、不替换连接、也不改变事务类型
4. 乙：`回滚` → `BEGIN DEFERRED` → 输入 `7` → `加上` → `提交`
   （已提交值变为 **112**）

### 示例二补二：两栏只读开关（PRAGMA query_only）与已有事务、写锁的关系

> 开关在**该栏原长期连接**上真实执行 `PRAGMA query_only=1/0`：不换连接、不结束
> 事务、不先读值（不建立读快照）。开启后读值 / BEGIN / 提交 / 回滚照常，
> 加法按钮也照常可点；未越界的加法由 SQLite 返回 **SQLITE_READONLY（8）**，
> 但不撤销此前修改。提交 / 回滚不重置开关；两栏开关互相独立。

1. 甲：`BEGIN DEFERRED` → 输入 `5` → `加上`（先不提交，甲持有写锁）
2. 甲：勾选本栏 **只读开关** → 日志显示
   `只读模式：开启 → 成功（原连接、当前事务不变；提交 / 回滚不重置本开关）`，
   状态栏「只读」变为 **开启**
3. 甲：输入 `7` → `加上` → 立即报错 **SQLITE_READONLY（错误码 8）**：
   attempt to write to a readonly database。甲的事务仍然开启，第 1 步的 +5
   **不被撤销**；甲点 `读值` 仍能读（此时读到本事务内的 **105**）
4. 乙：`BEGIN DEFERRED`（不显式读值）→ 输入 `7` → `加上` →
   立即报错 **SQLITE_BUSY（错误码 5）**（甲仍持有写锁；只读是甲自己连接的事，
   与写锁互不干扰），乙的事务不会被自动结束
5. 甲：`提交`（只读下允许提交：+5 落地，已提交值变为 **105**）；
   提交后甲的只读开关**仍为开启**
6. 乙：**在原事务中**再次输入 `7` → `加上` → 成功 → `提交`
   （已提交值变为 **112**）
7. 甲：取消勾选只读开关（`PRAGMA query_only=0`）→ `BEGIN DEFERRED` →
   输入 `3` → `加上` → 成功（关闭后在原 / 新事务都能继续写）

补充：在只读开启时 `回滚` 同样允许——例如甲事务内先 +9 再开启只读，
`回滚` 会撤销 +9（已提交值不变），且回滚后开关**仍为开启**；开关只由本栏的
开关动作改变。若加法的累加结果本身就超出 64 位整数范围，仍优先被既有的
溢出检查拒绝（「累加结果超出有符号 64 位整数范围，本次未修改」）。

#### HTTP 调用示例

开关接口为每栏独立的 POST，body 必须且只能是 JSON 布尔；模式同时出现在
`/status` 的 `a.readonly` / `b.readonly` 中：

```bash
EXP=<新建实验返回的 id>

# 甲：事务内 +5 后开启只读
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/a/op \
  -H 'Content-Type: application/json' -d '{"op":"begin"}'
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/a/op \
  -H 'Content-Type: application/json' -d '{"op":"add","value":5}'
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/a/readonly \
  -H 'Content-Type: application/json' -d '{"enabled":true}'
# {"ok":true,"error":null,
#  "state":{"in_transaction":true,"readonly":true,"last_read":null,"log":[...]}}

# 只读下再加 7：SQLite 原样返回 SQLITE_READONLY(8)，事务保持开启，+5 保留
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/a/op \
  -H 'Content-Type: application/json' -d '{"op":"add","value":7}'
# {"ok":false,
#  "error":{"name":"SQLITE_READONLY","code":8,
#           "message":"attempt to write to a readonly database"},
#  "state":{"in_transaction":true,"readonly":true,...}}

# 读值 / 提交在只读下照常；提交后 readonly 仍为 true（提交不重置开关）
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/a/op \
  -H 'Content-Type: application/json' -d '{"op":"read"}'
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/a/op \
  -H 'Content-Type: application/json' -d '{"op":"commit"}'

# 关闭只读：原连接执行 PRAGMA query_only=0，之后可继续加法
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/a/readonly \
  -H 'Content-Type: application/json' -d '{"enabled":false}'

# 非布尔值一律 400（1/0、"true"/"false"、null、缺字段、非 JSON body 同理）
for v in '1' '0' '"true"' 'null'; do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST \
    http://localhost:8000/api/experiments/$EXP/conn/a/readonly \
    -H 'Content-Type: application/json' -d "{\"enabled\":$v}"
done
# 400
# 400
# 400
# 400

# 当前模式随状态接口返回（不执行任何 SQL）
curl -s http://localhost:8000/api/experiments/$EXP/status
# {"id":"...","a":{"in_transaction":false,"readonly":false,...},
#  "b":{"in_transaction":false,"readonly":false,...},"checkpoint":{...}}
```

> 开关请求与本栏其它操作共用同一规则：请求未完成时禁用本栏控件（另一栏不受
> 影响）；「新建实验」进行中禁用；实验删除后冻结、接口返回 **410**；
> 旧实验的迟到开关响应不会影响新实验页面。请求失败（网络错误 / 非 2xx）时
> 错误显示在本栏，复选框与文案一起恢复到**最近一次服务端确认的模式**
> （在途轮询已确认新模式则恢复新模式，无新确认则恢复点击前模式）；相同模式
> 的后续轮询也会纠正本地偏差，无须等日志或读值变化。详见上文
> 「前端交错问题的复现」第 3 类。


### 示例三：大整数精度（9007199254741093）

1. 甲：`BEGIN DEFERRED` → 输入 `9007199254740993`（超出 JavaScript 安全整数范围，
   前端会以十进制字符串发送）→ `加上` → `提交`
2. 顶部「已提交值」显示 **9007199254741093**（= 100 + 9007199254740993，精确）
3. 甲：`BEGIN DEFERRED` → `读值` → 同样显示 **9007199254741093**
   （修复前因 `parseInt` 精度丢失会少 1，显示 9007199254741092）

### 示例四：64 位溢出拒绝（不会存成 REAL）

1. 甲：`BEGIN DEFERRED` → 输入 `4000000000000000000` → `加上` → 成功
   （当前值 4000000000000000100）
2. 甲：再次 `加上` → 成功（当前值 8000000000000000100）
3. 甲：第三次 `加上` → 被拒绝：**累加结果超出有符号 64 位整数范围，本次未修改**
   （修复前 SQLite 会静默存成浮点数 `1.2e+19`）
4. 甲的事务仍然开启，前两次结果保留：可 `提交`
   （已提交值变为 **8000000000000000100**），也可 `回滚`（已提交值保持 100）

### 示例五：读事务阻止 WAL 回收（手动 PASSIVE / TRUNCATE 检查点）

> 检查点每次由**独立的全新连接**真实执行 `PRAGMA wal_checkpoint(...)`，
> `busy_timeout=0`；甲、乙两栏的写连接均已关闭自动检查点
> （`PRAGMA wal_autocheckpoint=0`），不点击按钮 / 不调 HTTP 就不会发生检查点。
> 返回值为 SQLite 原始三项：`busy`、`log`（WAL 总帧数）、
> `checkpointed`（已回写帧数），另附操作后 WAL 文件字节数。

1. 甲：`BEGIN DEFERRED` → `读值` → 显示 **100**（读事务建立读快照）
2. 乙：`BEGIN DEFERRED` → 输入 `10` → `加上` → `提交`
   （顶部「已提交值」变为 **110**，WAL 新增 1 帧）
3. 点击 **PASSIVE 检查点** → 显示 **busy=0，WAL 总帧数=1，已回写帧数=0，
   WAL=4152 字节，回写未完成（0/1 帧）**（4152 = 32 字节 WAL 头 + 24 字节帧头 +
   4096 字节数据页，即默认页大小下的 1 帧；若页大小不同字节数相应变化）：
   甲的读快照还停留在该帧之前，SQLite 不能把帧回写到主库，WAL 也不能被复用 / 截断
4. 点击 **TRUNCATE 检查点** → **立即**返回 **busy=1，WAL 总帧数=1，
   已回写帧数=0，WAL=4152 字节**（`busy_timeout=0` 不等待）；
   甲此时再点 `读值` 仍为 **100**（检查点不改变甲的快照，也不提交 / 回滚甲的事务）
5. 甲：`回滚`（读事务结束，不再有连接保留旧读快照）
6. 再次点击 **TRUNCATE 检查点** → **busy=0，WAL 总帧数=0，已回写帧数=0，
   WAL=0 字节**，即 SQLite 原始结果 `(0,0,0)`，WAL 文件被截断为 0 字节
7. 顶部「已提交值」仍为 **110**：检查点只负责把已提交的帧搬回主库，
   不会撤销乙的提交

> 页面上的状态轮询只返回最近一次检查点结果的内存缓存，不执行任何 SQL、
> 不触发检查点；检查点按钮请求进行中只禁用检查点按钮，甲、乙两栏照常操作。

#### HTTP 调用示例

检查点接口既接受路径参数，也接受 JSON body（模式大小写均可）：

```bash
EXP=<新建实验返回的 id>

# PASSIVE 检查点（路径形式）
curl -s -X POST http://localhost:8000/api/experiments/$EXP/checkpoint/PASSIVE
# {"busy":0,"checkpointed":0,"log_frames":1,"mode":"PASSIVE","ok":true,
#  "wal_size":4152, ...}   读事务未结束时已回写帧数为 0，WAL 保持 4152 字节

# TRUNCATE 检查点（JSON body 形式）
curl -s -X POST http://localhost:8000/api/experiments/$EXP/checkpoint \
  -H 'Content-Type: application/json' -d '{"mode":"TRUNCATE"}'
# 甲未回滚时：{"busy":1,"checkpointed":0,"log_frames":1,"wal_size":4152,...}
# 甲回滚后：  {"busy":0,"checkpointed":0,"log_frames":0,"wal_size":0,...}
```

甲、乙两栏操作仍使用原接口，例如：

```bash
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/a/op \
  -H 'Content-Type: application/json' -d '{"op":"begin"}'
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/a/op \
  -H 'Content-Type: application/json' -d '{"op":"read"}'
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/b/op \
  -H 'Content-Type: application/json' -d '{"op":"add","value":10}'
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/b/op \
  -H 'Content-Type: application/json' -d '{"op":"commit"}'
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/a/op \
  -H 'Content-Type: application/json' -d '{"op":"rollback"}'
```

删除实验使用 `DELETE` 方法（页面上对应「删除当前实验」按钮，点击后需二次确认）：

```bash
EXP=<新建实验返回的 id>

# 删除：等待在途请求完成 → 回滚未提交事务 → 关闭连接 → 删除 DB/WAL/SHM
curl -s -X DELETE http://localhost:8000/api/experiments/$EXP
# {"deleted":true,"id":"xxxxxxxxxxxx"}

# 本次运行中再次删除同一 ID（以及该 ID 的任何接口）：410 Gone
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE \
  http://localhost:8000/api/experiments/$EXP
# 410

# 从未存在过的 ID：404 Not Found
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE \
  http://localhost:8000/api/experiments/does-not-exist
# 404
```

> 删除等待期间该实验的新请求（操作 / 状态 / 已提交值 / 检查点）立即返回 **410**，
> 已进入的请求正常完成；删除不会阻塞其他实验，未删除实验的甲、乙两栏仍可独立操作。
> 页面收到删除成功或 410 后停止轮询并禁用旧实验操作，迟到响应不会恢复旧展示，
> 也不会自动新建实验；需要时点击「新建实验」即可。

### 示例六：提交时间线（按事务汇总，按实际提交顺序编号）

> 仅「至少一次加法成功且 **COMMIT 成功**」的事务产生一条记录：栏位、首次成功
> 加法前的值、提交后的值与按执行顺序排列的成功增量。失败加法不计入；回滚、
> 失败提交与纯读事务不记录；未提交内容不可见；提交重试成功只记一次。
> 页面底部区块随状态轮询自动刷新，也可通过 HTTP 直接查询。

1. 甲：`BEGIN DEFERRED` → 输入 `5` → `加上` → 输入 `7` → `加上`（先不提交，
   甲持有写锁）
2. 乙：`BEGIN DEFERRED`（不显式读值）→ 输入 `3` → `加上` →
   立即报错 **SQLITE_BUSY（错误码 5）**——失败加法**不计入**时间线，
   乙的事务不会被自动结束
3. 甲：`提交` → 时间线出现第 1 条：**#1 甲（A） 100 → 112 增量 [5, 7]**
4. 乙：**在原事务中**再次输入 `3` → `加上` → 成功 → `提交` →
   时间线追加第 2 条：**#2 乙（B） 112 → 115 增量 [3]**
   （「首次成功加法前的值」是乙重试时实际看到的 112，不是失败时的 100；
   记录顺序与实际提交顺序一致）

#### HTTP 调用示例

```bash
EXP=<新建实验返回的 id>

# 甲：事务内 +5、+7（先不提交）
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/a/op \
  -H 'Content-Type: application/json' -d '{"op":"begin"}'
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/a/op \
  -H 'Content-Type: application/json' -d '{"op":"add","value":5}'
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/a/op \
  -H 'Content-Type: application/json' -d '{"op":"add","value":7}'

# 乙：甲未提交时加法撞写锁（失败加法不计入时间线）
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/b/op \
  -H 'Content-Type: application/json' -d '{"op":"begin"}'
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/b/op \
  -H 'Content-Type: application/json' -d '{"op":"add","value":3}'
# {"ok":false,"error":{"name":"SQLITE_BUSY","code":5,...},...}

# 甲提交：时间线出现第 1 条
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/a/op \
  -H 'Content-Type: application/json' -d '{"op":"commit"}'

# 乙在原事务重试 +3 并提交：时间线追加第 2 条
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/b/op \
  -H 'Content-Type: application/json' -d '{"op":"add","value":3}'
curl -s -X POST http://localhost:8000/api/experiments/$EXP/conn/b/op \
  -H 'Content-Type: application/json' -d '{"op":"commit"}'

# 查询提交时间线（只读内存，不在两栏连接上读值）
curl -s http://localhost:8000/api/experiments/$EXP/timeline
# {"timeline":[
#   {"seq":1,"side":"a","before":100,"after":112,"deltas":[5,7]},
#   {"seq":2,"side":"b","before":112,"after":115,"deltas":[3]}]}

# 同一时间线也随状态接口返回（轮询不执行任何 SQL）
curl -s http://localhost:8000/api/experiments/$EXP/status
# {"id":"...","a":{...},"b":{...},"checkpoint":{...},
#  "timeline":[{"seq":1,"side":"a","before":100,"after":112,"deltas":[5,7]},
#              {"seq":2,"side":"b","before":112,"after":115,"deltas":[3]}]}
```

> 不产生记录的情形：事务只有读值（纯读事务）、加法全部失败（如 SQLITE_BUSY /
> SQLITE_READONLY / 越界拒绝）、`回滚` 收尾、`提交` 失败（重试成功才记录一次）。
> 大整数同样无损：超出 ±2⁵³−1 的 `before` / `after` / `deltas` 以十进制字符串
> 返回。时间线与值在同一事务写入实验库，随实验删除而移除；服务重启 / 进程
> 被强制终止后按原实验 ID 完整恢复（见「跨重启与强制终止恢复」）。

### 示例七：脚本逐步执行——乙遇写锁后跳过、甲提交、乙再次加法

> 脚本装载后两栏手工事务操作与只读开关即被后端锁定；每个动作都在**该栏原长期
> 连接**真实执行。步失败（写锁冲突）不前移、不回滚，可重试或跳过；推进 / 跳过 /
> 取消必须带脚本 ID 与所见步号，过期请求返回 `409` 且不影响当前脚本。

**页面操作**：在「脚本逐步执行」区块点「填入：乙写锁跳过示例」（或粘贴下面的
JSON）→ `装载脚本`，然后反复点 `推进一步`；到第 4 步乙的加法撞甲写锁时点
`跳过本步`，再继续推进到完成。

脚本（7 步；第 4 步是乙撞锁的加法，撞锁后由人在页面点「跳过本步」）：

```json
[
  {"side": "a", "op": "begin"},
  {"side": "a", "op": "add", "value": 5},
  {"side": "b", "op": "begin"},
  {"side": "b", "op": "add", "value": 7},
  {"side": "a", "op": "commit"},
  {"side": "b", "op": "add", "value": 7},
  {"side": "b", "op": "commit"}
]
```

逐步结果：第 1～3 步成功（甲 BEGIN、甲 +5 持写锁、乙 BEGIN）；第 4 步乙 +7
立即失败 **SQLITE_BUSY（错误码 5）**，步号停在 4、乙事务不被回滚；**跳过本步**
后到第 5 步，甲 COMMIT（已提交值 **105**）；第 6 步乙在**原事务**再次 +7 成功
（无读快照，不报 SQLITE_BUSY_SNAPSHOT）；第 7 步乙 COMMIT（已提交值 **112**），
脚本 `finished`，手工操作恢复。

#### HTTP 调用示例

```bash
EXP=<新建实验返回的 id>
BASE=http://localhost:8000/api/experiments/$EXP

# 装载：1～50 步 JSON 数组（非法整份 400；有事务 / 已有有效脚本 409）
curl -s -X POST $BASE/script -H 'Content-Type: application/json' -d '[
  {"side":"a","op":"begin"},
  {"side":"a","op":"add","value":5},
  {"side":"b","op":"begin"},
  {"side":"b","op":"add","value":7},
  {"side":"a","op":"commit"},
  {"side":"b","op":"add","value":7},
  {"side":"b","op":"commit"}
]'
# {"ok":true,"script":{"id":"f1a2b3c4d5e6","status":"active","step":1,
#   "total":7,"steps":[...],"records":[]}}

SID=f1a2b3c4d5e6

# 逐步推进（必须带脚本 ID 与所见步号）
curl -s -X POST $BASE/script/advance -H 'Content-Type: application/json' \
  -d "{\"script_id\":\"$SID\",\"step\":1}"   # 甲 BEGIN → ok:true, step:2
curl -s -X POST $BASE/script/advance -H 'Content-Type: application/json' \
  -d "{\"script_id\":\"$SID\",\"step\":2}"   # 甲 +5    → ok:true, step:3
curl -s -X POST $BASE/script/advance -H 'Content-Type: application/json' \
  -d "{\"script_id\":\"$SID\",\"step\":3}"   # 乙 BEGIN → ok:true, step:4

# 第 4 步乙 +7 撞甲写锁：ok=false，步号仍是 4，记录原始错误名 / 码
curl -s -X POST $BASE/script/advance -H 'Content-Type: application/json' \
  -d "{\"script_id\":\"$SID\",\"step\":4}"
# {"ok":false,"script":{"id":"f1a2b3c4d5e6","status":"active","step":4,
#   "total":7,...,"records":[
#     ...,
#     {"step":4,"side":"b","op":"add","value":7,"result":"failure",
#      "error":{"name":"SQLITE_BUSY","code":5,"message":"database is locked"}}]}}

# 失败不回滚、不前移：可带同一脚本 ID / 步号重试（仍会失败，直到甲提交），
# 或跳过本步（只记 skipped，不执行 SQL）
curl -s -X POST $BASE/script/skip -H 'Content-Type: application/json' \
  -d "{\"script_id\":\"$SID\",\"step\":4}"
# {"ok":true,"script":{"...","step":5,...,"records":[...,
#   {"step":4,"side":"b","op":"add","value":7,"result":"skipped","error":null}]}}

# 第 5 步甲提交（已提交值 105）
curl -s -X POST $BASE/script/advance -H 'Content-Type: application/json' \
  -d "{\"script_id\":\"$SID\",\"step\":5}"   # → step:6

# 第 6 步乙在原事务再次 +7：成功（写锁已释放、无快照）
curl -s -X POST $BASE/script/advance -H 'Content-Type: application/json' \
  -d "{\"script_id\":\"$SID\",\"step\":6}"   # → step:7

# 第 7 步乙提交：脚本 finished，手工操作恢复（不自动提交 / 回滚）
curl -s -X POST $BASE/script/advance -H 'Content-Type: application/json' \
  -d "{\"script_id\":\"$SID\",\"step\":7}"
# {"ok":true,"script":{"id":"f1a2b3c4d5e6","status":"finished","step":null,
#   "total":7,...}}

curl -s $BASE/committed                     # {"committed":112}

# 查询脚本（脚本 ID、当前步号、全部步骤、逐次记录；只读内存）
curl -s $BASE/script
curl -s $BASE/status | python3 -m json.tool # script 字段随状态轮询返回
```

取消（在任何未完成步停止；不提交、不回滚，手工操作随即恢复；取消后旧脚本的
任何推进 / 跳过 / 取消都返回 `409`）：

```bash
curl -s -X POST $BASE/script/cancel -H 'Content-Type: application/json' \
  -d "{\"script_id\":\"$SID\",\"step\":3}"
# {"ok":true,"script":{"id":"...","status":"cancelled","step":3,"total":7,...}}
```

脚本有效期间手工操作 / 只读切换被锁定，查询 / 检查点 / 删除照常：

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/conn/a/op \
  -H 'Content-Type: application/json' -d '{"op":"begin"}'
# 409
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/conn/b/readonly \
  -H 'Content-Type: application/json' -d '{"enabled":true}'
# 409
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/checkpoint/PASSIVE
# 200（检查点不被脚本锁定）
```

> 过期请求：用旧脚本 ID（或已被新脚本替换后的旧 ID）、错误步号，或对已完成 /
> 已取消脚本发起推进 / 跳过 / 取消，都返回 **409**，不执行、不产生记录、不影响
> 新脚本。非法请求体（缺少字符串 `script_id` 或整数 `step`）返回 **400**。
> 装载时若任一栏仍有未提交事务（如上个脚本取消时留下的事务），先手工
> `提交` / `回滚` 再装载，否则返回 **409**。

### 示例八：补偿末条提交（已提交的加法不能用 ROLLBACK 撤回）

> 对时间线**末条普通提交**执行补偿：系统用一条**独立连接**真实提交，把值恢复
> 为该记录的 `before`，并追加一条 `side=system` 的补偿记录（`before`/`after`
> 对调、增量逆序取负、`compensates` 记原 seq）；原记录保留。过期序号、补偿
> 记录或脚本有效期间返回 `409`；两栏持写锁时立即返回 `SQLITE_BUSY`，失败不改
> 库也不改时间线；两栏事务、只读模式、最近显式读值与读快照都不受影响。

1. 甲：`BEGIN DEFERRED` → 输入 `5` → `加上` → 输入 `7` → `加上` → `提交`
   （已提交值 **112**，时间线 **#1 甲（A） 100 → 112 增量 [5, 7]**）
2. 乙：`BEGIN DEFERRED` → `读值` → 显示 **112**（乙的读快照建立，先不结束事务）
3. 在时间线区块的序号框输入 `1` → 点击 **补偿该条提交**：
   已提交值变为 **100**，时间线追加 **#2 系统 112 → 100 增量 [-7, -5] 补偿 #1**；
   #1 原样保留
4. 乙：再点 `读值` → 仍为 **112**（已有读快照仍见旧值；乙的事务、两栏只读模式
   与最近显式读值都不受补偿影响）→ `回滚`
5. 再对 #2 或 #1 补偿都返回 **409**（补偿记录不可再补偿，#1 已不是末条）；
   甲重新提交一笔后，只能补偿新的末条

#### HTTP 调用示例

```bash
EXP=<新建实验返回的 id>
BASE=http://localhost:8000/api/experiments/$EXP

# 甲：事务内 +5、+7 后提交（时间线 #1：100 → 112，增量 [5, 7]）
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"begin"}'
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"add","value":5}'
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"add","value":7}'
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"commit"}'

# 补偿末条普通提交 #1：值恢复为 100，追加 #2（system，112→100，[-7,-5]）
curl -s -X POST $BASE/timeline/compensate \
  -H 'Content-Type: application/json' -d '{"seq":1}'
# {"ok":true,"error":null,
#  "record":{"seq":2,"side":"system","before":112,"after":100,
#            "deltas":[-7,-5],"compensates":1},
#  "timeline":[
#    {"seq":1,"side":"a","before":100,"after":112,"deltas":[5,7]},
#    {"seq":2,"side":"system","before":112,"after":100,
#     "deltas":[-7,-5],"compensates":1}]}
curl -s $BASE/committed                     # {"committed":100}

# 补偿记录不可再补偿；#1 已成为过期目标：均 409
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/timeline/compensate \
  -H 'Content-Type: application/json' -d '{"seq":2}'   # 409
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/timeline/compensate \
  -H 'Content-Type: application/json' -d '{"seq":1}'   # 409

# 非法 seq（非正整数、字符串、布尔、缺字段）：400
curl -s -o /dev/null -w "%{http_code}\n" -X POST $BASE/timeline/compensate \
  -H 'Content-Type: application/json' -d '{"seq":0}'   # 400

# 乙提交一笔新的末条（时间线 #3：100 → 103，增量 [3]）
curl -s -X POST $BASE/conn/b/op -H 'Content-Type: application/json' \
  -d '{"op":"begin"}'
curl -s -X POST $BASE/conn/b/op -H 'Content-Type: application/json' \
  -d '{"op":"add","value":3}'
curl -s -X POST $BASE/conn/b/op -H 'Content-Type: application/json' \
  -d '{"op":"commit"}'

# 两栏持写锁时补偿：200 + ok:false，如实返回 SQLITE_BUSY(5)，库与时间线不变
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"begin"}'
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"add","value":9}'        # 甲持写锁（未提交）
curl -s -X POST $BASE/timeline/compensate \
  -H 'Content-Type: application/json' -d '{"seq":3}'
# {"ok":false,"error":{"name":"SQLITE_BUSY","code":5,
#   "message":"database is locked"},"timeline":[...]}   甲未提交，写锁被占
curl -s -X POST $BASE/conn/a/op -H 'Content-Type: application/json' \
  -d '{"op":"rollback"}'             # 甲释放写锁
curl -s -X POST $BASE/timeline/compensate \
  -H 'Content-Type: application/json' -d '{"seq":3}'   # 成功：值恢复 100
curl -s $BASE/committed                     # {"committed":100}
```

> 补偿与普通提交并发交错时，后端在同一临界区重新核对目标：同一 seq 的并发
> 补偿最多成功一次，后到者收到 `409`；脚本有效期间补偿同样返回 `409`。
> 删除中的实验返回 `410`，未知实验返回 `404`（与其他接口一致）。


> 提示：一栏的请求未完成时，该栏按钮会被禁用，另一栏仍可正常操作；
> 两栏的状态轮询与「已提交值」均通过独立连接 / 内存状态完成，
> 不会干扰甲、乙两栏各自的事务快照。
