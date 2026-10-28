"""Regression: long receipt, payment vs. pre-discount total, misread fiscal metadata."""
import unittest
from datetime import datetime, timezone

from wmm.receipts import from_text, ocr_total, parse_items

# Sanitized fragments representing OCR errors from an actual Komandor receipt.
OCR_SAMPLE = """
НАС 10% Сухарики сухарев 100г Бородинские с чесноком
359.99*0.250КГ=90.00
НДС 22% Кове Бависта Про Бар зерно 1000г W/V: 6
АКЦИЗ 1499. 99*1UT.=1499.99
НДС 22% ВИНО Санта Каролина селлар
НДС 10% Булочка детство с сахаром 55г Командор 12.99*6шт.=77.94
НДС 22% Пакет-майка ПНД с логотипом Аллея 10.00*2шт.=20.00
СУнНа 583 СКИДКИ: 2947.92
БАНКОВСКИЕ ОПЛАТЫ
28.10.26 18:38 ЧЕК 0178
MIR PAY
CYMMaCPYE): 2387.92
КОМИССИЯ ЗА ОПЕРАЦИЮ 0 РУБ
БЕЗНАЛИЧНЫМИ 2387.92
СУННа НДС 22 400.33
"""


class RealReceiptRegressionTests(unittest.TestCase):
    def test_payment_amount_preferred_to_before_discount(self):
        self.assertEqual(ocr_total(OCR_SAMPLE), 238792)

    def test_never_classify_payment_or_discount_lines_as_items(self):
        items = parse_items(OCR_SAMPLE)
        joined = "\n".join(item.name for item in items).casefold()
        for bad in ("сунна", "cyhmacpye", "безналичными", "акциз", "комиссия", "итого"):
            self.assertNotIn(bad, joined)
        self.assertTrue(any("булочка" in x.name.casefold() for x in items))

    def test_receipt_date_from_printed_text_when_qr_unreadable(self):
        receipt = from_text(b"receipt-image", "", OCR_SAMPLE, "Asia/Irkutsk",
                            now=datetime(2026, 10, 28, 18, 50, tzinfo=timezone.utc))
        self.assertFalse(receipt.fiscal)
        self.assertEqual(receipt.total_minor, 238792)
        self.assertEqual(receipt.occurred_at, "2026-10-28T10:38:00+00:00")

    def test_corrupted_payment_label_alone_is_not_trusted(self):
        self.assertIsNone(ocr_total("CyHMaCPYE): 2387.52"))

    def test_no_payment_marker_no_auto_inferred_total(self):
        bad = "Сумма без скидки: 2947.92\nКОМИССИЯ 0.00\nМОЛОКО 300.00"
        self.assertIsNone(ocr_total(bad))

    def test_conflicting_tops_do_not_auto_choose(self):
        text = "ИТОГО 250.00\nИТОГО 260.00"
        self.assertIsNone(ocr_total(text))


if __name__ == "__main__":
    unittest.main()
