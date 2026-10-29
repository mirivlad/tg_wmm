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

    def test_modern_ocr_split_product_and_price_lines(self):
        """PP-OCRv5 detects product name and amount as separate lines."""
        from wmm.receipts import parse_items
        lines = """НдС 10% Сухарики ржаные с чесноком
359.99*0.250KГ=90.00
НДС 22% Кофе Бариста Про зерно 1000г
1499.99*1WТ.=1499.99
НДС 22% Вино Санта Каролина
699.99*1ШТ.=699.99
НДС 10% Булочка Детство с сахаром 55г
12.99*6ШТ.=77.94
НДС 22% Пакет-майка 23мк
10.00*2ШТ.=20.00
ИТОГО 2387.92"""
        items = parse_items(lines)
        self.assertEqual(len(items), 5)
        self.assertEqual([x.amount_minor for x in items], [9000, 149999, 69999, 7794, 2000])
        self.assertEqual(sum(x.amount_minor for x in items), 238792)
        self.assertIn("Сухарики", items[0].name)
        self.assertIn("Кофе", items[1].name)

    def test_real_ocr_does_not_assign_cracker_price_to_coffee(self):
        """Actual scan: 90 rub belongs to unseen crackers, NOT the coffee."""
        from wmm.receipts import parse_items
        ocr = """НДС 22% КОФе Бариста Про Бар зернО 100Ог м/У;6
359.99*0.250KG=90.00
1499.99*1ШT.=1499.99
АКЦИ3
66669
Х H о H   е
11.5-12% 0.75л
699.99*1т.=699.99
ндс 10% Булочка детство с сахаром 55г командор 12.9жшт, 77.94
ндс 22% пакет-майка пнд 38+1860 23мк с логотипом аллея: 1500
10.00*2шT.=20.00"""
        items = parse_items(ocr)
        # With one visible product header and two prices, the association is
        # ambiguous. It is safer to omit the item than invent a price.
        self.assertFalse(any("КОФе" in x.name for x in items))
        self.assertNotIn(9000, [x.amount_minor for x in items])
        self.assertNotIn(69999, [x.amount_minor for x in items])  # wine name missing

    def test_real_two_header_two_price_run_restores_all_five_items(self):
        """Actual saved alternate OCR contains 5 products and 5 amount formulas."""
        sample = """НAC 10% СУХАРИКИ
CхаpeBb
ржаные
Бородинские с чесноком <пл
aстинa); 3
НДС 22% КОФе Бариста Про Бар зерно 100 м6
359.99*0.250KG=90.00
1499.99*1ШT.=1499.99
АКЦИ3
699.99
НДс 22% Вино Санта Каролина Селлар
Селекшн Совиньон Блан бел сух
11.5-12% 0.75n
699.99*1ШT.=699.99
НДС 10% БУлОчка
Детство с сахаром 55г Командор
р 12.99*6Т.=77.94
Ндс 22% пакет-майка пнд 38+18 2мк с логотипом алле 1500
10.00*2WT.=20.00
Сумма без скидки:
2947.92"""
        items = parse_items(sample)
        self.assertEqual([x.amount_minor for x in items],
                         [9000, 149999, 69999, 7794, 2000])
        self.assertEqual(sum(x.amount_minor for x in items), 238792)
        self.assertTrue(items[0].name.casefold().startswith("сухарики"))
        self.assertIn("КОФе", items[1].name)
        self.assertIn("Вино", items[2].name)

    def test_do_not_invent_when_price_lines_missing(self):
        sample = """НАС 10% Сухарики
НДС 22% Кофе 1000г
1499.99*1ШТ.=1499.99"""
        self.assertEqual(parse_items(sample), ())

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

    def test_latest_ocr_wine_not_lost_after_bun_inline_price(self):
        """Derived from newest OCR: wine formula appears after bun with inline price."""
        sample = """НДС 10% Сухарики ржаные с чесноком
НДС 22% Кофе зерно 1000г
359.99*0.250Кг=90.00
1499.99*1ШТ.=1499.99
НДС 2% Вино Совиньон Блан белое сухое
11.5-12% 0.75л
НДС 10% Булочка с сахаром 12.99жт,77.94
699.99*1ШТ.=699.99
НДС 22% Пакет-майка 23мк
10.00*2ШТ.=20.00
Сумма без скидки: 2947.92"""
        self.assertEqual([x.amount_minor for x in parse_items(sample)],
                         [9000, 149999, 69999, 7794, 2000])

    def test_no_payment_marker_no_auto_inferred_total(self):
        bad = "Сумма без скидки: 2947.92\nКОМИССИЯ 0.00\nМОЛОКО 300.00"
        self.assertIsNone(ocr_total(bad))

    def test_conflicting_tops_do_not_auto_choose(self):
        text = "ИТОГО 250.00\nИТОГО 260.00"
        self.assertIsNone(ocr_total(text))


if __name__ == "__main__":
    unittest.main()
