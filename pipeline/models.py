from __future__ import annotations

import threading
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from sumble.models import MatchedOrganization, Person


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class QualifiedOrg(_Base):
    name: str
    domain: str
    employee_count: int


class CreditLedger(_Base):
    find_people: int = 0

    @property
    def total(self) -> int:
        return self.find_people


class PersonEvaluation(_Base):
    person: Person
    level: str
    reasoning: str
    is_target: bool


class ContactResult(_Base):
    org: QualifiedOrg
    matched_org: MatchedOrganization | None = None
    target: Person | None = None
    evaluations: list[PersonEvaluation]
    credits: CreditLedger

    @property
    def target_sumble_url(self) -> str | None:
        if self.target and self.matched_org:
            return f"https://sumble.com/orgs/{self.matched_org.slug}/people/{self.target.id}"
        return None


class OrgQualification(_Base):
    org_name: str
    org_domain: str | None = None
    employee_count: int | None = None
    verdict: bool
    reason: str


class DiscoveryResult(_Base):
    qualified: list[QualifiedOrg]
    evaluations: list[OrgQualification]
    sumble_credits: int


class PipelineState:
    """Thread-safe shared state for the Rich display."""

    _MAX_ACTIVITY = 15

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.activity: list[str] = []
        self.in_progress: dict[str, str] = {}
        self.ready_summaries: list[str] = []
        self.stopped: bool = False

    def add_activity(self, msg: str) -> None:
        with self._lock:
            self.activity.append(msg)
            if len(self.activity) > self._MAX_ACTIVITY:
                self.activity = self.activity[-self._MAX_ACTIVITY :]

    def add_in_progress(self, org_name: str) -> None:
        with self._lock:
            self.in_progress[org_name] = "finding contact..."

    def remove_in_progress(self, org_name: str) -> None:
        with self._lock:
            self.in_progress.pop(org_name, None)

    def add_ready(self, summary: str) -> None:
        with self._lock:
            self.ready_summaries.append(summary)

    def pop_ready(self) -> str | None:
        with self._lock:
            if self.ready_summaries:
                return self.ready_summaries.pop(0)
            return None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "activity": list(self.activity),
                "in_progress": dict(self.in_progress),
                "ready": list(self.ready_summaries),
                "stopped": self.stopped,
            }


@dataclass
class ReadyTarget:
    """Everything the consumer (main thread) needs for one target."""
    org_name: str
    org_domain: str
    employee_count: int
    person_name: str
    person_title: str | None
    person_id: int
    person_linkedin: str | None
    qualification: str
    targeting_reason: str
    sumble_url: str
    target_id: int
    pain_points: str
    suggested_subject: str
