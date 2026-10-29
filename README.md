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
| `DASHBOARD_PASSWORD_HASH` | `python -m scripts.hash_password` |
| `SESSION_SECRET` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook (see **Slack alerts** below) |

Gmail OAuth files (`credentials.json`, `token.json`, `token_send.json`) live in the project root and are git-ignored. See **Gmail API setup** below.

### Gmail API setup (Google Cloud Console)

You do this once. Use the Google account whose inbox you want tracked.

1. **Create a project.** Go to https://console.cloud.google.com/, open the project picker (top bar) → **New project** → name it `job-tracker` → **Create**, then select it.
2. **Enable the Gmail API.** Go to **APIs & Services → Library**, search for "Gmail API", open it, and click **Enable**.
3. **Configure the consent screen.** Go to **Google Auth Platform** (also reachable as **APIs & Services → OAuth consent screen**) and click **Get started**:
   - App name: `job-tracker`; user support email: your address
   - Audience: **External**
   - Contact email: your address → agree → **Create**
4. **Add scopes.** Under **Data Access → Add or remove scopes**, tick `.../auth/gmail.readonly` and `.../auth/gmail.send` → **Update** → **Save**.
5. **Add yourself as a test user.** Under **Audience → Test users → Add users**, enter your Gmail address and save.
6. **Avoid weekly re-logins (recommended).** While the app is in *Testing*, Google expires refresh tokens after 7 days, which would break the hourly job. Under **Audience**, click **Publish app** and confirm. You do not need verification for your own account: at login you will see "Google hasn't verified this app". Click **Advanced → Go to job-tracker (unsafe)**. It is your own app.
7. **Create the OAuth client.** Under **Clients → Create client**, set Application type **Web application** and name it `job-tracker-web`:
   - Authorized JavaScript origins: leave empty
   - Authorized redirect URIs → **Add URI**: `http://localhost:8080/` (include the trailing slash; the port matches `GMAIL_OAUTH_PORT`)
   - **Create** → **Download JSON**. Save the file as `job-tracker/credentials.json`.

   (A **Desktop app** client also works and needs no redirect URI. When the app is deployed in step 9, you'll add the deployed URL's callback as a second redirect URI on this same client.)
8. **Authorize the tracker (read-only):**
   ```bash
   python -m scripts.gmail_auth readonly
   ```
   A browser tab opens. Approve the read-only access, and `token.json` is written. The `send` token is created later, in build step 8 (`python -m scripts.gmail_auth send`), so until then the app holds no permission to send mail.
9. **Dry run.** This shows which recent emails pass the prefilter. It is read-only and prints no email bodies:
   ```bash
   python -m scripts.gmail_dry_run --preview-days 14
   python -m scripts.gmail_dry_run --preview-days 14 --show-dropped
   ```

**How fetching works:** the first run backfills `INITIAL_BACKFILL_DAYS` (default 30). After that, each run reads only changes since the stored Gmail `historyId`. If Google expires that cursor (after roughly a week of downtime), the next run catches up from the last successful run time instead of rescanning the mailbox.

### Classifier evaluation

`eval/labeled_emails.jsonl` holds 100 synthetic, hand-labeled emails modeled on real inbox patterns (25 confirmations, 20 rejections, 16 interview invites, 9 assessments, 6 offers, 10 recruiter outreach, 14 other). With `ANTHROPIC_API_KEY` set in `.env`:

```bash
python -m eval.run_eval --limit 10   # smoke run
python -m eval.run_eval              # full set: precision/recall/F1 per type, company match, confusions
```

Each run makes one Claude call per email. Results written with `--out` go to `eval/results/`, which is git-ignored.

### 3. Database

Enable pgvector once in your database's SQL editor:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Run migrations:

```bash
alembic upgrade head
```

### Run the tracker

One pass now, the same thing the hourly job does:

```bash
python -m scripts.run_tracker
```

The first run backfills `INITIAL_BACKFILL_DAYS` (30) of mail, so it makes one Claude call per email that passes the prefilter (roughly 1-2 cents each). After that, each hourly run only sees new mail.

When the web app is running (`uvicorn app.main:app`), the scheduler runs the tracker every `POLL_INTERVAL_MINUTES` inside the same process (`SCHEDULER_ENABLED=true`). A Postgres advisory lock keeps two processes from running it at once.

**How emails become applications**

- Matching: same Gmail thread → fuzzy company + role → embedding similarity (local `bge-small` model; only used when the email names no company).
- Status: `applied → assessment → interview → offer`, `rejected` from anywhere. Never backward, and an email older than the last status change never changes it.
- Review queue (`emails.needs_review`): confidence < 0.75, classifier errors, ambiguous matches (e.g. a rejection naming a company you applied to twice), activity after a rejection, or no company named.
- Interview reschedules update the pending interview instead of adding one; a new invite after the interview date is a new round.
- Recruiter outreach and other emails are stored (so they're never re-classified) but never become applications.

### Slack alerts and daily digest

Interview invites post to Slack right away (company, role, time in your timezone, meeting link); reschedules post as "Interview rescheduled". At `DIGEST_HOUR` (20 = 8 PM, `TIMEZONE`) a digest lists what moved forward, upcoming interviews for the next 7 days, new applications, rejections, and how many items wait for review. Changes are timed by the email that caused them, so a backfill doesn't flood the digest.

Setup (about 3 minutes):

1. In Slack, pick or create a channel for alerts (e.g. `#job-search`). A free workspace works.
2. Go to https://api.slack.com/apps → **Create New App** → **From scratch** → name it `Job Tracker`, pick your workspace → **Create App**.
3. **Incoming Webhooks** (left sidebar) → turn **Activate Incoming Webhooks** on → **Add New Webhook** (bottom of page) → choose the channel → **Allow**.
4. Copy the **Webhook URL** (`https://hooks.slack.com/services/...`). Treat it like a password: anyone with it can post to that channel.
5. Save it and send a test message:
   ```bash
   python -m scripts.set_secret SLACK_WEBHOOK_URL
   python -m scripts.send_digest --test
   ```

Other commands: `python -m scripts.send_digest --preview` prints today's digest without sending; `python -m scripts.send_digest` sends it now. Without a webhook, alerts are only logged and the digest is skipped.

### 4. Run

Set the dashboard password once (stores a bcrypt hash in `.env` and creates `SESSION_SECRET`):

```bash
python -m scripts.hash_password
```

```bash
uvicorn app.main:app --reload
```

Open http://localhost:8000/healthz to check that the server is up, and http://localhost:8000/docs for the interactive API docs.

### API

Every endpoint except `/healthz` needs a login. `POST /auth/login` (form fields `username`, `password`) sets a signed, HTTP-only session cookie (SameSite=Lax, Secure in production) valid for 14 days; five failed attempts lock a client out for 15 minutes.

| Endpoint | Purpose |
|---|---|
| `GET /applications` | List; filters `status` (repeatable), `needs_review`, `q` (company/role search), `sort`, `limit`, `offset` |
| `GET /applications/{id}` | One application with its status timeline, linked emails (with Gmail links) and interviews |
| `PATCH /applications/{id}` | Edit company, role, applied date, status (manual changes are trusted, even backward), or clear `needs_review` |
| `GET /stats` | Status counts, cumulative funnel, response and interview rates, last 12 weeks, upcoming interviews |
| `GET /review-queue` | Emails and applications flagged for review |
| `POST /review-queue/emails/{id}` | Resolve one: `dismiss`, `link` to an application, or `create` a new one |

The cold-mailing endpoints (`/resumes`, `/outreach/...`) arrive with build steps 7 and 8.

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
