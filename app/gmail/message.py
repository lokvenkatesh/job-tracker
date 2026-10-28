"""Gmail API message dicts -> EmailMessage.

Bodies are kept out of repr so they never end up in logs."""

import base64
import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parseaddr
from html.parser import HTMLParser

MAX_BODY_CHARS = 20_000


@dataclass(frozen=True)
class EmailMessage:
    gmail_id: str
    thread_id: str
    sender_name: str
    sender_email: str
    subject: str
    snippet: str
    received_at: datetime
    label_ids: tuple[str, ...] = ()
    body_text: str | None = field(default=None, repr=False)

    @property
    def sender_domain(self) -> str:
        return self.sender_email.rpartition("@")[2].lower()


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in {"br", "p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(markup)
    text = "".join(extractor.parts)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t\r\f\v]+", " ", text)).strip()


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _collect_bodies(part: dict, plain: list[str], htmls: list[str]) -> None:
    mime = part.get("mimeType", "")
    data = part.get("body", {}).get("data")
    if data and not part.get("filename"):
        if mime == "text/plain":
            plain.append(_decode(data))
        elif mime == "text/html":
            htmls.append(_decode(data))
    for child in part.get("parts", []) or []:
        _collect_bodies(child, plain, htmls)


def extract_body(payload: dict) -> str | None:
    plain: list[str] = []
    htmls: list[str] = []
    _collect_bodies(payload, plain, htmls)
    if plain:
        text = "\n".join(plain).strip()
    elif htmls:
        text = html_to_text("\n".join(htmls))
    else:
        return None
    return text[:MAX_BODY_CHARS]


def parse_message(msg: dict) -> EmailMessage:
    """Parse a messages.get response (format 'metadata' or 'full')."""
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    name, addr = parseaddr(headers.get("from", ""))
    received_ms = int(msg.get("internalDate", 0))
    return EmailMessage(
        gmail_id=msg["id"],
        thread_id=msg["threadId"],
        sender_name=name,
        sender_email=addr.lower(),
        subject=headers.get("subject", ""),
        snippet=html.unescape(msg.get("snippet", "")),
        received_at=datetime.fromtimestamp(received_ms / 1000, tz=UTC),
        label_ids=tuple(msg.get("labelIds", [])),
        body_text=extract_body(payload) if "parts" in payload or "body" in payload else None,
    )
