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
    # Serves the admin's own CSS and JS straight from gunicorn. DEBUG is False here, so
    # without this the admin renders as unstyled HTML and nothing explains why.
    "whitenoise.middleware.WhiteNoiseMiddleware",
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
    "digest.fetch_all_sources": {"queue": "fetch"},
    "digest.fetch_source": {"queue": "fetch"},
    "digest.triage_article": {"queue": "llm"},
    "digest.classify_article": {"queue": "llm"},
    "digest.triage_and_classify": {"queue": "llm"},
    "digest.analyse_for_digest": {"queue": "llm"},
    "digest.compose_and_publish": {"queue": "publish"},
    "digest.dispatch_worker_heartbeats": {"queue": "fetch"},
}
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# --- Ollama -----------------------------------------------------------------
OLLAMA_BASE_URL = env("OLLAMA_BASE_URL", default="").rstrip("/")
OLLAMA_FAST_MODEL = env("OLLAMA_FAST_MODEL", default="gemma4:latest")
OLLAMA_DEEP_MODEL = env("OLLAMA_DEEP_MODEL", default="gemma4:31b")
OLLAMA_FAST_TIMEOUT = env("OLLAMA_FAST_TIMEOUT")
OLLAMA_DEEP_TIMEOUT = env("OLLAMA_DEEP_TIMEOUT")
OLLAMA_MAX_CONCURRENCY = 2

# --- Editorial provider (ADR-004 §5) ----------------------------------------
# Scoped to the editorial stage only. Triage and classification always run on
# local Ollama. Set LLM_PROVIDER=ollama to revert; that is the whole change.
#
# The MiMo Token Plan forbids automated/backend use. This is accepted by the
# project owner for the testing phase only, with a stated intention to move to a
# local model or a backend-permitted tier before release. See ADR-004 §5.
# Per-stage providers. Measured 2026-08-17 on the seven live digest items:
#
#   English analysis   MiMo    real reasoning; gemma4:31b garbled Uzbek and was slower
#   Translation        Ollama  gemma4:latest lost 0/7 numbers and kept the glossary;
#                              mimo-v2.5 changed 2.4 trillion to 2 trillion and
#                              calqued open-weight in two posts
#
# Translation is a constrained task: the input is fixed and the output shape is fixed.
# A stronger model spends its extra freedom changing things, and in translation any
# change is an error. Heavy reasoning to the heavy model, fidelity to the local one.
LLM_PROVIDER = env("LLM_PROVIDER", default="ollama")
EDITORIAL_EN_PROVIDER = env("EDITORIAL_EN_PROVIDER", default=LLM_PROVIDER)
TRANSLATION_PROVIDER = env("TRANSLATION_PROVIDER", default="ollama")
#: Uzbek tokenises poorly, so a 1200-token cap truncated the JSON mid-object and the
#: whole translation was lost. Measured: 2500 gives 7/7 twice, 1200 gave 2/7.
TRANSLATION_NUM_PREDICT = env.int("TRANSLATION_NUM_PREDICT", default=2500)
MIMO_BASE_URL = env("MIMO_BASE_URL", default="").rstrip("/")
MIMO_API_KEY = env("MIMO_API_KEY", default="")
MIMO_FAST_MODEL = env("MIMO_FAST_MODEL", default="mimo-v2.5")
MIMO_DEEP_MODEL = env("MIMO_DEEP_MODEL", default="mimo-v2.5-pro")
#: mimo-v2.5 measured clean Uzbek in ADR-004 §5; pro is not required for summaries.
MIMO_EDITORIAL_MODEL = env("MIMO_EDITORIAL_MODEL", default="mimo-v2.5")
MIMO_TIMEOUT = env.int("MIMO_TIMEOUT", default=120)

# --- Internal LLM gateway ----------------------------------------------------
# OpenAI-compatible front door to the same local GPU models. Callers name a tier alias
# (`fast`/`smart`) rather than a model, so a tier can be repointed without a redeploy;
# sending a real model name is a 404. This runs alongside the direct Ollama path for the
# whole migration — the direct path goes away only once every stage has moved.
GATEWAY_BASE_URL = env("GATEWAY_BASE_URL", default="").rstrip("/")
GATEWAY_TOKEN = env("GATEWAY_TOKEN", default="")
GATEWAY_FAST_MODEL = env("GATEWAY_FAST_MODEL", default="fast")
GATEWAY_SMART_MODEL = env("GATEWAY_SMART_MODEL", default="smart")
#: The gateway's own upstream read timeout is 300s, and a request also waits in its queue
#: for up to 30s. A shorter client timeout abandons a generation the gateway still runs.
GATEWAY_TIMEOUT = env.int("GATEWAY_TIMEOUT", default=300)

#: Triage and classification. Defaults to "ollama", not LLM_PROVIDER: these two stages
#: were hardwired to local Ollama, and inheriting the global default would silently move
#: several hundred calls a day onto whatever the editorial stage happens to use.
CLASSIFIER_PROVIDER = env("CLASSIFIER_PROVIDER", default="ollama")

# --- Telegram ---------------------------------------------------------------
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_CHANNEL_ID = env("TELEGRAM_CHANNEL_ID", default="")
TELEGRAM_GROUP_ID = env("TELEGRAM_GROUP_ID", default="")
TELEGRAM_ADMIN_CHAT_ID = env("TELEGRAM_ADMIN_CHAT_ID", default="")
TELEGRAM_FORWARD_TTL = env.int("TELEGRAM_FORWARD_TTL", default=300)
PUBLISHING_ENABLED = env("PUBLISHING_ENABLED")

# --- Benchmark evidence verification ----------------------------------------
# This deterministic post-cluster check only promotes evidence when an independent
# outlet repeats a metric-bearing number. Keep it off until the real-corpus review passes.
BENCHMARK_VERIFICATION_ENABLED = env.bool("BENCHMARK_VERIFICATION_ENABLED", default=False)
#: On by default: link preview is the approved image delivery mechanism (Option A, 2026-08-18).
#: Telegram unfurls the article URL and fetches og:image without our code downloading or storing
#: images, preserving the 4096-char sendMessage limit and working gracefully when no image exists.
TELEGRAM_LINK_PREVIEW = env.bool("TELEGRAM_LINK_PREVIEW", default=True)

# --- Post format v2 redesign ------------------------------------------------
POST_FORMAT_V2_ENABLED = env.bool("POST_FORMAT_V2_ENABLED", default=True)
#: Guard only. The real budget is POST_MAX_SENTENCES.
POST_MAX_CHARS = env.int("POST_MAX_CHARS", default=450)
#: Words are the wrong unit for Uzbek: it folds prepositions into suffixes, so the same
#: content is fewer, longer words than in English or Russian. 3 = lead + body_1 +
#: body_2, with body_2 the first to go when trimming.
POST_MAX_SENTENCES = env.int("POST_MAX_SENTENCES", default=3)

# --- Ingestion --------------------------------------------------------------
USER_AGENT = "news-radar/0.1 (+daily AI digest)"
ARTICLE_MIN_CHARS = 400
ARTICLE_MAX_AGE_DAYS = 7
SOURCE_DEGRADED_AFTER = 3

# --- Evening pipeline lock ---------------------------------------------------
# A heartbeat exists so this TTL can be SHORT. The two work together: a live holder
# refreshes roughly every 120s (every 20 articles at ~6s each), so 360s is refreshed
# three times over before it can expire, while a dead holder releases the lock in six
# minutes instead of sixty.
#
# The original failure this fixes: stopping a watcher shell on Windows did not kill its
# python child, which kept running orphaned and held the lock. A 3600s TTL with a
# heartbeat solves the opposite problem — a long job losing its lock — which was never
# the problem here.
EVENING_LOCK_TTL = env.int("EVENING_LOCK_TTL", default=360)

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
# One post per news item (ADR-004 §6), so this is a post count, not a list length.
# The project owner expects 10-15 posts per day.
DIGEST_MAX_ITEMS = env.int("DIGEST_MAX_ITEMS", default=15)
DIGEST_MAX_PER_TOPIC = env.int("DIGEST_MAX_PER_TOPIC", default=3)

# --- Clustering, Tier A (ADR-004 §3) ----------------------------------------
# Character 5-gram Jaccard over article text. Measured in
# docs/spike/DEDUP_MEASUREMENT.md: separates a real duplicate (0.900) from two
# consecutive releases (0.110). The 0.79 gap means any value in 0.2-0.9 decides both
# cases identically, so this threshold needs no tuning — do not treat it as a knob.
CLUSTER_JACCARD_THRESHOLD = 0.80
CLUSTER_SHINGLE_SIZE = 5
CLUSTER_TEXT_CHARS = 6000

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
        # httpx logs every request URL at INFO, and every Telegram URL contains the bot token.
        # It has been printed to a terminal twice. Raise the level rather than rely on care.
        "httpx": {"level": "WARNING", "propagate": True},
        "httpcore": {"level": "WARNING", "propagate": True},
    },
}

# --- Paper sources -----------------------------------------------------------
# Papers are filtered out of every digest by construction: `maturity_ceiling` caps a
# paper URL at `paper_only` and EXCLUDED_MATURITIES excludes that from ranking. Triaging
# them therefore spends the model for a result that cannot be published.
#
# Measured 2026-08-18: 216 of 411 stored articles came from paper domains and consumed
# 169 triage and classification calls, and not one had ever reached a digest.
#
# Set to False when M2 artifact verification lands. That feature admits a paper whose
# promised repository actually resolves, and it cannot judge articles never triaged.
SKIP_PAPER_DOMAINS = env.bool("SKIP_PAPER_DOMAINS", default=True)

# --- Subject diversity -------------------------------------------------------
# Digest #11 opened with three consecutive releases from the same project. The existing
# topic cap correctly allowed them because they were distinct releases; subject variety
# needs its own independent limit.
#
# Unlike CLUSTER_JACCARD_THRESHOLD, this one IS a knob. That threshold is settled by a
# 0.79 separation gap in the measurement: any value from 0.2 to 0.9 decides both known
# cases identically, so changing it is meaningless. The choice between one and two items
# per subject is settled by no measurement at all -- it is a judgement about how the
# channel should read. 1 is the default because it produced the correct result on digest
# #11, and 2 is a defensible choice, not a mistake.
#
# See docs/superpowers/specs/2026-08-18-digest-subject-diversity-design.md
DIGEST_MAX_PER_SUBJECT = env.int("DIGEST_MAX_PER_SUBJECT", default=1)
#: Hosts that carry many unrelated projects, where the owner segment is part of the identity.
#: Matched by exact equality, so raw.githubusercontent.com is an ordinary host.
SUBJECT_CODE_HOSTS = ("github.com", "gitlab.com", "huggingface.co")

# --- Artifact verification ---------------------------------------------------
# Paper domains are skipped before triage unless their promised repository is verified.
ARTIFACT_VERIFICATION_ENABLED = env.bool("ARTIFACT_VERIFICATION_ENABLED", default=True)
ARTIFACT_TIMEOUT = env.int("ARTIFACT_TIMEOUT", default=15)
