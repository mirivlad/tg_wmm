"""Offline Russian fiscal QR and printed-receipt recognition."""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

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
    """Conservative: only item + its final line price; ignore fiscal/payment metadata."""
    items: list[Item] = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line.strip()).strip("-–* ")
        if len(line) < 5 or SKIP.search(line):
            continue
        money = list(AMOUNT.finditer(line))
        if not money:
            continue
        amount_match = money[-1]
        trailing = line[amount_match.end():].strip(" ₽р.уб")
        if trailing and not re.fullmatch(r"[₽руб.\s]*", trailing, re.I):
            continue
        name = line[:amount_match.start()].strip(" .:;—")
        name = re.sub(r"^\d{1,3}[.)]\s*", "", name)
        # Do not treat pure quantity/price calculations as products.
        if not name or not re.search(r"[А-Яа-яЁёA-Za-z]{3,}", name):
            continue
        if re.search(r"(^|\s)(?:шт|кг|x|х)\s*[\d,. ]", name, re.I) and len(name) < 16:
            continue
        name = name[:100]
        try:
            amount = money_to_minor(amount_match.group(1))
        except ReceiptError:
            continue
        items.append(Item(name=name, amount_minor=amount))
        if len(items) > MAX_ITEMS:
            return ()
    return tuple(items)


def ocr_total(text: str) -> int | None:
    for line in reversed(text.splitlines()):
        matched = TOTAL.search(re.sub(r"\s+", " ", line))
        if matched:
            try:
                return money_to_minor(matched.group(1))
            except ReceiptError:
                continue
    return None


def from_text(image_bytes: bytes, qr: str, ocr: str, tz: str,
              now: datetime | None = None) -> Recognized:
    key = "image:" + hashlib.sha256(image_bytes).hexdigest()
    fiscal = fiscal_qr(qr, tz) if qr else None
    items = parse_items(ocr)
    total = fiscal[0] if fiscal else ocr_total(ocr)
    date = fiscal[1] if fiscal else (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
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
    from PIL import Image, ImageEnhance, ImageOps, UnidentifiedImageError
    import pytesseract
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
    for variant in (original, ImageOps.grayscale(original)):
        barcodes = decode_barcodes(variant, symbols=[ZBarSymbol.QRCODE])
        for code in barcodes:
            for encoding in ("utf-8", "cp866"):
                try:
                    candidate = code.data.decode(encoding)
                except UnicodeDecodeError:
                    continue
                if fiscal_qr(candidate, tz):
                    qr_text = candidate
                    break
            if qr_text:
                break
        if qr_text:
            break
    gray = ImageEnhance.Contrast(ImageOps.grayscale(original)).enhance(1.6)
    if gray.width < 1400:
        scale = min(2.5, 1400 / gray.width)
        gray = gray.resize((int(gray.width * scale), int(gray.height * scale)))
    if max(gray.size) > 3200:
        gray.thumbnail((3200, 3200))
    try:
        text = pytesseract.image_to_string(gray, lang="rus+eng", config="--psm 6", timeout=20)
    except (RuntimeError, pytesseract.TesseractError, OSError) as exc:
        if not qr_text:
            raise ReceiptError("Не удалось распознать текст. Попробуй более чёткое фото.") from exc
        text = ""
    return from_text(image_bytes, qr_text, text, tz)
