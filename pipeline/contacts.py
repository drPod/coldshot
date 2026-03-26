from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from anthropic import Anthropic
from dotenv import load_dotenv

from sumble import PeopleFilters, SumbleClient
from sumble.models import Person

from .models import (
    ContactResult,
    CreditLedger,
    PersonEvaluation,
    QualifiedOrg,
)
from .prompts import build_eval_prompt

if TYPE_CHECKING:
    from recorder import Recorder

_LEVELS = ["CXO", "VP", "Director", "Head", "Manager"]


def _make_llm_client() -> Anthropic:
    load_dotenv()
    respan_key = os.environ.get("RESPAN_API_KEY")
    if respan_key:
        return Anthropic(
            api_key=respan_key,
            base_url="https://api.respan.ai/api/anthropic/",
        )
    return Anthropic()  # falls back to ANTHROPIC_API_KEY


def _evaluate_person(
    llm: Anthropic,
    person: Person,
    org: QualifiedOrg,
    recorder: Recorder | None = None,
    on_status: Callable[[str], None] | None = None,
) -> PersonEvaluation:
    prompt = build_eval_prompt(person, org)
    t0 = time.monotonic()
    response = llm.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    # Extract text from multi-block response (web search produces mixed blocks)
    text = "\n".join(
        block.text for block in response.content if hasattr(block, "text")
    ).strip()
    is_target = any(
        line.strip().upper().startswith("TARGET:")
        for line in text.splitlines()
    )

    if recorder is not None:
        usage = getattr(response, "usage", None)
        recorder.record_llm_call(
            purpose="evaluate_person",
            model="claude-sonnet-4-20250514",
            prompt=prompt,
            response=text,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            latency_ms=latency_ms,
            org_domain=org.domain,
            person_id=person.id,
            verdict="TARGET" if is_target else "SKIP",
        )

    if on_status:
        tag = "TARGET" if is_target else "SKIP"
        title = person.job_title or "unknown title"
        on_status(f"      {tag}: {person.name} ({title}) — {text[:80]}")

    return PersonEvaluation(
        person=person,
        level=person.job_level or "unknown",
        reasoning=text,
        is_target=is_target,
    )


def surface_contacts(
    org: QualifiedOrg,
    *,
    client: SumbleClient | None = None,
    recorder: Recorder | None = None,
    on_status: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
) -> ContactResult:
    """Walk down the org chart level by level, evaluating each person
    with an LLM until finding the right cold outreach target."""
    own_client = client is None
    if own_client:
        client = SumbleClient(recorder=recorder)

    if on_status:
        on_status(f"  Finding contact at {org.name}...")

    llm = _make_llm_client()

    # Reuse evaluations from previous runs (avoids re-paying for LLM calls)
    eval_cache: dict[int, tuple[bool, str]] = {}
    if recorder is not None:
        eval_cache = recorder.get_person_eval_cache(org.domain)

    try:
        scope = client.org(domain=org.domain)
        credits = CreditLedger()
        evaluations: list[PersonEvaluation] = []
        matched_org = None

        for level in _LEVELS:
            if stop_event is not None and stop_event.is_set():
                break
            if on_status:
                on_status(f"    Checking {level} level...")
            resp = scope.find_people(
                filters=PeopleFilters(job_levels=[level]),
                limit=5,
            )
            credits.find_people += resp.credits_used
            matched_org = resp.organization

            for person in resp.people:
                if stop_event is not None and stop_event.is_set():
                    break

                if person.id in eval_cache:
                    is_target, reasoning = eval_cache[person.id]
                    evaluation = PersonEvaluation(
                        person=person,
                        level=person.job_level or "unknown",
                        reasoning=reasoning,
                        is_target=is_target,
                    )
                    if on_status:
                        tag = "TARGET" if is_target else "SKIP"
                        title = person.job_title or "unknown title"
                        on_status(
                            f"      {tag}: {person.name} ({title}) — cached"
                        )
                else:
                    evaluation = _evaluate_person(
                        llm, person, org, recorder=recorder,
                        on_status=on_status,
                    )

                evaluations.append(evaluation)

                if evaluation.is_target:
                    return ContactResult(
                        org=org,
                        matched_org=matched_org,
                        target=person,
                        evaluations=evaluations,
                        credits=credits,
                    )

        if on_status:
            on_status(f"  No suitable contact found at {org.name}.")

        return ContactResult(
            org=org,
            matched_org=matched_org,
            target=None,
            evaluations=evaluations,
            credits=credits,
        )
    finally:
        if own_client:
            client.close()
