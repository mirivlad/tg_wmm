"""SQLite storage. Monetary values are INTEGER kopecks, never floating point."""
from __future__ import annotations

import csv
import io
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .parser import CATEGORIES, Expense, Repayment, normalize

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  default_source_id INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL COLLATE NOCASE,
  kind TEXT NOT NULL CHECK(kind IN ('debit','credit','cash','other')),
  UNIQUE(user_id, name)
);
CREATE TABLE IF NOT EXISTS source_aliases (
  user_id INTEGER NOT NULL REFERENCES users(id),
  alias TEXT NOT NULL,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  PRIMARY KEY(user_id, alias)
);
CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL COLLATE NOCASE,
  UNIQUE(user_id, name)
);
CREATE TABLE IF NOT EXISTS learned_aliases (
  user_id INTEGER NOT NULL REFERENCES users(id),
  alias TEXT NOT NULL,
  category_id INTEGER NOT NULL REFERENCES categories(id),
  PRIMARY KEY(user_id, alias)
);
CREATE TABLE IF NOT EXISTS operations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  kind TEXT NOT NULL CHECK(kind IN ('expense','income','repayment')),
  amount_minor INTEGER NOT NULL CHECK(amount_minor>0),
  source_id INTEGER NOT NULL REFERENCES sources(id),
  target_source_id INTEGER REFERENCES sources(id),
  category_id INTEGER REFERENCES categories(id),
  description TEXT NOT NULL,
  original_text TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  chat_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  part_index INTEGER NOT NULL,
  batch_key TEXT NOT NULL,
  unclassified INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(user_id, chat_id, message_id, part_index)
);
CREATE TABLE IF NOT EXISTS operation_lines (
  operation_id INTEGER NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
  purpose TEXT NOT NULL CHECK(purpose IN ('principal','interest','fee')),
  amount_minor INTEGER NOT NULL CHECK(amount_minor >= 0),
  PRIMARY KEY(operation_id, purpose)
);
CREATE INDEX IF NOT EXISTS ix_operations_user_date ON operations(user_id,occurred_at);
CREATE INDEX IF NOT EXISTS ix_operations_batch ON operations(user_id,batch_key);
PRAGMA user_version=1;
"""


class Database:
    def __init__(self, filename: str):
        self.filename = filename

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.filename, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self) -> None:
        Path(self.filename).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f"Unknown database schema version: {version}")
            db.executescript(SCHEMA)
            db.execute("""
                CREATE TABLE IF NOT EXISTS category_rules (
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    builtin_name TEXT NOT NULL,
                    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                    PRIMARY KEY(user_id, builtin_name)
                )
            """)
            # Also seed links for existing v0.1 databases without touching custom names.
            for user in db.execute("SELECT id FROM users").fetchall():
                self._seed_category_rules(db, user["id"])

    @staticmethod
    def _seed_category_rules(db: sqlite3.Connection, user_id: int) -> None:
        for label in CATEGORIES:
            db.execute(
                "INSERT OR IGNORE INTO category_rules(user_id,builtin_name,category_id) "
                "SELECT ?,?,id FROM categories WHERE user_id=? AND name=? COLLATE NOCASE",
                (user_id, label, user_id, label),
            )

    def ensure_user(self, user_id: int) -> None:
        with self.connection() as db:
            inserted = db.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (user_id,)).rowcount
            if inserted:
                for category in CATEGORIES:
                    db.execute("INSERT INTO categories(user_id,name) VALUES (?,?)", (user_id, category))
                self._seed_category_rules(db, user_id)
            current = db.execute("SELECT default_source_id FROM users WHERE id=?", (user_id,)).fetchone()
            if current["default_source_id"] is None:
                cur = db.execute("INSERT INTO sources(user_id,name,kind) VALUES (?, 'Основной','debit')", (user_id,))
                db.execute("UPDATE users SET default_source_id=? WHERE id=?", (cur.lastrowid, user_id))

    def sources(self, user_id: int) -> list[sqlite3.Row]:
        with self.connection() as db:
            return db.execute(
                "SELECT s.*, (u.default_source_id=s.id) AS is_default FROM sources s "
                "JOIN users u ON u.id=s.user_id WHERE s.user_id=? ORDER BY s.id",
                (user_id,),
            ).fetchall()

    def source_aliases(self, user_id: int) -> tuple[dict[str, int], int]:
        with self.connection() as db:
            default = db.execute("SELECT default_source_id FROM users WHERE id=?", (user_id,)).fetchone()
            mapping = {}
            for row in db.execute("SELECT id,name FROM sources WHERE user_id=?", (user_id,)):
                mapping[row["name"].casefold()] = row["id"]
                mapping[row["name"].replace(" ", "").casefold()] = row["id"]
            for row in db.execute("SELECT alias,source_id FROM source_aliases WHERE user_id=?", (user_id,)):
                mapping[row["alias"].casefold()] = row["source_id"]
            return mapping, default["default_source_id"]

    def category_names(self, user_id: int) -> list[str]:
        return [row["name"] for row in self.categories(user_id)]

    def categories(self, user_id: int) -> list[sqlite3.Row]:
        with self.connection() as db:
            return db.execute("SELECT c.id,c.name,COUNT(o.id) AS uses FROM categories c "
                              "LEFT JOIN operations o ON o.category_id=c.id AND o.user_id=c.user_id "
                              "WHERE c.user_id=? GROUP BY c.id ORDER BY c.name COLLATE NOCASE",
                              (user_id,)).fetchall()

    def category(self, user_id: int, category_id: int) -> sqlite3.Row | None:
        with self.connection() as db:
            return db.execute("SELECT c.id,c.name,COUNT(o.id) AS uses FROM categories c "
                              "LEFT JOIN operations o ON o.category_id=c.id AND o.user_id=c.user_id "
                              "WHERE c.user_id=? AND c.id=? GROUP BY c.id",
                              (user_id, category_id)).fetchone()

    def learned_aliases(self, user_id: int) -> dict[str, str]:
        with self.connection() as db:
            return {r["alias"]: r["name"] for r in db.execute(
                "SELECT l.alias,c.name FROM learned_aliases l JOIN categories c ON c.id=l.category_id WHERE l.user_id=?",
                (user_id,),
            )}

    def add_source(self, user_id: int, name: str, kind: str, aliases: list[str]) -> None:
        if kind not in ("credit", "debit", "cash", "other"):
            raise ValueError("Тип: debit, credit, cash или other.")
        if not name or len(name) > 60:
            raise ValueError("Название источника: 1–60 символов.")
        with self.connection() as db:
            cur = db.execute("INSERT INTO sources(user_id,name,kind) VALUES (?,?,?)", (user_id, name, kind))
            for alias in aliases:
                key = alias.strip().casefold()
                if key and len(key) <= 60:
                    db.execute("INSERT INTO source_aliases(user_id,alias,source_id) VALUES (?,?,?)",
                               (user_id, key, cur.lastrowid))

    def default_source(self, user_id: int, source_id: int) -> bool:
        with self.connection() as db:
            owned = db.execute("SELECT 1 FROM sources WHERE user_id=? AND id=?", (user_id, source_id)).fetchone()
            if owned:
                db.execute("UPDATE users SET default_source_id=? WHERE id=?", (source_id, user_id))
            return bool(owned)

    @staticmethod
    def _clean_category_name(name: str) -> str:
        name = " ".join(name.strip().split())
        if not name or len(name) > 60 or name.startswith("/"):
            raise ValueError("Название категории: от 1 до 60 символов, без начального «/».")
        return name

    def add_category(self, user_id: int, name: str) -> int:
        name = self._clean_category_name(name)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._lookup_category(db, user_id, name):
                raise ValueError("Такая категория уже существует.")
            result = db.execute("INSERT INTO categories(user_id,name) VALUES (?,?)", (user_id, name))
            return result.lastrowid

    def rename_category(self, user_id: int, category_id: int, name: str) -> bool:
        name = self._clean_category_name(name)
        with self.connection() as db:
            old = db.execute("SELECT name FROM categories WHERE user_id=? AND id=?",
                             (user_id, category_id)).fetchone()
            if not old:
                return False
            if old["name"].casefold() == "прочее":
                raise ValueError("Системную категорию «Прочее» нельзя переименовать.")
            if name.casefold() == "прочее":
                raise ValueError("Категория «Прочее» уже существует.")
            existing = self._lookup_category(db, user_id, name)
            if existing and existing["id"] != category_id:
                raise ValueError("Такая категория уже существует.")
            db.execute("UPDATE categories SET name=? WHERE user_id=? AND id=?",
                       (name, user_id, category_id))
            return True

    def delete_category(self, user_id: int, category_id: int) -> int | None:
        """Move existing expenses to Other, delete learned aliases/rules, then category."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            target = db.execute("SELECT name FROM categories WHERE user_id=? AND id=?",
                                (user_id, category_id)).fetchone()
            if not target:
                return None
            if target["name"].casefold() == "прочее":
                raise ValueError("Системную категорию «Прочее» нельзя удалять.")
            other_id = self._category_id(db, user_id, "Прочее")
            count = db.execute("UPDATE operations SET category_id=?, unclassified=1 "
                               "WHERE user_id=? AND category_id=?",
                               (other_id, user_id, category_id)).rowcount
            db.execute("DELETE FROM learned_aliases WHERE user_id=? AND category_id=?",
                       (user_id, category_id))
            db.execute("DELETE FROM category_rules WHERE user_id=? AND category_id=?",
                       (user_id, category_id))
            db.execute("DELETE FROM categories WHERE user_id=? AND id=?",
                       (user_id, category_id))
            return count

    def create_and_assign_category(self, user_id: int, operation_id: int, name: str) -> int | None:
        """Atomic create-and-assign. Return None for a missing/foreign operation."""
        name = self._clean_category_name(name)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT description FROM operations "
                             "WHERE user_id=? AND id=? AND kind='expense'",
                             (user_id, operation_id)).fetchone()
            if not row:
                return None
            # If user typed an existing category, use it instead of creating a duplicate.
            existing = self._lookup_category(db, user_id, name)
            category_id = (existing["id"] if existing else
                           db.execute("INSERT INTO categories(user_id,name) VALUES (?,?)",
                                      (user_id, name)).lastrowid)
            self._assign_category(db, user_id, operation_id, row["description"], category_id)
            return category_id

    @staticmethod
    def _lookup_category(db: sqlite3.Connection, user_id: int, name: str) -> sqlite3.Row | None:
        # SQLite NOCASE only handles ASCII; Russian categories need Unicode case folding.
        for row in db.execute("SELECT id,name FROM categories WHERE user_id=?", (user_id,)):
            if row["name"].casefold() == name.casefold():
                return row
        return None

    @classmethod
    def _category_id(cls, db: sqlite3.Connection, user_id: int, name: str) -> int:
        row = cls._lookup_category(db, user_id, name)
        if not row:
            raise ValueError("Нет такой категории.")
        return row["id"]

    def save_expenses(self, user_id: int, chat_id: int, message_id: int,
                      items: list[Expense], original: str) -> list[int]:
        batch = f"{chat_id}:{message_id}"
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM operations WHERE user_id=? AND batch_key=?",
                          (user_id, batch)).fetchone():
                return []
            ids = []
            for index, item in enumerate(items):
                category_row = self._lookup_category(db, user_id, item.category)
                if category_row is None:
                    category_row = db.execute(
                        "SELECT category_id AS id FROM category_rules "
                        "WHERE user_id=? AND builtin_name=?", (user_id, item.category),
                    ).fetchone()
                if category_row is None and item.category not in CATEGORIES:
                    raise ValueError("Нет такой категории.")
                unclassified = item.unclassified or category_row is None
                category = (category_row["id"] if category_row is not None else
                            self._category_id(db, user_id, "Прочее"))
                cur = db.execute(
                    "INSERT INTO operations (user_id,kind,amount_minor,source_id,category_id,"
                    "description,original_text,occurred_at,chat_id,message_id,part_index,batch_key,unclassified) "
                    "VALUES (?,'expense',?,?,?,?,?,?,?,?,?,?,?)",
                    (user_id, item.amount_minor, item.source_id, category, item.description, original,
                     item.occurred_at, chat_id, message_id, index, batch, int(unclassified)),
                )
                ids.append(cur.lastrowid)
            return ids

    def save_repayment(self, user_id: int, chat_id: int, message_id: int, item: Repayment) -> int | None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM operations WHERE user_id=? AND chat_id=? AND message_id=?",
                          (user_id, chat_id, message_id)).fetchone():
                return None
            cur = db.execute(
                "INSERT INTO operations(user_id,kind,amount_minor,source_id,target_source_id,description,"
                "original_text,occurred_at,chat_id,message_id,part_index,batch_key) "
                "VALUES (?,'repayment',?,?,?,?,?,?,?,?,0,?)",
                (user_id, item.amount_minor, item.source_id, item.target_source_id, "Погашение кредитки",
                 item.raw, item.occurred_at, chat_id, message_id, f"{chat_id}:{message_id}"),
            )
            for purpose, value in (("principal", item.principal_minor), ("interest", item.interest_minor)):
                db.execute("INSERT INTO operation_lines(operation_id,purpose,amount_minor) VALUES (?,?,?)",
                           (cur.lastrowid, purpose, value))
            return cur.lastrowid

    def history(self, user_id: int, limit: int = 15) -> list[sqlite3.Row]:
        with self.connection() as db:
            return db.execute(
                "SELECT o.*,s.name AS source_name,t.name AS target_name,c.name AS category_name "
                "FROM operations o JOIN sources s ON s.id=o.source_id "
                "LEFT JOIN sources t ON t.id=o.target_source_id "
                "LEFT JOIN categories c ON c.id=o.category_id "
                "WHERE o.user_id=? ORDER BY o.id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()

    def operation(self, user_id: int, operation_id: int) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute(
                "SELECT o.*,s.name AS source_name,c.name AS category_name "
                "FROM operations o JOIN sources s ON s.id=o.source_id "
                "LEFT JOIN categories c ON c.id=o.category_id "
                "WHERE o.user_id=? AND o.id=?", (user_id, operation_id)
            ).fetchone()

    def update_description(self, user_id: int, operation_id: int, description: str) -> bool:
        description = " ".join(description.strip().split())
        if not description or len(description) > 200 or description.startswith("/"):
            raise ValueError("Название покупки: от 1 до 200 символов, без начального «/».")
        with self.connection() as conn:
            result = conn.execute(
                "UPDATE operations SET description=? WHERE user_id=? AND id=? AND kind='expense'",
                (description, user_id, operation_id))
            return bool(result.rowcount)

    def update_amount(self, user_id: int, operation_id: int, amount_minor: int) -> bool:
        """Adjust the recorded total of an ordinary expense."""
        with self.connection() as db:
            cur = db.execute("UPDATE operations SET amount_minor=? WHERE id=? AND user_id=? AND kind='expense'",
                             (amount_minor, operation_id, user_id))
            return bool(cur.rowcount)

    def update_category(self, user_id: int, operation_id: int, name: str) -> bool:
        with self.connection() as db:
            row = db.execute("SELECT description FROM operations WHERE id=? AND user_id=? AND kind='expense'",
                             (operation_id, user_id)).fetchone()
            if not row:
                return False
            category = self._category_id(db, user_id, name)
            db.execute("UPDATE operations SET category_id=?,unclassified=0 WHERE id=? AND user_id=?",
                       (category, operation_id, user_id))
            # Only learn exact descriptions, not generalized guesses about similar items.
            key = normalize(row["description"])
            db.execute("INSERT INTO learned_aliases(user_id,alias,category_id) VALUES (?,?,?) "
                       "ON CONFLICT(user_id,alias) DO UPDATE SET category_id=excluded.category_id",
                       (user_id, key, category))
            return True

    @staticmethod
    def _assign_category(conn, owner_id, record_id, label, category_id):
        conn.execute("UPDATE operations SET category_id=? WHERE id=? AND user_id=?",
                     (category_id, record_id, owner_id))
        key = normalize(label)
        if key:
            conn.execute("INSERT INTO learned_aliases(user_id,alias,category_id) VALUES (?,?,?) "
                         "ON CONFLICT(user_id,alias) DO UPDATE SET category_id=excluded.category_id",
                         (owner_id, key, category_id))

    def assign_category(self, user_id, operation_id, category_id):
        with self.connection() as conn:
            record = conn.execute("SELECT description FROM operations WHERE user_id=? AND id=? AND kind='expense'",
                                  (user_id, operation_id)).fetchone()
            category = conn.execute("SELECT id FROM categories WHERE user_id=? AND id=?",
                                    (user_id, category_id)).fetchone()
            if not record or not category:
                return False
            self._assign_category(conn, user_id, operation_id, record["description"], category_id)
            return True

    def delete(self, user_id: int, operation_id: int) -> bool:
        with self.connection() as db:
            cur = db.execute("DELETE FROM operations WHERE user_id=? AND id=?", (user_id, operation_id))
            return bool(cur.rowcount)

    def delete_batch(self, user_id: int, batch_key: str) -> int:
        """Remove only this user's specified Telegram-message batch."""
        with self.connection() as db:
            cur = db.execute("DELETE FROM operations WHERE user_id=? AND batch_key=?",
                             (user_id, batch_key))
            return cur.rowcount

    def undo(self, user_id: int) -> int:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT batch_key FROM operations WHERE user_id=? ORDER BY id DESC LIMIT 1",
                             (user_id,)).fetchone()
            if not row:
                return 0
            cur = db.execute("DELETE FROM operations WHERE user_id=? AND batch_key=?",
                             (user_id, row["batch_key"]))
            return cur.rowcount

    def report(self, user_id: int, begin: str, end: str) -> dict:
        """UTC ISO interval [begin,end); interest is counted once, principal is not an expense."""
        with self.connection() as db:
            ops = db.execute(
                "SELECT o.id,o.kind,o.amount_minor,c.name AS category_name,s.name AS source_name,s.kind AS source_kind "
                "FROM operations o JOIN sources s ON s.id=o.source_id "
                "LEFT JOIN categories c ON c.id=o.category_id "
                "WHERE o.user_id=? AND o.occurred_at>=? AND o.occurred_at<?",
                (user_id, begin, end),
            ).fetchall()
            ids = [r["id"] for r in ops if r["kind"] == "repayment"]
            interest = {}
            if ids:
                placeholders = ",".join("?" for _ in ids)
                interest = {r["operation_id"]: r["amount_minor"] for r in db.execute(
                    f"SELECT operation_id,amount_minor FROM operation_lines WHERE purpose='interest' "
                    f"AND operation_id IN ({placeholders})", ids,
                )}
            categories: dict[str, int] = {}
            sources: dict[str, int] = {}
            expenses = cash_out = repayments = 0
            for row in ops:
                amount = row["amount_minor"]
                # Credit-card purchases create debt, not immediate cash outflow.
                if row["kind"] == "repayment" or (row["kind"] == "expense" and row["source_kind"] != "credit"):
                    cash_out += amount
                if row["kind"] == "expense":
                    counted = amount
                    label = row["category_name"]
                elif row["kind"] == "repayment":
                    repayments += amount
                    counted = interest.get(row["id"], 0)
                    label = "Проценты и комиссии"
                else:
                    counted = 0
                    label = ""
                expenses += counted
                if counted:
                    categories[label] = categories.get(label, 0) + counted
                    sources[row["source_name"]] = sources.get(row["source_name"], 0) + counted
            return dict(expenses=expenses, cash_out=cash_out, repayments=repayments,
                        categories=sorted(categories.items(), key=lambda x: -x[1]),
                        sources=sorted(sources.items(), key=lambda x: -x[1]), count=len(ops))

    def export_csv(self, user_id: int) -> bytes:
        stream = io.StringIO()
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(("id", "date", "kind", "description", "amount_rub", "source", "target",
                         "category", "principal_rub", "interest_rub", "original_text"))
        with self.connection() as db:
            for r in db.execute(
                "SELECT o.*, s.name source_name,t.name target_name,c.name category_name,"
                "(SELECT amount_minor FROM operation_lines WHERE operation_id=o.id AND purpose='principal') principal,"
                "(SELECT amount_minor FROM operation_lines WHERE operation_id=o.id AND purpose='interest') interest "
                "FROM operations o JOIN sources s ON o.source_id=s.id "
                "LEFT JOIN sources t ON o.target_source_id=t.id "
                "LEFT JOIN categories c ON o.category_id=c.id "
                "WHERE o.user_id=? ORDER BY o.id", (user_id,),
            ):
                fmt = lambda x: "" if x is None else str(x // 100) + "." + f"{x % 100:02d}"
                # Guard formula injection in CSV spreadsheet applications.
                clean = lambda x: "'" + x if isinstance(x, str) and x.lstrip().startswith(("=", "+", "-", "@")) else x
                writer.writerow((r["id"], r["occurred_at"], r["kind"], clean(r["description"]),
                                 fmt(r["amount_minor"]), clean(r["source_name"]), clean(r["target_name"]),
                                 clean(r["category_name"]), fmt(r["principal"]), fmt(r["interest"]),
                                 clean(r["original_text"])))
        return ("\ufeff" + stream.getvalue()).encode("utf-8")
