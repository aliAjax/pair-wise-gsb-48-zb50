import tempfile
import unittest
from pathlib import Path

from app import build_settlement_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


OFFICER = Actor("officer", "settlement_officer")
TRADER = Actor("trader", "trader")


class SettlementServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_settlement_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def create(self, reference="SET-1", qty=1000, amount=10000.0):
        return self.service.create_instruction(OFFICER, reference, {"required_quantity": qty, "required_amount": amount})

    def register(self, instruction_id, qty, amount, source, operator="张三"):
        return self.service.register(OFFICER, instruction_id, {
            "quantity": qty, "amount": amount, "source_ref": source, "operator": operator,
        })

    def test_permission_denied_for_trader(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_instruction(TRADER, "SET-X", {"required_quantity": 1, "required_amount": 1.0})

    def test_accumulate_until_settled(self):
        instruction = self.create()
        view = self.register(instruction["id"], 600, 6000.0, "SRC-A")
        self.assertEqual(view["status"], "processing")
        self.assertEqual(view["accumulated_quantity"], 600)
        self.assertEqual(view["remaining_quantity"], 400)
        self.assertEqual(view["remaining_amount"], 4000.0)
        self.assertEqual(view["entry_count"], 1)
        self.assertEqual(view["last_entry"]["source_ref"], "SRC-A")
        self.assertEqual(view["entries"][0]["operator"], "张三")

        view = self.register(instruction["id"], 400, 4000.0, "SRC-B", operator="李四")
        self.assertEqual(view["status"], "settled")
        self.assertEqual((view["remaining_quantity"], view["remaining_amount"]), (0, 0.0))
        self.assertTrue(view["complete"])

    def test_entry_exceeding_remaining_returned_whole(self):
        instruction = self.create()
        self.register(instruction["id"], 600, 6000.0, "SRC-A")
        with self.assertRaises(ValidationError):
            self.register(instruction["id"], 401, 1000.0, "SRC-B")
        with self.assertRaises(ValidationError):
            self.register(instruction["id"], 100, 4000.01, "SRC-C")
        # 整笔退回：没有残留明细，累计不变
        view = self.service.get_instruction(OFFICER, instruction["id"])
        self.assertEqual(view["entry_count"], 1)
        self.assertEqual(view["accumulated_quantity"], 600)
        self.assertEqual(view["status"], "processing")

    def test_duplicate_source_ref_counts_once(self):
        instruction = self.create()
        self.register(instruction["id"], 100, 1000.0, "DUP")
        with self.assertRaises(Conflict):
            self.register(instruction["id"], 100, 1000.0, "DUP", operator="李四")
        view = self.service.get_instruction(OFFICER, instruction["id"])
        self.assertEqual(view["entry_count"], 1)

    def test_settled_instruction_rejects_more_registration(self):
        instruction = self.create()
        self.register(instruction["id"], 1000, 10000.0, "SRC-A")
        with self.assertRaises(Conflict):
            self.register(instruction["id"], 1, 1.0, "SRC-Z")

    def test_reverse_returns_remaining_and_keeps_arrival_detail(self):
        instruction = self.create("SET-R")
        self.register(instruction["id"], 600, 6000.0, "SRC-A", operator="张三")
        self.register(instruction["id"], 300, 3000.0, "SRC-B", operator="李四")
        view = self.service.reverse(OFFICER, instruction["id"], {"reverse_reason": "客户撤单"})
        self.assertEqual(view["status"], "reversed")
        arrivals = [e for e in view["entries"] if e["entry_type"] == "arrival"]
        returns = [e for e in view["entries"] if e["entry_type"] == "return"]
        self.assertEqual(len(arrivals), 2)  # 到账明细全部保留
        self.assertEqual(len(returns), 1)
        self.assertEqual((returns[0]["quantity"], returns[0]["amount"]), (100, 1000.0))
        # 冲正后不再接收交收
        with self.assertRaises(Conflict):
            self.register(instruction["id"], 100, 1000.0, "SRC-C")
        # 不能重复冲正
        with self.assertRaises(Conflict):
            self.service.reverse(OFFICER, instruction["id"], {"reverse_reason": "再次"})

    def test_reverse_settled_keeps_all_arrivals_zero_return(self):
        instruction = self.create("SET-S")
        self.register(instruction["id"], 1000, 10000.0, "SRC-A")
        view = self.service.reverse(OFFICER, instruction["id"], {"reverse_reason": "差错"})
        self.assertEqual(view["status"], "reversed")
        self.assertEqual(len([e for e in view["entries"] if e["entry_type"] == "arrival"]), 1)
        self.assertEqual(len([e for e in view["entries"] if e["entry_type"] == "return"]), 0)

    def test_audit_timeline_records_every_step(self):
        instruction = self.create("SET-T")
        self.register(instruction["id"], 1000, 10000.0, "SRC-A")
        self.service.reverse(OFFICER, instruction["id"], {"reverse_reason": "x"})
        timeline = self.service.timeline(OFFICER, instruction["id"])
        actions = [event["action"] for event in timeline]
        self.assertEqual(actions, ["created", "register", "reverse"])


if __name__ == "__main__":
    unittest.main()
