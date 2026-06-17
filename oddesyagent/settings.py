import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_allowed_user_ids(raw: str) -> list[int]:
    allowed_user_ids: list[int] = []
    for item in raw.split(","):
        stripped = item.strip()
        if not stripped:
            continue
        allowed_user_ids.append(int(stripped))
    return allowed_user_ids


def resolve_workflows_dir(
    base_dir: Path,
    env_value: str | None = None,
    candidate_paths: list[str | Path] | None = None,
) -> Path:
    if env_value:
        return Path(env_value).resolve()

    candidates = candidate_paths or [
        Path(r"C:\ComfyUI\user\default\workflows"),
        Path(r"C:\ComfyUI\workflows"),
        base_dir / "workflows",
    ]
    resolved_candidates = [Path(candidate).resolve() for candidate in candidates]
    for candidate in resolved_candidates:
        if candidate.is_dir():
            return candidate
    return resolved_candidates[-1]


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY") or os.getenv("SECRET_KEY", "unsafe-development-key")
DEBUG = env_bool("DJANGO_DEBUG", env_bool("DEBUG", True))
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "127.0.0.1,localhost")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.core.apps.CoreConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "oddesyagent.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "oddesyagent.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = Path(os.getenv("ODDESY_MEDIA_ROOT", "") or (BASE_DIR / "media"))

WORKFLOWS_DIR = resolve_workflows_dir(BASE_DIR, os.getenv("ODDESY_WORKFLOWS_DIR"))
DEFAULT_WORKFLOW_NAME = "i2v_wan_480p"
TEXT_TO_IMAGE_WORKFLOW_NAME = os.getenv("ODDESY_TEXT_TO_IMAGE_WORKFLOW_NAME", "jugg_latent_cyberpony")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USER_IDS = parse_allowed_user_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""))

COMFYUI_BASE_URL = os.getenv("COMFYUI_BASE_URL", "http://127.0.0.1:8188")
MVD_REPO_DIR = os.getenv("MVD_REPO_DIR", r"C:\source\python\musicvideo-director")
MVD_PYTHON_EXECUTABLE = os.getenv(
    "MVD_PYTHON_EXECUTABLE",
    r"C:\Users\tompe\.virtualenvs\musicvideo-director\Scripts\python.exe",
)

ODDESY_INTERNAL_API_ENABLED = env_bool("ODDESY_INTERNAL_API_ENABLED", False)
ODDESY_INTERNAL_API_TOKEN = os.getenv("ODDESY_INTERNAL_API_TOKEN", "")

LITELLM_ENABLED = env_bool("LITELLM_ENABLED", False)
LITELLM_MODEL = os.getenv("LITELLM_MODEL", "")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY", "")

ODDESY_SAFE_ROOTS = env_list("ODDESY_SAFE_ROOTS", "")
VAST_API_KEY = os.getenv("VAST_API_KEY", "")
YOUTUBE_UPLOAD_ENABLED = env_bool("YOUTUBE_UPLOAD_ENABLED", False)
