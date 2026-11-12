import json
import logging
import re
import sqlite3
from typing import Literal

import discord
from discord.ext import commands

from ..config import COLOR_ERROR, COLOR_INFO, COLOR_WARNING
from ..services import DAY, RECOVERY_BLOCKED, SLOT_SQL, ServiceError, is_staff, notice_embed, now, rel, scrub
from ..ui import Confirm, info_embed, paginate_lines, send_pages, success_embed
from ..views import STATUS_LABEL, ClaimPanelView, panel_embed, plan_rows

logger = logging.getLogger(__name__)

PLAN_FIELDS = {
    "name": "name",
    "price": "price_label",
    "duration": "duration_days",
    "description": "description",
    "benefits": "benefits",
    "here": "here_per_day",
    "everyone": "everyone_per_week",
    "category": "category_id",
    "active": "active",
    "free": "is_free",
    "permissions": "permissions",
}
PERMISSION_KEYS = frozenset({"attach_files", "embed_links", "add_reactions", "use_external_emojis"})
TRUE_WORDS = {"yes", "on", "true", "1", "y"}
FALSE_WORDS = {"no", "off", "false", "0", "n"}
STATUSES = ("all", "pending", "active", "expired", "banned", "removed", "deleted")


def staff_only():
    async def predicate(ctx):
        if not is_staff(ctx.bot, ctx.author):
            raise commands.CheckFailure("Only administrators can use this command.")
        return True

    return commands.check(predicate)


def parse_id(raw: str) -> int | None:
    match = re.search(r"\d{15,25}", raw)
    return int(match.group()) if match else None


def parse_bool(raw: str) -> bool:
    word = raw.strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise ServiceError("Use yes or no.")


def parse_permissions(raw: str) -> dict:
    if raw.strip().lower() in ("", "none"):
        return {}
    result = {}
    for part in raw.split(","):
        if "=" not in part:
            raise ServiceError("Use the format `attach_files=yes,embed_links=no`.")
        key, value = (piece.strip().lower() for piece in part.split("=", 1))
        if key not in PERMISSION_KEYS:
            raise ServiceError(f"Unknown permission `{key}`. Allowed: {', '.join(sorted(PERMISSION_KEYS))}.")
        result[key] = parse_bool(value)
    return result


def bounded_int(value: int, low: int, high: int, what: str) -> int:
    if not low <= value <= high:
        raise ServiceError(f"{what} must be between {low} and {high}.")
    return value


async def audit(bot, actor_id: int, action: str, details: str = "", slot_id: int | None = None) -> None:
    """Record an administrative action in the history table and the log channel. Never stores keys."""
    safe = scrub(details)
    await bot.db.write(
        "INSERT INTO history (slot_id, user_id, actor_id, action, details, created_at) VALUES (?, NULL, ?, ?, ?, ?)",
        (slot_id, actor_id, action, safe[:500], now()),
    )
    await bot.slots.log("Administrative action", f"<@{actor_id}> ran **{action}**.\n{safe}", COLOR_INFO)


async def refresh_panel(bot) -> None:
    """Re-render the claim panel so it reflects current plans and requirements."""
    channel_id = await bot.db.setting("panel_channel_id")
    message_id = await bot.db.setting("panel_message_id")
    if not channel_id or not message_id:
        return
    try:
        channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
        message = await channel.fetch_message(int(message_id))
        await message.edit(embed=await panel_embed(bot), view=ClaimPanelView())
    except (discord.HTTPException, ValueError):
        logger.warning("Could not refresh the claim panel", exc_info=True)


async def confirm(ctx, prompt: str) -> bool:
    view = Confirm(ctx.author.id)
    await ctx.send(embed=info_embed("Please confirm", prompt), view=view, ephemeral=True)
    await view.wait()
    return bool(view.value)


class AdminCommands(commands.Cog):
    """Administrator commands. Every command checks staff permissions before running."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.svc = bot.slots

    # ---------------------------------------------------------------- helpers

    async def _plan(self, ref: str, active_only: bool = False):
        row = await self.svc.plan_by_key(ref, active_only=active_only)
        if row is None:
            raise ServiceError(f"No plan matches `{ref}`.")
        return row

    async def _slot(self, slot_id: int):
        slot = await self.svc.slot_by_id(slot_id)
        if slot is None:
            raise ServiceError(f"No slot with ID {slot_id}.")
        return slot

    @staticmethod
    def _text(raw: str, low: int, high: int, what: str, allow_empty: bool = False) -> str:
        value = raw.strip()
        if not value and not allow_empty:
            raise ServiceError(f"{what} can't be empty.")
        if len(value) > high or len(value) < low:
            raise ServiceError(f"{what} must be {low}–{high} characters.")
        return value

    # -------------------------------------------------------------- setup

    @commands.hybrid_command(name="setup")
    @commands.guild_only()
    @staff_only()
    async def setup(self, ctx):
        """Show the current configuration and anything still missing."""
        s = self.db.setting
        log_id = await s("log_channel_id")
        panel_id = await s("panel_channel_id")
        default_cat = await s("default_category_id")
        archive_cat = await s("archive_category_id")
        req_on = await s("requirement_enabled", "0") == "1"
        phrase = await s("requirement_phrase", "")
        gl = await s("gl_text", "")
        action = await s("expiry_action", "archive")
        threshold = await s("blacklist_threshold", "3")
        plan_count = len(await plan_rows(self.bot))
        me = ctx.guild.me.guild_permissions
        perms_ok = me.manage_channels and me.manage_messages and me.read_message_history

        missing = []
        if not log_id:
            missing.append("Log channel: `/setlogchannel`")
        if not panel_id:
            missing.append("Claim panel: `/setclaimpanel`")
        if plan_count == 0:
            missing.append("At least one plan: `/createplan`")
        if not perms_ok:
            missing.append("Bot needs Manage Channels, Manage Messages and Read Message History")

        embed = info_embed("⚙️ Slot program setup")
        embed.add_field(name="Log channel", value=f"<#{log_id}>" if log_id else "Not set", inline=True)
        embed.add_field(name="Claim panel", value=f"<#{panel_id}>" if panel_id else "Not set", inline=True)
        embed.add_field(name="Plans", value=str(plan_count), inline=True)
        embed.add_field(name="Default category", value=f"<#{default_cat}>" if default_cat not in (None, "", "0") else "Not set", inline=True)
        embed.add_field(name="Archive category", value=f"<#{archive_cat}>" if archive_cat not in (None, "", "0") else "Not set", inline=True)
        embed.add_field(name="Expiry action", value=action, inline=True)
        embed.add_field(
            name="Free-slot verification",
            value=(f"On · `{phrase}`" if req_on and phrase else "Off"),
            inline=True,
        )
        embed.add_field(name="GL search text", value=gl or "Not set", inline=True)
        embed.add_field(name="Auto-blacklist after", value=f"{threshold} removals", inline=True)
        embed.add_field(
            name="Checklist",
            value="\n".join(f"• {item}" for item in missing) if missing else "✅ Everything is configured.",
            inline=False,
        )
        await ctx.send(embed=embed, ephemeral=True)

    @commands.hybrid_command(name="setlogchannel")
    @commands.guild_only()
    @staff_only()
    async def setlogchannel(self, ctx, channel: discord.TextChannel):
        """Set the channel where administrative and slot events are logged."""
        await self.db.set_setting("log_channel_id", channel.id)
        await audit(self.bot, ctx.author.id, "setlogchannel", f"channel={channel.id}")
        await ctx.send(embed=success_embed("Log channel saved", f"Events will be logged to {channel.mention}."), ephemeral=True)

    @commands.hybrid_command(name="setcategory")
    @commands.guild_only()
    @staff_only()
    async def setcategory(
        self, ctx, category: discord.CategoryChannel, scope: Literal["default", "archive"] = "default"
    ):
        """Set the default category for new slots, or the archive category for retired slots."""
        key = "default_category_id" if scope == "default" else "archive_category_id"
        await self.db.set_setting(key, category.id)
        await audit(self.bot, ctx.author.id, "setcategory", f"{scope}={category.id}")
        await ctx.send(
            embed=success_embed("Category saved", f"The {scope} category is now **{category.name}**."),
            ephemeral=True,
        )

    @commands.hybrid_command(name="setexpiryaction")
    @commands.guild_only()
    @staff_only()
    async def setexpiryaction(self, ctx, action: Literal["archive", "disable", "delete"]):
        """Choose what happens to a channel when its slot expires or is removed."""
        await self.db.set_setting("expiry_action", action)
        await audit(self.bot, ctx.author.id, "setexpiryaction", action)
        await ctx.send(embed=success_embed("Expiry action saved", f"Expired channels will be **{action}d**."), ephemeral=True)

    @commands.hybrid_command(name="setthreshold")
    @commands.guild_only()
    @staff_only()
    async def setthreshold(self, ctx, removals: int):
        """Set how many slot removals trigger an automatic blacklist."""
        bounded_int(removals, 1, 50, "Threshold")
        await self.db.set_setting("blacklist_threshold", removals)
        await audit(self.bot, ctx.author.id, "setthreshold", str(removals))
        await ctx.send(embed=success_embed("Threshold saved", f"Automatic blacklist after **{removals}** removals."), ephemeral=True)

    @commands.hybrid_command(name="setgltext")
    @commands.guild_only()
    @staff_only()
    async def setgltext(self, ctx, *, text: str = ""):
        """Set the default search text used by /find. Leave empty to clear it."""
        value = self._text(text, 0, 200, "GL text", allow_empty=True)
        await self.db.set_setting("gl_text", value)
        await audit(self.bot, ctx.author.id, "setgltext", value or "(cleared)")
        await ctx.send(embed=success_embed("GL text saved", f"Default search: `{value or 'none'}`"), ephemeral=True)

    @commands.hybrid_command(name="setclaimpanel")
    @commands.guild_only()
    @staff_only()
    async def setclaimpanel(self, ctx, channel: discord.TextChannel):
        """Post the claim panel in a channel. Replaces the previous panel."""
        old_channel = await self.db.setting("panel_channel_id")
        old_message = await self.db.setting("panel_message_id")
        message = await channel.send(embed=await panel_embed(self.bot), view=ClaimPanelView())
        await self.db.set_setting("panel_channel_id", channel.id)
        await self.db.set_setting("panel_message_id", message.id)
        if old_channel and old_message and str(old_message) != str(message.id):
            try:
                old = self.bot.get_channel(int(old_channel)) or await self.bot.fetch_channel(int(old_channel))
                await (await old.fetch_message(int(old_message))).delete()
            except (discord.HTTPException, ValueError):
                pass
        await audit(self.bot, ctx.author.id, "setclaimpanel", f"channel={channel.id}")
        await ctx.send(embed=success_embed("Claim panel posted", f"Panel is live in {channel.mention}."), ephemeral=True)

    # ------------------------------------------------- free-slot verification

    @commands.hybrid_command(name="setrequirement")
    @commands.guild_only()
    @staff_only()
    async def setrequirement(self, ctx, *, phrase: str):
        """Set the phrase members must put in their custom status for free slots."""
        value = self._text(phrase, 3, 128, "Phrase")
        await self.db.set_setting("requirement_phrase", value)
        await refresh_panel(self.bot)
        await audit(self.bot, ctx.author.id, "setrequirement", value)
        await ctx.send(
            embed=success_embed(
                "Requirement saved",
                f"Phrase: `{value}`. Turn it on with `/setrequirementstatus on`.",
            ),
            ephemeral=True,
        )

    @commands.hybrid_command(name="setrequirementstatus")
    @commands.guild_only()
    @staff_only()
    async def setrequirementstatus(self, ctx, status: Literal["on", "off"]):
        """Turn free-slot custom-status verification on or off."""
        if status == "on" and not (await self.db.setting("requirement_phrase", "") or "").strip():
            raise ServiceError("Set a phrase first with `/setrequirement`.")
        await self.db.set_setting("requirement_enabled", "1" if status == "on" else "0")
        await refresh_panel(self.bot)
        await audit(self.bot, ctx.author.id, "setrequirementstatus", status)
        await ctx.send(embed=success_embed("Verification updated", f"Custom-status verification is **{status}**."), ephemeral=True)

    @commands.hybrid_command(name="remove-requirement")
    @commands.guild_only()
    @staff_only()
    async def remove_requirement(self, ctx):
        """Delete the verification phrase and turn verification off."""
        await self.db.set_setting("requirement_phrase", "")
        await self.db.set_setting("requirement_enabled", "0")
        await refresh_panel(self.bot)
        await audit(self.bot, ctx.author.id, "remove-requirement")
        await ctx.send(embed=success_embed("Requirement removed", "Free slots now go through manual review."), ephemeral=True)

    @commands.hybrid_command(name="verify-user")
    @commands.guild_only()
    @staff_only()
    async def verify_user(self, ctx, member: discord.Member):
        """Manually verify a member's pending free-slot request and create their channel."""
        slot = await self.svc.slot_for_owner(member.id, "('pending')")
        if slot is None:
            raise ServiceError(f"{member.display_name} has no pending request.")
        if not slot["is_free"]:
            raise ServiceError("Paid requests are confirmed with `/approvepayment`.")
        channel, dm_ok = await self.svc.approve(slot["id"], ctx.author.id)
        await audit(self.bot, ctx.author.id, "verify-user", f"user={member.id} slot={slot['id']}", slot["id"])
        note = "" if dm_ok else " I couldn't DM the recovery key. Ask them to run `/regenerate-key`."
        await ctx.send(
            embed=success_embed("User verified", f"{member.mention} verified. Channel: {channel.mention}.{note}"),
            ephemeral=True,
        )

    # --------------------------------------------------------------- plans

    @commands.hybrid_command(name="createplan")
    @commands.guild_only()
    @staff_only()
    async def createplan(
        self,
        ctx,
        name: str,
        price_label: str,
        duration_days: int,
        max_slots: int,
        is_free: bool = False,
        here_per_day: int = 2,
        everyone_per_week: int = 1,
    ):
        """Create a plan. Use underscores for spaces in the name and price."""
        name_value = self._text(name.replace("_", " "), 1, 40, "Plan name")
        price = self._text(price_label.replace("_", " "), 0, 40, "Price label", allow_empty=True)
        bounded_int(duration_days, 1, 3650, "Duration")
        bounded_int(max_slots, 0, 10000, "Max slots")
        bounded_int(here_per_day, 0, 50, "@here allowance")
        bounded_int(everyone_per_week, 0, 50, "@everyone allowance")
        try:
            await self.db.write(
                "INSERT INTO plans (name, price_label, duration_days, max_slots, is_free, here_per_day, everyone_per_week) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name_value, price, duration_days, max_slots, int(is_free), here_per_day, everyone_per_week),
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError(f"A plan named `{name_value}` already exists.") from exc
        await refresh_panel(self.bot)
        await audit(self.bot, ctx.author.id, "createplan", f"{name_value} ({price or 'Free'}, {duration_days}d)")
        await ctx.send(embed=success_embed("Plan created", f"**{name_value}** is open with {max_slots} slots."), ephemeral=True)

    @commands.hybrid_command(name="editplan")
    @commands.guild_only()
    @staff_only()
    async def editplan(
        self,
        ctx,
        plan: str,
        field: Literal[
            "name", "price", "duration", "description", "benefits", "here", "everyone",
            "category", "active", "free", "permissions",
        ],
        *,
        value: str,
    ):
        """Edit one field of a plan. Permissions use the form attach_files=yes,embed_links=no."""
        row = await self._plan(plan)
        raw = value.strip()
        if field == "name":
            new = self._text(raw, 1, 40, "Plan name")
        elif field == "price":
            new = self._text(raw, 0, 40, "Price label", allow_empty=True)
        elif field == "duration":
            new = bounded_int(int(raw), 1, 3650, "Duration")
        elif field == "description":
            new = self._text(raw, 0, 500, "Description", allow_empty=True)
        elif field == "benefits":
            new = self._text(raw, 0, 1000, "Benefits", allow_empty=True)
        elif field == "here":
            new = bounded_int(int(raw), 0, 50, "@here allowance")
        elif field == "everyone":
            new = bounded_int(int(raw), 0, 50, "@everyone allowance")
        elif field == "category":
            if raw.lower() in ("", "none"):
                new = None
            else:
                new = parse_id(raw)
                if new is None:
                    raise ServiceError("Give a category channel mention or ID, or `none`.")
        elif field in ("active", "free"):
            new = int(parse_bool(raw))
        else:  # permissions
            new = json.dumps(parse_permissions(raw))

        column = PLAN_FIELDS[field]  # whitelisted above; never user-controlled
        try:
            await self.db.write(f"UPDATE plans SET {column} = ? WHERE id = ?", (new, row["id"]))
        except sqlite3.IntegrityError as exc:
            raise ServiceError("Another plan already uses that name.") from exc
        await refresh_panel(self.bot)
        await audit(self.bot, ctx.author.id, "editplan", f"{row['name']}: {field} updated")
        await ctx.send(embed=success_embed("Plan updated", f"**{row['name']}**: `{field}` changed."), ephemeral=True)

    @commands.hybrid_command(name="deleteplan")
    @commands.guild_only()
    @staff_only()
    async def deleteplan(self, ctx, plan: str):
        """Delete a plan. Plans with slot history are closed instead."""
        row = await self._plan(plan)
        used = (await self.db.one("SELECT COUNT(*) AS c FROM slots WHERE plan_id = ?", (row["id"],)))["c"]
        if used:
            await self.db.write("UPDATE plans SET active = 0 WHERE id = ?", (row["id"],))
            await refresh_panel(self.bot)
            await audit(self.bot, ctx.author.id, "deleteplan", f"{row['name']} closed (has history)")
            await ctx.send(
                embed=info_embed(
                    "Plan closed",
                    f"**{row['name']}** has {used} slot record(s), so it was closed instead of deleted.",
                ),
                ephemeral=True,
            )
            return
        if not await confirm(ctx, f"Permanently delete **{row['name']}**? This cannot be undone."):
            return
        await self.db.write("DELETE FROM plans WHERE id = ?", (row["id"],))
        await refresh_panel(self.bot)
        await audit(self.bot, ctx.author.id, "deleteplan", row["name"])
        await ctx.send(embed=success_embed("Plan deleted", f"**{row['name']}** was removed."), ephemeral=True)

    @commands.hybrid_command(name="listplans")
    @commands.guild_only()
    @staff_only()
    async def listplans(self, ctx):
        """List every plan, including closed ones, with usage."""
        rows = await self.db.all(
            "SELECT p.*, (SELECT COUNT(*) FROM slots s WHERE s.plan_id = p.id "
            "AND s.status IN ('pending','active','banned')) AS used FROM plans p ORDER BY p.id"
        )
        if not rows:
            raise ServiceError("No plans exist yet. Create one with `/createplan`.")
        lines = [
            f"{'🟢' if r['active'] else '⚫'} **{r['name']}** · {r['price_label'] or 'Free'} · "
            f"{r['duration_days']}d · {r['used']}/{r['max_slots']} used · "
            f"@here {r['here_per_day']}/day · @everyone {r['everyone_per_week']}/week · id `{r['id']}`"
            for r in rows
        ]
        await send_pages(ctx, paginate_lines("📦 Plans", lines, per_page=8), ephemeral=True)

    @commands.hybrid_command(name="setplanlimit")
    @commands.guild_only()
    @staff_only()
    async def setplanlimit(self, ctx, plan: str, max_slots: int):
        """Change how many slots a plan can have open at once."""
        row = await self._plan(plan)
        bounded_int(max_slots, 0, 10000, "Max slots")
        await self.db.write("UPDATE plans SET max_slots = ? WHERE id = ?", (max_slots, row["id"]))
        await refresh_panel(self.bot)
        await audit(self.bot, ctx.author.id, "setplanlimit", f"{row['name']} → {max_slots}")
        used = row["used"] if "used" in row.keys() else None
        note = ""
        if used is not None and used > max_slots:
            note = " Existing slots were kept, so the plan is now over its limit."
        await ctx.send(embed=success_embed("Limit updated", f"**{row['name']}** now allows {max_slots} slots.{note}"), ephemeral=True)

    # ----------------------------------------------------------- payments

    @commands.hybrid_command(name="approvepayment")
    @commands.guild_only()
    @staff_only()
    async def approvepayment(self, ctx, slot_id: int):
        """Approve a pending paid request and create the channel."""
        slot = await self._slot(slot_id)
        if slot["status"] != "pending":
            raise ServiceError(f"Request #{slot_id} is {slot['status']}, not pending.")
        channel, dm_ok = await self.svc.approve(slot_id, ctx.author.id)
        await audit(self.bot, ctx.author.id, "approvepayment", f"slot={slot_id} plan={slot['plan_name']}", slot_id)
        note = "" if dm_ok else " I couldn't DM the recovery key; ask the owner to run `/regenerate-key`."
        await ctx.send(embed=success_embed("Payment approved", f"Channel created: {channel.mention}.{note}"), ephemeral=True)

    @commands.hybrid_command(name="rejectpayment")
    @commands.guild_only()
    @staff_only()
    async def rejectpayment(self, ctx, slot_id: int, *, reason: str = "Payment could not be confirmed"):
        """Reject a pending request and tell the member why."""
        slot = await self._slot(slot_id)
        await self.svc.reject(slot_id, ctx.author.id, reason)
        await self.svc.dm_user(slot["owner_id"], notice_embed("Request rejected", reason, COLOR_ERROR))
        await audit(self.bot, ctx.author.id, "rejectpayment", f"slot={slot_id}: {reason}", slot_id)
        await ctx.send(embed=success_embed("Request rejected", f"Request #{slot_id} rejected."), ephemeral=True)

    # ------------------------------------------------------- slot management

    @commands.hybrid_command(name="createslot")
    @commands.guild_only()
    @staff_only()
    async def createslot(self, ctx, member: discord.Member, plan: str):
        """Create a slot for a member directly, without payment or verification."""
        plan_row = await self._plan(plan, active_only=True)
        slot_id = await self.svc.create_request(member.id, plan_row, verified=True)
        await self.db.write(
            "UPDATE slots SET payment_status = 'approved' WHERE id = ? AND payment_status = 'awaiting_approval'",
            (slot_id,),
        )
        try:
            channel, dm_ok = await self.svc.activate(slot_id, ctx.author.id)
        except Exception:
            await self.db.write(
                "UPDATE slots SET status = 'removed', note = 'activation failed' WHERE id = ? AND status = 'pending'",
                (slot_id,),
            )
            raise
        await audit(self.bot, ctx.author.id, "createslot", f"user={member.id} plan={plan_row['name']}", slot_id)
        note = "" if dm_ok else " I couldn't DM the key; ask them to run `/regenerate-key`."
        await ctx.send(embed=success_embed("Slot created", f"{channel.mention} is ready for {member.mention}.{note}"), ephemeral=True)

    @commands.hybrid_command(name="deleteslot")
    @commands.guild_only()
    @staff_only()
    async def deleteslot(self, ctx, slot_id: int):
        """Permanently delete a slot's channel. Requires confirmation."""
        slot = await self._slot(slot_id)
        if not await confirm(ctx, f"Delete the channel for slot #{slot_id} (<#{slot['channel_id']}>)? "
                                  "The channel will be removed from Discord."):
            return
        await self.svc.hard_delete(slot_id, ctx.author.id)
        await audit(self.bot, ctx.author.id, "deleteslot", f"slot={slot_id}", slot_id)
        await ctx.send(embed=success_embed("Slot deleted", f"Slot #{slot_id} was deleted."), ephemeral=True)

    @commands.hybrid_command(name="giveslot")
    @commands.guild_only()
    @staff_only()
    async def giveslot(self, ctx, slot_id: int, member: discord.Member):
        """Transfer an active slot to another member and issue them a new key."""
        dm_ok = await self.svc.transfer_slot(slot_id, member, ctx.author.id, "transferred")
        await audit(self.bot, ctx.author.id, "giveslot", f"slot={slot_id} to={member.id}", slot_id)
        note = "" if dm_ok else " Their DMs are closed, so the key was not delivered."
        await ctx.send(embed=success_embed("Slot transferred", f"Slot #{slot_id} is now {member.mention}'s.{note}"), ephemeral=True)

    @commands.hybrid_command(name="admin-recover")
    @commands.guild_only()
    @staff_only()
    async def admin_recover(self, ctx, key: str, member: discord.Member):
        """Administrator recovery: move a slot to a member using its recovery key, regardless of the old owner."""
        if ctx.interaction is None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
        slot = await self.svc.slot_by_key(key)
        if slot is None:
            raise ServiceError("That recovery key doesn't match any slot.")
        if slot["status"] != "active":
            raise ServiceError(RECOVERY_BLOCKED.get(slot["status"], "That slot can't be recovered."))
        dm_ok = await self.svc.transfer_slot(slot["id"], member, ctx.author.id, "admin_recovered")
        await audit(self.bot, ctx.author.id, "admin-recover", f"slot={slot['id']} to={member.id}", slot["id"])
        note = "" if dm_ok else " Their DMs are closed, so the new key was not delivered."
        await ctx.send(embed=success_embed("Slot recovered", f"Slot #{slot['id']} is now {member.mention}'s.{note}"), ephemeral=True)

    @commands.hybrid_command(name="removeslot")
    @commands.guild_only()
    @staff_only()
    async def removeslot(self, ctx, slot_id: int, *, reason: str = "Removed by administrator"):
        """Remove a slot. This is not counted as a member violation."""
        slot = await self._slot(slot_id)
        await self.svc.remove_slot(slot, ctx.author.id, reason, violation=False)
        await audit(self.bot, ctx.author.id, "removeslot", f"slot={slot_id}: {reason}", slot_id)
        await ctx.send(embed=success_embed("Slot removed", f"Slot #{slot_id} was removed."), ephemeral=True)

    @commands.hybrid_command(name="ban-slot")
    @commands.guild_only()
    @staff_only()
    async def ban_slot(self, ctx, slot_id: int, *, reason: str = "Suspended by administrator"):
        """Suspend a slot. Its channel is hidden until it is unbanned."""
        await self.svc.set_banned(slot_id, True, ctx.author.id, reason)
        await audit(self.bot, ctx.author.id, "ban-slot", f"slot={slot_id}: {reason}", slot_id)
        await ctx.send(embed=success_embed("Slot suspended", f"Slot #{slot_id} is suspended."), ephemeral=True)

    @commands.hybrid_command(name="unban-slot")
    @commands.guild_only()
    @staff_only()
    async def unban_slot(self, ctx, slot_id: int):
        """Restore a suspended slot."""
        await self.svc.set_banned(slot_id, False, ctx.author.id, "Restored by administrator")
        await audit(self.bot, ctx.author.id, "unban-slot", f"slot={slot_id}", slot_id)
        await ctx.send(embed=success_embed("Slot restored", f"Slot #{slot_id} is active again."), ephemeral=True)

    @commands.hybrid_command(name="extend-slot")
    @commands.guild_only()
    @staff_only()
    async def extend_slot(self, ctx, slot_id: int, days: int):
        """Add days to a slot. Expired slots are reopened."""
        bounded_int(days, 1, 3650, "Days")
        new_expiry = await self.svc.extend(slot_id, days, ctx.author.id)
        await audit(self.bot, ctx.author.id, "extend-slot", f"slot={slot_id} +{days}d", slot_id)
        await ctx.send(embed=success_embed("Slot extended", f"Slot #{slot_id} now expires {rel(new_expiry)}."), ephemeral=True)

    @commands.hybrid_command(name="setexpiry")
    @commands.guild_only()
    @staff_only()
    async def setexpiry(self, ctx, slot_id: int, days: int):
        """Set a slot to expire a number of days from now."""
        bounded_int(days, 1, 3650, "Days")
        when = now() + days * DAY
        await self.svc.set_expiry(slot_id, when, ctx.author.id)
        await audit(self.bot, ctx.author.id, "setexpiry", f"slot={slot_id} in {days}d", slot_id)
        await ctx.send(embed=success_embed("Expiry set", f"Slot #{slot_id} expires {rel(when)}."), ephemeral=True)

    @commands.hybrid_command(name="allslots")
    @commands.guild_only()
    @staff_only()
    async def allslots(self, ctx, status: Literal[STATUSES] = "all"):
        """List slots, optionally filtered by status."""
        if status == "all":
            rows = await self.db.all(SLOT_SQL + " ORDER BY s.id DESC LIMIT 500")
        else:
            rows = await self.db.all(SLOT_SQL + " WHERE s.status = ? ORDER BY s.id DESC LIMIT 500", (status,))
        lines = [
            f"`#{r['id']}` <#{r['channel_id']}> · <@{r['owner_id']}> · {r['plan_name']} · "
            f"{STATUS_LABEL.get(r['status'], r['status'])} · {rel(r['expires_at'])}"
            if r["channel_id"]
            else f"`#{r['id']}` no channel · <@{r['owner_id']}> · {r['plan_name']} · "
                 f"{STATUS_LABEL.get(r['status'], r['status'])}"
            for r in rows
        ]
        await send_pages(ctx, paginate_lines(f"🗂️ Slots · {status}", lines, per_page=10), ephemeral=True)

    @commands.hybrid_command(name="searchslot")
    @commands.guild_only()
    @staff_only()
    async def searchslot(self, ctx, *, query: str):
        """Find slots by slot ID, owner mention or ID, channel mention or ID, or plan name."""
        text = query.strip()
        snowflake = parse_id(text)
        if text.isdigit() and len(text) < 15:
            rows = await self.db.all(SLOT_SQL + " WHERE s.id = ? ORDER BY s.id DESC LIMIT 50", (int(text),))
        elif snowflake is not None:
            rows = await self.db.all(
                SLOT_SQL + " WHERE s.owner_id = ? OR s.channel_id = ? ORDER BY s.id DESC LIMIT 50",
                (snowflake, snowflake),
            )
        else:
            rows = await self.db.all(
                SLOT_SQL + " WHERE p.name LIKE ? COLLATE NOCASE ORDER BY s.id DESC LIMIT 50", (f"%{text}%",)
            )
        if not rows:
            raise ServiceError(f"Nothing matched `{text}`.")
        lines = [
            f"`#{r['id']}` <@{r['owner_id']}> · {r['plan_name']} · {STATUS_LABEL.get(r['status'], r['status'])}"
            for r in rows
        ]
        await send_pages(ctx, paginate_lines("🔍 Slot search", lines, per_page=10), ephemeral=True)

    @commands.hybrid_command(name="slotstats")
    @commands.guild_only()
    @staff_only()
    async def slotstats(self, ctx):
        """Show slot counts, plan usage, and recent violations."""
        rows = await self.db.all("SELECT status, COUNT(*) AS c FROM slots GROUP BY status")
        counts = {r["status"]: r["c"] for r in rows}
        violations = (await self.db.one(
            "SELECT COUNT(*) AS c FROM violations WHERE created_at >= ?", (now() - 7 * DAY,)
        ))["c"]
        blacklisted = (await self.db.one("SELECT COUNT(*) AS c FROM blacklist"))["c"]
        plans = await plan_rows(self.bot)

        embed = info_embed("📊 Slot statistics")
        embed.add_field(
            name="Slots",
            value="\n".join(f"{STATUS_LABEL.get(k, k)}: **{v}**" for k, v in counts.items()) or "None yet",
            inline=True,
        )
        embed.add_field(name="Violations (7 days)", value=str(violations), inline=True)
        embed.add_field(name="Blacklisted users", value=str(blacklisted), inline=True)
        embed.add_field(
            name="Plan usage",
            value="\n".join(f"**{p['name']}**: {p['used']}/{p['max_slots']}" for p in plans)[:1024] or "No open plans",
            inline=False,
        )
        await ctx.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------- members

    @commands.hybrid_command(name="blacklist")
    @commands.guild_only()
    @staff_only()
    async def blacklist(self, ctx, user: discord.User, *, reason: str = "Blacklisted by administrator"):
        """Block a user from the slot program and remove their open slot."""
        await self.svc.blacklist_user(user.id, reason, ctx.author.id)
        await self.svc.dm_user(user.id, notice_embed("Slot program access removed", reason, COLOR_ERROR))
        await audit(self.bot, ctx.author.id, "blacklist", f"user={user.id}: {reason}")
        await ctx.send(embed=success_embed("User blacklisted", f"{user.mention} can no longer hold a slot."), ephemeral=True)

    @commands.hybrid_command(name="unblacklist")
    @commands.guild_only()
    @staff_only()
    async def unblacklist(self, ctx, user: discord.User):
        """Remove a user from the blacklist."""
        _, rows = await self.db.write("DELETE FROM blacklist WHERE user_id = ?", (user.id,))
        if rows == 0:
            raise ServiceError(f"{user.mention} is not blacklisted.")
        await audit(self.bot, ctx.author.id, "unblacklist", f"user={user.id}")
        await ctx.send(embed=success_embed("Blacklist cleared", f"{user.mention} can hold a slot again."), ephemeral=True)

    @commands.hybrid_command(name="resetuser")
    @commands.guild_only()
    @staff_only()
    async def resetuser(self, ctx, user: discord.User):
        """Clear a user's blacklist and violation history. Their slots are not changed."""
        if not await confirm(ctx, f"Clear the blacklist and violation history for {user.mention}? "
                                  "Their slots will not be touched."):
            return
        await self.db.write("DELETE FROM blacklist WHERE user_id = ?", (user.id,))
        await self.db.write("DELETE FROM violations WHERE user_id = ?", (user.id,))
        await audit(self.bot, ctx.author.id, "resetuser", f"user={user.id}")
        await ctx.send(embed=success_embed("User reset", f"{user.mention}'s history has been cleared."), ephemeral=True)
