"""承诺版本链重放引擎。

所有"当前生效承诺"都不是某张可更新的表里读出来的，而是把该采购行的
不可变快照按 ``seq`` 重放一遍导出。规则对任意输入消息都是确定的：

- 确认：新承诺按 FIFO 覆盖最早未覆盖需求；接受量同时受剩余需求与
  供应商产能窗口限制（超出部分形成确定性的部分接受）。
- 拒绝：从该供应商自己的生效承诺 FIFO 取消，取消出来的缺口盖上
  责任人（供应商、确认人、消息、原因）。
- 改期/转单：不产生总量缺口，只移动承诺的日期/供应商，并在承诺上
  留下变更历史；跨到期日的改期在按期缺口里归因给操作人。
- 新确认优先填补最早的未覆盖需求（包括此前被拒出的缺口），缺口
  责任随缺口被填补而消除。
"""
from __future__ import annotations

from typing import Any

from .models import (
    ACTION_CONFIRM,
    ACTION_REJECT,
    ACTION_RESCHEDULE,
    ACTION_TRANSFER,
    RESP_LATE,
    RESP_OPEN,
    RESP_RESCHEDULE,
    RESP_SHORTFALL,
    RESP_TRANSFER,
)


def event_from_row(row: Any) -> dict[str, Any]:
    """把快照行转成重放事件。"""
    return {
        "commitment_id": row["commitment_id"],
        "supplier": row["supplier"],
        "promised_date": row["promised_date"],
        "quantity": row["quantity"],
        "action": row["action"],
        "seq": row["seq"],
        "confirmed_by": row["confirmed_by"],
        "message_id": row["message_id"],
        "reason": row["reason"],
        "transferred_to": row["transferred_to"],
    }


def initial_state(requirement: int) -> dict[str, Any]:
    """初始状态：整批需求无人确认。"""
    return {
        "requirement": requirement,
        # 未覆盖需求段，按产生时间排序；confirm 永远从队首填补。
        "open_segments": [
            {"qty": requirement, "opened_seq": -1,
             "blame": {"type": RESP_OPEN, "supplier": None, "confirmed_by": None,
                       "message_id": None, "reason": None}},
        ],
        # 供应商 -> 生效承诺 FIFO（每段一次确认/转入）。
        "coverings": {},
        "events": [],
    }


def _open_qty(state: dict[str, Any]) -> int:
    return sum(seg["qty"] for seg in state["open_segments"])


def _cover(state: dict[str, Any], supplier: str, date: str, qty: int,
           by: str, msg: str, seq: int, history: list[dict[str, Any]] | None = None,
           origin: str | None = None) -> None:
    """把 qty 数量的承诺压到供应商 FIFO 尾部，并从队首填补未覆盖需求。"""
    if qty <= 0:
        return
    remaining = qty
    while remaining > 0:
        seg = state["open_segments"][0]
        take = min(seg["qty"], remaining)
        seg["qty"] -= take
        remaining -= take
        if seg["qty"] == 0:
            state["open_segments"].pop(0)
    state["coverings"].setdefault(supplier, []).append({
        "supplier": supplier,
        "date": date,
        "qty": qty,
        "confirmed_by": by,
        "message_id": msg,
        "seq": seq,
        "origin": origin or msg,
        "history": history or [],
    })


def _place_covering(state: dict[str, Any], supplier: str, date: str, qty: int,
                    by: str, msg: str, seq: int, history: list[dict[str, Any]],
                    origin: str) -> None:
    """把一段已覆盖数量放回某供应商 FIFO（改期/转单使用）。

    与 :func:`_cover` 不同：它不消费未覆盖需求，因为移动前后总覆盖量不变。
    """
    state["coverings"].setdefault(supplier, []).append({
        "supplier": supplier,
        "date": date,
        "qty": qty,
        "confirmed_by": by,
        "message_id": msg,
        "seq": seq,
        "origin": origin,
        "history": history,
    })


def _consume_supplier(state: dict[str, Any], supplier: str, qty: int
                      ) -> tuple[list[dict[str, Any]], int]:
    """从供应商 FIFO 队首切出 qty，返回切片段与实际消耗量。"""
    queue = state["coverings"].get(supplier, [])
    portions = peek_supplier(state, supplier, qty)
    consumed = sum(p["qty"] for p in portions)
    # 按预演结果真正出队
    need = consumed
    while need > 0:
        head = queue[0]
        if head["qty"] <= need:
            need -= head["qty"]
            queue.pop(0)
        else:
            head["qty"] -= need
            need = 0
    if not queue and supplier in state["coverings"]:
        del state["coverings"][supplier]
    return portions, consumed


def peek_supplier(state: dict[str, Any], supplier: str, qty: int
                  ) -> list[dict[str, Any]]:
    """纯函数预演：返回若从该供应商 FIFO 队首切 qty 会切到哪些段。

    不修改 state。供服务层做容量可行性预演，保证"先算清楚再落版本"。
    """
    queue = state["coverings"].get(supplier, [])
    portions: list[dict[str, Any]] = []
    need = qty
    for head in queue:
        if need <= 0:
            break
        take = min(head["qty"], need)
        portions.append({
            "supplier": head["supplier"], "date": head["date"], "qty": take,
            "confirmed_by": head["confirmed_by"], "message_id": head["message_id"],
            "seq": head["seq"], "origin": head["origin"],
            "history": list(head["history"]),
        })
        need -= take
    return portions


def apply(state: dict[str, Any], ev: dict[str, Any]) -> dict[str, Any]:
    """在 state 上应用一个事件，返回 state（原地变更）。"""
    seq = ev["seq"]
    supplier = ev["supplier"]
    date = ev["promised_date"]
    qty = ev["quantity"]
    record = {
        "seq": seq,
        "commitment_id": ev["commitment_id"],
        "action": ev["action"],
        "supplier": supplier,
        "quantity": qty,
        "confirmed_by": ev["confirmed_by"],
        "message_id": ev["message_id"],
        "reason": ev["reason"],
    }

    if ev["action"] == ACTION_CONFIRM:
        _cover(state, supplier, date, qty, ev["confirmed_by"], ev["message_id"], seq)
        record["promised_date"] = date

    elif ev["action"] == ACTION_REJECT:
        portions, consumed = _consume_supplier(state, supplier, qty)
        for p in portions:
            state["open_segments"].append({
                "qty": p["qty"], "opened_seq": seq,
                "blame": {"type": RESP_SHORTFALL, "supplier": supplier,
                          "confirmed_by": ev["confirmed_by"],
                          "message_id": ev["message_id"], "reason": ev["reason"]},
            })
        record["rejected_qty"] = consumed

    elif ev["action"] == ACTION_RESCHEDULE:
        portions, consumed = _consume_supplier(state, supplier, qty)
        for p in portions:
            history = p["history"] + [{
                "seq": seq, "action": ACTION_RESCHEDULE,
                "from_date": p["date"], "to_date": date,
                "by": ev["confirmed_by"], "message_id": ev["message_id"],
            }]
            _place_covering(state, supplier, date, p["qty"], ev["confirmed_by"],
                            ev["message_id"], seq, history=history, origin=p["origin"])
        record["moved_qty"] = consumed
        record["to_date"] = date

    elif ev["action"] == ACTION_TRANSFER:
        target = ev["transferred_to"]
        portions, consumed = _consume_supplier(state, supplier, qty)
        for p in portions:
            history = p["history"] + [{
                "seq": seq, "action": ACTION_TRANSFER,
                "from_supplier": supplier, "to_supplier": target,
                "from_date": p["date"], "to_date": date,
                "by": ev["confirmed_by"], "message_id": ev["message_id"],
            }]
            _place_covering(state, target, date, p["qty"], ev["confirmed_by"],
                            ev["message_id"], seq, history=history, origin=p["origin"])
        record["moved_qty"] = consumed
        record["target_supplier"] = target
        record["to_date"] = date
    else:
        raise ValueError(f"未知动作：{ev['action']}")

    state["events"].append(record)
    return state


def replay(rows: list[Any], requirement: int) -> dict[str, Any]:
    """从快照行重放出当前状态。"""
    state = initial_state(requirement)
    for row in sorted(rows, key=lambda r: r["seq"]):
        apply(state, event_from_row(row))
    return state


# ---- 只读投影：覆盖桶与责任 ------------------------------------------------

def active_coverings(state: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for queue in state["coverings"].values():
        out.extend(queue)
    return sorted(out, key=lambda c: (c["date"], c["seq"], c["supplier"]))


def coverage_view(state: dict[str, Any], due_date: str) -> dict[str, Any]:
    """生成接口返回的覆盖/缺口/责任视图。"""
    requirement = state["requirement"]
    coverings = active_coverings(state)
    committed = sum(c["qty"] for c in coverings)
    gap = requirement - committed

    # 按承诺日期聚合
    by_date: dict[str, int] = {}
    by_date_supplier: dict[str, dict[str, int]] = {}
    for c in coverings:
        by_date[c["date"]] = by_date.get(c["date"], 0) + c["qty"]
        by_date_supplier.setdefault(c["date"], {})
        by_date_supplier[c["date"]][c["supplier"]] = (
            by_date_supplier[c["date"]].get(c["supplier"], 0) + c["qty"])

    buckets = []
    cum = 0
    for d in sorted(by_date):
        cum += by_date[d]
        buckets.append({
            "promised_date": d,
            "committed": by_date[d],
            "by_supplier": dict(sorted(by_date_supplier[d].items())),
            "cumulative_committed": cum,
            "required": requirement,
            "gap_at_date": max(0, requirement - cum),
            "on_time": d <= due_date,
        })

    # 总缺口责任：未覆盖段按 (类型, 供应商, 确认人, 消息, 原因) 聚合
    gap_resp: dict[tuple, int] = {}
    for seg in state["open_segments"]:
        b = seg["blame"]
        key = (b["type"], b["supplier"], b["confirmed_by"], b["message_id"], b["reason"])
        gap_resp[key] = gap_resp.get(key, 0) + seg["qty"]
    gap_responsibility = [
        {"type": k[0], "supplier": k[1], "confirmed_by": k[2],
         "message_id": k[3], "reason": k[4], "quantity": v}
        for k, v in sorted(gap_resp.items(), key=lambda kv: (-kv[1], str(kv[0])))
    ]

    # 按期缺口责任：承诺日晚于到期日的覆盖量，按最近一次跨期变更归因
    on_time_committed = sum(c["qty"] for c in coverings if c["date"] <= due_date)
    on_time_gap = requirement - on_time_committed
    late_resp: dict[tuple, int] = {}
    for c in coverings:
        if c["date"] <= due_date:
            continue
        blame_type = RESP_LATE
        blame_by = c["confirmed_by"]
        blame_msg = c["message_id"]
        # 找到把日期推过到期日的最近一次变更（改期/转单）
        for h in reversed(c["history"]):
            if h.get("to_date", "9999-99-99") > due_date >= h.get("from_date", "0000-00-00"):
                blame_type = (RESP_RESCHEDULE if h["action"] == ACTION_RESCHEDULE
                              else RESP_TRANSFER)
                blame_by = h["by"]
                blame_msg = h["message_id"]
                break
        key = (blame_type, c["supplier"], blame_by, blame_msg)
        late_resp[key] = late_resp.get(key, 0) + c["qty"]
    late_responsibility = [
        {"type": k[0], "supplier": k[1], "confirmed_by": k[2],
         "message_id": k[3], "quantity": v}
        for k, v in sorted(late_resp.items(), key=lambda kv: (-kv[1], str(kv[0])))
    ]

    return {
        "required": requirement,
        "committed": committed,
        "gap": gap,
        "covered": gap == 0,
        "due_date": due_date,
        "on_time_committed": on_time_committed,
        "on_time_gap": max(0, on_time_gap),
        "buckets": buckets,
        "gap_responsibility": gap_responsibility,
        "schedule_responsibility": late_responsibility,
        "change_history": state["events"],
        "active_commitments": [
            {"supplier": c["supplier"], "promised_date": c["date"],
             "quantity": c["qty"], "confirmed_by": c["confirmed_by"],
             "message_id": c["message_id"], "history": c["history"]}
            for c in coverings
        ],
    }


def freeze(state: dict[str, Any]) -> dict[str, Any]:
    """把重放状态转成可哈希/落盘的冻结 JSON。"""
    return {
        "requirement": state["requirement"],
        "open_segments": [dict(seg) for seg in state["open_segments"]],
        "coverings": [
            dict(c) for queue in state["coverings"].values() for c in queue
        ],
        "events": list(state["events"]),
    }
