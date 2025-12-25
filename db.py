from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from .models import FinishedTask, LiveTask, Preferences, StateSnapshot, TodosCache, TaskStatus


def _default_db_path() -> str:
    """
    Choose a sensible per-user default location.

    - Linux:   ~/.local/share/statusbar_textual/statusbar2.db (or $XDG_DATA_HOME)
    - macOS:   ~/Library/Application Support/statusbar_textual/statusbar2.db
    - Windows: %APPDATA%\\statusbar_textual\\statusbar2.db (fallback to ~\\AppData\\Roaming)
    """
    home = Path.home()

    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        base = Path(appdata) / "statusbar_textual"
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "statusbar_textual"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            base = Path(xdg) / "statusbar_textual"
        else:
            base = home / ".local" / "share" / "statusbar_textual"

    base.mkdir(parents=True, exist_ok=True)
    return str(base / "statusbar2.db")


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


class StatusbarDB:
    """
    SQLite-backed storage for cache and preferences.

    Tables:
      - users(email, password_hash) [legacy]
      - preferences(email, vocal_enabled, vocal_frequency) [legacy]
      - cache(one row): server_api_url, api_key, email, vocal_enabled, vocal_frequency
      - live_tasks(email, id, value, deadline, managed, pos)
      - finished_tasks(email, id, value, deadline, managed, pos, status)
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or _default_db_path()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def _init_schema(self) -> None:
        cur = self.conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              email TEXT PRIMARY KEY,
              password_hash TEXT NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS preferences (
              email TEXT PRIMARY KEY REFERENCES users(email) ON DELETE CASCADE,
              vocal_enabled INTEGER NOT NULL DEFAULT 0,
              vocal_frequency INTEGER NOT NULL DEFAULT 300
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cache (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              server_api_url TEXT NOT NULL,
              api_key TEXT NOT NULL,
              email TEXT NOT NULL,
              vocal_enabled INTEGER,
              vocal_frequency INTEGER
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS live_tasks (
              email TEXT NOT NULL REFERENCES users(email) ON DELETE CASCADE,
              id TEXT NOT NULL,
              value TEXT NOT NULL,
              deadline INTEGER NULL,
              managed TEXT NULL,
              pos INTEGER NOT NULL,
              PRIMARY KEY(email, id)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS finished_tasks (
              email TEXT NOT NULL REFERENCES users(email) ON DELETE CASCADE,
              id TEXT NOT NULL,
              value TEXT NOT NULL,
              deadline INTEGER NULL,
              managed TEXT NULL,
              pos INTEGER NOT NULL,
              status TEXT NOT NULL,
              PRIMARY KEY(email, id)
            )
            """
        )

        self.conn.commit()
        self._ensure_cache_columns()

    def _ensure_cache_columns(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(cache)")
        cols = {row["name"] for row in cur.fetchall()}
        if "vocal_enabled" not in cols:
            cur.execute("ALTER TABLE cache ADD COLUMN vocal_enabled INTEGER")
        if "vocal_frequency" not in cols:
            cur.execute("ALTER TABLE cache ADD COLUMN vocal_frequency INTEGER")
        self.conn.commit()

    # -------------------------
    # Users / Auth
    # -------------------------
    def ensure_user(self, email: str, password: str) -> None:
        """Create user if missing; if exists, validate password."""
        cur = self.conn.cursor()
        cur.execute("SELECT password_hash FROM users WHERE email = ?", (email,))
        row = cur.fetchone()
        pw_hash = _hash_password(password)

        if row is None:
            cur.execute("INSERT INTO users(email, password_hash) VALUES(?, ?)", (email, pw_hash))
            cur.execute(
                "INSERT INTO preferences(email, vocal_enabled, vocal_frequency) VALUES(?, 0, 300)",
                (email,),
            )
            self.conn.commit()
            return

        if row["password_hash"] != pw_hash:
            raise ValueError("Invalid email or password.")

        # Ensure preferences row exists
        cur.execute("SELECT 1 FROM preferences WHERE email = ?", (email,))
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO preferences(email, vocal_enabled, vocal_frequency) VALUES(?, 0, 300)",
                (email,),
            )
            self.conn.commit()

    # -------------------------
    # Preferences
    # -------------------------
    def get_preferences(self, email: str) -> Preferences:
        cur = self.conn.cursor()
        cur.execute("SELECT vocal_enabled, vocal_frequency FROM preferences WHERE email = ?", (email,))
        row = cur.fetchone()
        if row is None:
            prefs = Preferences(vocal_enabled=False, vocal_frequency=300)
            self.set_preferences(email, prefs)
            return prefs
        return Preferences(vocal_enabled=bool(row["vocal_enabled"]), vocal_frequency=int(row["vocal_frequency"]))

    def set_preferences(self, email: str, prefs: Preferences) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO preferences(email, vocal_enabled, vocal_frequency)
            VALUES(?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
              vocal_enabled = excluded.vocal_enabled,
              vocal_frequency = excluded.vocal_frequency
            """,
            (email, int(bool(prefs.vocal_enabled)), int(prefs.vocal_frequency)),
        )
        self.conn.commit()

    # -------------------------
    # Cache
    # -------------------------
    def save_cache(self, cache: TodosCache) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO cache(id, server_api_url, api_key, email, vocal_enabled, vocal_frequency)
            VALUES(1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              server_api_url = excluded.server_api_url,
              api_key = excluded.api_key,
              email = excluded.email,
              vocal_enabled = excluded.vocal_enabled,
              vocal_frequency = excluded.vocal_frequency
            """,
            (
                cache.server_api_url,
                cache.api_key,
                "",
                int(bool(cache.preferences.vocal_enabled)),
                int(cache.preferences.vocal_frequency),
            ),
        )
        self.conn.commit()

    def ensure_user_stub(self, email: str) -> None:
        """Ensure a user row exists for preference foreign keys."""
        cur = self.conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO users(email, password_hash) VALUES(?, ?)",
            (email, ""),
        )
        self.conn.commit()

    def load_cache(self) -> Optional[TodosCache]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT server_api_url,
                   api_key,
                   email,
                   vocal_enabled,
                   vocal_frequency
            FROM cache
            WHERE id = 1
            """
        )
        row = cur.fetchone()
        if row is None:
            return None

        vocal_enabled = row["vocal_enabled"]
        vocal_frequency = row["vocal_frequency"]

        prefs: Preferences
        if vocal_enabled is None or vocal_frequency is None:
            legacy = None
            email = row["email"]
            if email:
                legacy = self._load_legacy_preferences(email)
            if legacy is not None:
                prefs = legacy
                cur.execute(
                    "UPDATE cache SET vocal_enabled = ?, vocal_frequency = ? WHERE id = 1",
                    (int(bool(prefs.vocal_enabled)), int(prefs.vocal_frequency)),
                )
                self.conn.commit()
            else:
                prefs = Preferences(vocal_enabled=False, vocal_frequency=300)
        else:
            prefs = Preferences(
                vocal_enabled=bool(vocal_enabled),
                vocal_frequency=int(vocal_frequency),
            )

        return TodosCache(
            server_api_url=str(row["server_api_url"]),
            api_key=str(row["api_key"]),
            preferences=prefs,
        )

    def _load_legacy_preferences(self, email: str) -> Optional[Preferences]:
        cur = self.conn.cursor()
        cur.execute("SELECT vocal_enabled, vocal_frequency FROM preferences WHERE email = ?", (email,))
        row = cur.fetchone()
        if row is None:
            return None
        return Preferences(vocal_enabled=bool(row["vocal_enabled"]), vocal_frequency=int(row["vocal_frequency"]))

    def clear_cache(self) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM cache WHERE id = 1")
        self.conn.commit()

    # -------------------------
    # Snapshot & Tasks
    # -------------------------
    def get_snapshot(self, email: str) -> StateSnapshot:
        cur = self.conn.cursor()

        cur.execute(
            "SELECT id, value, deadline, managed FROM live_tasks WHERE email = ? ORDER BY pos ASC",
            (email,),
        )
        live = [
            LiveTask(
                id=row["id"],
                value=row["value"],
                deadline=row["deadline"],
                managed=row["managed"],
            )
            for row in cur.fetchall()
        ]

        cur.execute(
            "SELECT id, value, deadline, managed, status FROM finished_tasks WHERE email = ? ORDER BY pos ASC",
            (email,),
        )
        finished = [
            FinishedTask(
                id=row["id"],
                value=row["value"],
                deadline=row["deadline"],
                managed=row["managed"],
                status=row["status"],
            )
            for row in cur.fetchall()
        ]

        return StateSnapshot(live=live, finished=finished)

    def _next_pos_front(self, table: str, email: str) -> int:
        cur = self.conn.cursor()
        cur.execute(f"SELECT MIN(pos) AS min_pos FROM {table} WHERE email = ?", (email,))
        row = cur.fetchone()
        min_pos = row["min_pos"] if row and row["min_pos"] is not None else 0
        return int(min_pos) - 1

    def insert_live_task(self, email: str, task_id: str, value: str, deadline: Optional[int]) -> None:
        pos = self._next_pos_front("live_tasks", email)
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO live_tasks(email, id, value, deadline, managed, pos)
            VALUES(?, ?, ?, ?, NULL, ?)
            """,
            (email, task_id, value, deadline, pos),
        )
        self.conn.commit()

    def edit_task(self, email: str, task_id: str, value: str, deadline: Optional[int]) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE live_tasks SET value = ?, deadline = ? WHERE email = ? AND id = ?",
            (value, deadline, email, task_id),
        )
        self.conn.commit()

    def finish_live_task(self, email: str, task_id: str, status: TaskStatus) -> None:
        cur = self.conn.cursor()

        cur.execute(
            "SELECT id, value, deadline, managed, pos FROM live_tasks WHERE email = ? AND id = ?",
            (email, task_id),
        )
        row = cur.fetchone()
        if row is None:
            return

        cur.execute("DELETE FROM live_tasks WHERE email = ? AND id = ?", (email, task_id))

        pos = self._next_pos_front("finished_tasks", email)
        cur.execute(
            """
            INSERT INTO finished_tasks(email, id, value, deadline, managed, pos, status)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (email, row["id"], row["value"], row["deadline"], row["managed"], pos, status),
        )
        self.conn.commit()

        self._normalize_positions("live_tasks", email)
        self._normalize_positions("finished_tasks", email)

    def restore_finished_task(self, email: str, task_id: str) -> None:
        cur = self.conn.cursor()

        cur.execute(
            "SELECT id, value, deadline, managed, pos FROM finished_tasks WHERE email = ? AND id = ?",
            (email, task_id),
        )
        row = cur.fetchone()
        if row is None:
            return

        cur.execute("DELETE FROM finished_tasks WHERE email = ? AND id = ?", (email, task_id))

        pos = self._next_pos_front("live_tasks", email)
        cur.execute(
            """
            INSERT INTO live_tasks(email, id, value, deadline, managed, pos)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (email, row["id"], row["value"], row["deadline"], row["managed"], pos),
        )
        self.conn.commit()

        self._normalize_positions("live_tasks", email)
        self._normalize_positions("finished_tasks", email)

    def move_live_task(self, email: str, id_del: str, id_ins: str) -> None:
        cur = self.conn.cursor()
        cur.execute("SELECT id, pos FROM live_tasks WHERE email = ? ORDER BY pos ASC", (email,))
        rows = cur.fetchall()
        ids = [row["id"] for row in rows]
        if id_del not in ids or id_ins not in ids:
            return

        from_index = ids.index(id_del)
        to_index = ids.index(id_ins)
        if from_index == to_index:
            return

        task_id = ids.pop(from_index)
        ids.insert(to_index, task_id)

        for idx, tid in enumerate(ids):
            cur.execute("UPDATE live_tasks SET pos = ? WHERE email = ? AND id = ?", (idx, email, tid))
        self.conn.commit()

    def reverse_live_task(self, email: str, id1: str, id2: str) -> None:
        cur = self.conn.cursor()
        cur.execute("SELECT id, pos FROM live_tasks WHERE email = ? ORDER BY pos ASC", (email,))
        rows = cur.fetchall()
        ids = [row["id"] for row in rows]
        if id1 not in ids or id2 not in ids:
            return

        i1 = ids.index(id1)
        i2 = ids.index(id2)
        start = min(i1, i2)
        end = max(i1, i2)

        ids[start : end + 1] = list(reversed(ids[start : end + 1]))

        for idx, tid in enumerate(ids):
            cur.execute("UPDATE live_tasks SET pos = ? WHERE email = ? AND id = ?", (idx, email, tid))
        self.conn.commit()

    def _normalize_positions(self, table: str, email: str) -> None:
        cur = self.conn.cursor()
        cur.execute(f"SELECT id FROM {table} WHERE email = ? ORDER BY pos ASC", (email,))
        ids = [row["id"] for row in cur.fetchall()]
        for idx, tid in enumerate(ids):
            cur.execute(f"UPDATE {table} SET pos = ? WHERE email = ? AND id = ?", (idx, email, tid))
        self.conn.commit()
