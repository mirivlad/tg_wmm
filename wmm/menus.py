"""Inline Telegram menus and short-lived input dialogues for editing expenses/categories."""
from __future__ import annotations

import math
import sqlite3

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .db import Database
from .parser import ParseError, minor, rub

PAGE_SIZE = 8


class Input(StatesGroup):
    description = State()
    amount = State()
    new_expense_category = State()
    new_category = State()
    rename_category = State()


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def buttons(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def receipt_keyboard(batch_key: str, records: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = []
    for record_id, name in records:
        rows.append([btn(f"✏️ Исправить #{record_id} {name[:23]}", f"ui:edit:{record_id}")])
    rows.append([btn("↩️ Отменить запись", f"undo:{batch_key}")])
    rows.append([btn("📊 Отчёт за месяц", "report:month")])
    return buttons(*rows)


def history_keyboard(records) -> InlineKeyboardMarkup:
    rows = [[btn(f"✏️ #{r['id']} {r['description'][:24]}", f"ui:edit:{r['id']}")]
            for r in records if r["kind"] == "expense"]
    rows.append([btn("📁 Категории", "ui:cats:0")])
    return buttons(*rows)


def edit_menu(record) -> tuple[str, InlineKeyboardMarkup]:
    operation_id = record["id"]
    if record["kind"] != "expense":
        return (f"#{operation_id} — погашение кредита. Для изменения разбивки пока удали "
                "операцию командой /delete и отправь платёж заново.",
                buttons([btn("📜 История", "ui:history")]))
    text = (f"✏️ Расход #{operation_id}\n"
            f"Название: {record['description']}\n"
            f"Сумма: {rub(record['amount_minor'])}\n"
            f"Категория: {record['category_name']}\n"
            f"Источник: {record['source_name']}")
    keyboard = buttons(
        [btn("📝 Название", f"ui:field:n:{operation_id}"),
         btn("💰 Сумма", f"ui:field:a:{operation_id}")],
        [btn("📁 Категория", f"ui:select:{operation_id}:0")],
        [btn("📜 К истории", "ui:history")],
    )
    return text, keyboard


def category_picker(db: Database, user_id: int, operation_id: int, page: int):
    all_categories = db.categories(user_id)
    page = max(0, min(page, max(0, math.ceil(len(all_categories) / PAGE_SIZE) - 1)))
    visible = all_categories[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    rows = [[btn(r["name"], f"ui:pick:{operation_id}:{r['id']}")] for r in visible]
    paging = []
    if page:
        paging.append(btn("◀️", f"ui:select:{operation_id}:{page - 1}"))
    if len(all_categories) > (page + 1) * PAGE_SIZE:
        paging.append(btn("▶️", f"ui:select:{operation_id}:{page + 1}"))
    if paging:
        rows.append(paging)
    rows.extend([
        [btn("➕ Добавить категорию", f"ui:new:{operation_id}")],
        [btn("↩️ Назад", f"ui:edit:{operation_id}")],
    ])
    return f"Выбери категорию для расхода #{operation_id} (страница {page + 1}):", buttons(*rows)


def category_manager(db: Database, user_id: int, page: int):
    categories = db.categories(user_id)
    pages = max(1, math.ceil(len(categories) / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    current = categories[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    rows = [[btn(f"{r['name']} ({r['uses']})", f"ui:manage:{r['id']}:{page}")] for r in current]
    paging = []
    if page:
        paging.append(btn("◀️", f"ui:cats:{page - 1}"))
    if page + 1 < pages:
        paging.append(btn("▶️", f"ui:cats:{page + 1}"))
    if paging:
        rows.append(paging)
    rows.append([btn("➕ Добавить категорию", f"ui:make:{page}")])
    return (f"📁 Категории — {page + 1}/{pages}\n"
            "Выбери категорию, чтобы переименовать или удалить.\n"
            "Число в скобках — количество расходов.", buttons(*rows))


def category_detail(row, page: int):
    category_id = row["id"]
    protected = row["name"].casefold() == "прочее"
    rows = []
    if not protected:
        rows.append([btn("✏️ Переименовать", f"ui:rename:{category_id}:{page}")])
        rows.append([btn("🗑 Удалить", f"ui:remove:{category_id}:{page}")])
    rows.append([btn("↩️ К категориям", f"ui:cats:{page}")])
    return (f"Категория: {row['name']}\nРасходов: {row['uses']}" +
            ("\n«Прочее» — системная категория; удалить её нельзя." if protected else ""),
            buttons(*rows))


def register_menus(router: Router, db: Database) -> None:
    """Register BEFORE the generic text handler. Router-level access middleware remains in effect."""

    @router.message(Command("cancel"))
    async def cancel_input(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Ввод отменён. Обычные расходы можно отправлять как раньше.")

    @router.callback_query(F.data.startswith("ui:"))
    async def on_menu(callback: CallbackQuery, state: FSMContext):
        if not isinstance(callback.message, Message):
            return await callback.answer("Меню устарело. Открой /history или /categories.", show_alert=True)
        user_id = callback.from_user.id
        parts = callback.data.split(":")
        action = parts[1]
        await state.clear()

        def operation_from(value: str):
            try:
                return db.operation(user_id, int(value))
            except ValueError:
                return None

        try:
            if action == "history":
                history = db.history(user_id)
                text = ("Последние операции:\n" + "\n".join(
                    f"#{r['id']} {r['description']} — {rub(r['amount_minor'])}"
                    for r in history
                )) if history else "История пуста."
                await callback.message.edit_text(text, reply_markup=history_keyboard(history))
            elif action == "edit":
                operation = operation_from(parts[2])
                if not operation:
                    return await callback.answer("Запись не найдена.", show_alert=True)
                label, keyboard = edit_menu(operation)
                await callback.message.edit_text(label, reply_markup=keyboard)
            elif action == "field":
                field = parts[2]
                record = operation_from(parts[3])
                if record is None or record["kind"] != "expense":
                    return await callback.answer("Расход не найден.", show_alert=True)
                if field not in ("n", "a"):
                    return await callback.answer("Неизвестное поле.", show_alert=True)
                await state.set_state(Input.description if field == "n" else Input.amount)
                await state.update_data(operation_id=record["id"])
                prompt = ("Введи новое название" if field == "n" else "Введи новую сумму") + (
                    f" для расхода #{record['id']}.\n/cancel — отменить.")
                await callback.message.answer(prompt, reply_markup=ForceReply(selective=True))
            elif action == "select":
                record = operation_from(parts[2])
                if record is None or record["kind"] != "expense":
                    return await callback.answer("Расход не найден.", show_alert=True)
                label, keyboard = category_picker(db, user_id, record["id"], int(parts[3]))
                await callback.message.edit_text(label, reply_markup=keyboard)
            elif action == "pick":
                record_id, category_id = int(parts[2]), int(parts[3])
                if not db.assign_category(user_id, record_id, category_id):
                    return await callback.answer("Запись или категория уже удалена.", show_alert=True)
                label, keyboard = edit_menu(db.operation(user_id, record_id))
                await callback.message.edit_text("Категория изменена и запомнена.\n\n" + label,
                                                 reply_markup=keyboard)
            elif action == "new":
                record = operation_from(parts[2])
                if record is None or record["kind"] != "expense":
                    return await callback.answer("Расход не найден.", show_alert=True)
                await state.set_state(Input.new_expense_category)
                await state.update_data(operation_id=record["id"])
                await callback.message.answer(
                    f"Введи название новой категории для расхода #{record['id']}.\n"
                    "Она будет создана и сразу назначена расходу.\n/cancel — отмена.",
                    reply_markup=ForceReply(selective=True))
            elif action == "cats":
                label, keyboard = category_manager(db, user_id, int(parts[2]))
                await callback.message.edit_text(label, reply_markup=keyboard)
            elif action == "manage":
                row = db.category(user_id, int(parts[2]))
                if row is None:
                    return await callback.answer("Категория удалена.", show_alert=True)
                label, keyboard = category_detail(row, int(parts[3]))
                await callback.message.edit_text(label, reply_markup=keyboard)
            elif action == "make":
                await state.set_state(Input.new_category)
                await state.update_data(page=int(parts[2]))
                await callback.message.answer(
                    "Введи название новой категории.\n/cancel — отмена.",
                    reply_markup=ForceReply(selective=True))
            elif action == "rename":
                row = db.category(user_id, int(parts[2]))
                if row is None:
                    return await callback.answer("Категория удалена.", show_alert=True)
                if row["name"].casefold() == "прочее":
                    return await callback.answer("«Прочее» нельзя переименовывать.", show_alert=True)
                await state.set_state(Input.rename_category)
                await state.update_data(category_id=row["id"], page=int(parts[3]))
                await callback.message.answer(
                    f"Новое название категории «{row['name']}»:\n/cancel — отмена.",
                    reply_markup=ForceReply(selective=True))
            elif action == "remove":
                row = db.category(user_id, int(parts[2]))
                if row is None:
                    return await callback.answer("Категория удалена.", show_alert=True)
                if row["name"].casefold() == "прочее":
                    return await callback.answer("«Прочее» нельзя удалять.", show_alert=True)
                page = int(parts[3])
                await callback.message.edit_text(
                    f"Удалить категорию «{row['name']}»?\n"
                    f"Связанных расходов: {row['uses']}.\n"
                    "Они сохранятся, но попадут в «Прочее».",
                    reply_markup=buttons(
                        [btn("🗑 Да, удалить", f"ui:confirm:{row['id']}:{page}")],
                        [btn("↩️ Не удалять", f"ui:manage:{row['id']}:{page}")]))
            elif action == "confirm":
                moved = db.delete_category(user_id, int(parts[2]))
                if moved is None:
                    return await callback.answer("Категория уже удалена.", show_alert=True)
                label, keyboard = category_manager(db, user_id, int(parts[3]))
                await callback.message.edit_text(
                    f"Категория удалена. Перенесено в «Прочее»: {moved}.\n\n" + label,
                    reply_markup=keyboard)
            else:
                return await callback.answer("Неизвестная кнопка.", show_alert=True)
        except (ValueError, sqlite3.IntegrityError) as exc:
            return await callback.answer(f"Не удалось выполнить действие: {str(exc)[:100]}", show_alert=True)
        await callback.answer()

    @router.message(Input.description)
    async def input_description(message: Message, state: FSMContext):
        if not message.text or message.text.startswith("/"):
            return await message.answer("Напиши только новое название или /cancel.")
        info = await state.get_data()
        record_id = info["operation_id"]
        try:
            changed = db.update_description(message.from_user.id, record_id, message.text)
        except ValueError as exc:
            return await message.answer(str(exc) + "\nПопробуй ещё раз или /cancel.")
        await state.clear()
        if not changed:
            return await message.answer("Расход удалён или недоступен.")
        label, keyboard = edit_menu(db.operation(message.from_user.id, record_id))
        await message.answer("Название изменено.\n\n" + label, reply_markup=keyboard)

    @router.message(Input.amount)
    async def input_amount(message: Message, state: FSMContext):
        if not message.text or message.text.startswith("/"):
            return await message.answer("Введи сумму числом, например 450,50, или /cancel.")
        try:
            value = minor(message.text.strip().replace(" ₽", "").replace(" руб", ""))
        except ParseError as exc:
            return await message.answer(str(exc) + " Попробуй ещё раз или /cancel.")
        info = await state.get_data()
        record_id = info["operation_id"]
        changed = db.update_amount(message.from_user.id, record_id, value)
        await state.clear()
        if not changed:
            return await message.answer("Расход удалён или недоступен.")
        label, keyboard = edit_menu(db.operation(message.from_user.id, record_id))
        await message.answer("Сумма изменена.\n\n" + label, reply_markup=keyboard)

    @router.message(Input.new_expense_category)
    async def input_new_expense_category(message: Message, state: FSMContext):
        if not message.text or message.text.startswith("/"):
            return await message.answer("Введи название категории или /cancel.")
        info = await state.get_data()
        try:
            category_id = db.create_and_assign_category(
                message.from_user.id, info["operation_id"], message.text)
        except (ValueError, sqlite3.IntegrityError) as exc:
            return await message.answer(f"{exc}\nПопробуй снова или /cancel.")
        await state.clear()
        if category_id is None:
            return await message.answer("Расход удалён или недоступен.")
        label, keyboard = edit_menu(db.operation(message.from_user.id, info["operation_id"]))
        await message.answer("Категория добавлена и назначена.\n\n" + label, reply_markup=keyboard)

    @router.message(Input.new_category)
    async def input_new_category(message: Message, state: FSMContext):
        if not message.text or message.text.startswith("/"):
            return await message.answer("Введи название категории или /cancel.")
        try:
            db.add_category(message.from_user.id, message.text)
        except (ValueError, sqlite3.IntegrityError) as exc:
            return await message.answer(f"{exc}\nПопробуй ещё раз или /cancel.")
        info = await state.get_data()
        await state.clear()
        label, keyboard = category_manager(db, message.from_user.id, info.get("page", 0))
        await message.answer("Категория добавлена.\n\n" + label, reply_markup=keyboard)

    @router.message(Input.rename_category)
    async def input_rename_category(message: Message, state: FSMContext):
        if not message.text or message.text.startswith("/"):
            return await message.answer("Введи новое название или /cancel.")
        info = await state.get_data()
        try:
            changed = db.rename_category(message.from_user.id, info["category_id"], message.text)
        except (ValueError, sqlite3.IntegrityError) as exc:
            return await message.answer(f"{exc}\nПопробуй ещё раз или /cancel.")
        await state.clear()
        if not changed:
            return await message.answer("Категория удалена или недоступна.")
        row = db.category(message.from_user.id, info["category_id"])
        label, keyboard = category_detail(row, info.get("page", 0))
        await message.answer("Категория переименована.\n\n" + label, reply_markup=keyboard)
