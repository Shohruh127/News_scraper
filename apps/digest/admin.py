"""Admin is the operator interface (ADR-001). Source review must be two clicks."""

from django.conf import settings
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils.html import format_html

from . import llm, publish
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
    list_display = (
        "title",
        "source",
        "status",
        "artifact_verified",
        "published_at",
        "fetched_at",
        "post_actions",
    )
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

    # Two buttons per article, neither of which touches a Digest. "Write" runs the
    # editorial stage on this one article with the current prompt, bypassing the reuse
    # memo, and stores an Analysis row. "Send" renders the latest such row and posts it
    # as a photo to the eval channel -- by that name and never TELEGRAM_CHANNEL_ID, so
    # a preview cannot reach the live channel even when the two ids match. No
    # DigestItem is created because a Digest claims a (digest_date, edition) slot.

    @admin.display(description="Post")
    def post_actions(self, obj: Article):
        if not obj.pk:
            return ""
        write_url = reverse("admin:digest_article_write_post", args=[obj.pk])
        send_url = reverse("admin:digest_article_send_preview", args=[obj.pk])
        has_post = obj.analyses.filter(stage=Analysis.Stage.EDITORIAL_UZ).exists()
        return format_html(
            '<a class="button" href="{}">Yangi post yoz</a> '
            '<a class="button" style="{}" href="{}">Sinov kanaliga yubor</a>',
            write_url,
            "" if has_post else "opacity:.5;pointer-events:none",
            send_url,
        )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:article_id>/write-post/",
                self.admin_site.admin_view(self.write_post_view),
                name="digest_article_write_post",
            ),
            path(
                "<int:article_id>/send-preview/",
                self.admin_site.admin_view(self.send_preview_view),
                name="digest_article_send_preview",
            ),
        ]
        return custom_urls + urls

    def _back(self, request):
        return HttpResponseRedirect(
            request.META.get("HTTP_REFERER") or reverse("admin:digest_article_changelist")
        )

    def write_post_view(self, request, article_id: int):
        article = get_object_or_404(Article.objects.select_related("source"), pk=article_id)
        try:
            rows = llm.analyse_for_digest_logic([article.id], force=True)
        except Exception as exc:  # the stage logs the cause; the operator needs the message
            self.message_user(request, f"Post yozilmadi: {exc}", level=messages.ERROR)
            return self._back(request)
        if not rows:
            self.message_user(
                request,
                "Post yozildi, lekin gate'lardan o'tmadi va saqlanmadi - "
                "Analysis ro'yxatida 'discarded_violations' bilan turibdi.",
                level=messages.WARNING,
            )
            return self._back(request)
        lead = rows[0].payload.get("lead_uz", "")
        self.message_user(request, f"Yangi post yozildi: {lead}", level=messages.SUCCESS)
        return self._back(request)

    def send_preview_view(self, request, article_id: int):
        article = get_object_or_404(Article.objects.select_related("source"), pk=article_id)
        chat_id = settings.TELEGRAM_EVAL_CHANNEL_ID
        if not chat_id:
            self.message_user(
                request,
                "TELEGRAM_EVAL_CHANNEL_ID o'rnatilmagan; sinov posti faqat o'sha kanalga ketadi.",
                level=messages.ERROR,
            )
            return self._back(request)
        row = (
            article.analyses.filter(stage=Analysis.Stage.EDITORIAL_UZ)
            .order_by("-created_at")
            .first()
        )
        if not row or not row.payload.get("lead_uz"):
            self.message_user(request, "Bu maqolada hali post yo'q.", level=messages.WARNING)
            return self._back(request)
        try:
            caption = llm.render_editorial_preview(article, row.payload)
        except ValueError as exc:
            self.message_user(request, f"Post render bo'lmadi: {exc}", level=messages.ERROR)
            return self._back(request)
        photo = publish.resolve_photo_url(article)
        if not photo:
            self.message_user(
                request, "Rasm topilmadi; rasmsiz post yuborilmaydi.", level=messages.ERROR
            )
            return self._back(request)
        res = publish.send_photo(chat_id=chat_id, photo_url=photo, caption=caption)
        if res.get("suppressed"):
            self.message_user(
                request, "PUBLISHING_ENABLED o'chiq; yuborilmadi.", level=messages.WARNING
            )
        else:
            mid = (res.get("result") or {}).get("message_id")
            self.message_user(
                request, f"Sinov kanaliga yuborildi (xabar {mid}).", level=messages.SUCCESS
            )
        return self._back(request)


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
            ch_msg = res.get("channel_message_id")
            self.message_user(
                request,
                f"Item #{item.position} (ID {item.id}) successfully sent to Telegram "
                f"(Channel msg {ch_msg}).",
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
