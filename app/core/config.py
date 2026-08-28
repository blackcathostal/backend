from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_name: str = "BlackCat API"
    api_prefix: str = "/api"
    app_host: str = "127.0.0.1"
    app_port: int = 9456
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "https://blackcathostal.com",
        "https://www.blackcathostal.com",
        "https://admin.blackcathostal.com",
        "https://blackcathostal-admin-frontend.web.app",
        "https://blackcathostal-admin-frontend.firebaseapp.com",
        "https://www.tripadvisor.cl",
        "https://www.tripadvisor.com",
        "https://tripadvisor.cl",
        "https://tripadvisor.com",
    ]
    database_url: str
    secret_key: str = "blackcat-dev-secret-change-me"
    access_token_expire_minutes: int = 60 * 12
    contact_inbox_email: str = "reservas@blackcathostal.com"
    admin_email: str = "admin@blackcathostal.com"
    admin_password: str = "admin123"
    admin_full_name: str = "Administrator"
    admin_role_name: str = "admin"
    uploads_dir: Path = BASE_DIR / "uploads"
    google_places_api_key: str = ""
    google_place_id: str = ""
    google_place_query: str = "Black Cat Hostal Boutique Compañía de Jesús 1921 Santiago Chile"
    google_reviews_cache_seconds: int = 1800
    google_photos_url: str = (
        "https://www.google.com/maps/place/HOSTAL+BOUTIQUE+BLACK+CAT/"
        "@-33.4397308,-70.6637911,17z/data=!4m9!3m8!1s0x9662c5b8477cf75b:0x9bc2ca30f81b6eff"
        "!5m2!4m1!1i2!8m2!3d-33.4397308!4d-70.6637911!16s%2Fg%2F11h190x96c"
    )
    google_photos_cache_seconds: int = 1800
    tripadvisor_location_url: str = (
        "https://www.tripadvisor.cl/Hotel_Review-g294305-d18941046-Reviews-"
        "Hostal_Boutique_Black_Cat-Santiago_Santiago_Metropolitan_Region.html"
    )
    tripadvisor_photos_url: str = (
        "https://www.tripadvisor.cl/Hotel_Review-g294305-d18941046-Reviews-"
        "Hostal_Boutique_Black_Cat-Santiago_Santiago_Metropolitan_Region.html"
        "#/media/18941046/?type=TRAVELER&albumid=107&category=107"
    )
    tripadvisor_location_id: str = "18941046"
    tripadvisor_api_key: str = ""
    tripadvisor_reviews_cache_seconds: int = 1800
    tripadvisor_photos_cache_seconds: int = 1800

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
settings.uploads_dir.mkdir(parents=True, exist_ok=True)
(settings.uploads_dir / "sliders").mkdir(parents=True, exist_ok=True)
(settings.uploads_dir / "media").mkdir(parents=True, exist_ok=True)
(settings.uploads_dir / "posts").mkdir(parents=True, exist_ok=True)
(settings.uploads_dir / "campaigns").mkdir(parents=True, exist_ok=True)
(settings.uploads_dir / "signatures").mkdir(parents=True, exist_ok=True)
