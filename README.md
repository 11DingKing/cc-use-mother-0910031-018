# 供应商交付承诺

采购计划下达后，多家供应商会对同一物料分批确认数量和日期。本服务把每一次
供应商回复登记为**不可变承诺版本**，重放版本链即可随时算出每批需求的承诺
覆盖、缺口与变更责任，解决"邮件更新覆盖旧承诺、短缺无法归责"的问题。

## 核心不变量

对应 `domain/contract.json` 中的四项约束：

| 契约约束 | 实现方式 |
| --- | --- |
| 承诺版本链 | 每个版本 `(line_id, seq)` 顺序编号，`chain_hash = sha256(prev_hash ‖ 业务载荷+冻结状态)`，新版本的 `references` 指回被改期/拒绝/转单的前序版本；可 `GET .../verify` 重放校验 |
| 累计数量约束 | 接受确认时同时受**采购行剩余需求**与**供应商产能窗口可用量**双重约束，超量确认和产能超出部分形成确定性的"部分接受" |
| 并发回复幂等 | 所有写入在单把写锁 + `BEGIN IMMEDIATE` 事务内串行；消息台账以 `message_id` 为主键并记录请求体哈希，重复邮件返回首次结果，**不会生成第二份承诺**；同号不同内容返回 409 |
| 缺口责任追溯 | 缺口以 FIFO 段记录责任（供应商、确认人、消息号、原因）；拒绝、改期、转单的每一次变更都留在承诺 `history` 上，按期缺口可追到"是谁在哪封邮件里把日期推过了到期日" |

### 为什么快照真正不可变

- `commitment_snapshots` 表上有 `BEFORE UPDATE` / `BEFORE DELETE` 触发器，
  数据库直接拒绝任何改写或删除；
- 拒绝/改期/转单**不更新旧行**，而是插入新版本并通过 `references` 关联旧版本；
- "当前生效承诺"不是某张可变表，而是重放全部版本（`commitment/engine.py`）
  导出的，每个版本还冻结了当次重放状态（`state_json`），可验证状态可重现。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/commitment/`：承诺服务实现。
  - `models.py`：状态/动作/责任常量与数据模型。
  - `storage.py`：SQLite 存储、触发器、哈希链、消息台账、并发写锁。
  - `engine.py`：版本链重放引擎（FIFO 覆盖、缺口责任、按期归因投影）。
  - `service.py`：登记与消息处理、累计数量/产能校验、确定性部分接受。
  - `httpapi.py`：标准库 HTTP 接口（无第三方依赖）。
  - `app.py` / `__main__.py`：装配与启动入口。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、领域行为（含并发/不可变/归责场景）、HTTP 端到端测试。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/procurement-lines` | 登记采购行（物料、数量、到期日） |
| POST | `/api/procurement-lines/{id}/release` | 下达（下达后才受理回复） |
| POST | `/api/procurement-lines/{id}/close` | 关闭 |
| POST | `/api/capacity-windows` | 登记供应商产能窗口（供应商/物料/日期/产能） |
| GET  | `/api/capacity-windows` | 窗口产能与已占用量 |
| POST | `/api/messages` | 供应商回复（确认/拒绝/改期/转单），幂等入口 |
| GET  | `/api/procurement-lines` | 全部采购行的覆盖视图 |
| GET  | `/api/procurement-lines/{id}` | **覆盖、缺口、责任视图** |
| GET  | `/api/procurement-lines/{id}/versions` | 承诺版本链与引用关系 |
| GET  | `/api/procurement-lines/{id}/verify` | 哈希链完整性 + 状态可重现性 |
| GET  | `/healthz` | 健康检查 |

回复消息体示例：

```json
{
  "message_id": "<mail-123@supplier>",
  "sender": "sales@huadong.example",
  "action": "改期",
  "line_id": "PO-001",
  "supplier": "华东钢厂",
  "quantity": 200,
  "promised_date": "2026-12-15",
  "confirmed_by": "华东-周敏",
  "reason": "高炉检修"
}
```

`action` 取值：`确认`、`拒绝`（部分/全部取消已承诺量）、`改期`（同供应商换日期）、
`转单`（需附 `transferred_to`）。`message_id`/`sender` 也可通过请求头
`X-Message-Id` / `X-Sender` 传递。

覆盖视图关键字段：`required`、`committed`、`gap`、`on_time_gap`、
`buckets`（按承诺日期的覆盖桶及累计）、`gap_responsibility`（总量缺口是谁造成的）、
`schedule_responsibility`（迟交是谁的哪次改期/转单造成的）、
`active_commitments`（含每段承诺的完整变更 `history`）。

## 运行

```bash
# 测试
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约摘要
python3 tools/check_contract.py domain/contract.json

# 启动 HTTP 服务（零第三方依赖）
PYTHONPATH=src COMMITMENT_PORT=8080 python3 -m commitment
# 数据库默认 data/commitment.sqlite3，可用 COMMITMENT_DB 覆盖；:memory: 为内存库
```

## 确定性语义说明

- **部分拒绝/改期/转单**：超出该供应商当前生效承诺量的部分进入 `ignored_quantity`，
  不影响其他供应商。
- **超量确认**：超过采购行剩余需求的部分被拒（`kind=超量确认`）；即使需求未满，
  超过当日产能窗口的部分同样被拒（`kind=产能不足`）。
- **FIFO**：拒绝/改期/转单总是从该供应商最早的承诺开始切；新确认总是优先填补
  最早的未覆盖需求，因此旧缺口被填补时其责任同步消除。
- **并发**：同一采购行的并发回复按获取写锁的先后定序，seq 单调，结果唯一确定；
  重复消息无论并发多少份，只产生一个版本。
