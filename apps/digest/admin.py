"""Admin is the operator interface (ADR-001). Source review must be two clicks."""

from django.contrib import admin

from .models import Analysis, Article, Digest, DigestItem, Feedback, Source


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "connector",
        "stream",
        "enabled",
        "is_degraded",
        "consecutive_failures",
        "last_fetched_at",
        "priority",
    )
    list_filter = ("is_degraded", "enabled", "connector", "stream")
    list_editable = ("enabled", "priority")
    search_fields = ("name", "url")
    readonly_fields = ("last_fetched_at", "consecutive_failures", "last_error", "last_alerted_on")
    fieldsets = (
        (None, {"fields": ("name", "connector", "url", "stream", "priority", "enabled")}),
        (
            "Connector config",
            {
                "fields": ("config",),
                "description": "For html sources: CSS selectors and min_items.",
            },
        ),
        (
            "Health",
            {
                "fields": (
                    "is_degraded",
                    "consecutive_failures",
                    "last_fetched_at",
                    "last_error",
                    "last_alerted_on",
                ),
                "description": "A degraded source keeps being fetched. "
                "Only a human disables it — see ADR-002.",
            },
        ),
    )

    @admin.action(description="Clear failure counter and degraded flag")
    def clear_failures(self, request, queryset):
        n = queryset.update(consecutive_failures=0, is_degraded=False, last_error="")
        self.message_user(request, f"{n} source(s) reset.")

    actions = ["clear_failures"]


class AnalysisInline(admin.TabularInline):
    model = Analysis
    extra = 0
    readonly_fields = ("model_tag", "topic", "maturity", "latency_ms", "created_at", "payload")
    can_delete = False


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ("title", "source", "status", "published_at", "fetched_at")
    list_filter = ("status", "source", "language")
    search_fields = ("title", "canonical_url")
    date_hierarchy = "published_at"
    readonly_fields = ("canonical_url", "content_hash", "fetched_at", "extracted_text", "meta")
    inlines = [AnalysisInline]


@admin.register(Analysis)
class AnalysisAdmin(admin.ModelAdmin):
    list_display = ("article", "model_tag", "topic", "maturity", "latency_ms", "created_at")
    list_filter = ("model_tag",)
    search_fields = ("article__title",)
    readonly_fields = (
        "article",
        "model_tag",
        "model_digest",
        "payload",
        "latency_ms",
        "created_at",
    )


class DigestItemInline(admin.TabularInline):
    model = DigestItem
    extra = 0
    readonly_fields = ("article", "position", "score", "channel_message_id", "group_message_id")


@admin.register(Digest)
class DigestAdmin(admin.ModelAdmin):
    list_display = ("digest_date", "status", "item_count", "composed_at", "published_at")
    list_filter = ("status",)
    date_hierarchy = "digest_date"
    inlines = [DigestItemInline]

    @admin.display(description="items")
    def item_count(self, obj):
        return obj.items.count()


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ("digest_item", "user_id", "reaction", "created_at")
    list_filter = ("reaction",)
