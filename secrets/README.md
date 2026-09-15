# secrets/

This folder holds credential and environment config files.

| File | Committed? | Purpose |
|---|---|---|
| `.env.example` | ✅ Yes | Template — copy to `.env` and fill in values |
| `.env` | ❌ No (gitignored) | Real credentials — never commit this |
| `.gitkeep` | ✅ Yes | Keeps the folder tracked in git |

## Setup

```bash
cp secrets/.env.example secrets/.env
# Edit secrets/.env and add your TELEGRAM_BOT_TOKEN
```

## What goes in .env

```
TELEGRAM_BOT_TOKEN=<from @BotFather>
DATABASE_URL=sqlite+aiosqlite:///./flam.db   # default, fine for Phase 1
MAX_DAILY_SUBMISSIONS=20                      # rate cap per day
```

## What never goes here

- Email passwords (out of scope forever — doc 01)
- OAuth tokens in plaintext (Phase 3: stored encrypted, not in .env)
