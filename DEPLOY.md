# Deploying (Railway)

Once deployed, the hourly scan, the cold-mail sender and the 8 PM digest run 24/7 without your laptop. The app runs as one always-on container; the database stays on Neon.

**Cost:** Railway's Hobby plan is about $5/month and this app should mostly fit inside its included usage. (Render works too: see the end. Its free tier sleeps, which would stop the scheduler, so it needs the $7/month Starter instance.)

## What the server needs

- The code from GitHub (Railway builds the `Dockerfile`; `railway.json` sets the health check and one replica).
- Environment variables (below). Nothing secret is in the repo or the image: `.env`, `credentials.json` and `token*.json` are excluded by `.gitignore` and `.dockerignore`.
- Inbox logins are already in the database, encrypted, so **no token files** go to the server. They only work with the **same `TOKEN_ENCRYPTION_KEY`** as your local `.env`.

Each deploy runs `alembic upgrade head` before starting, so schema changes apply automatically.

## Steps

1. **Create the project.** Go to [railway.com](https://railway.com) and sign in with GitHub. Click **New Project → Deploy from GitHub repo** and pick `job-tracker`; allow the Railway GitHub app to see that repo if asked. Railway starts a first build. It will fail health checks until the variables are set, which is expected.

2. **Get the public address.** In the service, open **Settings → Networking → Generate Domain**. Copy the address, e.g. `https://job-tracker-production-abcd.up.railway.app`.

3. **Set the variables.** On your Mac, build the list from your `.env` straight into the clipboard (it contains secrets, so it isn't printed):

   ```bash
   python -m scripts.deploy_env --url https://YOUR-APP.up.railway.app | pbcopy
   ```

   In Railway, open **Variables → Raw Editor**, paste, and **Update Variables**. This includes `APP_ENV=production` (HTTPS-only cookies), `PUBLIC_BASE_URL`, `SCHEDULER_ENABLED=true`, and `GOOGLE_OAUTH_CLIENT_JSON` (the contents of `credentials.json`). Railway redeploys automatically.

4. **Allow the new address in Google.** In Google Cloud → Google Auth Platform → **Clients** → your web client → **Authorized redirect URIs**, add `https://YOUR-APP.up.railway.app/oauth/google/callback` (keep the localhost ones), then **Save**.

5. **Stop the local scheduler.** Only one copy of the app should run the jobs, or you'd get two 8 PM digests and double scans. Either stop `uvicorn` on your Mac, or set `SCHEDULER_ENABLED=false` in your local `.env` if you still want to open the dashboard locally.

6. **Check it.**
   - `https://YOUR-APP.up.railway.app/healthz` shows `{"status":"ok"}`.
   - Log in with your dashboard password; the **Inboxes** page lists your connected inboxes.
   - **Deploy Logs** show `Tracker every 60 min; digest daily at 20:00 America/New_York`, and the next hourly scan logs a line per inbox.

## Updating

Push to GitHub; Railway rebuilds and redeploys, running any new migrations first.

```bash
git push
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Health check fails, logs show `DATABASE_URL is empty` or similar | A variable is missing; repeat step 3 |
| Inboxes show "needs attention: can't be decrypted" | `TOKEN_ENCRYPTION_KEY` on Railway differs from your local one; copy it exactly |
| Google says `redirect_uri_mismatch` when connecting an inbox | Step 4 (the exact `https://…/oauth/google/callback` URL) |
| Logged out after every click | `APP_ENV=production` requires HTTPS; use the `https://` address |
| Two digests at 8 PM | The local app is still running its scheduler (step 5) |

## Render instead

New → **Web Service** → connect the repo → Runtime **Docker** → instance **Starter** → Health check path `/healthz` → add the same variables (Environment → Add from .env, pasting the `deploy_env` output with the Render URL) → add `https://YOUR-APP.onrender.com/oauth/google/callback` in Google Cloud.
