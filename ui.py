import discord

from .config import COLOR_ERROR, COLOR_INFO, COLOR_SUCCESS


def info_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOR_INFO)


def success_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"✅ {title}", description=description, color=COLOR_SUCCESS)


def error_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"❌ {title}", description=description, color=COLOR_ERROR)


def paginate_lines(title: str, lines: list[str], per_page: int = 10, color: int = COLOR_INFO) -> list[discord.Embed]:
    chunks = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    pages = []
    for number, chunk in enumerate(chunks, start=1):
        embed = discord.Embed(title=title, description="\n".join(chunk) or "Nothing here yet.", color=color)
        embed.set_footer(text=f"Page {number}/{len(chunks)} · {len(lines)} total")
        pages.append(embed)
    return pages


class Paginator(discord.ui.View):
    def __init__(self, pages: list[discord.Embed]):
        super().__init__(timeout=180)
        self.pages = pages
        self.index = 0
        self._refresh()

    def _refresh(self) -> None:
        self.previous.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.pages) - 1
        self.counter.label = f"{self.index + 1}/{len(self.pages)}"

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._refresh()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer()

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._refresh()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)


async def send_pages(ctx, pages: list[discord.Embed], ephemeral: bool = False) -> None:
    if not pages:
        return
    view = Paginator(pages) if len(pages) > 1 else None
    await ctx.send(embed=pages[0], view=view, ephemeral=ephemeral)


class Confirm(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who ran this command can respond.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", emoji="✅", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.value = True
        self.stop()
        await interaction.response.edit_message(content="Confirmed.", view=None)

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.value = False
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)
