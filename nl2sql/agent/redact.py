"""Deterministic redaction of emails and separator-written phone numbers."""
import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(
    r"(?<!\d)(?:"
    r"\+\d{1,3}[\s.-](?:\(?\d{1,4}\)?[\s.-]){1,3}\d{2,4}"
    r"|\(\d{2,4}\)[\s.-]?\d{2,4}[\s.-]\d{2,4}"
    r"|\d{3}[\s.-]\d{3}[\s.-]\d{4}"
    r")(?!\d)"
)
_TOKEN = "[redacted]"


def redact(text: str) -> str:
    """Replace an email or a phone number that contains a separator."""
    cleaned = _EMAIL.sub(_TOKEN, text or "")
    return _PHONE.sub(_TOKEN, cleaned)
