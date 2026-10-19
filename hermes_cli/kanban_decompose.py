"""Kanban decomposer — fan a triage task out into a graph of child tasks.

Invoked by ``hermes kanban decompose [task_id | --all]`` and the gateway
dispatcher's auto-decompose path. Reads the profile roster (with
descriptions), asks the auxiliary LLM for a task graph in JSON, then
atomically creates the children, links them under the root, and flips the
root ``triage -> todo``. The root stays alive as parent of every leaf child so
it wakes back up when the graph completes and its assignee (the orchestrator
profile) can judge completion and add more work.

Mirrors ``kanban_specify`` (lazy aux import, lenient parse, never raises on
expected failures). ``fanout=false`` collapses to the ``specify`` behaviour
(tighten + promote, no children), making ``decompose`` a strict superset.
Unknown assignees are rewritten to ``default_assignee`` — a child NEVER ends
up with ``assignee=None``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_graph import decompose_triage_task
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import profiles as profiles_mod
from hermes_cli.kanban_specify import (
    _call_aux, _extract_json_blob, _load_triage_task, _task_prompt_fields, _title_body,
)
from hermes_cli.kanban_specify import _profile_author as _specify_author

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Kanban decomposer for the Hermes Agent board.

A user dropped a rough idea into the Triage column. Your job is to break it
into a small graph of concrete child tasks and route each one to the best-
matching profile from the available roster.

You will be given:
  - The original task title and body
  - The card's comments, oldest to newest
  - The list of available profiles (each with name + description)
  - The fallback "default_assignee" used when no profile fits

Output a single JSON object with this exact shape:

  {
    "fanout": true,
    "rationale": "<one sentence on why this decomposition>",
    "tasks": [
      {
        "title": "<concrete task title, imperative voice, <= 80 chars>",
        "body":  "<detailed spec for the worker on this child task>",
        "assignee": "<profile name from the roster, or null for default>",
        "parents": [<int>, ...]
      },
      ...
    ]
  }

Rules:
  - "parents" is a list of INDICES (0-based) into this same "tasks" list,
    expressing actual data dependencies. Tasks with no parents run in
    PARALLEL. Tasks with parents wait until every parent completes.
  - Prefer parallelism. If two tasks can be done independently, give
    them no parents so the dispatcher fans them out at once.
  - Use 2-6 tasks for normal work. Don't create 20 tiny tasks. Don't
    cram everything into 1 task.
  - Pick assignees from the roster by matching the task to the profile's
    DESCRIPTION (not just the name). When nothing matches well, use null
    and the system will route to the default_assignee.
  - Each child task body is what a fresh worker will read with no other
    context — be specific about goal, approach, and acceptance criteria.
  - Comments are newer than the card body and therefore authoritative. If a
    comment conflicts with the body, obey the newest comment and carry that
    override into every affected child body.
  - If the instructions cannot be reconciled safely, do not guess. Return
    {"needs_clarification": true, "clarification": "<specific question>"}.

When the task is genuinely a single unit of work (no useful decomposition),
return:

  {
    "fanout": false,
    "rationale": "<one sentence>",
    "title": "<tightened title>",
    "body":  "<concrete spec for a single worker>",
    "assignee": "<profile name from the roster, or null for default>"
  }

In that case the task stays as one work item, just with a tightened spec and
a concrete assignee. If no profile fits, use null and the system will route to
the default_assignee.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Card comments (oldest to newest; newer comments override the body):
{comments}

Available profiles (assignees you may pick from):
{roster}

Default assignee (used when no profile fits a task): {default_assignee}
"""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class DecomposeOutcome:
    """Result of decomposing a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    fanout: bool = False
    child_ids: list[str] | None = None
    new_title: Optional[str] = None


@dataclass
class _SourceContext:
    task: kb.Task
    comments: list[kb.Comment]
    latest_comment_id: int


def _profile_author() -> str:
    """Mirror of ``hermes_cli.kanban._profile_author``."""
    return _specify_author("decomposer")


def _resolve_profile_from_cfg(cfg: dict, key: str, *, fallback: Optional[str] = None) -> str:
    """``kanban.<key>`` if it names an existing profile, else ``fallback``
    (the root task's own assignee) if that does, else the active default
    profile — so a task is never stranded for lack of an owner.
    ``orchestrator_profile`` owns the root after fan-out; ``default_assignee``
    catches children the decomposer can't route.

    The root's assignee sits before the active profile because the decomposer
    runs inside whatever profile hosts the dispatcher — an operator's
    credential-less incognito profile, say — and that profile must never
    silently become the owner of work the card was assigned away from (#114294).
    """
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get(key) or "").strip()
    for candidate in (explicit, (fallback or "").strip()):
        if candidate:
            try:
                if profiles_mod.profile_exists(candidate):
                    return candidate
            except Exception:
                pass
    try:
        return profiles_mod.get_active_profile_name() or "default"
    except Exception:
        return "default"


def _build_roster() -> tuple[list[dict], set[str]]:
    """``(roster_for_prompt, valid_assignee_names)``; entries are
    ``{name, description, has_description}``."""
    try:
        all_profiles = profiles_mod.list_profiles()
    except Exception as exc:
        logger.warning("decompose: failed to list profiles: %s", exc)
        return [], set()
    roster = []
    for p in all_profiles:
        desc = (p.description or "").strip()
        roster.append({
            "name": p.name,
            "description": desc or f"(no description; profile named {p.name!r})",
            "has_description": bool(desc),
        })
    return roster, {p.name for p in all_profiles}


def _format_roster(roster: list[dict]) -> str:
    if not roster:
        return "  (no profiles installed — decomposer cannot route work)"
    return "\n".join(
        f"  - {entry['name']}{'' if entry['has_description'] else ' ⚠ undescribed'}: {entry['description']}"
        for entry in roster
    )


def _normalize_assignee_choice(assignee: object, *, default_assignee: str, valid_names: set[str]) -> str:
    """A valid assignee, else ``default_assignee`` — promoted work is never
    left unassigned."""
    if not isinstance(assignee, str) or not assignee.strip():
        return default_assignee
    chosen = assignee.strip()
    return chosen if chosen in valid_names else default_assignee


@dataclass
class _Routing:
    """Config-derived routing context for one decomposition."""

    orchestrator: str
    default_assignee: str
    auto_promote: bool
    roster: list[dict]
    valid_names: set[str]


def _load_routing(*, root_assignee: Optional[str] = None) -> _Routing:
    from hermes_cli.config import load_config_readonly
    try:
        cfg = load_config_readonly()
    except Exception:  # decompose_task promises ok=False, never a raise, on config trouble
        cfg = {}
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    roster, valid_names = _build_roster()
    return _Routing(
        orchestrator=_resolve_profile_from_cfg(cfg, "orchestrator_profile", fallback=root_assignee),
        default_assignee=_resolve_profile_from_cfg(cfg, "default_assignee", fallback=root_assignee),
        auto_promote=bool(kanban_cfg.get("auto_promote_children", True)),
        roster=roster,
        valid_names=valid_names,
    )


def _apply_single(task: kb.Task, parsed: dict, routing: _Routing, author: str) -> DecomposeOutcome:
    """``fanout=false``: single-task spec promotion (same effect as specify)."""
    title_val, body_val = _title_body(parsed)
    assignee_val = None
    if not task.assignee:
        assignee_val = _normalize_assignee_choice(
            parsed.get("assignee"), default_assignee=routing.default_assignee, valid_names=routing.valid_names,
        )
    if title_val is None and body_val is None:
        return DecomposeOutcome(task.id, False, "decomposer returned fanout=false with no title/body")
    with kbc.connect_closing() as conn:
        ok = kb.specify_triage_task(
            conn, task.id, title=title_val, body=body_val, assignee=assignee_val, author=author,
        )
    if not ok:
        return DecomposeOutcome(task.id, False, "task moved out of triage before promotion")
    return DecomposeOutcome(task.id, True, "single task (no fanout)", fanout=False, new_title=title_val)


def _format_comments(comments: list[kb.Comment]) -> str:
    if not comments:
        return "  (no comments)"
    rendered = []
    for comment in comments[-20:]:
        body = comment.body.strip()
        rendered.append(f"  - comment #{comment.id} by {comment.author}: {body[:1000]}")
    selected = []
    selected_length = 0
    for entry in reversed(rendered):
        separator_length = 1 if selected else 0
        if selected_length + separator_length + len(entry) > 6000:
            break
        selected.append(entry)
        selected_length += separator_length + len(entry)
    return "\n".join(reversed(selected))


def _source_comments_appendix(comments: list[kb.Comment]) -> str:
    if not comments:
        return ""
    return (
        "\n\nAuthoritative source-card comments (newer than the original body; obey on conflict):\n"
        + _format_comments(comments)
    )


def _has_stop_comment(comments: list[kb.Comment]) -> bool:
    """Recognise explicit operator stop/cancel language, not bounded directives
    such as "do not commit to main" that should instead flow into child specs."""
    stop_patterns = (
        r"(?im)^\s*(?:(?:tr|human|operator)\s+decision\s*:\s*)?"
        r"(?:stop|cancel)\b(?=\s*(?:[,;:.!?—-]|$))",
        r"\bstop\s+(?:this\s+)?(?:card|task|work)\b",
        r"\bstop\s+now\b",
        r"\bno\s+further\s+(?:feature\s+)?work\b",
        r"\bnothing\s+should\s+run\b",
        r"\bcancel(?:led)?\s+(?:this\s+)?(?:card|task|work)\b",
    )
    resume_patterns = (
        r"\bresume\s+(?:this\s+)?(?:card|task|work)\b",
        r"\bcontinue\s+(?:this\s+)?(?:card|task|work)\b",
        r"\bproceed\s+with\s+(?:this\s+)?(?:card|task|work)\b",
    )
    stopped = False
    for comment in comments:
        if any(re.search(pattern, comment.body, re.IGNORECASE) for pattern in stop_patterns):
            stopped = True
        if any(re.search(pattern, comment.body, re.IGNORECASE) for pattern in resume_patterns):
            stopped = False
    return stopped


def _load_source_context(task_id: str) -> tuple[Optional[_SourceContext], str]:
    task, reason = _load_triage_task(task_id)
    if task is None:
        return None, reason
    with kbc.connect_closing() as conn:
        comments = kb.list_comments(conn, task_id)
    if task.block_recurrences >= kb.BLOCK_RECURRENCE_LIMIT:
        return None, "human-stopped triage escalation (block loop)"
    if _has_stop_comment(comments):
        return None, "human-stopped by card comment"
    return _SourceContext(task, comments, comments[-1].id if comments else 0), ""


def _source_guard_reason(source: _SourceContext) -> str:
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, source.task.id)
        comments = kb.list_comments(conn, source.task.id)
    if task is None or task.status != "triage":
        return "task moved out of triage before decomposition"
    if task.block_recurrences >= kb.BLOCK_RECURRENCE_LIMIT or _has_stop_comment(comments):
        return "task was human-stopped before decomposition"
    latest_comment_id = comments[-1].id if comments else 0
    if latest_comment_id != source.latest_comment_id:
        return "card comments changed during decomposition; stale plan discarded"
    return ""


def _clean_children(
    task_id: str, raw_tasks: list, routing: _Routing, comments: list[kb.Comment],
) -> tuple[list[dict], str]:
    """Validate/normalise the LLM's ``tasks`` list; ``(children, "")`` or ``([], reason)``.
    Unknown assignees route to the default; never assignee=None."""
    children: list[dict] = []
    comments_appendix = _source_comments_appendix(comments)
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            return [], f"tasks[{idx}] is not an object"
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            return [], f"tasks[{idx}].title is missing or empty"
        body = entry.get("body")
        assignee = entry.get("assignee")
        chosen = _normalize_assignee_choice(
            assignee, default_assignee=routing.default_assignee, valid_names=routing.valid_names,
        )
        if isinstance(assignee, str) and assignee.strip() and assignee.strip() not in routing.valid_names:
            logger.info(
                "decompose: task %s child %d picked unknown assignee %r — "
                "routing to default_assignee %r",
                task_id, idx, assignee, routing.default_assignee,
            )
        parents = entry.get("parents") or []
        if not isinstance(parents, list):
            parents = []
        children.append({
            "title": title.strip()[:200],
            "body": (body.strip() if isinstance(body, str) else "") + comments_appendix,
            "assignee": chosen,
            # Drop non-int, out-of-range and self parent indices.
            "parents": [p for p in parents if isinstance(p, int) and 0 <= p < len(raw_tasks) and p != idx],
        })
    return children, ""


def _apply_fanout(
    source: _SourceContext, parsed: dict, routing: _Routing, author: str,
) -> DecomposeOutcome:
    task_id = source.task.id
    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return DecomposeOutcome(task_id, False, "decomposer returned fanout=true with empty tasks list")
    children, reason = _clean_children(task_id, raw_tasks, routing, source.comments)
    if reason:
        return DecomposeOutcome(task_id, False, reason)
    reason = _source_guard_reason(source)
    if reason:
        return DecomposeOutcome(task_id, False, reason)
    try:
        with kbc.connect_closing() as conn:
            child_ids = decompose_triage_task(
                conn,
                task_id,
                root_assignee=routing.orchestrator,
                children=children,
                author=author,
                auto_promote=routing.auto_promote,
                expected_latest_comment_id=source.latest_comment_id,
            )
    except ValueError as exc:
        return DecomposeOutcome(task_id, False, f"DB rejected graph: {exc}")
    except Exception as exc:
        logger.exception("decompose: DB error on task %s", task_id)
        return DecomposeOutcome(task_id, False, f"DB error: {type(exc).__name__}")
    if child_ids is None:
        return DecomposeOutcome(task_id, False, "task already decomposed or moved out of triage")
    return DecomposeOutcome(
        task_id, True, f"decomposed into {len(child_ids)} children", fanout=True, child_ids=child_ids,
    )


def decompose_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> DecomposeOutcome:
    """Decompose a triage task into a graph of child tasks. Expected failures
    (not in triage, no aux client, API error, malformed/empty reply) surface
    as ``ok=False``."""
    source, reason = _load_source_context(task_id)
    if source is None:
        return DecomposeOutcome(task_id, False, reason)
    task = source.task

    routing = _load_routing(root_assignee=task.assignee)
    raw, reason = _call_aux(
        "decompose", task_id, aux_task="kanban_decomposer", system=_SYSTEM_PROMPT,
        user=_USER_TEMPLATE.format(
            **_task_prompt_fields(task),
            comments=_format_comments(source.comments),
            roster=_format_roster(routing.roster),
            default_assignee=routing.default_assignee,
        ),
        max_tokens=4000, timeout=timeout or 180, log=logger,
    )
    if raw is None:
        return DecomposeOutcome(task_id, False, reason)

    parsed = _extract_json_blob(raw, _FENCE_RE)
    if parsed is None:
        return DecomposeOutcome(task_id, False, "LLM returned malformed JSON")

    audit_author = author or _profile_author()
    if parsed.get("needs_clarification"):
        clarification = parsed.get("clarification")
        if not isinstance(clarification, str) or not clarification.strip():
            clarification = "The card's body and comments could not be reconciled safely."
        with kbc.connect_closing() as conn:
            kb.add_comment(
                conn, task_id, audit_author,
                f"Auto-decomposition paused for clarification: {clarification.strip()}",
            )
        return DecomposeOutcome(task_id, False, "decomposition needs clarification")
    if not parsed.get("fanout"):
        reason = _source_guard_reason(source)
        if reason:
            return DecomposeOutcome(task_id, False, reason)
        body = parsed.get("body")
        if isinstance(body, str):
            parsed = {**parsed, "body": body + _source_comments_appendix(source.comments)}
        return _apply_single(task, parsed, routing, audit_author)
    return _apply_fanout(source, parsed, routing, audit_author)


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Return actionable triage ids, excluding human-stopped escalations."""
    with kbc.connect_closing() as conn:
        rows = kb.list_tasks(conn, status="triage", tenant=tenant, limit=1000)
        return [
            row.id for row in rows
            if row.block_recurrences < kb.BLOCK_RECURRENCE_LIMIT
            and not _has_stop_comment(kb.list_comments(conn, row.id))
        ]
