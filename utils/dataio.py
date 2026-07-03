"""
### DATAIO : Gestion centralisée et asynchrone des données des cogs
Pour l'utiliser, déclarez une instance avec `get_instance(cog)` dans l'initialisation du cog
pour récupérer sa classe de gestion des données, puis liez-y des tables avec `.link(...)`.

Chaque modèle (guilde, utilisateur, ou base "globale") possède sa propre base SQLite,
stockée sous `cogs/<nom_du_cog>/data/`. Les accès sont entièrement asynchrones (aiosqlite),
ils ne bloquent donc jamais la boucle d'événements du bot.
"""

import logging
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiosqlite
import discord
from discord.ext import commands

COMMON_RESOURCES_PATH = Path("common/resources")

logger = logging.getLogger("BOT.DataIO")

__COGDATA_INSTANCES: dict[str, "CogData"] = {}


# DEFINITIONS DE TABLES ======================================================

class TableBuilder:
    def __init__(
        self,
        query: str,
        default_values: Sequence[dict[str, Any]] = (),
        *,
        insert_on_reconnect: bool = False,
    ):
        """Définition d'une table de données.

        :param query: Requête de création de la table (doit commencer par `CREATE TABLE`)
        :param default_values: Valeurs par défaut à insérer dans la table
        :param insert_on_reconnect: Si `True`, les valeurs par défaut sont réinsérées
            (avec `INSERT OR IGNORE`) à chaque connexion, même si la table existait déjà.
        """
        if not query.strip().startswith("CREATE TABLE"):
            raise ValueError("La requête doit commencer par 'CREATE TABLE'")
        self.query = query

        if default_values:
            keys = set(default_values[0].keys())
            if not all(set(d.keys()) == keys for d in default_values):
                raise ValueError("Les valeurs par défaut doivent toutes avoir les mêmes clés")
        self.default_values = list(default_values)
        self.insert_on_reconnect = insert_on_reconnect

    def __repr__(self) -> str:
        return f"<TableBuilder table_name={self.table_name!r}>"

    @property
    def table_name(self) -> str:
        match = re.search(r"CREATE TABLE (?:IF NOT EXISTS )?([^\s(]+)", self.query)
        if match is None:
            raise ValueError("Impossible de déterminer le nom de la table depuis la requête")
        return match.group(1)


class DictTableBuilder(TableBuilder):
    """Table simplifiée clé/valeur (paramètres par modèle)."""

    def __init__(
        self,
        name: str,
        default_values: dict[str, Any] | None = None,
        *,
        insert_on_reconnect: bool = True,
    ):
        query = f"CREATE TABLE IF NOT EXISTS {name} (key TEXT PRIMARY KEY, value TEXT)"
        default_values = default_values or {}
        defaults = [{"key": k, "value": v} for k, v in default_values.items()]
        super().__init__(query, defaults, insert_on_reconnect=insert_on_reconnect)

    def __repr__(self) -> str:
        return f"<DictTableBuilder table_name={self.table_name!r}>"


# GESTIONNAIRE PAR MODELE ====================================================

class ModelDataManager:
    """Gestionnaire de la base de données SQLite d'un modèle (guilde, utilisateur, "global", ...)."""

    def __init__(self, model: discord.abc.Snowflake | str, db_path: Path, *, builders: Sequence[TableBuilder] = ()):
        self.model = model
        self.db_path = db_path
        self.builders = tuple(builders)
        self._conn: aiosqlite.Connection | None = None

    def __repr__(self) -> str:
        return f"<ModelDataManager model={self.model!r}>"

    async def _ensure_connection(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        self._conn = conn

        existing_tables = {row[0] for row in await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}

        needs_commit = False
        for builder in self.builders:
            if builder.table_name not in existing_tables:
                logger.info(f"Initialisation de la table {self.model}:{builder.table_name}...")
                await conn.execute(builder.query)
                if builder.default_values:
                    columns = ", ".join(builder.default_values[0].keys())
                    placeholders = ", ".join("?" for _ in builder.default_values[0])
                    await conn.executemany(
                        f"INSERT OR IGNORE INTO {builder.table_name} ({columns}) VALUES ({placeholders})",
                        [tuple(d.values()) for d in builder.default_values],
                    )
                needs_commit = True
            elif builder.insert_on_reconnect and builder.default_values:
                columns = ", ".join(builder.default_values[0].keys())
                placeholders = ", ".join("?" for _ in builder.default_values[0])
                await conn.executemany(
                    f"INSERT OR IGNORE INTO {builder.table_name} ({columns}) VALUES ({placeholders})",
                    [tuple(d.values()) for d in builder.default_values],
                )
                needs_commit = True

        if needs_commit:
            await conn.commit()
        return conn

    # --- Requêtes génériques ---

    async def execute(self, query: str, *args: Any, commit: bool = True) -> None:
        conn = await self._ensure_connection()
        await conn.execute(query, args)
        if commit:
            await conn.commit()

    async def executemany(self, query: str, args: Iterable[Sequence[Any]], *, commit: bool = True) -> None:
        conn = await self._ensure_connection()
        await conn.executemany(query, args)
        if commit:
            await conn.commit()

    async def fetchone(self, query: str, *args: Any) -> aiosqlite.Row | None:
        conn = await self._ensure_connection()
        async with conn.execute(query, args) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, query: str, *args: Any) -> list[aiosqlite.Row]:
        conn = await self._ensure_connection()
        async with conn.execute(query, args) as cursor:
            return await cursor.fetchall()

    async def commit(self) -> None:
        if self._conn is not None:
            await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def tables(self) -> list[str]:
        rows = await self.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
        return [row["name"] for row in rows]

    async def column_names(self, table_name: str) -> list[str]:
        conn = await self._ensure_connection()
        async with conn.execute(f"SELECT * FROM {table_name} LIMIT 0") as cursor:
            return [d[0] for d in cursor.description]

    # --- Raccourcis tables clé/valeur ---

    async def _check_dict_table(self, table_name: str) -> None:
        if table_name not in await self.tables():
            raise ValueError(f"La table {table_name!r} n'existe pas")
        columns = await self.column_names(table_name)
        if "key" not in columns or "value" not in columns:
            raise ValueError(f"La table {table_name!r} n'est pas une table clé/valeur")

    async def get_dict_value(self, table_name: str, key: str, *, cast: type = str) -> Any:
        await self._check_dict_table(table_name)
        row = await self.fetchone(f"SELECT value FROM {table_name} WHERE key=?", key)
        if row is None:
            return None
        if cast is bool:
            return bool(int(row["value"]))
        return cast(row["value"])

    async def get_dict_values(self, table_name: str) -> dict[str, str]:
        await self._check_dict_table(table_name)
        rows = await self.fetchall(f"SELECT key, value FROM {table_name}")
        return {row["key"]: row["value"] for row in rows}

    async def set_dict_value(self, table_name: str, key: str, value: Any) -> None:
        await self._check_dict_table(table_name)
        if isinstance(value, bool):
            value = int(value)
        await self.execute(
            f"INSERT OR REPLACE INTO {table_name} (key, value) VALUES (?, ?)", key, str(value)
        )

    async def delete_dict_value(self, table_name: str, key: str) -> None:
        await self._check_dict_table(table_name)
        await self.execute(f"DELETE FROM {table_name} WHERE key=?", key)


# GESTIONNAIRE DE COG ========================================================

class CogData:
    def __init__(self, cog_name: str):
        """Gestionnaire de données d'un cog.

        :param cog_name: Nom du cog (utilisé pour le chemin `cogs/<cog_name>/data/`)
        """
        self.cog_name = cog_name
        self.cog_folder = Path(f"cogs/{cog_name}")
        self.cog_folder.mkdir(parents=True, exist_ok=True)

        self.__managers: dict[discord.abc.Snowflake | str, ModelDataManager] = {}
        self.__builders: dict[type | str, tuple[TableBuilder, ...]] = {}

    def __repr__(self) -> str:
        return f"<CogData cog_name={self.cog_name!r}>"

    # --- Résolution de nom ---

    @staticmethod
    def _model_db_name(model: discord.abc.Snowflake | str) -> str:
        if isinstance(model, str):
            return re.sub(r"[^a-z0-9_]", "_", model.lower())
        if isinstance(model, discord.abc.Snowflake):
            return f"{model.__class__.__name__}_{model.id}".lower()
        raise TypeError(f"Type de modèle invalide : {type(model)}")

    # --- Dossiers ---

    def get_subfolder(self, name: str, *, create: bool = False) -> Path:
        """Renvoie le chemin du sous-dossier `name` du cog."""
        folder = self.cog_folder / name
        if create:
            folder.mkdir(parents=True, exist_ok=True)
        return folder

    @property
    def assets_path(self) -> Path:
        return self.get_subfolder("assets", create=True)

    # --- Définitions de tables ---

    def link(self, model_type: type[discord.abc.Snowflake] | str = "global", *builders: TableBuilder) -> None:
        """Lie des définitions de tables à un type de modèle (ex: `discord.Guild`) ou à une base nommée.

        :param model_type: Type du modèle ou nom d'une base "globale" (par défaut `"global"`)
        :param builders: Définitions de tables à créer/vérifier à la connexion
        """
        key = model_type.lower() if isinstance(model_type, str) else model_type
        self.__builders[key] = builders

    def _linked_builders(self, model_type: type[discord.abc.Snowflake] | str) -> tuple[TableBuilder, ...]:
        key = model_type.lower() if isinstance(model_type, str) else model_type
        return self.__builders.get(key, ())

    # --- Accès aux modèles ---

    def get(self, model: discord.abc.Snowflake | str) -> ModelDataManager:
        """Renvoie (en le créant si besoin) le gestionnaire de données du modèle spécifié.

        :param model: `discord.Guild`, `discord.User`, ou une chaîne (ex: `"global"`) pour une base nommée
        """
        key = model.lower() if isinstance(model, str) else model
        if key not in self.__managers:
            model_type = type(model) if isinstance(model, discord.abc.Snowflake) else key
            builders = self._linked_builders(model_type)
            db_name = self._model_db_name(key)
            path = self.cog_folder / "data" / f"{db_name}.db"
            self.__managers[key] = ModelDataManager(key, path, builders=builders)
        return self.__managers[key]

    def get_all(self) -> list[ModelDataManager]:
        return list(self.__managers.values())

    async def close(self, model: discord.abc.Snowflake | str) -> None:
        key = model.lower() if isinstance(model, str) else model
        manager = self.__managers.pop(key, None)
        if manager is not None:
            await manager.close()

    async def close_all(self) -> None:
        for manager in self.__managers.values():
            await manager.close()
        self.__managers.clear()

    async def delete(self, model: discord.abc.Snowflake | str) -> None:
        key = model.lower() if isinstance(model, str) else model
        await self.close(key)
        db_path = self.cog_folder / "data" / f"{self._model_db_name(key)}.db"
        if db_path.exists():
            db_path.unlink()

    async def delete_all(self) -> None:
        await self.close_all()
        for db_path in (self.cog_folder / "data").glob("*.db"):
            db_path.unlink()


# INSTANCES ===================================================================

def get_instance(cog: commands.Cog | str) -> CogData:
    """Renvoie (en le créant si besoin) le gestionnaire de données du cog spécifié.

    :param cog: Instance de cog ou nom de cog
    """
    cog_name = cog.lower() if isinstance(cog, str) else cog.qualified_name.lower()
    if cog_name not in __COGDATA_INSTANCES:
        __COGDATA_INSTANCES[cog_name] = CogData(cog_name)
    return __COGDATA_INSTANCES[cog_name]


def get_resource_path(path: str | Path) -> Path:
    """Renvoie le chemin d'une ressource commune (`common/resources/...`)."""
    return COMMON_RESOURCES_PATH / path
