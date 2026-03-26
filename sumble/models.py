from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ── Organization ──────────────────────────────────────────────────────


class MatchedOrganization(_Base):
    id: int
    slug: str
    name: str
    domain: str


# ── Filters ───────────────────────────────────────────────────────────


class PeopleFilters(_Base):
    job_functions: list[str] | None = None
    job_levels: list[str] | None = None
    countries: list[str] | None = None
    since: str | None = None


class JobFilters(_Base):
    technologies: list[str] | None = None
    technology_categories: list[str] | None = None
    countries: list[str] | None = None
    since: str | None = None


class EnrichFilters(_Base):
    technologies: list[str] | None = None
    technology_categories: list[str] | None = None
    since: str | None = None


# ── Response entities ─────────────────────────────────────────────────


class Person(_Base):
    id: int
    url: str
    linkedin_url: str | None = None
    name: str
    job_title: str | None = None
    job_function: str | None = None
    job_level: str | None = None
    location: str | None = None
    country: str | None = None
    start_date: str | None = None
    country_code: str | None = None


class RelatedPerson(Person):
    direction: str | None = None


class Job(_Base):
    id: int
    organization_id: int
    organization_name: str
    organization_domain: str | None = None
    job_title: str
    datetime_pulled: str
    primary_job_function: str | None = None
    location: str | None = None
    teams: str | None = None
    matched_projects: str | None = None
    projects_description: str | None = None
    matched_technologies: str | None = None
    matched_job_functions: str | None = None
    projects: str | None = None
    description: str | None = None
    url: str


class Technology(_Base):
    name: str
    last_job_post: str | None = None
    jobs_count: int
    jobs_data_url: str
    people_count: int
    people_data_url: str
    teams_count: int
    teams_data_url: str


# ── API responses ─────────────────────────────────────────────────────


class _ApiResponse(_Base):
    id: str
    credits_used: int
    credits_remaining: int


class FindPeopleResponse(_ApiResponse):
    organization: MatchedOrganization
    people_count: int
    people: list[Person]
    people_data_url: str


class FindJobsResponse(_ApiResponse):
    total: int
    jobs: list[Job]
    source_data_url: str


class JobRelatedPeopleResponse(_ApiResponse):
    total: int
    people: list[Person]
    source_data_url: str


class PersonRelatedPeopleResponse(_ApiResponse):
    total: int
    people: list[RelatedPerson]
    source_data_url: str


class EnrichResponse(_ApiResponse):
    organization: MatchedOrganization
    technologies_found: str
    technologies_count: int
    source_data_url: str
    technologies: list[Technology]


class OrganizationItem(_Base):
    id: int
    name: str
    domain: str | None = None
    url: str | None = None
    industry: str | None = None
    total_employees: int | None = None
    matching_people_count: int | None = None
    matching_team_count: int | None = None
    matching_job_post_count: int | None = None
    headquarters_country: str | None = None
    headquarters_state: str | None = None
    linkedin_organization_url: str | None = None


class FindOrganizationsResponse(_ApiResponse):
    total: int
    organizations: list[OrganizationItem]
    source_data_url: str
