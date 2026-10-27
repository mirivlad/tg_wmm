"""Deterministic free-form Russian expense parser. Never guesses missing amounts."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

CATEGORIES: dict[str, tuple[str, ...]] = {
    "Продукты": ("еда", "продукты", "магазин", "хлеб", "молоко", "сыр", "мясо", "овощи", "фрукты", "перекус"),
    "Кафе и рестораны": ("обед", "ужин", "завтрак", "кафе", "ресторан", "столовая", "шаурма", "пицца", "доставка еды"),
    "Кофе и напитки": ("кофе", "капучино", "латте", "чай", "напиток", "энергетик"),
    "Табак": ("сиги", "сигареты", "сигарет", "табак", "курево", "пачка сигарет"),
    "Снеки": ("семки", "семечки", "чипсы", "сухарики", "орешки"),
    "Топливо": ("бензин", "заправка", "бенз", "солярка", "дизель", "газ", "топливо", "азс"),
    "Транспорт": ("такси", "автобус", "маршрутка", "метро", "проезд", "парковка"),
    "ЖКХ и связь": ("жкх", "коммуналка", "квартплата", "интернет", "телефон", "мобильная связь", "электричество", "вода"),
    "Медицина": ("аптека", "лекарства", "врач", "стоматолог", "больница"),
    "Покупки": ("одежда", "обувь", "техника", "покупки", "хозтовары"),
    "Развлечения": ("кино", "игры", "концерт", "подписка", "развлечения"),
    "Кредиты": ("кредит", "ипотека", "платеж по кредиту", "платёж по кредиту"),
    "Переводы и подарки": ("настe", "насте", "перевод", "подарок", "подарил"),
    "Проценты и комиссии": ("проценты", "комиссия", "комиссии"),
    "Прочее": (),
}
SPLIT = re.compile(r"[,;](?!\d)|\n+|\.(?=\s+[-—–]*[а-яёa-z])", re.I)
MONEY = re.compile(r"(?<![\w])(\d[\d ]*(?:[.,]\d{1,2})?)(?:\s*(?:₽|руб(?:лей|ля|\.|ль)?))?(?![\w])", re.I)
RELATIVE_DATE = re.compile(r"^(сегодня|вчера|позавчера)\s+", re.I)
REPAY = re.compile(r"^\s*(?:погасил(?:а)?|погашение|закинул(?:а)?|внёс|внес|оплатил(?:а)?\s+кредитк\w*)\b", re.I)


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Expense:
    description: str
    amount_minor: int
    source_id: int
    category: str
    occurred_at: str
    raw: str
    unclassified: bool


@dataclass(frozen=True)
class Repayment:
    amount_minor: int
    principal_minor: int
    interest_minor: int
    source_id: int
    target_source_id: int
    occurred_at: str
    raw: str


def minor(value: str) -> int:
    compact = value.replace(" ", "").replace(",", ".")
    try:
        dec = Decimal(compact)
    except InvalidOperation as exc:
        raise ParseError("Не понял сумму.") from exc
    if not dec.is_finite() or dec <= 0 or dec.as_tuple().exponent < -2:
        raise ParseError("Сумма должна быть положительной, не более двух знаков после запятой.")
    result = int(dec * 100)
    if result > 10**12:
        raise ParseError("Слишком большая сумма.")
    return result


def rub(value: int) -> str:
    sign = "-" if value < 0 else ""
    whole, cents = divmod(abs(value), 100)
    return sign + f"{whole:,}".replace(",", " ") + (f",{cents:02d}" if cents else "") + " ₽"


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]+", " ", text.casefold().replace("ё", "е"))).strip()


def effective_date(text: str, timezone: str, now: datetime | None = None) -> tuple[str, str]:
    local = (now or datetime.now(ZoneInfo(timezone))).astimezone(ZoneInfo(timezone))
    match = RELATIVE_DATE.match(text.strip())
    offset = {"сегодня": 0, "вчера": 1, "позавчера": 2}
    if match:
        local = local - timedelta(days=offset[match.group(1).casefold()])
        text = text[match.end():]
    return text.strip(), local.astimezone(dt_timezone.utc).isoformat(timespec="seconds")


def _take_source(text: str, aliases: dict[str, int], default_source: int) -> tuple[str, int]:
    """Recognize a source at the beginning or end of the phrase, longest first."""
    for label, source_id in sorted(aliases.items(), key=lambda kv: len(kv[0]), reverse=True):
        escaped = re.escape(label).replace(r"\ ", r"\s*")
        for pattern in (rf"^\s*{escaped}(?!\w)\s*", rf"\s+(?<!\w){escaped}\s*$"):
            match = re.search(pattern, text, re.I)
            if match:
                return (text[:match.start()] + " " + text[match.end():]).strip(), source_id
    return text, default_source


def category_for(description: str, aliases: dict[str, str] | None = None) -> tuple[str, bool]:
    clean = normalize(description)
    # Explicit learned aliases override all built-in heuristics.
    if aliases:
        for key, category in sorted(aliases.items(), key=lambda x: -len(x[0])):
            if clean == key or clean.startswith(key + " "):
                return category, False
    for category, keywords in CATEGORIES.items():
        for keyword in sorted(keywords, key=len, reverse=True):
            k = normalize(keyword)
            if re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", clean):
                return category, False
    return "Прочее", True


def parse_expenses(
    text: str, sources: dict[str, int], default_source: int,
    learned: dict[str, str] | None = None, timezone: str = "Europe/Moscow",
    now: datetime | None = None,
) -> list[Expense]:
    text, at = effective_date(text, timezone, now)
    if not text:
        raise ParseError("Пустая запись.")
    if REPAY.match(text) or (re.search(r"кредитк", text, re.I) and re.search(r"зачисл|погаш|процент", text, re.I)):
        raise ParseError("Погашение кредита запиши с разбивкой: «погасил кредитка1 9000, зачислилось 6000».")
    result = []
    for chunk in SPLIT.split(text):
        raw = chunk.strip(" \t-—–.")
        if not raw:
            continue
        amounts = list(MONEY.finditer(raw))
        if not amounts:
            raise ParseError(f"Не нашёл сумму в «{raw}». Ничего из сообщения не записано.")
        last = amounts[-1]
        # '2 пачки 420' means 420 total; explicitly stated unit-price is ambiguous.
        if re.search(r"\bпо\s+\d", raw, re.I) and len(amounts) > 1:
            raise ParseError(f"«{raw}»: укажи итоговую сумму без «по».")
        amount = minor(last.group(1))
        title = (raw[:last.start()] + " " + raw[last.end():]).strip(" \t-—–.")
        title, source_id = _take_source(title, sources, default_source)
        if not title:
            raise ParseError(f"Укажи, за что заплачено: «{raw}».")
        category, unknown = category_for(title, learned)
        result.append(Expense(title, amount, source_id, category, at, raw, unknown))
    if not result:
        raise ParseError("Не вижу расходов. Пример: «кофе 130, заправка 3500».")
    if len(result) > 30:
        raise ParseError("Максимум 30 расходов в одном сообщении.")
    return result


def parse_repayment(text: str, sources: dict[str, int], default_source: int,
                    timezone: str = "Europe/Moscow", now: datetime | None = None) -> Repayment | None:
    original = text.strip()
    text, at = effective_date(original, timezone, now)
    if not REPAY.match(text):
        return None
    first = re.split(r"[,;\n]", text, maxsplit=1)[0]
    all_amounts = list(MONEY.finditer(first))
    if not all_amounts:
        raise ParseError("Укажи сумму платежа по кредитке.")
    total = minor(all_amounts[-1].group(1))
    target = None
    for label, source_id in sorted(sources.items(), key=lambda x: -len(x[0])):
        if "кредит" not in normalize(label) and not re.search(r"\bк\d+\b", normalize(label)):
            continue
        if re.search(r"(?<!\w)" + re.escape(label).replace(r"\ ", r"\s*") + r"(?!\w)", first, re.I):
            target = source_id
            break
    if target is None:
        raise ParseError("Не понял, какую кредитку погашаешь. Добавь её через /source_add.")
    principal = re.search(r"(?:зачисл\w*|основн\w*\s+долг|в\s+тело)[^\d]*(\d[\d ]*(?:[.,]\d{1,2})?)", text, re.I)
    interest = re.search(r"(?:процент\w*|комисси\w*)[^\d]*(\d[\d ]*(?:[.,]\d{1,2})?)", text, re.I)
    if not interest:
        interest = re.search(r"(\d[\d ]*(?:[.,]\d{1,2})?)\s*(?:процент\w*|комисси\w*)", text, re.I)
    if principal:
        main = minor(principal.group(1))
        extra = total - main
    elif interest:
        extra = minor(interest.group(1))
        main = total - extra
    else:
        raise ParseError("Сколько пошло на основной долг? Например: «погасил кредитка1 9000, зачислилось 6000». Платёж не записан.")
    if main < 0 or extra < 0 or (interest and principal and extra != minor(interest.group(1))):
        raise ParseError("Суммы основного долга и процентов не сходятся с платежом.")
    if target == default_source:
        raise ParseError("Источник списания совпадает с кредиткой. Назначь дебетовый источник через /source_default.")
    return Repayment(total, main, extra, default_source, target, at, original)
