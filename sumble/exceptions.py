from __future__ import annotations

from typing import Any

import httpx


class SumbleAPIError(Exception):
    """Base exception for Sumble API errors."""

    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        self.detail: Any
        try:
            self.detail = response.json()
        except Exception:
            self.detail = response.text
        super().__init__(f"HTTP {self.status_code}: {self.detail}")


class NotFoundError(SumbleAPIError):
    """404 — resource not found."""


class AuthenticationError(SumbleAPIError):
    """401 — invalid or missing API key."""


class InsufficientCreditsError(SumbleAPIError):
    """402 — not enough credits."""


class ValidationError(SumbleAPIError):
    """422 — invalid request parameters."""


class RateLimitError(SumbleAPIError):
    """429 — too many requests."""


class ServerError(SumbleAPIError):
    """500 — Sumble server error."""
