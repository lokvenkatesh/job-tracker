"""Single-user login. The password is checked against the bcrypt hash in DASHBOARD_PASSWORD_HASH;
a signed session cookie (SESSION_SECRET) keeps you logged in."""

import hmac
import time
from collections import defaultdict, deque

import bcrypt
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status

from app.api.deps import SESSION_USER_KEY, get_settings_dep, require_user
from app.config import Settings

router = APIRouter(prefix="/auth", tags=["auth"])

MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60


class LoginThrottle:
    """In-memory per-client lockout after repeated failures. Enough for a single-user app."""

    def __init__(self, max_failures: int = MAX_FAILURES, window: float = LOCKOUT_SECONDS) -> None:
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def locked(self, key: str, now: float | None = None) -> bool:
        return len(self._recent(key, now or time.monotonic())) >= self.max_failures

    def fail(self, key: str, now: float | None = None) -> None:
        now = now or time.monotonic()
        self._recent(key, now).append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:  # malformed hash
        return False


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, str]:
    if settings.dashboard_password_hash is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Login is not set up. Run: python -m scripts.hash_password",
        )
    throttle: LoginThrottle = request.app.state.login_throttle
    client = request.client.host if request.client else "unknown"
    if throttle.locked(client):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts; try later")

    user_ok = hmac.compare_digest(username.encode(), settings.dashboard_username.encode())
    pass_ok = verify_password(password, settings.dashboard_password_hash.get_secret_value())
    if not (user_ok and pass_ok):
        throttle.fail(client)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong username or password")

    throttle.reset(client)
    request.session.clear()
    request.session[SESSION_USER_KEY] = settings.dashboard_username
    return {"user": settings.dashboard_username}


@router.post("/logout")
def logout(request: Request) -> dict[str, bool]:
    request.session.clear()
    return {"ok": True}


@router.get("/me")
def me(user: str = Depends(require_user)) -> dict[str, str]:
    return {"user": user}
