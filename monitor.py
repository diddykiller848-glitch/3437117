import logging

import discord
from discord.ext import commands, tasks

from ..config import COLOR_WARNING
from ..services import now

logger = logging.getLogger(__name__)


class Monitor(commands.Cog):
    """Background work: expiry cycle, channel monitoring, and departure tracking."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.svc = bot.slots

    async def cog_load(self) -> None:
        self.expiry_loop.start()

    async def cog_unload(self) -> None:
        self.expiry_loop.cancel()

    @tasks.loop(minutes=15)
    async def expiry_loop(self) -> None:
        try:
            await self.svc.expiry_cycle()
        except Exception:
            logger.exception("Expiry cycle failed")

    @expiry_loop.before_loop
    async def before_expiry_loop(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self.svc.handle_message(message)
        except Exception:
            logger.exception("Message monitor failed")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        # Recovery is only allowed for slots whose owner has left, so record when they left.
        await self.db.write(
            "UPDATE slots SET owner_left_at = ? WHERE owner_id = ? AND status IN ('active','banned') "
            "AND owner_left_at IS NULL",
            (now(), member.id),
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self.db.write(
            "UPDATE slots SET owner_left_at = NULL WHERE owner_id = ? AND owner_left_at IS NOT NULL",
            (member.id,),
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        # Slot removals mark the database first, so this only catches channels deleted by hand.
        _, rows = await self.db.write(
            "UPDATE slots SET status = 'deleted', note = 'channel deleted manually' "
            "WHERE channel_id = ? AND status IN ('active','banned')",
            (channel.id,),
        )
        if rows:
            await self.svc.log(
                "Channel deleted manually",
                f"A slot channel ({channel.name}) was deleted outside the bot. Its slot is marked deleted.",
                COLOR_WARNING,
            )
