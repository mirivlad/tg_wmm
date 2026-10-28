"""Telegram chat interface for a single-user-first expense journal."""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Settings
from .db import Database
from .menus import category_manager, history_keyboard, receipt_keyboard, register_menus
from .parser import ParseError, minor, parse_expenses, parse_repayment, rub
from .receipt_store import ReceiptStore
from .receipt_ui import register_receipts

LOG = logging.getLogger("wmm")

HELP = (
    "Where My Money — учёт расходов без форм.\n\n"
    "Просто напиши:\n"
    "сиги 210, обед 130, заправка 3500\n"
    "вчера кофе 150 кредитка1\n"
    "погасил кредитка1 9000, зачислилось 6000\n\n"
    "Команды:\n"
    "/today — сегодня; /month — месяц\n"
    "/report [today|week|month|YYYY-MM] — отчёт\n"
    "/history — история; /undo — отменить последнее сообщение\n"
    "/edit ID СУММА — изменить сумму\n"
    "/category ID НАЗВАНИЕ — поменять категорию и обучить бота\n"
    "/delete ID — удалить запись\n"
    "/categories — категории; /category_add НАЗВАНИЕ\n"
    "/sources — источники и их ID\n"
    "/source_add НАЗВАНИЕ | credit/debit/cash | АЛИАС,АЛИАС\n"
    "/source_default ID — основной источник\n"
    "/export — выгрузка всех операций в CSV\n"
    "/cancel — отменить ввод исправления\n"
    "Исправление записи и управление категориями доступны кнопками.\n"
    "/help — эта справка\n\n"
    "Отправь фото чека, изображение-файл или текст фискального QR: сначала проверишь распознавание, затем подтвердишь. Голос добавим позже. "
    "Для погашения кредитки необходима разбивка по основному долгу и процентам."
)


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_ids: frozenset[int], db: Database):
        self.allowed_ids = allowed_ids
        self.db = db

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        message = event if isinstance(event, Message) else getattr(event, "message", None)
        if not user or user.id not in self.allowed_ids or not message or message.chat.type != "private":
            return None
        self.db.ensure_user(user.id)
        return await handler(event, data)


def date_interval(period: str, tz: str) -> tuple[str, str, str]:
    zone = ZoneInfo(tz)
    today = datetime.now(zone).date()
    name = period.lower()
    if name in ("today", "сегодня", ""):
        begin = today
        end = today + timedelta(days=1)
        title = "Сегодня"
    elif name in ("week", "неделя"):
        begin = today - timedelta(days=today.weekday())
        end = begin + timedelta(days=7)
        title = "Текущая неделя"
    elif name in ("month", "месяц"):
        begin = today.replace(day=1)
        end = begin.replace(year=begin.year + 1, month=1) if begin.month == 12 else begin.replace(month=begin.month + 1)
        title = "Текущий месяц"
    elif re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", name):
        yy, mm = (int(x) for x in name.split("-"))
        begin = today.replace(year=yy, month=mm, day=1)
        end = begin.replace(year=yy + 1, month=1) if mm == 12 else begin.replace(month=mm + 1)
        title = f"{mm:02d}.{yy}"
    else:
        raise ValueError("Укажи период: today, week, month или YYYY-MM.")
    utc_begin = datetime.combine(begin, datetime.min.time(), zone).astimezone(timezone.utc).isoformat()
    utc_end = datetime.combine(end, datetime.min.time(), zone).astimezone(timezone.utc).isoformat()
    return utc_begin, utc_end, title


def report_text(db: Database, uid: int, period: str, tz: str) -> str:
    begin, end, title = date_interval(period, tz)
    result = db.report(uid, begin, end)
    lines = [f"Расходы — {title}", f"По категориям: {rub(result['expenses'])}",
             f"Списано с некредитных источников: {rub(result['cash_out'])}"]
    if result["repayments"]:
        lines.append(f"В том числе платежи по кредиткам: {rub(result['repayments'])} "
                     "(основной долг не дублируется в расходах)")
    if result["categories"]:
        lines.extend(["", "Категории:"])
        lines.extend(f"• {name}: {rub(amount)}" for name, amount in result["categories"])
        lines.extend(["", "Источники расходов:"])
        lines.extend(f"• {name}: {rub(amount)}" for name, amount in result["sources"])
    else:
        lines.append("Записей нет.")
    lines.append("\nОстатки и задолженности появятся после добавления учёта доходов.")
    return "\n".join(lines)


def keyboard(batch_key: str | None = None) -> InlineKeyboardMarkup:
    buttons = []
    if batch_key:
        buttons.append(InlineKeyboardButton(text="Отменить запись", callback_data=f"undo:{batch_key}"))
    buttons.append(InlineKeyboardButton(text="Отчёт за месяц", callback_data="report:month"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def make_router(settings: Settings, db: Database) -> Router:
    router = Router(name="wmm")
    access = AccessMiddleware(settings.allowed_ids, db)
    router.message.middleware(access)
    router.callback_query.middleware(access)

    @router.message(CommandStart())
    async def on_start(message: Message):
        await message.answer("Привет! Я запоминаю расходы и показываю, куда уходят деньги.\n\n" + HELP)

    @router.message(Command("help"))
    async def on_help(message: Message):
        await message.answer(HELP)

    @router.message(Command("sources"))
    async def on_sources(message: Message):
        names = {"debit": "Дебетовый", "credit": "Кредитка", "cash": "Наличные", "other": "Прочее"}
        lines = ["Источники расходов:"]
        for s in db.sources(message.from_user.id):
            lines.append(f"#{s['id']} {s['name']} — {names[s['kind']]}" +
                         (" (по умолчанию)" if s["is_default"] else ""))
        lines.append("\nДобавить: /source_add Кредитка 1 | credit | кредитка1,к1")
        await message.answer("\n".join(lines))

    @router.message(Command("source_add"))
    async def on_source_add(message: Message):
        parts = [x.strip() for x in (message.text or "").partition(" ")[2].split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return await message.answer("Пример: /source_add Кредитка 1 | credit | кредитка1,к1")
        aliases = [a.strip() for a in parts[2].split(",")] if len(parts) > 2 else []
        try:
            db.add_source(message.from_user.id, parts[0], parts[1].lower(), aliases)
        except (ValueError, sqlite3.IntegrityError):
            return await message.answer("Не получилось добавить источник: проверь название, тип и уникальность алиасов.")
        await message.answer(f"Источник «{parts[0]}» добавлен. Посмотреть ID: /sources")

    @router.message(Command("source_default"))
    async def on_source_default(message: Message):
        arg = (message.text or "").partition(" ")[2].strip()
        if not arg.isdigit() or not db.default_source(message.from_user.id, int(arg)):
            return await message.answer("Пример: /source_default 2 (ID из /sources)")
        await message.answer("Источник по умолчанию изменён.")

    @router.message(Command("categories"))
    async def on_categories(message: Message):
        text, reply = category_manager(db, message.from_user.id, 0)
        await message.answer(text, reply_markup=reply)

    @router.message(Command("category_add"))
    async def on_category_add(message: Message):
        name = (message.text or "").partition(" ")[2].strip()
        try:
            db.add_category(message.from_user.id, name)
        except ValueError as exc:
            return await message.answer(str(exc))
        except sqlite3.IntegrityError:
            return await message.answer("Такая категория уже существует.")
        await message.answer("Категория добавлена.")

    @router.message(Command("history"))
    async def on_history(message: Message):
        rows = db.history(message.from_user.id)
        if not rows:
            return await message.answer("Пока нет записей.")
        lines = ["Последние операции:"]
        for r in rows:
            if r["kind"] == "repayment":
                label = f"Погашение {r['target_name']}"
            else:
                label = f"{r['description']} [{r['category_name']}]"
            stamp = r["occurred_at"][:10]
            lines.append(f"#{r['id']} {stamp}: {label} — {rub(r['amount_minor'])}, {r['source_name']}")
        lines.append("\nИзменить: /edit ID СУММА или /category ID НАЗВАНИЕ")
        await message.answer("\n".join(lines), reply_markup=history_keyboard(rows))

    @router.message(Command("undo"))
    async def on_undo(message: Message):
        count = db.undo(message.from_user.id)
        await message.answer(f"Отменено операций: {count}.")

    @router.message(Command("delete"))
    async def on_delete(message: Message):
        value = (message.text or "").partition(" ")[2].strip()
        ok = value.isdigit() and db.delete(message.from_user.id, int(value))
        await message.answer("Запись удалена." if ok else "Не вижу такую запись. ID — в /history.")

    @router.message(Command("edit"))
    async def on_edit(message: Message):
        parts = (message.text or "").split()
        if len(parts) != 3 or not parts[1].isdigit():
            return await message.answer("Пример: /edit 42 450")
        try:
            amount = minor(parts[2])
        except ParseError as exc:
            return await message.answer(str(exc))
        ok = db.update_amount(message.from_user.id, int(parts[1]), amount)
        await message.answer("Сумма исправлена." if ok else "Такой расход не найден (погашение меняется через удаление и новый ввод).")

    @router.message(Command("category"))
    async def on_category(message: Message):
        match = re.fullmatch(r"/category(?:@\w+)?\s+(\d+)\s+(.+)", message.text or "", re.S)
        if not match:
            return await message.answer("Пример: /category 42 Табак")
        try:
            ok = db.update_category(message.from_user.id, int(match.group(1)), match.group(2).strip())
        except ValueError as exc:
            return await message.answer(str(exc) + " Список: /categories")
        await message.answer("Категория изменена, название покупки запомнил." if ok else "Не вижу такой расход.")

    @router.message(Command("today"))
    async def on_today(message: Message):
        await message.answer(report_text(db, message.from_user.id, "today", settings.timezone))

    @router.message(Command("month"))
    async def on_month(message: Message):
        await message.answer(report_text(db, message.from_user.id, "month", settings.timezone))

    @router.message(Command("report"))
    async def on_report(message: Message):
        try:
            await message.answer(report_text(db, message.from_user.id,
                                             (message.text or "").partition(" ")[2].strip() or "month",
                                             settings.timezone))
        except ValueError as exc:
            await message.answer(str(exc))

    @router.message(Command("export"))
    async def on_export(message: Message):
        contents = db.export_csv(message.from_user.id)
        await message.answer_document(BufferedInputFile(contents, filename="wmm-operations.csv"),
                                      caption="CSV-архив операций. Внутри есть первоначальный текст записей.")

    @router.callback_query(F.data.startswith("undo:"))
    async def on_undo_button(callback: CallbackQuery):
        key = callback.data.removeprefix("undo:")
        # All operations in the batch are owned by the callback user.
        count = db.delete_batch(callback.from_user.id, key)
        await callback.answer("Удалено: " + str(count), show_alert=True)
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(F.data == "report:month")
    async def on_month_button(callback: CallbackQuery):
        if callback.message:
            await callback.message.answer(report_text(db, callback.from_user.id, "month", settings.timezone))
        await callback.answer()

    receipt_store = ReceiptStore(db)
    register_receipts(router, db, receipt_store, settings.timezone)
    register_menus(router, db)

    @router.message(F.text)
    async def on_text(message: Message):
        uid = message.from_user.id
        raw = message.text.strip()
        if raw.startswith("/"):
            return await message.answer("Не знаю такую команду. /help")
        if re.search(r"^(?:сколько|расходы|траты|отчёт|отчет)\b", raw, re.I):
            period = "today" if "сегодня" in raw.lower() else "month"
            return await message.answer(report_text(db, uid, period, settings.timezone))
        aliases, default = db.source_aliases(uid)
        try:
            repay = parse_repayment(raw, aliases, default, settings.timezone)
            if repay is not None:
                op = db.save_repayment(uid, message.chat.id, message.message_id, repay)
                if op is None:
                    return await message.answer("Это сообщение уже учтено.")
                await message.answer(
                    f"Погашение #{op} записано.\nСписано: {rub(repay.amount_minor)}\n"
                    f"Основной долг: {rub(repay.principal_minor)}\n"
                    f"Проценты и комиссии: {rub(repay.interest_minor)}",
                    reply_markup=keyboard(f"{message.chat.id}:{message.message_id}"),
                )
                return
            items = parse_expenses(raw, aliases, default, db.learned_aliases(uid), settings.timezone)
        except ParseError as exc:
            return await message.answer("Не записано. " + str(exc))
        ids = db.save_expenses(uid, message.chat.id, message.message_id, items, raw)
        if not ids:
            return await message.answer("Это сообщение уже учтено.")
        lines = [f"Записано операций: {len(ids)}"]
        for op, item in zip(ids, items):
            saved = db.operation(uid, op)
            suffix = " (не уверен — нажми кнопку исправления)" if saved["unclassified"] else ""
            lines.append(f"#{op} {item.description}: {rub(item.amount_minor)} — {saved['category_name']}{suffix}")
        lines.append(f"\nВсего: {rub(sum(x.amount_minor for x in items))}")
        await message.answer("\n".join(lines),
                             reply_markup=receipt_keyboard(f"{message.chat.id}:{message.message_id}", [(i, x.description) for i, x in zip(ids, items)]))

    @router.message()
    async def on_media(message: Message):
        await message.answer("Отправь чек фотографией или JPEG/PNG/WebP-файлом. Голос пока не поддерживается.")

    return router


async def run() -> None:
    settings = Settings.from_env()
    db = Database(settings.database_path)
    db.initialize()
    ReceiptStore(db).initialize()
    session = AiohttpSession(proxy=settings.proxy_url) if settings.proxy_url else AiohttpSession()
    bot = Bot(token=settings.bot_token, session=session)
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(settings, db))
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        LOG.info("Where My Money started (long polling).")
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())
