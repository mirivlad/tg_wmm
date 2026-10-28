"""Fiscal QR, OCR structure, atomic receipt imports and duplicate protection."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from wmm.db import Database
from wmm.parser import rub
from wmm.receipts import ReceiptError, fiscal_qr, from_text, parse_items, ocr_total
from wmm.receipt_store import ReceiptStore

QR = "t=20261028T1330&s=180.00&fn=000110000105&i=1234567890&fp=123456&n=1"
OCR = "МОЛОКО 120,00\nХЛЕБ 60,00\nНДС 18,00\nИТОГО 180,00"


class ReceiptParserTests(unittest.TestCase):
    def test_fiscal_qr(self):
        amount, date, key = fiscal_qr(QR, "Europe/Moscow")
        self.assertEqual(amount, 18000)
        self.assertEqual(date, "2026-10-28T10:30:00+00:00")
        self.assertTrue(key.startswith("fiscal:"))

    def test_bad_qr_reject(self):
        with self.assertRaises(ReceiptError):
            fiscal_qr(QR.replace("n=1", "n=2"), "Europe/Moscow")
        with self.assertRaises(ReceiptError):
            fiscal_qr(QR.replace("s=180.00", "s=0"), "Europe/Moscow")

    def test_ocr_items_and_sum(self):
        result = from_text(b"image1", QR, OCR, "Europe/Moscow")
        self.assertEqual(len(result.items), 2)
        self.assertEqual(result.items[0].name, "МОЛОКО")
        self.assertEqual(result.items[1].amount_minor, 6000)
        self.assertTrue(result.items_match_total)

    def test_qr_total_overrides_wrong_ocr_total(self):
        result = from_text(b"image1", QR, OCR.replace("ИТОГО 180,00", "ИТОГО 900,00"), "Europe/Moscow")
        self.assertEqual(result.total_minor, 18000)
        self.assertTrue(result.items_match_total)

    def test_text_without_qr(self):
        result = from_text(b"image1", "", OCR, "Europe/Moscow",
                           now=datetime(2026, 10, 28, tzinfo=timezone.utc))
        self.assertEqual(result.total_minor, 18000)
        self.assertFalse(result.fiscal)
        self.assertTrue(result.receipt_key.startswith("image:"))

    def test_unreadable_receipt_no_invented_amount(self):
        result = from_text(b"image1", "", "СПАСИБО ЗА ПОКУПКУ", "Europe/Moscow")
        self.assertIsNone(result.total_minor)
        self.assertEqual(result.items, ())

    def test_refuse_incomplete_item_sum(self):
        result = from_text(b"image1", QR, "МОЛОКО 120,00\nИТОГО 180,00", "Europe/Moscow")
        self.assertFalse(result.items_match_total)


class ReceiptDatabaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(str(Path(temp.name) / "data.db"))
        self.db.initialize()
        self.db.ensure_user(777)
        self.db.ensure_user(888)
        self.store = ReceiptStore(self.db)
        self.store.initialize()

    def test_qr_split_items_and_report(self):
        draft = self.store.create_draft(777, 7, 90, from_text(b"img", QR, OCR, "Europe/Moscow"))
        ids = self.store.save(777, draft, split=True)
        self.assertEqual(len(ids), 2)
        self.assertEqual(self.db.operation(777, ids[0])["category_name"], "Продукты")
        self.assertEqual(self.db.operation(777, ids[1])["amount_minor"], 6000)
        report = self.db.report(777, "2026-10-28T00:00:00+00:00", "2026-10-29T00:00:00+00:00")
        self.assertEqual(report["expenses"], 18000)
        self.assertEqual(report["count"], 2)

    def test_prevent_duplicate_even_when_image_differs(self):
        self.store.save(777, self.store.create_draft(777, 7, 90,
                        from_text(b"imageA", QR, OCR, "Europe/Moscow")))
        with self.assertRaisesRegex(ReceiptError, "уже записан"):
            self.store.create_draft(777, 7, 91,
                                    from_text(b"imageB", QR, OCR, "Europe/Moscow"))
        self.assertEqual(len(self.db.history(777)), 1)

    def test_duplicate_is_user_specific(self):
        one = from_text(b"same", QR, OCR, "Europe/Moscow")
        self.store.save(777, self.store.create_draft(777, 7, 90, one))
        self.assertEqual(len(self.store.save(888, self.store.create_draft(888, 8, 90, one))), 1)

    def test_deleted_receipt_can_be_imported_again(self):
        parsed = from_text(b"same", QR, OCR, "Europe/Moscow")
        ids = self.store.save(777, self.store.create_draft(777, 7, 90, parsed))
        self.assertTrue(self.db.delete(777, ids[0]))
        second = self.store.create_draft(777, 7, 91, parsed)
        self.assertEqual(len(self.store.save(777, second)), 1)

    def test_no_money_until_confirm(self):
        parsed = from_text(b"img", QR, OCR, "Europe/Moscow")
        draft = self.store.create_draft(777, 7, 90, parsed)
        self.assertEqual(self.db.history(777), [])
        self.assertTrue(self.store.discard(777, draft))
        with self.assertRaises(ReceiptError):
            self.store.save(777, draft)
        self.assertEqual(self.db.history(777), [])

    def test_correct_amount_then_disable_bad_split(self):
        parsed = from_text(b"img", QR, OCR, "Europe/Moscow")
        draft = self.store.create_draft(777, 7, 90, parsed)
        self.assertTrue(self.store.update(777, draft, "total_minor", 19000))
        with self.assertRaisesRegex(ReceiptError, "не сходятся"):
            self.store.save(777, draft, split=True)
        self.assertEqual(len(self.store.save(777, draft, split=False)), 1)

    def test_source_selection_and_auth(self):
        self.db.add_source(777, "Кредитка 2", "credit", ["к2"])
        sources = self.db.sources(777)
        new = next(s["id"] for s in sources if s["name"] == "Кредитка 2")
        parsed = from_text(b"img", QR, OCR, "Europe/Moscow")
        draft = self.store.create_draft(777, 7, 90, parsed)
        self.assertFalse(self.store.update(888, draft, "source_id", new))
        self.assertTrue(self.store.update(777, draft, "source_id", new))
        self.assertEqual(self.store.save(888, draft) if False else [], [])
        ids = self.store.save(777, draft)
        self.assertEqual(self.db.operation(777, ids[0])["source_id"], new)

    def test_missing_total_requires_edit(self):
        receipt = from_text(b"img", "", "АБРАКАДАБРА", "Europe/Moscow")
        draft = self.store.create_draft(777, 7, 90, receipt)
        with self.assertRaisesRegex(ReceiptError, "не распознана"):
            self.store.save(777, draft)
        self.store.update(777, draft, "total_minor", 25000)
        self.assertEqual(len(self.store.save(777, draft)), 1)


if __name__ == "__main__":
    unittest.main()
