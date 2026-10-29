"""Per-type precision/recall for classifier evals."""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from app.companies import company_match, normalize_company  # noqa: F401 (re-exported)


@dataclass(frozen=True)
class TypeMetrics:
    support: int  # gold count
    predicted: int
    true_positives: int

    @property
    def precision(self) -> float | None:
        return self.true_positives / self.predicted if self.predicted else None

    @property
    def recall(self) -> float | None:
        return self.true_positives / self.support if self.support else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or p + r == 0:
            return None
        return 2 * p * r / (p + r)


def per_type_metrics(
    pairs: Iterable[tuple[str, str]], types: Iterable[str]
) -> dict[str, TypeMetrics]:
    """pairs: (gold, predicted) type per email."""
    pairs = list(pairs)
    gold = Counter(g for g, _ in pairs)
    pred = Counter(p for _, p in pairs)
    hits = Counter(g for g, p in pairs if g == p)
    return {t: TypeMetrics(gold[t], pred[t], hits[t]) for t in types}


def confusion(pairs: Iterable[tuple[str, str]]) -> Counter[tuple[str, str]]:
    return Counter((g, p) for g, p in pairs if g != p)
