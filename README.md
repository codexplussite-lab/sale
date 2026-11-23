# CodeXplus Lead Engine (Full Production Bundle)

Complete FastAPI + SQLite + Docker web application with:
- **Authenticated Login & Admin Panel** (`HttpOnly` signed session cookie)
- **4-Key Gemini Failover Pool** (`GEMINI_API_KEY_1` through `GEMINI_API_KEY_4` with automatic rotation, live per-key test buttons, and `responseMimeType: "application/json"` structured schema)
- **Active Gemini Models** with automatic failover (`gemini-2.5-flash`, `gemini-2.0-flash`, `gemini-1.5-flash`, `gemini-2.5-pro`, `gemini-3.5-flash-lite`, `gemini-3.8-flash`)
- **6 Core Agency Service Verticals**: Web/App Dev, E-Commerce, ERP/Portal, UI/UX & Branding, Video/Reels, SMM/SEO
- **Mandatory Search Freshness / Date Filter**: Last 24 Hours (`qdr:d` default for live hunting), Last 7 Days (`qdr:w`), Last 30 Days (`qdr:m`)
- **Strict Duplicate Lead Prevention**: Pre-checks existing URLs and external IDs before calling Gemini to eliminate wasted API quota
- **Multi-Channel Lead Scraper**: Reddit via Arctic Shift + X, LinkedIn, Threads, Facebook, Telegram, Discord, and Twist via Serper.dev
- **Dashboard Filters & Controls**: Channel pills, 6 Service Category filters, Region filters (USA, UK/Europe, UAE/Gulf, Pakistan), Stated Budget `$` filter, Client Pain Point highlights, Tailored Pitch 1-click copy, and manual Gemini reply regeneration

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
3. Open `https://hunt.codexplus.site`, log in with `ADMIN_USERNAME` and `ADMIN_PASSWORD`, and manage your 4 Gemini keys anytime in **Agent Studio & Keys**.
