"""大额交收：指令状态、逐笔到账明细的数据结构与单笔登记校验。

只负责"交收明细"本身：每笔登记必须带数量、金额、来源单号、经办人。
累计是否达标、能否入账由 settlement_rules 判断，持久化由 settlement_repository 负责。
"""
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from .domain import integer, number, text

# 指令状态：处理中 / 已结清 / 已冲正
STATUS_PROCESSING = "processing"
STATUS_SETTLED = "settled"
STATUS_REVERSED = "reversed"
TERMINAL_STATUSES = (STATUS_SETTLED, STATUS_REVERSED)

# 明细类型：到账登记 / 冲正时对未完成部分的退回
ENTRY_ARRIVAL = "arrival"
ENTRY_RETURN = "return"

STATUS_LABELS = {
    STATUS_PROCESSING: "处理中",
    STATUS_SETTLED: "已结清",
    STATUS_REVERSED: "已冲正",
}


@dataclass(frozen=True)
class EntryDraft:
    """一次交收登记的明细（按笔累计的最小单位）。"""

    quantity: int
    amount: float
    source_ref: str
    operator: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "quantity": self.quantity,
            "amount": self.amount,
            "source_ref": self.source_ref,
            "operator": self.operator,
        }


def validate_instruction(payload: Dict[str, Any]) -> Tuple[int, float]:
    """校验大额交收指令的应到数量与应到金额。"""
    data = payload or {}
    required_quantity = integer(data, "required_quantity", 1)
    required_amount = round(number(data, "required_amount", 0.01), 2)
    return required_quantity, required_amount


def validate_entry(payload: Dict[str, Any]) -> EntryDraft:
    """校验单笔到账登记：数量、金额、来源单号、经办人缺一不可。"""
    data = payload or {}
    return EntryDraft(
        quantity=integer(data, "quantity", 1),
        amount=round(number(data, "amount", 0.01), 2),
        source_ref=text(data, "source_ref"),
        operator=text(data, "operator"),
    )
