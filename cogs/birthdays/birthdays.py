import logging
import re
from datetime import date, datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import dataio

logger = logging.getLogger("BOT.Birthdays")

MONTHS_FR = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
DEFAULT_ANNOUNCE_LIMIT = 15
MIN_BIRTH_YEAR = 1900
CURRENT_YEAR = date.today().year


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_valid_date(day: int, month: int, year: int | None) -> bool:
    if not (1 <= month <= 12) or not (1 <= day <= 31):
        return False
    try:
        # Année 2000 (bissextile) utilisée comme référence quand l'année n'est pas précisée,
        # afin de permettre l'enregistrement d'un anniversaire au 29 février.
        datetime(year or 2000, month, day)
        return True
    except ValueError:
        return False


def _safe_date(year: int, month: int, day: int) -> date:
    if month == 2 and day == 29:
        try:
            return date(year, 2, 29)
        except ValueError:
            return date(year, 3, 1)
    return date(year, month, day)


def _next_occurrence(day: int, month: int, today: date) -> date:
    candidate = _safe_date(today.year, month, day)
    if candidate < today:
        candidate = _safe_date(today.year + 1, month, day)
    return candidate


def _format_date(day: int, month: int, year: int | None) -> str:
    base = f"{day} {MONTHS_FR[month - 1]}"
    return f"{base} {year}" if year else base


def _countdown_label(days_until: int) -> str:
    if days_until == 0:
        return "aujourd'hui"
    if days_until == 1:
        return "demain"
    return f"dans {days_until} jours"


def _parse_month(raw: str) -> int | None:
    """Accepte aussi bien un numéro (`3`) qu'un nom de mois (`mars`), pour couvrir le cas où
    l'utilisateur valide sans passer par l'autocomplétion."""
    raw = raw.strip().lower()
    if raw.isdigit():
        month = int(raw)
        return month if 1 <= month <= 12 else None
    try:
        return MONTHS_FR.index(raw) + 1
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# UI — Annonce des anniversaires du jour (Components V2, purement présentatif)
# ---------------------------------------------------------------------------

def _build_announcement_view(members: list[tuple[discord.Member, dict]]) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container()

    container.add_item(discord.ui.TextDisplay("## Anniversaires du jour"))
    container.add_item(discord.ui.Separator())

    today = date.today()
    for member, birthday in members:
        year = birthday["year"]
        if year:
            age = today.year - year
            detail = f"Fête ses {age} ans aujourd'hui."
        else:
            detail = "Fête son anniversaire aujourd'hui."
        container.add_item(
            discord.ui.Section(
                f"**{member.display_name}**\n{detail}",
                accessory=discord.ui.Thumbnail(member.display_avatar.url),
            )
        )

    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(f"-# {len(members)} anniversaire(s) aujourd'hui."))

    view.add_item(container)
    return view


# ---------------------------------------------------------------------------
# UI — Panneau de configuration (salon + heure d'annonce)
# ---------------------------------------------------------------------------

class AnnounceTimeModal(discord.ui.Modal, title="Heure d'annonce"):
    def __init__(self, view_ref: "BirthdayConfigView"):
        super().__init__()
        self._view_ref = view_ref
        self.time_input = discord.ui.TextInput(
            label="Heure (HH:MM)",
            placeholder="Ex. 09:00",
            default=f"{view_ref.hour:02d}:{view_ref.minute:02d}",
            max_length=5,
        )
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        match = TIME_RE.match(self.time_input.value.strip())
        if not match:
            await interaction.followup.send("**Erreur ·** Le format attendu est `HH:MM` (ex. `09:00`).", ephemeral=True)
            return
        hour, minute = int(match.group(1)), int(match.group(2))
        cog, guild = self._view_ref.cog, self._view_ref.guild
        await cog.data.get(guild).set_dict_value("settings", "AnnounceHour", hour)
        await cog.data.get(guild).set_dict_value("settings", "AnnounceMinute", minute)
        await self._view_ref.refresh()


class EditTimeButton(discord.ui.Button):
    def __init__(self, view_ref: "BirthdayConfigView"):
        super().__init__(label="Modifier", style=discord.ButtonStyle.secondary)
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AnnounceTimeModal(self._view_ref))


class ToggleAnnounceButton(discord.ui.Button):
    """Active/désactive l'annonce sans perdre le salon configuré (mémorisé pour réactivation)."""

    def __init__(self, view_ref: "BirthdayConfigView"):
        active = view_ref.announce_channel is not None
        super().__init__(
            label="Désactiver" if active else "Activer",
            style=discord.ButtonStyle.red if active else discord.ButtonStyle.green,
        )
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        cog, guild = self._view_ref.cog, self._view_ref.guild
        settings = cog.data.get(guild)

        if self._view_ref.announce_channel is not None:
            await settings.set_dict_value("settings", "LastAnnounceChannelID", self._view_ref.announce_channel.id)
            await settings.set_dict_value("settings", "AnnounceChannelID", 0)
            await self._view_ref.refresh()
            return

        last_id = await settings.get_dict_value("settings", "LastAnnounceChannelID", cast=int)
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
        await settings.set_dict_value("settings", "AnnounceChannelID", channel.id)
        await self._view_ref.refresh()


class AnnounceChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, view_ref: "BirthdayConfigView"):
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="Sélectionner le salon d'annonce",
            min_values=0,
            max_values=1,
        )
        self._view_ref = view_ref

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        cog, guild = self._view_ref.cog, self._view_ref.guild

        if not self.values:
            if self._view_ref.announce_channel is not None:
                await cog.data.get(guild).set_dict_value(
                    "settings", "LastAnnounceChannelID", self._view_ref.announce_channel.id
                )
            await cog.data.get(guild).set_dict_value("settings", "AnnounceChannelID", 0)
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

        await cog.data.get(guild).set_dict_value("settings", "AnnounceChannelID", channel.id)
        await self._view_ref.refresh()


class BirthdayConfigView(discord.ui.LayoutView):
    """Panneau de configuration unique des anniversaires (salon et heure d'annonce)."""

    def __init__(
        self,
        cog: "Birthdays",
        guild: discord.Guild,
        *,
        announce_channel: discord.TextChannel | None,
        hour: int,
        minute: int,
        tracked_count: int,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.announce_channel = announce_channel
        self.hour = hour
        self.minute = minute
        self.tracked_count = tracked_count
        self._interaction: discord.Interaction | None = None
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "**Action impossible ·** La permission `Gérer le serveur` est requise.", ephemeral=True
            )
            return False
        return True

    def _build(self) -> None:
        self.clear_items()
        container = discord.ui.Container()

        container.add_item(discord.ui.TextDisplay(f"## Configuration des anniversaires — {self.guild.name}"))
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.Section(
                f"**Salon d'annonce**\n{self.announce_channel.mention if self.announce_channel else '*Non configuré*'}",
                accessory=ToggleAnnounceButton(self),
            )
        )
        channel_row = discord.ui.ActionRow()
        channel_row.add_item(AnnounceChannelSelect(self))
        container.add_item(channel_row)
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.Section(
                f"**Heure d'annonce**\n{self.hour:02d}:{self.minute:02d} (heure du serveur hébergeant le bot)",
                accessory=EditTimeButton(self),
            )
        )
        container.add_item(discord.ui.Separator())

        container.add_item(
            discord.ui.TextDisplay(f"-# {self.tracked_count} anniversaire(s) enregistré(s) sur ce serveur.")
        )

        self.add_item(container)

    async def _reload(self) -> None:
        self.announce_channel = await self.cog.get_announce_channel(self.guild)
        self.hour, self.minute = await self.cog.get_announce_time(self.guild)
        self.tracked_count = await self.cog.count_tracked(self.guild)

    async def refresh(self) -> None:
        await self._reload()
        self._build()
        if self._interaction:
            await self._interaction.edit_original_response(view=self)

    async def start(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction
        await interaction.response.send_message(view=self, ephemeral=True)


# ---------------------------------------------------------------------------
# UI — Liste des prochains anniversaires
# ---------------------------------------------------------------------------

def _build_upcoming_view(entries: list[tuple[discord.Member, dict, date, int]]) -> discord.ui.LayoutView:
    """`entries` : (membre, anniversaire, prochaine_occurrence, jours_restants), déjà triés.

    Regroupées par mois pour alléger l'affichage ; les mentions utilisent `AllowedMentions.none()`
    côté envoi pour ne pas notifier les membres cités.
    """
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container()

    container.add_item(discord.ui.TextDisplay("## Prochains anniversaires"))
    container.add_item(discord.ui.Separator())

    if not entries:
        container.add_item(discord.ui.TextDisplay("*Aucun anniversaire enregistré sur ce serveur.*"))
    else:
        groups: list[tuple[str, list[tuple[discord.Member, dict, date, int]]]] = []
        for entry in entries:
            month_name = MONTHS_FR[entry[2].month - 1].capitalize()
            if groups and groups[-1][0] == month_name:
                groups[-1][1].append(entry)
            else:
                groups.append((month_name, [entry]))

        for i, (month_name, group_entries) in enumerate(groups):
            if i > 0:
                container.add_item(discord.ui.Separator())
            lines = [f"**{month_name}**"]
            for member, birthday, _occurrence, days_until in group_entries:
                lines.append(f"{member.mention} · le **{birthday['day']}**\n-# {_countdown_label(days_until)}")
            container.add_item(discord.ui.TextDisplay("\n".join(lines)))

    view.add_item(container)
    return view


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Birthdays(commands.Cog):
    """Système d'anniversaires : chaque membre peut renseigner sa date de naissance."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = dataio.get_instance(self)

        settings = dataio.DictTableBuilder(
            "settings",
            {
                "AnnounceChannelID": 0,
                "LastAnnounceChannelID": 0,
                "AnnounceHour": 9,
                "AnnounceMinute": 0,
                "LastAnnouncedDate": "",
            },
        )
        self.data.link(discord.Guild, settings)

        birthdays_table = dataio.TableBuilder(
            """CREATE TABLE IF NOT EXISTS birthdays (
                user_id INTEGER PRIMARY KEY,
                day INTEGER NOT NULL,
                month INTEGER NOT NULL,
                year INTEGER
            )"""
        )
        self.data.link("global", birthdays_table)

    async def cog_load(self) -> None:
        self._check_announcements.start()

    async def cog_unload(self) -> None:
        self._check_announcements.cancel()
        await self.data.close_all()

    # ------------------------------------------------------------------
    # Données — anniversaires (globaux, indépendants du serveur)
    # ------------------------------------------------------------------

    async def get_birthday(self, user_id: int) -> dict | None:
        row = await self.data.get("global").fetchone("SELECT * FROM birthdays WHERE user_id=?", user_id)
        if row is None:
            return None
        return {"day": row["day"], "month": row["month"], "year": row["year"]}

    async def set_birthday(self, user_id: int, day: int, month: int, year: int | None) -> None:
        await self.data.get("global").execute(
            "INSERT OR REPLACE INTO birthdays (user_id, day, month, year) VALUES (?, ?, ?, ?)",
            user_id, day, month, year,
        )

    async def remove_birthday(self, user_id: int) -> None:
        await self.data.get("global").execute("DELETE FROM birthdays WHERE user_id=?", user_id)

    async def get_all_birthdays(self) -> dict[int, dict]:
        rows = await self.data.get("global").fetchall("SELECT * FROM birthdays")
        return {row["user_id"]: {"day": row["day"], "month": row["month"], "year": row["year"]} for row in rows}

    async def count_tracked(self, guild: discord.Guild) -> int:
        all_birthdays = await self.get_all_birthdays()
        return sum(1 for uid in all_birthdays if guild.get_member(uid) is not None)

    # ------------------------------------------------------------------
    # Paramètres — annonce par serveur
    # ------------------------------------------------------------------

    async def get_announce_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel_id = await self.data.get(guild).get_dict_value("settings", "AnnounceChannelID", cast=int)
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def get_announce_time(self, guild: discord.Guild) -> tuple[int, int]:
        settings = self.data.get(guild)
        hour = await settings.get_dict_value("settings", "AnnounceHour", cast=int)
        minute = await settings.get_dict_value("settings", "AnnounceMinute", cast=int)
        return hour, minute

    # ------------------------------------------------------------------
    # Annonce quotidienne
    # ------------------------------------------------------------------

    async def _post_todays_birthdays(self, guild: discord.Guild, channel: discord.TextChannel) -> None:
        today = date.today()
        all_birthdays = await self.get_all_birthdays()
        members: list[tuple[discord.Member, dict]] = []
        for user_id, birthday in all_birthdays.items():
            if birthday["day"] != today.day or birthday["month"] != today.month:
                continue
            member = guild.get_member(user_id)
            if member:
                members.append((member, birthday))

        if not members:
            return

        try:
            await channel.send(view=_build_announcement_view(members), silent=False)
        except discord.HTTPException as e:
            logger.error(f"Erreur lors de l'annonce des anniversaires sur {guild.name} : {e}")

    @tasks.loop(seconds=20)
    async def _check_announcements(self) -> None:
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        for guild in self.bot.guilds:
            try:
                channel = await self.get_announce_channel(guild)
                if not channel:
                    continue
                hour, minute = await self.get_announce_time(guild)
                if now.hour != hour or now.minute != minute:
                    continue

                settings = self.data.get(guild)
                last_date = await settings.get_dict_value("settings", "LastAnnouncedDate")
                if last_date == today_str:
                    continue
                await settings.set_dict_value("settings", "LastAnnouncedDate", today_str)
                await self._post_todays_birthdays(guild, channel)
            except Exception as e:
                logger.error(f"Erreur lors de la vérification des anniversaires pour {guild.name} : {e}", exc_info=True)

    @_check_announcements.before_loop
    async def _before_check_announcements(self) -> None:
        await self.bot.wait_until_ready()

    # ==================================================================
    # COMMANDES — gestion de sa propre date de naissance
    # ==================================================================

    birthday_group = app_commands.Group(name="birthday", description="Gérer les dates de naissance", guild_only=True)

    async def _autocomplete_month(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        current = current.strip().lower()
        names = [name for name in MONTHS_FR if current in name] if current else list(MONTHS_FR)
        return [
            app_commands.Choice(name=name.capitalize(), value=str(MONTHS_FR.index(name) + 1))
            for name in names[:12]
        ]

    @birthday_group.command(name="set")
    @app_commands.rename(day="jour", month="mois", year="annee")
    @app_commands.describe(
        day="Jour de naissance (1-31)",
        month="Mois de naissance (utilisez l'autocomplétion)",
        year="Année de naissance (optionnel, sert uniquement à afficher l'âge)",
    )
    async def set_own_birthday(
        self,
        interaction: discord.Interaction,
        day: app_commands.Range[int, 1, 31],
        month: str,
        year: app_commands.Range[int, MIN_BIRTH_YEAR, CURRENT_YEAR] | None = None,
    ) -> None:
        """Renseigne ou met à jour votre date de naissance."""
        month_num = _parse_month(month)
        if month_num is None:
            return await interaction.response.send_message(
                "**Erreur ·** Mois invalide. Utilisez l'autocomplétion proposée.", ephemeral=True
            )
        if not _is_valid_date(day, month_num, year):
            return await interaction.response.send_message("**Erreur ·** Cette date n'existe pas.", ephemeral=True)

        await self.set_birthday(interaction.user.id, day, month_num, year)
        await interaction.response.send_message(
            f"**Date enregistrée ·** {_format_date(day, month_num, year)}.", ephemeral=True
        )

    @set_own_birthday.autocomplete("month")
    async def autocomplete_set_own_month(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_month(interaction, current)

    @birthday_group.command(name="remove")
    async def remove_own_birthday(self, interaction: discord.Interaction) -> None:
        """Retire votre date de naissance enregistrée."""
        if not await self.get_birthday(interaction.user.id):
            return await interaction.response.send_message(
                "**Erreur ·** Vous n'avez pas de date de naissance enregistrée.", ephemeral=True
            )
        await self.remove_birthday(interaction.user.id)
        await interaction.response.send_message("**Date retirée ·** Votre date de naissance a été supprimée.", ephemeral=True)

    @birthday_group.command(name="show")
    @app_commands.rename(user="utilisateur")
    @app_commands.describe(user="Utilisateur dont afficher la date de naissance (par défaut, vous)")
    async def show_birthday(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        """Affiche une date de naissance enregistrée."""
        target = user or interaction.user
        birthday = await self.get_birthday(target.id)
        if not birthday:
            subject = "Vous n'avez" if target == interaction.user else f"{target.display_name} n'a"
            return await interaction.response.send_message(
                f"**Info ·** {subject} pas de date de naissance enregistrée.", ephemeral=True
            )
        date_str = _format_date(birthday["day"], birthday["month"], birthday["year"])
        await interaction.response.send_message(
            f"**Date de naissance de {target.display_name} ·** {date_str}", ephemeral=True
        )

    # ------------------------------------------------------------------
    # COMMANDES — modération : gérer la date de naissance d'un autre membre
    # ------------------------------------------------------------------

    birthday_admin_group = app_commands.Group(
        name="admin",
        description="Gérer la date de naissance d'un autre membre",
        parent=birthday_group,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @birthday_admin_group.command(name="set")
    @app_commands.rename(user="utilisateur", day="jour", month="mois", year="annee")
    @app_commands.describe(
        user="Membre concerné",
        day="Jour de naissance (1-31)",
        month="Mois de naissance (utilisez l'autocomplétion)",
        year="Année de naissance (optionnel, sert uniquement à afficher l'âge)",
    )
    async def admin_set_birthday(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        day: app_commands.Range[int, 1, 31],
        month: str,
        year: app_commands.Range[int, MIN_BIRTH_YEAR, CURRENT_YEAR] | None = None,
    ) -> None:
        """Renseigne ou met à jour la date de naissance d'un membre du serveur."""
        month_num = _parse_month(month)
        if month_num is None:
            return await interaction.response.send_message(
                "**Erreur ·** Mois invalide. Utilisez l'autocomplétion proposée.", ephemeral=True
            )
        if not _is_valid_date(day, month_num, year):
            return await interaction.response.send_message("**Erreur ·** Cette date n'existe pas.", ephemeral=True)

        await self.set_birthday(user.id, day, month_num, year)
        await interaction.response.send_message(
            f"**Date enregistrée ·** {user.mention} — {_format_date(day, month_num, year)}.", ephemeral=True
        )

    @admin_set_birthday.autocomplete("month")
    async def autocomplete_admin_set_month(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_month(interaction, current)

    @birthday_admin_group.command(name="remove")
    @app_commands.rename(user="utilisateur")
    @app_commands.describe(user="Membre concerné")
    async def admin_remove_birthday(self, interaction: discord.Interaction, user: discord.Member) -> None:
        """Retire la date de naissance enregistrée d'un membre du serveur."""
        if not await self.get_birthday(user.id):
            return await interaction.response.send_message(
                f"**Erreur ·** {user.mention} n'a pas de date de naissance enregistrée.", ephemeral=True
            )
        await self.remove_birthday(user.id)
        await interaction.response.send_message(
            f"**Date retirée ·** La date de naissance de {user.mention} a été supprimée.", ephemeral=True
        )

    # ==================================================================
    # COMMANDES — serveur
    # ==================================================================

    @app_commands.command(name="birthdays")
    @app_commands.guild_only()
    @app_commands.rename(limit="nombre")
    async def upcoming_birthdays(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] = DEFAULT_ANNOUNCE_LIMIT) -> None:
        """Affiche les prochains anniversaires du serveur, dans l'ordre.

        :param limit: Nombre d'anniversaires à afficher
        """
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )

        await interaction.response.defer()
        today = date.today()
        all_birthdays = await self.get_all_birthdays()

        entries: list[tuple[discord.Member, dict, date, int]] = []
        for user_id, birthday in all_birthdays.items():
            member = guild.get_member(user_id)
            if not member:
                continue
            occurrence = _next_occurrence(birthday["day"], birthday["month"], today)
            days_until = (occurrence - today).days
            entries.append((member, birthday, occurrence, days_until))

        entries.sort(key=lambda e: e[2])
        await interaction.followup.send(
            view=_build_upcoming_view(entries[:limit]), allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="birthdayconfig")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def config_birthday(self, interaction: discord.Interaction) -> None:
        """Ouvre le panneau de configuration des anniversaires (salon et heure d'annonce)."""
        guild = interaction.guild
        if not isinstance(guild, discord.Guild):
            return await interaction.response.send_message(
                "**Erreur ·** Cette commande ne peut être utilisée que sur un serveur.", ephemeral=True
            )

        hour, minute = await self.get_announce_time(guild)
        view = BirthdayConfigView(
            self,
            guild,
            announce_channel=await self.get_announce_channel(guild),
            hour=hour,
            minute=minute,
            tracked_count=await self.count_tracked(guild),
        )
        await view.start(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Birthdays(bot))
