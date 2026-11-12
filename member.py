import math
from typing import Literal

import discord
from discord.ext import commands

from ..config import COLOR_INFO
from ..services import ServiceError
from ..ui import info_embed, send_pages, success_embed
from ..views import (
    ClaimPickerView,
    claim_response,
    myslot_embed,
    plan_detail_embed,
    plan_rows,
    plans_embed,
    rules_embed,
)


def build_find_pages(term: str, hits: list) -> list[discord.Embed]:
    per_page = 5
    total_pages = max(1, math.ceil(len(hits) / per_page))
    pages = []
    for index in range(total_pages):
        chunk = hits[index * per_page:(index + 1) * per_page]
        embed = discord.Embed(
            title=f"🔎 Results for “{term}”"[:256],
            description=f"**{len(hits)}** matching active slot channel(s).",
            color=COLOR_INFO,
        )
        for channel, row, where in chunk:
            embed.add_field(
                name=f"#{channel.name}"[:256],
                value=f"{channel.mention}\nOwner: <@{row['owner_id']}>\nMatch: {where}"[:1024],
                inline=False,
            )
        embed.set_footer(text=f"Page {index + 1}/{total_pages}")
        pages.append(embed)
    return pages


class MemberCommands(commands.Cog):
    """Commands available to every member. Each works as a slash command and a dot command."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.svc = bot.slots

    @commands.hybrid_command(name="claim-slot")
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def claim_slot(self, ctx, plan: str = ""):
        """Claim a promotional slot."""
        if not plan:
            plans = await plan_rows(self.bot)
            if not plans:
                raise ServiceError("No plans are open right now.")
            return await ctx.send(
                embed=info_embed("🎯 Choose your plan", "Pick a plan below to claim a slot."),
                view=ClaimPickerView(self.bot, plans),
                ephemeral=True,
            )
        row = await self.svc.plan_by_key(plan)
        if row is None:
            raise ServiceError(f"No open plan named `{plan}`. See `/available-slots`.")
        await ctx.defer(ephemeral=True)
        embed, view = await claim_response(self.bot, ctx.author, row)
        await ctx.send(embed=embed, view=view, ephemeral=True)

    @commands.hybrid_command(name="available-slots")
    @commands.guild_only()
    async def available_slots(self, ctx):
        """Show plans and how many slots are still open."""
        await ctx.send(embed=await plans_embed(self.bot), ephemeral=True)

    @commands.hybrid_command(name="slot-rules")
    async def slot_rules(self, ctx):
        """Show the slot program rules."""
        await ctx.send(embed=await rules_embed(self.bot), ephemeral=True)

    @commands.hybrid_command(name="slot-info")
    @commands.guild_only()
    async def slot_info(self, ctx, *, plan: str = ""):
        """Show details for one plan, or for every plan."""
        if plan.strip():
            row = await self.svc.plan_by_key(plan)
            if row is None:
                raise ServiceError(f"No open plan named `{plan}`.")
            return await ctx.send(embed=plan_detail_embed(row), ephemeral=True)
        rows = await plan_rows(self.bot)
        if not rows:
            raise ServiceError("No plans are open right now.")
        await send_pages(ctx, [plan_detail_embed(row) for row in rows], ephemeral=True)

    @commands.hybrid_command(name="myslot")
    @commands.guild_only()
    async def myslot(self, ctx):
        """Show your promotional slot."""
        await ctx.send(embed=await myslot_embed(self.bot, ctx.author), ephemeral=True)

    @commands.hybrid_command(name="renew-slot")
    @commands.guild_only()
    @commands.cooldown(1, 3600, commands.BucketType.user)
    async def renew_slot(self, ctx):
        """Request a renewal for your slot."""
        slot = await self.svc.slot_for_owner(ctx.author.id, "('active','expired')")
        if slot is None:
            raise ServiceError("You have no active or expired slot to renew.")
        await self.svc.request_renewal(slot, ctx.author)
        await ctx.send(
            embed=info_embed("Renewal requested", "An administrator will review your renewal."),
            ephemeral=True,
        )

    @commands.hybrid_command(name="recover-slot")
    @commands.guild_only()
    @commands.cooldown(3, 300, commands.BucketType.user)
    async def recover_slot(self, ctx, *, key: str):
        """Recover a slot with your recovery key."""
        if ctx.interaction is None:
            # Keep the key out of channel history.
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
        await ctx.defer(ephemeral=True)
        message = await self.svc.recover(ctx.author, key)
        await ctx.send(embed=success_embed("Slot recovery", message), ephemeral=True)

    @commands.hybrid_command(name="regenerate-key")
    @commands.guild_only()
    @commands.cooldown(1, 600, commands.BucketType.user)
    async def regenerate_key(self, ctx):
        """Replace your recovery key. The old key stops working."""
        await ctx.defer(ephemeral=True)
        await self.svc.regenerate_key(ctx.author)
        await ctx.send(
            embed=success_embed(
                "New key issued",
                "Your new recovery key was sent to your DMs. The old key no longer works.",
            ),
            ephemeral=True,
        )

    @commands.hybrid_command(name="find")
    @commands.guild_only()
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def find(self, ctx, *, query: str = ""):
        """Search active slot channels for text."""
        term = query.strip() or ((await self.db.setting("gl_text", "")) or "").strip()
        if not term:
            raise ServiceError("Give me some text to search for, or ask an administrator to set the GL text.")
        await ctx.defer(ephemeral=True)
        hits = await self.svc.search_slots(term, ctx.author)
        if not hits:
            await ctx.send(
                embed=info_embed("No matches", f"No active slot channel contains `{term}`."), ephemeral=True
            )
            return
        await send_pages(ctx, build_find_pages(term, hits), ephemeral=True)

    @commands.hybrid_command(name="post-promo")
    @commands.guild_only()
    @commands.cooldown(2, 60, commands.BucketType.user)
    async def post_promo(self, ctx, mention: Literal["none", "here", "everyone"], *, text: str):
        """Publish a promotion in your slot using your mention allowance."""
        text = text.strip()
        if not text or len(text) > 3900:
            raise ServiceError("Your promotion must be between 1 and 3900 characters.")
        await ctx.defer(ephemeral=True)
        target = await self.svc.post_promo(ctx.author, mention, text)
        await ctx.send(embed=success_embed("Promotion published", f"Posted in {target}."), ephemeral=True)
