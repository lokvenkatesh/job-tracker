"""Run the classifier over eval/labeled_emails.jsonl and report precision/recall per type.

python -m eval.run_eval                 # full set (~100 Claude calls)
python -m eval.run_eval --limit 10      # quick smoke run
python -m eval.run_eval --out eval/results/latest.json
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path

from app.classifier import EmailClassifier, EmailType
from app.classifier.metrics import company_match, confusion, per_type_metrics
from app.config import get_settings
from app.gmail.message import EmailMessage

DATASET = Path(__file__).with_name("labeled_emails.jsonl")


def load_dataset(path: Path = DATASET) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def to_message(row: dict) -> EmailMessage:
    name, addr = parseaddr(row["sender"])
    return EmailMessage(
        gmail_id=row["id"],
        thread_id=row["id"],
        sender_name=name,
        sender_email=addr.lower(),
        subject=row["subject"],
        snippet=row["body"][:200],
        received_at=datetime.fromisoformat(row["received_at"]),
        body_text=row["body"],
    )


def _fmt(value: float | None) -> str:
    return "   —" if value is None else f"{value:.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--model", help="override CLAUDE_CLASSIFIER_MODEL")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--out", type=Path, help="write per-email results as JSON")
    args = parser.parse_args()

    settings = get_settings()
    if settings.anthropic_api_key is None:
        sys.exit("ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example).")

    rows = load_dataset()[: args.limit]
    classifier = EmailClassifier(settings=settings, model=args.model, effort=args.effort)
    print(f"Classifying {len(rows)} emails with {classifier.model} (effort={args.effort})...")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda r: classifier.classify(to_message(r)), rows))

    threshold = settings.classifier_confidence_threshold
    pairs = [(r["label"]["type"], res.type.value) for r, res in zip(rows, results, strict=True)]
    metrics = per_type_metrics(pairs, [t.value for t in EmailType])

    print(f"\n{'type':<26}{'support':>8}{'pred':>6}{'prec':>7}{'recall':>8}{'f1':>7}")
    for t, m in metrics.items():
        print(
            f"{t:<26}{m.support:>8}{m.predicted:>6}{_fmt(m.precision):>7}"
            f"{_fmt(m.recall):>8}{_fmt(m.f1):>7}"
        )
    f1s = [m.f1 for m in metrics.values() if m.support and m.f1 is not None]
    accuracy = sum(g == p for g, p in pairs) / len(pairs)
    print(f"\naccuracy {accuracy:.3f}   macro-F1 {sum(f1s) / len(f1s) if f1s else 0:.3f}")

    company_rows = [
        (r, res)
        for r, res in zip(rows, results, strict=True)
        if r["label"]["type"] != "other" and r["label"]["company"]
    ]
    company_hits = sum(company_match(r["label"]["company"], res.company) for r, res in company_rows)
    print(f"company match {company_hits}/{len(company_rows)}")
    review = sum(res.needs_review(threshold) for res in results)
    errors = sum(res.error is not None for res in results)
    print(f"needs_review {review}/{len(results)} (threshold {threshold}), errors {errors}")

    wrong = confusion(pairs)
    if wrong:
        print("\nconfusions (gold -> predicted):")
        for (g, p), n in wrong.most_common():
            print(f"  {g} -> {p}: {n}")
        print("\nmisclassified:")
        for r, res in zip(rows, results, strict=True):
            if r["label"]["type"] != res.type.value:
                print(
                    f"  {r['id']}  {r['label']['type']} -> {res.type.value} "
                    f"({res.confidence:.2f})  {r['subject'][:60]}"
                )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                [
                    {"id": r["id"], "label": r["label"], "result": res.model_dump(mode="json")}
                    for r, res in zip(rows, results, strict=True)
                ],
                indent=2,
            )
        )
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
