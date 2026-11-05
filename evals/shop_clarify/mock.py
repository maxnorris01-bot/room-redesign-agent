"""The canned callers live in `app.shop.clarify_mock` (the service uses them in mock mode too)."""

from app.shop.clarify_mock import (
    DIMENSION_WORDS,
    QUESTION_TEXT,
    mock_audit_caller,
    mock_caller,
    mock_verify_caller,
    questions_for,
)

__all__ = [
    "DIMENSION_WORDS",
    "QUESTION_TEXT",
    "mock_audit_caller",
    "mock_caller",
    "mock_verify_caller",
    "questions_for",
]
