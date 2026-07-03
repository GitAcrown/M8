# Maria

Bot Discord GPT conçu pour s'intégrer naturellement dans une communauté — pas un assistant, quelqu'un qui est là.

Personnalité directe et Gen Z, Maria s'adapte au ton de chaque salon, retient les préférences des membres, et cherche sur le web quand elle ne sait pas.

---

## Ce qu'elle fait

- **Conversation** — contexte restreint par salon, mémoire utilisateur persistante (profil + notes dynamiques)
- **Recherche web** — Brave Search (+ fallback DuckDuckGo) avec crawling de pages
- **Rappels** — planification et envoi de rappels personnalisés
- **Médias** — analyse d'images, transcription audio
- **Personnalité par salon** — configurable par la modération via `/chatbot personality`

## Stack

- [discord.py](https://discordpy.readthedocs.io/) — interface Discord
- [OpenAI](https://platform.openai.com/) — `gpt-5.4-mini` (principal) · `gpt-5.4-nano` (recherche contexte) · `gpt-4o-transcribe`
- [Brave Search API](https://brave.com/search/api/) — recherche web (optionnel)
- SQLite — persistance locale

## Licence

[MIT](LICENSE) — Acrone, 2026
