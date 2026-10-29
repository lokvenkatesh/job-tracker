# Job Application Tracker — project guide for Claude

## Working agreements
- Build in the order given under BUILD ORDER. After each step: run `pytest`, fix failures, `git commit`.
- Stop and ask the user only for credentials, API keys, OAuth logins, or real decisions.
- Never put secrets in code. All secrets come from `.env` via `app/config.py`; keep `.env.example` in sync.
- Python venv lives at `.venv` (Python 3.12). Run tests with `.venv/bin/pytest`.
- Decisions made with the user that override the spec below: notifications go to **Slack** (incoming webhook), not Telegram (2026-10-29). Claude model defaults are `claude-opus-5-5`.

## Layout
- `app/config.py` — typed settings (pydantic-settings), the only place env vars are read
- `app/main.py` — FastAPI app factory
- `app/gmail/` fetcher + prefilter · `app/classifier/` Claude classifier · `app/pipeline/` LangGraph graph
- `app/db/` models, session, matcher, state machine · `app/api/` routes + auth · `app/notify/` Telegram/Slack
- `app/outreach/` cold mailing · `app/web/` Jinja2 templates + static assets
- `alembic/` migrations · `eval/` labeled emails + eval script · `tests/` pytest

=== PROJECT SPEC ===

GOAL
Scan my Gmail every hour, detect job applications, track their status, show them on a website dashboard, alert me instantly on interview invites, and send a daily digest. Also a cold-mailing tool: I give recipient emails + a job description + my resume, it writes a personalized, human-sounding email for each recipient, and sends each one separately after I approve it.

STACK
- Python 3.11+, FastAPI, LangGraph, Claude API (structured JSON output)
- Postgres + pgvector (Neon or Supabase), SQLAlchemy + Alembic
- APScheduler for the hourly job
- Gmail API (OAuth): gmail.readonly for the tracker, gmail.send for cold mailing only
- Resume parsing: pdfplumber, python-docx
- Notifications: Telegram bot first, then Slack webhook. WhatsApp (Twilio) optional, last
- Frontend: simple, clean dashboard served by FastAPI (Jinja2 + HTMX or a small React app), behind login

TRACKER PIPELINE (LangGraph): fetch -> prefilter -> classify -> match -> update_db -> notify
- fetch: incremental via stored Gmail historyId, never full rescans
- prefilter: pass ATS senders (greenhouse.io, lever.co, myworkdayjobs, ashbyhq, smartrecruiters, icims, linkedin) or keywords (application, interview, unfortunately, next steps, assessment, offer); drop the rest before any LLM call
- classify: Claude returns JSON {type, company, role, interview_datetime, meeting_link, confidence}
  types: application_confirmation, rejection, interview_invite, assessment, offer, recruiter_outreach, other
- match: link email to an application by thread_id -> fuzzy company+role -> pgvector similarity
- update_db: apply state machine, write a status_event
- notify: interview_invite -> instant Telegram message (company, role, time, link)
- separate scheduled job: daily digest at 8 PM (new applications, rejections, upcoming interviews)

STATUS STATE MACHINE
Applied -> Assessment -> Interview -> Offer; Rejected reachable from any state.
- never move status backward based on an older email
- confidence < 0.75 or ambiguous match -> needs_review, never guess
- interview reschedules update the existing interview, no duplicates
- recruiter_outreach stored separately, not counted as an application

COLD MAILING
Input (dashboard form): recipient list (emails, optional name/company/title, paste or CSV), job description (text), resume (PDF/DOCX upload, reuse latest by default), optional notes.
Generation (Claude):
- extract role, company, top 3-4 skills from the JD
- extract matching experience and 1-2 concrete wins with numbers from the resume
- ONE email per recipient, personalized with their name/title/company
- 90-150 words, warm, specific, one clear ask (15-min chat or referral), sounds like a real person
- banned phrases: "I hope this email finds you well", "I am writing to express my interest", "I'm excited to apply", buzzword lists, more than one exclamation mark
- vary opening lines and phrasing so no two emails are identical
- short specific subject line per email
- never invent experience, metrics, or connections not in my resume or notes
Review and send:
- preview screen for every draft: edit, regenerate, or skip each
- nothing sends without my explicit approval (per email or approve all)
- send each email individually via Gmail API, never To/CC/BCC lists
- optional resume attachment toggle
- random 60-180 second gap between sends, cap 30 per day (configurable)
- validate emails, skip duplicates, skip anyone emailed in the last 30 days unless I override
Tracking:
- log every sent email, status outreach_sent
- hourly tracker detects replies on the same thread -> outreach_replied
- suggest a follow-up draft after 5 business days with no reply (needs my approval)

DATABASE
- applications: id, company, role, status, applied_date, last_update, source, needs_review
- emails: gmail_id (unique), thread_id, application_id, type, received_at, confidence, embedding
- status_events: id, application_id, old_status, new_status, email_id, created_at
- sync_state: last_history_id, last_run_at
- resumes: id, filename, parsed_text, uploaded_at, is_default
- outreach_campaigns: id, jd_text, company, role, resume_id, notes, created_at
- outreach_emails: id, campaign_id, recipient_email, recipient_name, subject, body, status (draft/approved/sent/skipped/replied), gmail_message_id, thread_id, sent_at, application_id

API (all endpoints require auth)
- GET /applications, GET /stats (funnel, response rate), PATCH /applications/{id}, GET /review-queue
- POST /resumes, POST /outreach/campaigns, GET /outreach/campaigns/{id}
- PATCH /outreach/emails/{id}, POST /outreach/emails/{id}/regenerate
- POST /outreach/campaigns/{id}/send (approved only, throttled background job)

DASHBOARD
- applications table + kanban by status, funnel stats, timeline, review queue
- cold mailing page: form, draft preview/edit/approve, sent history with reply status
- responsive, light and dark mode

RULES
- secrets only in .env (Gmail creds, Claude API key, DB URL, Telegram token); .gitignore must exclude .env, credentials.json, token.json
- never log full email bodies
- tracker never sends, modifies, or deletes mail; only the cold-mail sender uses gmail.send, only for approved drafts
- idempotent: re-processing the same gmail_id never creates duplicates
- pytest tests for classifier parsing, matcher, state machine, cold-mail throttling and dedupe
- eval/labeled_emails.jsonl (~100 emails) + script reporting precision/recall per type

BUILD ORDER
0. Project structure, requirements.txt, .env.example, .gitignore, README with setup instructions
1. Gmail OAuth + incremental fetcher + prefilter (walk me through Google Cloud Console setup)
2. Classifier + eval script
3. DB models, migrations, matcher, state machine, hourly scheduler
4. FastAPI endpoints + auth
5. Telegram alerts + daily digest (walk me through BotFather setup)
6. Dashboard frontend
7. Cold mailing: resume upload/parsing, campaign + draft generation, review screen
8. Cold mailing: throttled sender, reply tracking, follow-up drafts
9. Slack webhook, deployment guide (Railway or Render)

=== END SPEC ===
