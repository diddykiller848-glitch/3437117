import discord

from .config import COLOR_PRIMARY
from .services import SLOT_SQL, ClaimResult, ServiceError, rel, ts
from .ui import error_embed, info_embed, success_embed

STATUS_LABEL = {
    "pending": "⏳ Awaiting approval",
    "active": "🟢 Active",
    "expired": "⌛ Expired",
    "banned": "🔒 Suspended",
    "removed": "🗑️ Removed",
    "deleted": "🗑️ Deleted",
}


async def plan_rows(bot):
    """Active plans with the number of slots currently in use."""
    return await bot.db.all(
        "SELECT p.*, (SELECT COUNT(*) FROM slots s WHERE s.plan_id = p.id "
        "AND s.status IN ('pending','active','banned')) AS used "
        "FROM plans p WHERE p.active = 1 ORDER BY p.is_free DESC, p.id"
    )


def plan_left(plan) -> int:
    return max(plan["max_slots"] - plan["used"], 0)


def plan_field_value(plan) -> str:
    lines = [
        plan["description"] or "—",
        f"⏳ {plan['duration_days']} days · 📦 {plan_left(plan)} of {plan['max_slots']} open",
        f"📢 @here {plan['here_per_day']}/day · @everyone {plan['everyone_per_week']}/week",
    ]
    return "\n".join(lines)[:1024]


def plan_title(plan) -> str:
    icon = "🆓" if plan["is_free"] else "💎"
    return f"{icon} {plan['name']} · {plan['price_label'] or 'Free'}"[:256]


async def plans_embed(bot) -> discord.Embed:
    plans = await plan_rows(bot)
    embed = discord.Embed(title="📦 Available plans", color=COLOR_PRIMARY)
    if not plans:
        embed.description = "No plans are open right now."
        return embed
    for plan in plans[:24]:
        embed.add_field(name=plan_title(plan), value=plan_field_value(plan), inline=False)
    return embed


async def panel_embed(bot) -> discord.Embed:
    guild = bot.get_guild(bot.cfg.guild_id)
    embed = discord.Embed(
        title="🚀 Promotional Slot Program",
        description=(
            "Get your own promotion channel in front of every member.\n"
            "Press **Claim Slot**, pick a plan, and follow the steps."
        ),
        color=COLOR_PRIMARY,
    )
    if guild is not None and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    for plan in (await plan_rows(bot))[:20]:
        embed.add_field(name=plan_title(plan), value=plan_field_value(plan), inline=False)

    enabled = await bot.db.setting("requirement_enabled", "0") == "1"
    phrase = (await bot.db.setting("requirement_phrase", "") or "").strip()
    if enabled and phrase:
        requirement = (
            "Set your **Discord custom status** so it contains:\n"
            f"`{phrase}`\n"
            "Then press **Claim Slot**. If the bot can't see your status, "
            "press **Check again** or **Request manual review**."
        )
    else:
        requirement = "Free slots are reviewed by an administrator."
    embed.add_field(name="✅ Free slot requirement", value=requirement, inline=False)
    embed.add_field(
        name="📜 Rules",
        value=(
            "Channels are view-only for members. Publish with `/post-promo` within your allowance. "
            "Going over it removes your slot."
        ),
        inline=False,
    )
    return embed


async def rules_embed(bot) -> discord.Embed:
    return info_embed("📜 Slot rules", await bot.db.setting("rules_text", "") or "No rules set yet.")


def plan_detail_embed(plan) -> discord.Embed:
    benefits = [b.strip() for b in (plan["benefits"] or "").split(";") if b.strip()]
    embed = discord.Embed(title=plan_title(plan), description=plan["description"] or "—", color=COLOR_PRIMARY)
    embed.add_field(name="Duration", value=f"{plan['duration_days']} days", inline=True)
    embed.add_field(
        name="Mention allowance",
        value=f"@here {plan['here_per_day']}/day\n@everyone {plan['everyone_per_week']}/week",
        inline=True,
    )
    embed.add_field(
        name="Benefits",
        value="\n".join(f"• {b}" for b in benefits)[:1024] or "—",
        inline=False,
    )
    return embed


async def myslot_embed(bot, user) -> discord.Embed:
    slot = await bot.db.one(
        SLOT_SQL + " WHERE s.owner_id = ? "
        "ORDER BY (s.status IN ('pending','active','banned')) DESC, s.id DESC LIMIT 1",
        (user.id,),
    )
    if slot is None:
        return info_embed("No slot yet", "You don't have a promotional slot. Use `/claim-slot` or the claim panel.")
    channel = bot.get_channel(slot["channel_id"]) if slot["channel_id"] else None
    expires = slot["expires_at"]
    embed = discord.Embed(title=f"📌 Your slot #{slot['id']}", color=COLOR_PRIMARY)
    embed.add_field(name="Channel", value=channel.mention if channel else "Not available", inline=True)
    embed.add_field(name="Plan", value=slot["plan_name"], inline=True)
    embed.add_field(name="Status", value=STATUS_LABEL.get(slot["status"], slot["status"]), inline=True)
    embed.add_field(name="Created", value=ts(slot["created_at"]), inline=True)
    embed.add_field(
        name="Expires",
        value=f"{ts(expires)} ({rel(expires)})" if expires else "—",
        inline=True,
    )
    embed.add_field(
        name="Recovery",
        value=(
            "Lost access? Run `/recover-slot` with your key.\n"
            "Lost the key? Run `/regenerate-key` while the slot is active."
        ),
        inline=False,
    )
    return embed


async def claim_response(bot, member, plan):
    """Run a claim and build the reply. Returns (embed, view_or_None)."""
    try:
        result: ClaimResult = await bot.slots.claim(member, plan)
    except ServiceError as exc:
        return error_embed("Claim not possible", str(exc)), None

    if result.status == "active":
        if result.dm_ok:
            note = "Your recovery key was sent to your DMs. Save it now; it is shown only once."
        else:
            note = "⚠️ I couldn't DM your recovery key. Enable DMs from server members, then run `/regenerate-key`."
        return success_embed(
            "Slot created 🎉",
            f"Your channel: {result.channel.mention}\nPlan: **{plan['name']}**\n\n{note}",
        ), None

    if result.status == "pending":
        if plan["is_free"]:
            text = "Your request is in the review queue. An administrator will verify it and create your channel."
        else:
            text = (
                "Complete the payment. An administrator will confirm it and create your channel. "
                "Track it with `/myslot`."
            )
        if result.message:
            text += f"\n\n{result.message}"
        return info_embed("Request submitted ⏳", text), None

    text = (
        "Your custom status doesn't contain the required phrase yet.\n\n"
        f"1. Set your custom status to include:\n`{result.message}`\n"
        "2. Press **Check again**.\n\n"
        "If you've already done this, Discord may not have shared your status with the bot yet. "
        "Wait a moment and press **Check again**, or press **Request manual review**."
    )
    return info_embed("Verification needed 🛂", text), RequirementView(bot, plan["id"])


class RequirementView(discord.ui.View):
    def __init__(self, bot, plan_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.plan_id = plan_id

    async def _plan(self):
        return await self.bot.db.one("SELECT * FROM plans WHERE id = ?", (self.plan_id,))

    @discord.ui.button(label="Check again", emoji="🔄", style=discord.ButtonStyle.primary)
    async def recheck(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        plan = await self._plan()
        embed, view = await claim_response(self.bot, interaction.user, plan)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Request manual review", emoji="🛂", style=discord.ButtonStyle.secondary)
    async def manual(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        plan = await self._plan()
        try:
            await self.bot.slots.request_manual(interaction.user, plan)
            embed = info_embed("Request submitted ⏳", "An administrator will review your request.")
        except ServiceError as exc:
            embed = error_embed("Request not possible", str(exc))
        await interaction.followup.send(embed=embed, ephemeral=True)


class PlanSelect(discord.ui.Select):
    def __init__(self, bot, plans):
        options = [
            discord.SelectOption(
                label=plan["name"][:100],
                value=str(plan["id"]),
                description=f"{plan['price_label'] or 'Free'} · {plan['duration_days']} days"[:100],
            )
            for plan in plans[:25]
        ]
        super().__init__(placeholder="Choose a plan", options=options, min_values=1, max_values=1)
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        plan = await self.bot.db.one("SELECT * FROM plans WHERE id = ? AND active = 1", (int(self.values[0]),))
        if plan is None:
            await interaction.followup.send(
                embed=error_embed("Plan unavailable", "That plan was just closed."), ephemeral=True
            )
            return
        embed, view = await claim_response(self.bot, interaction.user, plan)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class ClaimPickerView(discord.ui.View):
    def __init__(self, bot, plans):
        super().__init__(timeout=120)
        self.add_item(PlanSelect(bot, plans))


class ClaimPanelView(discord.ui.View):
    """Persistent panel. Registered at startup so buttons keep working after restarts."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim Slot", emoji="🎯", style=discord.ButtonStyle.success, custom_id="slotpanel:claim")
    async def claim(self, interaction: discord.Interaction, _button: discord.ui.Button):
        plans = await plan_rows(interaction.client)
        if not plans:
            await interaction.response.send_message(
                embed=error_embed("No plans open", "No plans are open right now."), ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=info_embed("🎯 Choose your plan", "Pick a plan to claim."),
            view=ClaimPickerView(interaction.client, plans),
            ephemeral=True,
        )

    @discord.ui.button(label="My Slot", emoji="📌", style=discord.ButtonStyle.primary, custom_id="slotpanel:my")
    async def my_slot(self, interaction: discord.Interaction, _button: discord.ui.Button):
        embed = await myslot_embed(interaction.client, interaction.user)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Rules", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="slotpanel:rules")
    async def rules(self, interaction: discord.Interaction, _button: discord.ui.Button):
        embed = await rules_embed(interaction.client)
        await interaction.response.send_message(embed=embed, ephemeral=True)
