# GoodServiceBot on Replit

This is a Python 3.12 Telegram polling bot using aiogram and SQLite, not a web application. Keep the existing project structure and database.

## Configuration

Provide these through Replit Secrets, not source files or chat:

- `BOT_TOKEN`: the bot token from Telegram's @BotFather.
- `ADMIN_IDS`: one or more numeric Telegram administrator IDs, separated by commas.

Optional environment variables are documented in README.md. Defaults include
`DB_PATH=data/bot.sqlite3` and `TIMEZONE=Europe/Moscow`.
No `.env` file is needed on Replit.

## Run

Use the **Telegram bot** console workflow, which runs `python -m bot` from the
project root. There is no HTTP port or browser preview; interact with the bot
in Telegram. A successful startup logs a Telegram connection and starts polling.
Run only one polling instance per token; stop any copy running elsewhere.

SQLite initializes and seeds itself on first startup. The local database is
ignored by Git. Do not delete it when restarting the bot. This development
setup does not configure production hosting or database backups.

## Dependencies and tests

Runtime dependencies are in `requirements.txt`; test dependencies are in
`requirements-dev.txt`. Use Replit's Python package management without a
virtual environment.

Run tests with `python -m pytest -q`. Tests use temporary databases and do not
require a live Telegram token.
