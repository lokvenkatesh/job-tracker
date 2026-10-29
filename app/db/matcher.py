"""Link a classified email to an existing application.

Tiers, in order: same Gmail thread -> fuzzy company + role -> pgvector similarity to emails already
linked to an application. Anything uncertain comes back `ambiguous` so the caller sends it to
review instead of guessing.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.companies import company_match, role_similarity
from app.db.models import Application, Email, Status

ROLE_MATCH_SCORE = 85
ROLE_TIE_MARGIN = 5
VECTOR_MAX_DISTANCE = 0.15  # cosine distance
VECTOR_MARGIN = 0.05


@dataclass(frozen=True)
class MatchResult:
    application_id: int | None
    method: str  # thread | fuzzy | vector | none
    ambiguous: bool = False
    reason: str = ""

    @classmethod
    def none(cls, reason: str = "") -> "MatchResult":
        return cls(None, "none", reason=reason)


@dataclass(frozen=True)
class Candidate:
    id: int
    company: str
    role: str | None
    status: str


def fuzzy_match(
    company: str | None, role: str | None, candidates: Sequence[Candidate]
) -> MatchResult:
    if company is None:
        return MatchResult.none("no_company")
    same_company = [c for c in candidates if company_match(company, c.company)]
    if not same_company:
        return MatchResult.none("no_company_match")

    if role is None:
        if len(same_company) == 1:
            return MatchResult(same_company[0].id, "fuzzy")
        return _prefer_active(same_company, "multiple_candidates")

    scored = sorted(
        ((role_similarity(role, c.role), c) for c in same_company), key=lambda sc: -sc[0]
    )
    best_score = scored[0][0]
    if best_score >= ROLE_MATCH_SCORE:
        tied = [c for score, c in scored if best_score - score < ROLE_TIE_MARGIN]
        if len(tied) == 1:
            return MatchResult(tied[0].id, "fuzzy")
        return _prefer_active(tied, "multiple_candidates")

    # Same company, no role match. One application with no recorded role is safe to link.
    roleless = [c for c in same_company if c.role is None]
    if len(same_company) == 1 and roleless:
        return MatchResult(roleless[0].id, "fuzzy")
    return MatchResult(None, "fuzzy", ambiguous=True, reason="role_mismatch")


def _prefer_active(candidates: Sequence[Candidate], reason: str) -> MatchResult:
    active = [c for c in candidates if c.status != Status.REJECTED]
    if len(active) == 1:
        return MatchResult(active[0].id, "fuzzy")
    return MatchResult(None, "fuzzy", ambiguous=True, reason=reason)


def pick_vector_match(neighbors: Sequence[tuple[int, float]]) -> MatchResult:
    """neighbors: (application_id, cosine distance), nearest first."""
    if not neighbors or neighbors[0][1] > VECTOR_MAX_DISTANCE:
        return MatchResult.none("no_vector_match")
    best_app, best_distance = neighbors[0]
    rivals = [
        app for app, d in neighbors[1:] if app != best_app and d - best_distance < VECTOR_MARGIN
    ]
    if rivals:
        return MatchResult(None, "vector", ambiguous=True, reason="vector_tie")
    return MatchResult(best_app, "vector")


class Matcher:
    def match(
        self,
        session: Session,
        *,
        thread_id: str,
        company: str | None,
        role: str | None,
        embedding: list[float] | None,
    ) -> MatchResult:
        linked = session.scalar(
            select(Email.application_id)
            .where(Email.thread_id == thread_id, Email.application_id.is_not(None))
            .order_by(Email.received_at.desc())
            .limit(1)
        )
        if linked is not None:
            return MatchResult(linked, "thread")

        candidates = [
            Candidate(a.id, a.company, a.role, a.status)
            for a in session.scalars(select(Application))
        ]
        result = fuzzy_match(company, role, candidates)
        if result.application_id is not None or result.ambiguous:
            return result
        if result.reason == "no_company_match" and company is not None:
            # A named company we've never seen is a new application, not a vector lookup.
            return result
        if embedding is None or session.get_bind().dialect.name != "postgresql":
            return result

        distance = Email.embedding.cosine_distance(embedding)
        rows = session.execute(
            select(Email.application_id, distance)
            .where(Email.application_id.is_not(None), Email.embedding.is_not(None))
            .order_by(distance)
            .limit(5)
        ).all()
        return pick_vector_match([(app_id, float(d)) for app_id, d in rows])
