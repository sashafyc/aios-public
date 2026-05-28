"""
error_classifier.py — классификация ошибок раннеров с hints для bridge.

Вместо тупого retry на любую ошибку — умная реакция:
  - context overflow → compress and retry
  - auth error → не ретраить, алертить
  - rate limit → backoff с задержкой
  - timeout → retry один раз
  - model not found → fallback или алерт
  - unknown → retry один раз

Использование:
    from error_classifier import classify_error
    classified = classify_error(error_text, exit_code)
    if classified.should_retry: ...
    if classified.should_compress: ...
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ErrorKind(Enum):
    AUTH = "auth"
    AUTH_PERMANENT = "auth_permanent"
    RATE_LIMIT = "rate_limit"
    OVERLOADED = "overloaded"
    TIMEOUT = "timeout"
    CONTEXT_OVERFLOW = "context_overflow"
    MODEL_NOT_FOUND = "model_not_found"
    BILLING = "billing"
    SERVER_ERROR = "server_error"
    TOOL_BUDGET = "tool_budget"
    UNKNOWN = "unknown"


@dataclass
class ClassifiedError:
    kind: ErrorKind
    original: str
    should_retry: bool = False
    should_compress: bool = False
    should_alert: bool = False
    retry_after_s: int = 0
    hint: str = ""


_PATTERNS: list[tuple[re.Pattern, ErrorKind, dict]] = [
    # Context overflow
    (re.compile(r"context.*(too long|overflow|exceed|limit)", re.I),
     ErrorKind.CONTEXT_OVERFLOW,
     {"should_retry": True, "should_compress": True, "hint": "compress context and retry"}),
    (re.compile(r"max.*token.*exceed|token.*limit.*reached|prompt.*too.*long", re.I),
     ErrorKind.CONTEXT_OVERFLOW,
     {"should_retry": True, "should_compress": True, "hint": "compress context and retry"}),

    # Rate limit
    (re.compile(r"rate.?limit|429|too many requests|throttl", re.I),
     ErrorKind.RATE_LIMIT,
     {"should_retry": True, "retry_after_s": 30, "hint": "rate limited, backoff"}),
    (re.compile(r"retry.after|please.try.again.later", re.I),
     ErrorKind.RATE_LIMIT,
     {"should_retry": True, "retry_after_s": 60, "hint": "rate limited, long backoff"}),

    # Auth
    (re.compile(r"401|unauthorized|invalid.api.key|auth.*fail|invalid.*token", re.I),
     ErrorKind.AUTH,
     {"should_alert": True, "hint": "auth failed, check API key"}),
    (re.compile(r"403|forbidden|access.denied|permission.denied", re.I),
     ErrorKind.AUTH_PERMANENT,
     {"should_alert": True, "hint": "access denied, check permissions"}),

    # Billing
    (re.compile(r"402|payment|billing|quota.*exceed|insufficient.*fund|credit", re.I),
     ErrorKind.BILLING,
     {"should_alert": True, "hint": "billing issue, check subscription"}),

    # Model not found
    (re.compile(r"model.*not.*found|model.*not.*available|unknown.*model|invalid.*model", re.I),
     ErrorKind.MODEL_NOT_FOUND,
     {"should_alert": True, "hint": "model not available"}),

    # Overloaded
    (re.compile(r"overloaded|503|service.unavailable|capacity|temporarily", re.I),
     ErrorKind.OVERLOADED,
     {"should_retry": True, "retry_after_s": 15, "hint": "service overloaded, retry"}),

    # Server error
    (re.compile(r"500|502|504|internal.server.error|bad.gateway|gateway.timeout", re.I),
     ErrorKind.SERVER_ERROR,
     {"should_retry": True, "retry_after_s": 5, "hint": "server error, quick retry"}),

    # Timeout
    (re.compile(r"timeout|timed?.out", re.I),
     ErrorKind.TIMEOUT,
     {"should_retry": True, "hint": "timeout, retry once"}),

    # Tool budget
    (re.compile(r"tool.budget.exhausted", re.I),
     ErrorKind.TOOL_BUDGET,
     {"should_alert": False, "hint": "agent hit tool budget limit"}),
]


def classify_error(error_text: str, exit_code: int = 0) -> ClassifiedError:
    if not error_text:
        return ClassifiedError(kind=ErrorKind.UNKNOWN, original="", hint="empty error")

    for pattern, kind, opts in _PATTERNS:
        if pattern.search(error_text):
            return ClassifiedError(
                kind=kind,
                original=error_text[:500],
                **opts,
            )

    return ClassifiedError(
        kind=ErrorKind.UNKNOWN,
        original=error_text[:500],
        should_retry=True,
        hint="unknown error, retry once",
    )


def extract_retry_after(error_text: str) -> int | None:
    """Extract Retry-After seconds from error message if present."""
    m = re.search(r"retry.after[:\s]+(\d+)", error_text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"try again in (\d+)\s*s", error_text, re.I)
    if m:
        return int(m.group(1))
    return None
