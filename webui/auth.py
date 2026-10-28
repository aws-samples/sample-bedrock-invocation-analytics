# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Login helpers: credential check, failed-attempt lockout, redirect sanitizing."""

import hmac
import time
from collections import defaultdict, deque

MAX_FAILURES = 5
FAILURE_WINDOW_S = 15 * 60


def safe_redirect(target: str) -> str:
    """Allow only same-site absolute paths (blocks //host and scheme URLs)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def credentials_match(username: str, password: str, expected_user: str, expected_pass: str) -> bool:
    """Constant-time comparison of both fields (no early exit on a wrong username)."""
    user_ok = hmac.compare_digest((username or "").encode(), expected_user.encode())
    pass_ok = hmac.compare_digest((password or "").encode(), expected_pass.encode())
    return user_ok and pass_ok


class LoginThrottle:
    """Per-client lockout after MAX_FAILURES failures within FAILURE_WINDOW_S (in-process memory)."""

    def __init__(self, max_failures: int = MAX_FAILURES, window_s: int = FAILURE_WINDOW_S):
        self.max_failures, self.window_s = max_failures, window_s
        self._failures: dict[str, deque] = defaultdict(deque)

    def is_locked(self, client: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        attempts = self._failures[client]
        while attempts and now - attempts[0] > self.window_s:
            attempts.popleft()
        return len(attempts) >= self.max_failures

    def record_failure(self, client: str, now: float | None = None):
        self._failures[client].append(time.time() if now is None else now)

    def reset(self, client: str):
        self._failures.pop(client, None)
