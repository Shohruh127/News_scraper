"""Create the eight M1 sources. Idempotent — safe to run repeatedly."""

from django.core.management.base import BaseCommand

from apps.digest.models import Source, Topic

SOURCES = [
    {
        "name": "openai",
        "connector": "rss",
        "priority": 90,
        "url": "https://openai.com/news/rss.xml",
        "stream": Topic.FRONTIER_MODELS,
    },
    {
        "name": "deepmind",
        "connector": "rss",
        "priority": 90,
        "url": "https://deepmind.google/blog/feed/basic/",
        "stream": Topic.FRONTIER_MODELS,
    },
    # Anthropic publishes no RSS — verified 2026-08-14, see TECHNICAL_REVIEW.md C2.
    {
        "name": "anthropic",
        "connector": "html",
        "priority": 90,
        "url": "https://www.anthropic.com/news",
        "stream": Topic.FRONTIER_MODELS,
        "config": {"link_pattern": "/news/", "min_items": 5},
    },
    {
        "name": "hf_papers",
        "connector": "hf",
        "priority": 60,
        "url": "https://huggingface.co/papers",
        "stream": Topic.NEW_APPROACHES,
        "config": {"limit": 100},
    },
    {
        "name": "gh_langgraph",
        "connector": "github",
        "priority": 70,
        "url": "https://github.com/langchain-ai/langgraph",
        "stream": Topic.AI_AGENTS,
        "config": {"repo": "langchain-ai/langgraph"},
    },
    {
        "name": "gh_mcp",
        "connector": "github",
        "priority": 70,
        "url": "https://github.com/modelcontextprotocol/servers",
        "stream": Topic.AI_AGENTS,
        "config": {"repo": "modelcontextprotocol/servers"},
    },
    {
        "name": "gh_ollama",
        "connector": "github",
        "priority": 70,
        "url": "https://github.com/ollama/ollama",
        "stream": Topic.PRODUCTION_ENGINEERING,
        "config": {"repo": "ollama/ollama"},
    },
    {
        "name": "gh_whisper",
        "connector": "github",
        "priority": 60,
        "url": "https://github.com/openai/whisper",
        "stream": Topic.SPEECH_VOICE,
        "config": {"repo": "openai/whisper"},
    },
    {
        "name": "gh_faster_whisper",
        "connector": "github",
        "priority": 60,
        "url": "https://github.com/SYSTRAN/faster-whisper",
        "stream": Topic.SPEECH_VOICE,
        "config": {"repo": "SYSTRAN/faster-whisper"},
    },
    {
        "name": "nvidia_robotics",
        "connector": "rss",
        "priority": 50,
        "url": "https://developer.nvidia.com/blog/category/robotics/feed/",
        "stream": Topic.ROBOTICS,
    },
    {
        "name": "arxiv_cs_cr",
        "connector": "rss",
        "priority": 50,
        "url": "https://rss.arxiv.org/rss/cs.CR",
        "stream": Topic.SAFETY_SECURITY,
    },
    {
        "name": "hn",
        "connector": "hn",
        "priority": 40,
        "url": "https://hn.algolia.com/",
        "stream": "",
        "config": {"min_points": 50},
    },
    # --- Added 2026-08-18. Feeds verified live the same day. -------------------
    # govtech had no source at all before this; these five are its whole supply.
    {
        "name": "nextgov",
        "connector": "rss",
        "priority": 50,
        "url": "https://www.nextgov.com/rss/all/",
        "stream": Topic.GOVTECH,
    },
    {
        "name": "fedscoop",
        "connector": "rss",
        "priority": 50,
        "url": "https://fedscoop.com/feed/",
        "stream": Topic.GOVTECH,
    },
    {
        "name": "statescoop",
        "connector": "rss",
        "priority": 60,
        "url": "https://statescoop.com/feed/",
        "stream": Topic.GOVTECH,
    },
    {
        "name": "ec_digital",
        "connector": "rss",
        "priority": 50,
        "url": "https://digital-strategy.ec.europa.eu/en/rss.xml",
        "stream": Topic.GOVTECH,
    },
    {
        "name": "gds_uk",
        "connector": "rss",
        "priority": 60,
        "url": "https://gds.blog.gov.uk/feed/",
        "stream": Topic.GOVTECH,
    },
    # speech_voice: the three large vendors publish no feed, so release feeds are the supply.
    {
        "name": "gh_sherpa_onnx",
        "connector": "github",
        "priority": 50,
        "url": "https://github.com/k2-fsa/sherpa-onnx",
        "stream": Topic.SPEECH_VOICE,
        "config": {"repo": "k2-fsa/sherpa-onnx"},
    },
    {
        "name": "gh_pyannote",
        "connector": "github",
        "priority": 50,
        "url": "https://github.com/pyannote/pyannote-audio",
        "stream": Topic.SPEECH_VOICE,
        "config": {"repo": "pyannote/pyannote-audio"},
    },
    {
        "name": "gh_whisperx",
        "connector": "github",
        "priority": 60,
        "url": "https://github.com/m-bain/whisperX",
        "stream": Topic.SPEECH_VOICE,
        "config": {"repo": "m-bain/whisperX"},
    },
    # startups
    {
        "name": "techcrunch_ai",
        "connector": "rss",
        "priority": 50,
        "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
        "stream": Topic.STARTUPS,
    },
    {
        "name": "crunchbase_news",
        "connector": "rss",
        "priority": 60,
        "url": "https://news.crunchbase.com/feed/",
        "stream": Topic.STARTUPS,
    },
    {
        "name": "sifted",
        "connector": "rss",
        "priority": 60,
        "url": "https://sifted.eu/feed",
        "stream": Topic.STARTUPS,
    },
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
