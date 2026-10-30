"""Claude email classifier: one structured-output call per email that passed the prefilter."""

import logging

import anthropic

from app.classifier.prompt import SYSTEM_PROMPT, build_user_message
from app.classifier.schema import (
    OUTPUT_SCHEMA,
    Classification,
    ClassificationParseError,
    failed,
    parse_classification,
)
from app.config import Settings, get_settings
from app.gmail.message import EmailMessage
from app.llm import fallback_kwargs

log = logging.getLogger(__name__)

MAX_TOKENS = 4096


class EmailClassifier:
    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        settings: Settings | None = None,
        *,
        model: str | None = None,
        effort: str = "low",
    ) -> None:
        settings = settings or get_settings()
        if client is None:
            key = settings.anthropic_api_key
            client = anthropic.Anthropic(api_key=key.get_secret_value() if key else None)
        self.client = client
        self.model = model or settings.claude_classifier_model
        self.effort = effort
        self.timezone = settings.timezone

    def classify(self, msg: EmailMessage) -> Classification:
        """API errors propagate (the SDK already retried 429/5xx), so the caller leaves the sync
        cursor where it was and the email is retried next run. A refusal or unusable output
        returns a failed() result, which lands in the review queue."""
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            **fallback_kwargs(self.model),
            system=[
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{"role": "user", "content": build_user_message(msg, timezone=self.timezone)}],
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
        )

        if response.stop_reason == "refusal":
            log.warning("Classifier refused gmail_id=%s", msg.gmail_id)
            return failed("refusal")
        if response.stop_reason == "max_tokens":
            log.warning("Classifier hit max_tokens gmail_id=%s", msg.gmail_id)
            return failed("max_tokens")

        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            return failed("no_text")
        try:
            result = parse_classification(text)
        except ClassificationParseError as e:
            log.warning("Unparseable classification gmail_id=%s: %s", msg.gmail_id, e)
            return failed(f"parse: {e}")
        log.info(
            "Classified gmail_id=%s type=%s confidence=%.2f",
            msg.gmail_id,
            result.type,
            result.confidence,
        )
        return result
