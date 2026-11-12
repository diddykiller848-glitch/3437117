import sqlite3

import pytest

from bot import security
from bot.config import Config
from bot.db import Database
from bot.services import KEY_RE, scrub, slugify


# ------------------------------------------------------------------ security

def test_key_format_matches_pattern():
    key = security.generate_key()
    assert KEY_RE.fullmatch(key)
    assert key.startswith("SLOT-")


def test_keys_are_unique_across_many_generations():
    keys = {security.generate_key() for _ in range(2000)}
    assert len(keys) == 2000


def test_hash_is_keyed_normalized_and_not_plaintext():
    pepper = b"p" * 32
    key = security.generate_key()
    digest = security.hash_key(key, pepper)
    assert digest != key
    assert key not in digest
    # Case and surrounding whitespace must not change the result.
    assert security.hash_key("  " + key.lower() + " ", pepper) == digest
    # A different pepper must produce a different digest.
    assert security.hash_key(key, b"q" * 32) != digest


def test_scrub_removes_recovery_keys_from_text():
    text = "Recovered with SLOT-ABCD-EFGH-2345 today"
    cleaned = scrub(text)
    assert "ABCD-EFGH-2345" not in cleaned
    assert "SLOT-****-****-****" in cleaned


def test_slugify_handles_symbols_and_empty_input():
    assert slugify("Hello World!!") == "hello-world"
    assert slugify("!!!") == "member"


# ------------------------------------------------------------------ config

def test_config_rejects_short_pepper(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GUILD_ID", "1")
    monkeypatch.setenv("OWNER_ID", "2")
    monkeypatch.setenv("KEY_PEPPER", "too-short")
    with pytest.raises(RuntimeError):
        Config.from_env()


def test_config_requires_token(monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setenv("KEY_PEPPER", "x" * 40)
    with pytest.raises(RuntimeError):
        Config.from_env()


# ------------------------------------------------------------------ database

@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


async def add_plan(db: Database) -> int:
    await db.write("INSERT INTO plans (name, duration_days, max_slots) VALUES ('Gold', 30, 5)")
    row = await db.one("SELECT id FROM plans WHERE name = 'Gold'")
    return row["id"]


async def test_default_settings_are_seeded(db):
    assert await db.setting("expiry_action") == "archive"
    assert await db.setting("requirement_enabled") == "0"


async def test_one_open_slot_per_user_is_enforced(db):
    plan_id = await add_plan(db)
    await db.write(
        "INSERT INTO slots (owner_id, plan_id, status, created_at) VALUES (1, ?, 'active', 0)", (plan_id,)
    )
    with pytest.raises(sqlite3.IntegrityError):
        await db.write(
            "INSERT INTO slots (owner_id, plan_id, status, created_at) VALUES (1, ?, 'pending', 0)", (plan_id,)
        )


async def test_expired_slot_does_not_block_a_new_one(db):
    plan_id = await add_plan(db)
    await db.write(
        "INSERT INTO slots (owner_id, plan_id, status, created_at) VALUES (2, ?, 'expired', 0)", (plan_id,)
    )
    await db.write(
        "INSERT INTO slots (owner_id, plan_id, status, created_at) VALUES (2, ?, 'active', 0)", (plan_id,)
    )
    row = await db.one("SELECT COUNT(*) AS c FROM slots WHERE owner_id = 2")
    assert row["c"] == 2


async def test_transaction_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        async with db.tx() as tx:
            await tx.exec("INSERT INTO blacklist (user_id, reason, created_at) VALUES (5, 'x', 0)")
            raise RuntimeError("boom")
    assert await db.one("SELECT 1 FROM blacklist WHERE user_id = 5") is None


async def test_transaction_commits_on_success(db):
    async with db.tx() as tx:
        await tx.exec("INSERT INTO blacklist (user_id, reason, created_at) VALUES (6, 'y', 0)")
    assert await db.one("SELECT 1 FROM blacklist WHERE user_id = 6") is not None


async def test_data_survives_reconnect(tmp_path):
    path = str(tmp_path / "persist.db")
    first = Database(path)
    await first.connect()
    await first.set_setting("gl_text", "hello")
    await first.close()

    second = Database(path)
    await second.connect()
    assert await second.setting("gl_text") == "hello"
    await second.close()
