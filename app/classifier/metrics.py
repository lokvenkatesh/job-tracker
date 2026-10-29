"""Per-type precision/recall for classifier evals."""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from rapidfuzz import fuzz

_COMPANY_SUFFIX_RE = re.compile(
    r"[,.]|\b(inc|llc|ltd|corp|corporation|co|company|plc|gmbh|careers|recruiting|the)\b"
)


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


def normalize_company(name: str) -> str:
    return " ".join(_COMPANY_SUFFIX_RE.sub(" ", name.lower()).split())


def company_match(gold: str | None, predicted: str | None) -> bool:
    if gold is None or predicted is None:
        return gold is predicted
    g, p = normalize_company(gold), normalize_company(predicted)
    return g == p or fuzz.ratio(g, p) >= 90 or (len(g) >= 4 and (g in p or p in g))
