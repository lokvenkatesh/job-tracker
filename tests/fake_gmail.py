"""Minimal stand-in for the googleapiclient Gmail service. Records every API call made."""

import base64

import httplib2
from googleapiclient.errors import HttpError


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def make_message(
    gmail_id,
    *,
    thread_id=None,
    sender="Jobs <no-reply@example.com>",
    subject="Hello",
    snippet="",
    internal_ms=1_700_000_000_000,
    labels=("INBOX",),
    body=None,
):
    payload = {
        "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]
    }
    if body is not None:
        payload["mimeType"] = "text/plain"
        payload["body"] = {"data": b64(body)}
    return {
        "id": gmail_id,
        "threadId": thread_id or f"t-{gmail_id}",
        "labelIds": list(labels),
        "snippet": snippet,
        "internalDate": str(internal_ms),
        "payload": payload,
    }


def http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"{}")


class _Request:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeGmail:
    def __init__(self, *, messages=(), history_pages=(), list_pages=None, profile_history_id="900"):
        self.messages_by_id = {m["id"]: m for m in messages}
        self.history_pages = list(history_pages)  # list of dicts, or an HttpError to raise
        self.list_pages = (
            list_pages
            if list_pages is not None
            else [{"messages": [{"id": i} for i in self.messages_by_id]}]
        )
        self.profile_history_id = profile_history_id
        self.calls: list[tuple[str, dict]] = []

    # service.users()
    def users(self):
        return self

    def getProfile(self, **kw):
        self.calls.append(("users.getProfile", kw))
        return _Request(lambda: {"historyId": self.profile_history_id})

    def history(self):
        return _Namespace(self, "users.history", list=self._history_list)

    def messages(self):
        return _Namespace(self, "users.messages", list=self._messages_list, get=self._messages_get)

    def _history_list(self, **kw):
        token = kw.get("pageToken")
        index = int(token) if token else 0

        def run():
            page = self.history_pages[index]
            if isinstance(page, Exception):
                raise page
            return page

        return _Request(run)

    def _messages_list(self, **kw):
        token = kw.get("pageToken")
        index = int(token) if token else 0
        return _Request(lambda: self.list_pages[index])

    def _messages_get(self, **kw):
        def run():
            if kw["id"] not in self.messages_by_id:
                raise http_error(404)
            return self.messages_by_id[kw["id"]]

        return _Request(run)


class _Namespace:
    def __init__(self, fake, prefix, **methods):
        self._fake, self._prefix, self._methods = fake, prefix, methods

    def __getattr__(self, name):
        if name not in self._methods:
            raise AttributeError(f"{self._prefix}.{name} is not allowed in the fake")
        method = self._methods[name]

        def call(**kw):
            self._fake.calls.append((f"{self._prefix}.{name}", kw))
            return method(**kw)

        return call
