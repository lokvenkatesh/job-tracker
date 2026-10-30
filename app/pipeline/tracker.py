"""The hourly tracker as a LangGraph graph:

    fetch -> prefilter -> (classify -> match -> update_db -> notify)* -> END

Emails are processed one at a time, oldest first, each in its own transaction, so a later email
in the same batch can match an application created by an earlier one, and a failure partway
through keeps the work already committed (re-runs skip known gmail_ids).
"""

import logging
import operator
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.classifier.classifier import EmailClassifier
from app.classifier.schema import Classification, EmailType
from app.config import Settings
from app.db.matcher import Matcher, MatchResult
from app.db.models import Email, OutreachEmail
from app.db.session import session_scope
from app.embeddings import Embedder, embedding_text
from app.gmail.fetcher import GmailFetcher
from app.gmail.message import EmailMessage
from app.gmail.prefilter import prefilter
from app.gmail.sync_state import SyncState, SyncStateStore
from app.notify.base import InterviewAlert, Notifier
from app.outreach.replies import ReplyAlert, record_reply
from app.pipeline.update import ApplyOutcome, apply_email

log = logging.getLogger(__name__)


@dataclass
class TrackerDeps:
    fetcher: GmailFetcher
    classifier: EmailClassifier
    session_factory: sessionmaker[Session]
    sync_store: SyncStateStore
    notifier: Notifier
    settings: Settings
    embedder: Embedder | None = None
    matcher: Matcher = field(default_factory=Matcher)
    mail_account_id: int | None = None  # which connected inbox this run reads


class TrackerState(TypedDict, total=False):
    history_id: str
    mode: str
    fetched: int
    prefilter_reasons: dict[str, int]
    queue: list[EmailMessage]
    current: EmailMessage | None
    classification: Classification | None
    match: MatchResult | None
    embedding: list[float] | None
    pending_alert: InterviewAlert | None
    pending_reply: ReplyAlert | None
    outcomes: Annotated[list[ApplyOutcome], operator.add]
    alerts_sent: Annotated[int, operator.add]


@dataclass(frozen=True)
class RunSummary:
    mode: str
    fetched: int
    queued: int
    actions: dict[str, int]
    alerts_sent: int
    history_id: str


def build_tracker_graph(deps: TrackerDeps):
    s = deps.settings

    def fetch(state: TrackerState) -> TrackerState:
        result = deps.fetcher.fetch_new(deps.sync_store.load())
        return {
            "history_id": result.history_id,
            "mode": result.mode,
            "fetched": len(result.messages),
            "queue": result.messages,
        }

    def prefilter_node(state: TrackerState) -> TrackerState:
        messages = state["queue"]
        with session_scope(deps.session_factory) as session:
            ids = [m.gmail_id for m in messages]
            known = set(session.scalars(select(Email.gmail_id).where(Email.gmail_id.in_(ids))))
            tracked = set(
                session.scalars(select(Email.thread_id).where(Email.application_id.is_not(None)))
            ) | set(
                session.scalars(
                    select(OutreachEmail.thread_id).where(OutreachEmail.thread_id.is_not(None))
                )
            )
        new = [m for m in messages if m.gmail_id not in known]
        # A thread becomes tracked as soon as any message in it passes, including messages in
        # this same batch (e.g. a confirmation and the recruiter's reply arriving together).
        tracked |= {m.thread_id for m in new if prefilter(m, tracked_thread_ids=tracked).passed}
        reasons: Counter[str] = Counter({"already_processed": len(messages) - len(new)})
        queue = []
        for m in new:
            decision = prefilter(m, tracked_thread_ids=tracked)
            reasons["passed" if decision.passed else decision.reason.split(":")[0]] += 1
            if decision.passed:
                queue.append(m)
        queue.sort(key=lambda m: m.received_at)
        return {"queue": queue, "prefilter_reasons": dict(reasons)}

    def classify(state: TrackerState) -> TrackerState:
        msg, rest = state["queue"][0], state["queue"][1:]
        full = deps.fetcher.get_full(msg.gmail_id)
        classification = deps.classifier.classify(full) if full is not None else None
        return {"queue": rest, "current": full, "classification": classification}

    def match(state: TrackerState) -> TrackerState:
        msg, c = state["current"], state["classification"]
        if msg is None or c is None:
            return {"match": None, "embedding": None}
        embedding = None
        if deps.embedder is not None and c.type is not EmailType.OTHER:
            embedding = deps.embedder.embed(embedding_text(msg, c))
        with session_scope(deps.session_factory) as session:
            result = deps.matcher.match(
                session,
                thread_id=msg.thread_id,
                company=c.company,
                role=c.role,
                embedding=embedding,
            )
        return {"match": result, "embedding": embedding}

    def update_db(state: TrackerState) -> TrackerState:
        msg, c, m = state["current"], state["classification"], state["match"]
        if msg is None or c is None or m is None:
            return {"pending_alert": None, "pending_reply": None}
        with session_scope(deps.session_factory) as session:
            outcome = apply_email(
                session,
                msg,
                c,
                m,
                threshold=s.classifier_confidence_threshold,
                timezone=s.timezone,
                embedding=state.get("embedding"),
                mail_account_id=deps.mail_account_id,
            )
            reply = record_reply(
                session,
                msg,
                application_id=outcome.application_id,
                mail_account_id=deps.mail_account_id,
            )
        log.info("gmail_id=%s type=%s -> %s", msg.gmail_id, c.type.value, outcome.action)
        return {"outcomes": [outcome], "pending_alert": outcome.alert, "pending_reply": reply}

    def notify(state: TrackerState) -> TrackerState:
        reply = state.get("pending_reply")
        on_reply = getattr(deps.notifier, "outreach_reply", None)
        if reply is not None and on_reply is not None:
            try:
                on_reply(reply)
            except Exception:
                log.exception("Reply alert failed for outreach email %s", reply.outreach_email_id)
        alert = state.get("pending_alert")
        if alert is None:
            return {"pending_alert": None}
        try:
            deps.notifier.interview_alert(alert)
        except Exception:
            # The email is already committed; a failed alert must not abort the run. The daily
            # digest still lists upcoming interviews.
            log.exception("Interview alert failed for application %s", alert.application_id)
            return {"pending_alert": None}
        return {"pending_alert": None, "alerts_sent": 1}

    def next_or_end(state: TrackerState) -> str:
        return "classify" if state.get("queue") else END

    graph = StateGraph(TrackerState)
    graph.add_node("fetch", fetch)
    graph.add_node("prefilter", prefilter_node)
    graph.add_node("classify", classify)
    graph.add_node("match", match)
    graph.add_node("update_db", update_db)
    graph.add_node("notify", notify)
    graph.add_edge(START, "fetch")
    graph.add_edge("fetch", "prefilter")
    graph.add_conditional_edges("prefilter", next_or_end, ["classify", END])
    graph.add_edge("classify", "match")
    graph.add_edge("match", "update_db")
    graph.add_edge("update_db", "notify")
    graph.add_conditional_edges("notify", next_or_end, ["classify", END])
    return graph.compile()


def run_once(deps: TrackerDeps, *, now: datetime | None = None) -> RunSummary:
    """One tracker pass. The sync cursor is saved only after every email was processed, so an
    exception (e.g. Claude API down) leaves it in place and the next run retries."""
    graph = build_tracker_graph(deps)
    final = graph.invoke({"outcomes": [], "alerts_sent": 0}, config={"recursion_limit": 1_000_000})
    deps.sync_store.save(
        SyncState(last_history_id=final["history_id"], last_run_at=now or datetime.now(UTC))
    )
    actions = Counter(o.action.split(":")[0] for o in final["outcomes"])
    return RunSummary(
        mode=final["mode"],
        fetched=final["fetched"],
        queued=final["prefilter_reasons"].get("passed", 0),
        actions=dict(actions),
        alerts_sent=final["alerts_sent"],
        history_id=final["history_id"],
    )
