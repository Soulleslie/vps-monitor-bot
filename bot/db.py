"""SQLite 存储。所有时间均为 Unix 时间戳（秒）。"""
from __future__ import annotations

import os
import time
from typing import Any, Iterable

import aiosqlite

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS chats (
    chat_id        INTEGER PRIMARY KEY,
    title          TEXT,
    chat_type      TEXT,                    -- private / group / supergroup / channel
    paused         INTEGER NOT NULL DEFAULT 0,
    notify_soldout INTEGER NOT NULL DEFAULT 0,
    notify_new     INTEGER NOT NULL DEFAULT 1,
    created_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    url            TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    interval_sec   INTEGER,                 -- NULL 表示使用全局默认
    probe_mode     TEXT NOT NULL DEFAULT 'auto',  -- auto / off
    enabled        INTEGER NOT NULL DEFAULT 1,
    last_check     INTEGER,
    last_ok        INTEGER,
    fail_count     INTEGER NOT NULL DEFAULT 0,
    fail_alerted   INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    parse_method   TEXT,
    created_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id          INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    key              TEXT NOT NULL,
    name             TEXT NOT NULL,
    status           TEXT NOT NULL,
    qty              INTEGER,
    price            TEXT,
    order_url        TEXT,
    description      TEXT,
    present          INTEGER NOT NULL DEFAULT 1,   -- 最近一次是否出现在页面上
    first_seen       INTEGER NOT NULL,
    last_seen        INTEGER NOT NULL,
    in_stock_since   INTEGER,
    last_notified_at INTEGER,
    UNIQUE (site_id, key)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    site_id    INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('product', 'keyword', 'all')),
    value      TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    UNIQUE (chat_id, site_id, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_sub_site ON subscriptions(site_id);

-- 某个站点推送到哪些会话
CREATE TABLE IF NOT EXISTS chat_sites (
    chat_id    INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    site_id    INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    alias      TEXT,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, site_id)
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,          -- restock / soldout / new
    qty        INTEGER,
    duration   INTEGER,                -- soldout 事件：本次有货持续秒数
    ts         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_product ON events(product_id, ts);

-- 已发出的补货/上新消息，售罄后回头编辑成“已售罄”
CREATE TABLE IF NOT EXISTS notices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL,
    site_id    INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    closed     INTEGER NOT NULL DEFAULT 0      -- 全部售罄或消息已无法编辑后置 1
);
CREATE INDEX IF NOT EXISTS idx_notices_open ON notices(closed, site_id);

CREATE TABLE IF NOT EXISTS notice_items (
    notice_id  INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,                  -- restock / new
    name       TEXT NOT NULL,
    price      TEXT,
    qty        INTEGER,
    order_url  TEXT,
    ts         INTEGER NOT NULL,               -- 补货时间
    soldout_ts INTEGER,                        -- 售罄时间（NULL 表示仍有货）
    PRIMARY KEY (notice_id, product_id)
);

-- Web 后台保存的设置（覆盖环境变量）
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- Web 登录会话：只保存令牌的 SHA-256，不保存明文
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    ip         TEXT
);
"""


def now() -> int:
    return int(time.time())


class DB:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ---------- 基础 ----------
    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        async with self.conn.execute(sql, tuple(params)) as cur:
            await self.conn.commit()
            return cur.lastrowid

    # ---------- 会话 / 权限 ----------
    async def ensure_chat(self, chat_id: int, title: str | None, chat_type: str | None = None) -> aiosqlite.Row:
        await self.conn.execute(
            "INSERT INTO chats (chat_id, title, chat_type, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title = COALESCE(excluded.title, title), "
            "chat_type = COALESCE(excluded.chat_type, chat_type)",
            (chat_id, title, chat_type, now()),
        )
        await self.conn.commit()
        return await self.fetchone("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))

    async def get_chat(self, chat_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))

    async def set_chat_flag(self, chat_id: int, column: str, value: int) -> None:
        if column not in ("paused", "notify_soldout", "notify_new"):
            raise ValueError(column)
        await self.execute(f"UPDATE chats SET {column} = ? WHERE chat_id = ?", (value, chat_id))

    # ---------- 站点 ----------
    async def get_or_create_site(self, url: str, name: str) -> aiosqlite.Row:
        row = await self.fetchone("SELECT * FROM sites WHERE url = ?", (url,))
        if row:
            return row
        await self.execute("INSERT INTO sites (url, name, created_at) VALUES (?, ?, ?)", (url, name, now()))
        return await self.fetchone("SELECT * FROM sites WHERE url = ?", (url,))

    async def get_site(self, site_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM sites WHERE id = ?", (site_id,))

    async def attach_site(self, chat_id: int, site_id: int) -> None:
        await self.execute(
            "INSERT OR IGNORE INTO chat_sites (chat_id, site_id, created_at) VALUES (?, ?, ?)",
            (chat_id, site_id, now()),
        )

    async def detach_site(self, chat_id: int, site_id: int) -> None:
        await self.conn.execute("DELETE FROM subscriptions WHERE chat_id = ? AND site_id = ?", (chat_id, site_id))
        await self.conn.execute("DELETE FROM chat_sites WHERE chat_id = ? AND site_id = ?", (chat_id, site_id))
        await self.conn.commit()

    async def delete_site(self, site_id: int) -> None:
        """删除站点及其机型、订阅、历史（外键级联）。"""
        await self.execute("DELETE FROM sites WHERE id = ?", (site_id,))

    async def delete_chat(self, chat_id: int) -> None:
        await self.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))

    async def set_chat_subscriptions(
        self, chat_id: int, site_id: int, watch_all: bool, product_keys: list[str], keywords: list[str]
    ) -> None:
        """整体替换某会话在某站点上的订阅。"""
        ts = now()
        await self.conn.execute(
            "INSERT OR IGNORE INTO chat_sites (chat_id, site_id, created_at) VALUES (?, ?, ?)", (chat_id, site_id, ts)
        )
        await self.conn.execute("DELETE FROM subscriptions WHERE chat_id = ? AND site_id = ?", (chat_id, site_id))
        rows = [("all", "")] if watch_all else []
        rows += [("product", k) for k in dict.fromkeys(product_keys)]
        rows += [("keyword", k) for k in dict.fromkeys(kw.strip().lower() for kw in keywords) if k]
        for kind, value in rows:
            await self.conn.execute(
                "INSERT OR IGNORE INTO subscriptions (chat_id, site_id, kind, value, created_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, site_id, kind, value, ts),
            )
        await self.conn.commit()

    async def chat_owns_site(self, chat_id: int, site_id: int) -> bool:
        return (
            await self.fetchone("SELECT 1 FROM chat_sites WHERE chat_id = ? AND site_id = ?", (chat_id, site_id))
            is not None
        )

    async def chat_sites(self, chat_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT s.*, cs.alias FROM chat_sites cs JOIN sites s ON s.id = cs.site_id "
            "WHERE cs.chat_id = ? ORDER BY s.id",
            (chat_id,),
        )

    async def due_sites(self, default_interval: int, ts: int) -> list[aiosqlite.Row]:
        """到期需要检查、且至少有一个订阅的站点。失败时按指数退避（最长 30 分钟）。"""
        rows = await self.fetchall(
            "SELECT s.* FROM sites s WHERE s.enabled = 1 "
            "AND EXISTS (SELECT 1 FROM subscriptions sub WHERE sub.site_id = s.id)"
        )
        due = []
        for r in rows:
            interval = r["interval_sec"] or default_interval
            if r["fail_count"]:
                interval = min(interval * (2 ** min(r["fail_count"], 6)), 1800)
            if r["last_check"] is None or ts - r["last_check"] >= interval:
                due.append(r)
        return due

    async def update_site_check(
        self, site_id: int, ok: bool, error: str | None = None, method: str | None = None, ts: int | None = None
    ) -> aiosqlite.Row:
        ts = ts or now()
        if ok:
            await self.execute(
                "UPDATE sites SET last_check = ?, last_ok = ?, fail_count = 0, last_error = NULL, "
                "parse_method = COALESCE(?, parse_method) WHERE id = ?",
                (ts, ts, method, site_id),
            )
        else:
            await self.execute(
                "UPDATE sites SET last_check = ?, fail_count = fail_count + 1, last_error = ? WHERE id = ?",
                (ts, (error or "")[:300], site_id),
            )
        return await self.get_site(site_id)

    # ---------- 机型 ----------
    async def site_products(self, site_id: int, present_only: bool = False) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM products WHERE site_id = ?"
        if present_only:
            sql += " AND present = 1"
        return await self.fetchall(sql + " ORDER BY id", (site_id,))

    async def get_product(self, product_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM products WHERE id = ?", (product_id,))

    # ---------- 订阅 ----------
    async def subscribe(self, chat_id: int, site_id: int, kind: str, value: str = "") -> None:
        await self.execute(
            "INSERT OR IGNORE INTO subscriptions (chat_id, site_id, kind, value, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, site_id, kind, value, now()),
        )

    async def unsubscribe(self, chat_id: int, site_id: int, kind: str, value: str = "") -> None:
        await self.execute(
            "DELETE FROM subscriptions WHERE chat_id = ? AND site_id = ? AND kind = ? AND value = ?",
            (chat_id, site_id, kind, value),
        )

    async def chat_subscriptions(self, chat_id: int, site_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM subscriptions WHERE chat_id = ? AND site_id = ? ORDER BY id", (chat_id, site_id)
        )

    async def site_subscriptions(self, site_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT sub.* FROM subscriptions sub JOIN chats c ON c.chat_id = sub.chat_id "
            "WHERE sub.site_id = ?",
            (site_id,),
        )

    async def site_watchers(self, site_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT c.* FROM chat_sites cs JOIN chats c ON c.chat_id = cs.chat_id "
            "WHERE cs.site_id = ? AND c.paused = 0",
            (site_id,),
        )

    # ---------- 可编辑的推送消息 ----------
    async def add_notice(self, chat_id: int, message_id: int, site_id: int, items: list[dict], ts: int) -> int:
        cur = await self.conn.execute(
            "INSERT INTO notices (chat_id, message_id, site_id, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, message_id, site_id, ts),
        )
        notice_id = cur.lastrowid
        for it in items:
            await self.conn.execute(
                "INSERT OR IGNORE INTO notice_items (notice_id, product_id, kind, name, price, qty, order_url, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (notice_id, it["product_id"], it["kind"], it["name"], it["price"], it["qty"], it["order_url"], it["ts"]),
            )
        await self.conn.commit()
        return notice_id

    async def mark_items_soldout(self, product_ids: list[int], ts: int) -> list[int]:
        """把仍未关闭的消息里这些机型标记为售罄，返回受影响的消息 id。"""
        if not product_ids:
            return []
        marks = ",".join("?" * len(product_ids))
        rows = await self.fetchall(
            f"SELECT DISTINCT ni.notice_id FROM notice_items ni JOIN notices n ON n.id = ni.notice_id "
            f"WHERE n.closed = 0 AND ni.soldout_ts IS NULL AND ni.product_id IN ({marks})",
            product_ids,
        )
        ids = [r["notice_id"] for r in rows]
        if ids:
            await self.conn.execute(
                f"UPDATE notice_items SET soldout_ts = ? WHERE soldout_ts IS NULL AND product_id IN ({marks}) "
                f"AND notice_id IN ({','.join('?' * len(ids))})",
                (ts, *product_ids, *ids),
            )
            await self.conn.commit()
        return ids

    async def get_notice(self, notice_id: int) -> tuple[aiosqlite.Row | None, list[aiosqlite.Row]]:
        notice = await self.fetchone("SELECT * FROM notices WHERE id = ?", (notice_id,))
        items = await self.fetchall("SELECT * FROM notice_items WHERE notice_id = ? ORDER BY rowid", (notice_id,))
        return notice, items

    async def close_notice(self, notice_id: int) -> None:
        await self.execute("UPDATE notices SET closed = 1 WHERE id = ?", (notice_id,))

    # ---------- 设置 ----------
    async def get_settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in await self.fetchall("SELECT key, value FROM settings")}

    async def save_settings(self, values: dict[str, str]) -> None:
        ts = now()
        for key, value in values.items():
            await self.conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, ts),
            )
        await self.conn.commit()

    # ---------- Web 会话 ----------
    async def create_session(self, token_hash: str, ttl: int, ip: str | None) -> None:
        ts = now()
        await self.conn.execute("DELETE FROM web_sessions WHERE expires_at < ?", (ts,))
        await self.conn.execute(
            "INSERT INTO web_sessions (token_hash, created_at, expires_at, ip) VALUES (?, ?, ?, ?)",
            (token_hash, ts, ts + ttl, ip),
        )
        await self.conn.commit()

    async def session_valid(self, token_hash: str) -> bool:
        row = await self.fetchone(
            "SELECT 1 FROM web_sessions WHERE token_hash = ? AND expires_at >= ?", (token_hash, now())
        )
        return row is not None

    async def delete_session(self, token_hash: str) -> None:
        await self.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))

    async def delete_all_sessions(self) -> None:
        await self.execute("DELETE FROM web_sessions")

    # ---------- 历史 ----------
    async def add_event(self, product_id: int, kind: str, qty: int | None, duration: int | None, ts: int) -> None:
        await self.conn.execute(
            "INSERT INTO events (product_id, kind, qty, duration, ts) VALUES (?, ?, ?, ?, ?)",
            (product_id, kind, qty, duration, ts),
        )

    async def site_events(self, site_id: int, limit: int = 15) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT e.*, p.name FROM events e JOIN products p ON p.id = e.product_id "
            "WHERE p.site_id = ? ORDER BY e.ts DESC, e.id DESC LIMIT ?",
            (site_id, limit),
        )
