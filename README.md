# Software House Lead Engine — Full-Stack Web App

A self-hosted **FastAPI + PostgreSQL + Tailwind Dashboard** web application that monitors high-intent project orders and client requests across **Reddit, HackerNews, X, and Threads**, scored and drafted by **Gemini AI**.

---

## Features
- **Multi-Source Fetching**:
  - **Reddit Real-time**: Near real-time fresh posts via Arctic Shift public archive (no API keys required).
  - **HackerNews**: Live Ask HN and hiring posts via Algolia API (no API keys required).
  - **Serper Google Search**: Scans Reddit, Threads, Telegram, and X for clients actively hiring with budgets.
  - **X (Twitter) v2**: Recent tweet search for relevant client project queries.
- **Gemini AI Scoring & Reply Drafting**: Evaluates buying intent (1-10) with structured output, categorized by project type (MVP, Web App, Mobile App, Custom Software, Design/Art, Marketing/SEO, Arduino/IoT), and generates value-first, non-pushy replies.
- **FastAPI REST API**: Endpoints for filtering leads (`/api/leads`), triggering sync (`/api/sync`), and adjusting engine search queries on the fly (`/api/settings`).
- **Modern Dark Dashboard**: Clean Tailwind dashboard with search, platform/status/score filters, one-click reply copy, and review status tracking.
- **Discord Webhook Alerts**: Immediate notification on Discord for qualified leads (score 7+).
- **One-Command Deployment**: Docker Compose setup with persistent PostgreSQL and health checks.

---

## Project Structure
- `app/main.py` — FastAPI application, REST endpoints, and background scheduler.
- `app/worker.py` — Lead collection engines, Gemini AI scoring with automatic fallback, and Discord alerts.
- `app/db.py` — PostgreSQL connection pool and schema migrations (`social_leads`, `app_settings`).
- `app/static/index.html` — Responsive dark mode dashboard UI.
- `docker-compose.yml` & `Dockerfile` — Production deployment configuration.
- `deploy.sh` — Automated deployment script for VPS (Docker + Compose + setup).

---

## Deploy on VPS (Contabo / Ubuntu / Debian)

### 1. Clone the repository
```bash
git clone https://github.com/codexplussite-lab/sale.git lead-engine-webapp
cd lead-engine-webapp
```

### 2. Configure `.env`
```bash
cp .env.example .env
nano .env
```
Fill in your `GEMINI_API_KEY`, database password, and any optional keys (`SERPER_API_KEY`, `X_BEARER_TOKEN`, `DISCORD_WEBHOOK_URL`).

### 3. Run Deploy Script
```bash
chmod +x deploy.sh
./deploy.sh
```

### 4. Access the Dashboard
Open `http://YOUR_VPS_IP:8000` in your browser. (Ensure port `8000` is allowed: `sudo ufw allow 8000/tcp`).
