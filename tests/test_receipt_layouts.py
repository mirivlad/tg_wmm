"""Cross-layout OCR tests: rules must not depend on any particular shop."""
import unittest

from wmm.receipts import parse_items

class ReceiptLayoutTests(unittest.TestCase):
    def assert_items(self, text, amounts):
        self.assertEqual([x.amount_minor for x in parse_items(text)], amounts)

    def test_plain_item_and_price_on_same_line(self):
        self.assert_items("Молоко 120,00\nХлеб 60,00\nИТОГО 180,00",
                          [12000, 6000])

    def test_vat_label_and_price_formula_on_next_line(self):
        self.assert_items("НДС 10% Молоко пастеризованное\n120.00*1ШТ.=120.00\n"
                          "НДС 22% Салфетки бумажные\n60.00*1ШТ.=60.00",
                          [12000, 6000])

    def test_two_item_headers_then_two_price_lines(self):
        self.assert_items("НДС 10% Гречка ядрица\nНДС 10% Огурцы\n"
                          "45.00*1ШТ.=45.00\n65.00*2ШТ.=130.00",
                          [4500, 13000])

    def test_invalid_vat_ocr_2_percent_and_latin_lookalikes(self):
        self.assert_items("НДС 2% Яблочный сок\n99.90*1ШT.=99.90\n"
                          "HAC 1O% Буханка хлеба\n49.00*1ШT.=49.00",
                          [9990, 4900])

    def test_inline_product_total_and_later_product_formula(self):
        self.assert_items("НДС 22% Чай зелёный\n"
                          "НДС 10% Булочка с маком 12.50*2ШТ.=25.00\n"
                          "230.00*1ШТ.=230.00",
                          [23000, 2500])

    def test_inline_price_without_formula_and_spilled_other_formula(self):
        self.assert_items("НДС 22% Шампунь\n"
                          "НДС 10% Сметана 20% 250г 149.99\n"
                          "390.00*1ШТ.=390.00",
                          [39000, 14999])

    def test_wrapped_vat_product_name(self):
        items = parse_items("НДС 20% Средство для мытья\n"
                            "посуды аромат лимона\n"
                            "199.00*1ШТ.=199.00")
        self.assertEqual(len(items), 1)
        self.assertIn("аромат лимона", items[0].name)
        self.assertEqual(items[0].amount_minor, 19900)

    def test_missing_price_rejects_ambiguous_headers(self):
        self.assert_items("НДС 10% Йогурт\nНДС 10% Творог\n"
                          "120.00*1ШТ.=120.00", [])

    def test_extra_price_does_not_shift_one_header(self):
        self.assert_items("НДС 10% Йогурт\n"
                          "120.00*1ШТ.=120.00\n70.00*1ШТ.=70.00", [])

    def test_missing_header_does_not_assign_to_neighbor(self):
        self.assert_items("НДС 22% Чай в упаковке\n"
                          "10.00*0.250КГ=2.50\n"
                          "220.00*1ШТ.=220.00", [])

    def test_payment_and_discount_not_products(self):
        self.assert_items("НДС 10% Макароны\n80.00*1ШТ.=80.00\n"
                          "Сумма без скидки: 100.00\nИТОГО 80.00\n"
                          "БЕЗНАЛИЧНЫМИ 80.00\nКомиссия 0.00", [8000])

    def test_arbitrary_product_name_not_dictionary_based(self):
        self.assert_items("НДС 10% Неизвестный продукт Zeta-47\n"
                          "333.30*1ШТ.=333.30", [33330])

    def test_mixed_decimal_comma(self):
        self.assert_items("НДС 10% Конфеты весовые\n"
                          "400,00*0,250КГ=100,00", [10000])

    def test_receipt_no_vat_labels_with_inline_formula(self):
        self.assert_items("Крупа Пшеничная 89.90*1ШТ.=89.90\n"
                          "Шоколад молочный 120.00*1ШТ.=120.00",
                          [8990, 12000])

if __name__ == "__main__":
    unittest.main()
