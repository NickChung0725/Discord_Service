# Discord Service

## Setup

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
2. Install the project dependencies:

   ```powershell
   uv sync
   ```

3. Copy `.env.example` to `.env` and set `BOT_TOKEN` to your Discord bot token.
4. Start the bot:

   ```powershell
   uv run python bot_server.py
   ```

Enable the **Message Content Intent** for the bot in the Discord Developer Portal.
