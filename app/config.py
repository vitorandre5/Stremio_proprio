import os


TMDB_API_TOKEN = os.getenv("TMDB_API_TOKEN", "").strip()
TMDB_LANGUAGE = os.getenv("TMDB_LANGUAGE", "pt-BR").strip() or "pt-BR"
DATABASE_URL = os.getenv(
    "DATABASE_URL", "sqlite:////data/media_library.sqlite3"
).strip()
FROST_MANIFEST_URL = os.getenv(
    "FROST_MANIFEST_URL", "https://froststream.cloutteam.com/manifest.json"
).strip()
BESTCINE_MANIFEST_URL = os.getenv(
    "BESTCINE_MANIFEST_URL", "https://bestcine.dpdns.org/manifest.json"
).strip()
MEDIA_ROOT = os.getenv("MEDIA_ROOT", "/media/stream media").strip()
JELLYFIN_URL = os.getenv("JELLYFIN_URL", "").strip().rstrip("/")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "").strip()
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").strip().lower() not in {"0", "false", "no"}
