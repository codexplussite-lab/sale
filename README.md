# Software House Lead Engine — Full-Stack Web App

A self-hosted **FastAPI + PostgreSQL + Tailwind Dashboard** web application that replaces the n8n workflow and gives you a live UI to review, edit, and track Reddit + X leads scored by **Gemini 2.5 Flash**.

## Project Structure
- `app/main.py` — FastAPI server, REST API (`/api/leads`, `/api/sync`, `/api/settings`), and 20-minute background scheduler.
- `app/worker.py` — Reddit OAuth2 + X API v2 fetcher, Gemini 2.5 Flash scorer/reply drafter, and Discord webhook notifier.
- `app/db.py` — PostgreSQL connection pool and automatic `social_leads` + `app_settings` table setup.
- `app/static/index.html` — Lead Dashboard UI (filter by status/platform/score, edit drafted replies, trigger manual sync, update search queries).
- `docker-compose.yml` & `Dockerfile` — One-command deployment for your Contabo VPS.

## Deploy on Contabo VPS
1. Unzip and open this folder in **Antigravity**.
2. Copy `.env.example` to `.env` and fill in your API keys (`GEMINI_API_KEY`, `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `X_BEARER_TOKEN`, `DISCORD_WEBHOOK_URL`, and `POSTGRES_PASSWORD`).
3. Upload/sync the folder to your Contabo VPS and run:

```bash
chmod +x deploy.sh
./deploy.sh
```

4. Open `http://YOUR_VPS_IP:8000` in your browser (make sure port `8000` is allowed in UFW: `sudo ufw allow 8000/tcp`).
