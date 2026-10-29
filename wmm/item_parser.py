"""Conservative line-item reconstruction for receipt OCR.

A receipt may print several product descriptions, followed by their price
formulas. Never bind the first following formula to the last description.
Resolve a block only when number of formulas equals number of item headers.
"""
from __future__ import annotations

import re
from typing import Callable

from .receipt_heuristics import VAT_PREFIX, canonical, clean_item_name, is_service_line

_PRICE = re.compile(r"(?<!\d)(\d{1,7}(?:[ \u00a0]\d{3})*[,.]\d{2})(?!\d)")
_FORMULA = re.compile(r"\d[\d,. ]*\s*[*×xхж]\s*\d", re.I)
_WORD = re.compile(r"[А-Яа-яЁёA-Za-z]{3,}")
# A formula containing an equals sign is stronger evidence of the line total.
# For example 359.99 * 0.250KG = 90.00.
_QUANTITY_FORMULA = re.compile(r"=\s*\d[\d,. ]*[,.]\d{2}\s*(?:₽|руб\.?)?\s*$", re.I)
_BARCODE = re.compile(r"^(?:\d{9,}|[A-F0-9]{24,})$", re.I)


def parse_lines(text: str, money_to_minor: Callable[[str], int], max_items: int = 25):
    """Return [(name, minor_amount)] or [] when parsing was too ambiguous."""
    result: list[tuple[str, int]] = []
    headers: list[str] = []
    prices: list[int] = []
    # A price sequence can refer to *several* preceding product headers.
    # Do not choose the nearest header.
    def flush() -> None:
        nonlocal headers, prices
        if (headers and len(headers) == len(prices) and
                all(_WORD.search(name) for name in headers)):
            result.extend((name[:100], price) for name, price in zip(headers, prices) if price > 0)
        headers = []
        prices = []

    for raw in text.splitlines():
        line = " ".join(raw.strip().split()).strip("-–* ")
        if not line:
            continue
        # OCR mixes Latin and Cyrillic lookalikes, e.g. "НAC" for "НАС".
        vat_match = VAT_PREFIX.match(canonical(line))
        vat = vat_match is not None
        without_vat = line[vat_match.end():] if vat_match else line
        # A price formula starts with digits; clean_item_name would mistake
        # the first 3 digits and dot for an enumerated list marker.
        cleaned = (without_vat if without_vat[:1].isdigit()
                   else clean_item_name(without_vat))
        if not cleaned or is_service_line(cleaned) or _BARCODE.fullmatch(cleaned):
            continue

        matches = list(_PRICE.finditer(cleaned))
        formula = bool(_FORMULA.search(cleaned)) and bool(_QUANTITY_FORMULA.search(cleaned))
        first_price = matches[0] if matches else None
        last_price = matches[-1] if matches else None

        # A VAT label always begins a separate product. If the current block
        # already has price formulae, finish the block before adding this item.
        if vat and prices:
            flush()

        if formula and last_price:
            try:
                amount = money_to_minor(last_price.group(1))
            except ValueError:
                continue
            # A name preceding the first unit price signals an inline formula.
            leading = cleaned[:first_price.start()].strip(" .,;:-") if first_price else ""
            if vat and _WORD.search(leading):
                # Product and its own line total.
                flush()
                result.append((leading[:100], amount))
            elif leading and _WORD.search(leading) and not headers:
                result.append((leading[:100], amount))
            elif headers:
                prices.append(amount)
            # Without any labelled item, the formula is orphaned: ignore it.
            continue

        if vat and not last_price:
            if _WORD.search(cleaned):
                headers.append(cleaned[:160])
            continue

        if vat and last_price:
            try:
                amount = money_to_minor(last_price.group(1))
            except ValueError:
                continue
            name = clean_item_name(cleaned[:last_price.start()])
            if name and _WORD.search(name):
                flush()
                result.append((name[:100], amount))
            continue

        # A two-token numeric price/quantity line without an '=' may be an
        # alcohol percentage, a tax or the register payment. Never assign it.
        if last_price and not formula:
            name = clean_item_name(cleaned[:last_price.start()])
            trailing = cleaned[last_price.end():].strip(" ₽руб.")
            if (name and _WORD.search(name) and not trailing
                    and not headers and not is_service_line(name)):
                result.append((name[:100], money_to_minor(last_price.group(1))))
            # Standalone amounts are ignored, never taken as pending item prices.
            continue

        # Name continued onto another line before any formulas.
        if headers and not prices and not cleaned[0].isdigit() and _WORD.search(cleaned):
            if len(headers[-1]) < 145:
                headers[-1] = (headers[-1] + " " + cleaned)[:160]

    flush()
    # Nonmatching blocks are intentionally dropped, not guessed.
    return result if len(result) <= max_items else []
