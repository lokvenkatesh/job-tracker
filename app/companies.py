"""Company and role name normalization shared by the matcher and the eval."""

import re

from rapidfuzz import fuzz

_COMPANY_NOISE_RE = re.compile(
    r"[,.]|\b(inc|llc|ltd|corp|corporation|co|company|plc|gmbh|careers|recruiting|the)\b"
)
_ROLE_NOISE_RE = re.compile(r"\(.*?\)|\[.*?\]|\b(req|job|r)\s*#?\s*\d+\b|[-–—,/|:]")


def normalize_company(name: str) -> str:
    return " ".join(_COMPANY_NOISE_RE.sub(" ", name.lower()).split())


def company_match(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return a is b
    x, y = normalize_company(a), normalize_company(b)
    if not x or not y:
        return False
    return x == y or fuzz.ratio(x, y) >= 90 or (min(len(x), len(y)) >= 4 and (x in y or y in x))


def normalize_role(role: str) -> str:
    return " ".join(_ROLE_NOISE_RE.sub(" ", role.lower()).split())


def role_similarity(a: str | None, b: str | None) -> float:
    """0-100. Missing roles score 0."""
    if not a or not b:
        return 0.0
    return fuzz.token_set_ratio(normalize_role(a), normalize_role(b))
