# 供应商交付承诺服务

针对多供应商、多批次、多版本邮件确认的采购承诺管理服务。解决：

- 邮件更新覆盖旧承诺后，生产计划无法判断某次短缺由谁造成；
- 多家供应商对同一物料分批确认数量和日期，需要校验累计数量；
- 重复邮件 / 并发回复必须产生确定且唯一的结果；
- 部分拒绝、改期、转单、超量确认都要留痕、可追溯。

## 核心设计

| 机制 | 说明 |
| --- | --- |
| 采购行 + 批次 | 一个采购行对应一种物料，拆成多批需求（批次号、需求日期、数量）。 |
| 产能窗口 | 供应商按物料登记可供应日期区间与区间产能；承诺日期必须落在窗口内，窗口承载量不得超产能。 |
| 承诺版本链 | 供应商每封邮件生成一个版本；同一供应商新版本原子取代其旧版本（旧版本置 `superseded`，**永不删除**）。 |
| 不可变快照 | 每个版本落账时冻结规范 JSON 并计算 SHA-256 `snapshot_hash`，快照内含落账瞬间各批次需求/承诺/缺口/超量状态。 |
| 累计数量约束 | 按批次汇总所有供应商的**有效版本**承诺量；`strict` 模式下超量整封拒绝（409，返回可接受上限），`allow_over` 模式登记超量并标记。 |
| 幂等 | 幂等键为 `(line_id, message_id)`；重复邮件直接返回首份承诺并标记 `idempotent_replay`。所有写操作在同一把锁内完成校验→落账，8 路并发同一邮件只产生 1 个版本。 |
| 部分拒绝 | 同封邮件可带 `rejections`，承诺+拒绝不得超过批次需求量；缺口责任优先归因为拒绝方。 |
| 改期 | 对已持有承诺生成新版本，数量缺省沿用，逐条记录 `rescheduled` 变更；晚于需求日期标记为逾期。 |
| 转单 | 同一把锁内原子生成转出 / 转入两条版本，互相 `related_version_id` 关联，转出量不得超过持有量，转单守恒（总量不变）。 |
| 责任归因 | 按版本顺序回放：区分 `no_response`（无人响应）、`partial_rejection`（部分拒绝）、`coverage_broken`（曾足额后被下调/转出打破）、`never_fully_covered`，并记录供应商、确认人、邮件号、版本、时间、原因。 |

版本 ID 按采购行单调递增（`PO-x-V001`…），转单成对版本也不撞号。

## 目录

- `src/commitment_service/models.py`：领域模型（采购行、需求批次、产能窗口、承诺条目、拒绝、逐条变更、版本快照）。
- `src/commitment_service/service.py`：核心业务服务（登记、校验、版本链、改期、转单、幂等、归因）。
- `src/commitment_service/http_api.py`：基于 `http.server` 的零三方依赖 JSON HTTP 服务。
- `src/commitment_service/errors.py`：稳定错误码与 HTTP 状态映射。
- `src/domain_contract/`：领域契约读取与确定性校验（原有）。
- `domain/contract.json`：领域角色、状态、约束和样例。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/test_commitment_service.py`：核心规则与并发回归（20 例）。
- `tests/test_http_api.py`：HTTP 端到端回归。

## 启动

```bash
PYTHONPATH=src PORT=8080 python3 -m commitment_service.http_api
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/purchase-lines` | 登记采购行（含分批需求） |
| POST | `/purchase-lines/{id}/issue` | 下达采购计划（之后才能接收承诺） |
| POST | `/purchase-lines/{id}/close` | 关闭采购行 |
| POST | `/capacity-windows` | 登记供应商产能窗口 |
| POST | `/commitments` | 供应商确认 / 部分拒绝 / 超量确认（`mode=allow_over`） |
| POST | `/commitments/reschedule` | 改期（数量缺省沿用当前承诺） |
| POST | `/commitments/transfer` | 转单（原子返回 `transfer_out` + `transfer_in`） |
| GET | `/commitments/message/{message_id}?line_id=` | 按邮件号反查承诺（跨采购行同号时需指定 line_id） |
| GET | `/purchase-lines/{id}` | 每批需求的承诺覆盖、缺口、逾期、责任视图 |
| GET | `/purchase-lines/{id}/versions` | 承诺版本链 |
| GET | `/versions/{version_id}` | 单版本不可变快照 |
| GET | `/health` | 健康检查 |

### 承诺报文示例

```json
{
  "line_id": "PO-DEMO",
  "supplier_id": "S1",
  "message_id": "MAIL-1",
  "confirmer": "张三",
  "items": [{"batch_id": "W40", "promised_date": "2026-10-04", "qty": 60}],
  "rejections": [{"batch_id": "W40", "qty": 40, "reason": "原料短缺"}]
}
```

### 视图摘录（每批需求）

```json
{
  "batch_id": "W40", "qty": 100, "committed_qty": 90,
  "gap_qty": 10, "over_qty": 0, "coverage_rate": 0.9,
  "coverage": [
    {"supplier_id": "S2", "qty": 30, "promised_date": "2026-10-05", "late": false, "confirmer": "王五"}
  ],
  "responsibility": {
    "ever_fully_covered": false,
    "attribution": [{"cause": "partial_rejection", "supplier_id": "S1", "confirmer": "张三", "reason": "原料短缺"}],
    "late_commitments": [{"supplier_id": "S1", "promised_date": "2026-10-09", "qty": 60}]
  }
}
```

## 错误码

`NOT_FOUND` 404、`LINE_NOT_ISSUED`/`OVER_COMMITTED`/`NO_ACTIVE_COMMITMENT`/`TRANSFER_EXCEEDS_HELD` 409、
`CAPACITY_VIOLATION`/`SUPPLIER_NOT_REGISTERED` 422，以及 `INVALID_*` 400。
超量拒绝响应的 `details.over` 给出每批次需求量、预计累计量、超出量与**该供应商可接受上限**，供供应商更正。

## 验证

```bash
python3 -m unittest discover -s tests -v          # 20 例核心+并发 + HTTP 端到端
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```

并发保证已用测试固化：同一 `message_id` 8 线程同时提交只生成 1 个版本；两家供应商同时超量回复（各 60/100）重复 20 轮，每轮恰好 1 封成功、1 封 `OVER_COMMITTED`。
