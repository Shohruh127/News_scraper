"""Django settings. One file, one environment — this project runs on one server."""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    PUBLISHING_ENABLED=(bool, False),
    OLLAMA_FAST_TIMEOUT=(int, 60),
    OLLAMA_DEEP_TIMEOUT=(int, 300),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-only-not-for-production")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["127.0.0.1", "localhost"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_celery_beat",
    "django_celery_results",
    "apps.digest",
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

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

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
    },
]

DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgresql://news_radar:news_radar@127.0.0.1:5433/news_radar",
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", default="Asia/Tashkent")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Celery -----------------------------------------------------------------
# Two queues, two concurrency budgets. Network fetching is I/O bound and can run
# wide; Ollama measured a ceiling of 2 (docs/spike/OLLAMA_BENCHMARK.md §6).
CELERY_BROKER_URL = env("REDIS_URL", default="redis://127.0.0.1:6380/0")
CELERY_RESULT_BACKEND = "django-db"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_ROUTES = {
    "digest.fetch_source": {"queue": "fetch"},
    "digest.triage_article": {"queue": "llm"},
    "digest.classify_article": {"queue": "llm"},
}
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# --- Ollama -----------------------------------------------------------------
OLLAMA_BASE_URL = env("OLLAMA_BASE_URL", default="").rstrip("/")
OLLAMA_FAST_MODEL = env("OLLAMA_FAST_MODEL", default="gemma4:latest")
OLLAMA_DEEP_MODEL = env("OLLAMA_DEEP_MODEL", default="gemma4:31b")
OLLAMA_FAST_TIMEOUT = env("OLLAMA_FAST_TIMEOUT")
OLLAMA_DEEP_TIMEOUT = env("OLLAMA_DEEP_TIMEOUT")
OLLAMA_MAX_CONCURRENCY = 2

# --- Telegram ---------------------------------------------------------------
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_CHANNEL_ID = env("TELEGRAM_CHANNEL_ID", default="")
TELEGRAM_GROUP_ID = env("TELEGRAM_GROUP_ID", default="")
TELEGRAM_ADMIN_CHAT_ID = env("TELEGRAM_ADMIN_CHAT_ID", default="")
PUBLISHING_ENABLED = env("PUBLISHING_ENABLED")

# --- Ingestion --------------------------------------------------------------
USER_AGENT = "news-radar/0.1 (+daily AI digest)"
ARTICLE_MIN_CHARS = 400
ARTICLE_MAX_AGE_DAYS = 7
SOURCE_DEGRADED_AFTER = 3

# --- Ranking (ADR: weights are configuration, not code) ---------------------
# Only dimensions available in M1 classification schema (CONTENT_SCHEMA.md §4).
# technical_significance was removed (double-counted novelty + evidence).
# source_credibility stays at 0.10 to prevent systematic bias against HN/community sources.
RANKING_WEIGHTS = {
    "novelty": 0.35,
    "evidence": 0.30,
    "production_readiness": 0.15,
    "source_credibility": 0.10,
    "audience_relevance": 0.10,
}
DIGEST_MAX_ITEMS = 7
DIGEST_MAX_PER_TOPIC = 2

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        # trafilatura logs at ERROR for conditions that are normal for us: paywalls,
        # bot walls, pages with no article body. We count those ourselves as
        # "unusable" and do not want them in the log as errors.
        "trafilatura": {"level": "CRITICAL", "propagate": False},
    },
}
