"""Management command: evaluate LLM classifier against data/gold_set.jsonl."""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.digest import llm
from apps.digest.models import EXCLUDED_MATURITIES, Topic


class Command(BaseCommand):
    help = "Evaluate the classification model against data/gold_set.jsonl"

    def add_arguments(self, parser):
        parser.add_argument(
            "--model",
            type=str,
            default=getattr(settings, "OLLAMA_FAST_MODEL", "gemma4:latest"),
            help="Ollama model to evaluate (e.g. gemma4:latest or gemma4:31b)",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=120,
            help="Timeout per request in seconds",
        )
        parser.add_argument(
            "--gold-set",
            type=str,
            default="data/gold_set.jsonl",
            help="Path to gold standard jsonl file",
        )

    def handle(self, *args, **options):
        model = options["model"]
        timeout = options["timeout"]
        gold_set_path = Path(options["gold_set"])
        if not gold_set_path.is_absolute():
            gold_set_path = settings.BASE_DIR / gold_set_path

        if not gold_set_path.exists():
            raise CommandError(f"Gold set file not found: {gold_set_path}")

        self.stdout.write(f"\nEvaluating model '{model}' against {gold_set_path.name}...\n")

        rows = []
        with open(gold_set_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))

        self.stdout.write(f"Loaded {len(rows)} gold set rows.")

        tp, fp, tn, fn = 0, 0, 0, 0
        topic_correct, maturity_correct = 0, 0
        confusion_label = defaultdict(Counter)
        results = []
        error_count = 0

        for idx, row in enumerate(rows, 1):
            title = row.get("title", "")
            source = row.get("source", "")
            text = row.get("text_excerpt", "")
            human_label = row.get("human_label", "").lower()
            human_topic = row.get("human_topic", "")
            human_maturity = row.get("human_maturity", "")

            num_predict = 2000 if "31b" in model else 1200
            try:
                classification, raw_payload, latency_ms, digest = llm.classify_text(
                    title=title,
                    source_name=source,
                    text=text,
                    model=model,
                    timeout=timeout,
                    num_predict=num_predict,
                )
                pred_topic = classification.primary_topic.value
                pred_maturity = classification.maturity.value

                if (
                    classification.primary_topic == Topic.IRRELEVANT
                    or classification.maturity in EXCLUDED_MATURITIES
                ):
                    pred_label = "drop"
                else:
                    pred_label = "keep"

            except Exception as exc:
                self.stderr.write(f"Row {idx} ({title[:30]}...) FAILED: {exc}")
                error_count += 1
                results.append(
                    {
                        "title": title[:40],
                        "human": f"{human_label}/{human_topic}/{human_maturity}",
                        "pred": "ERROR",
                        "latency_ms": 0,
                        "match": False,
                    }
                )
                log_line = (
                    f"[{idx}/{len(rows)}] {title[:32]:<32} -> ERROR ({exc.__class__.__name__})"
                )
                self.stdout.write(self.style.ERROR(log_line))
                continue

            confusion_label[human_label][pred_label] += 1
            if human_label == "keep" and pred_label == "keep":
                tp += 1
            elif human_label == "drop" and pred_label == "keep":
                fp += 1
            elif human_label == "drop" and pred_label == "drop":
                tn += 1
            elif human_label == "keep" and pred_label == "drop":
                fn += 1

            if human_topic == pred_topic:
                topic_correct += 1
            if human_maturity == pred_maturity:
                maturity_correct += 1

            results.append(
                {
                    "title": title[:40],
                    "human": f"{human_label}/{human_topic}/{human_maturity}",
                    "pred": f"{pred_label}/{pred_topic}/{pred_maturity}",
                    "latency_ms": latency_ms,
                    "match": (human_label == pred_label),
                }
            )
            log_line = (
                f"[{idx}/{len(rows)}] {title[:32]:<32} -> "
                f"Human: {human_label:<4} Pred: {pred_label:<4} ({latency_ms}ms)"
            )
            self.stdout.write(log_line)

        # --- Abort if too many errors -------------------------------------------
        success_count = len(rows) - error_count
        if error_count > 0:
            self.stdout.write("\n" + "=" * 60)
            self.stdout.write(
                self.style.ERROR(
                    f"EVALUATION ABORTED: {error_count}/{len(rows)} rows failed "
                    f"(only {success_count} succeeded)."
                )
            )
            self.stdout.write(
                "Metrics are NOT reported because they would be misleading.\n"
                "Fix the errors above and re-run."
            )
            self.stdout.write("=" * 60)
            sys.exit(1)

        # --- All rows succeeded — report metrics --------------------------------
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
        topic_acc = topic_correct / len(rows) if rows else 0.0
        maturity_acc = maturity_correct / len(rows) if rows else 0.0

        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(f"EVALUATION REPORT: {model}")
        self.stdout.write("=" * 60)
        self.stdout.write(f"Total samples: {len(rows)}")
        self.stdout.write(f"True Positives (TP):  {tp}")
        self.stdout.write(f"False Positives (FP): {fp}")
        self.stdout.write(f"True Negatives (TN):  {tn}")
        self.stdout.write(f"False Negatives (FN): {fn}")
        self.stdout.write("-" * 60)
        self.stdout.write(f"Precision:         {precision:.4f} (Requirement >= 0.80)")
        self.stdout.write(f"Recall:            {recall:.4f}")
        self.stdout.write(f"F1 Score:          {f1:.4f}")
        self.stdout.write(f"Topic Accuracy:    {topic_acc:.4f} ({topic_correct}/{len(rows)})")
        self.stdout.write(f"Maturity Accuracy: {maturity_acc:.4f} ({maturity_correct}/{len(rows)})")
        self.stdout.write("-" * 60)

        self.stdout.write("Confusion Matrix (Keep/Drop):")
        self.stdout.write(f"{'':<15} {'Pred KEEP':<12} {'Pred DROP':<12}")
        k_k = confusion_label["keep"]["keep"]
        k_d = confusion_label["keep"]["drop"]
        d_k = confusion_label["drop"]["keep"]
        d_d = confusion_label["drop"]["drop"]
        self.stdout.write(f"{'Actual KEEP':<15} {k_k:<12} {k_d:<12}")
        self.stdout.write(f"{'Actual DROP':<15} {d_k:<12} {d_d:<12}")
        self.stdout.write("-" * 60)

        self.stdout.write(
            self.style.WARNING(
                "\n[MANDATORY CAVEAT]\n"
                "Both gold set labels and enum definitions share an AI origin (Claude).\n"
                "Precision >= 0.80 is necessary but not sufficient to prove editorial quality.\n"
                "Seven topics have 0 examples in this gold set.\n"
            )
        )

        if precision >= 0.80:
            msg = f"ACCEPTANCE CRITERIA MET: Precision {precision:.2f} >= 0.80"
            self.stdout.write(self.style.SUCCESS(msg))
        else:
            msg = f"ACCEPTANCE CRITERIA NOT MET: Precision {precision:.2f} < 0.80"
            self.stdout.write(self.style.ERROR(msg))
            sys.exit(1)
