import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.cogs.admin import AdminCommands
from bot.cogs.member import MemberCommands
from bot.cogs.monitor import Monitor
from bot.config import Config
from bot.db import Database
from bot.services import ServiceError, SlotService
from bot.ui import error_embed
from bot.views import ClaimPanelView

log = logging.getLogger("slotbot")


def unwrap(error: BaseException) -> BaseException:
    while getattr(error, "original", None) is not None:
        error = error.original
    return error


def error_text(error: BaseException) -> str | None:
    error = unwrap(error)
    if isinstance(error, ServiceError):
        return str(error)
    if isinstance(error, (commands.CheckFailure, app_commands.CheckFailure)):
        return str(error) or "You can't use this command."
    if isinstance(error, (commands.CommandOnCooldown, app_commands.CommandOnCooldown)):
        return f"Slow down. Try again in {error.retry_after:.0f} seconds."
    if isinstance(error, (commands.UserInputError, app_commands.TransformerError)):
        return f"Invalid input: {error}"
    return None


class SlotBot(commands.Bot):
    def __init__(self, cfg: Config):
        intents = discord.Intents.default()
        intents.members = True
        intents.presences = True
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned_or(cfg.prefix),
            intents=intents,
            help_command=None,
            case_insensitive=True,
        )
        self.cfg = cfg
        self.db = Database(cfg.database_path)
        self.slots = SlotService(self)
        self.tree.error(self.on_app_error)

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.add_cog(MemberCommands(self))
        await self.add_cog(AdminCommands(self))
        await self.add_cog(Monitor(self))
        self.add_view(ClaimPanelView())  # persistent: panel buttons survive restarts
        guild = discord.Object(id=self.cfg.guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("Synced %d slash commands to guild %s", len(synced), self.cfg.guild_id)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        text = error_text(error)
        if text is None:
            log.error("Unhandled command error", exc_info=error)
            text = "Something went wrong. The error has been logged."
        try:
            await ctx.send(embed=error_embed("Command failed", text), ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_app_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        text = error_text(error)
        if text is None:
            log.error("Unhandled slash command error", exc_info=error)
            text = "Something went wrong. The error has been logged."
        embed = error_embed("Command failed", text)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def close(self) -> None:
        await self.db.close()
        await super().close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    SlotBot(cfg).run(cfg.token, log_handler=None)


if __name__ == "__main__":
    main()
