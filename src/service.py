"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from . import settlement
from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        if action == "settle":
            return self._settle(actor, record, int(expected_version), data or {})
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        details: Dict[str, Any] = {"summary": summary, "input": data or {}, "from": record["state"], "to": new_state}
        if action == "reverse":
            # 冲正保留每次到账明细，未完成部分退回。
            progress = self._settlement_progress(record)
            new_payload["delivered_quantity"] = progress["delivered_quantity"]
            new_payload["cash_paid"] = progress["cash_paid"]
            new_payload["returned_quantity"] = progress["remaining_quantity"]
            new_payload["returned_amount"] = progress["remaining_amount"]
            details["settlement"] = progress
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details=details,
        )

    def _settlement_progress(self, record: Dict[str, Any]) -> Dict[str, Any]:
        required_quantity, required_amount = self.rules.required_settlement(record["payload"])
        entries = self.repository.list_settlements(record["id"])
        return settlement.evaluate(required_quantity, required_amount, entries)

    def _settle(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        entry = settlement.parse_entry(data, actor.user_id)
        entries = self.repository.list_settlements(record["id"])
        if any(item["source_ref"] == entry["source_ref"] for item in entries):
            return record  # 同一来源单号重复提交只算一次
        required_quantity, required_amount = self.rules.required_settlement(record["payload"])
        progress = settlement.evaluate(required_quantity, required_amount, entries)
        settlement.check_entry(entry, progress)
        updated = settlement.evaluate(required_quantity, required_amount, entries + [entry])
        new_state = "settled" if updated["complete"] else "settling"
        payload = dict(record["payload"])
        payload.update({
            "delivered_quantity": updated["delivered_quantity"],
            "cash_paid": updated["cash_paid"],
            "remaining_quantity": updated["remaining_quantity"],
            "remaining_amount": updated["remaining_amount"],
        })
        if updated["complete"]:
            summary = "交收累计达标，指令结清"
        else:
            summary = "交收登记成功，仍差数量%s、金额%s" % (updated["remaining_quantity"], updated["remaining_amount"])
        saved = self.repository.record_settlement(
            record_id=record["id"],
            expected_version=expected_version,
            entry=entry,
            state=new_state,
            payload=payload,
            actor_id=actor.user_id,
            action="settle",
            details={"summary": summary, "input": data, "from": record["state"], "to": new_state, "settlement": updated},
        )
        if saved is None:
            return self.repository.get(record["id"])  # 并发下同一来源单号重复提交只算一次
        return saved

    def settlements(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        entries = self.repository.list_settlements(record_id)
        required_quantity, required_amount = self.rules.required_settlement(record["payload"])
        progress = settlement.evaluate(required_quantity, required_amount, entries)
        return {"items": entries, "progress": progress}

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
