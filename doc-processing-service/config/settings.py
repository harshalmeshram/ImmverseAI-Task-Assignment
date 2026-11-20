"""Django settings. Everything environment-specific is read from env vars (see .env.example)."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-secret-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()]

INSTALLED_APPS = [
    "rest_framework",
    "documents",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
APPEND_SLASH = False  # endpoints are /documents, /jobs/{id} (no trailing slash)

# --- Database -----------------------------------------------------------------
if os.environ.get("DB_ENGINE", "postgres") == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "docproc"),
            "USER": os.environ.get("POSTGRES_USER", "docproc"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"

# --- REST framework (JSON only, no auth for this assignment) ----------------------
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
}

# --- Application settings -------------------------------------------------------
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
QUEUE_NAME = os.environ.get("QUEUE_NAME", "docproc:jobs")

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", str(BASE_DIR / "data" / "uploads"))
MAX_UPLOAD_SIZE = env_int("MAX_UPLOAD_SIZE", 10 * 1024 * 1024)  # bytes

JOB_MAX_ATTEMPTS = env_int("JOB_MAX_ATTEMPTS", 3)
JOB_RETRY_BACKOFF_SECONDS = env_int("JOB_RETRY_BACKOFF_SECONDS", 5)  # 5s, 10s, 20s ...
JOB_STALE_AFTER_SECONDS = env_int("JOB_STALE_AFTER_SECONDS", 300)

WORKER_POLL_TIMEOUT_SECONDS = env_int("WORKER_POLL_TIMEOUT_SECONDS", 2)
WORKER_SWEEP_INTERVAL_SECONDS = env_int("WORKER_SWEEP_INTERVAL_SECONDS", 30)

# Dotted path to a documents.processing.DocumentProcessor implementation.
DOCUMENT_PROCESSOR = os.environ.get("DOCUMENT_PROCESSOR", "documents.processing.LocalDocumentProcessor")
OCR_LANGUAGE = os.environ.get("OCR_LANGUAGE", "eng")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {"format": "%(asctime)s %(levelname)s [%(name)s] %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "default"},
    },
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}
