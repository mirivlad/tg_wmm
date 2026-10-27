"""Test inline button payloads without contacting Telegram."""
import tempfile
import unittest
from pathlib import Path

from wmm.db import Database
from wmm.menus import category_detail, category_manager, category_picker, edit_menu, receipt_keyboard


class InlineKeyboardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(str(Path(tmp.name) / "data.sqlite"))
        self.db.initialize()
        self.db.ensure_user(42)

    @staticmethod
    def payloads(markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    def test_receipt_offer_edit_for_every_record(self):
        markup = receipt_keyboard("42:90", [(5, "Кофе"), (6, "Сигареты")])
        self.assertEqual(
            self.payloads(markup),
            ["ui:edit:5", "ui:edit:6", "undo:42:90", "report:month"],
        )

    def test_picker_supports_pages_and_creation(self):
        msg, markup = category_picker(self.db, 42, 7, 0)
        self.assertIn("ui:new:7", self.payloads(markup))
        self.assertIn("ui:select:7:1", self.payloads(markup))
        self.assertTrue(any(x.startswith("ui:pick:7:") for x in self.payloads(markup)))

    def test_category_management(self):
        msg, markup = category_manager(self.db, 42, 0)
        self.assertIn("ui:make:0", self.payloads(markup))
        self.assertTrue(any(x.startswith("ui:manage:") for x in self.payloads(markup)))
        other = next(x for x in self.db.categories(42) if x["name"] == "Прочее")
        _, detail = category_detail(other, 0)
        self.assertFalse(any("remove" in x for x in self.payloads(detail)))

    def test_menu_available_for_real_expense(self):
        from wmm.parser import parse_expenses
        aliases, default = self.db.source_aliases(42)
        item = parse_expenses("кофе 130", aliases, default)
        [record_id] = self.db.save_expenses(42, 42, 10, item, "кофе 130")
        text, markup = edit_menu(self.db.operation(42, record_id))
        self.assertIn("Название", text)
        self.assertIn(f"ui:field:n:{record_id}", self.payloads(markup))
        self.assertIn(f"ui:field:a:{record_id}", self.payloads(markup))
        self.assertIn(f"ui:select:{record_id}:0", self.payloads(markup))


if __name__ == "__main__":
    unittest.main()
