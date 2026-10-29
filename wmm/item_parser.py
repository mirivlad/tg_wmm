"""Reconstruct receipt item lines without assuming that prices follow their names.

OCR can emit multiple product headers followed by a run of price formulas;
it can also shift a formula past an unrelated inline-priced product. We
therefore collect independent product headers and total-price formulas, then
align only when counts match. Incomplete or ambiguous groups are rejected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .receipt_heuristics import VAT_PREFIX, canonical, clean_item_name, is_service_line

_MONEY = re.compile(r"(?<!\d)(\d{1,7}(?:[ \u00a0]\d{3})*[,.]\d{2})(?!\d)")
_FORMULA = re.compile(r"\d[\d,. ]*\s*[*×xхж]\s*\d", re.I)
_EQUALS_TOTAL = re.compile(r"=\s*\d[\d,. ]*[,.]\d{2}\s*(?:₽|руб\.?)?\s*$", re.I)
_WORD = re.compile(r"[А-Яа-яЁёA-Za-z]{3,}")
_PAYMENT_SECTION = re.compile(
    r"^(?:СУММА\s+(?:БЕЗ\s+СКИДКИ|ЧЕКА)|"
    r"БАНКОВСКИЕ|БЕЗНАЛИЧНЫМИ|ИТОГО\b|К\s+ОПЛАТЕ\b|"
    r"ПРИХОД\b|КАССОВЫЙ\s+ЧЕК\b)",
    re.I,
)
_BAD_CONTINUATION = re.compile(
    r"^(?:АКЦИЗ|АКЦИ[З3]|КОПИЛКА|ИНФОЦЕНТР|НДС|НАС|ИНН|ФН|ФД|ФП|"
    r"СУММА|ТЕРМИНАЛ|ККТ|БАНКОВСКИЕ|ОПЛАТА|ЧЕК)\b",
    re.I,
)


@dataclass
class _Product:
    name: str
    amount: int | None = None


def parse_lines(text: str, money_to_minor: Callable[[str], int],
                max_items: int = 25) -> list[tuple[str, int]]:
    products: list[_Product] = []
    free_formulas: list[int] = []
    current: _Product | None = None
    saw_item = False

    for raw in text.splitlines():
        line = " ".join(raw.strip().split()).strip("-–* ")
        if not line:
            continue
        folded = canonical(line)
        if saw_item and _PAYMENT_SECTION.match(folded):
            break

        # VAT may be distorted: "НАС 10%" or "НДС 2%" for "НДС 22%".
        # A printed 10% can become "1O%" with Latin/Cyrillic O in OCR.
        normalized_vat = re.sub(r"(?<=\d)[ОO](?=\s*%)", "0", folded)
        match = VAT_PREFIX.match(normalized_vat)
        vat = match is not None
        cleaned = line[match.end():].strip() if match else line
        cleaned = (cleaned if cleaned[:1].isdigit() else clean_item_name(cleaned))
        if not cleaned or is_service_line(cleaned):
            current = None
            continue

        amounts = list(_MONEY.finditer(cleaned))
        is_formula = (bool(_FORMULA.search(cleaned)) and
                      bool(_EQUALS_TOTAL.search(cleaned)))
        if is_formula and amounts:
            try:
                value = money_to_minor(amounts[-1].group(1))
            except ValueError:
                current = None
                continue
            before = cleaned[:amounts[0].start()].strip(" .:;–-")
            if vat and _WORD.search(before):
                products.append(_Product(before[:160], value))
            elif (before and len(before) > 3 and _WORD.search(before)
                  and not is_service_line(before)):
                # A shop may show a product and its formula on the same
                # line without VAT annotations.
                products.append(_Product(before[:160], value))
            elif saw_item:
                free_formulas.append(value)
            current = None
            saw_item = True
            continue

        if vat and _WORD.search(cleaned):
            saw_item = True
            if amounts:
                # Inline-priced product is independent of nearby formula runs.
                last = amounts[-1]
                name = cleaned[:last.start()].strip(" .:;-–")
                if _WORD.search(name):
                    try:
                        products.append(_Product(name[:160], money_to_minor(last.group(1))))
                    except ValueError:
                        pass
                current = None
            else:
                current = _Product(cleaned[:160])
                products.append(current)
            continue

        if amounts:
            last = amounts[-1]
            # Ordinary line "МОЛОКО 120,00" without VAT.
            leading = cleaned[:last.start()].strip(" .:;–-")
            trailing = cleaned[last.end():].strip(" ₽р.уб")
            if (_WORD.search(leading) and not trailing and
                    not is_service_line(leading) and not _BAD_CONTINUATION.match(canonical(leading))):
                try:
                    products.append(_Product(leading[:160],
                                             money_to_minor(last.group(1))))
                    saw_item = True
                except ValueError:
                    pass
            current = None
            continue

        # Join wrapped product names, but never unrelated fiscal metadata.
        if (current is not None and _WORD.search(cleaned) and
                not cleaned[:1].isdigit() and
                not _BAD_CONTINUATION.match(folded) and
                not re.match(r"^\d{1,2}[.,]\d", cleaned)):
            current.name = (current.name + " " + cleaned)[:160]

    pending = [product for product in products if product.amount is None]
    if len(pending) == len(free_formulas):
        for product, amount in zip(pending, free_formulas):
            product.amount = amount
    # If counts differ, matching is ambiguous: retain only known inline
    # products. We must not silently shift prices between products.
    result = [
        (product.name[:100], product.amount)
        for product in products
        if product.amount is not None and product.amount > 0
    ]
    return result if len(result) <= max_items else []
