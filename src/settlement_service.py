"""大额交收用例编排：权限、明细校验、累计判断与保存的串联。"""
from typing import Any, Dict, List, Optional

from src.domain import Actor, PermissionDenied, text

from .settlement_detail import (
    STATUS_LABELS,
    validate_entry,
    validate_instruction,
)
from .settlement_repository import SettlementRepository
from .settlement_rules import (
    build_return_entry,
    check_register,
    progress,
    register_summary,
    validate_reverse,
)


ALLOWED_ROLES = {"settlement_officer"}


class SettlementService:
    def __init__(self, repository: SettlementRepository) -> None:
        self.repository = repository

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_role(self, actor: Actor) -> None:
        if actor.role != "admin" and actor.role not in ALLOWED_ROLES:
            raise PermissionDenied("角色无权操作大额交收")

    def create_instruction(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_role(actor)
        reference = text({"reference": reference}, "reference")
        required_quantity, required_amount = validate_instruction(payload or {})
        instruction = self.repository.create_instruction(reference, required_quantity, required_amount, payload or {}, actor.user_id)
        return self._view(instruction, [])

    def list_instructions(self, actor: Actor, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_role(actor)
        return [self._view(item, self.repository.list_entries(item["id"])) for item in self.repository.list_instructions(status=status, limit=limit)]

    def get_instruction(self, actor: Actor, instruction_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_role(actor)
        instruction = self.repository.get_instruction(instruction_id)
        return self._view(instruction, self.repository.list_entries(instruction_id))

    def register(self, actor: Actor, instruction_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """按笔登记一次到账；累计达标结清，未达标保持处理中并显示差额。"""
        actor = self._actor(actor)
        self._ensure_role(actor)
        instruction = self.repository.get_instruction(instruction_id)
        entries = self.repository.list_entries(instruction_id)
        draft = validate_entry(payload or {})
        check_register(instruction, entries, draft)
        summary = register_summary(instruction, entries, sum(1 for e in entries if e["entry_type"] == "arrival") + 1)
        result = self.repository.register_entry(instruction_id, draft.as_dict(), actor.user_id, summary)
        return self._view(result["instruction"], self.repository.list_entries(instruction_id), last_entry=result["entry"])

    def reverse(self, actor: Actor, instruction_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """冲正：保留每次到账明细，未完成部分退回。"""
        actor = self._actor(actor)
        self._ensure_role(actor)
        instruction = self.repository.get_instruction(instruction_id)
        reason = validate_reverse(instruction, data or {})
        entries = self.repository.list_entries(instruction_id)
        return_entry = build_return_entry(instruction, entries)
        result = self.repository.reverse_instruction(instruction_id, reason, return_entry, actor.user_id)
        return self._view(result["instruction"], result["entries"])

    def timeline(self, actor: Actor, instruction_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_role(actor)
        return self.repository.audit_timeline(instruction_id)

    @staticmethod
    def _view(instruction: Dict[str, Any], entries: List[Dict[str, Any]], last_entry: Dict[str, Any] = None) -> Dict[str, Any]:
        view = progress(instruction, entries)
        return {
            "id": instruction["id"],
            "reference": instruction["reference"],
            "status": instruction["status"],
            "status_label": STATUS_LABELS.get(instruction["status"], instruction["status"]),
            "required_quantity": int(instruction["required_quantity"]),
            "required_amount": round(float(instruction["required_amount"]), 2),
            "accumulated_quantity": view["accumulated_quantity"],
            "accumulated_amount": view["accumulated_amount"],
            "remaining_quantity": view["remaining_quantity"],
            "remaining_amount": view["remaining_amount"],
            "complete": view["complete"],
            "entry_count": sum(1 for e in entries if e["entry_type"] == "arrival"),
            "entries": entries,
            "last_entry": last_entry,
            "created_by": instruction["created_by"],
            "created_at": instruction["created_at"],
            "updated_at": instruction["updated_at"],
        }
