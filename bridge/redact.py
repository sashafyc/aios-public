"""
redact.py — маскирование секретов в логах и ответах агентов.

v9.10: regex-based redaction. Паттерны: API keys, tokens, passwords.
Короткие (<18 символов) маскируются полностью, длинные — first 4 + last 4.

Использование:
    from redact import redact_secrets
    safe_text = redact_secrets(text)

Вызывается из tg_bridge при логировании ответов агентов.
"""

from __future__ import annotations

import re

_PATTERNS: list[re.Pattern] = [
    re.compile(p) for p in [
        # Anthropic
        r"sk-ant-api\w{10,}",
        r"sk-ant-oat\w{10,}",
        # OpenAI
        r"sk-[A-Za-z0-9]{20,}",
        # GitHub
        r"ghp_[A-Za-z0-9]{36,}",
        r"gho_[A-Za-z0-9]{36,}",
        r"ghs_[A-Za-z0-9]{36,}",
        r"github_pat_[A-Za-z0-9_]{20,}",
        # Google
        r"AIza[A-Za-z0-9\-_]{35}",
        # AWS
        r"AKIA[A-Z0-9]{16}",
        # Telegram bot tokens
        r"\d{8,10}:[A-Za-z0-9_\-]{35}",
        # AssemblyAI
        r"[0-9a-f]{32}",  # 32-char hex (AssemblyAI, generic)
        # Stripe
        r"sk_live_[A-Za-z0-9]{20,}",
        r"sk_test_[A-Za-z0-9]{20,}",
        # SendGrid
        r"SG\.[A-Za-z0-9\-_]{20,}",
        # HuggingFace
        r"hf_[A-Za-z0-9]{30,}",
        # npm
        r"npm_[A-Za-z0-9]{30,}",
        # Supabase
        r"sbp_[A-Za-z0-9]{30,}",
        # DeepSeek (generic bearer-style)
        r"sk-[a-f0-9]{32,}",
        # Generic long hex/base64 tokens (40+ chars, likely secrets)
        r"(?<![A-Za-z0-9])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9])",
    ]
]

# Sensitive env var name patterns (for key=value in logs)
_ENV_KEY_RE = re.compile(
    r"((?:API_KEY|SECRET|TOKEN|PASSWORD|APIKEY|AUTH)[A-Z_]*)\s*=\s*(\S+)",
    re.IGNORECASE,
)


def _mask(value: str) -> str:
    if len(value) < 18:
        return "***REDACTED***"
    return value[:4] + "…" + value[-4:]


def redact_secrets(text: str) -> str:
    if not text:
        return text
    result = text
    for pat in _PATTERNS:
        result = pat.sub(lambda m: _mask(m.group(0)), result)
    result = _ENV_KEY_RE.sub(
        lambda m: f"{m.group(1)}={_mask(m.group(2))}",
        result,
    )
    return result
