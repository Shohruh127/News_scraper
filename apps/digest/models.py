"""Six models. Enum values come from docs/CONTENT_SCHEMA.md and must match it exactly."""

from django.db import models


class Topic(models.TextChoices):
    FRONTIER_MODELS = "frontier_models"
    AI_AGENTS = "ai_agents"
    NEW_APPROACHES = "new_approaches"
    SPEECH_VOICE = "speech_voice"
    ROBOTICS = "robotics"
    FINTECH = "fintech"
    GOVTECH = "govtech"
    PRODUCTION_ENGINEERING = "production_engineering"
    STARTUPS = "startups"
    TECHNICAL_TALKS = "technical_talks"
    SAFETY_SECURITY = "safety_security"
    IRRELEVANT = "irrelevant"


class Maturity(models.TextChoices):
    PRODUCTION_DEPLOYMENT = "production_deployment"
    LIVE_PRODUCT = "live_product"
    REPRODUCIBLE_OPEN_SOURCE = "reproducible_open_source"
    PUBLIC_PILOT = "public_pilot"
    ANNOUNCEMENT_ONLY = "announcement_only"
    PAPER_ONLY = "paper_only"


#: Never published, only stored. See CONTENT_SCHEMA.md §3.
EXCLUDED_MATURITIES = {Maturity.ANNOUNCEMENT_ONLY, Maturity.PAPER_ONLY}


class Source(models.Model):
    class Connector(models.TextChoices):
        RSS = "rss"
        GITHUB = "github"
        HN = "hn"
        HF = "hf"
        HTML = "html"

    name = models.CharField(max_length=100, unique=True)
    connector = models.CharField(max_length=20, choices=Connector)
    url = models.URLField(max_length=500)
    #: Connector-specific settings. For `html`: CSS selectors and `min_items`.
    config = models.JSONField(default=dict, blank=True)
    stream = models.CharField(max_length=40, choices=Topic, blank=True)
    enabled = models.BooleanField(default=True)
    priority = models.PositiveSmallIntegerField(default=50)

    last_fetched_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    #: Set at SOURCE_DEGRADED_AFTER failures. Alerts, never disables — see ADR-002.
    is_degraded = models.BooleanField(default=False)
    last_error = models.TextField(blank=True)
    last_alerted_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-priority", "name"]

    def __str__(self):
        return self.name


class Article(models.Model):
    class Status(models.TextChoices):
        FETCHED = "fetched"
        TRIAGED = "triaged"
        CLASSIFIED = "classified"
        SKIPPED = "skipped"

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="articles")
    canonical_url = models.URLField(max_length=1000, unique=True)
    content_hash = models.CharField(max_length=64, unique=True)
    title = models.CharField(max_length=500)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    fetched_at = models.DateTimeField(auto_now_add=True)
    language = models.CharField(max_length=10, blank=True)
    extracted_text = models.TextField()
    status = models.CharField(max_length=20, choices=Status, default=Status.FETCHED, db_index=True)
    meta = models.JSONField(default=dict, blank=True)
    #: Repository promised by the article, if one was found by the prefilter.
    artifact_url = models.URLField(max_length=1000, blank=True, default="")
    #: True when checked and non-empty; False when checked and empty/unreachable; NULL when unseen.
    artifact_verified = models.BooleanField(null=True, blank=True, default=None)

    class Meta:
        ordering = ["-published_at", "-fetched_at"]
        indexes = [models.Index(fields=["source", "status"])]

    def __str__(self):
        return self.title[:80]


class Analysis(models.Model):
    """One LLM call. Triage, classification, and editorial stages land here."""

    class Stage(models.TextChoices):
        TRIAGE = "triage"
        CLASSIFICATION = "classification"
        #: English analysis. Separated from translation so a bad summary can be traced
        #: to either comprehension or translation, not to an ambiguous single step.
        EDITORIAL_EN = "editorial_en"
        #: Uzbek translation of the *_en fields. `technical` stays English.
        EDITORIAL_UZ = "editorial_uz"
        #: Legacy single-step editorial (strategy C). Kept so old rows validate.
        EDITORIAL = "editorial"

    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name="analyses")
    stage = models.CharField(
        max_length=20,
        choices=Stage,
        default=Stage.CLASSIFICATION,
        db_index=True,
    )
    model_tag = models.CharField(max_length=60)
    #: Ollama digest. On this server the 8B model has no tag but `latest`, so a
    #: repointed tag is only detectable through this field.
    model_digest = models.CharField(max_length=64, blank=True)
    payload = models.JSONField()
    latency_ms = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "analyses"

    def __str__(self):
        return f"{self.stage}:{self.model_tag} → {self.article_id}"

    @property
    def topic(self):
        return self.payload.get("primary_topic")

    @property
    def maturity(self):
        return self.payload.get("maturity")


class Digest(models.Model):
    class Status(models.TextChoices):
        COMPOSED = "composed"
        PUBLISHED = "published"
        FAILED = "failed"

    class Edition(models.TextChoices):
        MORNING = "morning", "Morning"
        EVENING = "evening", "Evening"

    #: Unique together with edition, so a second run for the same slot is refused by DB.
    digest_date = models.DateField()
    edition = models.CharField(
        max_length=16,
        choices=Edition.choices,
        default=Edition.EVENING,
        db_index=True,
    )
    status = models.CharField(max_length=20, choices=Status, default=Status.COMPOSED)
    composed_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-digest_date", "-edition"]
        constraints = [
            models.UniqueConstraint(
                fields=["digest_date", "edition"],
                name="unique_digest_date_edition",
            )
        ]

    def __str__(self):
        return f"{self.digest_date} {self.edition} ({self.status})"


class DeliveryState(models.TextChoices):
    PENDING = "pending", "Pending"
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    UNKNOWN = "unknown", "Unknown"
    FAILED = "failed", "Failed"


class DigestItem(models.Model):
    digest = models.ForeignKey(Digest, on_delete=models.CASCADE, related_name="items")
    article = models.ForeignKey(Article, on_delete=models.PROTECT)
    secondary_articles = models.ManyToManyField(
        Article, related_name="secondary_in_digest_items", blank=True
    )
    position = models.PositiveSmallIntegerField()
    score = models.FloatField()
    channel_message_id = models.BigIntegerField(null=True, blank=True)
    group_message_id = models.BigIntegerField(null=True, blank=True)
    sent_as_photo = models.BooleanField(
        default=False,
        help_text="Whether this item was published to the channel as a photo with caption",
    )
    channel_delivery_state = models.CharField(
        max_length=16,
        choices=DeliveryState.choices,
        default=DeliveryState.PENDING,
        db_index=True,
    )
    channel_delivery_error = models.CharField(max_length=512, blank=True, default="")
    channel_delivery_attempted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["digest", "position"]
        constraints = [
            models.UniqueConstraint(fields=["digest", "position"], name="unique_digest_position"),
            models.UniqueConstraint(fields=["digest", "article"], name="unique_digest_article"),
        ]

    def __str__(self):
        return f"{self.digest.digest_date} #{self.position}"


class Feedback(models.Model):
    """Table exists from M1; written to in M2 when the bot arrives."""

    class Reaction(models.TextChoices):
        USEFUL = "useful"
        NOT_USEFUL = "not_useful"
        WANT_TO_BUILD = "want_to_build"

    digest_item = models.ForeignKey(DigestItem, on_delete=models.CASCADE, related_name="feedback")
    user_id = models.BigIntegerField()
    reaction = models.CharField(max_length=20, choices=Reaction)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["digest_item", "user_id"], name="one_reaction_per_user_per_item"
            ),
        ]

    def __str__(self):
        return f"{self.user_id} {self.reaction}"
