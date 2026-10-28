"""Offline Russian fiscal QR and printed-receipt recognition."""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from .receipt_heuristics import clean_item_name, detected_date, detected_total, is_service_line

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_ITEMS = 25

AMOUNT = re.compile(r"(?<!\d)(\d{1,7}(?:[ \u00a0]\d{3})*[,.]\d{2})(?!\d)")
TOTAL = re.compile(r"(?:^|\s)(?:итого|к\s+оплате|сумма\s+чека|всего\s+к\s+оплате|общая\s+сумма)\s*[:=]?\s*(\d[\d \u00a0]*[.,]\d{2})", re.I)
SKIP = re.compile(
    r"(^|\s)(?:итого|сумма|всего|к\s+оплате|ндс|скидк|сдача|оплачено|наличн|"
    r"безнал|банковск|кассир|чек|фд|фн|фп|инн|рн|ккт|qr|номер|дата|время|"
    r"скидка|бонус|спасибо|покупок|сайт|www|http|электронн|налог|получен|"
    r"провер|эквайр|терминал)\b", re.I
)


class ReceiptError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    name: str
    amount_minor: int


@dataclass(frozen=True)
class Recognized:
    image_hash: str
    receipt_key: str
    qr_text: str
    ocr_text: str
    total_minor: int | None
    occurred_at: str
    description: str
    items: tuple[Item, ...]
    fiscal: bool

    @property
    def items_match_total(self) -> bool:
        return bool(self.items and self.total_minor and
                    sum(x.amount_minor for x in self.items) == self.total_minor)


def money_to_minor(raw: str) -> int:
    try:
        num = Decimal(raw.replace(" ", "").replace("\u00a0", "").replace(",", "."))
    except InvalidOperation as error:
        raise ReceiptError("Неверная сумма чека.") from error
    if not num.is_finite() or num <= 0 or num.as_tuple().exponent < -2 or num > 10_000_000:
        raise ReceiptError("Неверная или слишком большая сумма чека.")
    return int(num * 100)


def fiscal_qr(text: str, tz: str) -> tuple[int, str, str] | None:
    """Returns total, UTC date and fiscal key; QR has NO items."""
    data = dict(parse_qsl(text.strip(), keep_blank_values=False))
    if not all(x in data for x in ("t", "s", "fn", "i", "fp", "n")):
        return None
    if not re.fullmatch(r"\d{8}T\d{4}(?:\d{2})?", data["t"]):
        raise ReceiptError("Некорректная дата в QR чека.")
    for key in ("fn", "i", "fp", "n"):
        if not re.fullmatch(r"\d{1,24}", data[key]):
            raise ReceiptError("Некорректные реквизиты фискального чека.")
    if data["n"] != "1":
        raise ReceiptError("Это не чек покупки (возможно, возврат). Учёт возвратов появится позже.")
    try:
        local = datetime.strptime(data["t"], "%Y%m%dT%H%M%S" if len(data["t"]) == 15 else "%Y%m%dT%H%M")
        at = local.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
    except ValueError as error:
        raise ReceiptError("Некорректная дата чека.") from error
    total = money_to_minor(data["s"])
    identity = hashlib.sha256(
        (data["fn"] + ":" + data["i"] + ":" + data["fp"]).encode("ascii")
    ).hexdigest()
    return total, at.isoformat(timespec="seconds"), "fiscal:" + identity


def parse_items(text: str) -> tuple[Item, ...]:
    """Pair VAT-labelled product lines with their separate price/quantity lines.

    A standalone terminal or fiscal amount must never become a product.
    A sum matching the receipt's total is necessary but not sufficient for
    trusting the itemization: the user must review it in Telegram.
    """
    from .receipt_heuristics import VAT_PREFIX

    result: list[Item] = []
    pending: str | None = None

    def append(name: str, amount: int) -> None:
        if amount > 0 and name and re.search(r"[А-Яа-яЁёA-Za-z]{3,}", name):
            result.append(Item(name=name[:100], amount_minor=amount))

    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw.strip()).strip("-–* ")
        vat_label = VAT_PREFIX.match(line)
        if vat_label:
            pending = None
        cleaned = clean_item_name(VAT_PREFIX.sub("", line))
        if not cleaned or is_service_line(cleaned):
            continue

        prices = list(AMOUNT.finditer(cleaned))
        amount_match = prices[-1] if prices else None
        arithmetic = bool(re.search(r"\d[\d.,]*\s*[*×xх]\s*\d", cleaned, re.I) and
                          "=" in cleaned)

        if not amount_match:
            if vat_label:
                # A new VAT label starts a new item. Never reuse an unrelated
                # previous name when the price for it was not readable.
                pending = cleaned if re.search(r"[А-Яа-яЁёA-Za-z]{3,}", cleaned) else None
            elif pending and not re.search(r"^\d", cleaned):
                pending = (pending + " " + cleaned)[:180]
            continue

        try:
            amount = money_to_minor(amount_match.group(1))
        except ReceiptError:
            continue

        after = cleaned[amount_match.end():].strip(" ₽р.уб")
        if after and not re.fullmatch(r"[₽руб.\s]*", after, re.I):
            continue

        if arithmetic:
            if pending:
                append(pending, amount)
                pending = None
                if len(result) > MAX_ITEMS:
                    return ()
                continue
            product_name = clean_item_name(cleaned[:amount_match.start()])
            # Product and price may share a line; name before its formula.
            if product_name:
                append(product_name, amount)
        else:
            name = clean_item_name(cleaned[:amount_match.start()])
            if vat_label or (name and not pending):
                append(name, amount)
            elif pending and name:
                append(pending + " " + name, amount)
            pending = None

        if len(result) > MAX_ITEMS:
            return ()
    return tuple(result)


def ocr_total(text: str) -> int | None:
    return detected_total(text)


def from_text(image_bytes: bytes, qr: str, ocr: str, tz: str,
              now: datetime | None = None) -> Recognized:
    key = "image:" + hashlib.sha256(image_bytes).hexdigest()
    fiscal = fiscal_qr(qr, tz) if qr else None
    items = parse_items(ocr)
    total = fiscal[0] if fiscal else ocr_total(ocr)
    date = (fiscal[1] if fiscal else detected_date(ocr, tz) or
            (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds"))
    key = fiscal[2] if fiscal else key
    # A safe neutral name when vendor identity cannot be established reliably.
    return Recognized(
        image_hash=hashlib.sha256(image_bytes).hexdigest(),
        receipt_key=key, qr_text=qr[:1000], ocr_text=ocr[:12000],
        total_minor=total, occurred_at=date,
        description="Покупка по чеку", items=items,
        fiscal=fiscal is not None,
    )


def recognize(image_bytes: bytes, tz: str) -> Recognized:
    """Blocking OCR/QR decode; call through asyncio.to_thread."""
    if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise ReceiptError("Размер изображения должен быть до 10 МБ.")
    from PIL import Image, ImageEnhance, ImageOps, ImageFilter
    from .ocr_engine import read_text, tesseract_text
    from pyzbar.pyzbar import decode as decode_barcodes, ZBarSymbol
    try:
        Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
        with Image.open(io.BytesIO(image_bytes)) as raw:
            if raw.format not in ("JPEG", "PNG", "WEBP"):
                raise ReceiptError("Принимаются JPEG, PNG и WebP.")
            if raw.width * raw.height > MAX_IMAGE_PIXELS:
                raise ReceiptError("Слишком большое изображение.")
            original = ImageOps.exif_transpose(raw).convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        if isinstance(exc, ReceiptError):
            raise
        raise ReceiptError("Не удалось прочитать изображение чека.") from exc
    qr_text = ""
    # Scan both full photo and dedicated areas: a QR may occupy only a small
    # fraction of a long receipt, especially in Telegram-compressed photos.
    gray_original = ImageOps.grayscale(original)
    w, h = original.size
    candidates = (original, gray_original,
                  gray_original.crop((w // 2, h // 2, w, h)),
                  gray_original.crop((w // 3, 0, w, h // 2)))
    for variant in candidates:
        barcodes = decode_barcodes(variant, symbols=[ZBarSymbol.QRCODE])
        for code in barcodes:
            for encoding in ("utf-8", "cp866"):
                try:
                    candidate = code.data.decode(encoding)
                except UnicodeDecodeError:
                    continue
                try:
                    matches_fiscal = fiscal_qr(candidate, tz)
                except ReceiptError:
                    matches_fiscal = None
                if matches_fiscal:
                    qr_text = candidate
                    break
            if qr_text:
                break
        if qr_text:
            break
    if not qr_text:
        # Additional limited QR-only pass on the two isolated regions.
        for crop in candidates[2:]:
            enhanced = ImageOps.autocontrast(crop)
            enhanced = enhanced.resize((crop.width * 2, crop.height * 2))
            enhanced = enhanced.filter(ImageFilter.UnsharpMask(radius=1.2, percent=140))
            for code in decode_barcodes(enhanced, symbols=[ZBarSymbol.QRCODE]):
                candidate = code.data.decode("utf-8", errors="replace")
                try:
                    matches_fiscal = fiscal_qr(candidate, tz)
                except ReceiptError:
                    matches_fiscal = None
                if matches_fiscal:
                    qr_text = candidate
                    break
            if qr_text:
                break
    ocr_image = original.copy()
    ocr_image.thumbnail((1900, 3000))
    try:
        text = read_text(ocr_image)
    except (RuntimeError, OSError, ValueError) as exc:
        if not qr_text:
            raise ReceiptError("Не удалось распознать текст. Попробуй более чёткое фото.") from exc
        text = ""
    result = from_text(image_bytes, qr_text, text, tz)
    if result.total_minor is None:
        # Targeted fallback: payment/fiscal block usually occupies the bottom
        # half of long receipts. Keep this OCR separate so duplicate product
        # rows do not contaminate the itemization.
        bottom = gray_original.crop((0, int(h * 0.50), w, h))
        bottom = ImageOps.autocontrast(bottom)
        if bottom.width < 1400:
            bottom = bottom.resize((bottom.width * 2, bottom.height * 2))
        if max(bottom.size) > 3200:
            bottom.thumbnail((3200, 3200))
        try:
            payment_text = read_text(bottom)
            if not ocr_total(payment_text):
                payment_text = tesseract_text(bottom)
        except (RuntimeError, OSError, ValueError):
            payment_text = ""
        payment_total = ocr_total(payment_text)
        if payment_total:
            result = replace(result, total_minor=payment_total)
        if not qr_text:
            printed_date = detected_date(payment_text, tz)
            if printed_date and not detected_date(text, tz):
                result = replace(result, occurred_at=printed_date)
    return result
