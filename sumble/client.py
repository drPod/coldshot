from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from recorder import Recorder

import httpx
from dotenv import load_dotenv

from .exceptions import (
    AuthenticationError,
    InsufficientCreditsError,
    NotFoundError,
    RateLimitError,
    ServerError,
    SumbleAPIError,
    ValidationError,
)
from .models import (
    EnrichFilters,
    EnrichResponse,
    FindJobsResponse,
    FindOrganizationsResponse,
    FindPeopleResponse,
    JobFilters,
    JobRelatedPeopleResponse,
    PeopleFilters,
    PersonRelatedPeopleResponse,
)

BASE_URL = "https://api.sumble.com/v5"

_ERROR_MAP: dict[int, type[SumbleAPIError]] = {
    401: AuthenticationError,
    402: InsufficientCreditsError,
    404: NotFoundError,
    422: ValidationError,
    429: RateLimitError,
    500: ServerError,
}


def _build_org_identifier(
    *,
    domain: str | None = None,
    id: int | None = None,
    slug: str | None = None,
    linkedin_url: str | None = None,
) -> dict[str, Any]:
    if domain is not None:
        return {"domain": domain}
    if id is not None:
        return {"id": id}
    if slug is not None:
        return {"slug": slug}
    if linkedin_url is not None:
        return {"linkedin_url": linkedin_url}
    raise ValueError("Provide one of: domain, id, slug, linkedin_url")


class SumbleClient:
    """Typed client for the Sumble v5 API.

    Usage::

        client = SumbleClient()                       # reads SUMBLE_API_KEY from .env
        org = client.org(domain="stripe.com")          # bind an org once
        people = org.find_people()                     # chain calls without re-specifying
        jobs   = org.find_jobs(include_descriptions=True)
        enrich = org.enrich(filters=EnrichFilters(technologies=["React"]))
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        recorder: Recorder | None = None,
    ) -> None:
        load_dotenv()
        resolved_key = api_key or os.environ.get("SUMBLE_API_KEY")
        if not resolved_key:
            raise ValueError(
                "No API key provided. Pass api_key= or set SUMBLE_API_KEY in .env"
            )
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {resolved_key}",
                "User-Agent": "coldshot/0.1.0",
            },
            timeout=timeout,
        )
        self._recorder = recorder

    # ── lifecycle ─────────────────────────────────────────────────────

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SumbleClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ── org scoping ───────────────────────────────────────────────────

    def org(
        self,
        *,
        domain: str | None = None,
        id: int | None = None,
        slug: str | None = None,
        linkedin_url: str | None = None,
    ) -> OrgScope:
        """Return an org-scoped handle for chaining calls."""
        ident = _build_org_identifier(
            domain=domain, id=id, slug=slug, linkedin_url=linkedin_url
        )
        return OrgScope(self, ident)

    # ── internal ──────────────────────────────────────────────────────

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        t0 = time.monotonic()
        error_str: str | None = None
        status_code = 0
        response_body: dict[str, Any] | None = None
        try:
            for attempt in range(4):
                resp = self._http.post(path, json=body)
                if resp.status_code == 429 and attempt < 3:
                    time.sleep(2 ** (attempt + 1))
                    continue
                status_code = resp.status_code
                if resp.status_code != 200:
                    exc_cls = _ERROR_MAP.get(resp.status_code, SumbleAPIError)
                    error_str = f"HTTP {resp.status_code}"
                    raise exc_cls(resp)
                response_body = resp.json()
                return response_body
            raise SumbleAPIError(resp)  # unreachable, but keeps mypy happy
        except Exception as exc:
            if error_str is None:
                error_str = str(exc)
            raise
        finally:
            if self._recorder is not None:
                credits_used = None
                if response_body and "credits_used" in response_body:
                    credits_used = response_body["credits_used"]
                self._recorder.record_api_call(
                    endpoint=path,
                    request_body=body,
                    response_body=response_body,
                    status_code=status_code,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    credits_used=credits_used,
                    error=error_str,
                )

    # ── endpoints ─────────────────────────────────────────────────────

    def find_people(
        self,
        org: dict[str, Any],
        *,
        filters: PeopleFilters | None = None,
        query: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> FindPeopleResponse:
        body: dict[str, Any] = {
            "organization": org,
            "filters": (
                {"query": query}
                if query
                else (filters or PeopleFilters()).model_dump(exclude_none=True)
            ),
            "limit": limit,
            "offset": offset,
        }
        return FindPeopleResponse.model_validate(self._post("/people/find", body))

    def find_jobs(
        self,
        *,
        org: dict[str, Any] | None = None,
        filters: JobFilters | None = None,
        query: str | None = None,
        include_descriptions: bool = False,
        limit: int = 10,
        offset: int = 0,
    ) -> FindJobsResponse:
        body: dict[str, Any] = {
            "filters": (
                {"query": query}
                if query
                else (filters or JobFilters()).model_dump(exclude_none=True)
            ),
            "include_descriptions": include_descriptions,
            "limit": limit,
            "offset": offset,
        }
        if org is not None:
            body["organization"] = org
        return FindJobsResponse.model_validate(self._post("/jobs/find", body))

    def find_job_related_people(
        self,
        job_id: int,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> JobRelatedPeopleResponse:
        body = {"job_id": job_id, "limit": limit, "offset": offset}
        return JobRelatedPeopleResponse.model_validate(
            self._post("/jobs/find-related-people", body)
        )

    def find_person_related_people(
        self,
        person_id: int,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> PersonRelatedPeopleResponse:
        body = {"person_id": person_id, "limit": limit, "offset": offset}
        return PersonRelatedPeopleResponse.model_validate(
            self._post("/people/find-related-people", body)
        )

    def enrich_org(
        self,
        org: dict[str, Any],
        *,
        filters: EnrichFilters,
    ) -> EnrichResponse:
        body: dict[str, Any] = {
            "organization": org,
            "filters": filters.model_dump(exclude_none=True),
        }
        return EnrichResponse.model_validate(
            self._post("/organizations/enrich", body)
        )

    def find_organizations(
        self,
        *,
        filters: dict[str, Any] | None = None,
        query: str | None = None,
        order_by_column: str | None = None,
        order_by_direction: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> FindOrganizationsResponse:
        body: dict[str, Any] = {
            "filters": (
                {"query": query}
                if query
                else (filters or {})
            ),
            "limit": limit,
            "offset": offset,
        }
        if order_by_column:
            body["order_by_column"] = order_by_column
        if order_by_direction:
            body["order_by_direction"] = order_by_direction
        return FindOrganizationsResponse.model_validate(
            self._post("/organizations/find", body)
        )


class OrgScope:
    """Org-bound handle returned by ``SumbleClient.org()``.

    Every method forwards the stored org identifier so you never
    re-specify it::

        org = client.org(domain="stripe.com")
        people  = org.find_people(filters=PeopleFilters(job_levels=["Director"]))
        jobs    = org.find_jobs(include_descriptions=True)
        enrich  = org.enrich(filters=EnrichFilters(technologies=["Kubernetes"]))
        related = org.find_job_related_people(job_id=jobs.jobs[0].id)
    """

    def __init__(self, client: SumbleClient, org: dict[str, Any]) -> None:
        self._client = client
        self._org = org

    def find_people(
        self,
        *,
        filters: PeopleFilters | None = None,
        query: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> FindPeopleResponse:
        return self._client.find_people(
            self._org, filters=filters, query=query, limit=limit, offset=offset
        )

    def find_jobs(
        self,
        *,
        filters: JobFilters | None = None,
        query: str | None = None,
        include_descriptions: bool = False,
        limit: int = 10,
        offset: int = 0,
    ) -> FindJobsResponse:
        return self._client.find_jobs(
            org=self._org,
            filters=filters,
            query=query,
            include_descriptions=include_descriptions,
            limit=limit,
            offset=offset,
        )

    def enrich(
        self,
        *,
        filters: EnrichFilters,
    ) -> EnrichResponse:
        return self._client.enrich_org(self._org, filters=filters)

    def find_job_related_people(
        self,
        job_id: int,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> JobRelatedPeopleResponse:
        return self._client.find_job_related_people(
            job_id, limit=limit, offset=offset
        )

    def find_person_related_people(
        self,
        person_id: int,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> PersonRelatedPeopleResponse:
        return self._client.find_person_related_people(
            person_id, limit=limit, offset=offset
        )
