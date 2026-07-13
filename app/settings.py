import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings:
    base_dir = BASE_DIR
    data_dictionary = BASE_DIR / "EPVRPPS151_DataDictionary_2024-08-09.csv"
    spanish_translation = BASE_DIR / "v1.4.1_Spanish_Translation.csv"
    upload_template = BASE_DIR / "participant_study_descriptor_upload_template_v1_3.csv"
    database_path = Path(os.environ.get("RPPS_DATABASE_PATH", BASE_DIR / "rpps.sqlite3"))
    template_dir = str(BASE_DIR / "app" / "templates")
    static_dir = str(BASE_DIR / "app" / "static")
    session_cookie = "rpps_admin_session"
    login_csrf_cookie = "rpps_login_csrf"
    session_ttl_seconds = int(os.environ.get("RPPS_SESSION_TTL_SECONDS", "28800"))
    cookie_secure = os.environ.get("RPPS_COOKIE_SECURE", "false").lower() in ("1", "true", "yes")


settings = Settings()
