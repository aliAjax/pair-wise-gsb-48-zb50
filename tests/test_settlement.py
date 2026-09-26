import tempfile
import unittest
from pathlib import Path

from app import build_service
from src import settlement
from src.domain import Actor, Conflict, ValidationError


CREATE_DATA = {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0, 'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'split', 'action_ratio': 2.0}
# 拆分后应交收数量2000，净额12518.0


class SettlementRulesTest(unittest.TestCase):
    def test_evaluate_remaining(self):
        progress = settlement.evaluate(2000, 12518.0, [{"quantity": 1200, "amount": 7000.0}])
        self.assertEqual(progress["delivered_quantity"], 1200)
        self.assertEqual(progress["cash_paid"], 7000.0)
        self.assertEqual(progress["remaining_quantity"], 800)
        self.assertEqual(progress["remaining_amount"], 5518.0)
        self.assertFalse(progress["complete"])

    def test_evaluate_complete(self):
        progress = settlement.evaluate(2000, 12518.0, [{"quantity": 1200, "amount": 7000.0}, {"quantity": 800, "amount": 5518.0}])
        self.assertTrue(progress["complete"])

    def test_check_entry_rejects_overshoot(self):
        progress = settlement.evaluate(100, 100.0, [])
        with self.assertRaises(ValidationError):
            settlement.check_entry({"source_ref": "A", "quantity": 101, "amount": 0.0, "operator": "x"}, progress)
        with self.assertRaises(ValidationError):
            settlement.check_entry({"source_ref": "B", "quantity": 0, "amount": 100.01, "operator": "x"}, progress)
        settlement.check_entry({"source_ref": "C", "quantity": 100, "amount": 100.0, "operator": "x"}, progress)

    def test_parse_entry_requires_source_and_value(self):
        with self.assertRaises(ValidationError):
            settlement.parse_entry({"delivered_quantity": 1, "cash_paid": 1.0}, "op")
        with self.assertRaises(ValidationError):
            settlement.parse_entry({"source_ref": "A"}, "op")
        entry = settlement.parse_entry({"source_ref": "A", "delivered_quantity": 1, "cash_paid": 0}, "op")
        self.assertEqual(entry["operator"], "op")


class SettlementFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        record = self.service.create(Actor("creator", "trader"), "TRD-1", CREATE_DATA)
        record = self.service.act(Actor("op", "corporate_actions"), record["id"], record["version"], "apply_corporate", {})
        self.record = self.service.act(Actor("op", "settlement_officer"), record["id"], record["version"], "approve", {})

    def tearDown(self):
        self.temp.cleanup()

    def settle(self, record, data):
        return self.service.act(Actor("officer", "settlement_officer"), record["id"], record["version"], "settle", data)

    def detail(self):
        return self.service.settlements(Actor("viewer", "trader"), self.record["id"])

    def test_partial_accumulates_until_complete(self):
        first = self.settle(self.record, {"source_ref": "PAY-1", "delivered_quantity": 1200, "cash_paid": 7000.0, "operator": "alice"})
        self.assertEqual(first["state"], "settling")
        self.assertEqual(first["payload"]["remaining_quantity"], 800)
        self.assertEqual(first["payload"]["remaining_amount"], 5518.0)
        detail = self.detail()
        self.assertEqual(len(detail["items"]), 1)
        self.assertEqual(detail["items"][0]["source_ref"], "PAY-1")
        self.assertEqual(detail["items"][0]["operator"], "alice")
        self.assertEqual(detail["progress"]["remaining_quantity"], 800)
        second = self.settle(first, {"source_ref": "PAY-2", "delivered_quantity": 800, "cash_paid": 5518.0})
        self.assertEqual(second["state"], "settled")
        self.assertEqual(second["payload"]["remaining_quantity"], 0)
        self.assertEqual(second["payload"]["remaining_amount"], 0.0)

    def test_overshoot_rejected_as_whole(self):
        with self.assertRaises(ValidationError):
            self.settle(self.record, {"source_ref": "PAY-X", "delivered_quantity": 2001, "cash_paid": 100.0})
        with self.assertRaises(ValidationError):
            self.settle(self.record, {"source_ref": "PAY-Y", "delivered_quantity": 10, "cash_paid": 12518.01})
        self.assertEqual(self.detail()["items"], [])
        record = self.service.get_record(Actor("viewer", "trader"), self.record["id"])
        self.assertEqual(record["state"], "approved")

    def test_duplicate_source_ref_counts_once(self):
        first = self.settle(self.record, {"source_ref": "PAY-1", "delivered_quantity": 1000, "cash_paid": 6000.0})
        again = self.settle(first, {"source_ref": "PAY-1", "delivered_quantity": 1000, "cash_paid": 6000.0})
        self.assertEqual(again["state"], "settling")
        detail = self.detail()
        self.assertEqual(len(detail["items"]), 1)
        self.assertEqual(detail["items"][0]["operator"], "officer")
        self.assertEqual(detail["progress"]["delivered_quantity"], 1000)
        self.assertEqual(detail["progress"]["cash_paid"], 6000.0)

    def test_settled_rejects_further_settlement(self):
        settled = self.settle(self.record, {"source_ref": "PAY-1", "delivered_quantity": 2000, "cash_paid": 12518.0})
        self.assertEqual(settled["state"], "settled")
        with self.assertRaises(Conflict):
            self.settle(settled, {"source_ref": "PAY-2", "delivered_quantity": 1, "cash_paid": 1.0})

    def test_reverse_keeps_entries_and_returns_shortfall(self):
        partial = self.settle(self.record, {"source_ref": "PAY-1", "delivered_quantity": 500, "cash_paid": 3000.0})
        reversed_record = self.service.act(Actor("op", "settlement_officer"), partial["id"], partial["version"], "reverse", {"reverse_reason": "对手方违约"})
        self.assertEqual(reversed_record["state"], "reversed")
        self.assertEqual(reversed_record["payload"]["delivered_quantity"], 500)
        self.assertEqual(reversed_record["payload"]["cash_paid"], 3000.0)
        self.assertEqual(reversed_record["payload"]["returned_quantity"], 1500)
        self.assertEqual(reversed_record["payload"]["returned_amount"], 9518.0)
        self.assertEqual(len(self.detail()["items"]), 1)
        timeline = self.service.timeline(Actor("viewer", "trader"), self.record["id"])
        self.assertEqual(timeline[-1]["action"], "reverse")
        self.assertIn("settlement", timeline[-1]["details"])
        with self.assertRaises(Conflict):
            self.settle(reversed_record, {"source_ref": "PAY-2", "delivered_quantity": 1, "cash_paid": 1.0})
