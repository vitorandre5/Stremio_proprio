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
FENIXFLIX_MANIFEST_URL = os.getenv(
    "FENIXFLIX_MANIFEST_URL",
    "https://fenixflix.fenixhub.online/qualities=4k,1080p,720p,sd%7Caudio=dublado,legendado%7Ccatalogs=populares_movie,populares_series,recentes_movie,recentes_series/manifest.json",
).strip()
BRAZUCA_TORRENTS_MANIFEST_URL = os.getenv(
    "BRAZUCA_TORRENTS_MANIFEST_URL",
    "https://94c8cb9f702d-brazuca-torrents.baby-beamup.club/manifest.json",
).strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://media.melhornegocio.shop").strip().rstrip("/")
MEDIA_ROOT = os.getenv("MEDIA_ROOT", "/media/stream media").strip()
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024 * 1024)))
MAX_TORRENT_BYTES = min(int(os.getenv("MAX_TORRENT_BYTES", str(20 * 1024 * 1024 * 1024))), 20 * 1024 * 1024 * 1024)
LINK_CHECK_INTERVAL_SECONDS = int(os.getenv("LINK_CHECK_INTERVAL_SECONDS", "300"))
LINK_CHECK_TIMEOUT_SECONDS = float(os.getenv("LINK_CHECK_TIMEOUT_SECONDS", "15"))
JELLYFIN_URL = os.getenv("JELLYFIN_URL", "").strip().rstrip("/")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "").strip()
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").strip().lower() not in {"0", "false", "no"}
