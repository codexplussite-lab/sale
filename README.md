# CodeXplus Lead Engine (Full Production Bundle)

Complete FastAPI + SQLite + Docker web application with:
- **Authenticated Login & Admin Panel** (`HttpOnly` signed session cookie)
- **4-Key Gemini Failover Pool** (`GEMINI_API_KEY_1` through `GEMINI_API_KEY_4` with automatic rotation and live per-key test buttons)
- **Active Gemini 2.5 Models** (`gemini-2.5-flash` and `gemini-2.5-pro`)
- **Multi-Channel Lead Scraper** (Reddit via Arctic Shift + X, Threads, Facebook, Telegram, Discord, Twist, and LinkedIn via Serper.dev)
- **Dashboard Filters & Controls** (Channel pills, USA / UK-Europe region filters, Stated Budget `$` filter, manual Gemini reply regeneration, and single/bulk delete)

## Quick Deploy on Contabo VPS

1. Copy `.env.example` to `.env` and set your credentials and API keys:
   ```bash
   cp .env.example .env
   nano .env
   ```
2. Build and start with Docker Compose:
   ```bash
   docker compose up -d --build
   ```
3. Open `https://hunt.codexplus.site`, log in with `ADMIN_USERNAME` and `ADMIN_PASSWORD`, and manage your 4 Gemini keys anytime in **Admin Panel (4 Keys)**.
