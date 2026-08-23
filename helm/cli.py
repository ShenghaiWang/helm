"""The small public Helm command-line interface."""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import re
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import watchdog as watchdog_module
from .watchdog import DEFAULT_INTERVAL as WATCHDOG_DEFAULT_INTERVAL
from .core import (
    EFFORT_LEVELS,
    AUTHORITY_ENV,
    DELIVERY_DECISION_KIND,
    BLOCKING_GATE_KINDS,
    GATE_ACTION_KINDS,
    PROTECTED_ACTIONS,
    Coordinator,
    HelmError,
    SafetyError,
    StateStore,
    _private_file,
    _validate_project_id,
    _write_private_text,
    project_glyph,
    worker_environment,
)
from . import doctor as doctor_module
from . import models
from . import preferences
from . import runtimes
from .herdr import (
    DEFAULT_WAIT_TIMEOUT,
    HerdrAdapter,
    HerdrUnavailable,
    SubprocessHerdrClient,
)


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _project_label(project: dict[str, Any]) -> str:
    # The text name and stable ID are always present; the coloured glyph and hex
    # value are additional visual cues, never the identity by themselves.  The
    # glyph is a character rather than an escape sequence, so it survives piped
    # output, NO_COLOR, and a Herdr pane, where a background tint does not.
    glyph = project_glyph(project.get("color", ""))
    prefix = f"{glyph} " if glyph else ""
    return f"{prefix}{project['name']} ({project['id']}) {project['color']}"


def _rgb(color: str) -> tuple[int, int, int] | None:
    value = str(color or "").strip().lstrip("#")
    if len(value) != 6:
        return None
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except ValueError:
        return None


def _color_enabled(stream: Any = None) -> bool:
    """Colour is decoration, so it is dropped whenever it could corrupt output."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("HELM_NO_COLOR"):
        return False
    stream = stream if stream is not None else sys.stdout
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _project_paint(project: dict[str, Any], text: str, *, stream: Any = None) -> str:
    """Tint one line with the project's own colour as its background.

    Several projects report into the same session, so the driver needs to see
    at a glance which one is speaking.  The text still names the project, and
    the colour never carries meaning on its own: identity stays in the label.
    """
    rgb = _rgb(project.get("color", ""))
    if rgb is None or not _color_enabled(stream):
        return text
    red, green, blue = rgb
    # Relative luminance picks a foreground that stays readable on any tint.
    luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
    foreground = "30" if luminance > 0.6 else "97"
    return f"\033[48;2;{red};{green};{blue};{foreground}m{text}\033[0m"


def _print_projects(projects: list[dict[str, Any]]) -> None:
    if not projects:
        print("No projects registered.")
        return
    for project in projects:
        print(
            f"{_project_label(project)}  root={project['root']}  "
            f"delivery={project['delivery_policy']}"
        )


def _print_agents(coordinator: Coordinator, *, check: bool = False) -> None:
    profiles = coordinator.agent_availability() if check else coordinator.list_agent_profiles()

    def print_herdr_only_integrations() -> None:
        if not check:
            return
        integrations = [
            entry
            for entry in coordinator.herdr_integration_availability()
            if not entry["builtin"] and entry["status"].startswith("current")
        ]
        if not integrations:
            return
        names = ", ".join(
            f"{entry['id']} ({'launchable profile' if entry['helm_launchable'] else 'Herdr-only'})"
            for entry in integrations
        )
        print(f"Herdr integrations not in Helm's built-ins: {names}")

    if not profiles:
        # No profile file is the normal case, not a broken one: list the
        # runtimes a task would actually be delegated to.
        for runtime in coordinator.builtin_runtime_availability():
            marker = ""
            if runtime["detected"]:
                marker = " <- this session"
            elif runtime["default"]:
                marker = " <- root default"
            state = (
                "excluded"
                if runtime["excluded"]
                else "available"
                if runtime["available"]
                else "unavailable"
            )
            herdr = ""
            if runtime.get("herdr_integration"):
                herdr = f", herdr={runtime['herdr_integration']}"
            print(f"{runtime['id']} ({runtime['name']}) [built-in, {state}{herdr}]{marker}")
        print_herdr_only_integrations()
        print("No agent profiles configured; built-in runtimes are listed above.")
        return
    for profile in profiles:
        if check:
            state = "available" if profile["available"] else "unavailable"
            if profile.get("builtin"):
                marker = ""
                if profile.get("detected"):
                    marker = " <- this session"
                elif profile.get("default"):
                    marker = " <- root default"
                if profile.get("excluded"):
                    state = "excluded"
                herdr = ""
                if profile.get("herdr_integration"):
                    herdr = f", herdr={profile['herdr_integration']}"
                print(f"{profile['id']} ({profile['name']}) [built-in, {state}{herdr}]{marker} reason={profile['reason']}")
                continue
            print(
                f"{profile['id']} ({profile['name']}) [{state}] "
                f"capacity={profile['active']}/{profile['capacity']} reason={profile['reason']}"
            )
        else:
            domains = ",".join(profile.get("domains", [])) or "-"
            runtime = profile.get("runtime")
            print(
                f"{profile['id']} ({profile['name']}) domains={domains} "
                f"capacity={profile['capacity']}"
                + (f" runtime={runtime}" if runtime else "")
                + f" source={profile['source']}"
            )
    print_herdr_only_integrations()


def _model_cost(entry: Any) -> str:
    """The only two statements Helm makes about cost."""
    return "free" if entry.free else "unknown"


def _profile_runtime_id(
    profile: dict[str, Any],
    *,
    which: "callable[[str], str | None] | None" = None,
) -> str | None:
    """Name the built-in runtime a profile's catalogue may safely be reused from.

    Two provable cases only -- never a bare basename guess, which would credit
    a profile with a built-in's catalogue merely because some other program on
    `PATH` happens to share its name:

    - a validated `runtime` declaration (`_load_agent_profiles` already
      refused anything that does not name a known built-in), as long as an
      explicit command does not name a different program; a declared runtime
      whose command disagrees is a misconfiguration, not evidence.
    - an undeclared command whose executable resolves, through the same
      search Helm would use to launch it, to the exact same file on disk as
      the built-in of that basename -- so `/opt/vendor/pi` is credited only
      when it *is* the `pi` Helm would launch, never merely named `pi`.

    The profile's command is never executed to answer this question.
    """
    find = shutil.which if which is None else which
    declared = profile.get("runtime")
    command = profile.get("command") or []
    basename = Path(str(command[0])).name if command else None
    if declared:
        if basename and basename != declared:
            return None
        return str(declared)
    if basename is None or runtimes.builtin_runtime(basename) is None:
        return None
    profile_resolved = find(command[0])
    builtin_resolved = find(basename)
    if profile_resolved is None or builtin_resolved is None:
        return None
    if profile_resolved != builtin_resolved:
        return None
    return basename


def _agent_models_report(coordinator: Coordinator) -> dict[str, Any]:
    """One deterministic report: readiness per runtime plus live catalogues.

    Catalogue queries run only for runtimes that are launchable *and* not
    excluded by this root -- an excluded runtime's catalogue is not queried,
    because its whole catalogue is unusable with it. `claude`, `codex` and
    `omp` publish no safe catalogue command and are reported unsupported
    rather than guessed at.
    """
    readiness = coordinator.builtin_runtime_availability()
    preference = coordinator.preferences().free_model
    queried: dict[str, Any] = {}
    for entry in readiness:
        if entry["available"] and not entry["excluded"]:
            queried[entry["id"]] = models.query_catalogue(entry["id"])
    runtimes_report = []
    by_id = {entry["id"]: entry for entry in readiness}
    for entry in readiness:
        query = queried.get(entry["id"])
        if query is not None:
            catalogue = {
                "command": list(query.command),
                "supported": query.supported,
                "available": query.available,
                "reason": query.reason,
                "models": [
                    {"id": model.id, "cost": _model_cost(model)}
                    for model in query.models
                ],
            }
        elif not by_id[entry["id"]]["available"]:
            catalogue = {
                "command": [],
                "supported": models.catalogue_command(entry["id"]) is not None,
                "available": False,
                "reason": entry["reason"],
                "models": [],
            }
        else:
            # entry["excluded"] is the only remaining case: every launchable,
            # non-excluded runtime was already queried above.
            catalogue = {
                "command": list(models.catalogue_command(entry["id"]) or ()),
                "supported": models.catalogue_command(entry["id"]) is not None,
                "available": False,
                "reason": "skipped: this root excludes the runtime",
                "models": [],
            }
        runtimes_report.append({
            "id": entry["id"],
            "name": entry["name"],
            "builtin": True,
            "launchable": entry["available"],
            "excluded": entry["excluded"],
            "default": entry["default"],
            "default_reason": entry["default_reason"],
            "detected": entry["detected"],
            "catalogue": catalogue,
        })
    # Same resolved id `builtin_runtime_availability` used above for the
    # built-in rows' own `default` flag, reused here so a profile can be
    # named as the root default too (e.g. `HELM_AGENT=<profile id>`).
    default_agent_id, default_agent_reason = coordinator._root_agent_default()
    excluded_agents = coordinator.excluded_agents()
    for profile in coordinator.list_agent_profiles():
        # A profile's command is never executed here. `_profile_runtime_id`
        # names the built-in runtime only for the two provable cases; anything
        # else (an opaque wrapper, a disagreeing command, no command at all)
        # is reported unsupported rather than guessed at.
        runtime_id = _profile_runtime_id(profile)
        command = profile.get("command") or []
        if runtime_id is not None and runtime_id in excluded_agents:
            # A profile that inherits an excluded runtime is refused the same
            # way a bare `--agent <that runtime>` would be: the root's cost
            # decision, not this command's own shape, is why it has no
            # catalogue -- so it reads exactly like the built-in's own row.
            catalogue = {
                "command": list(models.catalogue_command(runtime_id) or ()),
                "supported": models.catalogue_command(runtime_id) is not None,
                "available": False,
                "reason": "skipped: this root excludes the runtime",
                "models": [],
            }
        else:
            query = queried.get(runtime_id) if runtime_id else None
            if query is not None:
                catalogue = {
                    "command": list(query.command),
                    "supported": query.supported,
                    "available": query.available,
                    "reason": query.reason,
                    "models": [
                        {"id": model.id, "cost": _model_cost(model)}
                        for model in query.models
                    ],
                }
            else:
                catalogue = {
                    "command": [],
                    "supported": False,
                    "available": False,
                    "reason": (
                        f"configured command does not provably invoke a built-in "
                        f"runtime's catalogue command: {' '.join(command) or '(none)'}"
                    ),
                    "models": [],
                }
        runtimes_report.append({
            "id": profile["id"],
            "name": profile["name"],
            "builtin": False,
            "launchable": None,
            "excluded": runtime_id is not None and runtime_id in excluded_agents,
            "default": profile["id"] == default_agent_id,
            "default_reason": default_agent_reason if profile["id"] == default_agent_id else "",
            "detected": False,
            "catalogue": catalogue,
        })
    free_evidence = [
        {"id": model["id"], "runtime": entry["id"]}
        for entry in runtimes_report
        for model in entry["catalogue"]["models"]
        if model["cost"] == "free"
    ]
    return {
        "schema_version": 1,
        "preference": (
            {"key": preferences.KEY_MODEL_FREE, "value": preference}
            if preference
            else None
        ),
        "decision_order": list(coordinator.MODEL_SELECTION_ORDER),
        "rules": list(coordinator.MODEL_SELECTION_RULES),
        "runtimes": runtimes_report,
        "free_evidence": free_evidence,
        "note": (
            "Cost is classified only from an explicit free marker in the "
            "catalogue id; every other model is unknown. Helm reads no "
            "credential store and reports no prices."
        ),
    }


def _print_agent_models(coordinator: Coordinator, *, as_json: bool) -> None:
    report = _agent_models_report(coordinator)
    if as_json:
        print(json.dumps(report, sort_keys=True))
        return
    if report["preference"]:
        print(
            f"preference: {report['preference']['key']} = "
            f"{report['preference']['value']}"
        )
    else:
        print("preference: none (no free-model preference set)")
    print("decision order: " + " > ".join(report["decision_order"]))
    print()
    print(f"{'runtime':<10} {'catalogue':<20} {'status':<12} {'models':<7} {'free'}")
    for entry in report["runtimes"]:
        catalogue = entry["catalogue"]
        command = " ".join(catalogue["command"]) if catalogue["command"] else "(none)"
        if not catalogue["supported"]:
            status = "unsupported"
        elif entry["builtin"] and not entry["launchable"]:
            status = "unavailable"
        elif catalogue["available"]:
            status = "ok"
        else:
            status = "unavailable"
        # `default` fires for HELM_AGENT, the root preference, *and*
        # detection (see `Coordinator._root_agent_default`), but only the
        # first two are a standing choice worth calling "root default" --
        # detection is a guess made for lack of one, the same fact `[this
        # session]` already names for the built-in `detected` flag.
        detected_default = entry["default"] and entry["default_reason"].startswith(
            "same runtime as this Helm session"
        )
        marks = ""
        if entry["excluded"]:
            marks = " [excluded by root]"
        elif entry["default"] and not detected_default:
            marks = " [root default]"
        elif entry["detected"] or detected_default:
            marks = " [this session]"
        print(
            f"{entry['id']:<10} {command:<20} {status:<12} "
            f"{len(catalogue['models']):<7} "
            f"{sum(1 for m in catalogue['models'] if m['cost'] == 'free')}"
            f"{marks}"
        )
        if status != "ok":
            print(f"    reason: {catalogue['reason']}")
    if report["free_evidence"]:
        print()
        print("free (explicit catalogue evidence only):")
        for entry in report["free_evidence"]:
            print(f"  {entry['id']}  ({entry['runtime']})")
    else:
        print()
        print("no explicitly-free models reported by any launchable catalogue")
    print()
    print(report["note"])


def _open_pull_request(
    coordinator: Coordinator, task_id: str, pushed: dict[str, Any]
) -> None:
    """Create the PR with `gh` when it is installed; otherwise hand over a URL.

    Helm never invents a provider API. If the tool a human already uses for
    pull requests is present, use it; if not, say so and print the link rather
    than pretending the PR exists.
    """
    outcome = coordinator.task_outcome(task_id)
    title = outcome["brief"].strip().splitlines()[0][:72]
    body = f"Helm task {task_id} on branch {pushed['branch']}.\n\n{outcome['brief'][:1500]}"
    if shutil.which("gh") is None:
        print("  gh is not installed; open the PR from:")
        print(f"    {pushed['remote_url']}  ({pushed['branch']} -> {pushed['base_branch']})")
        return
    result = subprocess.run(
        [
            "gh", "pr", "create",
            "--head", pushed["branch"],
            "--base", pushed["base_branch"],
            "--title", title,
            "--body", body,
        ],
        cwd=str(Path(coordinator.store.load()["projects"][outcome["project_id"]]["root"])),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = result.stdout.strip()
    print(f"  {output or 'gh reported no output'}")
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("http://") or candidate.startswith("https://"):
            try:
                coordinator.record_pr_opened(task_id, candidate, source="gh")
            except HelmError as exc:
                print(f"  PR URL not recorded in Helm: {exc}")
            else:
                print("  Recorded PR open; monitor it with helm task pr-status")
            return


def _sync_pull_request_status(coordinator: Coordinator, task_id: str) -> dict[str, Any]:
    """Read the task's PR with gh and record the observed delivery state."""
    outcome = coordinator.task_outcome(task_id)
    delivery = outcome.get("delivery") or {}
    url = str(delivery.get("url") or "").strip()
    if not url:
        raise HelmError("task has no recorded PR URL; record it with helm task pr-status --state open --url ...")
    if shutil.which("gh") is None:
        raise HelmError("gh is not installed; record PR observations with helm task pr-status")
    result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            url,
            "--json",
            "url,state,reviewDecision,mergeStateStatus,mergeCommit,comments",
        ],
        cwd=str(Path(coordinator.store.load()["projects"][outcome["project_id"]]["root"])),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        raise HelmError(result.stdout.strip() or "gh pr view failed")
    payload = json.loads(result.stdout or "{}")
    state = str(payload.get("state") or "OPEN").lower()
    merge_commit = payload.get("mergeCommit") or {}
    if isinstance(merge_commit, dict):
        merge_commit = str(merge_commit.get("oid") or "")
    comments = payload.get("comments") or []
    return coordinator.record_pr_status(
        task_id,
        state="merged" if state == "merged" else "closed" if state == "closed" else "open",
        url=str(payload.get("url") or url),
        comments=len(comments) if isinstance(comments, list) else None,
        checks=str(payload.get("mergeStateStatus") or ""),
        review_decision=str(payload.get("reviewDecision") or ""),
        merge_commit=str(merge_commit or ""),
    )



_BOARD_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#16181d;--mut:#5b6270;--line:#e3e6ea}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--fg:#e8eaed;--mut:#98a0ae;--line:#252a33}}
*{box-sizing:border-box}body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mut);font-size:13px;margin-bottom:24px}
.proj{margin:0 0 28px}.proj h2{font-size:16px;margin:0 0 10px;display:flex;gap:8px;align-items:center}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--tone);
border-radius:10px;padding:14px 16px;margin:0 0 10px;overflow:hidden}
.top{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.pill{font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px;background:var(--tone);color:#fff}
.id{font:12px ui-monospace,Menlo,monospace;color:var(--mut)}
.brief{margin:8px 0 0;font-weight:600}
.meta{color:var(--mut);font-size:12px;margin-top:4px}
.result{margin-top:10px;font-size:13px;color:var(--mut);white-space:pre-wrap;
max-height:8.5em;overflow:auto}
.arts{margin-top:10px;display:flex;flex-wrap:wrap;gap:8px}
.arts a{font:12px ui-monospace,Menlo,monospace;text-decoration:none;color:inherit;
border:1px solid var(--line);border-radius:6px;padding:3px 8px}
.arts a.gone{opacity:.45;text-decoration:line-through}
video{margin-top:10px;max-width:min(100%,420px);border-radius:8px;display:block}
pre{margin:8px 0 0;font:12px ui-monospace,Menlo,monospace;color:var(--mut);
overflow-x:auto;white-space:pre}
.none{color:var(--mut)}
"""
_TONES = {"green": "#1f9d55", "amber": "#c98a00", "red": "#c9372c", "grey": "#6b7280"}


def _esc(text: Any) -> str:
    return (
        str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _board_html(projects: list[dict[str, Any]], generated: str) -> str:
    parts = [
        "<!doctype html><meta charset=utf-8><title>Helm board</title>",
        f"<style>{_BOARD_CSS}</style>",
        "<h1>Helm board</h1>",
        f"<div class=sub>Generated {_esc(generated)} \u00b7 what each agent produced, "
        "without merging anything</div>",
    ]
    if not projects:
        parts.append("<p class=none>No tasks yet.</p>")
    for project in projects:
        parts.append(
            f"<section class=proj><h2><span>{_esc(project['glyph'])}</span>"
            f"<span>{_esc(project['name'])}</span>"
            f"<span class=id>{_esc(project['id'])}</span></h2>"
        )
        for task in project["tasks"]:
            tone = _TONES.get(task["tone"], _TONES["grey"])
            parts.append(f"<div class=card style='--tone:{tone}'>")
            parts.append(
                f"<div class=top><span class=pill>{_esc(task['label'])}</span>"
                f"<span class=id>{_esc(task['id'])}</span></div>"
            )
            parts.append(f"<div class=brief>{_esc(task['brief'])}</div>")
            bits = [f"agent {task['agent'] or '-'}", f"domain {task['domain'] or 'none'}"]
            if task.get("branch"):
                bits.append(_esc(task["branch"]))
            joined = " \u00b7 ".join(_esc(b) for b in bits)
            parts.append(f"<div class=meta>{joined}</div>")
            if task["diffstat"]:
                parts.append("<pre>" + _esc(chr(10).join(task["diffstat"])) + "</pre>")
            if task["result"]:
                parts.append(f"<div class=result>{_esc(task['result'])}</div>")
            if task["artifacts"]:
                parts.append("<div class=arts>")
                for art in task["artifacts"]:
                    cls = "" if art["exists"] else " class=gone"
                    parts.append(
                        f"<a{cls} href='file://{_esc(art['abs'])}'>{_esc(art['path'])}</a>"
                    )
                parts.append("</div>")
                for art in task["artifacts"]:
                    if art["exists"] and art["kind"] in {"mp4", "mov", "m4v", "webm"}:
                        parts.append(
                            f"<video controls preload=metadata "
                            f"src='file://{_esc(art['abs'])}'></video>"
                        )
            parts.append("</div>")
        parts.append("</section>")
    return chr(10).join(parts)


def _print_outcome(outcome: dict[str, Any]) -> None:
    print(
        f"{outcome.get('glyph', '')} Task {outcome['task_id']} [{outcome['status']}] "
        f"project={outcome['project_id']} agent={outcome['agent_id']}"
    )
    print(f"  brief: {outcome['brief'][:160]}")
    print(f"  branch: {outcome['branch']} (from {outcome['base_branch']})")
    print(f"  worktree: {outcome['workspace']}{'' if outcome['workspace_exists'] else '  [GONE]'}")
    if outcome["commits"]:
        print("  commits:")
        for line in outcome["commits"]:
            print(f"    {line}")
    if outcome["diffstat"]:
        print("  changes:")
        for line in outcome["diffstat"]:
            print(f"    {line.strip()}")
    if outcome["dirty"]:
        # Uncommitted work is invisible to a merge and to a reviewer.
        print(f"  UNCOMMITTED ({len(outcome['dirty'])} path(s)) - not on the branch:")
        for line in outcome["dirty"][:10]:
            print(f"    {line}")
    if outcome["artifacts"]:
        print("  artifacts:")
        for entry in outcome["artifacts"]:
            where = "project+worktree" if entry["in_project"] and entry["in_worktree"] else (
                "worktree only" if entry["in_worktree"] else
                "project only" if entry["in_project"] else "MISSING"
            )
            print(f"    {entry['path']}  [{where}]")
    for message in outcome["messages"][-4:]:
        print(f"  {message['kind']}: {message['text'][:300]}")
    if outcome["workspace_exists"]:
        print(f"  open it:  cd {outcome['workspace']}")
        # The pinned revision, not the movable branch name: `base_branch`
        # can have advanced since this task was created, and diffing
        # against it would pull in commits nobody on this task wrote --
        # the same defect `task_outcome()` itself already avoids.
        diff_base = outcome.get("base_revision") or outcome["base_branch"]
        print(f"  full diff: git -C {outcome['workspace']} diff {diff_base}...HEAD")


def _print_delivery(delivered: list[dict[str, Any]]) -> None:
    if not delivered:
        print("No build outputs to deliver.")
        return
    for entry in delivered:
        if entry["status"] == "delivered":
            print(f"  delivered {entry['path']}")
        elif entry["status"] == "exists":
            print(f"  SKIPPED {entry['path']} (differs from the project copy; --force to replace)")
        elif entry["status"] == "missing":
            print(f"  MISSING {entry['path']} (recorded but not in the worktree)")


def _liveness_probe(coordinator):
    """A Herdr-aware liveness answer, or None when nothing can tell.

    `worker_health` judges a paneless worker on an idle log file alone, which
    reads a six-minute model call as a stall. Only the Herdr layer can ask the
    provider whether the session is still there, so the caller supplies it and
    core stays provider-neutral. Any failure answers "unknown" rather than
    "dead": an unavailable provider must never look like evidence.
    """
    adapter = None

    def probe(worker):
        nonlocal adapter
        try:
            if adapter is None:
                adapter = HerdrAdapter(coordinator)
            return adapter._provider_worker_alive(worker)
        except Exception:
            return None

    return probe

def _when_label(stamp: str | None = None, seconds: float | None = None) -> str:
    """When it arrived, in local time, beside how long it has been waiting.

    Two different questions, and a reader needs both. The age answers "how long
    have I left this" -- the one that decides whether to act now. The clock time
    answers "when did this reach me", which is what a reader correlates against
    a deploy, a log, or their own memory of what they were doing, and what an
    age alone cannot give back.

    Local time on purpose: the reader is deciding about their own day, not
    reconciling zones. Date included because a bare HH:MM on a nine-day-old
    item reads as this morning.
    """
    moment = None
    if stamp:
        with contextlib.suppress(ValueError):
            moment = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if moment is None and seconds is not None and seconds >= 0:
        moment = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds)
    if moment is None:
        return f"{'':>11} {_age_label(stamp, seconds)}"
    local = moment.astimezone()
    return f"{local:%m-%d %H:%M} {_age_label(stamp, seconds)}"


def _age_label(stamp: str | None = None, seconds: float | None = None) -> str:
    """How long this has been waiting, compactly, for the front of a line.

    Relative rather than absolute on purpose: the question a reader is asking
    is "has this been ignored, or did it just happen", and "6d" answers it
    where "2026-08-11 04:12" makes them do arithmetic first. Without it every
    item reads as equally urgent -- a blocker from six days ago and one from
    this minute look identical -- which is the same failure as an attention
    list full of healthy workers, wearing different clothes.

    It goes in the DISPLAY string only, never in the identity `--changes`
    compares on. An age ticks on every poll, so keying on it would make every
    unchanged line look new forever.
    """
    if seconds is None and stamp:
        with contextlib.suppress(ValueError):
            moment = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            seconds = _dt.datetime.now(_dt.timezone.utc).timestamp() - moment.timestamp()
    if seconds is None or seconds < 0:
        return "   ?"
    if seconds < 90:
        return f"{int(seconds):>3}s"
    if seconds < 5400:
        return f"{int(seconds // 60):>3}m"
    if seconds < 172800:
        return f"{int(seconds // 3600):>3}h"
    return f"{int(seconds // 86400):>3}d"

def _glyph_for(coordinator: Coordinator, project_id: str) -> str:
    """A project's colour glyph, so any report says which project it is about.

    A line about one project among several is ambiguous without it, and colour
    is the fastest thing a human reads.
    """
    for project in coordinator.list_projects():
        if project["id"] == project_id:
            return project_glyph(project.get("color", ""))
    return ""


def _print_project_status(status: dict[str, Any]) -> None:
    p = status["project"]
    print(f"{p['glyph']} {p['name']} ({p['id']})")
    counts = " ".join(f"{k}={v}" for k, v in status["counts"].items())
    print(f"  tasks: {counts or 'none'}")
    if status.get("pending_requests"):
        # Above the action items: an unanswered routed request is the one thing
        # here that a foreman is supposed to be acting on right now.
        print("  routed requests awaiting the foreman:")
        for entry in status["pending_requests"]:
            first = entry["text"].strip().splitlines()[0][:120]
            print(f"    {entry['at'][:10]} {first} (task={entry['task_id']})")
    if status["action_items"]:
        print("  action items:")
        for entry in status["action_items"]:
            task = f" task={entry['task_id']}" if entry.get("task_id") else ""
            gate = " [decision]" if entry.get("kind") in GATE_ACTION_KINDS else ""
            print(
                f"    {entry['at'][:10]}{gate} {entry['text']} "
                f"({entry['source']}{task})"
            )
    if status["situation"]:
        print("  situation:")
        for entry in status["situation"]:
            print(f"    {entry['at'][:10]} {entry['text']}")
    if status["unmerged"]:
        print("  unmerged:")
        for entry in status["unmerged"]:
            print(f"    [{entry['status']}] {entry['task_id']} {entry['brief']}")
    if status["needs_attention"]:
        print("  needs attention:")
        for entry in status["needs_attention"]:
            print(f"    {entry['worker_id']} [{entry['verdict']}] {entry['detail']}")
    if status["evidence"]:
        print("  unresolved failures (evidence kept):")
        for entry in status["evidence"]:
            signature = entry["signatures"][-1] if entry["signatures"] else "no signature"
            print(f"    {entry['task_id']} [{entry['task_status']}] {signature[:90]}")
    if status["grants"]:
        print(f"  standing approvals: {len(status['grants'])}")
    if status["history_entries"]:
        print(f"  ({status['history_entries']} older situation entries rolled to history)")


def _print_approval_grants(coordinator: Coordinator, *, include_revoked: bool = False) -> None:
    grants = coordinator.list_approval_grants(include_revoked=include_revoked)
    if not grants:
        print("No standing approvals; every protected action stops for a human.")
        return
    for grant in grants:
        state = "revoked" if grant.get("revoked_at") else "live"
        scope = grant["project_id"] or "all projects"
        print(
            f"{grant['id']} [{state}] {grant['action']} for {scope} "
            f"granted_by={grant['granted_by']}: {grant['note']}"
        )


#: Update lines that restate a decision this view already prints in full.
#: Echoing them above the real thing is not extra safety -- it pushes the
#: one genuinely new line (a foreman's report) off the top of the screen
#: behind twelve copies of a decision that is listed below anyway.
_DECISION_UPDATE_PREFIXES = ("DECISION REQUIRED:", "ACTION REQUIRED:")


def _print_new_project_updates(
    coordinator: Coordinator,
    project_id: str | None,
    *,
    heading: str,
    skip_decisions: bool = False,
) -> None:
    """Surface foreman/worker pushes the root has not been shown yet.

    A push lands in the durable record and stops there: nothing carries it
    into the root's own session, so it reached the commander only if the
    coordinator happened to re-read the project. That made relaying a
    finished investigation depend on the coordinator remembering to look,
    which is the same failure mode as every other rule that lives only in
    prose -- and it fails silently, because an unread report and a project
    with nothing to say are indistinguishable from the root.

    So every root command that touches a project says what arrived since it
    last looked. Marking them seen here is deliberate and matches `watch`:
    they have now been shown to the root, and repeating them forever would
    train the reader to skip exactly the lines that matter.
    """
    with contextlib.suppress(HelmError, OSError):
        # Marking happens for everything fetched, including what is filtered
        # out below: a decision suppressed here is still being shown to the
        # root, in full, by the caller's own action-item list.
        updates = coordinator.project_updates_for_watch(project_id)
        lines = []
        for update in updates:
            glyph = f"{update['glyph']} " if update.get("glyph") else ""
            first = next(
                (
                    line.strip()
                    for line in str(update.get("text", "")).splitlines()
                    if line.strip()
                ),
                "",
            )
            if skip_decisions and first.startswith(_DECISION_UPDATE_PREFIXES):
                continue
            lines.append(f"  {glyph}{update['project_id']}: {first[:220]}")
        if not lines:
            return
        print(heading)
        for line in lines:
            print(line)
        print()


def _print_status(coordinator: Coordinator, project_id: str | None) -> None:
    report = coordinator.status(project_id)
    # Before anything Helm is doing: what a foreman or worker said that the
    # root has not seen. A completed investigation nobody relayed is worse
    # than a noisy line, because it looks like silence.
    _print_new_project_updates(
        coordinator,
        project_id,
        heading="New since you last looked:",
        skip_decisions=True,
    )
    # First, because it is the only part nobody else can act on. Everything
    # below is what Helm is doing; this is what it is waiting on a human for.
    escalations = coordinator.open_escalations(project_id)
    if escalations:
        print(f"Needs you ({len(escalations)}):")
        for item in escalations:
            glyph = _glyph_for(coordinator, item["project_id"]) if item["project_id"] else " "
            first = next(
                (line.strip() for line in item["text"].splitlines() if line.strip()), ""
            )
            # A diagnostic entry could not be checked against a live record --
            # its worker or task is missing -- so the line says that plainly
            # rather than presenting it as an ordinary, verified escalation.
            mark = " [UNVERIFIED: worker or task record missing]" if item.get("diagnostic") else ""
            print(
                f"  {glyph} {item['kind']:15} {item['role']:8} {item['worker_id']}  "
                f"{first[:88]}{mark}"
            )
        print()
    # A worker result nobody merged is not an escalation -- no agent is stuck
    # on it -- so it never reached this view, and a fresh coordinator saw a
    # quiet project instead of a change waiting on a decision.
    pending = coordinator.open_action_items(project_id)
    # A confirmation gate is not a follow-up: a foreman is stopped behind it and
    # can launch nothing until it is answered. Printed among the delivery and
    # cleanup decisions it sorted level with branches nobody has swept in a
    # week, and a list that long is one the reader learns to skip -- which is
    # how a gate blocking live work sat unanswered while the project looked
    # merely quiet. It goes beside the escalations, which is the section that
    # means "nothing moves until you answer".
    blocking = [i for i in pending if i.get("kind") in BLOCKING_GATE_KINDS]
    pending = [i for i in pending if i.get("kind") not in BLOCKING_GATE_KINDS]
    if blocking:
        print(f"Waiting on your decision ({len(blocking)}):")
        for item in blocking:
            task = f" task={item['task_id']}" if item.get("task_id") else ""
            print(
                f"  {item['glyph']} {item['project_id']} GATE{task}: "
                f"{item['text'][:110]}"
            )
        print()
    if pending:
        # Delivery gates first: a follow-up is somebody's note about later, and
        # a gate is work sitting still until it is answered.
        ordered = sorted(
            pending, key=lambda i: i.get("kind") not in GATE_ACTION_KINDS
        )
        print(f"Decisions and follow-ups ({len(pending)}):")
        for item in ordered:
            task = f" task={item['task_id']}" if item.get("task_id") else ""
            label = "decision" if item.get("kind") in GATE_ACTION_KINDS else "action"
            print(
                f"  {item['glyph']} {item['project_id']} {label}{task}: "
                f"{item['text'][:110]}"
            )
        print()
    if report["projects"]:
        print("Projects:")
        _print_projects(report["projects"])
    else:
        print("Projects: none")
    print("Tasks:")
    if not report["tasks"]:
        print("  none")
    else:
        projects = {project["id"]: project for project in report["projects"]}
        # When filtered data is not requested, status() contains all projects.
        if not projects:
            projects = {project["id"]: project for project in coordinator.list_projects()}
        for task in report["tasks"]:
            project = projects.get(task["project_id"], {"name": "?", "id": task["project_id"], "color": "?"})
            print(
                f"  [{task['status']}] {_project_label(project)} task={task['id']} "
                f"brief={task['brief']} policy={task['delivery_policy']}"
            )
            if task.get("workspace"):
                print(f"    workspace={task['workspace']}")
            if task.get("domain"):
                print(f"    domain={task['domain']} ({task.get('domain_selection', 'selected')})")
            if task.get("agent_id"):
                print(f"    agent={task['agent_id']} reason={task.get('agent_reason', '')}")
    # The whole point of delegation is that nobody watches the panes, so the
    # default view has to say when a worker has gone quiet.
    needs_attention = [
        entry
        for entry in coordinator.worker_health(liveness=_liveness_probe(coordinator))
        # Same healthy set as `watch`, `driving` included for the same reason:
        # a foreman waiting on a worker it launched is working, and listing it
        # as needing attention is how a real stall stops standing out.
        if entry["verdict"] not in {
            "healthy", "settled", "reported", "starting", "driving", "working",
        }
        and (project_id is None or entry["project_id"] == project_id)
    ]
    if needs_attention:
        print("Needs attention:")
        for entry in needs_attention:
            print(
                f"  {entry['worker_id']} [{entry['verdict']}] task={entry['task_id']} "
                f"agent={entry['agent_id']}: {entry['detail']}"
            )
        print("  run: helm watch --nudge")
    # A standing approval acts without anyone being asked, so it is shown
    # every time rather than living only in the command that created it.
    live_grants = coordinator.list_approval_grants()
    if live_grants:
        print("Standing approvals:")
        for grant in live_grants:
            scope = grant["project_id"] or "all projects"
            print(f"  {grant['id']} {grant['action']} for {scope}: {grant['note']}")
    print("Recent messages:")
    if not report["messages"]:
        print("  none")
    else:
        projects = {project["id"]: project for project in coordinator.list_projects()}
        for message in report["messages"]:
            project = projects.get(
                message["project_id"],
                {"name": "?", "id": message["project_id"], "color": "?"},
            )
            status = f" status={message['status']}" if message.get("status") else ""
            line = (
                f"  [{_project_label(project)}] task={message.get('task_id') or '-'} "
                f"{message['kind']}{status}: {message['text']}"
            )
            print(_project_paint(project, line))


def _release_finished_space(coordinator: Coordinator, task: dict[str, Any]) -> None:
    """Close a project's space if this transition left it with nothing pending."""
    # A foreman never stopped on its own, and a running worker blocks the
    # release below -- so the space could never be freed while the project had
    # a driver, which is every project by default. Let it finish first.
    with contextlib.suppress(HelmError, SafetyError, OSError):
        stood_down = coordinator.stand_down_idle_foreman(task["project_id"])
        if stood_down:
            print(
                f"{_glyph_for(coordinator, task['project_id'])} {task['project_id']} "
                f"foreman {stood_down['id']} stood down; nothing left to drive"
            )
    # A task transition such as merge/deliver is when Helm knows the result has
    # been acted on. Release the settled tabs before deciding whether the whole
    # space is idle; otherwise those old panes make a finished project look
    # active and hide the result from the next coordinator.
    with contextlib.suppress(HelmError, OSError):
        released_tabs = HerdrAdapter(coordinator).release_finished_tabs()
        if released_tabs:
            print(f"Closed {len(released_tabs)} finished worker tab(s)")
    released = False
    with contextlib.suppress(HelmError, OSError):
        released = HerdrAdapter(coordinator).close_project_space_if_finished(task["project_id"])
    if released:
        print(f"Closed the Herdr space for project {task['project_id']}")


def _print_learning(proposal: dict[str, Any]) -> None:
    print(
        f"{proposal['id']} [{proposal['status']}] domain={proposal['domain_id']} "
        f"confidence={proposal['confidence']}: {proposal['proposed_fact']}"
    )
    if proposal.get("conflicts"):
        print(f"  conflicts: {_json(proposal['conflicts'])}")
    print(f"  source task: {proposal['source_task_id']}")
    print(f"  rationale: {proposal['rationale']}")


def _print_inspect(report: dict[str, Any]) -> None:
    task = report["task"]
    project = report["project"]
    print(
        _project_paint(
            project, f"Task {task['id']} [{task['status']}] project={_project_label(project)}"
        )
    )
    print(f"  brief: {task['brief']}")
    print(f"  policy: {task['delivery_policy']}")
    print(f"  branch: {task['branch']}")
    print(f"  workspace: {task['workspace']}")
    if task.get("domain"):
        print(f"  domain: {task['domain']} ({task.get('domain_selection', 'selected')})")
    if task.get("agent_id"):
        print(f"  agent: {task['agent_id']} ({task.get('agent_reason', '')})")
    if task.get("approval"):
        print(f"  approval: {_json(task['approval'])}")
    if task.get("read_only"):
        print("  read-only: exempt from the requirement/solution gates")
    gates = task.get("gates") or {}
    if any(gates.values()):
        print("  gates:")
        for gate_type in ("requirement", "solution"):
            gate = gates.get(gate_type)
            if gate is None:
                print(f"    {gate_type}: not proposed")
            elif gate.get("skipped"):
                print(f"    {gate_type}: skipped -- {gate['text']}")
            elif gate.get("confirmed_at"):
                print(f"    {gate_type}: confirmed at {gate['confirmed_at']} -- {gate['text']}")
            else:
                print(f"    {gate_type}: proposed, waiting on the commander -- {gate['text']}")
        if gates.get("bound_task_id"):
            print(
                f"    confirmed pair already authorized task {gates['bound_task_id']}; "
                "propose and reconfirm a gate to authorize another"
            )
    if report["workers"]:
        print("Workers:")
        for worker in report["workers"]:
            print(
                f"  {worker['id']} [{worker['status']}] pid={worker.get('pid')} "
                f"agent={worker.get('agent_id', 'default')} reason={worker.get('agent_reason', '')} "
                f"command={worker['command']}"
            )
    print("Messages:")
    if not report["messages"]:
        print("  none")
    for message in report["messages"]:
        # Same tint as the task header, so a message read out of context is
        # still attributable to its project at a glance.
        print(_project_paint(project, f"  {message['kind']}: {message['text']}"))
    if report["artifacts"]:
        print("Artifacts:")
        for artifact in report["artifacts"]:
            print(f"  {artifact['path']} ({artifact['kind']})")


def _root_argument(args: argparse.Namespace) -> Path | None:
    if args.command == "init":
        configured = os.environ.get("HELM_ROOT")
        requested = args.helm_root or args.init_root_option
        if requested is None and args.init_root != ".":
            requested = args.init_root
        return Path(requested or configured or ".").expanduser().resolve()
    if args.helm_root or getattr(args, "run_root_option", None):
        return Path(args.helm_root or args.run_root_option).expanduser().resolve()
    configured = os.environ.get("HELM_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    state_dir = args.state_dir or os.environ.get("HELM_STATE_DIR")
    if state_dir and Path(state_dir).expanduser().name == "state":
        return Path(state_dir).expanduser().resolve().parent
    current = Path.cwd().resolve()
    if args.command == "run" or (current / "projects").is_dir() or (current / "state").is_dir():
        return current
    return None


def _store_for_args(args: argparse.Namespace) -> tuple[StateStore, Path | None]:
    root = _root_argument(args)
    if args.command == "init" and root is not None:
        return StateStore(root / "state", helm_root=root), root
    # Opening a store normally repairs the permissions of the directory and
    # files it finds. That is right for a command about to write and wrong for
    # `doctor`, whose entire contract is that it changes nothing -- and the
    # store is opened here, before dispatch, so the repair would already have
    # happened by the time doctor looked. A read-only store performs no repair
    # and refuses every write.
    read_only = args.command == "doctor"
    state_dir = args.state_dir or os.environ.get("HELM_STATE_DIR")
    if state_dir:
        store = StateStore(state_dir, helm_root=root, read_only=read_only)
    elif root is not None:
        store = StateStore(root / "state", helm_root=root, read_only=read_only)
    else:
        store = StateStore(read_only=read_only)
    return store, root or store.configured_root()


def _discover_if_configured(coordinator: Coordinator, helm_root: Path | None) -> None:
    if helm_root is not None and (helm_root / "projects").is_dir():
        coordinator.discover_projects(helm_root)


def _task_brief(brief: str | None) -> str:
    if brief is not None:
        return brief
    prompt = "Task: "
    try:
        if sys.stdin.isatty():
            return input(prompt).strip()
        print(prompt, end="", flush=True)
        return sys.stdin.readline().strip()
    except (EOFError, OSError):
        return ""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helm",
        description="Thin local-first coordinator for isolated project workers",
    )
    parser.add_argument("--state-dir", help="local JSON state directory (or use HELM_STATE_DIR)")
    parser.add_argument(
        "--root",
        dest="helm_root",
        help="Helm root containing projects/ and state/ (or use HELM_ROOT)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create a Helm root layout")
    init.add_argument("init_root", nargs="?", default=".", help="Helm root (default: current directory)")
    init.add_argument("--root", dest="init_root_option", help="Helm root (alternative to ROOT)")

    run = commands.add_parser("run", help="discover a project and start one conversational task")
    run.add_argument("project_id")
    run.add_argument("--root", dest="run_root_option", help="Helm root (alternative to global --root)")
    run.add_argument("brief", nargs="?", help="task brief; omit it to be prompted")
    run.add_argument("--command", dest="worker_command_text", help="worker command, parsed without a shell")
    run.add_argument("--delivery", choices=("local", "pr"))
    run.add_argument("--domain", help="explicit domain override for ambiguous tasks")
    run.add_argument(
        "--no-domain", action="store_true", help="state that no domain applies to this task"
    )
    run.add_argument("--agent",
        help="agent runtime or configured profile for this task (built in: claude, codex, pi, opencode)")
    run.add_argument("--model",
        help="model this task runs on; overrides the project pin and HELM_MODEL")
    run.add_argument("--effort", choices=EFFORT_LEVELS,
        help="reasoning effort for this task; overrides the project pin and "
        "HELM_EFFORT")
    run.add_argument("--ticket",
        help="tracker id for this work; goes in the branch name so a human can find it")
    run.add_argument(
        "--herdr",
        dest="herdr",
        action="store_true",
        default=True,
        help="spawn the delegated worker in a Helm-owned Herdr space when available (default)",
    )
    run.add_argument(
        "--no-herdr",
        dest="herdr",
        action="store_false",
        help="skip Herdr presentation; the worker is still delegated to the core process launcher",
    )
    run.add_argument(
        "--async",
        dest="asynchronous",
        action="store_true",
        default=True,
        help="return while the worker runs (default; keeps the session free for the next task)",
    )
    run.add_argument(
        "--wait",
        dest="asynchronous",
        action="store_false",
        help="block until the worker reaches a terminal state",
    )

    project = commands.add_parser("project", help="register and list projects")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    add = project_commands.add_parser("add", help="register a Git project")
    add.add_argument("name")
    add.add_argument("root")
    add.add_argument("--id", dest="project_id")
    add.add_argument("--delivery", choices=("local", "pr"), default="local")
    add.add_argument("--init-git", action="store_true", help="initialize a non-Git root")
    add.add_argument("--confirm", action="store_true", help="confirm explicit non-Git initialization")
    project_commands.add_parser("list", help="list registered projects")

    task = commands.add_parser("task", help="create, inspect, and deliver tasks")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    create = task_commands.add_parser("create")
    create.add_argument("--project", required=True, dest="project_id")
    create.add_argument("--brief", required=True)
    create.add_argument("--delivery", choices=("local", "pr"))
    create.add_argument("--domain", help="explicit domain override for ambiguous tasks")
    create.add_argument("--no-domain", action="store_true")
    create.add_argument("--agent",
        help="agent runtime or configured profile for this task (built in: claude, codex, pi, opencode)")
    create.add_argument("--model",
        help="model this task runs on; overrides the project pin and HELM_MODEL")
    create.add_argument("--effort", choices=EFFORT_LEVELS,
        help="reasoning effort for this task; overrides the project pin and "
        "HELM_EFFORT. A runtime that cannot express it refuses rather than "
        "dropping it")
    create.add_argument("--ticket",
        help="tracker id for this work; goes in the branch name so a human can find it")
    create.add_argument(
        "--read-only", action="store_true",
        help="pure investigation/status work that changes nothing; exempt from the "
        "requirement/solution confirmation gates. Never pass this for work that edits anything",
    )
    for name in ("allocate", "inspect", "approve", "merge"):
        task_commands.add_parser(name).add_argument("task_id")
    evidence_cmd = task_commands.add_parser(
        "evidence",
        help="record the full test-suite result for a task's current tip, as "
        "the structured evidence a reviewer reads",
    )
    evidence_cmd.add_argument("task_id")
    evidence_cmd.add_argument(
        "--tip", required=True, help="the commit the suite ran against"
    )
    evidence_cmd.add_argument(
        # Not dest="command": that is the top-level subparser's own attribute,
        # and taking it leaves the parse undispatchable.
        "--command", dest="suite_command", required=True,
        help="the command that ran, verbatim",
    )
    evidence_cmd.add_argument(
        "--exit", dest="exit_code", type=int, required=True,
        help="its exact, unmasked exit status",
    )
    evidence_cmd.add_argument(
        "--detail", help="JSON object of per-package or per-runner counts"
    )
    continue_cmd = task_commands.add_parser(
        "continue",
        help="run another round on a finished task, reusing its worktree and branch",
    )
    continue_cmd.add_argument("task_id")
    continue_cmd.add_argument(
        "--brief", required=True, help="what this round is for"
    )
    continue_round_kind = continue_cmd.add_mutually_exclusive_group(required=True)
    continue_round_kind.add_argument(
        "--read-only", dest="read_only", action="store_true",
        help="this round is pure investigation/status work that changes nothing; "
        "the worktree is locked so it cannot write to it",
    )
    continue_round_kind.add_argument(
        "--state-changing", dest="read_only", action="store_false",
        help="this round may edit and commit; gated by the requirement/solution "
        "decisions the same as a fresh worker task",
    )
    cleanup_cmd = task_commands.add_parser(
        "cleanup", help="remove a settled task's worktree and its own branch"
    )
    cleanup_cmd.add_argument("task_id")
    cleanup_cmd.add_argument(
        "--delete-branch",
        action="store_true",
        help="also delete the task branch when it still holds unmerged commits",
    )
    pr_cmd = task_commands.add_parser(
        "pr", help="push the task branch so the change can be reviewed on the remote"
    )
    pr_cmd.add_argument("task_id")
    pr_cmd.add_argument("--remote", default="origin")
    pr_cmd.add_argument("--confirm", action="store_true", help="authorize this push explicitly")
    pr_cmd.add_argument("--grant", dest="grant_id", help="push under a standing approval")
    pr_cmd.add_argument("--no-open", action="store_true", help="push only; do not create a PR")
    pr_status = task_commands.add_parser(
        "pr-status", help="record the observed state of a task's pull request"
    )
    pr_status.add_argument("task_id")
    pr_status.add_argument("--state", required=True, choices=("open", "merged", "closed"))
    pr_status.add_argument("--url", default="")
    pr_status.add_argument("--comments", type=int)
    pr_status.add_argument("--checks", default="")
    pr_status.add_argument("--review-decision", default="")
    pr_status.add_argument("--merge-commit", default="")
    pr_sync = task_commands.add_parser(
        "pr-sync", help="read the recorded PR with gh and update Helm's PR status"
    )
    pr_sync.add_argument("task_id")
    outcome_cmd = task_commands.add_parser(
        "outcome", help="show a task's work without merging it: diff, commits, artifacts"
    )
    outcome_cmd.add_argument("task_id")
    deliver = task_commands.add_parser(
        "deliver", help="copy a task's build outputs from its worktree into the project"
    )
    deliver.add_argument("task_id")
    deliver.add_argument(
        "--force", action="store_true", help="replace an existing differing file"
    )
    approve = task_commands.choices["approve"]
    approve.add_argument("--note", default="")
    approve.add_argument(
        "--grant",
        dest="grant_id",
        help="approve under a standing approval instead of asking (see helm approval check)",
    )

    worker = commands.add_parser("worker", help="launch and receive worker messages")
    worker_commands = worker.add_subparsers(dest="worker_command", required=True)
    launch = worker_commands.add_parser("launch")
    launch.add_argument("task_id")
    launch.add_argument("--command", dest="worker_command_text", help="worker command, parsed without a shell")
    launch.add_argument("--domain", help="explicit domain override before launch")
    launch.add_argument("--agent",
        help="agent runtime or configured profile for this task (built in: claude, codex, pi, opencode)")
    launch.add_argument("--async", dest="asynchronous", action="store_true", help="return while worker runs")
    launch.add_argument(
        "--no-herdr", dest="herdr", action="store_false", default=True,
        help="start it as a bare process instead of a tab in the project's space",
    )
    round_cmd = worker_commands.add_parser(
        "round",
        help="send the next round to a task: into its live author session when "
        "one exists, or by relaunching when it does not",
    )
    round_cmd.add_argument("task_id")
    round_cmd.add_argument("--brief", required=True, help="what this round is for")
    round_kind = round_cmd.add_mutually_exclusive_group(required=True)
    round_kind.add_argument(
        "--read-only", dest="read_only", action="store_true",
        help="this round changes nothing; the worktree is locked",
    )
    round_kind.add_argument(
        "--state-changing", dest="read_only", action="store_false",
        help="this round may edit and commit",
    )
    round_cmd.add_argument(
        "--fresh", action="store_true",
        help="force a new worker even if the author session is still live "
        "(use on a scope change, where accumulated context misleads)",
    )
    for name in ("poll", "wait"):
        worker_commands.add_parser(name).add_argument("worker_id")
    report = worker_commands.add_parser("message", aliases=["report"])
    report.add_argument("worker_id")
    report.add_argument("--type", required=True, choices=(
        "status", "result", "blocker", "failure", "approval-needed", "artifact", "question",
    ))
    report.add_argument("--text", default="")
    report.add_argument(
        "--action",
        # `merge` is deliberately absent: no worker performs Helm's merge, so it
        # is not something a worker can ask to be authorized for. It finishes,
        # reports, and the branch is reviewed with helm task approve.
        choices=sorted(PROTECTED_ACTIONS - {"merge"}),
        help="with --type approval-needed: the exact protected action being asked for",
    )
    report.add_argument("--status", choices=("running", "completed", "blocked", "failed", "approval-needed"))
    report.add_argument("--path")
    report.add_argument("--payload", help="JSON object payload")

    stop = worker_commands.add_parser(
        "stop", help="stop a running worker and settle its task as abandoned"
    )
    stop.add_argument("worker_id")
    stop.add_argument(
        "--reason", default="", help="why it was stopped; recorded on the task"
    )

    action_start = worker_commands.add_parser(
        "action-start",
        help="a worker's own pre-action gate: validate and spend one authorization",
    )
    action_start.add_argument("worker_id")

    answer = worker_commands.add_parser(
        "answer", help="answer a worker's question from the task goal and let it continue"
    )
    answer.add_argument("worker_id")
    answer.add_argument("--text", required=True, help="the answer, in the worker's own terms")

    agent = commands.add_parser("agent", help="list and check configured worker profiles")
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)
    agent_commands.add_parser("list", help="list configured profiles without launching anything")
    agent_commands.add_parser("check", help="check profile commands and live availability checks")
    agent_models = agent_commands.add_parser(
        "models",
        help="query live model catalogues and classify explicitly-free models (read-only)",
        description=(
            "Query the catalogue commands the launchable built-in runtimes "
            "publish (pi --list-models, opencode models) with a bounded "
            "timeout, and report exact model ids. Free is classified only "
            "from an explicit marker in the id itself; everything else is "
            "unknown. Reads no credential store and writes nothing."
        ),
    )
    agent_models.add_argument(
        "--json", dest="agent_models_json", action="store_true",
        help="emit the machine-readable report",
    )

    # Read-only, so it is deliberately open to every caller: a worker that can
    # tell a broken root from a broken brief escalates the right one.
    doctor_cmd = commands.add_parser(
        "doctor",
        help="read-only preflight of this Helm root, and optionally one project",
        description=(
            "Inspect a Helm root without changing it. Exits 0 when nothing is "
            "an error (warnings do not change that), 1 when an error was found, "
            "and 2 when doctor could not run. Reports executable presence for "
            "named runtimes; it never runs a provider auth, status, or model "
            "command, and never reads a credential store. See docs/doctor.md."
        ),
    )
    doctor_cmd.add_argument(
        "--project", dest="doctor_project", help="also run checks for one managed project"
    )
    doctor_cmd.add_argument(
        "--probe-runtimes", action="store_true",
        help="also run each installed agent's --help to catch an effort flag "
        "Helm records but the CLI no longer publishes (starts processes, so "
        "it is opt-in)"
    )
    doctor_cmd.add_argument(
        "--json", dest="doctor_json", action="store_true", help="emit the machine-readable report"
    )

    prefs_cmd = commands.add_parser(
        "prefs",
        help="inspect and update this root's local operator preferences",
        description=(
            "Root-local, Git-ignored, non-secret operator preferences. Only the "
            "keys listed by `helm prefs keys` are accepted; anything else is "
            "refused rather than stored, so this never becomes a credential file."
        ),
    )
    prefs_commands = prefs_cmd.add_subparsers(dest="prefs_command", required=True)
    prefs_commands.add_parser("path", help="print where this root's preferences file lives")
    prefs_commands.add_parser("show", help="show the effective preferences and their sources")
    prefs_commands.add_parser("keys", help="list the supported preference keys")
    prefs_set = prefs_commands.add_parser("set", help="set one preference, atomically")
    prefs_set.add_argument("key", help="a key from `helm prefs keys`")
    prefs_set.add_argument("value", nargs="+", help="one value, or several for a list key")
    prefs_unset = prefs_commands.add_parser("unset", help="remove one preference")
    prefs_unset.add_argument("key")
    prefs_commands.add_parser(
        "migrate", help="copy legacy state.config exclusions into the preferences file"
    )

    herdr = commands.add_parser("herdr", help="present tasks in Herdr when available")
    herdr_commands = herdr.add_subparsers(dest="herdr_command", required=True)
    herdr_launch = herdr_commands.add_parser("launch", help="launch a task in a project Herdr workspace")
    herdr_launch.add_argument("task_id")
    herdr_launch.add_argument("--command", dest="worker_command_text", help="worker command, parsed without a shell")
    herdr_launch.add_argument("--domain", help="explicit domain override before launch")
    herdr_launch.add_argument("--agent",
        help="agent runtime or configured profile for this task (built in: claude, codex, pi, opencode)")
    herdr_launch.add_argument(
        "--async", dest="asynchronous", action="store_true", default=True
    )
    herdr_launch.add_argument("--wait", dest="asynchronous", action="store_false")
    for name in ("poll", "cleanup"):
        herdr_commands.add_parser(name).add_argument("task_id")
    herdr_wait = herdr_commands.add_parser("wait")
    herdr_wait.add_argument("task_id")
    # Stated here rather than left to the adapter's default, because `None`
    # there means "wait until terminal".  This command is the bounded probe.
    herdr_wait.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_WAIT_TIMEOUT,
        help="bounded wait in seconds (default: 5); expiry reports the worker still running",
    )
    cleanup_project = herdr_commands.add_parser("cleanup-project", help="close Helm-owned tabs and a project workspace")
    cleanup_project.add_argument("project_id")
    herdr_commands.add_parser("cleanup-coordinator", help="close Helm's coordinator workspace")
    herdr_commands.add_parser(
        "relabel", help="rename Helm's own spaces and tabs to the short panel-readable scheme"
    )

    domain_cmd = commands.add_parser(
        "domain", help="list domains and what each one applies to"
    )
    domain_cmd.add_subparsers(dest="domain_command", required=True).add_parser(
        "list", help="show every domain and the work it applies to"
    )

    skills_cmd = commands.add_parser(
        "skills", help="show a project's task-varying skills and what is wrong with any"
    )
    skills_cmd.add_argument("project_id")
    skills_cmd.add_argument(
        "--agent", dest="agent", default=None,
        help="the runtime the work would run on, which decides the runtime-specific root read",
    )
    skills_cmd.add_argument(
        "--brief", dest="brief", default=None,
        help="try a brief and show which skills it would select, and why",
    )

    status = commands.add_parser("status", help="show active tasks and recent results")
    status.add_argument("--project", dest="project_id")

    watchdog = commands.add_parser(
        "watchdog",
        help=(
            "the backstop when the reporting chain does not fire: check "
            "outside a conversation and notify when something needs a human"
        ),
    )
    watchdog_commands = watchdog.add_subparsers(dest="watchdog_command", required=True)
    watchdog_run = watchdog_commands.add_parser("run", help="check now, then on an interval")
    watchdog_run.add_argument("--interval", type=int, default=WATCHDOG_DEFAULT_INTERVAL)
    watchdog_run.add_argument(
        "--once", action="store_true", help="check once and exit (what a scheduler calls)"
    )
    watchdog_install = watchdog_commands.add_parser(
        "install", help="generate and load this platform's scheduler entry"
    )
    watchdog_install.add_argument(
        "--interval", type=int, default=WATCHDOG_DEFAULT_INTERVAL
    )
    watchdog_commands.add_parser("uninstall", help="remove the scheduler entry")

    pending = commands.add_parser(
        "pending",
        help=(
            "only what is waiting on a human, in a few lines -- silent when "
            "nothing is. Built for an automatic per-turn check"
        ),
    )
    pending.add_argument(
        "--changes",
        action="store_true",
        help=(
            "print only what is NEW since the last --changes call, and nothing "
            "else. For an unattended watch, where re-printing the current state "
            "on every poll is noise that buries the one new line"
        ),
    )

    ack = commands.add_parser(
        "ack",
        help="mark a project's owed reports as relayed to the commander",
    )
    ack.add_argument("project_id", help="project whose reports were relayed")
    ack.add_argument(
        "entry_ids",
        nargs="*",
        help="specific situation entry ids; omit to acknowledge every owed report",
    )

    watch = commands.add_parser(
        "watch", help="check every running worker's health without opening its UI"
    )
    watch.add_argument(
        "--silence",
        type=float,
        default=None,
        help="seconds of silence before a worker is called stalled (default: 300)",
    )
    watch.add_argument(
        "--nudge",
        action="store_true",
        help="ask each silent worker for a status push (once per worker)",
    )

    foreman = commands.add_parser(
        "foreman", help="put a foreman in charge of one project's loops"
    )
    foreman.add_argument("project_id")
    foreman.add_argument("--agent",
        help="agent runtime or configured profile for the foreman (built in: claude, codex, pi, opencode)")
    foreman.add_argument("--model",
        help="model the foreman runs on; overrides the project pin and HELM_MODEL")
    foreman.add_argument("--command", dest="worker_command_text",
        help="foreman command, parsed without a shell")
    foreman.add_argument(
        "--no-herdr", dest="herdr", action="store_false", default=True,
        help="start the foreman as a plain process instead of in the project's space",
    )

    route = commands.add_parser(
        "route",
        help="hand a request to one project's foreman and return immediately",
    )
    route.add_argument("project_id")
    route.add_argument("text", help="the request text for that project's foreman")
    route.add_argument("--agent",
        help="agent runtime or configured profile for a foreman route appoints "
        "(built in: claude, codex, pi, opencode); ignored if the project already has one")
    route.add_argument("--model",
        help="model for a foreman route appoints; overrides the project pin and HELM_MODEL; "
        "ignored if the project already has one")
    route.add_argument("--command", dest="worker_command_text",
        help="foreman command, parsed without a shell; only used if route appoints a foreman")
    route.add_argument(
        "--no-herdr", dest="herdr", action="store_false", default=True,
        help="appoint a missing foreman as a plain process instead of in the project's space",
    )

    board = commands.add_parser(
        "board", help="write a single page showing what every agent produced"
    )
    board.add_argument("--out", help="output path (default: <state>/board.html)")
    board.add_argument("--open", dest="open_it", action="store_true", help="open it after writing")

    reflect = commands.add_parser(
        "reflect", help="assemble recent evidence for a reflection on how Helm is working"
    )
    reflect.add_argument("--hours", type=float, default=24.0)

    tail = commands.add_parser("tail", help="show a worker's decoded terminal output")
    tail.add_argument("worker_id")
    tail.add_argument("-n", "--lines", type=int, default=40)

    approval = commands.add_parser(
        "approval", help="grant, list, and revoke standing approvals a human decided in advance"
    )
    approval_commands = approval.add_subparsers(dest="approval_command", required=True)
    grant = approval_commands.add_parser(
        "grant", help="pre-authorize one protected action so Helm need not ask again"
    )
    grant.add_argument("action", choices=sorted(PROTECTED_ACTIONS))
    grant.add_argument("--project", dest="project_id", help="limit to one project (default: all)")
    grant.add_argument("--note", required=True, help="what this permits and why (required)")
    project_release = project_commands.add_parser(
        "release",
        help="let go of what a finished project still holds, and report what it kept",
    )
    project_release.add_argument("project_id")
    status_cmd = project_commands.add_parser(
        "status", help="everything needed to take a project over mid-stream"
    )
    status_cmd.add_argument("project_id")
    note_cmd = project_commands.add_parser(
        "note", help="record one line of context Helm cannot derive"
    )
    note_cmd.add_argument("project_id")
    note_cmd.add_argument("text")
    note_cmd.add_argument("--supersedes", default="")
    action_cmd = project_commands.add_parser(
        "action", help="record one commander-visible follow-up or decision item"
    )
    action_cmd.add_argument("project_id")
    action_cmd.add_argument("text")
    action_cmd.add_argument("--source", default="helm")
    action_cmd.add_argument("--task", dest="task_id")
    domain_cmd = project_commands.add_parser(
        "domain", help="set the default domain every task on this project resolves to"
    )
    domain_cmd.add_argument("project_id")
    domain_cmd.add_argument(
        "domains", nargs="*", help="domain ids; pass none to clear the default"
    )

    list_grants = approval_commands.add_parser("list", help="list standing approvals")
    list_grants.add_argument(
        "--all", dest="include_revoked", action="store_true", help="include revoked grants"
    )
    revoke_grant = approval_commands.add_parser("revoke", help="withdraw a standing approval")
    revoke_grant.add_argument("grant_id")
    revoke_grant.add_argument("--note", default="")
    check_grant = approval_commands.add_parser(
        "check", help="report whether a standing approval covers an action"
    )
    check_grant.add_argument("action", choices=sorted(PROTECTED_ACTIONS))
    check_grant.add_argument("--project", dest="project_id")
    release_hold = approval_commands.add_parser(
        "release",
        help="authorize the protected action a paused task asked for, and let its worker continue",
    )
    release_hold.add_argument("task_id")
    release_hold.add_argument(
        "--action",
        required=True,
        # Same list a worker may ask for. Merging is reviewed and performed
        # through helm task approve / helm task merge, never authorized here.
        choices=sorted(PROTECTED_ACTIONS - {"merge"}),
        help="the exact action being authorized; it must match what was asked for",
    )
    release_hold.add_argument("--note", default="", help="why this was authorized")
    # Exactly one authority, stated. Allowing both let --confirm silently
    # discard a named grant, and allowing neither auto-selected one.
    authority_choice = release_hold.add_mutually_exclusive_group(required=True)
    authority_choice.add_argument(
        "--confirm", action="store_true", help="the commander is authorizing it now"
    )
    authority_choice.add_argument(
        "--grant", dest="grant_id", help="authorize under one standing approval instead"
    )
    release_hold.add_argument(
        "--text", default="", help="what to say to the worker (default: a plain go-ahead)"
    )
    repair_hold = approval_commands.add_parser(
        "repair",
        help="recover a task stranded on an approval, including one from an older Helm",
    )
    repair_hold.add_argument("task_id")
    repair_hold.add_argument(
        "--note", default="", help="why it is being repaired; recorded on the task"
    )

    gate = commands.add_parser(
        "gate",
        help="propose (foreman) and decide (commander) the requirement/solution gates",
    )
    gate_commands = gate.add_subparsers(dest="gate_command", required=True)
    gate_propose = gate_commands.add_parser(
        "propose", help="foreman: propose the requirement or solution contract for a task"
    )
    gate_propose.add_argument("task_id", help="the foreman task driving the project")
    gate_propose.add_argument("--type", dest="gate_type", required=True,
        choices=("requirement", "solution"))
    gate_propose.add_argument("--text", required=True,
        help="the concise contract: for requirement, goal/scope/exclusions/acceptance "
        "evidence; for solution, approach/boundaries/verification/risks")
    gate_decide = gate_commands.add_parser(
        "decide", help="commander: confirm or skip a proposed gate"
    )
    gate_decide.add_argument("task_id")
    gate_decide.add_argument("--type", dest="gate_type", required=True,
        choices=("requirement", "solution"))
    gate_decide_choice = gate_decide.add_mutually_exclusive_group(required=True)
    gate_decide_choice.add_argument("--confirm", action="store_true")
    gate_decide_choice.add_argument("--skip", action="store_true")
    gate_decide.add_argument("--note", default="", help="why, if useful to record")

    authority = commands.add_parser(
        "authority",
        help="configure the capability this root's protected commands require",
    )
    authority_commands = authority.add_subparsers(dest="authority_command", required=True)
    authority_commands.add_parser(
        "init", help="generate this root's authorization capability (the value is never printed)"
    )
    authority_commands.add_parser(
        "status", help="report whether protected commands require a capability here"
    )
    learning = commands.add_parser(
        "learning", aliases=["learn"], help="propose, review, and apply domain learnings"
    )
    learning_commands = learning.add_subparsers(dest="learning_command", required=True)
    propose = learning_commands.add_parser(
        "propose", help="extract candidate learning proposals from a completed task"
    )
    propose.add_argument("task_id")
    propose.add_argument("--domain")
    propose.add_argument("--fact", help="one concise fact/rule; omit to extract result/artifact candidates")
    propose.add_argument("--rationale")
    propose.add_argument("--confidence", type=float)
    propose.add_argument("--artifact", dest="artifact_ids", action="append")
    propose.add_argument("--message", dest="message_ids", action="append")
    list_learning = learning_commands.add_parser("list", help="list learning proposals")
    list_learning.add_argument("--domain")
    list_learning.add_argument("--status", choices=("proposed", "approved", "rejected", "applied"))
    list_learning.add_argument("--task", dest="task_id")
    inspect_learning = learning_commands.add_parser("inspect", help="inspect a learning proposal")
    inspect_learning.add_argument("proposal_id")
    edit_learning = learning_commands.add_parser("edit", help="edit an unapproved learning proposal")
    edit_learning.add_argument("proposal_id")
    edit_learning.add_argument("--fact")
    edit_learning.add_argument("--rationale")
    edit_learning.add_argument("--confidence", type=float)
    for name in ("approve", "reject"):
        action = learning_commands.add_parser(name)
        action.add_argument("proposal_id")
        action.add_argument("--note", default="")
        action.add_argument("--actor", default="user")
    apply_learning = learning_commands.add_parser(
        "apply", help="append an approved proposal to domain or project knowledge"
    )
    apply_learning.add_argument("proposal_id")
    apply_learning.add_argument("--actor", default="user")
    apply_learning.add_argument(
        "--scope",
        choices=("domain", "project"),
        default="domain",
        help="'project' writes the project's own .helm/knowledge.md instead, "
        "for a fact true of this project and no other",
    )

    review = commands.add_parser(
        "review", help="run an independent reviewer against a task until both agents agree"
    )
    review.add_argument("task_id")
    review.add_argument("--reviewer-agent", help="force a reviewer runtime")
    review.add_argument("--reviewer-model", help="review on a different model of the same runtime")
    review.add_argument("--rounds", type=int, default=2, help="bounded disagreement rounds (default 2)")
    review.add_argument("--timeout", type=float, default=1800.0)

    inspect = commands.add_parser("inspect", help="alias for task inspect")
    inspect.add_argument("task_id")
    return parser


def _run_worker_on_pty(command: list[str], cwd: str, env: dict[str, str], log: Any) -> int:
    """Run a worker on a pseudo-terminal, mirroring it to the pane and the log.

    An interactive agent only renders when it owns a TTY, and a pipe is not one.
    The runner therefore allocates a pty for the worker, proxies the pane's
    keystrokes into it, and copies everything it produces to both the pane and
    Helm's bounded log.  Capture and visibility stop being mutually exclusive.
    """
    import fcntl
    import pty
    import select
    import struct
    import termios
    import tty

    master, slave = pty.openpty()
    with contextlib.suppress(OSError):
        size = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(master, termios.TIOCSWINSZ, size)
    process = subprocess.Popen(
        command, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave, close_fds=True
    )
    os.close(slave)

    stdin_fd = sys.stdin.fileno()
    saved: Any = None
    with contextlib.suppress(OSError, termios.error):
        saved = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)
    watch_stdin = True
    try:
        while True:
            sources = [master, stdin_fd] if watch_stdin else [master]
            try:
                readable, _, _ = select.select(sources, [], [], 0.2)
            except (OSError, ValueError):
                break
            if master in readable:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                log.write(text)
                log.flush()
                sys.stdout.write(text)
                sys.stdout.flush()
            if watch_stdin and stdin_fd in readable:
                # Forward the user's keystrokes so the session stays usable.
                keys = b""
                try:
                    keys = os.read(stdin_fd, 4096)
                except OSError:
                    keys = b""
                if keys:
                    with contextlib.suppress(OSError):
                        os.write(master, keys)
                else:
                    # Closed stdin stays readable forever; polling it would spin
                    # and could keep the runner alive after the worker exits.
                    watch_stdin = False
            if process.poll() is not None and master not in readable:
                break
    finally:
        if saved is not None:
            with contextlib.suppress(OSError, termios.error):
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        with contextlib.suppress(OSError):
            os.close(master)
    return process.wait()


def _worker_runner(config_path: str) -> int:
    """Run a worker and write an exit record; not part of the public API."""
    try:
        _private_file(Path(config_path))
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        command = config["command"]
        cwd = config["cwd"]
        log_path = Path(config["log"])
        exit_path = Path(config["exit"])
        worker_env = dict(config["worker_env"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return _runner_failure(config_path, f"invalid worker runner config: {exc}")

    # Re-check at process startup, not only during allocation. A replaced
    # path or swapped worktree must never become a worker assignment.
    #
    # A foreman drives work and never edits, so core allocates it a Helm-owned
    # state directory rather than a worktree (see Coordinator._verify_workspace_record).
    # Re-checking it for a worktree here did not harden anything -- it just
    # killed every foreman at launch, because `git rev-parse` from that empty
    # directory walks up to the Helm root and reports a toplevel that is not
    # the assigned path. The isolation that still applies to a foreman is
    # where the directory is, so re-check exactly that, and keep it inside
    # Helm's state: the containment check is what stops a swapped path from
    # pointing a foreman at a project checkout or the Helm root itself.
    if config.get("workspace_kind") == "state-directory":
        try:
            state_dir = Path(config["state_dir"]).resolve()
            workspace = Path(cwd).resolve()
            if not workspace.is_dir():
                return _runner_failure(config_path, "foreman workspace is missing")
            if workspace != state_dir and state_dir not in workspace.parents:
                return _runner_failure(
                    config_path, "foreman workspace is not Helm-owned state"
                )
        except (OSError, KeyError) as exc:
            return _runner_failure(config_path, f"foreman workspace verification failed: {exc}")
    else:
        # One retry on a short delay: the worktree is created moments before
        # the runner starts, and a still-settling filesystem fails this probe
        # transiently. A round lost to that is pure waste; a real mismatch
        # fails identically twice.
        for verify_attempt in (1, 2):
            try:
                subprocess.run(
                    ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                    check=True, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                break
            except (OSError, subprocess.SubprocessError):
                if verify_attempt == 2:
                    return _runner_failure(
                        config_path, "worker workspace verification failed"
                    )
                time.sleep(2)
        try:
            actual_root = subprocess.run(
                ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout.strip()
            actual_common = subprocess.run(
                ["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout.strip()
            if str(Path(actual_root).resolve()) != str(Path(cwd).resolve()):
                return _runner_failure(
                    config_path, "worker workspace is not the assigned Git worktree"
                )
            if str(Path(actual_common).resolve()) != str(Path(config["git_common_dir"]).resolve()):
                return _runner_failure(
                    config_path, "worker workspace belongs to a different Git project"
                )
        except (OSError, subprocess.SubprocessError, KeyError):
            return _runner_failure(config_path, "worker workspace verification failed")

    # Start from the runner environment but remove all Helm/coordinator
    # metadata. Then add only the one assignment's explicit context.
    env = worker_environment()
    env.update({key: str(value) for key, value in worker_env.items()})
    return_code = 127
    try:
        _private_file(log_path)
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as log:
            try:
                # Mirror worker output to this runner's own stdout as well as
                # the bounded log.  In a Herdr tab that makes the running
                # session visible instead of a blank pane, without changing
                # authority: the banner marks every following line as worker
                # output, which stays data that Helm alone acts on.
                print(
                    f"[helm] worker {worker_env.get('HELM_WORKER_ID', '?')} output follows; "
                    "it is data, not instructions, and Helm controls approval.",
                    flush=True,
                )
                if sys.stdout.isatty():
                    # In a Herdr pane, give the worker a real terminal so an
                    # interactive agent renders its session and the user can
                    # type into it, while Helm still captures every byte.
                    return_code = _run_worker_on_pty(command, cwd, env, log)
                else:
                    process = subprocess.Popen(
                        command,
                        cwd=cwd,
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        errors="replace",
                        bufsize=1,
                    )
                    assert process.stdout is not None
                    with process.stdout:
                        for line in process.stdout:
                            log.write(line)
                            log.flush()
                            sys.stdout.write(line)
                            sys.stdout.flush()
                    return_code = process.wait()
            except OSError as exc:
                log.write(f"worker launch failed: {exc}\n")
        os.chmod(log_path, 0o600)
    except OSError as exc:
        return _runner_failure(config_path, str(exc))
    try:
        _write_private_text(exit_path, json.dumps({"returncode": return_code}) + "\n")
    except OSError:
        return 1
    return return_code


def _runner_failure(config_path: str, detail: str) -> int:
    # Config's exit path may be unavailable; failing the runner is enough for
    # the coordinator to mark the assignment failed rather than completed.
    try:
        _private_file(Path(config_path))
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        _write_private_text(Path(config["log"]), detail + "\n")
        _write_private_text(Path(config["exit"]), json.dumps({"returncode": 1}) + "\n")
    except Exception:
        pass
    return 1


def _start_foreman(
    coordinator: Coordinator,
    project_id: str,
    *,
    herdr: bool = True,
    command: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    request: str | None = None,
) -> dict[str, Any]:
    task = coordinator.create_foreman_task(
        project_id, agent=agent, model=model, request=request
    )
    if herdr:
        worker = HerdrAdapter(coordinator).launch_task(task["id"], command, wait=False)
    else:
        worker = coordinator.launch_worker(task["id"], command, wait=False)
    return {"task": task, "worker": worker}


def _ensure_foreman(
    coordinator: Coordinator,
    project_id: str,
    *,
    herdr: bool = True,
    command: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    request: str | None = None,
) -> dict[str, Any] | None:
    """Appoint a declared project's foreman if it has none.

    The project states this once in its own file, so the coordinator does not
    have to remember it at each request -- which is the same failure mode as
    every other rule that lived only in prose.

    A failure here never stops the work that triggered it. A project without
    its driver is degraded and says so on stderr; a task lost because
    appointing a driver failed would be a far worse trade.
    """
    try:
        if not coordinator.project_wants_foreman(project_id):
            return None
        if coordinator.foreman_for(project_id) is not None:
            return None
        started = _start_foreman(
            coordinator, project_id, herdr=herdr, command=command, agent=agent,
            model=model, request=request,
        )
    except (HelmError, SafetyError, OSError) as exc:
        print(
            f"helm: {project_id} asks for a foreman and has none; could not start one: {exc}",
            file=sys.stderr,
        )
        return None
    # Say where the decision actually came from. This claimed
    # ".helm/project.json" unconditionally, including for the projects that
    # have no such file -- asserting a declaration the user never wrote, about
    # the one file whose `"foreman": false` is the documented way to decline.
    record = coordinator.store.load()["projects"].get(project_id) or {}
    source = (
        "declared in the project record"
        if isinstance(record.get("foreman"), bool)
        else "declared in .helm/project.json"
        if (Path(record.get("root", "")) / ".helm" / "project.json").exists()
        else "every project gets one by default"
    )
    print(
        f"{_glyph_for(coordinator, project_id)} {project_id} appointed foreman "
        f"{started['worker']['id']} ({source})"
    )
    return started


def _doctor_command(
    coordinator: Coordinator, helm_root: Path | None, args: argparse.Namespace
) -> int:
    """Validate the invocation, then report. Never changes anything."""
    project_id = args.doctor_project
    if project_id is not None:
        if helm_root is None:
            raise HelmError("--project needs a Helm root; run helm doctor --root <path>")
        projects_root = helm_root / "projects"
        candidate = projects_root / _validate_project_id(project_id)
        # An id naming nothing at all is a mistyped invocation, not a finding
        # about the root, so it exits 2 like every other command's unknown
        # project rather than reading as a fault in a root that may be sound.
        #
        # That reasoning only holds for a projects/ directory doctor can trust.
        # When projects/ is itself a symlink, "the child is not there" is a
        # statement about somebody else's directory, and answering it here
        # would exit 2 with no report -- hiding the root.symlinks error that is
        # the actual finding. So the precheck stands down and doctor reports.
        trustworthy = projects_root.is_dir() and not projects_root.is_symlink()
        if trustworthy and not candidate.exists() and not candidate.is_symlink():
            raise HelmError(
                f"unknown project {project_id}; expected a Git project at {candidate}"
            )
    return _emit_doctor(
        args,
        doctor_module.run(
            coordinator,
            helm_root,
            project_id,
            probe_runtimes=getattr(args, "probe_runtimes", False),
        ),
    )


def _emit_doctor(args: argparse.Namespace, report: doctor_module.Report) -> int:
    """Print one doctor report and return its exit status.

    Shared by both callers so a root whose state will not open still gets the
    same document -- an automation branching on `--json` must never have to
    handle "sometimes there is no JSON".
    """
    if args.doctor_json:
        print(doctor_module.render_json(report))
    else:
        for line in doctor_module.render_text(report):
            print(line)
    return report.exit_code


def _doctor_root(args: argparse.Namespace) -> Path | None:
    with contextlib.suppress(HelmError, OSError, ValueError):
        return _root_argument(args)
    return None


def _prefs_command(
    coordinator: Coordinator, helm_root: Path | None, args: argparse.Namespace
) -> int:
    """Locate, inspect and update this root's operator preferences.

    Everything printed here is rebuilt from validated fields, never echoed
    from the file, so a value Helm did not understand can never be read back
    out of it. That is what keeps `helm prefs show` from being a way to print
    whatever somebody parked in the file.
    """
    path = preferences.preferences_path(helm_root)
    try:
        current = preferences.load(path)
    except preferences.PreferencesError as exc:
        raise HelmError(str(exc)) from exc

    if args.prefs_command == "path":
        if path is None:
            raise HelmError(
                "no Helm root is configured, so there is no preferences file. "
                "Run helm init first."
            )
        print(f"{path} ({'present' if current.present else 'not created yet'})")
        return 0

    if args.prefs_command == "keys":
        print("Supported preference keys (non-secret values only):")
        for line in preferences.key_help():
            print(f"  {line}")
        return 0

    if args.prefs_command == "show":
        print(f"file: {path or '(no Helm root configured)'}")
        entries = current.entries()
        if entries:
            for key, value in entries:
                print(f"  {key} = {value}")
        else:
            print("  (nothing set; Helm imposes no operator defaults of its own)")
        # Environment overrides and legacy state are shown beside the file
        # because "why is this runtime excluded" is otherwise a hunt through
        # three places, and the answer decides which one to edit.
        for variable in ("HELM_AGENT", "HELM_MODEL", "HELM_EXCLUDE_AGENTS",
                         "HELM_REVIEW_EXCLUDE_AGENTS"):
            value = os.environ.get(variable)
            if value is not None:
                print(f"  session override {variable}={value}")
        legacy = coordinator.legacy_excluded_agents()
        if legacy:
            print(
                "  legacy state.config exclusions: "
                + ", ".join(sorted(legacy))
                + " (run `helm prefs migrate` to move them into the file)"
            )
        print(f"effective excluded agents: {', '.join(sorted(coordinator.excluded_agents())) or 'none'}")
        return 0

    if args.prefs_command == "migrate":
        legacy = coordinator.legacy_excluded_agents()
        if not legacy:
            print("Nothing to migrate: no legacy state.config exclusions are recorded.")
            return 0
        merged = sorted(set(current.excluded_agents) | legacy)
        try:
            # The legacy entry is deliberately left in place. Removing it would
            # change behaviour for an older Helm still reading this root, and
            # the two sources are unioned anyway, so keeping it is a no-op the
            # operator can undo when they are ready.
            written = preferences.save(
                preferences.apply(current, preferences.KEY_AGENT_EXCLUDE, merged)
            )
        except preferences.PreferencesError as exc:
            raise HelmError(str(exc)) from exc
        print(f"Migrated {', '.join(sorted(legacy))} into {written}")
        print("  state.config is unchanged; the two sources are unioned.")
        return 0

    key = args.key
    values = args.value if args.prefs_command == "set" else None
    try:
        updated = preferences.apply(current, key, values)
        written = preferences.save(updated)
    except preferences.PreferencesError as exc:
        raise HelmError(str(exc)) from exc
    shown = dict(updated.entries()).get(key, "(unset)")
    print(f"{key} = {shown}")
    print(f"  written to {written}")
    return 0


# Commands no agent Helm started may run, whatever its role. Each either
# authorizes a protected action or changes what Helm itself trusts, and those
# belong to the human at the root -- a worker's text is data, and so is a
# worker's command line.
_ROOT_ONLY_COMMANDS = frozenset({
    ("init", None),
    # A preference is the commander's own cost and safety policy for this
    # machine. An agent that could write one could lift the exclusion that
    # stops it starting an expensive runtime, which is the same shape of
    # self-authorization as approving its own merge. Reading them is fine and
    # is left available: `path`, `show` and `keys` are not listed here.
    ("prefs", "set"),
    ("prefs", "unset"),
    ("prefs", "migrate"),
    ("project", "add"),
    ("task", "approve"),
    ("task", "merge"),
    ("task", "pr"),
    ("approval", "grant"),
    ("approval", "revoke"),
    # Releasing a hold *is* the authorization the worker asked for. An agent
    # that could run it would be approving its own protected action. This list
    # is now a fast, readable refusal only: the enforced boundary is in core,
    # because an agent that imports Coordinator never reaches CLI dispatch.
    ("approval", "release"),
    ("approval", "repair"),
    # Deciding a gate is the same shape as releasing an approval hold: it is
    # the commander's confirmation itself, so a foreman proposing it can
    # never also be the one who decides it.
    ("gate", "decide"),
    ("authority", None),
    ("learning", "approve"),
    ("learning", "reject"),
    ("learning", "apply"),
    ("foreman", None),
    # Routing is root Helm's own job: identify the project, ensure its
    # foreman is live, hand off, and return without waiting on that
    # foreman's own work. A worker or foreman routing on its own behalf
    # would be the second driver this file elsewhere refuses.
    ("route", None),
})
# Delegation is one level deep: a foreman spawns workers, a worker spawns
# nothing. Everything that starts or drives another agent is therefore the
# foreman's, and a worker that wants one asks Helm.
_FOREMAN_ONLY_COMMANDS = frozenset({
    ("run", None),
    ("task", "create"),
    # Reopening a task is starting work on it, and it drops any approval the
    # task carried -- both of those are the foreman's, not a worker's.
    ("task", "continue"),
    ("worker", "launch"),
    ("worker", "round"),
    ("worker", "answer"),
    ("herdr", "launch"),
    ("review", None),
    # Proposing a gate is the foreman's own reasoning about the project it
    # drives, same as spawning the worker that will act on it.
    ("gate", "propose"),
})


def _authority_refusal(coordinator: Coordinator, args: argparse.Namespace) -> str | None:
    """Why the calling agent may not run this command, or None.

    The rules were already written down for agents to follow. Writing them
    down is not enforcing them: a context document is guidance an agent may
    misread, and the actions on the other side of this line cannot be undone
    by deleting a branch. So decide it here, from the caller's own recorded
    role, before the command runs.
    """
    role = coordinator.caller_role()
    if role == "root":
        return None
    command = "learning" if args.command in {"learning", "learn"} else args.command
    sub = getattr(args, f"{command}_command", None)
    for key in ((command, sub), (command, None)):
        if key in _ROOT_ONLY_COMMANDS:
            return (
                f"helm {command} {sub or ''}".strip()
                + " is the human's, held at the Helm root. Report what you need and why;"
                " an agent cannot authorize it for itself."
            )
    if role == "foreman":
        return None
    for key in ((command, sub), (command, None)):
        if key in _FOREMAN_ONLY_COMMANDS:
            return (
                f"helm {command} {sub or ''}".strip()
                + " starts or drives another agent, and delegation is one level deep."
                " Push a message to Helm instead of spawning."
            )
    return None



#: What the root's own Herdr tab is called. Project spaces say
#: "Helm Reports · <project>"; the root says this, so anyone opening a Helm
#: root in Herdr sees the same name rather than a bare tab number.
ROOT_TAB_NAME = "Helm"


def _name_root_tab(coordinator: Coordinator) -> None:
    """Name the tab the ROOT's own session occupies, once.

    Helm never retitles a space it did not create, and this does not: it
    names only the tab whose id Herdr exported into this very process, and
    only when the caller is the root. A worker runs `helm` commands inside
    its OWN pane, so without that guard the first report a worker sent would
    rename its ticket-labelled tab and lose the label a human scans for.
    """
    tab_id = os.environ.get("HERDR_TAB_ID")
    if os.environ.get("HERDR_ENV") != "1" or not tab_id:
        return
    if os.environ.get("HELM_WORKER_ID"):
        return
    with contextlib.suppress(Exception):
        if coordinator.caller_role() != "root":
            return
        data = coordinator.store.load()
        named = data.get("integrations", {}).get("herdr", {}).get("root_tab")
        if named == tab_id:
            return
        client = SubprocessHerdrClient()
        rename = getattr(client, "tab_rename", None)
        if rename is None or not client.available():
            return
        rename(tab_id, ROOT_TAB_NAME)
        with coordinator.store.locked() as locked:
            integrations = locked.setdefault("integrations", {})
            integrations.setdefault("herdr", {})["root_tab"] = tab_id

def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # The runner is an internal child process. Keep it out of the public
    # command help so worker commands cannot accidentally depend on it.
    if raw_argv and raw_argv[0] == "_worker-runner":
        runner_parser = argparse.ArgumentParser(prog="helm _worker-runner")
        runner_parser.add_argument("--config", required=True)
        runner_args = runner_parser.parse_args(raw_argv[1:])
        return _worker_runner(runner_args.config)
    parser = _build_parser()
    args = parser.parse_args(raw_argv)
    try:
        store, helm_root = _store_for_args(args)
        coordinator = Coordinator(store)
        _name_root_tab(coordinator)
    except (
        HelmError,
        OSError,
        subprocess.SubprocessError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        # A state file Helm cannot open is precisely the condition doctor
        # exists to name, and every command opens the store before dispatch --
        # so without this, the one case that most needs a report got a bare
        # exit 2 and no output at all. The checks that need no store still run.
        if args.command == "doctor":
            return _emit_doctor(
                args,
                doctor_module.report_unopenable_state(
                    _doctor_root(args), str(exc), args.doctor_project
                ),
            )
        print(f"helm: {exc}", file=sys.stderr)
        return 2
    try:
        # Dispatched ahead of the role check on purpose. Doctor authorizes
        # nothing and is open to every caller, so the check is a no-op for it --
        # but it reads the state to identify the caller, and a root whose state
        # will not open is exactly the root doctor was run to diagnose.
        if args.command == "doctor":
            return _doctor_command(coordinator, helm_root, args)

        refusal = _authority_refusal(coordinator, args)
        if refusal is not None:
            print(f"helm: {refusal}", file=sys.stderr)
            return 2

        if args.command == "init":
            initialized = store.initialize_root(helm_root or args.init_root)
            print(f"Initialized Helm root {initialized}")
            print(f"  projects={initialized / 'projects'}")
            print(f"  state={initialized / 'state'}")
            return 0

        if args.command == "run":
            if helm_root is None:
                raise HelmError("a Helm root is required; run helm init first")
            brief = _task_brief(args.brief)
            if not brief:
                raise HelmError(
                    "No task supplied. Provide a brief or run helm run "
                    f"{args.project_id} interactively to start a conversation."
                )
            project = coordinator.discover_project(helm_root, args.project_id)
            task = coordinator.create_task(
                project["id"], brief, delivery_policy=args.delivery, domain=args.domain,
                agent=args.agent, model=args.model, effort=args.effort,
                ticket=args.ticket,
                no_domain=args.no_domain,
            )
            if args.herdr:
                worker = HerdrAdapter(coordinator).launch_task(
                    task["id"], args.worker_command_text, wait=not args.asynchronous
                )
                mode = (
                    "herdr"
                    if worker.get("execution") == "herdr"
                    else "process fallback (Herdr unavailable)"
                )
            else:
                worker = coordinator.launch_worker(
                    task["id"], args.worker_command_text, wait=not args.asynchronous
                )
                mode = "process (--no-herdr)"
            _ensure_foreman(coordinator, project["id"], herdr=args.herdr)
            print(
                f"Ran {_project_label(project)} task={task['id']} "
                f"worker={worker['id']} [{worker['status']}] mode={mode} "
                f"domain={task.get('domain') or 'none'} "
                f"agent={worker.get('agent_id', 'default')} reason={worker.get('agent_reason', '')}"
            )
            return 0

        if args.command == "project":
            if args.project_command == "release":
                outcome = coordinator.release_project(args.project_id)
                print(
                    f"Released {len(outcome['released'])} task(s) in {outcome['project_id']}"
                )
                for entry in outcome["kept"]:
                    print(f"  kept {entry['task_id']}: {entry['reason']}")
                with contextlib.suppress(HelmError, OSError):
                    if HerdrAdapter(coordinator).close_project_space_if_finished(
                        args.project_id
                    ):
                        print("  space closed")
            elif args.project_command == "status":
                _print_project_status(coordinator.project_status(args.project_id))
            elif args.project_command == "domain":
                project = coordinator.set_project_domains(args.project_id, args.domains)
                configured = project.get("domains") or []
                print(
                    f"{_glyph_for(coordinator, args.project_id)} {args.project_id} "
                    + (
                        f"defaults to domain {', '.join(configured)}"
                        if configured
                        else "has no default domain; tasks need --domain"
                    )
                )
            elif args.project_command == "note":
                entry = coordinator.record_situation(
                    args.project_id, args.text, supersedes=args.supersedes
                )
                print(f"Recorded {entry['id']}: {entry['text']}")
            elif args.project_command == "action":
                entry = coordinator.record_project_action_item(
                    args.project_id,
                    args.text,
                    source=args.source,
                    task_id=args.task_id,
                )
                print(f"Recorded action {entry['id']}: {entry['text']}")
            elif args.project_command == "add":
                project = coordinator.register_project(
                    args.name,
                    args.root,
                    project_id=args.project_id,
                    delivery_policy=args.delivery,
                    init_git=args.init_git,
                    confirm=args.confirm,
                )
                print(f"Registered {_project_label(project)}  root={project['root']}  delivery={project['delivery_policy']}")
            else:
                _discover_if_configured(coordinator, helm_root)
                _print_projects(coordinator.list_projects())
            return 0

        if args.command == "task":
            if args.task_command == "create":
                task = coordinator.create_task(
                    args.project_id,
                    args.brief,
                    delivery_policy=args.delivery,
                    domain=args.domain,
                    agent=args.agent,
                    model=args.model,
                    ticket=args.ticket,
                    no_domain=args.no_domain,
                    read_only=args.read_only,
                )
                print(f"Created task {task['id']} [{task['status']}] project={task['project_id']} policy={task['delivery_policy']}")
            elif args.task_command == "allocate":
                task = coordinator.allocate_task(args.task_id)
                print(f"Allocated {task['id']} workspace={task['workspace']} branch={task['branch']}")
            elif args.task_command == "inspect":
                _print_inspect(coordinator.inspect_task(args.task_id))
            elif args.task_command == "evidence":
                detail = {}
                if args.detail:
                    parsed = json.loads(args.detail)
                    if not isinstance(parsed, dict):
                        raise HelmError("--detail must be a JSON object")
                    detail = parsed
                recorded = coordinator.record_task_evidence(
                    args.task_id,
                    tip=args.tip,
                    command=args.suite_command,
                    exit_code=args.exit_code,
                    detail=detail,
                )
                print(
                    f"Recorded full-suite evidence for {args.task_id} at "
                    f"{recorded['tip']} (exit {recorded['exit']})"
                )
            elif args.task_command == "approve":
                task = coordinator.approve_task(
                    args.task_id, args.note, grant_id=args.grant_id
                )
                under = task["approval"].get("grant_id")
                authority = f" under standing grant {under}" if under else ""
                print(
                    f"Approved task {task['id']}{authority}; "
                    "merge remains an explicit separate command"
                )
            elif args.task_command == "pr":
                pushed = coordinator.publish_task_branch(
                    args.task_id,
                    remote=args.remote,
                    grant_id=args.grant_id,
                    confirm=args.confirm,
                )
                print(
                    f"Pushed {pushed['branch']} -> {pushed['remote']} "
                    f"(authorized by {pushed['authorized_by']})"
                )
                if not args.no_open:
                    _open_pull_request(coordinator, args.task_id, pushed)
                    with contextlib.suppress(HelmError, OSError):
                        task = coordinator.inspect_task(args.task_id)["task"]
                        if task.get("status") in {"pr-open", "pr-merged"}:
                            _release_finished_space(coordinator, task)
            elif args.task_command == "pr-status":
                task = coordinator.record_pr_status(
                    args.task_id,
                    state=args.state,
                    url=args.url,
                    comments=args.comments,
                    checks=args.checks,
                    review_decision=args.review_decision,
                    merge_commit=args.merge_commit,
                )
                delivery = task.get("delivery") or {}
                print(
                    f"Recorded PR {args.state} for task {task['id']} "
                    f"[{task['status']}]"
                    + (f" {delivery.get('url')}" if delivery.get("url") else "")
                )
                if task["status"] == "pr-merged":
                    _release_finished_space(coordinator, task)
            elif args.task_command == "pr-sync":
                task = _sync_pull_request_status(coordinator, args.task_id)
                delivery = task.get("delivery") or {}
                print(
                    f"Synced PR for task {task['id']} [{task['status']}]"
                    + (f" {delivery.get('url')}" if delivery.get("url") else "")
                )
                if task["status"] == "pr-merged":
                    _release_finished_space(coordinator, task)
            elif args.task_command == "outcome":
                _print_outcome(coordinator.task_outcome(args.task_id))
            elif args.task_command == "deliver":
                _print_delivery(coordinator.deliver_task_artifacts(args.task_id, force=args.force))
            elif args.task_command == "merge":
                task = coordinator.merge_task(args.task_id)
                print(f"Merged task {task['id']} with local fast-forward")
                # A merge moves tracked files only. Without this the rendered
                # video -- the actual product -- stays in the worktree and dies
                # with it.
                with contextlib.suppress(HelmError, OSError):
                    _print_delivery(coordinator.deliver_task_artifacts(args.task_id))
                _release_finished_space(coordinator, task)
            elif args.task_command == "continue":
                task = coordinator.continue_task(
                    args.task_id, args.brief, read_only=args.read_only
                )
                print(
                    f"Task {task['id']} reopened for round {len(task.get('rounds', [])) + 1} "
                    f"in {task['workspace']}"
                )
                print(f"  branch {task.get('branch') or '(none)'} — launch a worker to run it")
            elif args.task_command == "cleanup":
                task = coordinator.cleanup_task(
                    args.task_id, delete_branch=args.delete_branch
                )
                print(f"Cleaned task {task['id']} workspace (dirty/unresolved work is always refused)")
                if not task.get("branch"):
                    pass  # a foreman drives rather than edits and owns no branch
                elif task.get("branch_removed"):
                    print(f"  branch {task['branch']} deleted")
                else:
                    print(
                        f"  branch {task['branch']} kept; discard it with "
                        f"helm task cleanup {task['id']} --delete-branch"
                    )
                _release_finished_space(coordinator, task)
            return 0

        if args.command == "worker":
            if args.worker_command == "launch":
                # Herdr by default, like `helm run`. A worker started off to
                # the side has nowhere to be looked at, which matters most for
                # the agents a foreman spawns: it launches through this
                # command, so every one of them used to land invisible while
                # the foreman itself sat in a tab. The adapter falls back to
                # the process launcher when Herdr is unavailable, so this
                # changes where a worker is shown, never whether it runs.
                launcher = (
                    HerdrAdapter(coordinator).launch_task
                    if args.herdr
                    else coordinator.launch_worker
                )
                worker = launcher(
                    args.task_id,
                    args.worker_command_text,
                    wait=not args.asynchronous,
                    domain=args.domain,
                    agent=args.agent,
                )
                _ensure_foreman(coordinator, worker["project_id"], herdr=args.herdr)
                print(
                    f"Worker {worker['id']} [{worker['status']}] task={worker['task_id']} "
                    f"pid={worker.get('pid')} agent={worker.get('agent_id', 'default')} "
                    f"reason={worker.get('agent_reason', '')}"
                )
            elif args.worker_command == "round":
                adapter = HerdrAdapter(coordinator)
                data = coordinator.store.load()
                # The resident is the task's most recent worker whose pane
                # still exists. Its RECORD is usually settled -- a terminal
                # result settles the worker while the interactive session
                # stays open -- so liveness here means the pane, not the
                # status field.
                candidates = sorted(
                    (
                        w for w in data.get("workers", {}).values()
                        if w.get("task_id") == args.task_id
                    ),
                    key=lambda w: (w.get("started_at") or "", w.get("id") or ""),
                )
                resident = None
                if candidates and not args.fresh:
                    latest = candidates[-1]
                    layout = (
                        data.get("integrations", {}).get("herdr", {})
                        .get("workers", {}).get(latest["id"])
                    )
                    if layout is not None:
                        resident = latest
                if resident is not None:
                    task = coordinator.continue_task(
                        args.task_id, args.brief,
                        read_only=args.read_only,
                        reuse_worker=(
                            resident["id"]
                            if resident.get("status") == "running" else None
                        ),
                    )
                    round_no = len(task.get("rounds", [])) + 1
                    try:
                        delivered = adapter.answer_worker(
                        resident["id"],
                        f"ROUND {round_no} for your task {args.task_id} "
                        f"({'read-only' if args.read_only else 'state-changing'}). "
                        "Same worktree, same branch, same reporting protocol; finish "
                        f"with one result. BRIEF: {args.brief}",
                        )
                    except HerdrUnavailable:
                        # A vanished pane mid-delivery is the same fact as a
                        # refused delivery: the resident cannot take the
                        # round. Raw provider errors used to escape here and
                        # strand the task in its just-continued state.
                        delivered = False
                    if delivered:
                        print(
                            f"Round {round_no} delivered into live worker "
                            f"{resident['id']} on {args.task_id}"
                        )
                        return 0
                    # The session looked alive but the round never landed in it.
                    # A resident nobody can reach is a dead driver: stand it
                    # down and fall through to a fresh launch, saying so.
                    coordinator.stop_worker(
                        resident["id"],
                        reason="round delivery failed; replacing the resident session",
                    )
                    # Stopping a still-"running" resident fails its task, but
                    # this round was just opened on it: the failure belongs to
                    # the dead session, not the round. Restore the round's own
                    # state so the fresh launch below is not refused.
                    with coordinator.store.locked() as data:
                        stopped_task = data["tasks"][args.task_id]
                        if stopped_task.get("status") == "failed":
                            stopped_task["status"] = "allocated"
                    print(
                        f"Live session {resident['id']} did not accept the round; "
                        "stopped it and launching a fresh worker"
                    )
                else:
                    coordinator.continue_task(
                        args.task_id, args.brief, read_only=args.read_only
                    )
                try:
                    worker = adapter.launch_task(args.task_id, None, wait=False)
                except BaseException:
                    # The round was opened but its worker never launched.
                    # Left as-is the task sits in "allocated", which no later
                    # round may continue from -- so put back the settled
                    # status the round found it in.
                    with coordinator.store.locked() as failed_data:
                        stranded = failed_data["tasks"][args.task_id]
                        if stranded.get("status") == "allocated":
                            stranded["status"] = "completed"
                    raise
                print(
                    f"Round launched with fresh worker {worker['id']} on {args.task_id}"
                )
            elif args.worker_command == "poll":
                worker = coordinator.poll_worker(args.worker_id)
                print(f"Worker {worker['id']} [{worker['status']}] task={worker['task_id']} exit={worker.get('exit_code')}")
            elif args.worker_command == "wait":
                worker = coordinator.wait_worker(args.worker_id)
                print(f"Worker {worker['id']} [{worker['status']}] task={worker['task_id']} exit={worker.get('exit_code')}")
            elif args.worker_command in {"message", "report"}:
                payload: dict[str, Any] = {}
                if args.payload:
                    parsed = json.loads(args.payload)
                    if not isinstance(parsed, dict):
                        raise HelmError("--payload must be a JSON object")
                    payload.update(parsed)
                if args.path:
                    payload["path"] = args.path
                if args.action:
                    payload["action"] = args.action
                if args.type == "approval-needed" and not args.action:
                    # Refused at the edge as well as in core, so the worker gets
                    # the usable form rather than a validation error.
                    raise HelmError(
                        "--type approval-needed needs --action naming exactly what "
                        "you would do: push, publish, delete, or external"
                    )
                task = coordinator.record_worker_message(
                    args.worker_id,
                    args.type,
                    args.text,
                    payload=payload,
                    requested_status=args.status,
                )
                # Push the update onward to the project's pane now.  Presentation
                # must never decide whether the report itself was recorded.
                released = False
                released_tabs: list[str] = []
                routed: list[str] = []
                with contextlib.suppress(HelmError, OSError):
                    adapter = HerdrAdapter(coordinator)
                    adapter.route_worker_messages(args.worker_id)
                    # Before anything closes. This command runs inside the
                    # worker's own pane, so its output is printed onto the
                    # surface the next two calls are about to remove; the
                    # outcome and the decision it leaves have to reach the
                    # driver, the project's own pane, and the durable record
                    # first. A live foreman is one of those channels, not a
                    # precondition -- a project without a driver is exactly the
                    # case that needed telling.
                    if args.type in Coordinator.TERMINAL_REPORT_KINDS:
                        routed = adapter.notify_coordinator(args.worker_id)["channels"]
                    released_tabs = adapter.release_finished_tabs()
                    # A reported, clean finish releases the project's space.
                    released = adapter.close_project_space_if_finished(task["project_id"])
                told_foreman = "foreman" in routed
                print(f"Recorded {args.type} for task {task['id']} [{task['status']}]")
                if args.type == "approval-needed":
                    hold = coordinator.task_hold(task) or {}
                    print(
                        "  The task is paused, not finished; this session stays open. "
                        "A human authorizes it with: helm approval release "
                        f"{task['id']} --action {hold.get('action') or '<action>'} --confirm"
                    )
                    print(
                        "  When told it is approved, run helm worker action-start "
                        f"{args.worker_id} immediately before acting; it checks the "
                        "approval against this exact state and spends it once."
                    )
                if told_foreman:
                    print("  Told the project's foreman; it is theirs to act on")
                if routed:
                    print(f"  Routed the final outcome to: {', '.join(routed)}")
                if released_tabs:
                    print(f"  Closed {len(released_tabs)} finished worker tab(s)")
                if released:
                    print(f"Closed the Herdr space for project {task['project_id']}")
                # Say it here too. The gate is recorded either way, but the
                # coordinator reading this line is the one who can act on it
                # now rather than at the next `helm status`.
                if args.type in Coordinator.TERMINAL_REPORT_KINDS:
                    for item in coordinator.open_action_items(task["project_id"]):
                        if item.get("kind") != DELIVERY_DECISION_KIND:
                            continue
                        scope = f"task {item['task_id']}" if item.get("task_id") else "project"
                        print(f"  Commander decision pending on {scope}: {item['text']}")
            elif args.worker_command == "stop":
                # Through the adapter, because a Herdr worker's pane is what
                # is actually running it; core settles the record either way.
                stopped = HerdrAdapter(coordinator).stop_worker(
                    args.worker_id, args.reason
                )
                where = []
                if stopped.get("signalled"):
                    where.append("process signalled")
                if stopped.get("tab_closed"):
                    where.append("pane closed")
                print(
                    f"Stopped worker {stopped['id']} [{stopped['status']}] "
                    f"task={stopped['task_id']}"
                    + (f" ({', '.join(where)})" if where else "")
                )
                print(
                    "  Its log and worktree are kept as evidence; remove them with "
                    f"helm task cleanup {stopped['task_id']}"
                )
                with contextlib.suppress(HelmError, OSError):
                    if HerdrAdapter(coordinator).close_project_space_if_finished(
                        stopped["project_id"]
                    ):
                        print(f"Closed the Herdr space for project {stopped['project_id']}")
            elif args.worker_command == "action-start":
                started = coordinator.start_authorized_action(args.worker_id)
                print(
                    f"Authorized: {started['action']} for task {started['task_id']} "
                    f"[{started['status']}]"
                )
                if started.get("note"):
                    print(f"  Commander's note: {started['note']}")
                print(
                    "  This authorization is now spent. Act, then report the outcome "
                    "with --type result and any receipt in --payload."
                )
                with contextlib.suppress(HelmError, OSError):
                    HerdrAdapter(coordinator).route_worker_messages(args.worker_id)
            elif args.worker_command == "answer":
                # Record first: the answer is part of the task's audit trail
                # whether or not a presentation surface can deliver it.
                task = coordinator.record_worker_message(args.worker_id, "answer", args.text)
                delivered = False
                with contextlib.suppress(HelmError, OSError):
                    delivered = HerdrAdapter(coordinator).answer_worker(args.worker_id, args.text)
                print(
                    f"Answered worker {args.worker_id} for task {task['id']} "
                    f"[{'delivered' if delivered else 'recorded only'}]"
                )
            return 0

        if args.command in {"learning", "learn"}:
            if args.learning_command == "propose":
                proposals = coordinator.generate_learning_proposals(
                    args.task_id,
                    domain=args.domain,
                    fact=args.fact,
                    rationale=args.rationale,
                    confidence=args.confidence,
                    artifact_ids=args.artifact_ids,
                    message_ids=args.message_ids,
                )
                for proposal in proposals:
                    _print_learning(proposal)
            elif args.learning_command == "list":
                proposals = coordinator.list_learning_proposals(
                    domain=args.domain, status=args.status, task_id=args.task_id
                )
                if not proposals:
                    print("No learning proposals.")
                for proposal in proposals:
                    _print_learning(proposal)
            elif args.learning_command == "inspect":
                print(_json(coordinator.inspect_learning_proposal(args.proposal_id)))
            elif args.learning_command == "edit":
                proposal = coordinator.edit_learning_proposal(
                    args.proposal_id,
                    proposed_fact=args.fact,
                    rationale=args.rationale,
                    confidence=args.confidence,
                )
                _print_learning(proposal)
            elif args.learning_command == "approve":
                proposal = coordinator.approve_learning_proposal(
                    args.proposal_id, args.note, actor=args.actor
                )
                _print_learning(proposal)
            elif args.learning_command == "reject":
                proposal = coordinator.reject_learning_proposal(
                    args.proposal_id, args.note, actor=args.actor
                )
                _print_learning(proposal)
            elif args.learning_command == "apply":
                proposal = coordinator.apply_learning_proposal(
                    args.proposal_id, actor=args.actor, scope=args.scope
                )
                _print_learning(proposal)
            return 0

        if args.command == "agent":
            if args.agent_command == "models":
                _print_agent_models(coordinator, as_json=args.agent_models_json)
            else:
                _print_agents(coordinator, check=args.agent_command == "check")
            return 0

        if args.command == "prefs":
            return _prefs_command(coordinator, helm_root, args)

        if args.command == "herdr":
            adapter = HerdrAdapter(coordinator)
            if args.herdr_command == "launch":
                worker = adapter.launch_task(
                    args.task_id,
                    args.worker_command_text,
                    wait=not args.asynchronous,
                    domain=args.domain,
                    agent=args.agent,
                )
                mode = "herdr" if worker.get("execution") == "herdr" else "terminal fallback"
                _ensure_foreman(coordinator, worker["project_id"])
                print(
                    f"Worker {worker['id']} [{worker['status']}] task={worker['task_id']} "
                    f"pid={worker.get('pid')} mode={mode} "
                    f"agent={worker.get('agent_id', 'default')} reason={worker.get('agent_reason', '')}"
                )
            elif args.herdr_command == "poll":
                worker = adapter.poll_worker(args.task_id)
                print(f"Worker {worker['id']} [{worker['status']}] task={worker['task_id']} exit={worker.get('exit_code')}")
            elif args.herdr_command == "wait":
                worker = adapter.wait_worker(args.task_id, timeout=args.timeout)
                print(f"Worker {worker['id']} [{worker['status']}] task={worker['task_id']} exit={worker.get('exit_code')}")
            elif args.herdr_command == "cleanup":
                print(f"Cleaned Herdr worker resources: {adapter.cleanup_task(args.task_id)}")
            elif args.herdr_command == "cleanup-project":
                print(f"Cleaned Herdr project resources: {adapter.cleanup_project(args.project_id)}")
            elif args.herdr_command == "cleanup-coordinator":
                print(f"Cleaned Herdr coordinator resources: {adapter.cleanup_coordinator()}")
            elif args.herdr_command == "relabel":
                for entry in adapter.relabel():
                    if entry.get("error"):
                        print(f"  {entry['kind']} {entry['id']}: {entry['error']}")
                    else:
                        print(f"  renamed {entry['kind']} -> {entry['label']}")
            return 0

        if args.command == "status":
            _discover_if_configured(coordinator, helm_root)
            _print_status(coordinator, args.project_id)
            return 0

        if args.command == "skills":
            project = coordinator.get_project(args.project_id)
            if args.brief is not None:
                selection = coordinator.select_skills(
                    project, {"id": "-", "brief": args.brief}, args.agent
                )
                print(selection["reason"])
                for skill in selection["selected"]:
                    print(f"  {skill['id']}  {skill['path']}")
                    print(f"    because: {skill['reason']}")
                    print(f"    delivery: {skill['delivery']}")
                for entry in selection["skipped"]:
                    print(f"  - {entry['id']}: {entry['reason']}")
                problems = selection["problems"]
            else:
                found = coordinator.discover_skills(project, args.agent)
                print(f"Roots read: {', '.join(found['roots'])}")
                if not found["skills"]:
                    print("No readable skills in this project.")
                for skill in found["skills"]:
                    print(f"  {skill['id']}  {skill['path']}")
                    print(f"    {skill['description'][:200]}")
                    if skill["duplicate_of"]:
                        print(f"    also present at {skill['duplicate_of']}")
                problems = found["problems"]
            for problem in problems:
                # Reported rather than skipped in silence: a skill that cannot
                # be read is the case most likely to matter.
                print(f"  ! {problem.get('id') or '(root)'}: {problem['problem']}")
            return 1 if problems else 0

        if args.command == "domain":
            projects = coordinator.list_projects()
            catalogue = coordinator.domain_catalogue(projects[0] if projects else {"root": "."})
            if not catalogue:
                print("No domains found.")
                return 0
            print("Choose by what the task IS, not by words in its brief.")
            print("Pick a selectable domain, or none. Then pass --domain to helm run.\n")
            for entry in catalogue:
                if not entry["selectable"]:
                    continue
                print(f"  {entry['id']}")
                print(f"    is for:  {entry['applies_to'] or '(undeclared)'}")
                for line in entry["use_when"]:
                    print(f"    use when: {line}")
                for line in entry["not_for"]:
                    print(f"    NOT for:  {line}")
                if entry["extends"]:
                    print(f"    composes: {', '.join(entry['extends'])}")
                print()
            blocks = [e["id"] for e in catalogue if not e["selectable"]]
            if blocks:
                print(f"Building blocks (reached only via extends): {', '.join(blocks)}")
            return 0

        if args.command == "watchdog":
            # Root resolved here so a scheduler entry carries an absolute path:
            # a launchd agent or systemd timer has no shell, no cwd worth
            # trusting, and no idea which Helm root it was installed for.
            root_path = Path(args.helm_root).resolve() if args.helm_root else Path.cwd()
            if args.watchdog_command == "run":
                return watchdog_module.run(root_path, args.interval, once=args.once)
            if args.watchdog_command == "install":
                return watchdog_module.install(root_path, args.interval)
            return watchdog_module.uninstall(root_path)

        if args.command == "pending":
            # Deliberately narrow and deliberately silent. This is meant to run
            # on every turn, so anything it prints when nothing is wrong is
            # noise that trains the reader to skip it -- the same failure as an
            # attention list full of healthy workers. It prints four things: an
            # agent that asked a human and got no answer, a gate holding work
            # still, an outcome nobody has relayed yet, and a worker that has
            # stopped producing. Everything else waits to be asked for.
            # RECONCILE FIRST, because reporting is a push and a push can be
            # missed: a worker killed by the OS never reports anything, and its
            # record sat as `running` until a human happened to run `helm
            # watch`. Polling every worker whose process is gone settles those
            # here, so this check reflects what is TRUE rather than what was
            # reported -- and a task that died silently becomes a failure
            # decision on its own, without anybody noticing first.
            #
            # Cheap by construction: `poll_worker` on an already-settled worker
            # is a no-op, and the loop only touches workers still marked
            # running.
            with contextlib.suppress(HelmError, OSError):
                for entry in list(coordinator.store.load().get("workers", {}).values()):
                    if entry.get("status") != "running":
                        continue
                    with contextlib.suppress(HelmError, OSError):
                        coordinator.poll_worker(entry["id"])

            # (recency, text): a gate raised two minutes ago must not sit
            # under a blocker from yesterday. The list is read top-down and the
            # top is the only part reliably read, so ordering by age is the
            # difference between surfacing something and burying it. Items with
            # no timestamp sort oldest -- they are the long-standing ones.
            # (recency, display, identity). The identity is the UNtruncated
            # text: --changes compares on it, and comparing truncated lines
            # lets a widening number look like a new item. Every append must
            # carry all three -- a 2-tuple here crashes the whole command,
            # which is the one command that must never fail silently.
            entries: list[tuple[str, str, str]] = []
            for item in coordinator.open_escalations(None):
                glyph = _glyph_for(coordinator, item["project_id"]) if item["project_id"] else " "
                first = next(
                    (line.strip() for line in item["text"].splitlines() if line.strip()), ""
                )
                stamp = str(item.get("at") or item.get("created_at") or "")
                entries.append((
                    stamp,
                    f"{_when_label(stamp)} {glyph} {item['kind']} {item['worker_id']}: {first[:100]}",
                    f"{glyph} {item['kind']} {item['worker_id']}: {first}",
                ))
            for item in coordinator.open_action_items(None):
                if item.get("kind") not in BLOCKING_GATE_KINDS:
                    continue
                stamp = str(item.get("at") or "")
                entries.append((
                    stamp,
                    f"{_when_label(stamp)} {item['glyph']} {item['project_id']} GATE waiting: {item['text'][:100]}",
                    f"{item['glyph']} {item['project_id']} GATE waiting: {item['text']}",
                ))
            # Owed but unrelayed outcomes. `mark_seen=False` matters: this runs
            # unattended, and a check that consumed what it reported would be
            # the exact hole this command exists to close.
            for update in coordinator.project_updates_for_watch(None, mark_seen=False):
                if update.get("kind") != "situation":
                    continue
                # OWED reports only. This command is read unattended and its
                # whole value is that it changes when something needs a human;
                # a routine progress line has nothing that ever clears it, so
                # including one makes the list permanently non-empty and every
                # future change look like the same old news.
                if not update.get("owed"):
                    continue
                # Wider than the rest on purpose. A terminal report's payload is
                # usually at the END of the line -- a video id, a URL, a commit
                # -- so a 100-character cut removes exactly the part worth
                # reading and leaves something that looks like nothing was said.
                stamp = str(update.get("at") or "")
                entries.append((
                    stamp,
                    f"{_when_label(stamp)} {update['glyph']} {update['project_id']}: {update['text'][:220]}",
                    f"{update['glyph']} {update['project_id']}: {update['text']}",
                ))
            for entry in coordinator.worker_health(liveness=_liveness_probe(coordinator)):
                if entry["verdict"] in {
                    "healthy", "settled", "reported", "starting", "driving", "working",
                }:
                    continue
                glyph = _glyph_for(coordinator, entry["project_id"])
                idle = max(
                    entry.get("output_idle_seconds") or 0.0,
                    entry.get("reported_idle_seconds") or 0.0,
                )
                # A health verdict carries no timestamp, but it does carry its
                # own idleness -- so date it from that and it sorts among the
                # timestamped items correctly instead of falling to the bottom
                # as a block. Without this the list only claimed to be
                # newest-first.
                dated = (
                    _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=idle)
                ).isoformat().replace("+00:00", "Z")
                entries.append((
                    dated,
                    f"{_when_label(seconds=idle)} {glyph} {entry['project_id']} "
                    f"{entry['worker_id']} [{entry['verdict']}]: {entry['detail'][:80]}",
                    f"{glyph} {entry['project_id']} {entry['worker_id']} "
                    f"[{entry['verdict']}]: {entry['detail']}",
                ))
            ordered = sorted(entries, key=lambda e: e[0], reverse=True)
            lines = [text for _at, text, _identity in ordered]
            if args.changes:
                # Only what is NEW since the last --changes call. An unattended
                # watch that re-prints the whole list every poll buries the one
                # new line under things already read, and trains its reader to
                # skip it -- the same failure the list itself was built to fix.
                #
                # Compared with digits stripped, because a line carries elapsed
                # time ("quiet for 7481s") that differs on every poll: without
                # that, every health line reads as new, forever.
                #
                # The identity is built from the FULL text, never the truncated
                # display line. Truncating first reintroduces the bug by the
                # back door: when a counter grows from 994s to 1016s the line
                # gets one character longer, the cut lands one character
                # earlier, and the stripped tails differ -- so an unchanged
                # item announces itself again purely because a number got wider.
                #
                # Only ELAPSED TIMES are normalised, not every digit. Stripping
                # all of them also dissolved the worker id, whose digits are
                # most of what distinguishes one from another: two ids differing
                # only in where their digits sit reduced to the SAME key, so the
                # second worker's news was SUPPRESSED once the first had been
                # reported. That error runs the dangerous way -- a repeat is
                # noise, but a swallowed item is the silence this command exists
                # to prevent.
                def _key(text: str) -> str:
                    return re.sub(r"\d+s\b", "Ns", text)

                seen_file = coordinator.store.directory / "pending-seen.json"
                previous: set[str] = set()
                with contextlib.suppress(OSError, ValueError):
                    previous = set(json.loads(seen_file.read_text()))
                current = {_key(identity) for _at, _text, identity in ordered}
                fresh = [
                    text
                    for _at, text, identity in ordered
                    if _key(identity) not in previous
                ]
                with contextlib.suppress(OSError):
                    seen_file.write_text(json.dumps(sorted(current)))
                for line in fresh:
                    print(line.strip())
                return 0
            if not entries:
                return 0
            print(f"HELM NEEDS A HUMAN ({len(lines)}):")
            for line in lines:
                print(f"  {line}")
            print("  (helm status for detail; helm ack <project> once relayed)")
            return 0

        if args.command == "ack":
            done = coordinator.acknowledge_updates(
                args.project_id, args.entry_ids or None
            )
            if not done:
                print("No owed reports were waiting to be acknowledged.")
                return 0
            print(f"Acknowledged {len(done)} report(s) as relayed:")
            for entry in done:
                first = next(
                    (line.strip() for line in entry["text"].splitlines() if line.strip()),
                    "",
                )
                print(f"  {entry['id']} {first[:96]}")
            return 0

        if args.command == "watch":
            report = coordinator.sweep_workers(silence_seconds=args.silence)
            updates = coordinator.project_updates_for_watch()
            # A settled worker's pane is no longer evidence; leaving it open
            # makes the panel harder to read for no benefit.
            with contextlib.suppress(HelmError, OSError):
                adapter = HerdrAdapter(coordinator)
                released = adapter.release_finished_tabs()
                if released:
                    print(f"Closed {len(released)} finished worker tab(s)")
                for project_id in adapter.close_finished_project_spaces():
                    print(f"Closed the Herdr space for project {project_id}")
            if updates:
                print("Project updates:")
                for update in updates:
                    glyph = f"{update['glyph']} " if update.get("glyph") else ""
                    print(
                        f"  {glyph}{update['project_id']}: "
                        f"{str(update.get('text', ''))[:220]}"
                    )
            if not report:
                print("No running workers.")
                return 0
            attention = 0
            for entry in report:
                # `driving` belongs here: a foreman waiting on a worker it
                # launched is doing its job, not failing. Left out, it fell to
                # the foreman branch below and every healthy driver was stamped
                # "nothing is driving it" -- so the one row that was fine read
                # exactly like the two that were genuinely down, and the whole
                # signal stopped being worth reading.
                healthy = entry["verdict"] in {
                    "healthy", "settled", "reported", "starting", "driving", "working",
                }
                if healthy:
                    mark = ""
                elif entry.get("role") == "foreman":
                    # The foreman is what would have noticed the others. When
                    # it is down, nothing is driving the project at all, and
                    # that outranks any single stalled worker on the list.
                    mark = "  <-- URGENT: this project's foreman is down; nothing is driving it"
                else:
                    mark = "  <-- attention"
                if mark:
                    attention += 1
                role = f"{entry['agent_id']}" + (
                    " (foreman)" if entry.get("role") == "foreman" else ""
                )
                print(
                    f"{_glyph_for(coordinator, entry['project_id'])} {entry['worker_id']} "
                    f"[{entry['verdict']}] project={entry['project_id']} "
                    f"task={entry['task_id']} agent={role}: {entry['detail']}{mark}"
                )
                if args.nudge and entry["verdict"] in {"stalled", "quiet"} and not entry["nudged_at"]:
                    nudge = coordinator.nudge_worker(entry["worker_id"])
                    with contextlib.suppress(HelmError, OSError):
                        HerdrAdapter(coordinator).answer_worker(
                            entry["worker_id"], nudge["text"]
                        )
                    print(f"  nudged {entry['worker_id']} for a status push")
            # A non-zero exit lets a scheduled check page a human without
            # anyone reading the output.
            return 1 if attention else 0

        if args.command == "route":
            # Root Helm's whole job for one input: identify the project,
            # make sure its one foreman is live, hand the request off, and
            # come straight back -- never wait for that foreman to act on
            # it. Ensuring the foreman only ever spawns (wait=False); the
            # handoff itself is a pane send-text plus Enter, not a call that
            # blocks on the foreman's own work. Neither step waits on what
            # the foreman does with the request, so this command never
            # becomes the thing that makes one busy project's input hold up
            # another project's -- independent of how long either step
            # itself happens to take.
            #
            # Truthful, not optimistic: the project's own decision to decline
            # a foreman is reported as exactly that, not folded into the
            # generic "could not start one" path used for an actual failure.
            coordinator.get_project(args.project_id)  # truthful "unknown project" first
            # Routing is the moment root touches this project, so it is the
            # moment to say what the project said while nobody was reading.
            # Printed before the hand-off: a request routed on top of an
            # unread "investigation complete" is usually the wrong request.
            _print_new_project_updates(
                coordinator,
                args.project_id,
                heading="Before routing -- new from this project since you last looked:",
                # Same filter as `status`, for a sharper reason: what a router
                # needs is what the project SAID, and a standing delivery
                # decision is neither new nor answerable here. Unfiltered, a
                # dozen identical decision lines pushed the foreman's actual
                # report off the top of the very output meant to prevent
                # routing on top of it -- observed, not hypothesised.
                skip_decisions=True,
            )
            if not coordinator.project_wants_foreman(args.project_id):
                raise HelmError(
                    f'{args.project_id} has declined a foreman ("foreman": false in its '
                    "own record or .helm/project.json); there is nothing for route to hand "
                    "this request to. Appoint one explicitly with helm foreman "
                    f"{args.project_id} first if you want to route to it anyway."
                )
            existing = coordinator.foreman_for(args.project_id)
            started = None
            if existing is None:
                started = _ensure_foreman(
                    coordinator, args.project_id, herdr=args.herdr,
                    command=args.worker_command_text, agent=args.agent,
                    model=args.model,
                    # The request goes into the brief this appointment composes,
                    # so a foreman started by this very call comes up already
                    # holding it rather than hoping to read it afterwards.
                    request=args.text,
                )
                foreman = coordinator.foreman_for(args.project_id)
            else:
                foreman = existing
            if foreman is None:
                raise HelmError(
                    f"{args.project_id} has no live foreman to route to; appointing one "
                    "failed -- see the message above for why"
                )
            # Record first, always, while the worker is still whatever it
            # currently is: the request is part of the project's durable
            # record whether or not a presentation surface can deliver it,
            # the same guarantee `helm worker answer` gives a worker's
            # reply. This is safe to do before checking reachability
            # because `record_worker_message` no longer touches this
            # worker's `last_reported_at` for an `answer` push -- that field
            # is the worker's own liveness signal, and an outbound message
            # Helm is delivering is not evidence the worker is alive, let
            # alone that it received it. Recording after the reachability
            # check would instead let `session_reachable`'s own
            # reconciliation of a dead Herdr pane (it settles the worker to
            # "failed" -- the strongest evidence available that the session
            # is over) run first and leave no running worker left to record
            # onto, silently dropping the request while still saying
            # "recorded". Recording first closes that gap: the request
            # survives in the durable record regardless of what
            # reachability turns out to be.
            task = coordinator.record_worker_message(foreman["id"], "answer", args.text)
            # Reachability is checked after recording, and is unaffected by
            # having just recorded: `session_reachable` asks whether there
            # is a live Herdr pane, with a provider that confirms it, for
            # this worker right now -- not whether it has spoken recently.
            # It is correct for a plain-process foreman too (no `helm herdr`
            # input channel to send into at all) and for a foreman mid-task
            # or quietly idle (both reachable, neither is what this checks).
            # A foreman appointed by this very call is a separate case,
            # handled below via `started is not None` -- its process was
            # just spawned and cannot have a ready pane yet regardless of
            # what `session_reachable` would say.
            reachable = started is None and HerdrAdapter(coordinator).session_reachable(
                foreman["id"]
            )
            if started is not None:
                # A foreman appointed this call cannot have a ready pane yet
                # -- the agent process was just spawned and has not had time
                # to start reading, let alone attach a shell it can accept
                # text into. Sending into it now is a race that would either
                # be silently swallowed by a not-yet-listening pane or wedge
                # ahead of the agent's own startup output; either way a
                # "delivered" claim here would be false. The record above is
                # what makes the request survive regardless: the foreman's
                # own brief-time status read (`foreman_brief`,
                # `helm project status`) surfaces it once it comes up.
                print(
                    f"{_glyph_for(coordinator, args.project_id)} {args.project_id} routed to "
                    f"newly appointed foreman {foreman['id']} task={task['id']} "
                    "[recorded, and written into the brief this appointment composed; the "
                    "foreman is still starting and comes up holding the request, "
                    "not a live pane send]"
                )
                return 0
            if not reachable:
                # A live worker record with nothing that can actually
                # receive text: a plain-process foreman with no Herdr pane
                # at all, or a Herdr pane the provider says is gone or was
                # closed by hand. Neither is "unhealthy" in the sense
                # `worker_health` reports (a busy or quietly idle foreman
                # is not that), so this checks reachability directly rather
                # than reusing that verdict. Never claim a delivery that
                # could not have happened; one foreman per project is
                # preserved -- this does not stop or replace it, only says
                # how to if that is wanted.
                print(
                    f"{_glyph_for(coordinator, args.project_id)} {args.project_id} routed to "
                    f"foreman {foreman['id']} task={task['id']} "
                    "[recorded only; its foreman has no reachable session to send into -- "
                    "either a plain process with no input channel, or its Herdr pane is gone. "
                    f"Replace it with: helm worker stop {foreman['id']} --reason \"...\" "
                    f"&& helm foreman {args.project_id}]"
                )
                return 0
            delivered = False
            with contextlib.suppress(HelmError, OSError):
                delivered = HerdrAdapter(coordinator).answer_worker(foreman["id"], args.text)
            print(
                f"{_glyph_for(coordinator, args.project_id)} {args.project_id} routed to "
                f"foreman {foreman['id']} task={task['id']} "
                f"[{'delivered' if delivered else 'recorded only; the send itself failed'}]"
            )
            return 0

        if args.command == "foreman":
            existing = coordinator.foreman_for(args.project_id)
            if existing is not None:
                # One project, one foreman: a second driver is worse than none.
                # But "already has one" must never become a dead end -- a
                # foreman whose pane was closed by hand still reads as running,
                # and without a way out that project could never be given
                # another driver. So say how to replace it.
                health = {
                    entry["worker_id"]: entry for entry in coordinator.worker_health(liveness=_liveness_probe(coordinator))
                }.get(existing["id"], {})
                verdict = health.get("verdict", "unknown")
                print(
                    f"{_glyph_for(coordinator, args.project_id)} {args.project_id} already has "
                    f"a foreman: {existing['id']} [{verdict}] task={existing['task_id']}"
                )
                if verdict not in {"healthy", "starting", "reported"}:
                    print(
                        f"  It is not driving anything. Replace it with: "
                        f"helm worker stop {existing['id']} --reason \"...\" "
                        f"&& helm foreman {args.project_id}"
                    )
                return 0
            task = coordinator.create_foreman_task(
                args.project_id, agent=args.agent, model=args.model
            )
            if args.herdr:
                worker = HerdrAdapter(coordinator).launch_task(
                    task["id"], args.worker_command_text, wait=False
                )
                mode = (
                    "herdr"
                    if worker.get("execution") == "herdr"
                    else "process fallback (Herdr unavailable)"
                )
            else:
                worker = coordinator.launch_worker(
                    task["id"], args.worker_command_text, wait=False
                )
                mode = "process (--no-herdr)"
            print(
                f"{_glyph_for(coordinator, args.project_id)} {args.project_id} foreman "
                f"{worker['id']} [{worker['status']}] task={task['id']} mode={mode} "
                f"agent={worker.get('agent_id', 'default')}"
            )
            if not coordinator.project_wants_foreman(args.project_id):
                # Started by hand for a project that has not declared one:
                # say so, because nothing will reappoint it after it exits.
                print(
                    '  This project does not declare a foreman. Add "foreman": true to its '
                    ".helm/project.json to have Helm appoint one itself."
                )
            # Say what it cannot do, every time. A driver that looks like a
            # coordinator is the one mistake this command can cause.
            print(
                "  It drives this project's loops. It cannot approve, merge, push, "
                "publish, delete, or grant a standing approval -- those stay here."
            )
            return 0

        if args.command == "board":
            projects = coordinator.board()
            destination = Path(args.out) if args.out else store.directory / "board.html"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                _board_html(projects, __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")),
                encoding="utf-8",
            )
            shown = sum(len(p["tasks"]) for p in projects)
            print(f"Board written: {destination}  ({shown} task(s) across {len(projects)} project(s))")
            if args.open_it:
                subprocess.run(["open", str(destination)], check=False)
            return 0

        if args.command == "reflect":
            print(_json(coordinator.reflection_evidence(args.hours)))
            return 0

        if args.command == "tail":
            for line in coordinator.worker_output(args.worker_id, args.lines):
                print(line)
            return 0

        if args.command == "approval":
            if args.approval_command == "grant":
                granted = coordinator.grant_approval(
                    args.action, project_id=args.project_id, note=args.note
                )
                scope = granted["project_id"] or "all projects"
                print(f"Granted {granted['id']}: {granted['action']} for {scope}")
                # A standing approval is the one place a later action happens
                # without anyone being asked, so say plainly what changed.
                print(
                    f"  Helm may now {granted['action']} for {scope} without asking again. "
                    f"Withdraw it with: helm approval revoke {granted['id']}"
                )
            elif args.approval_command == "revoke":
                revoked = coordinator.revoke_approval_grant(args.grant_id, args.note)
                print(f"Revoked {revoked['id']}: {revoked['action']} for {revoked['project_id'] or 'all projects'}")
            elif args.approval_command == "release":
                task = coordinator.release_task_hold(
                    args.task_id,
                    action=args.action,
                    note=args.note,
                    grant_id=args.grant_id,
                    confirm=args.confirm,
                )
                hold = task.get("hold") or {}
                authorization = hold.get("authorization") or {}
                snapshot = authorization.get("snapshot") or hold.get("snapshot") or {}
                worker_id = hold.get("worker_id", "")
                message = args.text or (
                    f"Approved: {args.action}. The commander authorized exactly this"
                    + (f" ({args.note})" if args.note else "")
                    + ". Run `helm worker action-start "
                    f"{worker_id}` immediately before you act -- it checks the approval "
                    "against the state that was approved and spends it once -- then do "
                    "it and report the outcome with --type result."
                )
                # Delivery is its own fact. The decision is already recorded and
                # the task stays paused until the session itself acknowledges by
                # spending the ticket, so a failed delivery is a retry rather
                # than an authorization nobody received.
                delivered = False
                with contextlib.suppress(HelmError, OSError):
                    adapter = HerdrAdapter(coordinator)
                    if adapter.session_reachable(worker_id):
                        delivered = adapter.answer_worker(worker_id, message)
                if delivered:
                    # Recorded only when it actually arrived, so the escalation
                    # stays open while nobody has been told.
                    with contextlib.suppress(HelmError, OSError):
                        coordinator.record_worker_message(worker_id, "answer", message)
                    with contextlib.suppress(HelmError, OSError):
                        coordinator.mark_hold_delivered(args.task_id, delivered=True)
                print(
                    f"Authorized {args.action} for task {task['id']} [{task['status']}] "
                    f"worker={worker_id} "
                    f"[{'delivered' if delivered else 'NOT delivered'}]"
                )
                if authorization.get("grant_id"):
                    print(f"  Authority: standing grant {authorization['grant_id']}")
                else:
                    print(
                        f"  Authority: explicit confirmation "
                        f"({(authorization.get('authority') or {}).get('mode', 'session')})"
                    )
                if snapshot.get("scope") == "workspace":
                    print(
                        f"  Bound to {snapshot.get('branch')} @ "
                        f"{(snapshot.get('revision') or '')[:12]} plus its index, "
                        f"working tree, {len(snapshot.get('untracked', []))} untracked "
                        f"and {len(snapshot.get('artifacts', []))} declared artifact(s); "
                        "any change refuses at action-start"
                    )
                else:
                    print("  No worktree to bind: this task holds no branch of its own")
                if delivered:
                    print(
                        "  The task stays paused until the worker spends it with "
                        f"helm worker action-start {worker_id}"
                    )
                else:
                    print(
                        "  Nothing was delivered and nothing is spent. Retry with the "
                        f"same command (helm approval release {args.task_id} --action "
                        f"{args.action} --confirm), or repair the task with "
                        f"helm approval repair {args.task_id} if its session is gone."
                    )
                    return 1
            elif args.approval_command == "repair":
                # Provider evidence, gathered here: core never talks to a
                # presentation service and must not guess a session is alive.
                live = False
                with contextlib.suppress(HelmError, OSError):
                    adapter = HerdrAdapter(coordinator)
                    hold = coordinator.hold_worker_id(args.task_id)
                    live = bool(hold) and adapter.session_reachable(hold)
                repaired = coordinator.repair_task_hold(
                    args.task_id, session_live=live, note=args.note
                )
                print(
                    f"Repaired task {repaired['task_id']}: {repaired['outcome']}"
                    + (
                        f" (hold {repaired['hold']['id']} waiting on "
                        f"{repaired['hold']['action']})"
                        if repaired.get("hold")
                        else ""
                    )
                )
                if repaired["outcome"] == "abandoned":
                    print(
                        "  Its session is gone, so nothing could be authorized into it. "
                        "The task is failed: its log is the evidence, and it can now be "
                        f"cleaned up with helm task cleanup {repaired['task_id']}."
                    )
                elif repaired["outcome"] == "restate-requested":
                    print(
                        "  The recorded request named no usable action. The live worker "
                        "has been asked to re-report it with --action."
                    )
                else:
                    print(
                        "  Authorize it with: helm approval release "
                        f"{repaired['task_id']} --action {repaired['hold']['action']} --confirm"
                    )
            elif args.approval_command == "check":
                covering = coordinator.approval_grant_for(args.action, args.project_id)
                scope = args.project_id or "all projects"
                if covering is None:
                    print(f"No standing approval covers {args.action} for {scope}; ask the user.")
                    return 1
                print(
                    f"{covering['id']} covers {args.action} for "
                    f"{covering['project_id'] or 'all projects'}: {covering['note']}"
                )
            else:
                _print_approval_grants(coordinator, include_revoked=args.include_revoked)
            return 0
        if args.command == "gate":
            if args.gate_command == "propose":
                task = coordinator.propose_gate(args.task_id, args.gate_type, args.text)
                print(
                    f"Proposed the {args.gate_type} gate on task {task['id']}; "
                    "waiting on the commander: helm gate decide "
                    f"{task['id']} --type {args.gate_type} --confirm|--skip"
                )
            else:
                task = coordinator.decide_gate(
                    args.task_id, args.gate_type,
                    confirm=args.confirm, skip=args.skip, note=args.note,
                )
                verb = "Skipped" if args.skip else "Confirmed"
                print(f"{verb} the {args.gate_type} gate on task {task['id']}")
                # And TELL the foreman. A gate is the one thing a foreman is
                # explicitly instructed to stop and wait for, and the decision
                # was recorded where only a poll would find it -- so a
                # confirmed gate left the agent sitting idle, indefinitely,
                # having done nothing wrong. Two foremen on two projects
                # stalled that way in one session. Delivery is best-effort:
                # the decision is already durable, and a foreman that cannot
                # be reached still finds it in `helm project status`.
                notice = (
                    f"Helm: the commander {verb.lower()} your {args.gate_type} gate"
                    f" on task {task['id']}."
                    + (f" Note: {args.note}" if args.note else "")
                    + (
                        " Proceed."
                        if not args.skip
                        else " It was skipped, not confirmed -- do not treat that as "
                        "approval of the contract as proposed."
                    )
                )
                delivered = False
                live = None
                with contextlib.suppress(HelmError, OSError):
                    live = next(
                        (
                            worker
                            for worker in coordinator.store.load()
                            .get("workers", {})
                            .values()
                            if worker.get("task_id") == task["id"]
                            and worker.get("status") == "running"
                        ),
                        None,
                    )
                    if live is not None:
                        delivered = bool(
                            HerdrAdapter(coordinator).answer_worker(live["id"], notice)
                        )
                if not delivered:
                    # Two very different failures were being reported in one
                    # soft sentence. A live agent that merely could not be
                    # reached will find the decision by polling; a session that
                    # has ENDED never will, and the gate is bound to that
                    # task's row -- so a replacement foreman re-proposes and
                    # the decision just made is spent on nothing. That case
                    # needs a different next command, and it needs to be hard
                    # to miss: an agent waiting on a gate that answers into a
                    # dead session stalls the project until a human notices.
                    if live is not None:
                        print(
                            "  Not delivered into a live session. The decision is "
                            "recorded; the foreman will see it in helm project "
                            "status, or route it a message to move it along."
                        )
                    else:
                        project_id = task.get("project_id") or "<project>"
                        print(
                            "  NOT DELIVERED -- no live session holds this task, so "
                            "nothing is waiting on the decision you just made."
                        )
                        print(
                            "  The decision is recorded on the task and stays "
                            "recorded. But the gate is bound to THIS task, so a "
                            "replacement foreman starts a new task and proposes "
                            "its gates again; do not read this decision as "
                            "already spent on the work."
                        )
                        print(
                            f"  Revive the driver: helm foreman {project_id} "
                            "(replace a stale one first with helm worker stop "
                            "<worker-id> --reason \"...\")."
                        )
            return 0
        if args.command == "authority":
            if args.authority_command == "init":
                # Generated here, written 0600, and never printed: a capability
                # read out into a transcript has already left the machine.
                path = coordinator.configure_authority(secrets.token_urlsafe(48))
                print("This root now requires an authorization capability.")
                print(f"  Written to {path} (0600). Its value is never printed.")
                print(f'  Load it into your own shell: export {AUTHORITY_ENV}="$(cat {path})"')
                print(
                    "  Then remove the file if you like. No agent Helm starts can "
                    "inherit it: the worker environment is an allowlist."
                )
            else:
                configured = bool(coordinator._authority_hash())
                present = bool(os.environ.get(AUTHORITY_ENV))
                print(
                    "Protected commands here require a capability"
                    if configured
                    else "Protected commands here are guarded by session role only"
                )
                print(f"  capability configured: {'yes' if configured else 'no'}")
                print(f"  capability present in this session: {'yes' if present else 'no'}")
                if not configured:
                    print(
                        "  Set one up with helm authority init. Without it, a process "
                        "that is neither marked nor descended from a worker is treated "
                        "as the root."
                    )
            return 0
        if args.command == "review":
            outcome = HerdrAdapter(coordinator).run_review_cycle(
                args.task_id,
                reviewer_agent=args.reviewer_agent,
                reviewer_model=args.reviewer_model,
                rounds=args.rounds,
                timeout=args.timeout,
            )
            print(
                f"Review of {outcome['task_id']}: {outcome['verdict']} "
                f"(author={outcome['author_agent']} reviewer={outcome['reviewer_agent']} "
                f"independence={outcome['independence']})"
            )
            print(f"  {outcome['reviewer_reason']}")
            for entry in outcome["rounds"]:
                # Say where a verdict came from when it did not come the
                # normal way: a pane read is a recovery, not the record.
                recovered = (
                    "  (recovered from the reviewer's output; its report never reached Helm)"
                    if entry.get("source") == "output"
                    else ""
                )
                print(f"  round {entry['round']}: {entry['verdict']}{recovered}")
                if entry.get("text"):
                    print(f"    {entry['text'][:400]}")
            # Unresolved means an objection still stands; a human decides.
            return 0 if outcome["verdict"] == "approved" else 1

        if args.command == "inspect":
            _print_inspect(coordinator.inspect_task(args.task_id))
            return 0
        parser.error("unknown command")
    except (HelmError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"helm: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
