# Job Application Tracker

Scans your Gmail every hour, detects job-application emails, tracks each application's status, shows everything on a private dashboard, pings you on Telegram the moment an interview invite lands, and sends a daily digest at 8 PM. It also includes a cold-mailing tool: it drafts a personalized email for each recipient and sends each one individually, only after you approve it.

The full spec lives in [CLAUDE.md](CLAUDE.md).

## How it works

```
Gmail (readonly, incremental via historyId)
  → prefilter (ATS senders / keywords, no LLM)
  → classify (Claude → structured JSON)
  → match (thread_id → fuzzy company+role → pgvector)
  → update_db (status state machine + status_events)
  → notify (Telegram on interview invites)
```

Status flow: `Applied → Assessment → Interview → Offer`, with `Rejected` reachable from any state. Low-confidence (< 0.75) or ambiguous emails go to a review queue instead of being guessed.

## Requirements

- Python 3.11+ (developed on 3.12)
- A Postgres database with the `pgvector` extension (Neon or Supabase free tier works)
- A Google account with Gmail
- An Anthropic API key
- A Telegram account (for alerts)

## Setup

### 1. Install

```bash
cd job-tracker
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure secrets

```bash
cp .env.example .env
```

Then fill in `.env`:

| Variable | Where it comes from |
|---|---|
| `DATABASE_URL` | Neon/Supabase connection string, using the `postgresql+psycopg://` scheme |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
| `DASHBOARD_PASSWORD_HASH` | `python scripts/hash_password.py` |
| `SESSION_SECRET` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | BotFather (see step 5 of the build) |
| `SLACK_WEBHOOK_URL` | optional, Slack incoming webhook |

Gmail OAuth files (`credentials.json`, `token.json`, `token_send.json`) live in the project root and are git-ignored. Setup instructions for these are added in build step 1.

### 3. Database

Enable pgvector once in your database's SQL editor:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Run migrations (available from build step 3):

```bash
alembic upgrade head
```

### 4. Run

```bash
uvicorn app.main:app --reload
```

Open http://localhost:8000/healthz to check that the server is up.

### 5. Tests

```bash
pytest
```

## Project layout

```
app/
  config.py      typed settings from .env
  main.py        FastAPI app
  gmail/         OAuth, incremental fetcher, prefilter
  classifier/    Claude email classifier
  pipeline/      LangGraph tracker graph + scheduler
  db/            SQLAlchemy models, matcher, state machine
  api/           REST endpoints + auth
  notify/        Telegram, Slack, digest
  outreach/      cold mailing: resume parsing, drafting, throttled sender
  web/           Jinja2 templates + static assets
alembic/         migrations
eval/            labeled emails + precision/recall script
tests/           pytest suite
```

## Safety rules

- Secrets only in `.env`. `.env`, `credentials.json`, and `token*.json` are git-ignored.
- The tracker uses `gmail.readonly` only and never sends, modifies, or deletes mail.
- The `gmail.send` scope uses a separate token and is used only by the cold-mail sender, only for drafts you approved.
- Full email bodies are never logged.
- Re-processing the same Gmail message is idempotent.
