"""Durable receipt drafts and atomic conversion to ordinary expense operations."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from .db import Database
from .parser import CATEGORIES, category_for
from .receipts import Recognized, ReceiptError

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS receipt_drafts (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  chat_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  receipt_key TEXT NOT NULL,
  qr_text TEXT NOT NULL,
  ocr_text TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  description TEXT NOT NULL,
  total_minor INTEGER,
  items_json TEXT NOT NULL,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  category_id INTEGER REFERENCES categories(id),
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','saved','discarded')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(user_id, chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS receipt_imports (
  user_id INTEGER NOT NULL REFERENCES users(id),
  receipt_key TEXT NOT NULL,
  batch_key TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(user_id, receipt_key)
);
"""


class ReceiptStore:
    def __init__(self, db: Database):
        self.db = db

    def initialize(self):
        with self.db.connection() as conn:
            conn.executescript(CREATE_SQL)
            columns = {r['name'] for r in conn.execute('PRAGMA table_info(receipt_drafts)')}
            if 'category_id' not in columns:
                conn.execute('ALTER TABLE receipt_drafts ADD COLUMN category_id INTEGER REFERENCES categories(id)')

    def draft(self, user_id: int, draft_id: int) -> sqlite3.Row | None:
        with self.db.connection() as conn:
            return conn.execute("SELECT * FROM receipt_drafts WHERE id=? AND user_id=?",
                                (draft_id, user_id)).fetchone()

    @staticmethod
    def _existing_receipt(conn, user_id: int, receipt_key: str) -> bool:
        row = conn.execute("SELECT batch_key FROM receipt_imports WHERE user_id=? AND receipt_key=?",
                           (user_id, receipt_key)).fetchone()
        if not row:
            return False
        if conn.execute("SELECT 1 FROM operations WHERE user_id=? AND batch_key=?",
                        (user_id, row["batch_key"])).fetchone():
            return True
        # The user deleted all operations attached to this receipt: allow importing again.
        conn.execute("DELETE FROM receipt_imports WHERE user_id=? AND receipt_key=?",
                     (user_id, receipt_key))
        return False

    def create_draft(self, user_id: int, chat_id: int, message_id: int, result: Recognized) -> int:
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self._existing_receipt(conn, user_id, result.receipt_key):
                raise ReceiptError("Этот чек уже записан. Проверь /history.")
            previous = conn.execute(
                "SELECT id FROM receipt_drafts WHERE user_id=? AND chat_id=? AND message_id=?",
                (user_id, chat_id, message_id)).fetchone()
            if previous:
                return previous["id"]
            source = conn.execute("SELECT default_source_id FROM users WHERE id=?", (user_id,)).fetchone()
            if not source:
                raise ReceiptError("Сначала открой /start.")
            cursor = conn.execute(
                "INSERT INTO receipt_drafts (user_id,chat_id,message_id,receipt_key,qr_text,ocr_text,"
                "occurred_at,description,total_minor,items_json,source_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, chat_id, message_id, result.receipt_key, result.qr_text,
                 result.ocr_text, result.occurred_at, result.description, result.total_minor,
                 json.dumps([asdict(x) for x in result.items], ensure_ascii=False),
                 source["default_source_id"]),
            )
            return cursor.lastrowid

    def update(self, user_id: int, draft_id: int, field: str, value: str | int) -> bool:
        if field not in ("total_minor", "description", "source_id"):
            raise ValueError("Invalid editable field.")
        if field == "total_minor":
            if not isinstance(value, int) or value <= 0:
                raise ReceiptError("Сумма должна быть положительной.")
        elif field == "description":
            value = " ".join(str(value).strip().split())
            if not value or len(value) > 200 or value.startswith("/"):
                raise ReceiptError("Название покупки: 1–200 символов.")
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if field == "source_id":
                if not conn.execute("SELECT 1 FROM sources WHERE user_id=? AND id=?",
                                    (user_id, value)).fetchone():
                    return False
            result = conn.execute(f"UPDATE receipt_drafts SET {field}=? "
                                  "WHERE id=? AND user_id=? AND status='pending'",
                                  (value, draft_id, user_id))
            return bool(result.rowcount)

    def _category(self, conn, user_id: int, description: str, preferred: int | None = None):
        if preferred is not None:
            selected = conn.execute(
                "SELECT id,name FROM categories WHERE id=? AND user_id=?",
                (preferred, user_id),
            ).fetchone()
            if selected is None:
                raise ReceiptError("Выбранная категория больше не существует.")
            return selected, False
        category, unknown = category_for(description, self.db.learned_aliases(user_id))
        selected = self.db._lookup_category(conn, user_id, category)
        if not selected:
            selected = conn.execute(
                "SELECT c.id,c.name FROM category_rules r JOIN categories c ON c.id=r.category_id "
                "WHERE r.user_id=? AND r.builtin_name=?", (user_id, category),
            ).fetchone()
        if not selected:
            selected = self.db._lookup_category(conn, user_id, "Прочее")
            unknown = True
        return selected, unknown

    def category_label(self, user_id: int, description: str, preferred: int | None = None) -> str:
        with self.db.connection() as conn:
            found, _ = self._category(conn, user_id, description, preferred)
            return found["name"]

    def set_category(self, user_id: int, draft_id: int, slot: str,
                     category_id: int) -> bool:
        """Use slot 'all' for one expense, or 0-based item index for itemized."""
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            draft = conn.execute("SELECT * FROM receipt_drafts WHERE id=? AND user_id=? AND status='pending'",
                                 (draft_id, user_id)).fetchone()
            category = conn.execute("SELECT 1 FROM categories WHERE id=? AND user_id=?",
                                    (category_id, user_id)).fetchone()
            if not draft or not category:
                return False
            if slot == "all":
                conn.execute("UPDATE receipt_drafts SET category_id=? WHERE id=?", (category_id, draft_id))
                return True
            if not slot.isdigit():
                return False
            index = int(slot)
            items = json.loads(draft["items_json"])
            if index < 0 or index >= len(items):
                return False
            items[index]["category_id"] = category_id
            conn.execute("UPDATE receipt_drafts SET items_json=? WHERE id=?",
                         (json.dumps(items, ensure_ascii=False), draft_id))
            return True

    def discard(self, user_id: int, draft_id: int) -> bool:
        with self.db.connection() as conn:
            result = conn.execute("UPDATE receipt_drafts SET status='discarded' "
                                  "WHERE id=? AND user_id=? AND status='pending'",
                                  (draft_id, user_id))
            return bool(result.rowcount)

    def save(self, user_id: int, draft_id: int, split: bool = False) -> list[int]:
        """Commit receipt marker and all linked expenses in one DB transaction."""
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            draft = conn.execute(
                "SELECT * FROM receipt_drafts WHERE id=? AND user_id=? AND status='pending'",
                (draft_id, user_id)).fetchone()
            if not draft:
                raise ReceiptError("Чек уже обработан или отменён.")
            if not draft["total_minor"]:
                raise ReceiptError("Сумма не распознана — введи её перед сохранением.")
            if self._existing_receipt(conn, user_id, draft["receipt_key"]):
                raise ReceiptError("Этот чек уже записан. Проверь /history.")
            items = json.loads(draft["items_json"])
            if split and (not items or len(items) > 25 or
                          sum(x["amount_minor"] for x in items) != draft["total_minor"]):
                raise ReceiptError("Суммы товаров не сходятся с итогом чека. Выбери общую сумму.")
            entries = ([(str(x["name"]), int(x["amount_minor"]), x.get("category_id")) for x in items] if split
                       else [(draft["description"], draft["total_minor"], draft["category_id"])])
            if not conn.execute("SELECT 1 FROM sources WHERE id=? AND user_id=?",
                                (draft["source_id"], user_id)).fetchone():
                raise ReceiptError("Выбранный источник больше не существует.")
            batch = f"{draft['chat_id']}:{draft['message_id']}"
            if conn.execute("SELECT 1 FROM operations WHERE user_id=? AND batch_key=?",
                            (user_id, batch)).fetchone():
                raise ReceiptError("Для этого сообщения уже существуют операции.")
            learned = self.db.learned_aliases(user_id)
            ids = []
            original = ("Фискальный QR: " + draft["qr_text"] + "\n" if draft["qr_text"] else "") + draft["ocr_text"]
            for index, (description, amount, selected_category) in enumerate(entries):
                if amount <= 0 or not description:
                    raise ReceiptError("Некорректная товарная позиция.")
                actual, unknown = self._category(conn, user_id, description, selected_category)
                cur = conn.execute(
                    "INSERT INTO operations (user_id,kind,amount_minor,source_id,category_id,"
                    "description,original_text,occurred_at,chat_id,message_id,part_index,batch_key,unclassified) "
                    "VALUES (?,'expense',?,?,?,?,?,?,?,?,?,?,?)",
                    (user_id, amount, draft["source_id"], actual["id"], description, original[:12000],
                     draft["occurred_at"], draft["chat_id"], draft["message_id"], index, batch, int(unknown)),
                )
                ids.append(cur.lastrowid)
            conn.execute("INSERT INTO receipt_imports(user_id,receipt_key,batch_key) VALUES (?,?,?)",
                         (user_id, draft["receipt_key"], batch))
            conn.execute("UPDATE receipt_drafts SET status='saved' WHERE id=?", (draft_id,))
            return ids
