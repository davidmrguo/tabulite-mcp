"""Two-step confirmation for destructive tools.

Deleting an imported table throws away work, so it cannot happen in a single
tool call. The first call issues a short-lived, single-use token describing
exactly what would be destroyed; only a second call carrying that token *and*
the literal word the user was asked to type will go through.

What this guarantees: no one call can delete anything, and the warning is
always generated before a deletion is possible. What it cannot guarantee is
that a human, rather than the model, typed the confirmation word — no server
can see that. It makes the human step the path of least resistance and leaves
an obvious trace when it is skipped.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

# The word the user has to type. Compared exactly, capitals included.
CONFIRMATION_WORD = "DELETE"

DEFAULT_TTL_SECONDS = 300.0


class ConfirmationError(Exception):
    """Raised when a confirmation is missing, wrong, expired or reused."""


@dataclass(frozen=True)
class _Pending:
    action: str
    target: str
    expires_at: float


class ConfirmationRegistry:
    """In-memory store of pending destructive actions.

    Tokens live only as long as the server process: a restart cancels every
    pending confirmation, which is the safe direction to fail.
    """

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def issue(self, action: str, target: str) -> tuple[str, float]:
        """Register a pending action; return its token and expiry timestamp."""
        token = secrets.token_urlsafe(12)
        expires_at = time.time() + self._ttl
        with self._lock:
            self._purge_locked()
            self._pending[token] = _Pending(action, target, expires_at)
        return token, expires_at

    def consume(self, token: str | None, action: str, target: str, confirmation: str | None) -> None:
        """Validate and spend a token, or raise :class:`ConfirmationError`.

        Both halves are required: the token proves a warning was issued for
        this exact target, and the word proves the user answered it.
        """
        if confirmation is None or confirmation.strip() != CONFIRMATION_WORD:
            raise ConfirmationError(
                f"this action requires the user to type {CONFIRMATION_WORD} "
                f"(exactly, in capitals); pass it as confirm=\"{CONFIRMATION_WORD}\" "
                "only after they have actually typed it"
            )
        if not token:
            raise ConfirmationError(
                "missing confirmation_token: call this tool without a confirmation "
                "first, show the user the warning it returns, and use the token from it"
            )

        with self._lock:
            self._purge_locked()
            pending = self._pending.get(token)
            if pending is None:
                raise ConfirmationError(
                    "confirmation_token is unknown, already used or expired; "
                    "start again without a confirmation to get a fresh warning"
                )
            if pending.action != action or pending.target != target:
                raise ConfirmationError(
                    f"confirmation_token was issued for '{pending.target}', not '{target}'"
                )
            del self._pending[token]  # single use

    def pending_count(self) -> int:
        with self._lock:
            self._purge_locked()
            return len(self._pending)

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()

    def _purge_locked(self) -> None:
        now = time.time()
        for token in [t for t, p in self._pending.items() if p.expires_at <= now]:
            del self._pending[token]
