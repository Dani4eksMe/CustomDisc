"""SQLite data access layer for the Telegram support bot."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

import aiosqlite

_DB_PATH = Path(__file__).resolve().parent / "support_bot.db"


@dataclass
class Ticket:
    id: int
    user_id: int
    user_username: Optional[str]
    user_full_name: str
    group_id: int
    topic_id: int
    status: str
    assigned_support_id: Optional[int]
    created_at: datetime
    last_activity_at: datetime
    closed_at: Optional[datetime]
    rating: Optional[int]


@dataclass
class SupportMember:
    user_id: int
    username: Optional[str]
    full_name: str
    status: str
    tickets_resolved: int
    balance: int
    total_penalties: int
    wallet: Optional[str]
    is_admin: bool
    wage_per_ticket: int


@dataclass
class Ban:
    user_id: int
    reason: Optional[str]
    created_at: datetime


async def init_db(default_ticket_wage: int) -> None:
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS support_members (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                tickets_resolved INTEGER NOT NULL DEFAULT 0,
                balance INTEGER NOT NULL DEFAULT 0,
                total_penalties INTEGER NOT NULL DEFAULT 0,
                wallet TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                wage_per_ticket INTEGER
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_username TEXT,
                user_full_name TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                assigned_support_id INTEGER,
                created_at TIMESTAMP NOT NULL,
                last_activity_at TIMESTAMP NOT NULL,
                closed_at TIMESTAMP,
                rating INTEGER,
                FOREIGN KEY(assigned_support_id) REFERENCES support_members(user_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS bans (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                created_at TIMESTAMP NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS wages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                support_id INTEGER NOT NULL,
                ticket_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                created_at TIMESTAMP NOT NULL,
                FOREIGN KEY(support_id) REFERENCES support_members(user_id),
                FOREIGN KEY(ticket_id) REFERENCES tickets(id)
            )
            """
        )
        await db.commit()

    existing = await get_setting("default_ticket_wage")
    if existing is None:
        await set_setting("default_ticket_wage", str(default_ticket_wage))


@asynccontextmanager
async def _get_db() -> AsyncIterator[aiosqlite.Connection]:
    db = await aiosqlite.connect(_DB_PATH)
    try:
        db.row_factory = aiosqlite.Row
        yield db
    finally:
        await db.close()


async def get_setting(key: str) -> Optional[str]:
    async with _get_db() as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row["value"] if row else None


async def set_setting(key: str, value: str) -> None:
    async with _get_db() as db:
        await db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def add_support_member(member: SupportMember) -> None:
    async with _get_db() as db:
        await db.execute(
            """
            INSERT INTO support_members(user_id, username, full_name, status, tickets_resolved, balance, total_penalties, wallet, is_admin, wage_per_ticket)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                status=excluded.status,
                wallet=excluded.wallet,
                is_admin=excluded.is_admin,
                wage_per_ticket=COALESCE(excluded.wage_per_ticket, support_members.wage_per_ticket)
            """,
            (
                member.user_id,
                member.username,
                member.full_name,
                member.status,
                member.tickets_resolved,
                member.balance,
                member.total_penalties,
                member.wallet,
                int(member.is_admin),
                member.wage_per_ticket,
            ),
        )
        await db.commit()


async def get_support_member(user_id: int) -> Optional[SupportMember]:
    async with _get_db() as db:
        async with db.execute("SELECT * FROM support_members WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return SupportMember(**row) if row else None


async def list_support_members(status: Optional[str] = None) -> list[SupportMember]:
    query = "SELECT * FROM support_members"
    params: tuple[Any, ...] = ()
    if status:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY full_name"
    async with _get_db() as db:
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [SupportMember(**row) for row in rows]


async def update_support_status(user_id: int, status: str) -> None:
    async with _get_db() as db:
        await db.execute("UPDATE support_members SET status = ? WHERE user_id = ?", (status, user_id))
        await db.commit()


async def update_support_wallet(user_id: int, wallet: Optional[str]) -> None:
    async with _get_db() as db:
        await db.execute("UPDATE support_members SET wallet = ? WHERE user_id = ?", (wallet, user_id))
        await db.commit()


async def adjust_support_balance(user_id: int, amount: int) -> None:
    async with _get_db() as db:
        await db.execute("UPDATE support_members SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()


async def add_penalty(user_id: int, amount: int) -> None:
    async with _get_db() as db:
        await db.execute(
            "UPDATE support_members SET total_penalties = total_penalties + ?, balance = balance - ? WHERE user_id = ?",
            (amount, amount, user_id),
        )
        await db.commit()


async def increment_ticket_counter(user_id: int) -> None:
    async with _get_db() as db:
        await db.execute(
            "UPDATE support_members SET tickets_resolved = tickets_resolved + 1 WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()


async def set_support_wage(user_id: int, wage: int) -> None:
    async with _get_db() as db:
        await db.execute("UPDATE support_members SET wage_per_ticket = ? WHERE user_id = ?", (wage, user_id))
        await db.commit()


async def create_ticket(
    user_id: int,
    username: Optional[str],
    full_name: str,
    group_id: int,
    topic_id: int,
) -> int:
    async with _get_db() as db:
        now = datetime.utcnow()
        cursor = await db.execute(
            """
            INSERT INTO tickets(user_id, user_username, user_full_name, group_id, topic_id, status, created_at, last_activity_at)
            VALUES(?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (user_id, username, full_name, group_id, topic_id, now, now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_ticket(ticket_id: int) -> Optional[Ticket]:
    async with _get_db() as db:
        async with db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)) as cursor:
            row = await cursor.fetchone()
            return Ticket(**row) if row else None


async def get_ticket_by_thread(topic_id: int, group_id: int) -> Optional[Ticket]:
    async with _get_db() as db:
        async with db.execute(
            "SELECT * FROM tickets WHERE topic_id = ? AND group_id = ? AND status IN ('open', 'pending')",
            (topic_id, group_id),
        ) as cursor:
            row = await cursor.fetchone()
            return Ticket(**row) if row else None


async def get_open_ticket_for_user(user_id: int) -> Optional[Ticket]:
    async with _get_db() as db:
        async with db.execute(
            "SELECT * FROM tickets WHERE user_id = ? AND status IN ('open', 'pending') ORDER BY id DESC LIMIT 1",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return Ticket(**row) if row else None


async def assign_ticket(ticket_id: int, support_id: int) -> None:
    async with _get_db() as db:
        await db.execute(
            "UPDATE tickets SET assigned_support_id = ?, status = 'pending' WHERE id = ?",
            (support_id, ticket_id),
        )
        await db.commit()


async def update_ticket_activity(ticket_id: int) -> None:
    async with _get_db() as db:
        await db.execute(
            "UPDATE tickets SET last_activity_at = ? WHERE id = ?",
            (datetime.utcnow(), ticket_id),
        )
        await db.commit()


async def close_ticket(ticket_id: int) -> None:
    async with _get_db() as db:
        await db.execute(
            "UPDATE tickets SET status = 'closed', closed_at = ? WHERE id = ?",
            (datetime.utcnow(), ticket_id),
        )
        await db.commit()


async def delete_ticket(ticket_id: int) -> None:
    async with _get_db() as db:
        await db.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
        await db.commit()


async def set_ticket_rating(ticket_id: int, rating: int) -> None:
    async with _get_db() as db:
        await db.execute("UPDATE tickets SET rating = ? WHERE id = ?", (rating, ticket_id))
        await db.commit()


async def add_wage_record(support_id: int, ticket_id: int, amount: int) -> None:
    async with _get_db() as db:
        await db.execute(
            "INSERT INTO wages(support_id, ticket_id, amount, created_at) VALUES(?, ?, ?, ?)",
            (support_id, ticket_id, amount, datetime.utcnow()),
        )
        await db.commit()


async def list_wage_history(support_id: int, limit: int = 10) -> list[Dict[str, Any]]:
    async with _get_db() as db:
        async with db.execute(
            "SELECT * FROM wages WHERE support_id = ? ORDER BY created_at DESC LIMIT ?",
            (support_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def add_ban(user_id: int, reason: Optional[str] = None) -> None:
    async with _get_db() as db:
        await db.execute(
            "INSERT INTO bans(user_id, reason, created_at) VALUES(?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET reason = excluded.reason, created_at = excluded.created_at",
            (user_id, reason, datetime.utcnow()),
        )
        await db.commit()


async def remove_ban(user_id: int) -> None:
    async with _get_db() as db:
        await db.execute("DELETE FROM bans WHERE user_id = ?", (user_id,))
        await db.commit()


async def is_banned(user_id: int) -> bool:
    async with _get_db() as db:
        async with db.execute("SELECT 1 FROM bans WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone() is not None


async def get_total_resolved_tickets() -> int:
    async with _get_db() as db:
        async with db.execute("SELECT COUNT(*) as count FROM tickets WHERE status = 'closed'") as cursor:
            row = await cursor.fetchone()
            return row["count"] if row else 0
