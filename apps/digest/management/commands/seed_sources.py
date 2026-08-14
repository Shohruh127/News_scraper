"""Create the eight M1 sources. Idempotent — safe to run repeatedly."""

from django.core.management.base import BaseCommand

from apps.digest.models import Source, Topic

SOURCES = [
    {"name": "openai", "connector": "rss", "priority": 90,
     "url": "https://openai.com/news/rss.xml", "stream": Topic.FRONTIER_MODELS},
    {"name": "deepmind", "connector": "rss", "priority": 90,
     "url": "https://deepmind.google/blog/feed/basic/", "stream": Topic.FRONTIER_MODELS},
    # Anthropic publishes no RSS — verified 2026-08-14, see TECHNICAL_REVIEW.md C2.
    {"name": "anthropic", "connector": "html", "priority": 90,
     "url": "https://www.anthropic.com/news", "stream": Topic.FRONTIER_MODELS,
     "config": {"link_pattern": "/news/", "min_items": 5}},
    {"name": "hf_papers", "connector": "hf", "priority": 60,
     "url": "https://huggingface.co/papers", "stream": Topic.NEW_APPROACHES,
     "config": {"limit": 100}},
    {"name": "gh_langgraph", "connector": "github", "priority": 70,
     "url": "https://github.com/langchain-ai/langgraph", "stream": Topic.AI_AGENTS,
     "config": {"repo": "langchain-ai/langgraph"}},
    {"name": "gh_mcp", "connector": "github", "priority": 70,
     "url": "https://github.com/modelcontextprotocol/servers", "stream": Topic.AI_AGENTS,
     "config": {"repo": "modelcontextprotocol/servers"}},
    {"name": "gh_ollama", "connector": "github", "priority": 70,
     "url": "https://github.com/ollama/ollama", "stream": Topic.PRODUCTION_ENGINEERING,
     "config": {"repo": "ollama/ollama"}},
    {"name": "hn", "connector": "hn", "priority": 40,
     "url": "https://hn.algolia.com/", "stream": "",
     "config": {"min_points": 50}},
]


class Command(BaseCommand):
    help = "Create or update the eight M1 sources."

    def handle(self, *args, **options):
        for spec in SOURCES:
            spec.setdefault("config", {})
            obj, created = Source.objects.update_or_create(
                name=spec["name"],
                defaults={k: v for k, v in spec.items() if k != "name"},
            )
            self.stdout.write(f"  {'created' if created else 'updated'}  {obj.name}")
        self.stdout.write(self.style.SUCCESS(f"{Source.objects.count()} sources total"))
