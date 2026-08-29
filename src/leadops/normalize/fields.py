"""Field-level normalization.

These are small, but they are where duplicate CRM contacts actually come from:
`John@Example.com ` and `john@example.com` are the same person, and
`(555) 010-1234`, `555-010-1234` and `+15550101234` are the same phone.
Getting this wrong is the single most common cause of a messy GHL location.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime

import phonenumbers

_WS = re.compile(r"\s+")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def clean_text(value: object, *, max_len: int = 2000) -> str:
    """Collapse whitespace, strip control characters, bound the length."""
    if value is None:
        return ""
    text = str(value)
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    text = _WS.sub(" ", text).strip()
    return text[:max_len]


def normalize_email(value: object) -> str:
    """Lowercase and trim surrounding whitespace only.

    Two deliberate non-behaviours:

    * Gmail dots and +tags are preserved. Stripping them merges addresses that
      are genuinely different on business domains, and you cannot un-merge two
      customers once their histories are joined.
    * Internal whitespace is *not* removed. Turning "a b@c.com" into "ab@c.com"
      invents a different, deliverable address - which means email to the wrong
      person. An address with a space in it is invalid, and saying so is safer.
    """
    email = clean_text(value, max_len=254).lower()
    if not email or not _EMAIL.match(email):
        return ""
    return email


def normalize_phone(value: object, default_region: str = "US") -> str:
    """Return E.164 when the number parses, otherwise digits-only.

    Returning the digits rather than "" matters: a lead with an unparseable phone
    is still a lead, and we would rather route it than drop it.
    """
    raw = clean_text(value, max_len=40)
    if not raw:
        return ""
    # A number already in international form carries its own country code, so
    # forcing a default region onto it would mis-parse a foreign number.
    region = None if raw.startswith("+") else default_region
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        digits = re.sub(r"\D", "", raw)
        return digits
    if phonenumbers.is_valid_number(parsed):
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    digits = re.sub(r"\D", "", raw)
    return digits


def split_name(full: object) -> tuple[str, str]:
    """Best-effort first/last split for sources that only send one name field."""
    text = clean_text(full, max_len=200)
    if not text:
        return "", ""
    parts = text.split(" ")
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def parse_timestamp(value: object) -> datetime:
    """Accept ISO-8601 (with or without Z) and epoch seconds/milliseconds.

    Falls back to *now* rather than raising: an unreadable timestamp is not a
    reason to reject a sales lead.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float) and value > 0:
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=UTC)
    text = clean_text(value, max_len=64)
    if text:
        if text.isdigit():
            return parse_timestamp(int(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(tz=UTC)
