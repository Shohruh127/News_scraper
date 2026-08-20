"""Admin is the operator interface (ADR-001). Source review must be two clicks."""

from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils.html import format_html

from . import publish
from .models import Analysis, Article, DeliveryState, Digest, DigestItem, Feedback, Source


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
    list_display = ("title", "source", "status", "artifact_verified", "published_at", "fetched_at")
    list_filter = ("status", "artifact_verified", "source", "language")
    search_fields = ("title", "canonical_url")
    date_hierarchy = "published_at"
    readonly_fields = (
        "canonical_url",
        "content_hash",
        "fetched_at",
        "extracted_text",
        "meta",
        "artifact_url",
        "artifact_verified",
    )
    inlines = [AnalysisInline]


@admin.register(Analysis)
class AnalysisAdmin(admin.ModelAdmin):
    #: `stage` is the column that makes this table readable as a pipeline: one article
    #: carries a triage row, a classification row and two editorial rows, and without it
    #: they are four near-identical lines.
    list_display = (
        "article",
        "stage",
        "model_tag",
        "topic",
        "maturity",
        "latency_ms",
        "created_at",
    )
    list_filter = ("stage", "model_tag")
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
    readonly_fields = (
        "article",
        "position",
        "score",
        "channel_delivery_state",
        "channel_message_id",
        "group_message_id",
        "sent_as_photo",
        "channel_delivery_error",
        "channel_delivery_attempted_at",
        "manual_send_action",
    )

    @admin.display(description="Actions")
    def manual_send_action(self, obj: DigestItem):
        if not obj.pk:
            return "-"
        url = reverse("admin:digest_digestitem_send", args=[obj.pk])
        if obj.channel_delivery_state == DeliveryState.SENT or obj.channel_message_id:
            label = "Re-send"
            bg_color = "#5b80b2"
        elif obj.channel_delivery_state == DeliveryState.SENDING:
            label = "Sending..."
            bg_color = "#e09f3e"
        elif obj.channel_delivery_state in (DeliveryState.FAILED, DeliveryState.UNKNOWN):
            label = "Retry Send"
            bg_color = "#ba2121"
        else:
            label = "Send"
            bg_color = "#28a745"

        btn_style = (
            f"background-color: {bg_color}; color: white; padding: 3px 10px; "
            "border-radius: 4px; text-decoration: none; display: inline-block; "
            "font-weight: bold; white-space: nowrap;"
        )
        return format_html(
            '<a class="button" style="{}" href="{}">{}</a>',
            btn_style,
            url,
            label,
        )


@admin.register(DigestItem)
class DigestItemAdmin(admin.ModelAdmin):
    list_display = (
        "digest",
        "position",
        "article",
        "channel_delivery_state",
        "channel_message_id",
        "sent_as_photo",
        "channel_delivery_attempted_at",
        "manual_send_action",
    )
    list_filter = ("channel_delivery_state", "sent_as_photo")
    readonly_fields = (
        "digest",
        "article",
        "position",
        "score",
        "channel_message_id",
        "group_message_id",
        "sent_as_photo",
        "channel_delivery_state",
        "channel_delivery_error",
        "channel_delivery_attempted_at",
        "manual_send_action",
    )

    @admin.display(description="Actions")
    def manual_send_action(self, obj: DigestItem):
        if not obj.pk:
            return "-"
        url = reverse("admin:digest_digestitem_send", args=[obj.pk])
        if obj.channel_delivery_state == DeliveryState.SENT or obj.channel_message_id:
            label = "Re-send"
            bg_color = "#5b80b2"
        elif obj.channel_delivery_state == DeliveryState.SENDING:
            label = "Sending..."
            bg_color = "#e09f3e"
        elif obj.channel_delivery_state in (DeliveryState.FAILED, DeliveryState.UNKNOWN):
            label = "Retry Send"
            bg_color = "#ba2121"
        else:
            label = "Send"
            bg_color = "#28a745"

        btn_style = (
            f"background-color: {bg_color}; color: white; padding: 3px 10px; "
            "border-radius: 4px; text-decoration: none; display: inline-block; "
            "font-weight: bold; white-space: nowrap;"
        )
        return format_html(
            '<a class="button" style="{}" href="{}">{}</a>',
            btn_style,
            url,
            label,
        )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:item_id>/send/",
                self.admin_site.admin_view(self.send_item_view),
                name="digest_digestitem_send",
            ),
        ]
        return custom_urls + urls

    def send_item_view(self, request, item_id: int):
        item = get_object_or_404(
            DigestItem.objects.select_related(
                "digest", "article", "article__source"
            ).prefetch_related(
                "secondary_articles",
                "secondary_articles__source",
                "article__analyses",
            ),
            pk=item_id,
        )

        res = publish.publish_digest_item(item, republish=True)

        if res.get("suppressed"):
            self.message_user(
                request,
                f"Item #{item.position} (ID {item.id}): "
                "PUBLISHING_ENABLED is False (kill switch active). Message not sent.",
                level=messages.WARNING,
            )
        elif res.get("success"):
            app_info = (
                f" (Group appendix msg {res['group_message_id']})"
                if res.get("group_message_id")
                else ""
            )
            ch_msg = res.get("channel_message_id")
            self.message_user(
                request,
                f"Item #{item.position} (ID {item.id}) successfully sent to Telegram "
                f"(Channel msg {ch_msg}){app_info}.",
                level=messages.SUCCESS,
            )
        elif res.get("status") == "skipped":
            self.message_user(
                request,
                f"Item #{item.position} skipped: {res.get('error')}",
                level=messages.WARNING,
            )
        else:
            self.message_user(
                request,
                f"Failed to send Item #{item.position}: {res.get('error')}",
                level=messages.ERROR,
            )

        redirect_url = request.META.get("HTTP_REFERER") or reverse(
            "admin:digest_digestitem_changelist"
        )
        return HttpResponseRedirect(redirect_url)

    @admin.action(description="Send selected items to Telegram")
    def send_selected_items(self, request, queryset):
        sent_count = 0
        failed_count = 0
        for item in queryset.select_related(
            "digest", "article", "article__source"
        ).prefetch_related("secondary_articles", "secondary_articles__source", "article__analyses"):
            res = publish.publish_digest_item(item, republish=True)
            if res.get("success"):
                sent_count += 1
            else:
                failed_count += 1
        if sent_count:
            self.message_user(
                request, f"{sent_count} item(s) sent successfully.", level=messages.SUCCESS
            )
        if failed_count:
            self.message_user(
                request,
                f"{failed_count} item(s) failed or suppressed.",
                level=messages.WARNING,
            )

    actions = ["send_selected_items"]


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
