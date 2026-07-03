import asyncio
import colorsys
import functools
import json
import logging
import re
from io import BytesIO
from typing import Iterable

import aiohttp
import colorgram
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageOps

from utils import dataio, fuzzy

logger = logging.getLogger("BOT.Colors")

HEX_COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
REORGANIZE_EVERY = 10
PLACEMENT_LABELS = {"AboveLowest": "au-dessus du plus bas", "BelowHighest": "en-dessous du plus haut"}
PLACEMENT_CYCLE = ("AboveLowest", "BelowHighest")


def _parse_hex_color(raw: str) -> discord.Color | None:
    match = HEX_COLOR_RE.match(raw.strip())
    if not match:
        return None
    value = int(match.group(1), 16)
    return discord.Color(value or 1)  # 0 (noir pur) est remplacé par 1 pour éviter la transparence


def _role_hue(role: discord.Role) -> float:
    hex_color = role.name.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hsv(r, g, b)[0]


# ---------------------------------------------------------------------------
# Rendu image (fonctions synchrones pures, exécutées dans un threadpool)
# ---------------------------------------------------------------------------


def _draw_palette_sync(raw: bytes, n_colors: int, font: ImageFont.FreeTypeFont) -> Image.Image:
    image = Image.open(BytesIO(raw)).convert("RGB")
    colors = colorgram.extract(image.resize((100, 100)), n_colors)

    image = ImageOps.contain(image, (500, 500), method=Image.Resampling.LANCZOS)
    iw, ih = image.size
    palette = Image.new("RGB", (iw + 100, ih), color="white")

    max_colors = ih // 30
    colors = colors[:max_colors]
    block_height = ih // len(colors)

    draw = ImageDraw.Draw(palette)
    for i, color in enumerate(colors):
        y0 = i * block_height
        y1 = ih if i == len(colors) - 1 else y0 + block_height
        palette.paste(color.rgb, (iw, y0, iw + 100, y1))
        hex_color = f"#{color.rgb.r:02x}{color.rgb.g:02x}{color.rgb.b:02x}".upper()
        text_color = "white" if sum(color.rgb) < 384 else "black"
        draw.text((iw + 10, y0 + 10), hex_color, font=font, fill=text_color)

    palette.paste(image, (0, 0))
    return palette


def _draw_avatar_previews_sync(
    raw: bytes, display_name: str, limit: int, name_font: ImageFont.FreeTypeFont, content_font: ImageFont.FreeTypeFont
) -> list[tuple[Image.Image, str]]:
    avatar = Image.open(BytesIO(raw)).convert("RGBA")
    colors = colorgram.extract(avatar, limit)

    mask = Image.new("L", avatar.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0) + avatar.size, fill=255)
    avatar.putalpha(mask)
    avatar = avatar.resize((46, 46), Image.Resampling.LANCZOS)

    previews: list[tuple[Image.Image, str]] = []
    for color in colors:
        hex_color = f"#{color.rgb.r:02x}{color.rgb.g:02x}{color.rgb.b:02x}"
        if hex_color == "#000000":
            continue

        rows = []
        for bg_color in ((0, 0, 0), (54, 57, 63), (255, 255, 255)):
            row = Image.new("RGBA", (420, 75), color=bg_color)
            row.paste(avatar, (13, 13), avatar)
            draw = ImageDraw.Draw(row)
            draw.text((76, 10), display_name, font=name_font, fill=color.rgb)
            text_color = (255, 255, 255) if bg_color != (255, 255, 255) else (0, 0, 0)
            draw.text((76, 34), "Prévisualisation de l'affichage du rôle", font=content_font, fill=text_color)
            rows.append(row)

        full = Image.new("RGBA", (420, 75 * len(rows)))
        for i, row in enumerate(rows):
            full.paste(row, (0, 75 * i))
        previews.append((full, hex_color.upper()))

    return previews


# ---------------------------------------------------------------------------
# UI — Prévisualisation des couleurs d'avatar
# ---------------------------------------------------------------------------


class AvatarPreviewView(discord.ui.View):
    def __init__(self, owner: discord.abc.User, previews: list[tuple[Image.Image, str]], *, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.owner = owner
        self.previews = previews
        self.page = 0
        self.result: str | None = None
        self._interaction: discord.Interaction | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.owner:
            await interaction.response.send_message(
                "Seul l'auteur de la commande peut utiliser ce menu.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        self.stop()
        if self._interaction:
            try:
                await self._interaction.delete_original_response()
            except discord.HTTPException:
                pass

    def _embed(self) -> discord.Embed:
        hex_color = self.previews[self.page][1]
        em = discord.Embed(title=f"Prévisualisation · {hex_color}", color=discord.Color(int(hex_color[1:], 16)))
        em.set_image(url="attachment://preview.png")
        em.set_footer(text=f"Couleur {self.page + 1}/{len(self.previews)}")
        return em

    def _file(self) -> discord.File:
        buf = BytesIO()
        self.previews[self.page][0].save(buf, format="PNG")
        buf.seek(0)
        return discord.File(buf, filename="preview.png")

    async def start(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        await interaction.followup.send(embed=self._embed(), file=self._file(), view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.grey)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = (self.page - 1) % len(self.previews)
        await interaction.response.edit_message(embed=self._embed(), attachments=[self._file()])

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.defer()
        await interaction.delete_original_response()

    @discord.ui.button(label="Appliquer", style=discord.ButtonStyle.green)
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.result = self.previews[self.page][1]
        self.stop()
        await interaction.response.edit_message(view=None)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.grey)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = (self.page + 1) % len(self.previews)
        await interaction.response.edit_message(embed=self._embed(), attachments=[self._file()])


# ---------------------------------------------------------------------------
# UI — Panneau de configuration unique (Components V2)
# ---------------------------------------------------------------------------


class ToggleEnabledButton(discord.ui.Button):
    def __init__(self, view_ref: "ColorsConfigView"):
        super().__init__(
            label="Désactiver" if view_ref.enabled else "Activer",
            style=discord.ButtonStyle.red if view_ref.enabled else discord.ButtonStyle.green,
        )
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        cog, guild = self._view_ref.cog, self._view_ref.guild
        await cog.set_enabled(guild, not self._view_ref.enabled)
        await self._view_ref.refresh()


class CyclePlacementButton(discord.ui.Button):
    def __init__(self, view_ref: "ColorsConfigView"):
        super().__init__(label=PLACEMENT_LABELS[view_ref.placement].capitalize(), style=discord.ButtonStyle.secondary)
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        cog, guild = self._view_ref.cog, self._view_ref.guild
        idx = PLACEMENT_CYCLE.index(self._view_ref.placement)
        new_placement = PLACEMENT_CYCLE[(idx + 1) % len(PLACEMENT_CYCLE)]
        await cog.data.get(guild).set_dict_value("settings", "PlaceNewColorRole", new_placement)
        await self._view_ref.refresh()


class CleanupButton(discord.ui.Button):
    def __init__(self, view_ref: "ColorsConfigView"):
        super().__init__(label="Nettoyer", style=discord.ButtonStyle.secondary)
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        cog, guild = self._view_ref.cog, self._view_ref.guild
        color_roles = list(cog.fetch_all_color_roles(guild))
        if not color_roles:
            await interaction.followup.send("**Info ·** Aucun rôle de couleur à nettoyer.", ephemeral=True)
            return
        if guild.me.top_role.position <= max(r.position for r in color_roles):
            await interaction.followup.send(
                "**Erreur ·** Mon rôle doit être au-dessus des rôles de couleur pour pouvoir les gérer.",
                ephemeral=True,
            )
            return
        try:
            await cog.clear_color_roles(guild)
            await cog.reorganize_color_roles(guild)
        except Exception as e:
            logger.exception(e)
            await interaction.followup.send("**Erreur ·** Impossible de nettoyer les rôles de couleur.", ephemeral=True)
            return
        await self._view_ref.refresh()


class ColorsConfigView(discord.ui.LayoutView):
    """Panneau de configuration unique des rôles de couleur."""

    def __init__(
        self,
        cog: "Colors",
        guild: discord.Guild,
        *,
        enabled: bool,
        placement: str,
        role_count: int,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.enabled = enabled
        self.placement = placement
        self.role_count = role_count
        self._interaction: discord.Interaction | None = None
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "**Action impossible ·** La permission `Gérer les rôles` est requise.", ephemeral=True
            )
            return False
        return True

    def _build(self) -> None:
        self.clear_items()
        container = discord.ui.Container()

        container.add_item(discord.ui.TextDisplay(f"## Configuration des rôles de couleur — {self.guild.name}"))
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.Section(
                f"**État**\n{'Activé' if self.enabled else 'Désactivé'}",
                accessory=ToggleEnabledButton(self),
            )
        )
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.Section(
                f"**Placement des nouveaux rôles**\nPlacés {PLACEMENT_LABELS[self.placement]} dans la liste.",
                accessory=CyclePlacementButton(self),
            )
        )
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.Section(
                "**Nettoyage**\nSupprime les rôles de couleur inutilisés et réorganise les autres par teinte.",
                accessory=CleanupButton(self),
            )
        )
        container.add_item(discord.ui.Separator())

        container.add_item(discord.ui.TextDisplay(f"-# {self.role_count} rôle(s) de couleur sur ce serveur."))

        self.add_item(container)

    async def _reload(self) -> None:
        self.enabled = await self.cog.is_enabled(self.guild)
        self.placement = await self.cog.get_role_placing(self.guild)
        self.role_count = len(list(self.cog.fetch_all_color_roles(self.guild)))

    async def refresh(self) -> None:
        await self._reload()
        self._build()
        if self._interaction:
            await self._interaction.edit_original_response(view=self)

    async def start(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        await interaction.response.send_message(view=self, ephemeral=True)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class Colors(commands.Cog):
    """Système de distribution de rôles de couleur."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_instance(self)

        settings = dataio.DictTableBuilder(
            "settings",
            {"Enabled": False, "PlaceNewColorRole": "AboveLowest"},
        )
        self.data.link(discord.Guild, settings)

        self._fonts = {
            "text": ImageFont.truetype(str(dataio.COMMON_RESOURCES_PATH / "fonts" / "gg_sans_light.ttf"), 18),
            "name": ImageFont.truetype(str(dataio.COMMON_RESOURCES_PATH / "fonts" / "gg_sans.ttf"), 18),
        }
        self._color_names: dict[str, str] = self._load_color_names()
        self._reorg_counts: dict[int, int] = {}

    async def cog_unload(self) -> None:
        await self.data.close_all()

    def _load_color_names(self) -> dict[str, str]:
        path = self.data.assets_path / "color_names_fr.json"
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {entry["name"]: entry["hex"] for entry in raw}

    # ------------------------------------------------------------------
    # Rôles de couleur
    # ------------------------------------------------------------------

    def fetch_all_color_roles(self, guild: discord.Guild) -> Iterable[discord.Role]:
        return filter(lambda r: r.name.startswith("#") and len(r.name) == 7, guild.roles)

    def fetch_color_role(self, guild: discord.Guild, color: discord.Color) -> discord.Role | None:
        return discord.utils.get(self.fetch_all_color_roles(guild), color=color)

    def get_current_member_color_role(self, member: discord.Member) -> discord.Role | None:
        return discord.utils.find(lambda r: r in member.roles, self.fetch_all_color_roles(member.guild))

    def get_highest_color_role(self, guild: discord.Guild) -> discord.Role | None:
        return max(self.fetch_all_color_roles(guild), key=lambda r: r.position, default=None)

    def get_lowest_color_role(self, guild: discord.Guild) -> discord.Role | None:
        return min(self.fetch_all_color_roles(guild), key=lambda r: r.position, default=None)

    # ------------------------------------------------------------------
    # Paramètres
    # ------------------------------------------------------------------

    async def is_enabled(self, guild: discord.Guild) -> bool:
        return await self.data.get(guild).get_dict_value("settings", "Enabled", cast=bool)

    async def set_enabled(self, guild: discord.Guild, value: bool) -> None:
        await self.data.get(guild).set_dict_value("settings", "Enabled", value)

    async def get_role_placing(self, guild: discord.Guild) -> str:
        return await self.data.get(guild).get_dict_value("settings", "PlaceNewColorRole")

    # ------------------------------------------------------------------
    # Garde commune des commandes utilisateur
    # ------------------------------------------------------------------

    async def _guard(self, interaction: discord.Interaction) -> discord.Guild | None:
        """Vérifie les pré-conditions communes (serveur, système activé, permissions du bot)."""
        if not isinstance(interaction.guild, discord.Guild) or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )
            return None
        guild = interaction.guild
        if not await self.is_enabled(guild):
            await interaction.response.send_message(
                "**Erreur ·** Le système de rôles de couleur n'est pas activé sur ce serveur.", ephemeral=True
            )
            return None
        if not guild.me.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "**Erreur ·** Je n'ai pas la permission de gérer les rôles.", ephemeral=True
            )
            return None
        return guild

    # ------------------------------------------------------------------
    # Création et recyclage de rôles
    # ------------------------------------------------------------------

    async def give_color_role(self, member: discord.Member, color: discord.Color) -> discord.Role:
        guild = member.guild

        role = self.fetch_color_role(guild, color)
        if role:
            await member.add_roles(role, reason="Attribution de couleur")
            return role

        # Si le membre est seul propriétaire de son rôle actuel, on le recycle directement
        role = self.get_current_member_color_role(member)
        if role and len(role.members) == 1:
            await role.edit(name=f"#{color.value:06X}", color=color, reason="Recyclage de couleur")
            return role

        # Sinon, on recycle un rôle de couleur existant sans membre
        role = discord.utils.find(lambda r: not r.members, self.fetch_all_color_roles(guild))
        if role:
            await role.edit(name=f"#{color.value:06X}", color=color, reason="Recyclage de couleur")
            await member.add_roles(role, reason="Attribution de couleur")
            return role

        role = await guild.create_role(name=f"#{color.value:06X}", color=color, reason="Création d'un rôle de couleur")
        await member.add_roles(role, reason="Attribution de couleur")

        placement = await self.get_role_placing(guild)
        if placement == "AboveLowest":
            lowest = self.get_lowest_color_role(guild)
            if lowest:
                await role.edit(position=lowest.position + 1)
        elif placement == "BelowHighest":
            highest = self.get_highest_color_role(guild)
            if highest:
                await role.edit(position=highest.position - 1)

        self._reorg_counts[guild.id] = self._reorg_counts.get(guild.id, 0) + 1
        if self._reorg_counts[guild.id] >= REORGANIZE_EVERY:
            await self.clear_color_roles(guild)
            await self.reorganize_color_roles(guild)
            self._reorg_counts[guild.id] = 0

        return role

    async def remove_color_role(self, member: discord.Member) -> None:
        for role in [r for r in self.fetch_all_color_roles(member.guild) if r in member.roles]:
            await member.remove_roles(role, reason="Retrait de couleur")
            if not role.members:
                await role.delete(reason="Suppression de rôle de couleur inutilisé")

    async def reorganize_color_roles(self, guild: discord.Guild) -> None:
        color_roles = sorted(self.fetch_all_color_roles(guild), key=_role_hue)
        placement = await self.get_role_placing(guild)
        if placement == "AboveLowest":
            lowest = self.get_lowest_color_role(guild)
            if lowest:
                await guild.edit_role_positions({r: lowest.position + i + 1 for i, r in enumerate(color_roles)})
        elif placement == "BelowHighest":
            highest = self.get_highest_color_role(guild)
            if highest:
                await guild.edit_role_positions({r: highest.position - i - 1 for i, r in enumerate(color_roles)})

    async def clear_color_roles(self, guild: discord.Guild) -> None:
        for role in [r for r in self.fetch_all_color_roles(guild) if not r.members]:
            await role.delete(reason="Suppression de rôle de couleur inutilisé")

    # ------------------------------------------------------------------
    # Génération d'images
    # ------------------------------------------------------------------

    async def draw_image_palette(self, raw: bytes, n_colors: int) -> Image.Image:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, functools.partial(_draw_palette_sync, raw, n_colors, self._fonts["text"])
        )

    async def draw_discord_emulation(self, member: discord.Member, *, limit: int = 3) -> list[tuple[Image.Image, str]]:
        raw = await member.display_avatar.with_size(128).with_format("png").read()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(
                _draw_avatar_previews_sync, raw, member.display_name, limit, self._fonts["name"], self._fonts["text"]
            ),
        )

    # ------------------------------------------------------------------
    # Événements
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if not member.guild.me.guild_permissions.manage_roles:
            return
        if not self.get_current_member_color_role(member):
            return
        try:
            await self.remove_color_role(member)
        except discord.HTTPException:
            pass  # Sera récupéré par le nettoyage automatique

    # ==================================================================
    # COMMANDES
    # ==================================================================

    @app_commands.command(name="palette")
    @app_commands.rename(n_colors="nombre_couleurs", url="lien", image_file="image", user="utilisateur")
    async def create_palette(
        self,
        interaction: discord.Interaction,
        n_colors: app_commands.Range[int, 3, 10] = 5,
        url: str | None = None,
        image_file: discord.Attachment | None = None,
        user: discord.Member | None = None,
    ) -> None:
        """Génère une palette de couleurs à partir d'une image, d'un avatar ou du dernier visuel du salon.

        :param n_colors: Nombre de couleurs à extraire
        :param url: URL de l'image à utiliser
        :param image_file: Fichier image à utiliser
        :param user: Utilisateur dont l'avatar sera utilisé
        """
        await interaction.response.defer()

        raw: bytes | None = None
        if image_file:
            raw = await image_file.read()
        elif url:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        raw = await resp.read()
                    else:
                        return await interaction.followup.send(
                            "**Erreur ·** Impossible de télécharger l'image depuis l'URL.", ephemeral=True
                        )
        elif user:
            raw = await user.display_avatar.read()
        elif isinstance(interaction.channel, (discord.TextChannel, discord.Thread, discord.DMChannel, discord.GroupChannel)):
            async for message in interaction.channel.history(limit=15):
                if message.attachments:
                    raw = await message.attachments[0].read()
                    break

        if not raw:
            return await interaction.followup.send(
                "**Erreur ·** Aucune image fournie ni trouvée dans les 15 derniers messages.", ephemeral=True
            )

        try:
            palette = await self.draw_image_palette(raw, n_colors)
        except Exception as e:
            logger.exception(e)
            return await interaction.followup.send("**Erreur ·** Impossible de générer la palette.", ephemeral=True)

        buf = BytesIO()
        palette.save(buf, format="PNG")
        buf.seek(0)
        await interaction.followup.send(
            file=discord.File(buf, filename="palette.png", description="Palette de couleurs extraite")
        )

    color_group = app_commands.Group(name="color", description="Gestion de la couleur de votre pseudo", guild_only=True)

    @color_group.command(name="set")
    @app_commands.rename(color="couleur")
    async def set_color(self, interaction: discord.Interaction, color: str) -> None:
        """Obtenir une couleur de pseudo personnalisée.

        :param color: Couleur en hexadécimal (#RRGGBB), ou nom via l'autocomplétion
        """
        guild = await self._guard(interaction)
        if not guild:
            return

        parsed = _parse_hex_color(color)
        if not parsed:
            return await interaction.response.send_message(
                "**Erreur ·** La couleur doit être en hexadécimal (#RRGGBB). Utilisez l'autocomplétion pour un nom.",
                ephemeral=True,
            )

        try:
            role = await self.give_color_role(interaction.user, parsed)  # type: ignore[arg-type]
        except discord.HTTPException as e:
            logger.exception(e)
            return await interaction.response.send_message(
                "**Erreur ·** Impossible d'attribuer le rôle de couleur. Vérifiez mes permissions.", ephemeral=True
            )

        await interaction.response.send_message(
            f"**Couleur définie ·** Votre couleur de pseudo est désormais {role.mention}.", ephemeral=True
        )

    @color_group.command(name="remove")
    async def remove_color(self, interaction: discord.Interaction) -> None:
        """Retirer vos rôles de couleur gérés par le bot."""
        guild = await self._guard(interaction)
        if not guild:
            return

        if not self.get_current_member_color_role(interaction.user):  # type: ignore[arg-type]
            return await interaction.response.send_message(
                "**Erreur ·** Vous n'avez pas de rôle de couleur à retirer.", ephemeral=True
            )

        try:
            await self.remove_color_role(interaction.user)  # type: ignore[arg-type]
        except discord.HTTPException as e:
            logger.exception(e)
            return await interaction.response.send_message(
                "**Erreur ·** Impossible de retirer le(s) rôle(s). Vérifiez mes permissions.", ephemeral=True
            )

        await interaction.response.send_message("**Couleur retirée ·** Vos rôles de couleur ont été retirés.", ephemeral=True)

    @color_group.command(name="avatar")
    @app_commands.rename(user="utilisateur")
    async def avatar_color(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        """Choisir un rôle de couleur parmi les couleurs dominantes d'un avatar.

        :param user: Utilisateur dont l'avatar sera utilisé (par défaut, vous)
        """
        guild = await self._guard(interaction)
        if not guild:
            return
        target = user or interaction.user

        await interaction.response.defer(ephemeral=True)
        try:
            previews = await self.draw_discord_emulation(target)  # type: ignore[arg-type]
        except Exception as e:
            logger.exception(e)
            return await interaction.followup.send("**Erreur ·** Impossible de générer les prévisualisations.", ephemeral=True)
        if not previews:
            return await interaction.followup.send("**Erreur ·** Aucune couleur exploitable trouvée sur cet avatar.", ephemeral=True)

        view = AvatarPreviewView(interaction.user, previews)
        await view.start(interaction)
        await view.wait()
        if not view.result:
            return await interaction.followup.send("**Annulé ·** Aucune couleur sélectionnée.", ephemeral=True)

        try:
            role = await self.give_color_role(target, discord.Color(int(view.result.lstrip("#"), 16)))  # type: ignore[arg-type]
        except discord.HTTPException as e:
            logger.exception(e)
            return await interaction.followup.send("**Erreur ·** Impossible d'attribuer le rôle de couleur.", ephemeral=True)

        await interaction.followup.send(f"**Couleur définie ·** Le pseudo utilise désormais {role.mention}.", ephemeral=True)

    @app_commands.command(name="colorconfig")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    async def config_color(self, interaction: discord.Interaction) -> None:
        """Ouvre le panneau de configuration des rôles de couleur."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )

        view = ColorsConfigView(
            self,
            guild,
            enabled=await self.is_enabled(guild),
            placement=await self.get_role_placing(guild),
            role_count=len(list(self.fetch_all_color_roles(guild))),
        )
        await view.start(interaction)

    # ------------------------------------------------------------------
    # Autocomplétion
    # ------------------------------------------------------------------

    @set_color.autocomplete("color")
    async def autocomplete_color(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        matches = fuzzy.finder(current, self._color_names.keys())
        if not matches:
            if HEX_COLOR_RE.match(current):
                return [app_commands.Choice(name=f"Couleur personnalisée (#{current.lstrip('#').upper()})", value=current)]
            return [app_commands.Choice(name="Aucune couleur trouvée", value="")]
        return [app_commands.Choice(name=f"{name} (#{self._color_names[name]})", value=self._color_names[name]) for name in matches[:10]]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Colors(bot))
