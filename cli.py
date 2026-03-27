"""Cold-sales CLI — discover targets, write emails, send them.

Run with: .venv/bin/python cli.py

Architecture: a discovery thread qualifies orgs and immediately
dispatches contact-finding + pain-point research to a worker pool.
Ready targets land in a queue.  The main thread shows a Rich live
panel while waiting, then pauses it for the interactive email workflow.
"""
from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from anthropic import Anthropic
from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from mailer import send_email
from pipeline import (
    PipelineState,
    ReadyTarget,
    discover_orgs,
    surface_contacts,
)
from pipeline.models import QualifiedOrg
import config
from pipeline.prompts import build_pain_points_prompt, build_subject_prompt
from recorder import Recorder
from sumble import SumbleClient

load_dotenv()

BATCH_SIZE = 3  # qualified orgs per discovery round
TARGET_BUFFER = 3  # pause discovery when this many targets are ready/in-progress
_console = Console()


def _make_llm_client() -> Anthropic:
    respan_key = os.environ.get("RESPAN_API_KEY")
    if respan_key:
        return Anthropic(
            api_key=respan_key,
            base_url="https://api.respan.ai/api/anthropic/",
        )
    return Anthropic()  # falls back to ANTHROPIC_API_KEY


# ── Display ──────────────────────────────────────────────────────────


def _render_panel(state: PipelineState) -> Panel:
    """Build a Rich panel from the current pipeline state snapshot."""
    snap = state.snapshot()

    parts = Text()

    # Activity section — show more lines for the bigger display
    for line in snap["activity"][-20:]:
        parts.append(line + "\n")

    # Queue section
    n_ready = len(snap["ready"])
    n_progress = len(snap["in_progress"])

    if n_ready or n_progress:
        parts.append(
            f"\n-- Queue ({n_ready} ready, {n_progress} in progress) "
            + "-" * 30
            + "\n",
            style="bold",
        )
        for name, status in snap["in_progress"].items():
            parts.append(f"  ... {name} — {status}\n", style="yellow")
        for summary in snap["ready"]:
            parts.append(f"  OK  {summary}\n", style="green")
    elif not snap["stopped"]:
        parts.append("\nStarting...\n", style="dim")

    if snap["stopped"]:
        parts.append(
            "\nStopping — finishing in-flight work...\n", style="red"
        )

    return Panel(
        parts,
        title="Cold Sales Pipeline",
        border_style="blue",
        expand=True,
        padding=(1, 2),
    )


def _show_target(ready: ReadyTarget) -> None:
    """Display the target context box with Rich formatting."""
    width = 60
    c = _console
    c.print()
    c.print("=" * width, style="bold blue")
    header = f"{ready.org_name} ({ready.org_domain})"
    if ready.employee_count:
        header += f" · {ready.employee_count} employees"
    c.print(header, style="bold")
    c.print(f"{ready.person_name} — {ready.person_title or 'unknown title'}", style="bold cyan")
    c.print("-" * width, style="dim")
    c.print("Qualification:", style="bold", end=" ")
    c.print(ready.qualification, style="green")
    if ready.targeting_reason:
        c.print("Why this person:", style="bold", end=" ")
        c.print(ready.targeting_reason)
    c.print("-" * width, style="dim")
    c.print("Pain points:", style="bold")
    for line in ready.pain_points.strip().splitlines():
        c.print(f"  {line.strip()}")
    c.print("-" * width, style="dim")
    c.print("Suggested subject:", style="bold", end=" ")
    c.print(ready.suggested_subject)
    c.print("-" * width, style="dim")
    c.print("Find email:", style="bold", end=" ")
    c.print(ready.sumble_url, style="underline")
    c.print("=" * width, style="bold blue")
    c.print()


def _open_editor(ready: ReadyTarget) -> str | None:
    """Open $EDITOR with email template. Returns email body or None."""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vim"

    first_name = ready.person_name.split()[0] if ready.person_name else "there"

    header_lines = [
        f"# To: {ready.person_name}, {ready.person_title or 'unknown title'} "
        f"at {ready.org_name} ({ready.org_domain})",
        f"# {ready.qualification[:200]}",
        f"# Pain points:",
    ]
    for line in ready.pain_points.strip().splitlines():
        header_lines.append(f"#   {line.strip()}")
    header_lines += [
        f"# Suggested subject: {ready.suggested_subject}",
        f"# Lines starting with # are stripped before sending.",
        f"# :wq to send  |  :cq to cancel",
        f"",
        f"Hi {first_name},",
        f"",
        f"",
        f"",
        f"{config.load()['sender']['closing']}",
        f"{config.load()['sender']['name']}",
    ]

    content = "\n".join(header_lines) + "\n"

    # Cursor line: the blank line after "Hi Name,"
    # (header comment lines + blank + greeting + first blank)
    cursor_line = len(header_lines) - 3  # the empty line after "Hi Name,"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, prefix="cold-sales-"
    ) as f:
        f.write(content)
        tmppath = f.name

    try:
        cmd = [editor]
        if "vim" in editor or "nvim" in editor:
            cmd.append(f"+{cursor_line}")
        cmd.append(tmppath)
        subprocess.run(cmd, check=True)
        with open(tmppath) as f:
            lines = f.readlines()
        body = "".join(
            line for line in lines if not line.startswith("#")
        ).strip()
        return body or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    finally:
        os.unlink(tmppath)


# ── LLM helpers (pain points + subject line) ────────────────────────


def _research_pain_points(
    org: QualifiedOrg,
    qualification: str,
    recorder: Recorder,
    state: PipelineState,
) -> str:
    """Research company pain points using Opus 4.6 + web search."""
    state.add_activity(f"  Researching pain points at {org.name}...")
    llm = _make_llm_client()
    prompt = build_pain_points_prompt(org, qualification)
    t0 = time.monotonic()
    response = llm.messages.create(
        model="claude-opus-4-6",
        max_tokens=1500,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    text = "\n".join(
        block.text for block in response.content if hasattr(block, "text")
    ).strip()

    usage = getattr(response, "usage", None)
    recorder.record_llm_call(
        purpose="research_pain_points",
        model="claude-opus-4-6",
        prompt=prompt,
        response=text,
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        latency_ms=latency_ms,
        org_domain=org.domain,
    )

    state.add_activity(f"    Pain points found for {org.name}")
    return text


def _suggest_subject(
    org: QualifiedOrg,
    person_name: str,
    person_title: str | None,
    qualification: str,
    pain_points: str,
    recorder: Recorder,
) -> str:
    """Generate a subject line suggestion using Sonnet."""
    llm = _make_llm_client()
    prompt = build_subject_prompt(
        org, person_name, person_title, qualification, pain_points,
    )
    t0 = time.monotonic()
    response = llm.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}],
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    text = "\n".join(
        block.text for block in response.content if hasattr(block, "text")
    ).strip()

    usage = getattr(response, "usage", None)
    recorder.record_llm_call(
        purpose="suggest_subject",
        model="claude-sonnet-4-20250514",
        prompt=prompt,
        response=text,
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        latency_ms=latency_ms,
        org_domain=org.domain,
    )

    return text.strip().strip('"').strip("'")


# ── Contact worker (runs in ThreadPoolExecutor) ─────────────────────


def _enqueue_target(
    ready: ReadyTarget,
    summary: str,
    target_queue: queue.Queue[ReadyTarget | None],
    state: PipelineState,
    stop_event: threading.Event,
) -> None:
    """Add a ready target to the display state and queue."""
    enqueued = False
    while not stop_event.is_set():
        try:
            target_queue.put(ready, timeout=1.0)
            enqueued = True
            break
        except queue.Full:
            continue
    else:
        try:
            target_queue.put(ready, timeout=5.0)
            enqueued = True
        except queue.Full:
            pass
    if enqueued:
        state.add_ready(summary)


def _research_and_queue_target(
    target: dict,
    target_queue: queue.Queue[ReadyTarget | None],
    recorder: Recorder,
    state: PipelineState,
    stop_event: threading.Event,
) -> None:
    """Resume a target — skips steps that already completed."""
    org = QualifiedOrg(
        name=target["org_name"],
        domain=target["org_domain"],
        employee_count=target["org_employee_count"] or 0,
    )
    qualification = target["qualification"] or ""
    try:
        # Reuse pain points if already saved (ctrl+C after Opus but before subject)
        pain_points = target["pain_points"]
        if not pain_points:
            pain_points = _research_pain_points(org, qualification, recorder, state)
            recorder.update_target_research(target["id"], pain_points=pain_points)
        else:
            state.add_activity(f"    Pain points for {org.name} already cached")

        suggested_subject = target["suggested_subject"]
        if not suggested_subject:
            state.add_activity(f"  Generating subject line for {org.name}...")
            suggested_subject = _suggest_subject(
                org, target["person_name"], target["person_title"],
                qualification, pain_points, recorder,
            )
            recorder.update_target_research(
                target["id"], suggested_subject=suggested_subject,
            )
        else:
            state.add_activity(f"    Subject for {org.name} already cached")

        ready = ReadyTarget(
            org_name=org.name,
            org_domain=org.domain,
            employee_count=org.employee_count,
            person_name=target["person_name"],
            person_title=target["person_title"],
            person_id=target["person_id"],
            person_linkedin=target["person_linkedin"],
            qualification=qualification,
            targeting_reason=target["targeting_reason"] or "",
            sumble_url=target["sumble_url"] or "",
            target_id=target["id"],
            pain_points=pain_points,
            suggested_subject=suggested_subject,
            email=target.get("email") or "",
        )
        summary = (
            f"{org.name} — {target['person_name']}, "
            f"{target['person_title'] or 'unknown title'}"
        )
        _enqueue_target(ready, summary, target_queue, state, stop_event)
    except Exception as exc:
        state.add_activity(f"  Research error ({org.name}): {exc}")
    finally:
        state.remove_in_progress(org.name)


def _find_contact_and_queue(
    org: QualifiedOrg,
    qualification: str,
    target_queue: queue.Queue[ReadyTarget | None],
    recorder: Recorder,
    state: PipelineState,
    stop_event: threading.Event,
    exclude_person_ids: set[int] | None = None,
) -> None:
    """Find person, record target, then research pain points and queue.

    The target is recorded to the DB as soon as a contact is found — before
    the Opus pain-points call — so that a crash in research doesn't lose
    the contact work.  On the next restart, ``get_targets_needing_research``
    picks these up and only the cheap research step reruns.
    """
    client = SumbleClient(recorder=recorder)
    try:
        contacts = surface_contacts(
            org,
            client=client,
            recorder=recorder,
            on_status=state.add_activity,
            stop_event=stop_event,
            exclude_person_ids=exclude_person_ids,
        )

        if stop_event.is_set() and not contacts.target:
            return

        if not contacts.target:
            state.add_activity(f"  No suitable contact at {org.name}.")
            return

        person = contacts.target

        targeting_reason = ""
        for ev in contacts.evaluations:
            if ev.is_target:
                targeting_reason = ev.reasoning
                break

        sumble_url = ""
        if contacts.matched_org:
            sumble_url = (
                f"https://sumble.com/orgs/"
                f"{contacts.matched_org.slug}/people/{person.id}"
            )

        # Record target IMMEDIATELY — survives pain-point failures
        target_id = recorder.record_target(
            org_name=org.name,
            org_domain=org.domain,
            org_employee_count=org.employee_count,
            person_name=person.name,
            person_title=person.job_title,
            person_level=person.job_level,
            person_function=person.job_function,
            person_id=person.id,
            person_linkedin=person.linkedin_url,
            person_location=person.location,
            person_country=person.country,
            sumble_url=sumble_url,
            qualification=qualification,
            targeting_reason=targeting_reason,
        )

        # Now do the expensive Opus research — if ctrl+C hits between
        # steps, each result is already in the DB for resume.
        pain_points = _research_pain_points(
            org, qualification, recorder, state,
        )
        recorder.update_target_research(target_id, pain_points=pain_points)

        state.add_activity(f"  Generating subject line for {org.name}...")
        suggested_subject = _suggest_subject(
            org, person.name, person.job_title,
            qualification, pain_points, recorder,
        )
        recorder.update_target_research(target_id, suggested_subject=suggested_subject)

        ready = ReadyTarget(
            org_name=org.name,
            org_domain=org.domain,
            employee_count=org.employee_count,
            person_name=person.name,
            person_title=person.job_title,
            person_id=person.id,
            person_linkedin=person.linkedin_url,
            qualification=qualification,
            targeting_reason=targeting_reason,
            sumble_url=sumble_url,
            target_id=target_id,
            pain_points=pain_points,
            suggested_subject=suggested_subject,
        )
        summary = (
            f"{org.name} — {person.name}, "
            f"{person.job_title or 'unknown title'}"
        )
        _enqueue_target(ready, summary, target_queue, state, stop_event)

    except Exception as exc:
        state.add_activity(f"  Contact error ({org.name}): {exc}")
    finally:
        state.remove_in_progress(org.name)
        client.close()


# ── Producer (background thread) ─────────────────────────────────────


def _wait_for_buffer(state: PipelineState, stop_event: threading.Event, pause_event: threading.Event | None = None) -> bool:
    """Block until the target buffer has room. Returns False if stopped."""
    while not stop_event.is_set():
        if pause_event and pause_event.is_set():
            stop_event.wait(timeout=2.0)
            continue
        snap = state.snapshot()
        if len(snap["ready"]) + len(snap["in_progress"]) < TARGET_BUFFER:
            return True
        stop_event.wait(timeout=2.0)
    return False


def _producer(
    target_queue: queue.Queue[ReadyTarget | None],
    stop_event: threading.Event,
    recorder: Recorder,
    state: PipelineState,
    pause_event: threading.Event,
) -> None:
    """Discovery thread: find orgs, dispatch contact+research workers."""
    client = SumbleClient(recorder=recorder)
    contact_pool = ThreadPoolExecutor(max_workers=2)
    pending_futures: list = []

    qual_reasons: dict[str, str] = {}

    def on_qualified(org: QualifiedOrg, reason: str) -> None:
        qual_reasons[org.domain] = reason
        state.add_in_progress(org.name)
        future = contact_pool.submit(
            _find_contact_and_queue,
            org, reason, target_queue, recorder, state, stop_event,
        )
        pending_futures.append(future)

    try:
        # Phase 0: re-queue targets that were fully ready but the user
        # never saw (e.g. ctrl+C while targets were in the queue).
        # No API calls — just put them straight in the queue.
        ready_targets = recorder.get_ready_targets()
        if ready_targets:
            state.add_activity(
                f"Re-queuing {len(ready_targets)} ready targets..."
            )
            for target in ready_targets:
                if stop_event.is_set():
                    break
                qual_reasons[target["org_domain"]] = target["qualification"] or ""
                ready = ReadyTarget(
                    org_name=target["org_name"],
                    org_domain=target["org_domain"],
                    employee_count=target["org_employee_count"] or 0,
                    person_name=target["person_name"],
                    person_title=target["person_title"],
                    person_id=target["person_id"],
                    person_linkedin=target["person_linkedin"],
                    qualification=target["qualification"] or "",
                    targeting_reason=target["targeting_reason"] or "",
                    sumble_url=target["sumble_url"] or "",
                    target_id=target["id"],
                    pain_points=target["pain_points"],
                    suggested_subject=target["suggested_subject"],
                    email=target.get("email") or "",
                )
                summary = (
                    f"{target['org_name']} — {target['person_name']}, "
                    f"{target['person_title'] or 'unknown title'}"
                )
                _enqueue_target(
                    ready, summary, target_queue, state, stop_event,
                )

        # Phase 1: targets that already have a contact but no research
        # (e.g. Opus crashed after contact was found).  Only needs the
        # cheap pain-points + subject call — no Sumble, no contact eval.
        unresearched = recorder.get_targets_needing_research()
        if unresearched:
            state.add_activity(
                f"Researching {len(unresearched)} saved targets..."
            )
            for target in unresearched:
                if not _wait_for_buffer(state, stop_event, pause_event):
                    break
                qual_reasons[target["org_domain"]] = target["qualification"] or ""
                state.add_in_progress(target["org_name"])
                future = contact_pool.submit(
                    _research_and_queue_target,
                    target, target_queue, recorder, state, stop_event,
                )
                pending_futures.append(future)

        # Phase 2: qualified orgs that never found a contact at all.
        resumable = recorder.get_qualified_without_targets()
        if resumable:
            state.add_activity(
                f"Resuming {len(resumable)} qualified orgs..."
            )
            for row in resumable:
                if not _wait_for_buffer(state, stop_event, pause_event):
                    break
                org = QualifiedOrg(
                    name=row["org_name"],
                    domain=row["org_domain"],
                    employee_count=row["employee_count"] or 0,
                )
                reason = row["reason"] or ""
                qual_reasons[org.domain] = reason
                state.add_in_progress(org.name)
                future = contact_pool.submit(
                    _find_contact_and_queue,
                    org, reason, target_queue, recorder, state, stop_event,
                )
                pending_futures.append(future)

        # Phase 3: discover new orgs.
        while not stop_event.is_set():
            if not _wait_for_buffer(state, stop_event, pause_event):
                break

            skip_domains = recorder.get_known_domains()
            skip_domains |= set(qual_reasons.keys())

            result = discover_orgs(
                target=BATCH_SIZE,
                client=client,
                recorder=recorder,
                on_status=state.add_activity,
                skip_domains=skip_domains,
                stop_event=stop_event,
                on_qualified=on_qualified,
            )

            if stop_event.is_set():
                break

            if not result.qualified:
                state.add_activity("No more qualified targets found.")
                break

    except Exception as exc:
        state.add_activity(f"Producer error: {exc}")
    finally:
        contact_pool.shutdown(wait=True)
        state.stopped = True
        client.close()
        target_queue.put(None)


# ── Consumer (main thread) ───────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Cold sales pipeline")
    parser.add_argument("--init", action="store_true", help="Create coldshot.toml interactively")
    parser.add_argument("--max", type=int, default=None, metavar="N", help="Stop after presenting N targets")
    parser.add_argument("--draft", action="store_true", help="Compose emails without sending")
    args = parser.parse_args()

    if args.init:
        config.init_interactive()
        return

    errors = config.validate()
    if errors:
        for e in errors:
            print(f"  config error: {e}", file=sys.stderr)
        sys.exit(1)

    recorder = Recorder()
    recorder.start_session(pipeline_stage="pipeline")
    state = PipelineState()

    target_queue: queue.Queue[ReadyTarget | None] = queue.Queue(maxsize=5)
    stop_event = threading.Event()
    pause_event = threading.Event()

    producer_thread = threading.Thread(
        target=_producer,
        args=(target_queue, stop_event, recorder, state, pause_event),
        daemon=True,
    )
    producer_thread.start()

    live = Live(
        _render_panel(state),
        console=_console,
        refresh_per_second=4,
    )
    live.start()

    targets_shown = 0
    retry_ready: ReadyTarget | None = None

    try:
        while True:
            if retry_ready is not None:
                ready = retry_ready
                retry_ready = None
            else:
                # Poll queue with timeout so Live keeps refreshing
                try:
                    ready = target_queue.get(timeout=0.25)
                except queue.Empty:
                    live.update(_render_panel(state))
                    if state.stopped and target_queue.empty():
                        try:
                            ready = target_queue.get(timeout=1.0)
                        except queue.Empty:
                            break
                    else:
                        continue

                if ready is None:
                    break

            targets_shown += 1
            state.pop_ready()

            # Pause Live for interactive section
            live.stop()

            _show_target(ready)

            # Interactive command loop
            while True:
                if ready.email:
                    prompt = f"Email [{ready.email}] (Enter=use saved, /skip, d=draft, /stats, q=quit): "
                else:
                    prompt = "Email (Enter=try another, /skip=skip company, d=draft, /stats, /pause, /resume, q=quit): "
                try:
                    raw = input(prompt).strip()
                except EOFError:
                    raw = ""

                if raw == "/stats":
                    stats = recorder.get_stats()
                    _console.print("\n--- Pipeline Stats ---", style="bold")
                    _console.print(f"  Orgs discovered:  {stats['orgs_discovered']}")
                    _console.print(f"  Orgs qualified:   {stats['orgs_qualified']}")
                    _console.print(f"  Targets found:    {stats['targets_found']}")
                    _console.print(f"  Emails sent:      {stats['emails_sent']}", style="green")
                    _console.print(f"  Skipped:          {stats['targets_skipped']}")
                    _console.print(f"  Drafted:          {stats['targets_drafted']}", style="yellow")
                    _console.print(f"  Pending:          {stats['targets_pending']}\n")
                    continue

                if raw == "/pause":
                    pause_event.set()
                    _console.print("  Pipeline paused.", style="yellow")
                    continue

                if raw == "/resume":
                    pause_event.clear()
                    _console.print("  Pipeline resumed.", style="green")
                    continue

                # All other inputs break out of the command loop
                break

            email = raw

            if email.lower() == "q":
                print("Stopping — remaining targets saved for next run.")
                stop_event.set()
                break

            if email == "/skip":
                recorder.mark_target_skipped(ready.target_id)
                print("  Skipped company. Moving on...\n")
                if args.max and targets_shown >= args.max:
                    print(f"Reached --max {args.max} targets. Stopping.")
                    stop_event.set()
                    break
                live.start()
                live.update(_render_panel(state))
                continue

            if not email and ready.email:
                # Enter pressed with a saved email — use it
                email = ready.email

            if not email:
                # Try the next person at this company
                recorder.mark_target_skipped(ready.target_id)
                excluded = recorder.get_tried_person_ids(ready.org_domain)
                org = QualifiedOrg(
                    name=ready.org_name,
                    domain=ready.org_domain,
                    employee_count=ready.employee_count,
                )
                state.add_in_progress(ready.org_name)
                retry_q: queue.Queue[ReadyTarget | None] = queue.Queue(maxsize=1)
                retry_thread = threading.Thread(
                    target=_find_contact_and_queue,
                    args=(
                        org, ready.qualification, retry_q,
                        recorder, state, stop_event,
                    ),
                    kwargs={"exclude_person_ids": excluded},
                    daemon=True,
                )
                retry_thread.start()
                print("  Looking for another contact...\n")
                if args.max and targets_shown >= args.max:
                    print(f"Reached --max {args.max} targets. Stopping.")
                    stop_event.set()
                    break
                # Wait for the same-company result while keeping Live updated
                live.start()
                retry_result = None
                while retry_thread.is_alive():
                    try:
                        retry_result = retry_q.get(timeout=0.25)
                        break
                    except queue.Empty:
                        live.update(_render_panel(state))
                if retry_result is None:
                    try:
                        retry_result = retry_q.get_nowait()
                    except queue.Empty:
                        pass
                if retry_result is not None:
                    retry_ready = retry_result
                else:
                    live.stop()
                    print("  No other contacts found at this company.\n")
                    live.start()
                    live.update(_render_panel(state))
                continue

            if email.lower() == "d":
                recorder.mark_target_drafted(ready.target_id)
                _console.print("  Saved as draft for later.\n", style="yellow")
                if args.max and targets_shown >= args.max:
                    print(f"Reached --max {args.max} targets. Stopping.")
                    stop_event.set()
                    break
                live.start()
                live.update(_render_panel(state))
                continue

            # Persist email immediately so it survives a quit/crash
            recorder.save_target_email(ready.target_id, email)

            try:
                subject = input(
                    f"Subject [{ready.suggested_subject}]: "
                ).strip()
            except EOFError:
                subject = ""

            # Use suggestion if user just presses Enter
            if not subject:
                subject = ready.suggested_subject

            body = _open_editor(ready)
            if not body:
                recorder.mark_target_skipped(ready.target_id)
                print("  Cancelled.\n")
                live.start()
                live.update(_render_panel(state))
                continue

            # Confirmation before sending / saving draft
            action = "Save draft for" if args.draft else "Send to"
            try:
                confirm = input(f"{action} {email}? [Y/n]: ").strip().lower()
            except EOFError:
                confirm = "y"
            if confirm == "n":
                recorder.mark_target_skipped(ready.target_id)
                print("  Cancelled.\n")
                live.start()
                live.update(_render_panel(state))
                continue

            if args.draft:
                print(f"  Saving draft for {email}...")
                outreach_id = recorder.record_outreach(
                    org_name=ready.org_name,
                    org_domain=ready.org_domain,
                    person_name=ready.person_name,
                    person_title=ready.person_title,
                    person_id=ready.person_id,
                    email=email,
                    subject=subject,
                    body=body,
                    status="draft",
                )
                recorder.mark_target_drafted(ready.target_id, outreach_id)
                print("  Draft saved.\n")
            else:
                print(f"  Sending to {email}...")
                msg_id = send_email(
                    to=email,
                    subject=subject,
                    body=body,
                    recorder=recorder,
                    org_name=ready.org_name,
                    org_domain=ready.org_domain,
                    person_name=ready.person_name,
                    person_title=ready.person_title,
                    person_id=ready.person_id,
                )
                outreach_id = recorder.get_outreach_id_by_gmail_msg(msg_id)
                if outreach_id:
                    recorder.mark_target_emailed(ready.target_id, outreach_id)
                print(f"  Sent (message {msg_id})\n")

            if args.max and targets_shown >= args.max:
                print(f"Reached --max {args.max} targets. Stopping.")
                stop_event.set()
                break

            live.start()
            live.update(_render_panel(state))

    except KeyboardInterrupt:
        print("\nStopping...")
        stop_event.set()
    finally:
        try:
            live.stop()
        except Exception:
            pass
        stop_event.set()
        producer_thread.join(timeout=10.0)
        recorder.close()
        print("Done.")


if __name__ == "__main__":
    main()
