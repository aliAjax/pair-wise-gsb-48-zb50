"""大额交收 HTTP 路由与统一错误输出，与主业务接口风格保持一致。"""
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


SETTLEMENT_RE = re.compile(r"^/api/settlements/(\d+)$")
SETTLEMENT_ENTRIES_RE = re.compile(r"^/api/settlements/(\d+)/entries$")
SETTLEMENT_REVERSE_RE = re.compile(r"^/api/settlements/(\d+)/reverse$")
SETTLEMENT_AUDIT_RE = re.compile(r"^/api/settlements/(\d+)/audit$")


class SettlementApi:
    """把 /api/settlements* 的请求分派给 SettlementService。"""

    def __init__(self, service: Any) -> None:
        self.service = service

    def handle_get(self, actor: Actor, path: str, query: str) -> Any:
        parsed_path = path
        params = parse_qs(query)
        if parsed_path == "/api/settlements":
            return 200, {"items": self.service.list_instructions(
                actor,
                status=params.get("status", [None])[0],
                limit=int(params.get("limit", ["100"])[0]),
            )}
        match = SETTLEMENT_ENTRIES_RE.match(parsed_path)
        if match:
            view = self.service.get_instruction(actor, int(match.group(1)))
            return 200, {"items": view["entries"]}
        match = SETTLEMENT_AUDIT_RE.match(parsed_path)
        if match:
            return 200, {"items": self.service.timeline(actor, int(match.group(1)))}
        match = SETTLEMENT_RE.match(parsed_path)
        if match:
            return 200, self.service.get_instruction(actor, int(match.group(1)))
        return None

    def handle_post(self, actor: Actor, path: str, body: dict) -> Any:
        if path == "/api/settlements":
            return 201, self.service.create_instruction(actor, body.get("reference", ""), body.get("data", {}))
        match = SETTLEMENT_ENTRIES_RE.match(path)
        if match:
            # 单笔超限/整笔退回由领域层抛 ValidationError(422)，终态与重复来源单号抛 Conflict(409)
            return 200, self.service.register(actor, int(match.group(1)), body.get("data", {}))
        match = SETTLEMENT_REVERSE_RE.match(path)
        if match:
            return 200, self.service.reverse(actor, int(match.group(1)), body.get("data", {}))
        return None
