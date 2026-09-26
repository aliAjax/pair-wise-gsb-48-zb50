"""大额交收的 SQLite 保存：指令表、到账明细表、交收审计表。

所有累计写入在单事务内完成，并由条件 UPDATE 兜底：
- 终态指令（settled/reversed）无法再挂入到账明细；
- 数量/金额超剩余的登记无法更新指令累计列（整笔退回、事务回滚）；
- 同一来源单号由部分唯一索引保证只算一次。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound
from .settlement_detail import ENTRY_ARRIVAL, ENTRY_RETURN, STATUS_PROCESSING
from .settlement_rules import EPS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SettlementRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settlement_instructions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    required_quantity INTEGER NOT NULL,
                    required_amount REAL NOT NULL,
                    accumulated_quantity INTEGER NOT NULL DEFAULT 0,
                    accumulated_amount REAL NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settlement_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instruction_id INTEGER NOT NULL REFERENCES settlement_instructions(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    entry_type TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    source_ref TEXT NOT NULL,
                    operator TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settlement_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instruction_id INTEGER NOT NULL REFERENCES settlement_instructions(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_settlement_status ON settlement_instructions(status);
                CREATE INDEX IF NOT EXISTS idx_settlement_entries ON settlement_entries(instruction_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_settlement_arrival_source
                    ON settlement_entries(instruction_id, source_ref) WHERE entry_type = 'arrival';
                """
            )

    @staticmethod
    def _instruction_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _entry_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["amount"] = round(float(item["amount"]), 2)
        return item

    def create_instruction(self, reference: str, required_quantity: int, required_amount: float, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO settlement_instructions(reference,status,required_quantity,required_amount,accumulated_quantity,accumulated_amount,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (reference, STATUS_PROCESSING, required_quantity, float(required_amount), 0, 0.0,
                     json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                instruction_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO settlement_audit_events(instruction_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
                    (instruction_id, "created", actor_id,
                     json.dumps({"status": STATUS_PROCESSING, "required_quantity": required_quantity, "required_amount": required_amount}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM settlement_instructions WHERE id=?", (instruction_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._instruction_row(row)

    def get_instruction(self, instruction_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM settlement_instructions WHERE id=?", (instruction_id,)).fetchone()
        if row is None:
            raise NotFound("交收指令不存在")
        return self._instruction_row(row)

    def list_instructions(self, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if status:
                rows = connection.execute("SELECT * FROM settlement_instructions WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM settlement_instructions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._instruction_row(row) for row in rows]

    def list_entries(self, instruction_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM settlement_entries WHERE instruction_id=? ORDER BY id", (instruction_id,)).fetchall()
        return [self._entry_row(row) for row in rows]

    def register_entry(self, instruction_id: int, draft: Dict[str, Any], actor_id: str, summary: str) -> Dict[str, Any]:
        """原子地登记一笔到账并更新累计；状态与超限由条件 UPDATE 兜底。

        返回 (更新后的指令, 新明细)。任何兜底失败均回滚（整笔退回）。
        """
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM settlement_instructions WHERE id=?", (instruction_id,)).fetchone()
                if row is None:
                    connection.rollback()
                    raise NotFound("交收指令不存在")
                arrival_count = int(
                    connection.execute(
                        "SELECT COUNT(*) AS total FROM settlement_entries WHERE instruction_id=? AND entry_type=?",
                        (instruction_id, ENTRY_ARRIVAL),
                    ).fetchone()["total"]
                )
                cursor = connection.execute(
                    "INSERT INTO settlement_entries(instruction_id,seq,entry_type,quantity,amount,source_ref,operator,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (instruction_id, arrival_count + 1, ENTRY_ARRIVAL, int(draft["quantity"]), float(draft["amount"]),
                     draft["source_ref"], draft["operator"], "", now),
                )
                entry_id = int(cursor.lastrowid)
                # 条件更新：只处理处理中指令，且本笔不得超出剩余数量/金额；达标即结清。
                updated = connection.execute(
                    """
                    UPDATE settlement_instructions
                       SET accumulated_quantity = accumulated_quantity + ?,
                           accumulated_amount = ROUND(accumulated_amount + ?, 2),
                           status = CASE
                                      WHEN accumulated_quantity + ? >= required_quantity
                                       AND ROUND(accumulated_amount + ?, 2) + ? >= required_amount
                                      THEN 'settled' ELSE status
                                    END,
                           updated_by = ?, updated_at = ?
                     WHERE id = ?
                       AND status = 'processing'
                       AND ? <= required_quantity - accumulated_quantity
                       AND ? <= required_amount - accumulated_amount + ?
                    """,
                    (draft["quantity"], draft["amount"], draft["quantity"], draft["amount"], EPS,
                     actor_id, now, instruction_id,
                     draft["quantity"], draft["amount"], EPS),
                )
                if updated.rowcount != 1:
                    connection.rollback()
                    raise Conflict("指令已结清或已冲正，或本笔超出剩余数量/金额，整笔退回")
                connection.execute(
                    "INSERT INTO settlement_audit_events(instruction_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
                    (instruction_id, "register", actor_id,
                     json.dumps({"summary": summary, "entry": draft}, ensure_ascii=False, sort_keys=True), now),
                )
                instruction = self._instruction_row(
                    connection.execute("SELECT * FROM settlement_instructions WHERE id=?", (instruction_id,)).fetchone()
                )
                entry = self._entry_row(
                    connection.execute("SELECT * FROM settlement_entries WHERE id=?", (entry_id,)).fetchone()
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("来源单号已登记，重复提交只算一次") from exc
        return {"instruction": instruction, "entry": entry}

    def reverse_instruction(self, instruction_id: int, reason: str, return_entry: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        """冲正：保留全部到账明细，追加未完成部分的退回明细，指令置为已冲正。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM settlement_instructions WHERE id=?", (instruction_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("交收指令不存在")
            if str(row["status"]) == "reversed":
                connection.rollback()
                raise Conflict("指令已冲正，不能重复冲正")
            if return_entry["quantity"] > 0 or return_entry["amount"] > 0:
                seq = int(
                    connection.execute("SELECT COUNT(*) AS total FROM settlement_entries WHERE instruction_id=?", (instruction_id,)).fetchone()["total"]
                ) + 1
                connection.execute(
                    "INSERT INTO settlement_entries(instruction_id,seq,entry_type,quantity,amount,source_ref,operator,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (instruction_id, seq, ENTRY_RETURN, int(return_entry["quantity"]), float(return_entry["amount"]),
                     return_entry["source_ref"], return_entry.get("operator", ""), return_entry.get("note", ""), now),
                )
            connection.execute(
                "UPDATE settlement_instructions SET status='reversed', updated_by=?, updated_at=? WHERE id=?",
                (actor_id, now, instruction_id),
            )
            connection.execute(
                "INSERT INTO settlement_audit_events(instruction_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
                (instruction_id, "reverse", actor_id,
                 json.dumps({"reverse_reason": reason, "return": return_entry}, ensure_ascii=False, sort_keys=True), now),
            )
            instruction = self._instruction_row(
                connection.execute("SELECT * FROM settlement_instructions WHERE id=?", (instruction_id,)).fetchone()
            )
            entries = [self._entry_row(item) for item in connection.execute("SELECT * FROM settlement_entries WHERE instruction_id=? ORDER BY id", (instruction_id,)).fetchall()]
            connection.commit()
        return {"instruction": instruction, "entries": entries}

    def audit_timeline(self, instruction_id: int) -> List[Dict[str, Any]]:
        self.get_instruction(instruction_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM settlement_audit_events WHERE instruction_id=? ORDER BY id", (instruction_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
