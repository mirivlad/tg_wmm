"""Conservative heuristics for noisy Russian receipt OCR.

This module never fabricates line-item prices. Totals come only from payment/
fiscal-total labels, not from totals before discounts.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MONEY = re.compile(r"(?<![\d])(\d{1,7}(?:[ \u00a0]\d{3})*[,.]\d{2})(?![\d])")
DATE = re.compile(r"(?<!\d)(\d{2})[.](\d{2})[.](\d{2}|\d{4})\s+(\d{1,2}):(\d{2})(?!\d)")
# Cyrillic letters frequently appear as Latin lookalikes in OCR.
_TRANSLATION = str.maketrans({"C": "С", "Y": "У", "H": "Н", "M": "М",
    "A": "А", "P": "Р", "E": "Е", "O": "О", "B": "В", "K": "К", "T": "Т",
    "X": "Х", "I": "І", "a": "а", "c": "с", "y": "у"})
VAT_PREFIX = re.compile(r"^\s*(?:НДС|НАС|HAC|HOC)\s*\d{1,2}\s*%\s*", re.I)
BAD_LINE = re.compile(
    r"(?:^|[\s:])(?:НДС|АКЦИЗ|КОМИССИЯ|ТЕРМИНАЛ|КАССИР|ККТ|ЧЕК|КВИТАНЦИЯ|"
    r"ИНН|ФН|ФД|ФП|РН|БАНКОВСКИЕ|ОПЛАТЫ|МИР|ПРОВЕРЕНО|ПРИХОД|"
    r"СКИДКИ|БЕЗНАЛИЧНЫМИ|НАЛИЧНЫМИ|СДАЧА|ОПЛАЧЕНО|ИТОГО|СУММА)\b",
    re.I,
)
FORMULA_SUFFIX = re.compile(
    r"\s+\d{1,7}[,.]\d{2}\s*[*×xхж]\s*[\d,.]+\s*(?:шт\.?|кг|г|л|мл)?\s*=\s*$", re.I
)


def canonical(text: str) -> str:
    # Normalize visually similar Cyrillic/Latin characters in label matching only.
    return text.upper().translate(_TRANSLATION)


def is_service_line(raw: str) -> bool:
    text = canonical(raw).strip()
    if re.match(r"^[СC][УY][МН]{2}[АA]", text):
        return True  # Сумма, Сунна, CYHMa in poorly recognized OCR.
    if re.search(r"(?:БЕЗНАЛ|НАЛИЧН|ИТОГО|К\s+ОПЛАТЕ|СУММА|"
                 r"БАНКОВСК|МИР\s*PAY|АКЦИЗ|КОМИССИЯ|СКИДК|"
                 r"ТЕРМИНАЛ|ФИСКАЛ|КАССИР|ККТ|ИНН|ЧЕК|НДС)\b", text):
        return True
    return bool(BAD_LINE.search(text))


def clean_item_name(raw: str) -> str:
    # The VAT annotation preceding the product is not part of the product name.
    text = VAT_PREFIX.sub("", raw)
    text = re.sub(r"^\d{1,3}[.)]\s*", "", text)
    text = FORMULA_SUFFIX.sub("", text)
    return text.strip(" .:;-—*\t")


def detected_total(text: str) -> int | None:
    """Extract cashless/receipt total while refusing ambiguous conflicting values."""
    from .receipts import money_to_minor, ReceiptError

    candidates: list[tuple[int, int]] = []
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        normalized = canonical(raw)
        if re.search(r"СКИДК", normalized) or re.search(r"ДО\s+СКИДК", normalized):
            # 'Сумма без скидки' is explicitly NOT what was paid.
            continue
        rating = 0
        if re.search(r"(?:\bИТОГО\b|К\s+ОПЛАТЕ)\b", normalized):
            rating = 100
        elif re.search(r"\bБЕЗНАЛИЧН\w*", normalized):
            rating = 95
        elif re.search(r"\bСУММА\s*(?:\(?\s*РУБ\w*\)?|ЧЕКА\b)", normalized):
            rating = 85
        elif re.search(r"\b[С][У][МН]{2}[А].{0,6}[С][Р][У][Е]\b", normalized):
            rating = 78  # OCR corruption of СУММА(РУБ).
        if not rating:
            continue
        amounts = MONEY.findall(normalized)
        if not amounts and index + 1 < len(lines):
            next_line = canonical(lines[index + 1])
            if re.fullmatch(r"[\s:№#]*\d[\d \u00a0]*[,.]\d{2}[\s.₽]*", next_line):
                amounts = MONEY.findall(next_line)
        if amounts:
            try:
                candidates.append((rating, money_to_minor(amounts[-1])))
            except ReceiptError:
                pass
    if not candidates:
        return None
    # Reject disagreement between equally authoritative totals. Never guess.
    best_rank = max(rank for rank, _ in candidates)
    # A single heavily corrupted 'sum (rub)' label is not sufficiently reliable.
    if best_rank < 85:
        return None
    best_values = {v for rank, v in candidates if rank == best_rank}
    return next(iter(best_values)) if len(best_values) == 1 else None


def detected_date(text: str, tz: str) -> str | None:
    # A fiscal QR is preferred; printed date is only a fallback.
    for match in DATE.finditer(text):
        day, month, raw_year, hour, minute = match.groups()
        year = int(raw_year) if len(raw_year) == 4 else 2000 + int(raw_year)
        try:
            dt = datetime(year, int(month), int(day), int(hour), int(minute),
                          tzinfo=ZoneInfo(tz))
        except ValueError:
            continue
        if year < 2000 or year > datetime.now(ZoneInfo(tz)).year + 1:
            continue
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    return None
