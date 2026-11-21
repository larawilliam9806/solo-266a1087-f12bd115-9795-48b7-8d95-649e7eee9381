# 住户出行调查配权 API

基于 **FastAPI + SQLite + 原生 HiGHS 线性规划引擎** 的应答户权重校准服务，
支持 `docker compose` 一键启动。

## 业务规则

- 两份源表均可**整份换交**（历史版本保留，当前版本由指针切换）：
  - **应答名单**：唯一户号 `household_id`、区县 `district`、家庭人数档
    `size_band`、正数初始权重 `init_weight`；
  - **总体目标表**：两个维度（`district` / `size_band`）下各组的目标户数
    `target_count`。
- 配权时每户权重限制在初始权重的 **0.5～2 倍**。
- 两阶段字典序优化（均为线性规划）：
  1. **先**最小化两个维度所有组「加权户数 − 目标户数」的**绝对差之和**；
  2. **再**在保持第一阶段最优的前提下，最小化各户权重相对初始权重的
     **绝对改动之和**。
- 结果分两种：
  - **能配平**（绝对差之和为 0）：下游可领取**逐户权重**；
  - **不能配平**：不提供逐户权重，只给各组
    `difference = 加权户数 − 目标户数` 的差额，供调查员排查缺样。
- 异常均带原因：缺表、缺分组目标、求解失败分别返回明确错误码与说明。
- **配权期间允许换交**：换交失败（校验不过）保留原表与既有供数；
  换交成功后旧版结果立即停供，状态回到「待配权」，进行中的旧作业完成时
  其结果因版本过时而丢弃。
- **CSV 表头防错**：列名先去掉首尾空白再比对；任一必需列重复出现
  （如两列 `district`）即整表拒绝、错误信息点名重复列，当前源表与供数
  保持不变，避免后一列悄悄覆盖并按新内容切换版本。
- **旧库当前源表须核对**：由旧版本数据库升级而来的当前两表，需数据员按
  「表名 + 版号」提交核实后的 CSV 或 JSON，与入库记录逐项一致才记为已核；
  核对前该表不能用于配权，也不对外供数，状态接口列出待核表。
  - 比对按记录集合进行，**行序可以不同**；不一致时返回具体原因
    （缺记录 / 多记录 / 字段值不一致）并保留原核对状态。
  - 两张表可分别核对，核对期间另一张表可随时换交、无需等待；
    若被核对的表已换版，则本次核对被拒绝。
  - 另一张表换交或核对通过，不影响本表已生效的核对。
  - 修复后重新换交成功的版本直接可用，无需再核对；
    两表都核对通过后，原有且版号对应的结果按原作业号恢复供数。
- 两表尚未齐备或当前版本尚未算完时返回「待配权 / 计算中」；
  所有结果查询都**附带两表版号**。

## 启动

```bash
docker compose up -d --build
```

- 服务：`http://localhost:8000`
- 交互式文档（Swagger）：`http://localhost:8000/docs`
- 健康检查：`GET /health`
- SQLite 数据持久化在命名卷 `weighting-data`（容器内 `/data/weighting.db`）。

本地开发（Python 3.11）：

```bash
pip install -r requirements.txt
WEIGHTING_DB_PATH=./weighting.db uvicorn app.main:app --reload
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/tables/respondents` | 整份换交应答名单（JSON） |
| POST | `/tables/respondents/csv` | 整份换交应答名单（CSV 上传） |
| POST | `/tables/targets` | 整份换交总体目标表（JSON） |
| POST | `/tables/targets/csv` | 整份换交总体目标表（CSV 上传） |
| GET | `/tables/versions` | 查看两表当前版号与内容哈希 |
| GET | `/tables/verifications` | 查看两表核对状态与待核清单 |
| POST | `/tables/verify` | 按请求体中的表名与版号核对（JSON/CSV） |
| POST | `/tables/{table}/verify` | 按路径表名核对（JSON `records` 或 CSV 上传） |
| POST | `/tables/{table}/verify/csv` | 同上，CSV 上传专用别名 |
| POST | `/weighting/jobs` | 对当前两表版本发起配权（后台异步） |
| GET | `/weighting/status` | 查询状态（缺表/待核对/待配权/计算中/已配平/无法配平/失败） |
| GET | `/weighting/weights` | 配平后领取逐户权重（支持 `limit`/`offset`） |
| GET | `/weighting/differences` | 领取各组加权户数减目标户数的差额 |

JSON 请求体形如：

```json
{ "records": [
  {"household_id": "H0001", "district": "Dongcheng",
   "size_band": "S1", "init_weight": 1.0}
]}
```

```json
{ "records": [
  {"dimension": "district", "group_name": "Dongcheng", "target_count": 6.0},
  {"dimension": "size_band", "group_name": "S1", "target_count": 10.0}
]}
```

CSV 表头见 `examples/`：

```bash
curl -X POST http://localhost:8000/tables/respondents/csv \
  -F "file=@examples/respondents.csv"
curl -X POST http://localhost:8000/tables/targets/csv \
  -F "file=@examples/targets.csv"
```

## 旧库核对

旧版本数据库升级后，当前两表先进入「待核对」，此时配权与结果领取都会返回
`409 unverified_table`。状态接口会列出待核表：

```bash
curl localhost:8000/weighting/status
# {"status": "unverified", "pending_verification": [
#   {"table": "respondents", "label": "应答名单", "version": 1}, ...]}
```

数据员按表名与版号提交核实后的数据（CSV 或 JSON 均可，行序不限）：

```bash
# CSV（版号可放在查询串或表单里）
curl -X POST "localhost:8000/tables/respondents/verify/csv?version=1" \
  -F "file=@examples/respondents.csv"
curl -X POST localhost:8000/tables/targets/verify/csv \
  -F "version=1" -F "file=@examples/targets.csv"

# JSON
curl -X POST "localhost:8000/tables/targets/verify?version=1" \
  -H "Content-Type: application/json" \
  -d '{"records": [{"dimension": "district", "group_name": "Dongcheng",
                     "target_count": 6.0}]}'
```

- 与入库记录逐项一致才返回 `200`，并在响应里标明另一张表是否仍需核对；
- 不一致返回 `409 verification_mismatch`，`details` 说明是缺记录、多记录还是
  字段值不一致，原核对状态保持不变；
- 被核对的版号已不是当前版本时返回 `409 version_superseded`；
- 两表均核对通过后，若当前版号对已有完成的结果，直接按**原作业号**恢复供数。

## 典型流程

```bash
# 1. 换交两表（返回各自版号）
curl -X POST localhost:8000/tables/respondents/csv -F "file=@examples/respondents.csv"
curl -X POST localhost:8000/tables/targets/csv     -F "file=@examples/targets.csv"

# 2. 发起配权
curl -X POST localhost:8000/weighting/jobs

# 3. 轮询状态
curl localhost:8000/weighting/status

# 4a. 已配平 -> 逐户权重（响应含 respondent_version / target_version）
curl localhost:8000/weighting/weights

# 4b. 无法配平 -> 权重接口返回 409，改领差额
curl localhost:8000/weighting/differences
```

## 状态机与错误码

| HTTP / error 码 | 触发场景 |
| --- | --- |
| `409 missing_table` | 两表未齐备就发起配权或查询 |
| `409 unverified_table` | 当前源表尚未核对，配权或领取结果被拒 |
| `409 verification_mismatch` | 核对提交的数据与入库记录不一致（附具体原因） |
| `409 version_superseded` | 被核对的表已换版，旧版核对被拒 |
| `404 version_not_found` | 提交核对的版号不存在 |
| `409 missing_group_targets` | 名单中出现的区县/人数档在目标表中没有对应目标 |
| `409 weighting_in_progress` | 当前版本已有配权作业在跑，重复发起 |
| `409 not_ready` | 当前版本尚未算完（待配权/计算中），结果暂不可领 |
| `409 result_unavailable` | 无法配平时请求逐户权重，或作业计算失败 |
| `400 invalid_payload` | 换交数据校验失败（空表、重复户号/分组、重复 CSV 列、权重非正、目标为负等） |

换交校验在写入前完成，失败时数据库指针不变，旧表与旧结果继续供数；
换交成功后版号前进，旧版权重/差额立即停供。新换交的版本视为已核、可直接使用；
只有旧库遗留的当前版本才需要数据员核对。

## 设计要点

- **不可变版本快照 + 当前版本指针**：每次换交写入新快照并在事务内翻转指针，
  历史版本保留可审计，失败回滚不影响在线数据。
- **核对状态跟随版本**：`verified/verified_at` 记录在版本快照上，
  换交产生的新版本默认为已核；核对只作用于 `(表, 版号)`，因此两表可独立核对，
  且核对过程不持有服务锁，另一张表的换交无需等待。
- **结果与版本对绑定**：权重/差额按 `(respondent_version, target_version)`
  存储；后台作业落库前再次校验版本对仍为当前版本，换交导致的过期结果直接丢弃。
  核对放行后按同一版本对查询原作业，因此既有结果按原作业号恢复。
- **两阶段 LP（原生 HiGHS）**：用非负松弛对把绝对值线性化——
  组约束 `Σw − d⁺ + d⁻ = target`（差额即 `d⁺ − d⁻`），
  户改动用 `w − v ≤ init`、`−w − v ≤ −init`；
  第二阶段增加 `Σ(d⁺+d⁻) ≤ 第一阶段最优 + 容差` 锁定第一目标。
- 3000 户规模端到端求解约 0.1 秒。

## 测试

```bash
pip install pytest httpx
pytest -q
```

覆盖：配平/不可配平两条结果路径、0.5～2 倍权重边界、两阶段最优性、
缺表与缺分组目标、换交失败保留供数、换交成功停供旧结果、
配权进行中换交的作业过期处理、CSV 上传与分页，以及 CSV 重复必需列拒绝、
旧库两表独立核对、核对不一致原因与原状态保留、已换版拒绝核对、
核对期间另一表换交、核对后原作业恢复供数等共 32 个用例。
