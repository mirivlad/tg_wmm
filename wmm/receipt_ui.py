"""Telegram photo/document/fiscal QR interaction. No external OCR services."""
from __future__ import annotations

import asyncio
import io
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .db import Database
from .menus import receipt_keyboard
from .parser import ParseError, minor, rub
from .receipt_store import ReceiptStore
from .receipts import ReceiptError, from_text, recognize, MAX_IMAGE_BYTES


class ReceiptInput(StatesGroup):
    amount = State()
    description = State()


def markup(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=payload) for label, payload in row]
        for row in rows
    ])


def preview(db: Database, draft, tz: str) -> tuple[str, InlineKeyboardMarkup]:
    data = json.loads(draft["items_json"])
    amount = draft["total_minor"]
    total_ok = bool(amount and data and sum(i["amount_minor"] for i in data) == amount)
    source = next((s["name"] for s in db.sources(draft["user_id"]) if s["id"] == draft["source_id"]), "неизвестен")
    lines = [
        "🧾 Распознанный чек — ПРОВЕРЬ ПЕРЕД ЗАПИСЬЮ",
        "Сумма: " + (rub(amount) if amount else "НЕ РАСПОЗНАНА"),
        f"Название: {draft['description']}",
        f"Источник: {source}",
        "Дата: " + datetime.fromisoformat(draft["occurred_at"]).astimezone(ZoneInfo(tz)).strftime("%d.%m.%Y %H:%M"),
        "Фискальный QR: " + ("найден" if draft["qr_text"] else "не найден"),
    ]
    if data:
        lines.append("\nСтроки, предположительно распознанные OCR:")
        for item in data[:10]:
            lines.append(f"• {item['name'][:60]} — {rub(item['amount_minor'])}")
        if len(data) > 10:
            lines.append(f"… и ещё {len(data) - 10}")
        if not total_ok:
            lines.append("\nРазбивка не подтверждена: суммы позиций не совпали с итогом.")
        else:
            lines.append("\nСумма позиций совпадает с итогом.")
    else:
        lines.append("\nСписок товаров распознать надёжно не удалось.")
    lines.append("\nДанные не сохранены в расходы. Сначала проверь сумму и источник.")
    ident = draft["id"]
    rows = []
    if amount:
        rows.append([("✅ Записать общую сумму", f"rc:save:{ident}:all")])
        if total_ok:
            rows.append([("🛒 Записать по товарам", f"rc:save:{ident}:items")])
    rows.extend([
        [("💳 Источник", f"rc:sources:{ident}:0"), ("💰 Сумма", f"rc:amount:{ident}")],
        [("📝 Название", f"rc:name:{ident}")],
        [("✖️ Не записывать", f"rc:discard:{ident}")],
    ])
    return "\n".join(lines), markup(*rows)


def sources_markup(db: Database, uid: int, draft_id: int, page: int, selected_id: int) -> InlineKeyboardMarkup:
    sources = db.sources(uid)
    size = 8
    page = min(max(0, page), max(0, math.ceil(len(sources) / size) - 1))
    rows = [[(row["name"] + (" ✓" if row["id"] == selected_id else ""),
              f"rc:source:{draft_id}:{row['id']}")]
            for row in sources[page * size:(page + 1) * size]]
    navigation = []
    if page:
        navigation.append(("◀️", f"rc:sources:{draft_id}:{page - 1}"))
    if (page + 1) * size < len(sources):
        navigation.append(("▶️", f"rc:sources:{draft_id}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([("↩️ К чеку", f"rc:back:{draft_id}")])
    return markup(*rows)


def register_receipts(router: Router, db: Database, store: ReceiptStore, timezone: str) -> None:

    @router.message(F.photo)
    async def photo_receipt(message: Message, bot: Bot):
        chosen = message.photo[-1]
        if chosen.file_size and chosen.file_size > MAX_IMAGE_BYTES:
            return await message.answer("Фото больше 10 МБ. Отправь изображение меньшего размера.")
        await _from_image(message, bot, chosen.file_id)

    @router.message(F.document)
    async def document_receipt(message: Message, bot: Bot):
        attachment = message.document
        if attachment.mime_type not in ("image/png", "image/jpeg", "image/webp"):
            return await message.answer("Чеки принимаю как фото или JPEG/PNG/WebP. PDF пока не поддерживается.")
        if attachment.file_size and attachment.file_size > MAX_IMAGE_BYTES:
            return await message.answer("Изображение больше 10 МБ.")
        await _from_image(message, bot, attachment.file_id)

    async def _from_image(message: Message, bot: Bot, file_id: str):
        try:
            output = io.BytesIO()
            await bot.download(file_id, destination=output, timeout=30)
            data = output.getvalue()
            recognized = await asyncio.to_thread(recognize, data, timezone)
            draft_id = store.create_draft(message.from_user.id, message.chat.id,
                                          message.message_id, recognized)
        except ReceiptError as err:
            return await message.answer("Чек не записан. " + str(err))
        except (OSError, TimeoutError) as err:
            return await message.answer("Не удалось получить фото. Попробуй отправить ещё раз.")
        draft = store.draft(message.from_user.id, draft_id)
        label, keyboard = preview(db, draft, timezone)
        await message.answer(label, reply_markup=keyboard)

    @router.message(F.text.startswith("t="))
    async def qr_text(message: Message):
        try:
            qr = from_text(message.text.encode(), message.text.strip(), "", timezone)
            if not qr.fiscal:
                return await message.answer("Не вижу фискальных реквизитов в QR.")
            draft_id = store.create_draft(message.from_user.id, message.chat.id,
                                          message.message_id, qr)
        except ReceiptError as err:
            return await message.answer("Чек не записан. " + str(err))
        label, keyboard = preview(db, store.draft(message.from_user.id, draft_id), timezone)
        await message.answer(label, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("rc:"))
    async def on_receipt_button(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        if not isinstance(callback.message, Message):
            return await callback.answer("Меню недоступно.", show_alert=True)
        args = callback.data.split(":")
        if len(args) < 3 or not args[2].isdigit():
            return await callback.answer("Устаревшая кнопка.", show_alert=True)
        action = args[1]
        draft_id = int(args[2])
        draft = store.draft(uid, draft_id)
        if not draft or draft["status"] != "pending":
            return await callback.answer("Чек уже обработан или недоступен.", show_alert=True)
        if action == "save":
            split = len(args) > 3 and args[3] == "items"
            try:
                ids = store.save(uid, draft_id, split=split)
            except ReceiptError as err:
                return await callback.answer(str(err)[:190], show_alert=True)
            operations = [db.operation(uid, record_id) for record_id in ids]
            await state.clear()
            label = "✅ Чек записан.\n" + "\n".join(
                f"#{r['id']} {r['description']} — {rub(r['amount_minor'])} ({r['category_name']})"
                for r in operations
            )
            if len(label) > 3800:
                label = f"✅ Чек записан. Добавлено операций: {len(ids)}. Список: /history"
            await callback.message.edit_text(label, reply_markup=receipt_keyboard(
                f"{draft['chat_id']}:{draft['message_id']}",
                [(r["id"], r["description"]) for r in operations]
            ))
        elif action == "discard":
            store.discard(uid, draft_id)
            await state.clear()
            await callback.message.edit_text("Чек отменён. Расходы не добавлены.")
        elif action in ("amount", "name"):
            await state.set_state(ReceiptInput.amount if action == "amount" else ReceiptInput.description)
            await state.update_data(receipt_draft_id=draft_id)
            await callback.message.answer(
                ("Введи правильную итоговую сумму чека" if action == "amount"
                 else "Введи название покупки") + ".\n/cancel — отмена ввода.",
                reply_markup=ForceReply(selective=True),
            )
        elif action == "sources":
            page = int(args[3]) if len(args) > 3 and args[3].isdigit() else 0
            await callback.message.edit_text("Выбери источник списания:",
                                             reply_markup=sources_markup(db, uid, draft_id, page, draft["source_id"]))
        elif action == "source":
            if len(args) < 4 or not args[3].isdigit() or not store.update(uid, draft_id, "source_id", int(args[3])):
                return await callback.answer("Источник не найден.", show_alert=True)
            label, keyboard = preview(db, store.draft(uid, draft_id), timezone)
            await callback.message.edit_text(label, reply_markup=keyboard)
        elif action == "back":
            label, keyboard = preview(db, draft, timezone)
            await callback.message.edit_text(label, reply_markup=keyboard)
        else:
            return await callback.answer("Неизвестная кнопка.", show_alert=True)
        await callback.answer()

    @router.message(ReceiptInput.amount)
    async def receipt_amount(message: Message, state: FSMContext):
        if not message.text or message.text.startswith("/"):
            return await message.answer("Введи только сумму, например 245,50, либо /cancel.")
        try:
            value = minor(message.text.strip().replace(" ₽", "").replace(" руб", ""))
        except ParseError as err:
            return await message.answer(str(err) + " Повтори ввод или /cancel.")
        draft_id = (await state.get_data())["receipt_draft_id"]
        if not store.update(message.from_user.id, draft_id, "total_minor", value):
            await state.clear()
            return await message.answer("Чек уже обработан или недоступен.")
        await state.clear()
        label, keyboard = preview(db, store.draft(message.from_user.id, draft_id), timezone)
        await message.answer("Сумма изменена.\n\n" + label, reply_markup=keyboard)

    @router.message(ReceiptInput.description)
    async def receipt_description(message: Message, state: FSMContext):
        if not message.text or message.text.startswith("/"):
            return await message.answer("Напиши название или /cancel.")
        draft_id = (await state.get_data())["receipt_draft_id"]
        try:
            changed = store.update(message.from_user.id, draft_id, "description", message.text)
        except ReceiptError as err:
            return await message.answer(str(err) + " Повтори ввод или /cancel.")
        await state.clear()
        if not changed:
            return await message.answer("Чек уже обработан или недоступен.")
        label, keyboard = preview(db, store.draft(message.from_user.id, draft_id), timezone)
        await message.answer("Название изменено.\n\n" + label, reply_markup=keyboard)
