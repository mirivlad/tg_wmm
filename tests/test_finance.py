"""Run: python -m unittest discover -s tests -v (no Telegram token needed)."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from wmm.db import Database
from wmm.parser import ParseError, category_for, minor, parse_expenses, parse_repayment

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.sources = {"основной": 1, "кредитка1": 2, "к1": 2}

    def test_chat_example(self):
        text = "сигареты 2 пачки 420. еда 300, сиги 210, обед 130, семки 100, кофе 130, кредит 5000, насте 500, заправка 3500"
        entries = parse_expenses(text, self.sources, 1, now=NOW)
        self.assertEqual(len(entries), 9)
        self.assertEqual(sum(e.amount_minor for e in entries), 1029000)
        self.assertEqual([x.category for x in entries],
                         ["Табак", "Продукты", "Табак", "Кафе и рестораны", "Снеки",
                          "Кофе и напитки", "Кредиты", "Переводы и подарки", "Топливо"])

    def test_decimal_comma_is_not_separator(self):
        items = parse_expenses("чай 130,50, кофе 200.25", self.sources, 1, now=NOW)
        self.assertEqual([x.amount_minor for x in items], [13050, 20025])

    def test_source_suffix_and_prefix(self):
        items = parse_expenses("заправка 3500 кредитка1; к1 обед 300", self.sources, 1, now=NOW)
        self.assertEqual([x.source_id for x in items], [2, 2])

    def test_yesterday(self):
        item = parse_expenses("вчера обед 150", self.sources, 1, now=NOW)[0]
        self.assertEqual(item.occurred_at[:10], "2026-10-26")

    def test_incomplete_batch_is_rejected(self):
        with self.assertRaises(ParseError):
            parse_expenses("кофе 130, молоко", self.sources, 1, now=NOW)

    def test_unknown_category(self):
        item = parse_expenses("неизвестная вещь 700", self.sources, 1, now=NOW)[0]
        self.assertEqual(item.category, "Прочее")
        self.assertTrue(item.unclassified)

    def test_learned_category(self):
        self.assertEqual(category_for("уточнение", {"уточнение": "Медицина"}), ("Медицина", False))

    def test_minor(self):
        self.assertEqual(minor("9 000,90"), 900090)
        with self.assertRaises(ParseError):
            minor("0")

    def test_unit_price_not_assumed(self):
        with self.assertRaises(ParseError):
            parse_expenses("сигареты 2 пачки по 210", self.sources, 1, now=NOW)

    def test_repayment_breakdown(self):
        repay = parse_repayment("погасил кредитка1 9000, зачислилось 6000, остальное проценты",
                                self.sources, 1, now=NOW)
        self.assertEqual((repay.amount_minor, repay.principal_minor, repay.interest_minor),
                         (900000, 600000, 300000))
        self.assertEqual(repay.target_source_id, 2)

    def test_repayment_requires_allocation(self):
        with self.assertRaises(ParseError):
            parse_repayment("погасил кредитка1 9000", self.sources, 1, now=NOW)

    def test_repayment_rejects_inconsistent_values(self):
        with self.assertRaises(ParseError):
            parse_repayment("погасил кредитка1 9000, зачислилось 6000, проценты 4000",
                            self.sources, 1, now=NOW)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(str(Path(self.temp.name) / "data" / "wmm.db"))
        self.db.initialize()
        self.db.ensure_user(123)
        self.db.ensure_user(321)

    def test_initial_source_and_categories(self):
        sources, default = self.db.source_aliases(123)
        self.assertEqual(sources["основной"], default)
        self.assertIn("Табак", self.db.category_names(123))

    def test_atomic_batch_idempotency_and_undo(self):
        aliases, default = self.db.source_aliases(123)
        items = parse_expenses("кофе 130, заправка 3500", aliases, default, now=NOW)
        op_ids = self.db.save_expenses(123, 10, 20, items, "кофе 130, заправка 3500")
        self.assertEqual(len(op_ids), 2)
        self.assertEqual(self.db.save_expenses(123, 10, 20, items, "same"), [])
        self.assertEqual(len(self.db.history(123)), 2)
        self.assertEqual(self.db.undo(123), 2)
        self.assertEqual(len(self.db.history(123)), 0)

    def test_user_isolation(self):
        aliases, default = self.db.source_aliases(123)
        items = parse_expenses("еда 300", aliases, default, now=NOW)
        [op] = self.db.save_expenses(123, 10, 20, items, "еда 300")
        self.assertFalse(self.db.delete(321, op))
        self.assertFalse(self.db.update_amount(321, op, 90000))
        self.assertEqual(self.db.history(321), [])
        self.assertEqual(self.db.delete_batch(321, "10:20"), 0)

    def test_correction_learns_alias(self):
        aliases, default = self.db.source_aliases(123)
        items = parse_expenses("заморская штуковина 500", aliases, default, now=NOW)
        [op] = self.db.save_expenses(123, 10, 20, items, "заморская штуковина 500")
        self.assertTrue(self.db.update_category(123, op, "Продукты"))
        self.assertEqual(self.db.learned_aliases(123)["заморская штуковина"], "Продукты")
        self.assertTrue(self.db.update_amount(123, op, 55000))
        self.assertEqual(self.db.history(123)[0]["amount_minor"], 55000)

    def test_credit_principal_not_counted_as_expense(self):
        self.db.add_source(123, "Кредитка 1", "credit", ["кредитка1"])
        aliases, default = self.db.source_aliases(123)
        expense = parse_expenses("еда 3000 кредитка1", aliases, default, now=NOW)
        self.db.save_expenses(123, 10, 20, expense, "еда 3000 кредитка1")
        repay = parse_repayment("погасил кредитка1 9000, зачислилось 6000",
                                aliases, default, now=NOW)
        op = self.db.save_repayment(123, 10, 21, repay)
        self.assertIsNotNone(op)
        self.assertIsNone(self.db.save_repayment(123, 10, 21, repay))
        report = self.db.report(123, "2026-10-27T00:00:00+00:00", "2026-10-28T00:00:00+00:00")
        self.assertEqual(report["expenses"], 600000)  # 3000 purchase + 3000 interest
        self.assertEqual(report["cash_out"], 900000)  # only debit payment; card purchase increases debt
        self.assertEqual(report["repayments"], 900000)
        self.assertEqual(dict(report["categories"])["Проценты и комиссии"], 300000)

    def test_rollback_if_invalid_category(self):
        aliases, default = self.db.source_aliases(123)
        items = parse_expenses("кофе 130, еда 300", aliases, default, now=NOW)
        altered = [items[0], items[1].__class__(items[1].description, items[1].amount_minor,
                   items[1].source_id, "Несуществующая категория", items[1].occurred_at,
                   items[1].raw, True)]
        with self.assertRaises(ValueError):
            self.db.save_expenses(123, 10, 30, altered, "oops")
        self.assertEqual(self.db.history(123), [])

    def test_csv_export(self):
        aliases, default = self.db.source_aliases(123)
        items = parse_expenses("еда 300", aliases, default, now=NOW)
        self.db.save_expenses(123, 10, 20, items, "еда 300")
        result = self.db.export_csv(123).decode("utf-8-sig")
        self.assertIn("amount_rub", result)
        self.assertIn("300.00", result)


if __name__ == "__main__":
    unittest.main()
