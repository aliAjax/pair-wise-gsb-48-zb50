"""大额交收的累计判断规则（纯函数，不接触存储）。

规则要点：
- 按笔累计：每笔登记累加数量与金额，任一指标未达标即保持处理中并给出差额；
- 单笔超出剩余数量或金额，整笔退回（ValidationError，不留存）；
- 同一来源单号重复提交只算一次（Conflict，不留存）；
- 已结清或已冲正的指令不再接收交收；
- 冲正时保留全部到账明细，未完成部分生成退回明细。
"""
from typing import Any, Dict, List, Tuple

from .domain import Conflict, ValidationError, text
from .settlement_detail import (
    ENTRY_ARRIVAL,
    ENTRY_RETURN,
    STATUS_PROCESSING,
    STATUS_REVERSED,
    STATUS_SETTLED,
    EntryDraft,
)

# 金额比较的容差，规避浮点尾差导致永远差一分钱
EPS = 0.005


def accumulated(entries: List[Dict[str, Any]]) -> Tuple[int, float]:
    """按笔累计所有到账明细的数量与金额（退回明细不参与累计）。"""
    total_quantity = 0
    total_amount = 0.0
    for entry in entries or []:
        if entry.get("entry_type") == ENTRY_ARRIVAL:
            total_quantity += int(entry["quantity"])
            total_amount += float(entry["amount"])
    return total_quantity, round(total_amount, 2)


def remaining(instruction: Dict[str, Any], entries: List[Dict[str, Any]]) -> Tuple[int, float]:
    """还差多少：剩余数量、剩余金额。"""
    total_quantity, total_amount = accumulated(entries)
    left_quantity = int(instruction["required_quantity"]) - total_quantity
    left_amount = round(float(instruction["required_amount"]) - total_amount, 2)
    return max(0, left_quantity), round(max(0.0, left_amount), 2)


def is_complete(instruction: Dict[str, Any], entries: List[Dict[str, Any]]) -> bool:
    """累计达标：数量与金额同时备齐才算结清。"""
    total_quantity, total_amount = accumulated(entries)
    quantity_ok = total_quantity >= int(instruction["required_quantity"])
    amount_ok = total_amount + EPS >= float(instruction["required_amount"])
    return quantity_ok and amount_ok


def progress(instruction: Dict[str, Any], entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """累计判断的对外视图：总额、差额、是否达标。"""
    total_quantity, total_amount = accumulated(entries)
    left_quantity, left_amount = remaining(instruction, entries)
    return {
        "accumulated_quantity": total_quantity,
        "accumulated_amount": total_amount,
        "remaining_quantity": left_quantity,
        "remaining_amount": left_amount,
        "complete": is_complete(instruction, entries),
    }


def check_register(instruction: Dict[str, Any], entries: List[Dict[str, Any]], draft: EntryDraft) -> None:
    """登记前校验：终态拒收、重复来源单号拒收、超剩余则整笔退回。"""
    if instruction["status"] in (STATUS_SETTLED, STATUS_REVERSED):
        raise Conflict("指令已%s，不再接收交收" % instruction["status"])
    if any(e.get("entry_type") == ENTRY_ARRIVAL and e.get("source_ref") == draft.source_ref for e in entries):
        raise Conflict("来源单号%s已登记，重复提交只算一次" % draft.source_ref)
    left_quantity, left_amount = remaining(instruction, entries)
    if draft.quantity > left_quantity:
        raise ValidationError("本笔数量%s超出剩余数量%s，整笔退回" % (draft.quantity, left_quantity))
    if draft.amount > left_amount + EPS:
        raise ValidationError("本笔金额%s超出剩余金额%s，整笔退回" % (round(draft.amount, 2), left_amount))


def register_summary(instruction: Dict[str, Any], entries: List[Dict[str, Any]], arrived_count: int) -> str:
    """登记完成后的审计摘要文案，说明是否结清及差额。"""
    total_quantity, total_amount = accumulated(entries)
    if is_complete(instruction, entries):
        return "第%s笔到账，累计数量%s/金额%s，已备齐结清" % (arrived_count, total_quantity, round(total_amount, 2))
    left_quantity, left_amount = remaining(instruction, entries)
    return "第%s笔到账，累计数量%s/金额%s，处理中，还差数量%s/金额%s" % (
        arrived_count,
        total_quantity,
        round(total_amount, 2),
        left_quantity,
        left_amount,
    )


def build_return_entry(instruction: Dict[str, Any], entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """冲正时构造未完成部分的退回明细（已完成部分为0）。"""
    left_quantity, left_amount = remaining(instruction, entries)
    return {
        "entry_type": ENTRY_RETURN,
        "quantity": left_quantity,
        "amount": left_amount,
        "source_ref": "REVERSAL-%s" % instruction["reference"],
        "operator": "",
        "note": "冲正退回未完成部分",
    }


def validate_reverse(instruction: Dict[str, Any], data: Dict[str, Any]) -> str:
    """冲正校验：仅处理中/已结清可冲正，必须填写冲正原因。"""
    if instruction["status"] == STATUS_REVERSED:
        raise Conflict("指令已冲正，不能重复冲正")
    if instruction["status"] not in (STATUS_PROCESSING, STATUS_SETTLED):
        raise Conflict("当前状态不允许冲正")
    return text(data or {}, "reverse_reason")
