import asyncio
import copy
import functools
import logging
import re
import textwrap
from io import BytesIO

import aiohttp
import colorgram
import cv2
import discord
import numpy as np
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageChops, ImageDraw, ImageFont

from utils import dataio, pretty

logger = logging.getLogger("BOT.Quotes")

QUOTE_SIZE = (512, 512)
MULTI_WIDTH = 1000
SINGLE_BG_BLUR = 10
MULTI_BG_BLUR = 115
FLUSH_AFTER = 20
INSPIROBOT_URL = "https://inspirobot.me/api?generate=true"

# ---------------------------------------------------------------------------
# Utilitaires image
#
# Ces fonctions reproduisent fidèlement le rendu original des citations (même
# formules, mêmes proportions, mêmes couleurs) afin que les images générées
# soient strictement identiques visuellement. Seule l'implémentation a été
# optimisée (vectorisation numpy du dégradé, délégation du travail lourd à un
# threadpool, mise en cache correcte des fonds/icônes/polices).
# ---------------------------------------------------------------------------


def _normalize_text(text: str) -> str:
    text = re.sub(r"<a?:(\w+):\d+>", r":\1:", text)
    text = re.sub(r"(\*|_|`|~|\\)", r"", text)
    return text


def _add_gradient_dir(
    image: Image.Image,
    gradient_magnitude: float = 1.0,
    color: tuple[int, int, int] = (0, 0, 0),
    direction: str = "bottom_to_top",
) -> Image.Image:
    """Superpose un dégradé de transparence sur l'image (identique pixel pour pixel
    à une version dessinée ligne par ligne, mais calculée par vectorisation numpy)."""
    w, h = image.size
    end_alpha = int(gradient_magnitude * 255)
    r, g, b = color

    if direction in ("top_to_bottom", "bottom_to_top"):
        ys = np.arange(h)
        if direction == "top_to_bottom":
            alphas = (ys / h * end_alpha).astype(np.uint8)
        else:
            alphas = ((h - ys) / h * end_alpha).astype(np.uint8)
        alpha_channel = np.repeat(alphas[:, None], w, axis=1)
    elif direction in ("left_to_right", "right_to_left"):
        xs = np.arange(w)
        if direction == "left_to_right":
            alphas = (xs / w * end_alpha).astype(np.uint8)
        else:
            alphas = ((w - xs) / w * end_alpha).astype(np.uint8)
        alpha_channel = np.repeat(alphas[None, :], h, axis=0)
    else:
        raise ValueError(f"Direction de dégradé invalide : {direction}")

    gradient_arr = np.zeros((h, w, 4), dtype=np.uint8)
    gradient_arr[..., 0] = r
    gradient_arr[..., 1] = g
    gradient_arr[..., 2] = b
    gradient_arr[..., 3] = alpha_channel
    gradient = Image.fromarray(gradient_arr, "RGBA")
    return Image.alpha_composite(image.convert("RGBA"), gradient)


def _round_corners(
    img: Image.Image,
    rad: int,
    *,
    top_left: bool = True,
    top_right: bool = True,
    bottom_left: bool = True,
    bottom_right: bool = True,
) -> Image.Image:
    circle = Image.new("L", (rad * 2, rad * 2), 0)
    draw = ImageDraw.Draw(circle)
    draw.ellipse((0, 0, rad * 2, rad * 2), fill=255)
    w, h = img.size
    alpha = img.split()[3] if img.mode == "RGBA" else None
    mask = Image.new("L", img.size, 255)
    if top_left:
        mask.paste(circle.crop((0, 0, rad, rad)), (0, 0))
    if top_right:
        mask.paste(circle.crop((rad, 0, rad * 2, rad)), (w - rad, 0))
    if bottom_left:
        mask.paste(circle.crop((0, rad, rad, rad * 2)), (0, h - rad))
    if bottom_right:
        mask.paste(circle.crop((rad, rad, rad * 2, rad * 2)), (w - rad, h - rad))
    if alpha:
        img.putalpha(ImageChops.multiply(alpha, mask))
    else:
        img.putalpha(mask)
    return img


# ---------------------------------------------------------------------------
# UI — Vue de sélection / prévisualisation (Components V2)
# ---------------------------------------------------------------------------


class MessageSelect(discord.ui.Select):
    """Menu de sélection des messages à inclure dans la citation."""

    def __init__(self, view_ref: "QuoteSelectionView", options: list[discord.SelectOption]):
        super().__init__(
            placeholder="Ajouter / retirer des messages…",
            min_values=1,
            max_values=min(len(options), 10),
            options=options,
        )
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        selected_ids = {int(v) for v in self.values}
        self._view_ref.selected_messages = [
            m for m in self._view_ref.potential_messages if m.id in selected_ids
        ]
        await self._view_ref.refresh(interaction)


class GenerateButton(discord.ui.Button):
    def __init__(self, view_ref: "QuoteSelectionView"):
        super().__init__(label="Générer l'image", style=discord.ButtonStyle.green)
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._view_ref.on_generate(interaction)


class CancelButton(discord.ui.Button):
    def __init__(self, view_ref: "QuoteSelectionView"):
        super().__init__(label="Annuler", style=discord.ButtonStyle.red)
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._view_ref.on_cancel(interaction)


class GeneratingView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay("-# Génération de l'image en cours…"))
        self.add_item(container)


class QuoteSelectionView(discord.ui.LayoutView):
    """Vue de sélection / prévisualisation avant génération de la citation."""

    def __init__(self, cog: "Quotes", initial_message: discord.Message, *, timeout: float = 30):
        super().__init__(timeout=timeout)
        self._cog = cog
        self.initial_message = initial_message
        self.potential_messages: list[discord.Message] = []
        self.selected_messages: list[discord.Message] = [initial_message]
        self._interaction: discord.Interaction | None = None

        self._container = discord.ui.Container()
        self.add_item(self._container)

    # ------------------------------------------------------------------
    # Checks / lifecycle
    # ------------------------------------------------------------------

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self._interaction:
            return False
        if interaction.user != self._interaction.user:
            await interaction.response.send_message(
                "**Action impossible ·** Seul l'auteur de la commande peut utiliser ce menu.",
                ephemeral=True,
                delete_after=10,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if self._interaction:
            try:
                img_file = await self._cog.generate_quote(self.selected_messages)
                link_view = discord.ui.View()
                link_view.add_item(
                    discord.ui.Button(
                        label="Message d'origine",
                        url=self.initial_message.jump_url,
                        style=discord.ButtonStyle.link,
                    )
                )
                await self._interaction.followup.send(file=img_file, view=link_view)
            except Exception:
                pass
            try:
                await self._interaction.delete_original_response()
            except discord.HTTPException:
                pass
        self.stop()

    # ------------------------------------------------------------------
    # Construction du contenu
    # ------------------------------------------------------------------

    def _build_select_options(self) -> list[discord.SelectOption]:
        selected_ids = {m.id for m in self.selected_messages}
        return [
            discord.SelectOption(
                label=pretty.shorten_text(m.clean_content, 95) or "[message sans texte]",
                value=str(m.id),
                description=f"{m.author.display_name} · {m.created_at.strftime('%H:%M %d/%m/%y')}",
                default=m.id in selected_ids,
            )
            for m in self.potential_messages
        ]

    def _rebuild_container(self) -> None:
        self._container.clear_items()
        msgs = sorted(self.selected_messages, key=lambda m: m.created_at)

        groups: list[list[discord.Message]] = []
        for msg in msgs:
            if groups and groups[-1][0].author == msg.author:
                groups[-1].append(msg)
            else:
                groups.append([msg])

        self._container.add_item(
            discord.ui.TextDisplay(f"### Prévisualisation · {len(msgs)} message(s) sélectionné(s)")
        )
        self._container.add_item(discord.ui.Separator())

        max_len = 800 if len(groups) == 1 else 300
        for group in groups:
            author = group[0].author
            content = pretty.shorten_text("\n".join(m.clean_content for m in group), max_len)
            self._container.add_item(
                discord.ui.Section(
                    f"**{author.display_name}** · {group[0].created_at.strftime('%d/%m/%y %H:%M')}\n"
                    f"{content or '*[message sans texte]*'}",
                    accessory=discord.ui.Thumbnail(author.display_avatar.url),
                )
            )

        self._container.add_item(discord.ui.Separator())

        if self.potential_messages:
            select_row = discord.ui.ActionRow()
            select_row.add_item(MessageSelect(self, self._build_select_options()))
            self._container.add_item(select_row)

        button_row = discord.ui.ActionRow()
        button_row.add_item(GenerateButton(self))
        button_row.add_item(CancelButton(self))
        self._container.add_item(button_row)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def start(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        potential = await self._cog.fetch_context_messages(self.initial_message)
        if len(potential) > 1:
            self.potential_messages = potential
        self._rebuild_container()
        await interaction.followup.send(view=self, ephemeral=True)
        self._interaction = interaction

    async def refresh(self, interaction: discord.Interaction) -> None:
        self._rebuild_container()
        await interaction.edit_original_response(view=self)

    async def on_generate(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=GeneratingView())
        self.stop()
        link_view = discord.ui.View()
        link_view.add_item(
            discord.ui.Button(
                label="Message d'origine", url=self.initial_message.jump_url, style=discord.ButtonStyle.link
            )
        )
        try:
            img_file = await self._cog.generate_quote(self.selected_messages)
            await interaction.followup.send(file=img_file, view=link_view)
            await interaction.delete_original_response()
        except Exception as e:
            logger.exception(e)
            await interaction.followup.send(
                "**Erreur ·** Impossible de générer l'image de citation.", ephemeral=True
            )

    async def on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.defer()
        if self._interaction:
            try:
                await self._interaction.delete_original_response()
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class Quotes(commands.Cog):
    """Créateur de citations et de compilations de messages."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_instance(self)

        self.__assets: dict = {}
        self.__bg_cache: dict[str, Image.Image] = {}
        self.__flush_countdown = FLUSH_AFTER

        self.ctx_create_quote = app_commands.ContextMenu(
            name="Créer une citation",
            callback=self.create_quote_callback,
        )
        self.bot.tree.add_command(self.ctx_create_quote)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_create_quote.name, type=self.ctx_create_quote.type)
        await self.data.close_all()

    # ------------------------------------------------------------------
    # Ressources (polices, icônes, fonds d'avatar)
    # ------------------------------------------------------------------

    def _font(self, name: str, size: int) -> ImageFont.FreeTypeFont:
        uid = f"font_{name}_{size}"
        if uid not in self.__assets:
            path = dataio.COMMON_RESOURCES_PATH / "fonts" / f"{name}.ttf"
            self.__assets[uid] = ImageFont.truetype(str(path), size)
        return self.__assets[uid]

    def _icon(self, name: str, size: int) -> Image.Image:
        uid = f"icon_{name}_{size}"
        if uid not in self.__assets:
            path = dataio.COMMON_RESOURCES_PATH / "images" / f"{name}.png"
            icon = Image.open(path)
            icon = icon.resize((size, size))
            self.__assets[uid] = icon
        return self.__assets[uid]

    @staticmethod
    def _blur_raw_sync(raw: bytes, blur: int) -> Image.Image:
        """Partie synchrone lourde (PIL + cv2) — exécutée dans un thread."""
        img = Image.open(BytesIO(raw)).convert("RGBA").resize((512, 512))
        ksize = blur if blur % 2 == 1 else (blur - 1 if blur > 1 else 1)
        blurred = cv2.GaussianBlur(np.array(img), (ksize, ksize), 0)
        return Image.fromarray(blurred)

    async def _get_blurred_bg(self, member: discord.Member, blur: int) -> Image.Image:
        """Crée (ou récupère du cache) le fond flouté à partir de l'avatar d'un membre."""
        key = f"{member.guild.id}-{member.id}-{blur}"
        if key in self.__bg_cache:
            return self.__bg_cache[key]
        raw = await member.display_avatar.with_size(512).read()
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, functools.partial(self._blur_raw_sync, raw, blur))
        self.__bg_cache[key] = result
        return result

    def _flush_bg_cache(self) -> None:
        self.__bg_cache.clear()

    # ------------------------------------------------------------------
    # Génération d'image — mono-auteur
    # ------------------------------------------------------------------

    def _render_single_sync(
        self,
        bg: Image.Image,
        text: str,
        author_name: str,
        channel_name: str,
        date: str,
        *,
        size: tuple[int, int] = QUOTE_SIZE,
    ) -> Image.Image:
        """Rendu PIL synchrone de l'image mono-auteur — exécuté dans un thread pool."""
        text = text.upper()
        w, h = size
        box_w = int(w * 0.92)

        image = copy.copy(bg)
        bg_color = colorgram.extract(bg.resize((30, 30)), 1)[0].rgb
        luminosity = (0.2126 * bg_color[0] + 0.7152 * bg_color[1] + 0.0722 * bg_color[2]) / 255

        text_size = int(h * 0.08)
        text_font = self._font("NotoBebasNeue", text_size)

        draw = ImageDraw.Draw(image)
        text_color = (255, 255, 255) if luminosity < 0.5 else (0, 0, 0)

        # Texte principal --------
        max_lines = len(text) // 60 + 2 if len(text) > 200 else 4
        wrap_width = max(1, int(box_w / (text_font.getlength("A") * 0.85)))
        lines = textwrap.fill(text, width=wrap_width, max_lines=max_lines, placeholder="§")
        while lines.endswith("§") and text_size > 6:
            text_size -= 2
            text_font = self._font("NotoBebasNeue", text_size)
            wrap_width = max(1, int(box_w / (text_font.getlength("A") * 0.85)))
            lines = textwrap.fill(text, width=wrap_width, max_lines=max_lines, placeholder="§")
        draw.multiline_text(
            (w / 2, h * 0.835), lines, font=text_font, spacing=1, align="center", fill=text_color, anchor="md"
        )

        # Icone et lignes ---------
        icon_name = "quotemark_white" if luminosity < 0.5 else "quotemark_black"
        icon_image = self._icon(icon_name, int(w * 0.06))
        icon_left = w / 2 - icon_image.width / 2
        image.paste(icon_image, (int(icon_left), int(h * 0.85 - icon_image.height / 2)), icon_image)

        author_font = self._font("NotoBebasNeue", int(h * 0.06))
        draw.text((w / 2, h * 0.95), author_name, font=author_font, fill=text_color, anchor="md", align="center")

        draw.line((icon_left - w * 0.25, h * 0.85, icon_left - w * 0.02, h * 0.85), fill=text_color, width=1)
        draw.line(
            (icon_left + icon_image.width + w * 0.02, h * 0.85, icon_left + icon_image.width + w * 0.25, h * 0.85),
            fill=text_color,
            width=1,
        )

        # Date -------------------
        date_font = self._font("NotoBebasNeue", int(h * 0.04))
        date_text = f"#{channel_name} • {date}"
        draw.text((w / 2, h * 0.9875), date_text, font=date_font, fill=text_color, anchor="md", align="center")
        return image

    async def _build_single_image(self, messages: list[discord.Message]) -> Image.Image:
        msgs = sorted(messages, key=lambda m: m.created_at)
        base_message = msgs[0]
        if not isinstance(base_message.author, discord.Member):
            raise ValueError("Le message de base doit être envoyé par un membre du serveur.")

        bg = await self._get_blurred_bg(base_message.author, SINGLE_BG_BLUR)
        bg = _add_gradient_dir(bg, 0.7, direction="top_to_bottom")

        full_content = pretty.shorten_text(" ".join(_normalize_text(m.content) for m in msgs), 800)
        author = base_message.author
        author_name = f"@{author.name}" if not author.nick else f"{author.nick} (@{author.name})"

        channel = msgs[0].channel
        if isinstance(channel, (discord.DMChannel, discord.PartialMessageable)):
            channel_name = "MP"
        else:
            channel_name = channel.name if getattr(channel, "name", None) else "Inconnu"
        date_str = msgs[0].created_at.strftime("%d.%m.%Y")

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(self._render_single_sync, bg, full_content, author_name, channel_name, date_str),
        )

    # ------------------------------------------------------------------
    # Génération d'image — multi-auteurs
    # ------------------------------------------------------------------

    @staticmethod
    def _render_multi_sync(group_data: list[dict], fonts: dict) -> Image.Image:
        """Rendu PIL synchrone de l'image multi-auteurs — exécuté dans un thread pool."""
        width = MULTI_WIDTH
        ggsans = fonts["ggsans"]
        ggsans_xs = fonts["ggsans_xs"]
        ggsans_semi = fonts["ggsans_semi"]

        images: list[Image.Image] = []
        total_height = 0
        for gd in group_data:
            author = gd["author"]
            full_text = gd["text"]

            base_height = 200
            height = base_height + 40 * (full_text.count("\n") - 1 if full_text.count("\n") > 0 else 0)

            img = Image.new("RGB", (width, height), (255, 255, 255))
            draw = ImageDraw.Draw(img)

            disp_avatar = _add_gradient_dir(gd["bg"], 0.9, direction="right_to_left")
            bg = copy.copy(disp_avatar)
            text_color = (255, 255, 255)

            bg = bg.resize((width, width))
            if bg.height > height:
                bg = bg.crop((0, (bg.height - height) // 2, bg.width, (bg.height - height) // 2 + height))
            elif bg.height < height:
                bg = bg.resize((height, height), Image.Resampling.LANCZOS)
                bg = bg.crop(((bg.width - width) // 2, 0, (bg.width - width) // 2 + width, bg.height))
            img.paste(bg, (0, 0))

            avatar = Image.open(BytesIO(gd["avatar_raw"])).convert("RGBA").resize((240, 240))
            avatar = _round_corners(avatar, 30)
            avatar = avatar.resize((120, 120), Image.Resampling.LANCZOS)
            img.paste(avatar, (40, 40), avatar)

            if author.display_name.lower() == author.name.lower():
                draw.text((180, 30), author.display_name, text_color, font=ggsans)
            else:
                draw.text((180, 30), author.display_name, text_color, font=ggsans)
                draw.text(
                    (180 + ggsans.getlength(author.display_name) + 10, 44),
                    f"@{author.name}",
                    (text_color[0], text_color[1], text_color[2], 220),
                    font=ggsans_xs,
                )

            draw.multiline_text((180, 80), full_text, text_color, font=ggsans_semi)

            total_height += height
            images.append(img)

        final_img = Image.new("RGBA", (width, total_height), (0, 0, 0, 0))
        y = 0
        for img in images:
            final_img.paste(img, (0, y))
            y += img.height

        return final_img

    async def _build_multi_image(self, messages: list[discord.Message]) -> Image.Image:
        msgs = sorted(messages, key=lambda m: m.created_at)
        base_message = msgs[0]
        if not isinstance(base_message.author, discord.Member):
            raise ValueError("Le message de base doit être envoyé par un membre du serveur.")

        groups: list[list[discord.Message]] = []
        for msg in msgs:
            if groups and groups[-1][0].author == msg.author:
                groups[-1].append(msg)
            else:
                groups.append([msg])

        # On ne récupère le fond flouté et l'avatar qu'une seule fois par auteur.
        author_assets: dict[int, tuple[Image.Image, bytes]] = {}
        for g in groups:
            author = g[0].author
            if author.id not in author_assets and isinstance(author, discord.Member):
                bg = await self._get_blurred_bg(author, MULTI_BG_BLUR)
                avatar_raw = await author.display_avatar.with_size(256).read()
                author_assets[author.id] = (bg, avatar_raw)

        group_data: list[dict] = []
        for g in groups:
            full_text = ""
            for msg in g:
                content = _normalize_text(msg.clean_content)
                if len(content) > 50:
                    content = "\n".join(textwrap.wrap(content, 50))
                full_text += f"{content}\n"
            full_text = full_text[:-1]

            author = g[0].author
            bg, avatar_raw = author_assets[author.id]
            group_data.append({"text": full_text, "author": author, "bg": bg, "avatar_raw": avatar_raw})

        fonts = {
            "ggsans": self._font("gg_sans", 40),
            "ggsans_xs": self._font("gg_sans", 24),
            "ggsans_semi": self._font("gg_sans_semi", 32),
        }

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, functools.partial(self._render_multi_sync, group_data, fonts))

    # ------------------------------------------------------------------
    # Génération finale
    # ------------------------------------------------------------------

    async def generate_quote(self, messages: list[discord.Message]) -> discord.File:
        authors = {m.author for m in messages}
        try:
            if len(authors) == 1:
                image = await self._build_single_image(messages)
                filename = "quote.png"
                alt = f'"{pretty.shorten_text(messages[0].clean_content, 300)}" — {messages[0].author.display_name}'
            else:
                image = await self._build_multi_image(messages)
                filename = "quote_multi.png"
                alt = f"Citation de {len(authors)} auteurs"
        except Exception as e:
            logger.exception(e)
            raise ValueError("Impossible de générer l'image de citation.")

        buf = BytesIO()
        image.save(buf, format="PNG")
        buf.seek(0)
        return discord.File(buf, filename=filename, description=alt)

    # ------------------------------------------------------------------
    # Récupération du contexte
    # ------------------------------------------------------------------

    async def fetch_context_messages(self, starting_message: discord.Message, limit: int = 15) -> list[discord.Message]:
        """Récupère le message cible et les messages suivants éligibles pour la citation."""
        messages = [starting_message]
        async for msg in starting_message.channel.history(limit=limit * 2, after=starting_message):
            if not msg.content or msg.content.isspace():
                continue
            messages.append(msg)
            if len(messages) >= limit:
                break
        return messages

    # ==================================================================
    # COMMANDES
    # ==================================================================

    @app_commands.command(name="quote")
    @app_commands.checks.cooldown(1, 600)
    async def inspirobot_quote(self, interaction: discord.Interaction) -> None:
        """Obtenir une citation aléatoire d'Inspirobot.me."""
        await interaction.response.defer()
        async with aiohttp.ClientSession() as session:
            async with session.get(INSPIROBOT_URL) as resp:
                if resp.status != 200:
                    return await interaction.followup.send(
                        "**Erreur ·** Impossible d'obtenir une citation depuis Inspirobot.me.", ephemeral=True
                    )
                url = await resp.text()
            async with session.get(url) as resp2:
                if resp2.status != 200:
                    return await interaction.followup.send(
                        "**Erreur ·** Impossible de télécharger l'image.", ephemeral=True
                    )
                data = BytesIO(await resp2.read())

        await interaction.followup.send(
            file=discord.File(data, "quote.png", description="Citation fournie par Inspirobot.me")
        )

    async def create_quote_callback(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Context menu : Créer une citation."""
        if not message.content or message.content.isspace():
            return await interaction.response.send_message(
                "**Action impossible ·** Le message est vide.", ephemeral=True
            )
        if interaction.channel_id != message.channel.id:
            return await interaction.response.send_message(
                "**Action impossible ·** Le message doit être dans le même salon.", ephemeral=True
            )

        try:
            view = QuoteSelectionView(self, message)
            await view.start(interaction)
        except Exception as e:
            logger.exception(e)
            await interaction.followup.send(
                f"**Erreur ·** Impossible d'initialiser le menu de sélection : `{e}`", ephemeral=True
            )

        self.__flush_countdown -= 1
        if self.__flush_countdown <= 0:
            self._flush_bg_cache()
            self.__flush_countdown = FLUSH_AFTER


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Quotes(bot))
