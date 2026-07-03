import logging
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import dataio, pretty

logger = logging.getLogger("BOT.MsgBoard")

LOGS_EXPIRATION = 60 * 60 * 24 * 7  # 7 jours
CACHE_SAVE_INTERVAL_MINUTES = 30

# Couvre les blocs unicode standards des emojis (symboles/pictogrammes, émoticônes, transport,
# drapeaux, symboles divers/dingbats) ainsi que le sélecteur de variante et le ZWJ (séquences
# combinées). Contrairement à une regex basée sur des paires de substitution UTF-16, celle-ci
# fonctionne correctement avec les chaînes Python 3 (points de code réels, pas de surrogates).
UNICODE_EMOJI_RE = re.compile(
    "^["
    "\u00a9\u00ae\u203c\u2049\u2122\u2139\u2194-\u21aa\u231a\u231b\u2328\u23cf\u23e9-\u23fa"
    "\u24c2\u25aa\u25ab\u25b6\u25c0\u25fb-\u25fe\u2600-\u27bf\u2934\u2935\u2b05-\u2b07\u2b1b"
    "\u2b1c\u2b50\u2b55\u3030\u303d\u3297\u3299\ufe0f\u200d"
    "\U0001f000-\U0001f0ff\U0001f100-\U0001f1ff\U0001f300-\U0001f5ff\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff\U0001f900-\U0001f9ff\U0001fa00-\U0001faff"
    "]+$"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_valid_vote_emoji(guild: discord.Guild, raw: str) -> tuple[bool, str, str | None]:
    """Valide et normalise un emoji de vote (Unicode ou personnalisé du serveur).

    :return: (valide, valeur_normalisée_à_stocker, message_d'erreur)
    """
    partial = discord.PartialEmoji.from_str(raw.strip())
    if partial.is_custom_emoji():
        if not guild.get_emoji(partial.id):
            return False, raw, "L'emoji personnalisé doit appartenir à ce serveur."
        return True, str(partial), None
    if not UNICODE_EMOJI_RE.match(str(partial)):
        return False, raw, "L'emoji doit être un emoji Unicode standard ou un emoji personnalisé de ce serveur."
    return True, str(partial), None


def _build_board_entry_view(message: discord.Message) -> discord.ui.LayoutView:
    """Construit le rendu Components V2 d'un message compilé (sans webhook ni embed).

    Purement présentatif : aucun composant interactif autre qu'un lien vers le message d'origine.
    """
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container()

    channel = message.channel
    channel_label = f"#{channel.name}" if isinstance(channel, (discord.TextChannel, discord.Thread)) else "MP"

    if message.reference and isinstance(message.reference.resolved, discord.Message):
        reply = message.reference.resolved
        reply_text = f"-# ↩ En réponse à **{reply.author.display_name}**"
        if reply.content:
            reply_text += f"\n-# {pretty.shorten_text(reply.content, 200)}"
        container.add_item(
            discord.ui.Section(
                reply_text,
                accessory=discord.ui.Thumbnail(reply.author.display_avatar.url),
            )
        )
        container.add_item(discord.ui.Separator())

    timestamp = int(message.created_at.timestamp())
    header = f"**{message.author.display_name}** · {channel_label} · <t:{timestamp}:R>"
    container.add_item(
        discord.ui.Section(
            f"{header}\n{message.content}" if message.content else header,
            accessory=discord.ui.Thumbnail(message.author.display_avatar.url),
        )
    )

    image_attachments = [a for a in message.attachments if (a.content_type or "").startswith("image/")]
    other_attachments = [a for a in message.attachments if a not in image_attachments]

    if image_attachments:
        container.add_item(
            discord.ui.MediaGallery(
                *[discord.MediaGalleryItem(a.url, description=a.filename) for a in image_attachments[:10]]
            )
        )
    if other_attachments:
        container.add_item(
            discord.ui.TextDisplay("\n".join(f"[{a.filename}]({a.url})" for a in other_attachments))
        )
    if message.stickers:
        container.add_item(
            discord.ui.TextDisplay("\n".join(f"[Sticker · {s.name}]({s.url})" for s in message.stickers))
        )
    if message.embeds:
        container.add_item(
            discord.ui.TextDisplay(f"-# Ce message contient {len(message.embeds)} embed(s) non reproduit(s) ici.")
        )

    container.add_item(discord.ui.Separator())
    link_row = discord.ui.ActionRow()
    link_row.add_item(discord.ui.Button(label="Message d'origine", url=message.jump_url, style=discord.ButtonStyle.link))
    container.add_item(link_row)

    view.add_item(container)
    return view


# ---------------------------------------------------------------------------
# UI — Panneau de configuration unique (Components V2)
# ---------------------------------------------------------------------------

MAX_AGE_CYCLE_HOURS = (6, 12, 24, 48)


class VoteSettingsModal(discord.ui.Modal, title="Paramètres de vote"):
    """Modal permettant de modifier en une fois le seuil et l'emoji de vote."""

    def __init__(self, view_ref: "MsgBoardConfigView"):
        super().__init__()
        self._view_ref = view_ref
        self.threshold_input = discord.ui.TextInput(
            label="Seuil de votes",
            placeholder="Nombre de votes nécessaires (ex. 3)",
            default=str(view_ref.threshold),
            max_length=3,
        )
        self.emoji_input = discord.ui.TextInput(
            label="Emoji de vote",
            placeholder="Emoji unicode ou personnalisé de ce serveur (ex. ⭐)",
            default=view_ref.vote_emoji,
            max_length=100,
        )
        self.add_item(self.threshold_input)
        self.add_item(self.emoji_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        raw_threshold = self.threshold_input.value.strip()
        if not raw_threshold.isdigit() or int(raw_threshold) < 1:
            await interaction.followup.send(
                "**Erreur ·** Le seuil doit être un nombre entier positif.", ephemeral=True
            )
            return
        valid, normalized_emoji, error = _is_valid_vote_emoji(self._view_ref.guild, self.emoji_input.value)
        if not valid:
            await interaction.followup.send(f"**Erreur ·** {error}", ephemeral=True)
            return

        cog, guild = self._view_ref.cog, self._view_ref.guild
        await cog.data.get(guild).set_dict_value("settings", "Threshold", int(raw_threshold))
        await cog.data.get(guild).set_dict_value("settings", "VoteEmoji", normalized_emoji)
        await self._view_ref.refresh()


class EditVoteSettingsButton(discord.ui.Button):
    def __init__(self, view_ref: "MsgBoardConfigView"):
        super().__init__(label="Modifier", style=discord.ButtonStyle.secondary)
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(VoteSettingsModal(self._view_ref))


class CycleMaxAgeButton(discord.ui.Button):
    def __init__(self, view_ref: "MsgBoardConfigView"):
        super().__init__(label=f"{view_ref.max_age_hours} h", style=discord.ButtonStyle.secondary)
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            idx = MAX_AGE_CYCLE_HOURS.index(self._view_ref.max_age_hours)
        except ValueError:
            idx = -1
        new_hours = MAX_AGE_CYCLE_HOURS[(idx + 1) % len(MAX_AGE_CYCLE_HOURS)]
        cog, guild = self._view_ref.cog, self._view_ref.guild
        await cog.data.get(guild).set_dict_value("settings", "MaxMessageAge", new_hours * 3600)
        await self._view_ref.refresh()


class ToggleActiveButton(discord.ui.Button):
    """Active/désactive le MsgBoard sans perdre le salon configuré (mémorisé pour réactivation)."""

    def __init__(self, view_ref: "MsgBoardConfigView"):
        active = view_ref.board_channel is not None
        super().__init__(
            label="Désactiver" if active else "Activer",
            style=discord.ButtonStyle.red if active else discord.ButtonStyle.green,
        )
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        cog, guild = self._view_ref.cog, self._view_ref.guild
        settings = cog.data.get(guild)

        if self._view_ref.board_channel is not None:
            await settings.set_dict_value("settings", "LastBoardChannelID", self._view_ref.board_channel.id)
            await settings.set_dict_value("settings", "BoardChannelID", 0)
            await self._view_ref.refresh()
            return

        last_id = await settings.get_dict_value("settings", "LastBoardChannelID", cast=int)
        channel = guild.get_channel(last_id) if last_id else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "**Erreur ·** Sélectionnez d'abord un salon via le menu ci-dessous.", ephemeral=True
            )
            return
        if not channel.permissions_for(guild.me).send_messages:
            await interaction.followup.send(
                "**Erreur ·** Je n'ai pas la permission d'envoyer des messages sur ce salon.", ephemeral=True
            )
            return
        await settings.set_dict_value("settings", "BoardChannelID", channel.id)
        await self._view_ref.refresh()


class BoardChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, view_ref: "MsgBoardConfigView"):
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="Sélectionner le salon de compilation",
            min_values=0,
            max_values=1,
        )
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        cog, guild = self._view_ref.cog, self._view_ref.guild

        if not self.values:
            if self._view_ref.board_channel is not None:
                await cog.data.get(guild).set_dict_value("settings", "LastBoardChannelID", self._view_ref.board_channel.id)
            await cog.data.get(guild).set_dict_value("settings", "BoardChannelID", 0)
            await self._view_ref.refresh()
            return

        channel = self.values[0].resolve()
        if channel is None:
            try:
                channel = await self.values[0].fetch()
            except discord.HTTPException:
                await interaction.followup.send("**Erreur ·** Salon introuvable.", ephemeral=True)
                return
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "**Erreur ·** Seuls les salons textuels sont pris en charge.", ephemeral=True
            )
            return
        if not channel.permissions_for(guild.me).send_messages:
            await interaction.followup.send(
                "**Erreur ·** Je n'ai pas la permission d'envoyer des messages sur ce salon.", ephemeral=True
            )
            return

        await cog.data.get(guild).set_dict_value("settings", "BoardChannelID", channel.id)
        await self._view_ref.refresh()


class MsgBoardConfigView(discord.ui.LayoutView):
    """Panneau de configuration unique du MsgBoard (salon, seuil de vote, âge maximal, stats)."""

    def __init__(
        self,
        cog: "MsgBoard",
        guild: discord.Guild,
        *,
        board_channel: discord.TextChannel | None,
        threshold: int,
        vote_emoji: str,
        max_age_hours: int,
        tracked_count: int,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.board_channel = board_channel
        self.threshold = threshold
        self.vote_emoji = vote_emoji
        self.max_age_hours = max_age_hours
        self.tracked_count = tracked_count
        self._interaction: discord.Interaction | None = None
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                "**Action impossible ·** La permission `Gérer les messages` est requise.", ephemeral=True
            )
            return False
        return True

    def _build(self) -> None:
        self.clear_items()

        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"## Configuration MsgBoard — {self.guild.name}"))
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.Section(
                f"**Salon de compilation**\n{self.board_channel.mention if self.board_channel else '*Non configuré*'}",
                accessory=ToggleActiveButton(self),
            )
        )
        channel_row = discord.ui.ActionRow()
        channel_row.add_item(BoardChannelSelect(self))
        container.add_item(channel_row)
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.Section(
                f"**Seuil de vote**\n{self.threshold} × {self.vote_emoji}",
                accessory=EditVoteSettingsButton(self),
            )
        )
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.Section(
                f"**Âge maximal des messages**\nLes messages de plus de {self.max_age_hours}h ne peuvent plus être compilés.",
                accessory=CycleMaxAgeButton(self),
            )
        )
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.TextDisplay(f"-# {self.tracked_count} message(s) compilé(s) sur les 7 derniers jours.")
        )

        self.add_item(container)

    async def _reload(self) -> None:
        self.board_channel = await self.cog.get_board_channel(self.guild)
        self.threshold = await self.cog.get_threshold(self.guild)
        self.vote_emoji = await self.cog.get_vote_emoji(self.guild)
        self.max_age_hours = (await self.cog.get_max_message_age(self.guild)) // 3600
        self.tracked_count = self.cog._tracked_count(self.guild)

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

class MsgBoard(commands.Cog):
    """Système de compilation des meilleurs messages."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_instance(self)

        settings = dataio.DictTableBuilder(
            "settings",
            {
                "BoardChannelID": 0,
                "LastBoardChannelID": 0,
                "Threshold": 3,
                "VoteEmoji": "⭐",
                "MaxMessageAge": 60 * 60 * 24,  # 24 h
            },
        )
        self.data.link(discord.Guild, settings)

        msgboard_logs = dataio.TableBuilder(
            """CREATE TABLE IF NOT EXISTS msgboard_logs (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                board_message_id INTEGER DEFAULT NULL,
                timestamp INTEGER
            )"""
        )
        self.data.link("global", msgboard_logs)

        self._board_cache: dict[int, dict] = {}
        self._pending_save = False

        self.ctx_add_to_board = app_commands.ContextMenu(
            name="Ajouter aux meilleurs messages",
            callback=self.add_to_board_callback,
        )
        self.bot.tree.add_command(self.ctx_add_to_board)

    async def cog_load(self) -> None:
        await self._load_cache()
        self._save_cache_loop.start()

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_add_to_board.name, type=self.ctx_add_to_board.type)
        self._save_cache_loop.cancel()
        await self._save_cache()
        await self.data.close_all()

    # ------------------------------------------------------------------
    # Paramètres
    # ------------------------------------------------------------------

    async def get_board_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel_id = await self.data.get(guild).get_dict_value("settings", "BoardChannelID", cast=int)
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def get_threshold(self, guild: discord.Guild) -> int:
        return await self.data.get(guild).get_dict_value("settings", "Threshold", cast=int)

    async def get_vote_emoji(self, guild: discord.Guild) -> str:
        return await self.data.get(guild).get_dict_value("settings", "VoteEmoji")

    async def get_max_message_age(self, guild: discord.Guild) -> int:
        return await self.data.get(guild).get_dict_value("settings", "MaxMessageAge", cast=int)

    # ------------------------------------------------------------------
    # Cache / logs anti-doublon
    # ------------------------------------------------------------------

    async def _load_cache(self) -> None:
        global_db = self.data.get("global")
        cutoff = datetime.now(timezone.utc).timestamp() - LOGS_EXPIRATION
        await global_db.execute("DELETE FROM msgboard_logs WHERE timestamp < ?", cutoff)
        # Un board_message_id à 0 signifie une tentative de compilation restée bloquée (crash/erreur
        # survenue entre le verrouillage et l'envoi effectif) : on la purge pour permettre une nouvelle tentative.
        await global_db.execute("DELETE FROM msgboard_logs WHERE board_message_id = 0 OR board_message_id IS NULL")
        rows = await global_db.fetchall("SELECT * FROM msgboard_logs")
        self._board_cache = {
            row["message_id"]: {
                "guild_id": row["guild_id"],
                "board_message_id": row["board_message_id"],
                "timestamp": row["timestamp"],
            }
            for row in rows
        }

    async def _save_cache(self) -> None:
        if not self._pending_save:
            return
        cutoff = datetime.now(timezone.utc).timestamp() - LOGS_EXPIRATION
        self._board_cache = {
            mid: data for mid, data in self._board_cache.items() if data["timestamp"] > cutoff
        }
        await self.data.get("global").executemany(
            "INSERT OR REPLACE INTO msgboard_logs VALUES (?, ?, ?, ?)",
            [
                (mid, data["guild_id"], data["board_message_id"], data["timestamp"])
                for mid, data in self._board_cache.items()
            ],
        )
        self._pending_save = False

    def _tracked_count(self, guild: discord.Guild) -> int:
        return sum(1 for data in self._board_cache.values() if data["guild_id"] == guild.id)

    @tasks.loop(minutes=CACHE_SAVE_INTERVAL_MINUTES)
    async def _save_cache_loop(self) -> None:
        try:
            await self._save_cache()
        except Exception as e:
            logger.error(f"Erreur lors de la sauvegarde périodique du cache MsgBoard : {e}", exc_info=True)

    def _mark_pending(self, message_id: int, guild_id: int, board_message_id: int) -> None:
        self._board_cache[message_id] = {
            "guild_id": guild_id,
            "board_message_id": board_message_id,
            "timestamp": datetime.now(timezone.utc).timestamp(),
        }
        self._pending_save = True

    def _is_already_posted(self, message_id: int) -> bool:
        return message_id in self._board_cache

    # ------------------------------------------------------------------
    # Reproduction du message
    # ------------------------------------------------------------------

    async def send_copied_message(self, message: discord.Message) -> discord.Message | None:
        """Ne lève jamais d'exception : renvoie `None` en cas d'échec (l'appelant peut alors
        annuler le verrou de cache posé par `_mark_pending` sans laisser le message bloqué)."""
        if not isinstance(message.guild, discord.Guild) or not isinstance(message.author, discord.Member):
            logger.warning(f"Message {message.id} ignoré : auteur invalide ou message hors serveur.")
            return None

        board_channel = await self.get_board_channel(message.guild)
        if not board_channel:
            return None

        try:
            view = _build_board_entry_view(message)
            return await board_channel.send(view=view, silent=True)
        except discord.HTTPException as e:
            logger.error(f"Erreur lors de la compilation du message {message.id} : {e}")
            return None
        except Exception as e:
            logger.error(f"Erreur inattendue lors de la compilation du message {message.id} : {e}", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # EVENT
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return
        channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        guild = channel.guild
        if not channel.permissions_for(guild.me).read_message_history:
            return

        board_channel_id = await self.data.get(guild).get_dict_value("settings", "BoardChannelID", cast=int)
        if not board_channel_id:
            return
        if channel.id == board_channel_id:
            # On ignore les votes dans le salon de compilation lui-même pour éviter les reposts en cascade.
            return

        vote_emoji = await self.get_vote_emoji(guild)
        if str(payload.emoji) != vote_emoji:
            return

        if self._is_already_posted(payload.message_id):
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.NotFound:
            return

        max_age = await self.get_max_message_age(guild)
        if message.created_at.timestamp() < datetime.now(timezone.utc).timestamp() - max_age:
            return

        threshold = await self.get_threshold(guild)
        counts = [r.count for r in message.reactions if str(r.emoji) == vote_emoji]
        if not counts or counts[0] < threshold:
            return

        # Marquage immédiat en cache pour éviter les doublons en cas de réactions concurrentes
        self._mark_pending(message.id, guild.id, 0)

        board_msg = await self.send_copied_message(message)
        if board_msg:
            self._board_cache[message.id]["board_message_id"] = board_msg.id
            self._pending_save = True
        else:
            # Échec de l'envoi : on retire le verrou pour permettre une nouvelle tentative
            self._board_cache.pop(message.id, None)
            return

        board_channel = await self.get_board_channel(guild)
        notif = (
            f"### `{vote_emoji}` {board_channel.mention} · Ce message a été ajouté au tableau !"
            if board_channel
            else f"### `{vote_emoji}` **MsgBoard** · Ce message a été ajouté au tableau !"
        )
        try:
            await message.reply(notif, mention_author=False, delete_after=30)
        except discord.HTTPException:
            pass

    # ==================================================================
    # COMMANDES
    # ==================================================================

    @app_commands.command(name="msgbconfig")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def config_msgboard(self, interaction: discord.Interaction) -> None:
        """Ouvre le panneau de configuration du MsgBoard (salon, seuil de vote, âge maximal)."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )

        view = MsgBoardConfigView(
            self,
            guild,
            board_channel=await self.get_board_channel(guild),
            threshold=await self.get_threshold(guild),
            vote_emoji=await self.get_vote_emoji(guild),
            max_age_hours=(await self.get_max_message_age(guild)) // 3600,
            tracked_count=self._tracked_count(guild),
        )
        await view.start(interaction)

    # ------------------------------------------------------------------
    # Menu contextuel — ajout manuel sans passer par le seuil de votes
    # ------------------------------------------------------------------

    async def add_to_board_callback(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Context menu : ajoute directement un message au MsgBoard, sans vote."""
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message(
                "**Action impossible ·** La permission `Gérer les messages` est requise pour utiliser cette action.",
                ephemeral=True,
            )
        guild = message.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Ce message ne provient pas d'un serveur.", ephemeral=True
            )
        if not isinstance(message.author, discord.Member):
            return await interaction.response.send_message(
                "**Erreur ·** Impossible de compiler ce message (auteur invalide).", ephemeral=True
            )
        if self._is_already_posted(message.id):
            return await interaction.response.send_message(
                "**Info ·** Ce message a déjà été compilé.", ephemeral=True
            )
        board_channel = await self.get_board_channel(guild)
        if not board_channel:
            return await interaction.response.send_message(
                "**Erreur ·** Aucun salon de compilation n'est configuré (`/config-msgboard`).", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        # Marquage immédiat en cache pour éviter tout doublon en cas de vote concurrent
        self._mark_pending(message.id, guild.id, 0)
        board_msg = await self.send_copied_message(message)
        if not board_msg:
            self._board_cache.pop(message.id, None)
            return await interaction.followup.send(
                "**Erreur ·** Impossible d'ajouter ce message au MsgBoard.", ephemeral=True
            )

        self._board_cache[message.id]["board_message_id"] = board_msg.id
        self._pending_save = True
        await interaction.followup.send(
            f"**Message ajouté ·** Compilé manuellement dans {board_channel.mention}.", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MsgBoard(bot))
