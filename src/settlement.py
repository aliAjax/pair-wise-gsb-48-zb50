"""交收明细解析与按笔累计判断。

本模块只负责单笔交收登记的校验和累计达标计算，
不触碰持久化与HTTP，便于与保存、接口、页面分层演进。
"""
from typing import Any, Dict, Iterable, List

from .domain import ValidationError, integer, number, optional_text, text


EPSILON = 1e-9


def parse_entry(data: Dict[str, Any], default_operator: str) -> Dict[str, Any]:
    """解析单笔交收登记：数量、金额、来源单号和经办人。"""
    data = dict(data or {})
    entry = {
        "source_ref": text(data, "source_ref"),
        "quantity": integer(data, "delivered_quantity", 0),
        "amount": round(number(data, "cash_paid", 0.0), 2),
        "operator": optional_text(data, "operator") or default_operator,
    }
    if entry["quantity"] == 0 and entry["amount"] <= 0:
        raise ValidationError("本次交收数量或金额必须大于0")
    return entry


def evaluate(required_quantity: int, required_amount: float, entries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """按笔累计：汇总已到账明细，计算距离结清还差多少。"""
    items: List[Dict[str, Any]] = list(entries)
    delivered = sum(int(item["quantity"]) for item in items)
    paid = round(sum(float(item["amount"]) for item in items), 2)
    remaining_quantity = int(required_quantity) - delivered
    remaining_amount = round(float(required_amount) - paid, 2)
    return {
        "required_quantity": int(required_quantity),
        "required_amount": round(float(required_amount), 2),
        "delivered_quantity": delivered,
        "cash_paid": paid,
        "remaining_quantity": remaining_quantity,
        "remaining_amount": remaining_amount,
        "entry_count": len(items),
        "complete": remaining_quantity <= 0 and remaining_amount <= EPSILON,
    }


def check_entry(entry: Dict[str, Any], progress: Dict[str, Any]) -> None:
    """单次登记超出剩余数量或金额时整笔退回，不计入累计。"""
    if entry["quantity"] > progress["remaining_quantity"]:
        raise ValidationError("交收数量%s超出剩余数量%s，整笔退回" % (entry["quantity"], progress["remaining_quantity"]))
    if entry["amount"] - progress["remaining_amount"] > EPSILON:
        raise ValidationError("交收金额%s超出剩余金额%s，整笔退回" % (entry["amount"], progress["remaining_amount"]))
