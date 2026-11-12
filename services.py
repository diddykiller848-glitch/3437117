"""Business logic. The only module that changes slot records or Discord channels."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import discord

from . import security
from .config import COLOR_ERROR, COLOR_INFO, COLOR_PRIMARY, COLOR_SUCCESS, COLOR_WARNING
from .db import Database

logger = logging.getLogger(__name__)

DAY = 86400
OPEN = "('pending','active','banned')"
NOTICES = (
    ("notice_7d", 7 * DAY, "7 days"),
    ("notice_3d", 3 * DAY, "3 days"),
    ("notice_24h", DAY, "24 hours"),
)
ALLOWED_OWNER_EXTRAS = frozenset({"attach_files", "embed_links", "add_reactions", "use_external_emojis"})
KEY_RE = re.compile(r"SLOT-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}")
SLOT_SQL = (
    "SELECT s.*, p.name AS plan_name, p.price_label, p.duration_days, p.category_id AS plan_category, "
    "p.permissions, p.here_per_day, p.everyone_per_week, p.max_slots, p.is_free, p.active AS plan_active "
    "FROM slots s JOIN plans p ON p.id = s.plan_id"
)
RECOVERY_BLOCKED = {
    "pending": "That slot is still waiting for approval.",
    "expired": "That slot has expired. Use `/renew-slot` to request a renewal.",
    "banned": "That slot is suspended by administrators.",
    "removed": "That slot was removed and can't be recovered.",
    "deleted": "That slot's channel was deleted and can't be recovered.",
}


class ServiceError(Exception):
    """An expected error. The message is safe to show to the user."""


@dataclass
class ClaimResult:
    status: str  # "active" | "pending" | "needs_verification"
    slot_id: int | None = None
    channel: discord.TextChannel | None = None
    dm_ok: bool = False
    message: str = ""


def now() -> int:
    return int(time.time())


def scrub(text: str) -> str:
    """Strip anything that looks like a recovery key before it reaches a log or embed."""
    return KEY_RE.sub("SLOT-****-****-****", text)


def ts(value: int | None) -> str:
    return f"<t:{value}:D>" if value else "—"


def rel(value: int | None) -> str:
    return f"<t:{value}:R>" if value else "—"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:20] or "member"


def is_staff(bot, member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.id == bot.cfg.owner_id or member.guild_permissions.administrator:
        return True
    return any(role.id in bot.cfg.admin_role_ids for role in member.roles)


def has_phrase(member: discord.Member, phrase: str) -> bool:
    """Checks the custom status only. Discord does not expose About Me through the bot API."""
    needle = phrase.casefold()
    return any(
        isinstance(activity, discord.CustomActivity) and needle in (activity.name or "").casefold()
        for activity in member.activities
    )


def notice_embed(title: str, description: str, color: int = COLOR_INFO) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def key_embed(key: str) -> discord.Embed:
    return discord.Embed(
        title="🔑 Your slot recovery key",
        color=COLOR_WARNING,
        description=(
            f"```{key}```\n**Save this key now. It is shown only once.**\n"
            "Use `/recover-slot` with it if you ever lose access to your slot. "
            "You can rotate it with `/regenerate-key`."
        ),
    )


async def send_dm(user, embed: discord.Embed) -> bool:
    try:
        await user.send(embed=embed)
        return True
    except discord.HTTPException:
        return False


async def _history(tx, slot_id, user_id, actor_id, action: str, details: str = "") -> None:
    await tx.exec(
        "INSERT INTO history (slot_id, user_id, actor_id, action, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (slot_id, user_id, actor_id, action, scrub(details), now()),
    )


class SlotService:
    def __init__(self, bot):
        self.bot = bot
        self.db: Database = bot.db
        self.cfg = bot.cfg
        self._activation_lock = asyncio.Lock()
        self._ping_lock = asyncio.Lock()

    # ------------------------------------------------------------------ helpers

    @property
    def guild(self) -> discord.Guild:
        guild = self.bot.get_guild(self.cfg.guild_id)
        if guild is None:
            raise ServiceError("The bot is not connected to the configured server yet.")
        return guild

    def _hash(self, key: str) -> str:
        return security.hash_key(key, self.cfg.key_pepper)

    def _channel_of(self, slot) -> discord.TextChannel | None:
        if not slot["channel_id"]:
            return None
        channel = self.guild.get_channel(slot["channel_id"])
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _channel_gone(self, channel_id: int) -> bool:
        try:
            await self.bot.fetch_channel(channel_id)
            return False
        except discord.NotFound:
            return True
        except discord.HTTPException:
            return False

    async def dm_user(self, user_id: int, embed: discord.Embed) -> bool:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            return False
        return await send_dm(user, embed)

    async def log(self, title: str, description: str, color: int = COLOR_INFO) -> None:
        try:
            channel_id = await self.db.setting("log_channel_id")
            if not channel_id:
                return
            channel = self.guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                return
            embed = discord.Embed(
                title=title,
                description=scrub(description)[:4000],
                color=color,
                timestamp=datetime.now(timezone.utc),
            )
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (ServiceError, discord.HTTPException, ValueError):
            logger.warning("Could not write to the log channel", exc_info=True)

    async def slot_by_id(self, slot_id: int):
        return await self.db.one(SLOT_SQL + " WHERE s.id = ?", (slot_id,))

    async def slot_by_key(self, raw_key: str):
        return await self.db.one(SLOT_SQL + " WHERE s.key_hash = ?", (self._hash(raw_key),))

    async def slot_for_owner(self, user_id: int, statuses: str = OPEN):
        return await self.db.one(
            SLOT_SQL + f" WHERE s.owner_id = ? AND s.status IN {statuses} ORDER BY s.id DESC LIMIT 1",
            (user_id,),
        )

    async def plan_by_key(self, key, active_only: bool = True):
        suffix = " AND active = 1" if active_only else ""
        text = str(key).strip()
        if text.isdigit():
            return await self.db.one(f"SELECT * FROM plans WHERE id = ?{suffix}", (int(text),))
        return await self.db.one(f"SELECT * FROM plans WHERE name = ? COLLATE NOCASE{suffix}", (text,))

    async def _assert_can_hold(self, conn, user_id: int) -> None:
        if await conn.one("SELECT 1 FROM blacklist WHERE user_id = ?", (user_id,)):
            raise ServiceError("Your account is blacklisted from the slot program. Contact an administrator.")
        if await conn.one(f"SELECT id FROM slots WHERE owner_id = ? AND status IN {OPEN}", (user_id,)):
            raise ServiceError("You already have an open slot or request. Check it with `/myslot`.")

    async def _precheck(self, conn, user_id: int, plan) -> None:
        await self._assert_can_hold(conn, user_id)
        if not plan["active"]:
            raise ServiceError("That plan is not open right now.")
        used = await conn.one(
            f"SELECT COUNT(*) AS c FROM slots WHERE plan_id = ? AND status IN {OPEN}", (plan["id"],)
        )
        if used["c"] >= plan["max_slots"]:
            raise ServiceError(f"The {plan['name']} plan is full right now.")

    # --------------------------------------------------------------- channels

    def _owner_overwrite(self, perms_json: str | None) -> discord.PermissionOverwrite:
        extras = {
            key: bool(value)
            for key, value in json.loads(perms_json or "{}").items()
            if key in ALLOWED_OWNER_EXTRAS
        }
        return discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
            **extras,
        )

    def _overwrites(self, owner: discord.Member, perms_json: str | None) -> dict:
        guild = self.guild
        staff = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True, manage_messages=True
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                add_reactions=False,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=False,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                embed_links=True,
                read_message_history=True,
                mention_everyone=True,
            ),
            owner: self._owner_overwrite(perms_json),
        }
        for role_id in self.cfg.admin_role_ids:
            role = guild.get_role(role_id)
            if role is not None:
                overwrites[role] = staff
        owner_user = guild.get_member(self.cfg.owner_id)
        if owner_user is not None and owner_user.id != owner.id:
            overwrites[owner_user] = staff
        return overwrites

    async def set_visibility(self, slot, visible: bool) -> None:
        channel = self._channel_of(slot)
        if channel is not None:
            await channel.set_permissions(self.guild.default_role, view_channel=visible, reason="Slot visibility")

    async def _retire(self, slot, action: str) -> None:
        """Apply the configured expiry action. Deleting requires a matching DB record."""
        channel = self._channel_of(slot)
        if channel is None:
            return
        if action == "delete":
            record = await self.db.one("SELECT id FROM slots WHERE id = ? AND channel_id = ?", (slot["id"], channel.id))
            if record is None:
                raise ServiceError("Refusing to delete a channel that has no matching slot record.")
            await channel.delete(reason=f"Slot #{slot['id']} retired")
            return
        if action == "archive":
            archive_id = int(await self.db.setting("archive_category_id", "0") or 0)
            category = self.guild.get_channel(archive_id) if archive_id else None
            if isinstance(category, discord.CategoryChannel):
                await channel.edit(category=category, reason=f"Slot #{slot['id']} archived")
                return
        await self.set_visibility(slot, False)  # "disable", or archive with no category set

    # ------------------------------------------------------------ requests

    async def create_request(self, user_id: int, plan, verified: bool) -> int:
        payment = "none" if plan["is_free"] else "awaiting_approval"
        try:
            async with self.db.tx() as tx:
                await self._precheck(tx, user_id, plan)
                slot_id, _ = await tx.exec(
                    "INSERT INTO slots (owner_id, plan_id, status, created_at, payment_status, verified) "
                    "VALUES (?, ?, 'pending', ?, ?, ?)",
                    (user_id, plan["id"], now(), payment, int(verified)),
                )
                await _history(tx, slot_id, user_id, None, "requested", f"plan={plan['name']}")
        except sqlite3.IntegrityError as exc:
            raise ServiceError("You already have an open slot or request.") from exc
        return slot_id

    async def activate(self, slot_id: int, actor_id: int | None) -> tuple[discord.TextChannel, bool]:
        """Create the channel, issue the key, and DM it. Returns (channel, dm_ok)."""
        async with self._activation_lock:
            slot = await self.slot_by_id(slot_id)
            if slot is None or slot["status"] != "pending":
                raise ServiceError("That request is no longer waiting for activation.")
            guild = self.guild
            owner = guild.get_member(slot["owner_id"])
            if owner is None:
                try:
                    owner = await guild.fetch_member(slot["owner_id"])
                except discord.NotFound:
                    raise ServiceError("The slot owner is no longer in the server.")
            category_id = slot["plan_category"] or int(await self.db.setting("default_category_id", "0") or 0)
            category = guild.get_channel(category_id) if category_id else None
            if not isinstance(category, discord.CategoryChannel):
                category = None
            channel = await guild.create_text_channel(
                name=f"promo-{slugify(owner.display_name)}-{slot_id}",
                category=category,
                overwrites=self._overwrites(owner, slot["permissions"]),
                reason=f"Promotional slot #{slot_id}",
            )
            key = security.generate_key()
            try:
                async with self.db.tx() as tx:
                    _, rows = await tx.exec(
                        "UPDATE slots SET status = 'active', channel_id = ?, created_at = ?, expires_at = ?, key_hash = ? "
                        "WHERE id = ? AND status = 'pending'",
                        (channel.id, now(), now() + slot["duration_days"] * DAY, self._hash(key), slot_id),
                    )
                    if rows != 1:
                        raise ServiceError("This request changed while being activated.")
                    await _history(tx, slot_id, slot["owner_id"], actor_id, "activated", f"channel={channel.id}")
            except Exception:
                await channel.delete(reason="Activation rolled back")
                raise
            dm_ok = await send_dm(owner, key_embed(key))
            if not dm_ok:
                # The key was never delivered, so it must not stay valid. The owner can run /regenerate-key.
                await self.db.write("UPDATE slots SET key_hash = NULL WHERE id = ?", (slot_id,))
        await self.log(
            "Slot created",
            f"<@{owner.id}> received {channel.mention} (slot #{slot_id}, {slot['plan_name']}).",
            COLOR_SUCCESS,
        )
        return channel, dm_ok

    async def approve(self, slot_id: int, actor_id: int) -> tuple[discord.TextChannel, bool]:
        await self.db.write(
            "UPDATE slots SET verified = 1, payment_status = CASE WHEN payment_status = 'awaiting_approval' "
            "THEN 'approved' ELSE payment_status END WHERE id = ? AND status = 'pending'",
            (slot_id,),
        )
        return await self.activate(slot_id, actor_id)

    async def reject(self, slot_id: int, actor_id: int, reason: str) -> None:
        async with self.db.tx() as tx:
            _, rows = await tx.exec(
                "UPDATE slots SET status = 'removed', payment_status = 'rejected', note = ? "
                "WHERE id = ? AND status = 'pending'",
                (reason, slot_id),
            )
            if rows != 1:
                raise ServiceError("There is no pending request with that ID.")
            await _history(tx, slot_id, None, actor_id, "rejected", reason)
        await self.log("Request rejected", f"Request #{slot_id} rejected: {reason}", COLOR_ERROR)

    async def claim(self, member: discord.Member, plan) -> ClaimResult:
        await self._precheck(self.db, member.id, plan)
        if plan["is_free"]:
            phrase = (await self.db.setting("requirement_phrase", "")).strip()
            enabled = await self.db.setting("requirement_enabled", "0") == "1"
            if enabled and phrase:
                if not has_phrase(member, phrase):
                    return ClaimResult("needs_verification", message=phrase)
                slot_id = await self.create_request(member.id, plan, verified=True)
                try:
                    channel, dm_ok = await self.activate(slot_id, actor_id=None)
                except Exception:
                    logger.exception("Activation failed for verified slot %s", slot_id)
                    return ClaimResult("pending", slot_id=slot_id, message="Verified, but channel creation failed.")
                return ClaimResult("active", slot_id=slot_id, channel=channel, dm_ok=dm_ok)
        return await self.request_manual(member, plan)

    async def request_manual(self, member: discord.Member, plan) -> ClaimResult:
        slot_id = await self.create_request(member.id, plan, verified=False)
        await self.log(
            "Slot requested",
            f"<@{member.id}> requested **{plan['name']}** (slot #{slot_id}). Awaiting review.",
            COLOR_WARNING,
        )
        return ClaimResult("pending", slot_id=slot_id)

    # ----------------------------------------------------- transfer / recovery

    async def _transfer(self, slot, member: discord.Member, actor_id: int | None, action: str) -> bool:
        """Move ownership, rotate the key, and fix channel permissions. Returns dm_ok."""
        new_key = security.generate_key()
        previous = slot["owner_id"]
        async with self.db.tx() as tx:
            _, rows = await tx.exec(
                "UPDATE slots SET owner_id = ?, owner_left_at = NULL, key_hash = ? "
                "WHERE id = ? AND status = 'active' AND owner_id = ?",
                (member.id, self._hash(new_key), slot["id"], previous),
            )
            if rows != 1:
                raise ServiceError("This slot changed while processing. Please try again.")
            await _history(tx, slot["id"], member.id, actor_id, action, f"previous_owner={previous}")
        channel = self._channel_of(slot)
        if channel is not None:
            if previous != member.id:
                await channel.set_permissions(discord.Object(id=previous), overwrite=None, reason="Slot transferred")
            await channel.set_permissions(
                member, overwrite=self._owner_overwrite(slot["permissions"]), reason="Slot transferred"
            )
        dm_ok = await send_dm(member, key_embed(new_key))
        if not dm_ok:
            await self.db.write("UPDATE slots SET key_hash = NULL WHERE id = ?", (slot["id"],))
        return dm_ok

    async def transfer_slot(
        self, slot_id: int, member: discord.Member, actor_id: int, action: str, require_departed: bool = False
    ) -> bool:
        slot = await self.slot_by_id(slot_id)
        if slot is None or slot["status"] != "active":
            raise ServiceError("Only active slots can be transferred.")
        if require_departed and self.guild.get_member(slot["owner_id"]) is not None:
            raise ServiceError("The current owner is still in the server. Use `/giveslot` to move it anyway.")
        if member.id != slot["owner_id"]:
            await self._assert_can_hold(self.db, member.id)
        dm_ok = await self._transfer(slot, member, actor_id, action)
        await self.log("Slot transferred", f"Slot #{slot_id} is now held by <@{member.id}> ({action}).", COLOR_WARNING)
        return dm_ok

    async def recover(self, member: discord.Member, raw_key: str) -> str:
        if await self.db.one("SELECT 1 FROM blacklist WHERE user_id = ?", (member.id,)):
            raise ServiceError("Your account is blacklisted from the slot program.")
        slot = await self.slot_by_key(raw_key)
        if slot is None:
            raise ServiceError("That recovery key doesn't match a slot. Check it and try again.")
        if slot["status"] != "active":
            raise ServiceError(RECOVERY_BLOCKED.get(slot["status"], "That slot can't be recovered."))
        if member.id != slot["owner_id"]:
            # A key only moves a slot whose owner has left the server. This prevents stealing.
            if self.guild.get_member(slot["owner_id"]) is not None:
                raise ServiceError("This slot is still held by an active member, so it can't be transferred to you.")
            await self._assert_can_hold(self.db, member.id)
        dm_ok = await self._transfer(slot, member, None, "recovered")
        await self.log("Slot recovered", f"<@{member.id}> recovered slot #{slot['id']}.", COLOR_SUCCESS)
        if dm_ok:
            return "Your slot is restored. A new recovery key was sent to your DMs. Save it."
        return "Your slot is restored, but I couldn't DM your new key. Enable DMs and run `/regenerate-key`."

    async def regenerate_key(self, member: discord.Member) -> None:
        slot = await self.slot_for_owner(member.id, "('active')")
        if slot is None:
            raise ServiceError("You don't have an active slot to generate a key for.")
        new_key = security.generate_key()
        await self.db.write("UPDATE slots SET key_hash = ? WHERE id = ?", (self._hash(new_key), slot["id"]))
        if await send_dm(member, key_embed(new_key)):
            await self.db.write(
                "INSERT INTO history (slot_id, user_id, actor_id, action, details, created_at) "
                "VALUES (?, ?, NULL, 'key_regenerated', '', ?)",
                (slot["id"], member.id, now()),
            )
            return
        # Nothing new was delivered, so the old key must keep working.
        await self.db.write("UPDATE slots SET key_hash = ? WHERE id = ?", (slot["key_hash"], slot["id"]))
        raise ServiceError(
            "I couldn't DM your new key, so your previous key is still active. Enable DMs and try again."
        )

    async def request_renewal(self, slot, member: discord.Member) -> None:
        await self.log(
            "Renewal requested",
            f"<@{member.id}> requested renewal for slot #{slot['id']} ({slot['plan_name']}). "
            "Use `/extend-slot` to approve.",
            COLOR_WARNING,
        )

    # ------------------------------------------------------- admin operations

    async def remove_slot(self, slot, actor_id: int | None, reason: str, violation: bool = False) -> None:
        """Mark the slot removed in the database first, then retire its channel."""
        action = await self.db.setting("expiry_action", "archive")
        async with self.db.tx() as tx:
            _, rows = await tx.exec(
                "UPDATE slots SET status = 'removed', note = ? WHERE id = ? AND status IN ('active','pending','banned')",
                (reason, slot["id"]),
            )
            if rows != 1:
                raise ServiceError("That slot is no longer open.")
            await _history(tx, slot["id"], slot["owner_id"], actor_id, "removed", reason)
            if violation:
                await tx.exec(
                    "INSERT INTO violations (user_id, slot_id, kind, reason, created_at) "
                    "VALUES (?, ?, 'slot_removed', ?, ?)",
                    (slot["owner_id"], slot["id"], reason, now()),
                )
        await self._retire(slot, action)
        await self.dm_user(slot["owner_id"], notice_embed("Your slot was removed", reason, COLOR_ERROR))
        await self.log(
            "Slot removed", f"Slot #{slot['id']} (<@{slot['owner_id']}>) removed: {reason}", COLOR_ERROR
        )

    async def hard_delete(self, slot_id: int, actor_id: int) -> None:
        slot = await self.slot_by_id(slot_id)
        if slot is None:
            raise ServiceError("No slot with that ID.")
        async with self.db.tx() as tx:
            _, rows = await tx.exec(
                "UPDATE slots SET status = 'deleted', note = 'deleted by administrator' "
                "WHERE id = ? AND status != 'deleted'",
                (slot_id,),
            )
            if rows != 1:
                raise ServiceError("That slot is already deleted.")
            await _history(tx, slot_id, slot["owner_id"], actor_id, "deleted", "channel deleted")
        await self._retire(slot, "delete")
        await self.log("Slot deleted", f"Slot #{slot_id} channel deleted.", COLOR_ERROR)

    async def set_banned(self, slot_id: int, banned: bool, actor_id: int, reason: str) -> None:
        slot = await self.slot_by_id(slot_id)
        if slot is None:
            raise ServiceError("No slot with that ID.")
        source, target = ("active", "banned") if banned else ("banned", "active")
        async with self.db.tx() as tx:
            _, rows = await tx.exec(
                "UPDATE slots SET status = ? WHERE id = ? AND status = ?", (target, slot_id, source)
            )
            if rows != 1:
                raise ServiceError(f"Slot #{slot_id} is not {source}.")
            await _history(tx, slot_id, slot["owner_id"], actor_id, "banned" if banned else "unbanned", reason)
        await self.set_visibility(slot, not banned)
        await self.log("Slot suspended" if banned else "Slot restored", f"Slot #{slot_id}: {reason}", COLOR_WARNING)

    async def extend(self, slot_id: int, days: int, actor_id: int) -> int:
        slot = await self.slot_by_id(slot_id)
        if slot is None or slot["status"] not in ("active", "expired", "banned"):
            raise ServiceError("That slot can't be extended.")
        reopen = slot["status"] == "expired"
        if reopen and self._channel_of(slot) is None:
            raise ServiceError(
                "The channel no longer exists, so this slot can't be reopened. Create a new slot instead."
            )
        new_expiry = max(slot["expires_at"] or 0, now()) + days * DAY
        async with self.db.tx() as tx:
            await tx.exec(
                "UPDATE slots SET expires_at = ?, status = CASE WHEN status = 'expired' THEN 'active' ELSE status END "
                "WHERE id = ?",
                (new_expiry, slot_id),
            )
            await tx.exec("DELETE FROM slot_notices WHERE slot_id = ?", (slot_id,))
            await _history(tx, slot_id, slot["owner_id"], actor_id, "extended", f"+{days} days")
        if reopen:
            await self.set_visibility(slot, True)
        await self.log("Slot extended", f"Slot #{slot_id} extended by {days} days.", COLOR_SUCCESS)
        return new_expiry

    async def set_expiry(self, slot_id: int, when: int, actor_id: int) -> None:
        async with self.db.tx() as tx:
            _, rows = await tx.exec(
                "UPDATE slots SET expires_at = ? WHERE id = ? AND status IN ('active','banned')", (when, slot_id)
            )
            if rows != 1:
                raise ServiceError("Only active or suspended slots have an expiry to set.")
            await tx.exec("DELETE FROM slot_notices WHERE slot_id = ?", (slot_id,))
            await _history(tx, slot_id, None, actor_id, "expiry_set", str(when))

    async def blacklist_user(self, user_id: int, reason: str, actor_id: int | None) -> None:
        await self.db.write(
            "INSERT INTO blacklist (user_id, reason, actor_id, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET reason = excluded.reason, actor_id = excluded.actor_id",
            (user_id, reason, actor_id, now()),
        )
        slot = await self.slot_for_owner(user_id)
        if slot is not None:
            await self.remove_slot(slot, actor_id, f"Blacklisted: {reason}")

    # --------------------------------------------------------------- expiry

    async def expire_slot(self, slot, reason: str = "Plan period ended") -> None:
        action = await self.db.setting("expiry_action", "archive")
        async with self.db.tx() as tx:
            _, rows = await tx.exec(
                "UPDATE slots SET status = 'expired' WHERE id = ? AND status = 'active'", (slot["id"],)
            )
            if rows != 1:
                return
            await _history(tx, slot["id"], slot["owner_id"], None, "expired", f"{reason}; action={action}")
        await self._retire(slot, action)
        await self.dm_user(slot["owner_id"], notice_embed(
            "Your slot has expired",
            f"Your **{slot['plan_name']}** slot has ended. Use `/renew-slot` to request a renewal.",
            COLOR_WARNING,
        ))
        await self.log(
            "Slot expired",
            f"Slot #{slot['id']} (<@{slot['owner_id']}>): {reason}; action `{action}`.",
            COLOR_WARNING,
        )

    async def _notify_expiry(self, slot, label: str) -> None:
        await self.dm_user(slot["owner_id"], notice_embed(
            "⏳ Slot expiring soon",
            f"Your **{slot['plan_name']}** slot expires in about **{label}** ({rel(slot['expires_at'])}). "
            "Use `/renew-slot` to request a renewal.",
            COLOR_WARNING,
        ))
        await self.log("Expiry reminder", f"Sent a {label} reminder to <@{slot['owner_id']}> for slot #{slot['id']}.")

    async def expiry_cycle(self) -> None:
        rows = await self.db.all(SLOT_SQL + " WHERE s.status = 'active'")
        current = now()
        for slot in rows:
            try:
                if slot["channel_id"] and self._channel_of(slot) is None and await self._channel_gone(slot["channel_id"]):
                    await self.db.write(
                        "UPDATE slots SET status = 'deleted', note = 'channel missing' WHERE id = ? AND status = 'active'",
                        (slot["id"],),
                    )
                    await self.log(
                        "Channel missing",
                        f"Slot #{slot['id']}'s channel no longer exists. Marked deleted.",
                        COLOR_WARNING,
                    )
                    continue
                if slot["expires_at"] is None:
                    continue
                remaining = slot["expires_at"] - current
                if remaining <= 0:
                    await self.expire_slot(slot)
                    continue
                for kind, seconds, label in NOTICES:
                    if remaining <= seconds:
                        # INSERT OR IGNORE makes each reminder fire exactly once, even across restarts.
                        _, inserted = await self.db.write(
                            "INSERT OR IGNORE INTO slot_notices (slot_id, kind) VALUES (?, ?)", (slot["id"], kind)
                        )
                        if inserted:
                            await self._notify_expiry(slot, label)
            except (ServiceError, discord.HTTPException):
                logger.exception("Expiry processing failed for slot %s", slot["id"])

    # ------------------------------------------------------- promotions & quotas

    async def check_quota(self, slot, here: bool, everyone: bool) -> str | None:
        current = now()
        if here:
            used = (await self.db.one(
                "SELECT COUNT(*) AS c FROM ping_log WHERE slot_id = ? AND kind = 'here' AND used_at >= ?",
                (slot["id"], current - DAY),
            ))["c"]
            if used >= slot["here_per_day"]:
                return f"@here allowance is {slot['here_per_day']} per 24 hours"
        if everyone:
            used = (await self.db.one(
                "SELECT COUNT(*) AS c FROM ping_log WHERE slot_id = ? AND kind = 'everyone' AND used_at >= ?",
                (slot["id"], current - 7 * DAY),
            ))["c"]
            if used >= slot["everyone_per_week"]:
                return f"@everyone allowance is {slot['everyone_per_week']} per 7 days"
        return None

    async def record_pings(self, slot_id: int, here: bool, everyone: bool) -> None:
        current = now()
        async with self.db.tx() as tx:
            if here:
                await tx.exec("INSERT INTO ping_log (slot_id, kind, used_at) VALUES (?, 'here', ?)", (slot_id, current))
            if everyone:
                await tx.exec(
                    "INSERT INTO ping_log (slot_id, kind, used_at) VALUES (?, 'everyone', ?)", (slot_id, current)
                )

    async def post_promo(self, member: discord.Member, mention: str, text: str) -> str:
        slot = await self.slot_for_owner(member.id, "('active')")
        if slot is None:
            raise ServiceError("You don't have an active slot.")
        channel = self._channel_of(slot)
        if channel is None:
            raise ServiceError("Your slot channel is missing. Ask an administrator to check it.")
        here = mention == "here"
        everyone = mention == "everyone"
        async with self._ping_lock:
            problem = await self.check_quota(slot, here, everyone)
            if problem:
                await self.punish(slot, member.id, f"Exceeded ping allowance: {problem}")
                raise ServiceError("You went over your ping allowance, so your slot has been removed.")
            content = "@everyone" if everyone else "@here" if here else None
            embed = discord.Embed(description=text[:3900], color=COLOR_PRIMARY)
            embed.set_author(name=f"Promotion from {member.display_name}", icon_url=member.display_avatar.url)
            await channel.send(
                content=content,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(everyone=here or everyone, users=False, roles=False),
            )
            await self.record_pings(slot["id"], here, everyone)
        return channel.mention

    async def punish(self, slot, user_id: int, reason: str) -> None:
        if slot is not None and slot["status"] in ("active", "pending", "banned"):
            await self.remove_slot(slot, None, reason, violation=True)
        else:
            await self.db.write(
                "INSERT INTO violations (user_id, slot_id, kind, reason, created_at) VALUES (?, ?, 'violation', ?, ?)",
                (user_id, slot["id"] if slot is not None else None, reason, now()),
            )
        await self._maybe_blacklist(user_id)

    async def _maybe_blacklist(self, user_id: int) -> None:
        threshold = int(await self.db.setting("blacklist_threshold", "3"))
        removals = (await self.db.one(
            "SELECT COUNT(*) AS c FROM violations WHERE user_id = ? AND kind = 'slot_removed'", (user_id,)
        ))["c"]
        already = await self.db.one("SELECT 1 FROM blacklist WHERE user_id = ?", (user_id,))
        if removals >= threshold and already is None:
            await self.db.write(
                "INSERT INTO blacklist (user_id, reason, actor_id, created_at) VALUES (?, ?, NULL, ?)",
                (user_id, f"Automatic: {removals} slot removals", now()),
            )
            await self.log(
                "User blacklisted (automatic)", f"<@{user_id}> reached {removals} slot removals.", COLOR_ERROR
            )

    async def handle_message(self, message: discord.Message) -> None:
        """Monitor slot channels for unauthorized or over-quota mentions."""
        if message.author.bot or message.guild is None:
            return
        content = message.content or ""
        if not (message.mention_everyone or "@everyone" in content or "@here" in content):
            return
        slot = await self.db.one(
            SLOT_SQL + " WHERE s.channel_id = ? AND s.status = 'active'", (message.channel.id,)
        )
        if slot is None or is_staff(self.bot, message.author):
            return
        if message.author.id == slot["owner_id"]:
            here = "@here" in content
            everyone = "@everyone" in content
            async with self._ping_lock:
                problem = await self.check_quota(slot, here, everyone)
                if problem is None:
                    await self.record_pings(slot["id"], here, everyone)
                    return
                await self._delete(message)
                await self.punish(slot, message.author.id, f"Unauthorized ping: {problem}")
            return
        await self._delete(message)
        await self.db.write(
            "INSERT INTO violations (user_id, slot_id, kind, reason, created_at) "
            "VALUES (?, ?, 'unauthorized_ping', 'Mass mention in a slot channel', ?)",
            (message.author.id, slot["id"], now()),
        )

    @staticmethod
    async def _delete(message: discord.Message) -> None:
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    # ---------------------------------------------------------------- search

    async def search_slots(self, term: str, member: discord.Member) -> list:
        rows = await self.db.all(SLOT_SQL + " WHERE s.status = 'active' AND s.channel_id IS NOT NULL")
        needle = term.casefold()
        guild = self.guild
        gate = asyncio.Semaphore(5)

        async def scan(row):
            channel = guild.get_channel(row["channel_id"])
            if not isinstance(channel, discord.TextChannel):
                return None
            bot_perms = channel.permissions_for(guild.me)
            if not (bot_perms.view_channel and bot_perms.read_message_history):
                return None
            if not channel.permissions_for(member).view_channel:
                return None
            if needle in channel.name.casefold():
                return channel, row, "channel name"
            if channel.topic and needle in channel.topic.casefold():
                return channel, row, "channel topic"
            async with gate:
                try:
                    async for message in channel.history(limit=50):
                        content = message.content or ""
                        index = content.casefold().find(needle)
                        if index != -1:
                            snippet = content[max(0, index - 30): index + len(term) + 30]
                            return channel, row, "…" + discord.utils.escape_mentions(snippet) + "…"
                except discord.HTTPException:
                    return None
            return None

        results = await asyncio.gather(*(scan(row) for row in rows))
        return [result for result in results if result]
