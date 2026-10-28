"""Verify editing conversations through aiogram dispatcher, without Telegram network."""
import tempfile
import unittest
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Message, Update

from wmm.bot import make_router
from wmm.config import Settings
from wmm.db import Database
from wmm.receipt_store import ReceiptStore


class SimulatedBot(Bot):
    def __init__(self):
        super().__init__(token="123456:TEST")
        self.outgoing = []

    async def __call__(self, method, request_timeout=None):
        self.outgoing.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message.model_validate({
                "message_id": 100,
                "date": 1790000000,
                "chat": {"id": 7, "type": "private"},
                "text": getattr(method, "text", "ok"),
            })
        if isinstance(method, AnswerCallbackQuery):
            return True
        raise AssertionError(type(method))


class MenuFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Database(str(Path(folder.name) / "journal.db"))
        self.db.initialize()
        ReceiptStore(self.db).initialize()
        settings = Settings("123456:TEST", frozenset([7]), "unused", "Europe/Moscow", None)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(make_router(settings, self.db))
        self.bot = SimulatedBot()
        self.addAsyncCleanup(self.bot.session.close)
        self.sequence = 0

    async def message(self, text):
        self.sequence += 1
        data = {"update_id": self.sequence, "message": {
            "message_id": self.sequence, "date": 1790000000,
            "from": {"id": 7, "is_bot": False, "first_name": "User"},
            "chat": {"id": 7, "type": "private"}, "text": text}}
        await self.dispatcher.feed_update(self.bot, Update.model_validate(data))

    async def click(self, payload):
        self.sequence += 1
        data = {"update_id": self.sequence, "callback_query": {
            "id": str(self.sequence),
            "from": {"id": 7, "is_bot": False, "first_name": "User"},
            "chat_instance": "test",
            "data": payload,
            "message": {"message_id": 100, "date": 1790000000,
                        "chat": {"id": 7, "type": "private"}, "text": "old"}}}
        await self.dispatcher.feed_update(self.bot, Update.model_validate(data))

    async def test_edit_and_new_category(self):
        await self.message("кофе 130")
        record_id = self.db.history(7)[0]["id"]
        await self.click(f"ui:edit:{record_id}")
        await self.click(f"ui:field:n:{record_id}")
        await self.message("Капучино большой")
        self.assertEqual(self.db.operation(7, record_id)["description"], "Капучино большой")
        self.assertEqual(len(self.db.history(7)), 1)
        await self.click(f"ui:field:a:{record_id}")
        await self.message("225,50")
        self.assertEqual(self.db.operation(7, record_id)["amount_minor"], 22550)
        await self.click(f"ui:new:{record_id}")
        await self.message("Кофе навынос")
        self.assertEqual(self.db.operation(7, record_id)["category_name"], "Кофе навынос")
        self.assertEqual(len(self.db.history(7)), 1)

    async def test_categories_add_rename_remove(self):
        await self.message("/categories")
        await self.click("ui:make:0")
        await self.message("Домашние животные")
        row = next(r for r in self.db.categories(7) if r["name"] == "Домашние животные")
        category_id = row["id"]
        await self.click(f"ui:rename:{category_id}:0")
        await self.message("Питомцы")
        self.assertEqual(self.db.category(7, category_id)["name"], "Питомцы")
        await self.click(f"ui:remove:{category_id}:0")
        await self.click(f"ui:confirm:{category_id}:0")
        self.assertIsNone(self.db.category(7, category_id))

    async def test_pick_existing_category(self):
        await self.message("неизвестная покупка 400")
        record_id = self.db.history(7)[0]["id"]
        transport = next(r for r in self.db.categories(7) if r["name"] == "Транспорт")
        await self.click(f"ui:select:{record_id}:0")
        await self.click(f"ui:pick:{record_id}:{transport['id']}")
        self.assertEqual(self.db.operation(7, record_id)["category_name"], "Транспорт")
        self.assertEqual(self.db.learned_aliases(7)["неизвестная покупка"], "Транспорт")

    async def test_fiscal_qr_preview_and_confirmation(self):
        qr = "t=20261028T1330&s=180.00&fn=000110000105&i=1234567890&fp=123456&n=1"
        await self.message(qr)
        self.assertEqual(len(self.db.history(7)), 0)
        store = ReceiptStore(self.db)
        draft = store.draft(7, 1)
        self.assertEqual(draft["total_minor"], 18000)
        await self.click(f"rc:save:{draft['id']}:all")
        self.assertEqual(len(self.db.history(7)), 1)
        self.assertEqual(self.db.history(7)[0]["amount_minor"], 18000)

    async def test_receipt_edit_amount_then_confirm(self):
        qr = "t=20261028T1330&s=180.00&fn=000110000105&i=1234567890&fp=123456&n=1"
        await self.message(qr)
        draft = ReceiptStore(self.db).draft(7, 1)
        await self.click(f"rc:amount:{draft['id']}")
        await self.message("200,50")
        self.assertEqual(ReceiptStore(self.db).draft(7, draft["id"])["total_minor"], 20050)
        await self.click(f"rc:save:{draft['id']}:all")
        self.assertEqual(self.db.history(7)[0]["amount_minor"], 20050)

    async def test_cancel_edit_then_enter_new_expense(self):
        await self.message("кофе 100")
        record_id = self.db.history(7)[0]["id"]
        await self.click(f"ui:field:n:{record_id}")
        await self.message("/cancel")
        await self.message("еда 300")
        self.assertEqual(len(self.db.history(7)), 2)


if __name__ == "__main__":
    unittest.main()
