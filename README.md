# CodeXplus Lead Engine (v3 — Local + VPS Ready, Includes `.env`)

## 1. Run Locally (Windows / Mac / Linux)
1. Open the unzipped folder and put your `AGENTROUTER_API_KEY` (and optional `SERPER_API_KEY`) inside `.env`.
2. Install dependencies and start the server:
   ```bash
   pip install -r requirements.txt
   uvicorn app.main:app --reload --port 8000
   ```
3. Open `http://127.0.0.1:8000` and sign in with:
   - **Email:** `admin@codexplus.site`
   - **Password:** `CodeXplus@2026`

## 2. Deploy on Contabo VPS (Without Overwriting Your Server `.env`)
- Because `.gitignore` excludes `.env` and `data/*.db`, pushing from local will **not** overwrite your VPS `.env`.
- On your VPS:
  ```bash
  cd /www/wwwroot/hunt.codexplus.site
  git fetch origin main && git reset --hard origin/main
  docker compose up -d --build
  ```
