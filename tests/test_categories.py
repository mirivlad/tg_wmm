"""Category management: preserve transactions, user isolation, classifier rules."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from wmm.db import Database
from wmm.parser import parse_expenses

NOW = datetime(2026, 10, 28, 0, 0, tzinfo=timezone.utc)


class CategoryManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.filename = str(Path(self.temp.name) / "wmm.db")
        self.db = Database(self.filename)
        self.db.initialize()
        self.db.ensure_user(7)
        self.db.ensure_user(8)

    def expense(self, description: str, message_id: int = 1):
        sources, default = self.db.source_aliases(7)
        items = parse_expenses(description, sources, default, self.db.learned_aliases(7), now=NOW)
        return self.db.save_expenses(7, 7, message_id, items, description)[0]

    def category_id(self, name: str) -> int:
        return next(r["id"] for r in self.db.categories(7) if r["name"] == name)

    def test_alcohol_category_migrates_only_once(self):
        category_id = self.category_id("Алкоголь")
        with self.db.connection() as conn:
            conn.execute("DELETE FROM category_rules WHERE category_id=? AND user_id=?", (category_id, 7))
            conn.execute("DELETE FROM categories WHERE id=? AND user_id=?", (category_id, 7))
            conn.execute("PRAGMA user_version=1")
        self.db.initialize()
        self.assertIn("Алкоголь", self.db.category_names(7))
        self.db.delete_category(7, self.category_id("Алкоголь"))
        self.db.initialize()
        self.assertNotIn("Алкоголь", self.db.category_names(7))

    def test_rename_builtin_keeps_auto_assignment_after_restart(self):
        groceries_id = self.category_id("Продукты")
        self.assertTrue(self.db.rename_category(7, groceries_id, "Питание дома"))
        first = self.expense("еда 300", 1)
        self.assertEqual(self.db.operation(7, first)["category_name"], "Питание дома")
        self.db.initialize()
        self.db.ensure_user(7)
        second = self.expense("молоко 200", 2)
        self.assertEqual(self.db.operation(7, second)["category_name"], "Питание дома")
        self.assertNotIn("Продукты", self.db.category_names(7))
        self.assertEqual(self.db.category(7, groceries_id)["uses"], 2)

    def test_delete_category_moves_expenses_to_other(self):
        a = self.expense("кофе 100", 1)
        b = self.expense("латте 200", 2)
        drinks = self.category_id("Кофе и напитки")
        self.assertEqual(self.db.delete_category(7, drinks), 2)
        self.assertEqual(self.db.operation(7, a)["category_name"], "Прочее")
        self.assertEqual(self.db.operation(7, b)["category_name"], "Прочее")
        self.assertIsNone(self.db.category(7, drinks))
        self.db.initialize()
        self.db.ensure_user(7)
        self.assertNotIn("Кофе и напитки", self.db.category_names(7))
        c = self.expense("кофе 99", 3)
        self.assertEqual(self.db.operation(7, c)["category_name"], "Прочее")
        self.assertEqual(len(self.db.history(7)), 3)

    def test_create_and_assign_new_category(self):
        record = self.expense("чудо 400")
        category_id = self.db.create_and_assign_category(7, record, "  Коллекции ")
        self.assertEqual(self.db.operation(7, record)["category_name"], "Коллекции")
        self.assertEqual(self.db.learned_aliases(7)["чудо"], "Коллекции")
        self.assertEqual(category_id, self.db.create_and_assign_category(7, record, "коллекции"))
        self.assertEqual(len([r for r in self.db.categories(7) if r["name"] == "Коллекции"]), 1)

    def test_name_edit_does_not_touch_raw_or_amount(self):
        record = self.expense("сиги 210")
        self.assertTrue(self.db.update_description(7, record, "Сигареты, пачка"))
        row = self.db.operation(7, record)
        self.assertEqual(row["description"], "Сигареты, пачка")
        self.assertEqual(row["original_text"], "сиги 210")
        self.assertEqual(row["amount_minor"], 21000)

    def test_button_owner_isolation_database(self):
        foreign_record = self.expense("еда 500")
        foreign_cat = self.category_id("Продукты")
        self.assertFalse(self.db.assign_category(8, foreign_record, foreign_cat))
        self.assertIsNone(self.db.create_and_assign_category(8, foreign_record, "Чужая категория"))
        self.assertFalse(self.db.rename_category(8, foreign_cat, "Чужая"))
        self.assertIsNone(self.db.delete_category(8, foreign_cat))
        self.assertIsNone(self.db.operation(8, foreign_record))
        self.assertNotIn("Чужая категория", self.db.category_names(8))

    def test_cannot_remove_or_rename_other(self):
        other = self.category_id("Прочее")
        with self.assertRaises(ValueError):
            self.db.rename_category(7, other, "Другое")
        with self.assertRaises(ValueError):
            self.db.delete_category(7, other)

    def test_assign_category_learns_expense(self):
        op = self.expense("непонятка 200")
        target = self.category_id("Транспорт")
        self.assertTrue(self.db.assign_category(7, op, target))
        self.assertEqual(self.db.operation(7, op)["category_name"], "Транспорт")
        self.assertEqual(self.db.learned_aliases(7)["непонятка"], "Транспорт")

    def test_category_delete_does_not_remove_other_users(self):
        self.db.add_category(8, "Вторая категория")
        name_id = self.category_id("Табак")
        self.db.delete_category(7, name_id)
        self.assertIn("Табак", self.db.category_names(8))
        self.assertIn("Вторая категория", self.db.category_names(8))


if __name__ == "__main__":
    unittest.main()
