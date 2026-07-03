# M8

Bot Discord modulaire (discord.py) organisé en cogs indépendants, chacun avec sa propre persistance SQLite et ses panneaux de configuration en Components V2 (`LayoutView`).

---

## Fonctionnalités

- **MsgBoard** — compile les meilleurs messages d'un serveur dans un salon dédié dès qu'un seuil de votes par réaction (emoji configurable) est atteint ; ajout manuel possible via menu contextuel.
- **Colors** — attribution de rôles de couleur personnalisés (`/color set`, `/color remove`) par code hexadécimal, nom (autocomplétion), ou couleur dominante d'un avatar ; génération de palettes d'images (`/palette`).
- **Quotes** — génère une image de citation (mono ou multi-auteurs) à partir d'un ou plusieurs messages sélectionnés via menu contextuel ; citations aléatoires Inspirobot (`/quote`).
- **Birthdays** — chaque membre renseigne sa date de naissance dans un panneau personnel (`/birthday`) ; annonce automatique quotidienne des anniversaires du jour dans un salon configurable, et consultation des prochains anniversaires triés (`/birthdays`).

Chaque cog expose son propre panneau de configuration réservé à la modération (`/msgboardconfig`, `/colorconfig`, `/birthdayconfig`).

## Administration

Commandes propriétaire (préfixe `&`) : `ping`, `restart`, `update` (git pull + redémarrage), `shutdown`, gestion des cogs (`cogs`, `load`, `unload`, `reload`) et synchronisation des slash commands (`sync`).

## Stack

- [discord.py](https://discordpy.readthedocs.io/) — interface Discord (Components V2 / `LayoutView`)
- aiosqlite — persistance locale, une base par cog/serveur/utilisateur
- Pillow, numpy, opencv-python-headless, colorgram.py — génération et analyse d'images
- python-dotenv — configuration via `.env`

## Licence

[MIT](LICENSE) — Acrone, 2026
