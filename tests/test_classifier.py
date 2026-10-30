import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.classifier import EmailClassifier, EmailType, parse_classification
from app.classifier.metrics import company_match, confusion, per_type_metrics
from app.classifier.schema import OUTPUT_SCHEMA, ClassificationParseError, failed
from app.config import Settings
from app.gmail.message import EmailMessage
from eval.run_eval import load_dataset, to_message


def payload(**overrides):
    data = {
        "type": "interview_invite",
        "company": "Baseten",
        "role": "Forward Deployed Engineer",
        "interview_datetime": "2026-11-03T14:00:00-05:00",
        "meeting_link": "https://meet.google.com/abc-defg-hij",
        "confidence": 0.93,
    }
    data.update(overrides)
    return json.dumps(data)


# --- parsing ---------------------------------------------------------------


def test_parse_valid_interview():
    c = parse_classification(payload())
    assert c.type is EmailType.INTERVIEW_INVITE
    assert (c.company, c.role) == ("Baseten", "Forward Deployed Engineer")
    assert c.interview_datetime == datetime.fromisoformat("2026-11-03T14:00:00-05:00")
    assert c.meeting_link == "https://meet.google.com/abc-defg-hij"
    assert c.confidence == 0.93
    assert c.error is None


def test_parse_accepts_dict():
    assert parse_classification(json.loads(payload())).type is EmailType.INTERVIEW_INVITE


@pytest.mark.parametrize(
    "raw,match",
    [
        ("not json", "invalid JSON"),
        ("[1, 2]", "JSON object"),
        (payload(type="phone_screen"), "unknown type"),
        (payload(type=None), "unknown type"),
        (payload(confidence="high"), "confidence"),
        (payload(confidence=True), "confidence"),
        (payload(confidence=None), "confidence"),
    ],
)
def test_parse_rejects_unusable_output(raw, match):
    with pytest.raises(ClassificationParseError, match=match):
        parse_classification(raw)


@pytest.mark.parametrize("value,expected", [(1.7, 1.0), (-0.2, 0.0), (1, 1.0)])
def test_parse_clamps_confidence(value, expected):
    assert parse_classification(payload(confidence=value)).confidence == expected


def test_parse_blank_and_whitespace_strings():
    c = parse_classification(payload(company="  ", role="  Data\n Scientist "))
    assert c.company is None
    assert c.role == "Data Scientist"


def test_parse_drops_naive_or_invalid_datetime():
    assert (
        parse_classification(payload(interview_datetime="2026-11-03T14:00:00")).interview_datetime
        is None
    )
    assert (
        parse_classification(payload(interview_datetime="next Tuesday")).interview_datetime is None
    )
    assert parse_classification(
        payload(interview_datetime="2026-11-03T19:00:00Z")
    ).interview_datetime == datetime(2026, 11, 3, 19, tzinfo=UTC)


@pytest.mark.parametrize("link", ["meet.google.com/abc", "javascript:alert(1)", "https://a b", ""])
def test_parse_drops_bad_links(link):
    assert parse_classification(payload(meeting_link=link)).meeting_link is None


def test_interview_fields_only_kept_for_interviews():
    c = parse_classification(payload(type="rejection"))
    assert c.interview_datetime is None and c.meeting_link is None


def test_needs_review():
    assert parse_classification(payload(confidence=0.74)).needs_review(0.75)
    assert not parse_classification(payload(confidence=0.75)).needs_review(0.75)
    assert failed("refusal").needs_review(0.0)


def test_output_schema_matches_types():
    assert OUTPUT_SCHEMA["properties"]["type"]["enum"] == [t.value for t in EmailType]
    assert set(OUTPUT_SCHEMA["required"]) == set(OUTPUT_SCHEMA["properties"])
    assert OUTPUT_SCHEMA["additionalProperties"] is False


# --- classifier with a fake Anthropic client ---------------------------------


class FakeAnthropic:
    def __init__(self, *, text=None, stop_reason="end_turn", blocks=None):
        if blocks is None:
            blocks = [SimpleNamespace(type="thinking", thinking="")]
            if text is not None:
                blocks.append(SimpleNamespace(type="text", text=text))
        self.response = SimpleNamespace(stop_reason=stop_reason, content=blocks)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def message():
    return EmailMessage(
        gmail_id="g1",
        thread_id="t1",
        sender_name="Baseten",
        sender_email="no-reply@ashbyhq.com",
        subject="Interview Confirmation",
        snippet="snippet",
        received_at=datetime(2026, 10, 28, 12, tzinfo=UTC),
        body_text="Your interview is confirmed for Tuesday at 2pm EST.",
    )


def make_classifier(fake):
    settings = Settings(_env_file=None, timezone="America/New_York")
    return EmailClassifier(fake, settings, model="claude-opus-5-5")


def test_classifier_request_shape():
    fake = FakeAnthropic(text=payload())
    result = make_classifier(fake).classify(message())

    assert result.type is EmailType.INTERVIEW_INVITE
    (call,) = fake.calls
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": OUTPUT_SCHEMA}
    assert call["output_config"]["effort"] == "low"
    assert call["fallbacks"] == "default"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    user = call["messages"][0]["content"]
    assert "America/New_York" in user
    assert "Your interview is confirmed" in user
    assert "<email_body>" in user


def test_classifier_uses_snippet_when_no_body():
    fake = FakeAnthropic(text=payload())
    msg = message().__class__(**{**message().__dict__, "body_text": None})
    make_classifier(fake).classify(msg)
    assert "snippet" in fake.calls[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    "fake,error",
    [
        (FakeAnthropic(stop_reason="refusal", blocks=[]), "refusal"),
        (FakeAnthropic(text='{"type": "rejec', stop_reason="max_tokens"), "max_tokens"),
        (FakeAnthropic(blocks=[]), "no_text"),
        (FakeAnthropic(text="not json"), "parse: invalid JSON"),
    ],
)
def test_classifier_failures_go_to_review(fake, error):
    result = make_classifier(fake).classify(message())
    assert result.type is EmailType.OTHER
    assert result.error.startswith(error)
    assert result.needs_review(0.75)


# --- metrics ---------------------------------------------------------------


def test_per_type_metrics():
    pairs = [("a", "a"), ("a", "b"), ("b", "b"), ("c", "b")]
    m = per_type_metrics(pairs, ["a", "b", "c", "d"])
    assert (m["a"].precision, m["a"].recall) == (1.0, 0.5)
    assert m["b"].precision == pytest.approx(1 / 3) and m["b"].recall == 1.0
    assert m["b"].f1 == pytest.approx(0.5)
    assert m["c"].precision is None and m["c"].recall == 0.0 and m["c"].f1 is None
    assert m["d"].support == 0 and m["d"].recall is None


def test_confusion_counts_only_errors():
    assert confusion([("a", "a"), ("a", "b"), ("a", "b")]) == {("a", "b"): 2}


@pytest.mark.parametrize(
    "gold,pred,expected",
    [
        ("Databricks", "Databricks, Inc.", True),
        ("Avenue Z", "Avenue Z, Inc.", True),
        ("Disney", "The Walt Disney Company", True),
        ("Goodwin", "Goodwin Procter", True),
        ("Home Depot", "The Home Depot", True),
        ("Chime", "Stripe", False),
        ("Via", "Avia Systems", False),
        (None, None, True),
        ("Chime", None, False),
    ],
)
def test_company_match(gold, pred, expected):
    assert company_match(gold, pred) is expected


# --- labeled dataset -------------------------------------------------------


def test_labeled_dataset_is_well_formed():
    rows = load_dataset()
    assert 90 <= len(rows) <= 120
    assert len({r["id"] for r in rows}) == len(rows)
    types = {t.value for t in EmailType}
    assert {r["label"]["type"] for r in rows} == types
    for r in rows:
        msg = to_message(r)
        assert msg.sender_email and msg.subject and msg.body_text
        assert msg.received_at.tzinfo is not None


def test_haiku_requests_omit_unsupported_fallback():
    fake = FakeAnthropic(text=payload())
    settings_ = Settings(_env_file=None, timezone="America/New_York")
    EmailClassifier(fake, settings_, model="claude-haiku-5-5").classify(message())
    call = fake.calls[0]
    assert "fallbacks" not in call and "betas" not in call
    assert call["output_config"]["effort"] == "low"
