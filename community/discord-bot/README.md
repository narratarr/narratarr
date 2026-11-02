# Narratarr Discord bot

The bot behind [Narratarr's Discord](https://discord.gg/kB5EgmfBrA). It does one thing: react with 🎧 on the pinned welcome post in #welcome and you get the **Listener** role, which lets you post. Take the reaction off and the role goes too. Any other reaction on that post is removed. When it starts, it gives the role to anyone who reacted while it was off.

## Run it

```bash
echo "DISCORD_TOKEN=paste-the-bot-token-here" > .env && chmod 600 .env
docker compose up -d --build
docker logs -f narratarr-discord   # "Watching 🎧 on the welcome post…"
```

It needs View Channels, Read Message History, Add Reactions, Manage Messages and Manage Roles, and its role must sit above **Listener**. No privileged intents.

Settings (optional, in `.env`): `GATE_EMOJI` (default 🎧), `GATE_ROLE` (default Listener), `WELCOME_CHANNEL` (a word in the channel's name, default welcome).
