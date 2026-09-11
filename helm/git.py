"""Git, as Helm uses it: one runner, remote probes, and base resolution.

Lifted out of `core` unchanged. Every function here takes a path and returns
a value or raises; none of them knows about a Coordinator, a task or the
state store, which is why the whole cluster imports nothing from `helm.core`
and can sit below it.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .errors import HelmError
from .paths import canonical
from .values import now

class _StaleBaseResolution(Exception):
    """Internal signal: the project's base config changed during a fetch.

    Never surfaced to a caller directly -- `create_task` catches this and
    retries resolution against the project's current configuration, bounded
    so a genuinely racing writer cannot spin it forever.
    """


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "git command failed"
        raise HelmError(detail)
    return proc.stdout.strip()


def base_after_merges(cwd: Path, task: dict[str, Any], pinned: str) -> str:
    """The merge-base with the upstream base branch, when it is ahead of `pinned`.

    A branch that merges its base branch back in carries everything that merge
    brought with it, and all of it sits between the cut point and the tip -- so
    diffing from the cut point hands a reader the base branch's work as this
    branch's. Measured on one review: 514 files shown for a 25-file change, and
    the same two files from main returned as findings three rounds running.

    Empty when the merge-base is not ahead, and that restraint is the point.
    Helm cuts a task branch from the project's HEAD, which can already carry
    work nobody merged; there the merge-base sits BEHIND the cut point, and
    moving back to it would drag a stranger's commit into the diff -- the defect
    pinning a revision exists to prevent. Only a strict descendant is better.
    """
    branch = str(task.get("branch") or "").strip()
    if not branch or not pinned:
        return ""
    candidates = (
        str(task.get("base_upstream") or "").strip(),
        str(task.get("base_branch") or "").strip(),
    )
    for candidate in candidates:
        if not candidate:
            continue
        found = _git(cwd, "merge-base", candidate, branch, check=False).strip()
        if not found or found == pinned:
            continue
        if _git(cwd, "merge-base", pinned, found, check=False).strip() == pinned:
            return found
    return ""


def _git_root(path: Path) -> Path | None:
    result = _git(path, "rev-parse", "--show-toplevel", check=False)
    if not result:
        return None
    return canonical(result)


def _git_common_dir(path: Path) -> Path:
    result = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if not result:
        # Older Git versions do not support --path-format. A worktree's
        # common dir is still unambiguous once resolved from its cwd.
        result = _git(path, "rev-parse", "--git-common-dir")
        return canonical(path / result)
    return canonical(result)


def _has_head(path: Path) -> bool:
    return bool(_git(path, "rev-parse", "--verify", "HEAD", check=False))


def _bounded_ls_remote(
    root: Path, *args: str, timeout: float = 15
) -> subprocess.CompletedProcess[str] | None:
    """Run `git ls-remote` against one remote, bounded and noninteractive.

    Returns `None` when the probe itself could not complete at all -- a
    timeout, a missing `git`, a transport error before anything answered --
    so a caller can tell "the probe never got an answer" apart from "it
    answered no". `GIT_TERMINAL_PROMPT=0` keeps a missing credential from
    turning into a hang the timeout would otherwise still have to catch.
    Never fetches objects or touches the working tree.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(
            ["git", "-C", str(root), "ls-remote", *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=timeout, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _remote_symbolic_default(path: Path, remote: str) -> str | None:
    """Ask a remote which branch its own HEAD points to, read-only.

    `ls-remote --symref` lists refs; it never fetches objects or touches the
    working tree, so it is safe to run even for a remote nothing has been
    fetched from yet. Bounded so an unreachable remote cannot hang
    registration. Returns `None` on any failure, timeout, or a remote that
    has no answer (an empty remote reports no symref at all) -- the caller
    treats that the same as disagreement, never as permission to guess.
    """
    result = _bounded_ls_remote(path, "--symref", remote, "HEAD")
    if result is None or result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("ref:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
                return parts[1][len("refs/heads/"):]
    return None


def _remote_is_empty(root: Path, remote: str) -> bool | None:
    """Whether `remote` carries no branches at all -- a repository never pushed.

    Distinct from "does not have THIS branch", and the distinction decides
    whether a missing upstream is innocent. A remote with branches that lacks
    the configured one is a likely misconfiguration -- a typo in `base_branch`,
    or a branch renamed upstream -- and must keep failing loudly. A remote with
    nothing in it at all has simply never been pushed to, which is where every
    new project starts. `None` means the probe did not complete, and is never
    treated as either answer.
    """
    result = _bounded_ls_remote(root, "--heads", remote)
    if result is None or result.returncode != 0:
        return None
    return not result.stdout.strip()


def _remote_has_branch(root: Path, remote: str, branch: str) -> bool | None:
    """Whether `remote` has a branch named `branch`, read-only and bounded.

    `None` means the probe itself did not complete -- a timeout, a hung
    transport, a credential prompt refused non-interactively -- and is
    deliberately distinct from a clean "no such branch" answer (`False`). A
    caller must not treat the two the same: an unreachable remote that
    would have matched must not be silently skipped in favor of one that
    plainly does not have the branch, which is exactly as wrong as never
    checking at all.
    """
    result = _bounded_ls_remote(root, "--exit-code", "--heads", remote, branch)
    if result is None:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        # `--exit-code` reports 2 specifically for "no matching refs" -- a
        # clean, reachable "no", not a transport failure.
        return False
    return None


def _repository_default_branch(path: Path) -> str:
    """Resolve a repository's own default branch without hardcoding a name.

    A repository with no remote at all has only its own checkout to go by,
    so the branch actually checked out is the answer there. A repository
    with a remote is different: the checked-out branch is a feature or
    worker branch, not evidence of the project's base, so it is never used
    as a fallback once a remote exists. The remote's own default is read
    locally when something already recorded it (a `git clone` does; so does
    `git remote set-head <remote> -a`); when nothing was recorded, a single
    read-only `ls-remote --symref` query asks the remote directly without
    fetching or touching the checkout. Only when every remote that answered
    agrees is the result unambiguous; anything else -- no remote answered,
    or two disagreed -- must be configured explicitly rather than guessed.
    """
    remotes = [line for line in _git(path, "remote", check=False).splitlines() if line]
    if not remotes:
        current = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        if current:
            return current
        raise HelmError(
            "cannot resolve a default base branch: the checkout is detached and "
            'the project has no remote; set an explicit "base_branch" in '
            ".helm/project.json"
        )
    candidates: set[str] = set()
    for remote in remotes:
        symbolic = _git(
            path, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD", check=False
        )
        prefix = f"{remote}/"
        if symbolic.startswith(prefix):
            candidates.add(symbolic[len(prefix):])
            continue
        queried = _remote_symbolic_default(path, remote)
        if queried:
            candidates.add(queried)
    if len(candidates) == 1:
        return next(iter(candidates))
    # A remote exists but no single default could be determined from it. The
    # branch currently checked out is deliberately NOT used here: blessing
    # it silently is exactly the failure this setting exists to prevent --
    # a plain `git init` plus `remote add`, registered while a feature
    # branch happens to be checked out, must not record that feature as the
    # project's base.
    raise HelmError(
        "cannot resolve a default base branch: this project has a remote but no "
        "unambiguous default branch could be determined from it; set an "
        'explicit "base_branch" in .helm/project.json'
    )


def _resolve_base_branch(project_root: Path, settings: dict[str, Any]) -> str:
    """The project's configured base branch, or the repository's own default.

    An explicit `base_branch` in `.helm/project.json` always wins -- it is
    already validated as a usable branch name by `_discovery_settings`. Only
    a project that never named one falls through to repository inspection,
    and that inspection runs once, at registration; it is not re-guessed on
    every later discovery pass.
    """
    explicit = settings.get("base_branch")
    if explicit:
        return explicit
    return _repository_default_branch(project_root)


#: Git's own markers for an operation the user has not finished. Shared with
#: `helm doctor`, which warns about exactly the states this function refuses a
#: task base for -- two lists would eventually disagree about what
#: "mid-operation" means, and the disagreement would surface as a preflight
#: that called a checkout healthy and a task that then refused it.
CHECKOUT_OPERATION_MARKERS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "rebase-merge",
    "rebase-apply",
)


def _project_checkout_conflict(root: Path) -> str | None:
    """Describe a dirty or mid-operation project checkout, or None if clean.

    This reads the *project's own* working tree, not a task's future
    worktree -- Helm is about to pin a commit as a task's base, and a
    checkout with uncommitted changes to tracked files or an unresolved
    merge/rebase/cherry-pick is exactly the state where quietly trusting
    "whatever the ref says" hides work the user has not finished dealing
    with. Reporting it is the whole of the response: nothing here stashes,
    resets, or otherwise touches the checkout to make it look clean.

    Untracked files are deliberately not part of this check. An untracked
    `.helm/project.json`, a build artifact, or a local scratch file is
    ordinary and does not change what `refs/heads/<base_branch>` resolves
    to; treating every untracked file as a block would make Helm unusable
    for exactly the project layout its own settings file expects.
    """
    for marker in CHECKOUT_OPERATION_MARKERS:
        located = _git(root, "rev-parse", "--git-path", marker, check=False)
        if located and (root / located).exists():
            return f"an unresolved {marker.replace('_HEAD', '').replace('-', ' ').lower()} is in progress"
    dirty = _git(root, "status", "--porcelain=v1", "--untracked-files=no", check=False)
    if dirty:
        return "the checkout has uncommitted changes to tracked files"
    return None


def _advance_tracking_ref_without_rewinding(
    root: Path, ref: str, new_value: str, *, attempts: int = 3
) -> str | None:
    """Best-effort, race-safe advance of a remote-tracking ref to `new_value`.

    Never rewinds it: the ref is only ever moved when its current value is
    behind (a strict ancestor of) `new_value`, and the write itself is a
    compare-and-swap against the value just read
    (`git update-ref <ref> <new> <old>`), so a concurrent fetch that landed
    between the read and the write cannot be silently overwritten by an
    older snapshot -- the CAS simply fails and this retries against
    whatever is there now. If the current value is already equal to, ahead
    of, or diverged from `new_value`, the ref is left alone rather than
    guessed at.

    Returns `None` on success or when nothing needed to change, or a short
    description of why the ref was left alone otherwise. Never raises: a
    task's own resolved `base_revision` does not depend on this shared ref
    being current, only on the private fetch this is called after.
    """
    for _ in range(attempts):
        current = _git(root, "rev-parse", "--verify", "--quiet", ref, check=False)
        if current == new_value:
            return None
        if current:
            merge_base = _git(root, "merge-base", current, new_value, check=False)
            if merge_base != current:
                # `current` is not behind `new_value`: either a concurrent
                # fetch already advanced it past this one, or the two have
                # diverged. Neither case is this call's to resolve.
                return None
        result = subprocess.run(
            ["git", "-C", str(root), "update-ref", ref, new_value, current],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if result.returncode == 0:
            return None
        # Either the compare-and-swap lost a race between the read above
        # and this write, or update-ref failed outright (permissions, a
        # concurrent lock). Loop and re-evaluate against whatever is
        # actually there now rather than assuming which one happened.
    return (
        f"could not advance {ref} to {new_value}: update-ref did not "
        f"succeed within {attempts} attempts"
    )


def _delete_ref(root: Path, ref: str) -> str | None:
    """Delete a ref, reporting a failed deletion instead of leaving it unremoved."""
    result = subprocess.run(
        ["git", "-C", str(root), "update-ref", "-d", ref],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode == 0:
        return None
    detail = result.stderr.strip() or result.stdout.strip() or "update-ref -d failed"
    return f"could not delete temporary ref {ref}: {detail}"


def _resolve_task_base(
    root: Path, base_branch: str, *, fetch: bool, local_delivery: bool = False
) -> dict[str, Any]:
    """Resolve the immutable commit a new task's baseline is cut from.

    Reads refs directly and, when `fetch` is true, fetches a configured
    upstream -- it never switches, resets, rebases, merges, force-updates, or
    otherwise touches the user-owned project checkout. Call this before the
    task record is written, and call it outside Helm's state lock: a fetch is
    a network call, and that lock is what every worker's protocol message
    waits on next.

    `fetch` is false for worktreeless roles (foreman, reviewer): they produce
    no worktree and no commits of their own, so neither a network round trip
    nor the configured branch actually resolving is a precondition for them
    -- a renamed or deleted base branch must not stop a foreman from driving
    or a reviewer from reviewing an already-pinned author task.
    """
    ref = f"refs/heads/{base_branch}"
    if not fetch:
        local_sha = _git(root, "rev-parse", "--verify", "--quiet", ref, check=False)
        return {
            "base_branch": base_branch,
            "base_revision": local_sha or None,
            "base_source": (
                "local (fetch skipped for this task's role)"
                if local_sha
                else "unresolved (fetch skipped for this task's role; configured "
                "base branch does not currently resolve)"
            ),
            "base_upstream": None,
            "base_fetched": False,
            "base_resolved_at": now(),
            "base_notes": [],
        }
    local_sha = _git(root, "rev-parse", "--verify", "--quiet", ref, check=False)
    if not local_sha:
        raise HelmError(f"configured base branch does not exist in the project: {base_branch}")
    conflict = _project_checkout_conflict(root)
    if conflict:
        raise HelmError(
            f"refusing to start a task from a dirty project checkout: {conflict}. "
            "Resolve or commit it yourself -- Helm will not merge, rebase, reset, "
            "or discard anything to clear it."
        )
    remotes = [line for line in _git(root, "remote", check=False).splitlines() if line]
    upstream_short = _git(root, "for-each-ref", "--format=%(upstream:short)", ref, check=False)
    upstream_remote = _git(root, "for-each-ref", "--format=%(upstream:remotename)", ref, check=False)
    if upstream_short and upstream_remote and upstream_remote != ".":
        # The ordinary case: the branch already names its own upstream.
        remote = upstream_remote
        remote_branch = upstream_short[len(remote) + 1:]
        upstream_label = upstream_short
    elif remotes:
        # A remote exists but this branch was never told to track one.
        # Trusting the local tip here would be exactly the "unverified
        # local state passed off as fresh" this whole gate exists to
        # prevent -- so look for one unambiguous same-named branch across
        # the configured remotes instead, and fetch that. Each probe is a
        # bounded, read-only existence check (`ls-remote`), never a fetch
        # of objects -- and a remote that never answers at all is treated
        # as a blocker, not as "no match": an unreachable remote that
        # would have matched must not be silently skipped in favor of one
        # that plainly does not have the branch.
        matches: list[str] = []
        unreachable: list[str] = []
        for remote in remotes:
            has_branch = _remote_has_branch(root, remote, base_branch)
            if has_branch is True:
                matches.append(remote)
            elif has_branch is None:
                unreachable.append(remote)
        if unreachable:
            raise HelmError(
                f"base branch {base_branch} has no upstream configured, and checking "
                f"whether {', '.join(unreachable)} has a matching branch failed or "
                "timed out; Helm will not guess -- configure an upstream, an "
                "explicit base_branch, or make the remote reachable before "
                "starting a task"
            )
        if len(matches) == 1:
            remote = matches[0]
            remote_branch = base_branch
            upstream_label = f"{remote}/{remote_branch}"
        elif not matches:
            # Only when every remote is EMPTY, not merely missing this branch.
            # A populated remote without the configured branch is a likely
            # misconfiguration -- a typo, or a rename upstream -- and keeps
            # failing loudly. A remote with nothing in it has never been pushed
            # to, which is where every new project starts.
            if local_delivery and all(
                _remote_is_empty(root, remote) is True for remote in remotes
            ):
                # There is no upstream that could be fresher, so this is the
                # "genuinely local-only" case above reached by another route. A
                # new project that adds its remote BEFORE the first push landed
                # here, and refusing it made adding the remote strictly worse
                # than leaving it off: with no remote at all Helm proceeds on
                # the local tip, while naming the empty one blocked every task
                # on the project, including read-only discovery.
                return {
                    "base_branch": base_branch,
                    "base_revision": local_sha,
                    "base_source": "local (no remote carries this branch yet)",
                    "base_upstream": None,
                    "base_fetched": False,
                    "base_resolved_at": now(),
                    "base_notes": [
                        f"none of {', '.join(remotes)} has {base_branch} yet; accepted "
                        "because this project's delivery is local, so an unpushed "
                        "branch is its normal state"
                    ],
                }
            raise HelmError(
                f"base branch {base_branch} has no upstream configured, and none of "
                f"this project's remotes ({', '.join(remotes)}) have a branch named "
                f"{base_branch}; configure an upstream (git branch --set-upstream-to) "
                "or an explicit base_branch before starting a task -- Helm will not "
                "start one from an unverified local tip"
            )
        else:
            raise HelmError(
                f"base branch {base_branch} has no upstream configured, and "
                f"{len(matches)} of this project's remotes ({', '.join(matches)}) each "
                f"have a branch named {base_branch}; configure its upstream explicitly "
                "(git branch --set-upstream-to) so Helm knows which one to trust"
            )
    else:
        # Genuinely local-only: no remote exists to fall behind or diverge
        # from, so the local tip is the freshest answer there is.
        return {
            "base_branch": base_branch,
            "base_revision": local_sha,
            "base_source": "local-only (project has no remote)",
            "base_upstream": None,
            "base_fetched": False,
            "base_resolved_at": now(),
            "base_notes": [],
        }
    # Fetch the exact configured branch by name into a Helm-owned temporary
    # ref, rather than the remote's whole default refspec into the shared
    # `FETCH_HEAD`. Two things this avoids: a plain `git fetch <remote>`
    # still exits 0 when the remote's own fetch refspec happens to exclude
    # this branch, silently leaving a deleted or excluded upstream's stale
    # tracking ref perfectly resolvable with no sign it is wrong; and
    # `FETCH_HEAD` is one file per repository, so a concurrent fetch
    # elsewhere in the same checkout -- another task, another `git`
    # command a human runs by hand -- can overwrite it between this fetch
    # finishing and the read that follows. A unique ref this call alone
    # created cannot race with anything.
    temp_ref = f"refs/helm/base-fetch/{uuid.uuid4().hex}"
    notes: list[str] = []
    try:
        try:
            fetched = subprocess.run(
                ["git", "-C", str(root), "fetch", remote, f"+{remote_branch}:{temp_ref}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HelmError(
                f"refusing a stale base: fetching {remote} {remote_branch} for "
                f"{base_branch} failed: {exc}"
            ) from exc
        if fetched.returncode != 0:
            detail = fetched.stderr.strip() or fetched.stdout.strip() or "git fetch failed"
            raise HelmError(
                f"refusing a stale base: fetching {remote} {remote_branch} for "
                f"{base_branch} failed: {detail}"
            )
        # A successful fetch that changed nothing is still a successful
        # fetch: freshness is verified here, not measured by whether
        # anything moved.
        upstream_sha = _git(root, "rev-parse", "--verify", "--quiet", temp_ref, check=False)
        if not upstream_sha:
            raise HelmError(
                f"base branch {base_branch}'s upstream {upstream_label} did not "
                "resolve after fetch"
            )
        # Bring the conventional remote-tracking ref in line with what was
        # just verified, so anything that reads it later -- a rebase-drop
        # review fallback, a human running `git log origin/main` -- sees
        # the same fetched truth this task was pinned against, rather than
        # whatever the remote's own refspec would or would not have
        # touched. Race-safe and never a rewind: see
        # `_advance_tracking_ref_without_rewinding`. A failure here does
        # not fail task creation -- this task's own `base_revision` came
        # from the private ref above, not from this shared one -- but it
        # is recorded rather than silently swallowed.
        note = _advance_tracking_ref_without_rewinding(
            root, f"refs/remotes/{remote}/{remote_branch}", upstream_sha
        )
        if note:
            notes.append(note)
    finally:
        delete_note = _delete_ref(root, temp_ref)
        if delete_note:
            notes.append(delete_note)
    if local_sha == upstream_sha:
        return {
            "base_branch": base_branch,
            "base_revision": upstream_sha,
            "base_source": "upstream (equal)",
            "base_upstream": upstream_label,
            "base_fetched": True,
            "base_resolved_at": now(),
            "base_notes": notes,
        }
    merge_base = _git(root, "merge-base", local_sha, upstream_sha, check=False)
    if merge_base == local_sha:
        # The local branch is a strict ancestor of its upstream: someone else
        # advanced it and the local ref has simply not caught up. That is
        # exactly the staleness this fetch exists to catch.
        return {
            "base_branch": base_branch,
            "base_revision": upstream_sha,
            "base_source": "upstream (behind)",
            "base_upstream": upstream_label,
            "base_fetched": True,
            "base_resolved_at": now(),
            "base_notes": notes,
        }
    if merge_base == upstream_sha:
        if local_delivery:
            # Under local delivery, `helm task merge` fast-forwards into the
            # project's own checkout and nothing pushes. So a base branch ahead
            # of its upstream is not a mistake to reconcile -- it is what this
            # project looks like the moment any task lands, and refusing it
            # blocked every following task until a human pushed. That turned a
            # merge into a hidden precondition for the next piece of work, on
            # exactly the projects that chose not to push at all.
            #
            # Nothing is being guessed here: the local tip strictly CONTAINS
            # the upstream, so it is the newer of the two and a baseline cut
            # from it includes everything the upstream has. Divergence, where
            # the two really have contradicted each other, still refuses below.
            return {
                "base_branch": base_branch,
                "base_revision": local_sha,
                "base_source": "local (ahead of upstream; project delivers locally)",
                "base_upstream": upstream_label,
                "base_fetched": True,
                "base_resolved_at": now(),
                "base_notes": notes + [
                    f"local {base_branch} is ahead of {upstream_label}; accepted "
                    "because this project's delivery is local, so unpushed merges "
                    "are its normal state"
                ],
            }
        raise HelmError(
            f"base branch {base_branch} is ahead of its upstream {upstream_label}; "
            "push or reconcile it before starting a task -- Helm will not mix "
            "unmerged local commits into a task baseline"
        )
    raise HelmError(
        f"base branch {base_branch} has diverged from its upstream {upstream_label}; "
        "reconcile it before starting a task -- Helm will not guess which side is right"
    )
