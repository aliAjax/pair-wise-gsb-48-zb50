import unittest

from src.domain import Conflict, ValidationError
from src.settlement_detail import (
    ENTRY_ARRIVAL,
    ENTRY_RETURN,
    STATUS_PROCESSING,
    STATUS_REVERSED,
    STATUS_SETTLED,
    EntryDraft,
    validate_entry,
    validate_instruction,
)
from src.settlement_rules import (
    accumulated,
    build_return_entry,
    check_register,
    is_complete,
    progress,
    register_summary,
    validate_reverse,
)


INSTRUCTION = {"id": 1, "reference": "SET-1", "status": STATUS_PROCESSING, "required_quantity": 1000, "required_amount": 10000.0}


def arrival(seq, qty, amount, source, operator="张三"):
    return {"seq": seq, "entry_type": ENTRY_ARRIVAL, "quantity": qty, "amount": amount, "source_ref": source, "operator": operator}


class SettlementDetailTest(unittest.TestCase):
    def test_instruction_validation(self):
        self.assertEqual(validate_instruction({"required_quantity": 500, "required_amount": 5000.5}), (500, 5000.5))
        for bad in ({}, {"required_quantity": 0, "required_amount": 1.0}, {"required_quantity": 1, "required_amount": 0}):
            with self.assertRaises(ValidationError):
                validate_instruction(bad)

    def test_entry_requires_all_fields(self):
        draft = validate_entry({"quantity": 100, "amount": 1000.0, "source_ref": "SRC-1", "operator": "李四"})
        self.assertEqual(draft, EntryDraft(100, 1000.0, "SRC-1", "李四"))
        for missing in ("quantity", "amount", "source_ref", "operator"):
            payload = {"quantity": 100, "amount": 1000.0, "source_ref": "SRC-1", "operator": "李四"}
            del payload[missing]
            with self.assertRaises(ValidationError):
                validate_entry(payload)

    def test_entry_amount_rounded(self):
        draft = validate_entry({"quantity": 1, "amount": 1.239, "source_ref": "S", "operator": "O"})
        self.assertEqual(draft.amount, 1.24)


class AccumulationRulesTest(unittest.TestCase):
    def test_accumulate_arrivals_only(self):
        entries = [arrival(1, 600, 6000.0, "A"), arrival(2, 300, 3000.0, "B"),
                   {"seq": 3, "entry_type": ENTRY_RETURN, "quantity": 100, "amount": 1000.0, "source_ref": "R"}]
        self.assertEqual(accumulated(entries), (900, 9000.0))
        self.assertEqual(progress(INSTRUCTION, entries)["remaining_quantity"], 100)
        self.assertEqual(progress(INSTRUCTION, entries)["remaining_amount"], 1000.0)
        self.assertFalse(is_complete(INSTRUCTION, entries))

    def test_complete_requires_both_quantity_and_amount(self):
        entries = [arrival(1, 1000, 9000.0, "A")]
        self.assertFalse(is_complete(INSTRUCTION, entries))  # 数量齐了，金额没齐
        entries = [arrival(1, 900, 10000.0, "A")]
        self.assertFalse(is_complete(INSTRUCTION, entries))  # 金额齐了，数量没齐
        entries = [arrival(1, 600, 6000.0, "A"), arrival(2, 400, 4000.0, "B")]
        self.assertTrue(is_complete(INSTRUCTION, entries))

    def test_float_tail_does_not_block_settle(self):
        entries = [arrival(1, 1000, 9999.999, "A")]
        self.assertTrue(is_complete(INSTRUCTION, entries))

    def test_single_entry_over_remaining_is_rejected_whole(self):
        entries = [arrival(1, 600, 6000.0, "A")]
        # 数量超出剩余400
        with self.assertRaises(ValidationError):
            check_register(INSTRUCTION, entries, EntryDraft(401, 3000.0, "B", "张三"))
        # 金额超出剩余4000
        with self.assertRaises(ValidationError):
            check_register(INSTRUCTION, entries, EntryDraft(300, 4000.01, "B", "张三"))
        # 恰好不超，允许
        check_register(INSTRUCTION, entries, EntryDraft(400, 4000.0, "B", "张三"))

    def test_duplicate_source_ref_counts_once(self):
        entries = [arrival(1, 600, 6000.0, "SRC-1")]
        with self.assertRaises(Conflict):
            check_register(INSTRUCTION, entries, EntryDraft(100, 1000.0, "SRC-1", "李四"))

    def test_terminal_instructions_reject_registration(self):
        draft = EntryDraft(100, 1000.0, "X", "张三")
        with self.assertRaises(Conflict):
            check_register({**INSTRUCTION, "status": STATUS_SETTLED}, [], draft)
        with self.assertRaises(Conflict):
            check_register({**INSTRUCTION, "status": STATUS_REVERSED}, [], draft)

    def test_register_summary_shows_shortage(self):
        entries = [arrival(1, 600, 6000.0, "A")]
        self.assertIn("处理中", register_summary(INSTRUCTION, entries, 2))
        self.assertIn("100", register_summary(INSTRUCTION, [arrival(1, 1000, 10000.0, "A")], 1))
        self.assertIn("结清", register_summary(INSTRUCTION, [arrival(1, 1000, 10000.0, "A")], 1))

    def test_reverse_keeps_detail_and_returns_remaining(self):
        entries = [arrival(1, 600, 6000.0, "A")]
        returned = build_return_entry(INSTRUCTION, entries)
        self.assertEqual(returned["entry_type"], ENTRY_RETURN)
        self.assertEqual(returned["quantity"], 400)
        self.assertEqual(returned["amount"], 4000.0)
        # 已冲正不能再冲正
        with self.assertRaises(Conflict):
            validate_reverse({**INSTRUCTION, "status": STATUS_REVERSED}, {"reverse_reason": "x"})
        # 冲正原因必填
        with self.assertRaises(ValidationError):
            validate_reverse(INSTRUCTION, {})


if __name__ == "__main__":
    unittest.main()
