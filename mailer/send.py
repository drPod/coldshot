from __future__ import annotations

import base64
import os
from email.message import EmailMessage
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from recorder import Recorder

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TOKEN_PATH = _PROJECT_ROOT / "token.json"

# Find the credentials file (name varies per Google Cloud project)
def _find_credentials_file() -> Path:
    for p in _PROJECT_ROOT.glob("client_secret*.json"):
        return p
    raise FileNotFoundError(
        "No client_secret*.json found in project root. "
        "Download OAuth credentials from Google Cloud Console."
    )


def _get_gmail_service():
    creds = None
    if _TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), _SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(_find_credentials_file()), _SCOPES
            )
            creds = flow.run_local_server(port=0)
        _TOKEN_PATH.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def send_email(
    to: str,
    subject: str,
    body: str,
    *,
    recorder: Recorder | None = None,
    org_name: str = "",
    org_domain: str = "",
    person_name: str = "",
    person_title: str | None = None,
    person_id: int | None = None,
) -> str:
    """Send a plain-text email via Gmail. Returns the message ID."""
    service = _get_gmail_service()

    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = service.users().messages().send(
        userId="me", body={"raw": encoded}
    ).execute()
    gmail_msg_id: str = result["id"]

    if recorder is not None:
        recorder.record_outreach(
            org_name=org_name,
            org_domain=org_domain,
            person_name=person_name,
            person_title=person_title,
            person_id=person_id,
            email=to,
            subject=subject,
            body=body,
            gmail_msg_id=gmail_msg_id,
        )

    return gmail_msg_id
