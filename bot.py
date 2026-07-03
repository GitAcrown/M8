import asyncio
import logging
import logging.handlers
import os
import subprocess
import sys
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import dotenv_values

# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

PREFIX = "&"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FORMAT = "[%(asctime)s] %(levelname)s (%(name)s) %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            filename="logs/bot.log",
            maxBytes=5 * 1024 * 1024,  # 5 Mo par fichier
            backupCount=3,
            encoding="utf-8",
        ),
    ],
)

logger = logging.getLogger("BOT.Main")

# Réduire le bruit des libs tierces
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Intents
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cog_path(name: str) -> str:
    """Retourne le chemin d'extension d'un cog depuis son nom de dossier."""
    return f"cogs.{name}.{name}"


async def load_cogs(bot: commands.Bot) -> None:
    if not os.path.isdir("./cogs"):
        logger.warning("Dossier ./cogs introuvable — aucun cog chargé.")
        return
    for folder in os.listdir("./cogs"):
        if not os.path.isdir(os.path.join("./cogs", folder)):
            continue
        ext = _cog_path(folder)
        try:
            await bot.load_extension(ext)
            logger.info(f"Cog chargé : {folder}")
        except Exception as e:
            logger.error(f"Erreur chargement cog '{folder}' : {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    bot = commands.Bot(
        command_prefix=PREFIX,
        intents=intents,
        help_command=None,
        allowed_mentions=discord.AllowedMentions(replied_user=False),
    )
    bot.config = dotenv_values(".env")  # type: ignore[attr-defined]

    # Validation des variables d'environnement obligatoires
    required_keys = ("TOKEN", "APP_ID")
    missing = [k for k in required_keys if not bot.config.get(k)]  # type: ignore[attr-defined]
    if missing:
        logger.critical(f"Variable(s) manquante(s) dans .env : {', '.join(missing)}")
        return

    async with bot:
        logger.info("Chargement des cogs...")
        await load_cogs(bot)
        logger.info("Cogs chargés.")

        # -------------------------------------------------------------------
        # Événements
        # -------------------------------------------------------------------

        @bot.event
        async def on_ready() -> None:
            assert bot.user is not None
            logger.info(f"Connecté en tant que {bot.user} (ID : {bot.user.id})")
            logger.info(f"discord.py {discord.__version__}")
            logger.info(
                f"Connecté à {len(bot.guilds)} serveur(s) : "
                + ", ".join(f"{g.name} ({g.id})" for g in bot.guilds)
            )
            invite = discord.utils.oauth_url(
                int(bot.config["APP_ID"]),  # type: ignore[index]
                permissions=discord.Permissions(8),
            )
            logger.info(f"Invitation (ADMIN) : {invite}")

        @bot.event
        async def on_command_error(ctx: commands.Context, error: Exception) -> None:
            if isinstance(error, commands.CommandNotFound):
                return
            if isinstance(error, commands.NotOwner):
                await ctx.send("**Erreur ·** Cette commande est réservée au propriétaire du bot.")
                return
            if isinstance(error, commands.MissingRequiredArgument):
                await ctx.send(f"**Erreur ·** Argument manquant : `{error.param.name}`")
                return
            logger.error(f"Erreur commande '{ctx.command}' : {error}", exc_info=True)

        @bot.tree.error
        async def on_app_command_error(
            interaction: discord.Interaction, error: app_commands.AppCommandError
        ) -> None:
            if isinstance(error, app_commands.CommandOnCooldown):
                minutes, seconds = divmod(error.retry_after, 60)
                hours, minutes = divmod(minutes, 60)
                parts = []
                if round(hours) > 0:
                    parts.append(f"{round(hours)} heure(s)")
                if round(minutes) > 0:
                    parts.append(f"{round(minutes)} minute(s)")
                if round(seconds) > 0:
                    parts.append(f"{round(seconds)} seconde(s)")
                delay = ", ".join(parts) or "quelques instants"
                await interaction.response.send_message(
                    f"**Cooldown ·** Tu pourras réutiliser cette commande dans {delay}.",
                    ephemeral=True,
                )
                return
            if isinstance(error, app_commands.MissingPermissions):
                perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
                await interaction.response.send_message(
                    f"**Erreur ·** Permission(s) manquante(s) : {perms}",
                    ephemeral=True,
                )
                return
            logger.error(f"Erreur slash command : {error}", exc_info=True)
            msg = f"**Erreur ·** Une erreur est survenue :\n`{error}`"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(content=msg, ephemeral=True)
                else:
                    await interaction.response.send_message(content=msg, ephemeral=True, delete_after=45)
            except discord.HTTPException:
                pass

        # -------------------------------------------------------------------
        # Commandes owner — Administration du bot
        # -------------------------------------------------------------------

        @bot.command(name="ping")
        @commands.is_owner()
        async def ping(ctx: commands.Context) -> None:
            """Affiche la latence WebSocket du bot."""
            await ctx.send(f"**Pong !** `{round(bot.latency * 1000)} ms`")

        @bot.command(name="shutdown")
        @commands.is_owner()
        async def shutdown(ctx: commands.Context) -> None:
            """Arrête proprement le bot."""
            await ctx.send("Arrêt du bot...")
            logger.info(f"Arrêt demandé par {ctx.author}")
            await bot.close()

        @bot.command(name="restart")
        @commands.is_owner()
        async def restart(ctx: commands.Context) -> None:
            """Redémarre le processus du bot."""
            await ctx.send("Redémarrage du bot...")
            logger.info(f"Redémarrage demandé par {ctx.author}")
            await bot.close()
            os.execv(sys.executable, [sys.executable] + sys.argv)

        @bot.command(name="update")
        @commands.is_owner()
        async def update(ctx: commands.Context) -> None:
            """Lance `git pull` puis redémarre le bot."""
            await ctx.send("Mise à jour en cours...")
            try:
                result = subprocess.run(
                    ["git", "pull"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                output = result.stdout.strip() or result.stderr.strip() or "(aucune sortie)"
                if result.returncode == 0:
                    await ctx.send(f"**Mise à jour réussie.**\n```\n{output[:1800]}\n```")
                    logger.info(f"git pull réussi — redémarrage par {ctx.author}")
                    await bot.close()
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                else:
                    await ctx.send(f"**Erreur git pull.**\n```\n{output[:1800]}\n```")
            except subprocess.TimeoutExpired:
                await ctx.send("**Erreur ·** `git pull` a dépassé le délai d'attente (30 s).")
            except FileNotFoundError:
                await ctx.send("**Erreur ·** `git` n'est pas installé ou introuvable dans le PATH.")
            except Exception as e:
                await ctx.send(f"**Erreur ·** {e}")

        # -------------------------------------------------------------------
        # Commandes owner — Gestion des cogs
        # -------------------------------------------------------------------

        @bot.command(name="cogs")
        @commands.is_owner()
        async def cogs_list(ctx: commands.Context) -> None:
            """Liste les cogs actuellement chargés."""
            loaded = [name for name in bot.cogs]
            if loaded:
                lines = "\n".join(f"• {name}" for name in sorted(loaded))
                await ctx.send(f"**Cogs chargés ({len(loaded)}) :**\n{lines}")
            else:
                await ctx.send("Aucun cog chargé.")

        @bot.command(name="load")
        @commands.is_owner()
        async def load_cog(ctx: commands.Context, cog: str) -> None:
            """Charge un cog par son nom de dossier."""
            try:
                await bot.load_extension(_cog_path(cog))
                await ctx.send(f"**Cog `{cog}` chargé.**")
                logger.info(f"Cog '{cog}' chargé manuellement par {ctx.author}")
            except commands.ExtensionAlreadyLoaded:
                await ctx.send(f"**Erreur ·** Le cog `{cog}` est déjà chargé.")
            except commands.ExtensionNotFound:
                await ctx.send(f"**Erreur ·** Cog `{cog}` introuvable.")
            except Exception as e:
                await ctx.send(f"**Erreur ·** `{type(e).__name__}: {e}`")

        @bot.command(name="unload")
        @commands.is_owner()
        async def unload_cog(ctx: commands.Context, cog: str) -> None:
            """Décharge un cog par son nom de dossier."""
            try:
                await bot.unload_extension(_cog_path(cog))
                await ctx.send(f"**Cog `{cog}` déchargé.**")
                logger.info(f"Cog '{cog}' déchargé manuellement par {ctx.author}")
            except commands.ExtensionNotLoaded:
                await ctx.send(f"**Erreur ·** Le cog `{cog}` n'est pas chargé.")
            except Exception as e:
                await ctx.send(f"**Erreur ·** `{type(e).__name__}: {e}`")

        @bot.command(name="reload")
        @commands.is_owner()
        async def reload_cog(ctx: commands.Context, cog: str) -> None:
            """Recharge à chaud un cog sans redémarrer le bot."""
            try:
                await bot.reload_extension(_cog_path(cog))
                await ctx.send(f"**Cog `{cog}` rechargé.**")
                logger.info(f"Cog '{cog}' rechargé par {ctx.author}")
            except commands.ExtensionNotLoaded:
                await ctx.send(f"**Erreur ·** Le cog `{cog}` n'est pas chargé.")
            except commands.ExtensionNotFound:
                await ctx.send(f"**Erreur ·** Cog `{cog}` introuvable.")
            except Exception as e:
                logger.error(f"Erreur reload cog '{cog}' : {e}", exc_info=True)
                await ctx.send(f"**Erreur ·** `{type(e).__name__}: {e}`")

        # -------------------------------------------------------------------
        # Commande owner — Synchronisation des slash commands
        # -------------------------------------------------------------------

        @bot.command(name="sync")
        @commands.guild_only()
        @commands.is_owner()
        async def sync(
            ctx: commands.Context,
            guilds: commands.Greedy[discord.Object],
            spec: Optional[Literal["~", "*", "^"]] = None,
        ) -> None:
            """Synchronise les slash commands.

            sync       → commandes globales
            sync ~     → serveur courant uniquement
            sync *     → copie global → serveur courant
            sync ^     → supprime les commandes du serveur courant
            sync <id>  → synchronise un/des serveur(s) spécifique(s)
            """
            if not guilds:
                if spec == "~":
                    synced = await ctx.bot.tree.sync(guild=ctx.guild)
                elif spec == "*":
                    ctx.bot.tree.copy_global_to(guild=ctx.guild)
                    synced = await ctx.bot.tree.sync(guild=ctx.guild)
                elif spec == "^":
                    ctx.bot.tree.clear_commands(guild=ctx.guild)
                    await ctx.bot.tree.sync(guild=ctx.guild)
                    synced = []
                else:
                    synced = await ctx.bot.tree.sync()

                scope = "globales" if spec is None else f"sur '{ctx.guild}'"
                names = ", ".join(f"`{c.name}`" for c in synced) if synced else "—"
                await ctx.send(
                    f"**{len(synced)} commande(s) synchronisée(s) {scope} :** {names}"
                )
                return

            ok = 0
            for guild in guilds:
                try:
                    await ctx.bot.tree.sync(guild=guild)
                    ok += 1
                except discord.HTTPException:
                    pass
            await ctx.send(f"Arbre synchronisé dans {ok}/{len(guilds)} serveur(s).")

        # -------------------------------------------------------------------
        # Démarrage
        # -------------------------------------------------------------------

        await bot.start(bot.config["TOKEN"])  # type: ignore[index]


if __name__ == "__main__":
    asyncio.run(main())
