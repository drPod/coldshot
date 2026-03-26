from __future__ import annotations

import config
from sumble.models import OrganizationItem, Person

from .models import QualifiedOrg


def build_eval_prompt(person: Person, org: QualifiedOrg) -> str:
    cfg = config.load()
    product = cfg["product"]
    scoring = cfg["targeting"]["scoring"]

    criteria = "\n".join(f"- {p}" for p in scoring)
    # Don't include LinkedIn URL — LinkedIn blocks bot/model access,
    # so the model can't actually view profiles.
    fields = [
        f"Name: {person.name}",
        f"Title: {person.job_title or 'unknown'}",
        f"Level: {person.job_level or 'unknown'}",
        f"Function: {person.job_function or 'unknown'}",
        f"In role since: {person.start_date or 'unknown'}",
    ]
    person_block = "\n".join(f"  {f}" for f in fields)

    return f"""You're deciding who to cold email about {product['pitch']}.

Company: {org.name} ({org.domain}), {org.employee_count} employees

Person:
{person_block}

Criteria:
{criteria}

Research this person and their company. Then respond with exactly one line:
TARGET: [one sentence why]
or
SKIP: [one sentence why]"""


def build_qualify_prompt(org: OrganizationItem) -> str:
    cfg = config.load()
    product = cfg["product"]

    return (
        f"You are qualifying {org.name} ({org.domain or org.url or 'unknown'}, "
        f"{org.total_employees or 'unknown'} employees) as a sales prospect "
        f"for {product['name']}, {product['pitch']}.\n\n"
        f"{product['qualifier']}\n\n"
        "Research this company. Then respond in EXACTLY this format:\n"
        "VERDICT: YES or NO\n"
        "REASON: One sentence — why they would or wouldn't need "
        f"{product['name']}. Be specific to this company."
    )


def build_pain_points_prompt(org: QualifiedOrg, qualification: str) -> str:
    cfg = config.load()
    product = cfg["product"]
    focus = cfg["research"]["focus"]

    focus_lines = "\n".join(f"- {f}" for f in focus)

    return (
        f"You are researching {org.name} ({org.domain}, {org.employee_count} employees) "
        f"to find specific pain points related to {product['pitch']}.\n\n"
        f"What we know: {qualification}\n\n"
        f"Research this company. Identify 2-3 specific, concrete pain points they "
        f"likely face. Focus on:\n"
        f"{focus_lines}\n\n"
        f"Respond with 2-3 bullet points. Each should be specific to THIS company, "
        f"not generic. Reference what you learned about their product."
    )


def build_subject_prompt(
    org: QualifiedOrg,
    person_name: str,
    person_title: str | None,
    qualification: str,
    pain_points: str,
) -> str:
    cfg = config.load()
    product = cfg["product"]

    return (
        f"Write a cold email subject line for reaching out to {person_name} "
        f"({person_title or 'unknown title'}) at {org.name} about {product['name']}, "
        f"{product['pitch']}.\n\n"
        f"Company: {org.name} ({org.domain}), {org.employee_count} employees\n"
        f"Why they qualify: {qualification}\n"
        f"Their pain points: {pain_points}\n\n"
        f"The subject should be:\n"
        f"- Under 50 characters\n"
        f"- Specific to their company, not generic\n"
        f"- Casual and direct, not salesy\n"
        f"- Something that would make a technical person open the email\n\n"
        f"Respond with ONLY the subject line, nothing else."
    )
