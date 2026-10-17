from collections import deque
from datetime import UTC, datetime, timedelta

_failures: dict[str, deque[datetime]] = {}


def key_for(user_id: int | None, login_value: str) -> str:
    return f"id:{user_id}" if user_id is not None else f"login:{login_value.strip().lower()}"


def _prune(key: str, window: timedelta, now: datetime) -> deque[datetime]:
    attempts = _failures.setdefault(key, deque())
    while attempts and now - attempts[0] >= window:
        attempts.popleft()
    return attempts


def retry_after_seconds(key: str, *, max_failures: int, window_minutes: int) -> int | None:
    now = datetime.now(UTC)
    window = timedelta(minutes=window_minutes)
    attempts = _prune(key, window, now)
    if len(attempts) < max_failures:
        return None
    return max(1, int((attempts[0] + window - now).total_seconds()))


def register_failure(key: str) -> None:
    _failures.setdefault(key, deque()).append(datetime.now(UTC))


def reset(key: str) -> None:
    _failures.pop(key, None)


def clear_all() -> None:
    _failures.clear()
