from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

from anthropic import Anthropic
from dotenv import load_dotenv

import config
from sumble import SumbleClient
from sumble.models import OrganizationItem

from .models import DiscoveryResult, OrgQualification, QualifiedOrg
from .prompts import build_qualify_prompt

if TYPE_CHECKING:
    from recorder import Recorder


def _make_llm_client() -> Anthropic:
    load_dotenv()
    respan_key = os.environ.get("RESPAN_API_KEY")
    if respan_key:
        return Anthropic(
            api_key=respan_key,
            base_url="https://api.respan.ai/api/anthropic/",
        )
    return Anthropic()  # falls back to ANTHROPIC_API_KEY


def _qualify_org(
    llm: Anthropic,
    org: OrganizationItem,
    recorder: Recorder | None = None,
    on_status: Callable[[str], None] | None = None,
) -> OrgQualification:
    if on_status:
        on_status(f"  Qualifying {org.name} ({org.domain})... searching the web")
    prompt = build_qualify_prompt(org)
    t0 = time.monotonic()
    response = llm.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    # Extract text from multi-block response (web search produces mixed blocks)
    text = "\n".join(
        block.text for block in response.content if hasattr(block, "text")
    ).strip()
    verdict = any(
        line.strip().upper().startswith("VERDICT:") and "YES" in line.upper()
        for line in text.splitlines()
    )
    # Extract everything after "REASON:" (may span multiple lines)
    reason_line = ""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.upper().startswith("REASON:"):
            rest = line.split(":", 1)[1].strip()
            remaining = [rest] if rest else []
            remaining.extend(
                l.strip() for l in lines[i + 1 :] if l.strip()
            )
            reason_line = " ".join(remaining)
            break

    if recorder is not None:
        usage = getattr(response, "usage", None)
        recorder.record_llm_call(
            purpose="qualify_org",
            model="claude-sonnet-4-20250514",
            prompt=prompt,
            response=text,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            latency_ms=latency_ms,
            org_domain=org.domain,
            verdict="YES" if verdict else "NO",
        )

    reason = reason_line or text
    if on_status:
        tag = "YES" if verdict else "NO"
        on_status(f"    → {tag}: {reason[:120]}")

    return OrgQualification(
        org_name=org.name,
        org_domain=org.domain,
        employee_count=org.total_employees,
        verdict=verdict,
        reason=reason,
    )


def discover_orgs(
    *,
    target: int = 10,
    client: SumbleClient | None = None,
    recorder: Recorder | None = None,
    on_status: Callable[[str], None] | None = None,
    skip_domains: set[str] | None = None,
    stop_event: threading.Event | None = None,
    on_qualified: Callable[[QualifiedOrg, str], None] | None = None,
) -> DiscoveryResult:
    """Find and qualify companies matching the discovery filters."""
    own_client = client is None
    if own_client:
        client = SumbleClient(recorder=recorder)

    llm = _make_llm_client()
    qualified: list[QualifiedOrg] = []
    evaluations: list[OrgQualification] = []
    total_credits = 0
    offset = 0
    batch_size = 5
    skip = skip_domains or set()

    disc = config.load().get("discovery", {})
    tech_filters: dict[str, object] = {
        "technologies": disc.get("technologies", []),
    }
    min_employees = disc.get("min_employees", 50)
    max_employees = disc.get("max_employees", 500)

    try:
        while len(qualified) < target:
            if stop_event is not None and stop_event.is_set():
                break
            if on_status:
                on_status(
                    f"Searching Sumble for companies "
                    f"(offset {offset})..."
                )
            resp = client.find_organizations(
                filters=tech_filters,
                order_by_column="jobs_count_growth_6mo",
                order_by_direction="DESC",
                limit=batch_size,
                offset=offset,
            )
            total_credits += resp.credits_used

            if not resp.organizations:
                if on_status:
                    on_status("  No more organizations found.")
                break

            # Filter candidates first
            candidates: list[OrganizationItem] = []
            for org in resp.organizations:
                if org.total_employees is None:
                    continue
                if not (min_employees <= org.total_employees <= max_employees):
                    continue
                if not org.domain:
                    continue
                if org.domain in skip:
                    if on_status:
                        on_status(f"  Skipping {org.name} (already processed)")
                    continue
                candidates.append(org)

            # Qualify in parallel — each worker gets its own LLM client
            # because httpx.Client is not thread-safe.
            if candidates:
                def _qualify_worker(org: OrganizationItem) -> OrgQualification:
                    worker_llm = _make_llm_client()
                    return _qualify_org(
                        worker_llm, org, recorder, on_status,
                    )

                with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
                    futures = {
                        pool.submit(_qualify_worker, org): org
                        for org in candidates
                    }
                    for future in as_completed(futures):
                        if stop_event is not None and stop_event.is_set():
                            pool.shutdown(wait=False, cancel_futures=True)
                            break
                        org = futures[future]
                        try:
                            qual = future.result()
                        except Exception:
                            continue
                        evaluations.append(qual)

                        if recorder is not None:
                            recorder.record_discovered_org(
                                org_name=org.name,
                                org_domain=org.domain,
                                employee_count=org.total_employees,
                                industry=org.industry,
                                hq_country=org.headquarters_country,
                                hq_state=org.headquarters_state,
                                linkedin_url=org.linkedin_organization_url,
                                verdict="YES" if qual.verdict else "NO",
                                reason=qual.reason,
                            )

                        if qual.verdict:
                            qorg = QualifiedOrg(
                                name=org.name,
                                domain=org.domain,
                                employee_count=org.total_employees,
                            )
                            qualified.append(qorg)
                            if on_qualified is not None:
                                on_qualified(qorg, qual.reason)

            if len(qualified) >= target:
                break

            offset += batch_size
            if offset >= resp.total:
                break

        if on_status:
            on_status(f"Discovery done: {len(qualified)} qualified orgs found.")

        return DiscoveryResult(
            qualified=qualified,
            evaluations=evaluations,
            sumble_credits=total_credits,
        )
    finally:
        if own_client:
            client.close()
