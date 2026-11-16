import logging
import os
import time
from dotenv import load_dotenv
from psycopg_pool import ConnectionPool

load_dotenv()

logger = logging.getLogger("lead_db")


def get_database_url() -> str:
    if os.getenv("DATABASE_URL"):
        return os.getenv("DATABASE_URL").strip()
    user = os.getenv("POSTGRES_USER", "leaduser").strip()
    password = os.getenv("POSTGRES_PASSWORD", "changeme_strong_password").strip()
    host = os.getenv("POSTGRES_HOST", "localhost").strip()
    port = os.getenv("POSTGRES_PORT", "5432").strip()
    db = os.getenv("POSTGRES_DB", "leads_db").strip()
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


DATABASE_URL = get_database_url()

pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10, open=False)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS social_leads (
  id BIGSERIAL PRIMARY KEY,
  platform TEXT NOT NULL,
  platform_post_id TEXT NOT NULL UNIQUE,
  author_handle TEXT,
  post_url TEXT,
  raw_text TEXT,
  intent_score INTEGER NOT NULL DEFAULT 0,
  project_category TEXT,
  pain_point_summary TEXT,
  drafted_reply TEXT,
  status TEXT NOT NULL DEFAULT 'pending_review',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_social_leads_platform_post_id
  ON social_leads (platform_post_id);

CREATE INDEX IF NOT EXISTS idx_social_leads_status_score
  ON social_leads (status, intent_score DESC);

CREATE INDEX IF NOT EXISTS idx_social_leads_created_at
  ON social_leads (created_at DESC);

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

DEFAULT_SETTINGS = {
    "reddit_query": '("looking for a developer" OR "need an MVP" OR "dev agency" OR "custom software" OR "build a SaaS") NOT ("hiring full-time" OR "internship")',
    "x_query": '("looking for a dev" OR "need an MVP" OR "software agency" OR "build a SaaS" OR "custom ERP") -is:retweet -is:reply lang:en',
    "min_intent_score": os.getenv("MIN_INTENT_SCORE", "7"),
    "sync_interval_minutes": os.getenv("SYNC_INTERVAL_MINUTES", "20"),
}


def init_db(max_retries: int = 5, retry_delay: float = 2.0) -> None:
    for attempt in range(1, max_retries + 1):
        try:
            if not pool.opened:
                pool.open(wait=True)
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(SCHEMA_SQL)
                    for k, v in DEFAULT_SETTINGS.items():
                        cur.execute(
                            "INSERT INTO app_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING;",
                            (k, str(v)),
                        )
                conn.commit()
            logger.info("Database initialized successfully.")
            return
        except Exception as exc:
            if attempt == max_retries:
                logger.error("Failed to initialize database after %s attempts: %s", max_retries, exc)
                raise
            logger.warning(
                "Database not ready (attempt %s/%s): %s. Retrying in %ss...",
                attempt,
                max_retries,
                exc,
                retry_delay,
            )
            time.sleep(retry_delay)


def get_settings_dict() -> dict[str, str]:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM app_settings;")
            return {row[0]: row[1] for row in cur.fetchall()}
