"""Django settings. One file, one environment — this project runs on one server."""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    PUBLISHING_ENABLED=(bool, False),
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
# wide; the GPU measured a ceiling of 2 (docs/spike/OLLAMA_BENCHMARK.md §6). That
# ceiling is about the hardware, so it survived the move to the gateway.
CELERY_BROKER_URL = env("REDIS_URL", default="redis://127.0.0.1:6380/0")
CELERY_RESULT_BACKEND = "django-db"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
# Without this, a TaskResult row stores its id, status and return value and nothing else —
# task_name, task_args, task_kwargs and worker all come back None. Measured 2026-08-26:
# an unexplained compose_and_publish ran on worker-publish and the result backend could not
# say which task it was, let alone who sent it or with what arguments. TRACK_STARTED above
# is what makes a row appear while the task is still running; this is what makes the row
# worth reading when it does.
CELERY_RESULT_EXTENDED = True
CELERY_TASK_ROUTES = {
    "digest.fetch_all_sources": {"queue": "fetch"},
    "digest.fetch_source": {"queue": "fetch"},
    "digest.triage_article": {"queue": "llm"},
    "digest.classify_article": {"queue": "llm"},
    "digest.triage_and_classify": {"queue": "llm"},
    "digest.analyse_for_digest": {"queue": "llm"},
    "digest.compose_and_publish": {"queue": "publish"},
    "digest.publish_next_item": {"queue": "publish"},
    "digest.publish_roundup": {"queue": "publish"},
    "digest.dispatch_worker_heartbeats": {"queue": "fetch"},
}
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# --- LLM providers ----------------------------------------------------------
# Two providers, two tiers each. The direct Ollama path was removed on 2026-08-25: the
# gateway fronts the same local GPU models (`fast` is the 8B, `smart` is the 31B), so the
# second client bought nothing and its model tags had become the tier vocabulary for
# providers that never spoke to it.
#
# Cost of that removal, recorded because it is not obvious: `Analysis.model_digest` is now
# always empty and `model_tag` records the tier alias, not the model. Only Ollama exposed
# /api/tags. The gateway can repoint an alias silently — that is its purpose — and nothing
# in the database will show it happened.
#
# Measured 2026-08-17 on the seven live digest items, and still the reason translation asks
# for the fast tier: the fast model lost 0/7 numbers and kept the glossary, while the deep
# one garbled Uzbek in the first digest. Translation is a constrained task — the input is
# fixed and the output shape is fixed — and a stronger model spends its extra freedom
# changing things, which in translation is always an error.
#
# MiMo stays available as a second provider. Its Token Plan forbids automated/backend use,
# so it is not the default anywhere; see ADR-004 §5.
LLM_MAX_CONCURRENCY = 2

LLM_PROVIDER = env("LLM_PROVIDER", default="gateway")
EDITORIAL_EN_PROVIDER = env("EDITORIAL_EN_PROVIDER", default=LLM_PROVIDER)
TRANSLATION_PROVIDER = env("TRANSLATION_PROVIDER", default=LLM_PROVIDER)
#: The single-stage Uzbek editorial (2026-08-26 design). Defaults to LLM_PROVIDER like the
#: two stages it replaces; CLASSIFIER_PROVIDER deliberately does not, because its volume is
#: several hundred calls a day and must not move silently.
EDITORIAL_UZ_PROVIDER = env("EDITORIAL_UZ_PROVIDER", default=LLM_PROVIDER)
#: Uzbek tokenises poorly, so a 1200-token cap truncated the JSON mid-object and the
#: whole translation was lost. Measured: 2500 gives 7/7 twice, 1200 gave 2/7.
TRANSLATION_NUM_PREDICT = env.int("TRANSLATION_NUM_PREDICT", default=2500)
#: The English editorial budget. Was a hardcoded 1500, which is enough on MiMo but not on the
#: gateway: its `smart` tier is a reasoning model and charges its reasoning to the same
#: budget. Measured 2026-08-21 on the live gateway with the real editorial prompt — 1500
#: returned finish_reason "length" and an empty message, 3000 completed. An unused cap costs
#: nothing (the model stops when it is done), a cap that is too small drops the article, so the
#: default takes the generous side of that asymmetry.
#:
#: Raised 4000 -> 5000 on 2026-08-26. 4000 dropped one article of eight in that morning's run
#: and one of seven in the first single-stage comparison, both with finish_reason "length".
#: The single-stage prompt is the longer of the two — it carries the article, the class block
#: and the Uzbek few-shot examples, and writes the post rather than notes — so it needs the
#: larger budget, and the two-stage path loses nothing by sharing it.
EDITORIAL_NUM_PREDICT = env.int("EDITORIAL_NUM_PREDICT", default=5000)
MIMO_BASE_URL = env("MIMO_BASE_URL", default="").rstrip("/")
MIMO_API_KEY = env("MIMO_API_KEY", default="")
MIMO_FAST_MODEL = env("MIMO_FAST_MODEL", default="mimo-v2.5")
MIMO_DEEP_MODEL = env("MIMO_DEEP_MODEL", default="mimo-v2.5-pro")
MIMO_TIMEOUT = env.int("MIMO_TIMEOUT", default=120)

# --- Internal LLM gateway ----------------------------------------------------
# OpenAI-compatible front door to the same local GPU models. Callers name a tier alias
# (`fast`/`smart`) rather than a model, so a tier can be repointed without a redeploy;
# sending a real model name is a 404. `fast` is the 8B model and `smart` the 31B — the
# same models the direct Ollama path used before it was removed on 2026-08-25.
GATEWAY_BASE_URL = env("GATEWAY_BASE_URL", default="").rstrip("/")
GATEWAY_TOKEN = env("GATEWAY_TOKEN", default="")
GATEWAY_FAST_MODEL = env("GATEWAY_FAST_MODEL", default="fast")
GATEWAY_SMART_MODEL = env("GATEWAY_SMART_MODEL", default="smart")
#: The gateway's own upstream read timeout is 300s, and a request also waits in its queue
#: for up to 30s. A shorter client timeout abandons a generation the gateway still runs.
GATEWAY_TIMEOUT = env.int("GATEWAY_TIMEOUT", default=300)

#: Triage and classification. A separate setting rather than inheriting LLM_PROVIDER:
#: these two stages make several hundred calls a day, and inheriting would move that
#: volume the moment the editorial provider changed. Any new provider setting must
#: default to preserving current behaviour.
CLASSIFIER_PROVIDER = env("CLASSIFIER_PROVIDER", default="gateway")

# --- Telegram ---------------------------------------------------------------
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_CHANNEL_ID = env("TELEGRAM_CHANNEL_ID", default="")
TELEGRAM_GROUP_ID = env("TELEGRAM_GROUP_ID", default="")
TELEGRAM_ADMIN_CHAT_ID = env("TELEGRAM_ADMIN_CHAT_ID", default="")
#: Target for `eval_editorial_uz --post`. Unset means the command refuses to post, which is
#: what makes an eval run on the server unable to reach the live channel.
TELEGRAM_EVAL_CHANNEL_ID = env("TELEGRAM_EVAL_CHANNEL_ID", default="")
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
#: Guard only. The real budget is POST_MAX_SENTENCES: the structure bounds the length, so
#: this should never bind on a well-formed post. A photo caption caps at 1024, so 500 leaves
#: room. Do not tune this against another channel's character counts — Uzbek agglutinates,
#: and a length calibrated on English or Russian means nothing here.
POST_MAX_CHARS = env.int("POST_MAX_CHARS", default=500)
#: Words are the wrong unit for Uzbek: it folds prepositions into suffixes, so the same
#: content is fewer, longer words than in English or Russian. 3 = lead + body_1 + kicker;
#: the headline and hashtag lines are labels and are not counted. body_2 was removed on
#: 2026-08-24 - it was always the first thing trimmed and the model confused it with body_1.
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
# One post per news item (ADR-004 §6). Two blocks a day, six posts each, one every two hours.
DIGEST_MAX_ITEMS = env.int("DIGEST_MAX_ITEMS", default=6)
#: 3 of 6 on one topic is half a block. Lowered with DIGEST_MAX_ITEMS on 2026-08-24.
DIGEST_MAX_PER_TOPIC = env.int("DIGEST_MAX_PER_TOPIC", default=2)
#: Selected above DIGEST_MAX_ITEMS so an item whose editorial or translation failed can be
#: dropped without shortening the block. At six items a block, one failure is 17% of it.
DIGEST_SELECT_MARGIN = env.int("DIGEST_SELECT_MARGIN", default=2)


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
