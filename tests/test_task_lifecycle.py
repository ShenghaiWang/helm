"""Project discovery, task creation, base-branch resolution and worktree allocation."""

from __future__ import annotations

import contextlib
import io
import os
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

from helm import cli
from helm.core import (
    Coordinator,
    HelmError,
    SafetyError,
    StateStore,
    inside,
)
from helm.herdr import HerdrAdapter

from tests.support import FakeHerdr, HelmTestCase, REPO_ROOT, SHIPPED_DOMAINS


class TaskLifecycleTests(HelmTestCase):
    def _repo_on_branch(self, name: str, branch: str) -> Path:
        """Like `self.repo`, but the initial branch is named explicitly.

        Base-branch resolution must not assume a common name -- these tests
        need a repository provably not on `main` or `develop` to prove that.
        """
        root = Path(self.temp.name) / name
        root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", branch, str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
        (root / "README.txt").write_text(name)
        subprocess.run(["git", "-C", str(root), "add", "README.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
        self.repos.append(root)
        return root

    def _bare_remote(self, name: str, *, default_branch: str = "main") -> Path:
        """A bare 'remote' whose own HEAD symref is set explicitly.

        Never relies on the host's `init.defaultBranch`: a bare repo left to
        that default reports whatever the *machine* happens to be configured
        with, which does not necessarily match the branch this fixture's
        content actually lives on, and every test here must hold regardless
        of that setting.
        """
        bare = Path(self.temp.name) / f"{name}.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", default_branch, str(bare)], check=True
        )
        return bare

    def _tracked_repo(self, name: str, *, branch: str = "main") -> tuple[Path, Path]:
        """A local repo whose `branch` tracks a bare local 'remote'.

        No real network is involved: the "remote" is a bare repo on the same
        filesystem, added as `origin` and pushed once so the branch has a real
        upstream (`branch.<name>.remote`/`.merge`), exactly what
        `_resolve_task_base` looks for before it fetches.
        """
        root = self._repo_on_branch(name, branch)
        bare = self._bare_remote(f"{name}-remote", default_branch=branch)
        self._run_git(root, "remote", "add", "origin", str(bare))
        self._run_git(root, "push", "-q", "-u", "origin", branch)
        return root, bare

    @contextlib.contextmanager
    def store_task(self, task_id: str):
        with self.coordinator.store.locked() as data:
            yield data["tasks"][task_id]

    def test_non_git_initialization_requires_confirmation(self) -> None:
        root = self.repo("plain", non_git=True)
        with self.assertRaises(SafetyError):
            self.coordinator.register_project("plain", str(root), project_id="plain")
        with self.assertRaises(SafetyError):
            self.coordinator.register_project("plain", str(root), project_id="plain", init_git=True)
        project = self.coordinator.register_project(
            "plain", str(root), project_id="plain", init_git=True, confirm=True
        )
        self.assertEqual(project["id"], "plain")
        self.assertTrue((root / ".git").exists())

    def test_init_layout_preserves_existing_projects(self) -> None:
        root = Path(self.temp.name) / "new-helm"
        sentinel = root / "projects" / "keep-me.txt"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("existing")
        self.assertEqual(cli.main(["init", str(root)]), 0)
        self.assertEqual(sentinel.read_text(), "existing")
        self.assertTrue((root / "state").is_dir())
        self.assertTrue((root / "projects").is_dir())

    def test_auto_discovery_persists_project_defaults_and_reuses_record(self) -> None:
        helm_root = self._helm_root()
        project_root = self.repo("discovered")
        destination = helm_root / "projects" / "media"
        shutil.move(str(project_root), str(destination))
        settings = destination / ".helm"
        settings.mkdir()
        (settings / "project.json").write_text(
            json.dumps({"label": "Publishing", "color": "#123456", "delivery_policy": "pr"})
        )
        projects = self.coordinator.discover_projects(helm_root)
        self.assertEqual(len(projects), 1)
        project = projects[0]
        self.assertEqual(project["id"], "media")
        self.assertEqual(project["name"], "Publishing")
        self.assertEqual(project["color"], "#123456")
        self.assertEqual(project["delivery_policy"], "pr")
        self.assertTrue(project["discovered"])
        self.assertEqual(self.coordinator.discover_project(helm_root, "media")["created_at"], project["created_at"])

    def test_auto_discovery_non_git_shows_explicit_confirmation(self) -> None:
        helm_root = self._helm_root()
        project_root = helm_root / "projects" / "plain"
        project_root.mkdir()
        with self.assertRaisesRegex(SafetyError, r"helm project add plain .*--init-git --confirm"):
            self.coordinator.discover_projects(helm_root)
        self.assertFalse((project_root / ".git").exists())

    def test_discovered_nested_repository_is_rejected_as_not_isolated(self) -> None:
        helm_root = self._helm_root()
        project_root = self.repo("parent")
        destination = helm_root / "projects" / "nested"
        shutil.move(str(project_root), str(destination))
        child = destination / "child"
        child.mkdir()
        # Discovery is direct-child-only, while explicit registration must also
        # refuse a path that is inside another Git repository.
        with self.assertRaises(SafetyError):
            self.coordinator.register_project("child", str(child), project_id="child")

    def test_run_creates_task_and_worker_context_for_discovered_project(self) -> None:
        helm_root = self._helm_root()
        project_root = self.repo("run-project")
        destination = helm_root / "projects" / "media"
        shutil.move(str(project_root), str(destination))
        command = [
            sys.executable,
            "-c",
            (
                "import json, os; from pathlib import Path; "
                "context=json.loads(Path(os.environ['HELM_CONTEXT_FILE']).read_text()); "
                "print(json.dumps({'helm':1,'type':'result','text':context['task']['brief']}))"
            ),
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli.main(
                    [
                        "--root",
                        str(helm_root),
                        "run",
                        "media",
                        "Prepare the next artifact",
                        "--command",
                        shlex.join(command),
                        # `run` is async by default now; this case asserts the
                        # captured result, so it opts into blocking.
                        "--wait",
                    ]
                ),
                0,
            )
        state = StateStore(helm_root / "state").load()
        # `run` also appoints the project's foreman, so pick the work task
        # rather than whichever the record happens to hold first.
        task = next(t for t in state["tasks"].values() if t["role"] == "worker")
        worker = next(w for w in state["workers"].values() if w["task_id"] == task["id"])
        self.assertEqual(task["brief"], "Prepare the next artifact")
        self.assertEqual(task["project_id"], "media")
        self.assertEqual(worker["workspace"], task["workspace"])
        self.assertTrue(any(message["kind"] == "result" for message in state["messages"]))

    def test_first_helm_run_starts_the_worker_from_the_freshly_fetched_remote_tip(self) -> None:
        """The first `helm run` on a project fetches before the worker starts.

        Through the real CLI entry point rather than calling
        `Coordinator.create_task()` directly, so this guards the exact path
        a user invokes, not just the coordinator method underneath it.
        """
        helm_root = self._helm_root()
        root, bare = self._tracked_repo("clirun")
        destination = helm_root / "projects" / "clirun"
        shutil.move(str(root), str(destination))

        # Someone advances the remote directly, after the project directory
        # is in place but before Helm has ever looked at it.
        clone = Path(self.temp.name) / "clirun-clone"
        subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
        self._run_git(clone, "config", "user.name", "Someone Else")
        self._run_git(clone, "config", "user.email", "else@example.invalid")
        (clone / "theirs.txt").write_text("advanced\n", encoding="utf-8")
        self._run_git(clone, "add", "theirs.txt")
        self._run_git(clone, "commit", "-qm", "advance the remote before the first run")
        self._run_git(clone, "push", "-q", "origin", "main")
        advanced_tip = self._run_git(clone, "rev-parse", "main")
        self.assertNotEqual(self._run_git(destination, "rev-parse", "main"), advanced_tip)

        command = [
            sys.executable,
            "-c",
            (
                "import json, os; from pathlib import Path; "
                "context=json.loads(Path(os.environ['HELM_CONTEXT_FILE']).read_text()); "
                "print(json.dumps({'helm':1,'type':'result','text':context['task']['brief']}))"
            ),
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli.main(
                    [
                        "--root",
                        str(helm_root),
                        "run",
                        "clirun",
                        "catch up before starting",
                        "--command",
                        shlex.join(command),
                        "--wait",
                    ]
                ),
                0,
            )
        state = StateStore(helm_root / "state").load()
        task = next(t for t in state["tasks"].values() if t["role"] == "worker")
        self.assertEqual(task["base_revision"], advanced_tip)
        self.assertTrue(task["base_fetched"])
        workspace_head = self._run_git(Path(task["workspace"]), "rev-parse", "HEAD")
        self.assertEqual(workspace_head, advanced_tip)

    def test_a_ticket_goes_in_the_branch_name(self) -> None:
        """The one place a human reliably reads routing metadata.

        This root already approved the learning -- put the tracker id in the
        branch name -- and then could not act on it, because the branch was
        built from the task id alone before any ticket was known. TICKET-113 and
        TICKET-192 both shipped on `helm/<project>/<task-id>` with the ticket
        nowhere a reviewer would look. A learning nobody can comply with is
        worse than none: it reads as a closed loop.
        """
        root = self.repo("ticketed")
        project = self.coordinator.register_project(
            "Ticketed", str(root), project_id="ticketed"
        )
        task = self.coordinator.create_task(
            project["id"], "acknowledge the click", ticket="TICKET-192"
        )
        self.assertEqual(task["ticket"], "TICKET-192")
        self.assertEqual(task["branch"], f"helm/ticketed/TICKET-192-{task['id']}")
        self.assertTrue(
            task["workspace"].endswith(f"/worktrees/ticketed/TICKET-192-{task['id']}")
        )
        # And the branch git actually gets is the one recorded.
        allocated = self.coordinator.allocate_task(task["id"])
        self.assertEqual(allocated["branch"], task["branch"])
        self.assertEqual(allocated["workspace"], task["workspace"])
        head = subprocess.run(
            ["git", "-C", allocated["workspace"], "rev-parse", "--abbrev-ref", "HEAD"],
            check=True, text=True, stdout=subprocess.PIPE,
        ).stdout.strip()
        self.assertEqual(head, task["branch"])

        # Without one, nothing changes -- the ticket is optional.
        plain = self.coordinator.create_task(project["id"], "no ticket here")
        self.assertIsNone(plain["ticket"])
        self.assertEqual(plain["branch"], f"helm/ticketed/{plain['id']}")
        self.assertTrue(
            plain["workspace"].endswith(f"/worktrees/ticketed/{plain['id']}")
        )

        # A value git could not carry is refused at the point it is given,
        # not later as an unmappable git error.
        for bad in ("has space", "dots..inside", "trailing.", "why?"):
            with self.assertRaises(HelmError):
                self.coordinator.create_task(project["id"], "b", ticket=bad)

    def test_run_without_brief_returns_a_clear_conversational_prompt(self) -> None:
        helm_root = self._helm_root()
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), mock.patch(
            "sys.stdin", io.StringIO("")
        ):
            self.assertEqual(cli.main(["--root", str(helm_root), "run", "media"]), 2)
        self.assertIn("No task supplied", errors.getvalue())

    def test_project_isolation_and_assignment(self) -> None:
        first = self.repo("first")
        second = self.repo("second")
        p1 = self.coordinator.register_project("First", str(first), project_id="first")
        p2 = self.coordinator.register_project("Second", str(second), project_id="second")
        task = self.coordinator.create_task(p1["id"], "work only in first")
        allocated = self.coordinator.allocate_task(task["id"])
        workspace = Path(allocated["workspace"])
        self.assertEqual(workspace, self.coordinator.verify_task_workspace(task["id"]))
        self.assertNotEqual(workspace, first)
        self.assertNotEqual(workspace, second)
        context = self.coordinator._context(p1, task, "worker")
        self.assertEqual(context["project"]["id"], "first")
        self.assertNotIn("second", json.dumps(context))
        self.assertIn("worktrees", str(workspace))
        self.assertEqual(p2["color"], self.coordinator.list_projects()[1]["color"])

    def test_another_round_reuses_the_worktree_instead_of_cloning_again(self) -> None:
        """A revision is the same branch and directory as the change it revises.

        Minting a fresh task per round allocated a second checkout and left the
        new branch to be rebased onto whatever the first had become.
        """
        root = self.repo("rounds")
        project = self.coordinator.register_project("Rounds", str(root), project_id="rounds")
        task = self.coordinator.create_task(project["id"], "write the design")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        first_workspace = self.coordinator.inspect_task(task["id"])["task"]["workspace"]
        first_branch = task["branch"]
        self.coordinator.record_worker_message(
            worker["id"], "result", "done", requested_status="completed"
        )

        reopened = self.coordinator.continue_task(task["id"], "revise it for the review")

        self.assertEqual(reopened["workspace"], first_workspace)
        self.assertEqual(reopened["branch"], first_branch)
        self.assertEqual(reopened["brief"], "revise it for the review")
        # The first round's brief is kept rather than overwritten.
        self.assertEqual(reopened["rounds"][0]["brief"], "write the design")
        # And it can actually take another worker, which is the point.
        second = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        self.assertNotEqual(second["id"], worker["id"])
        self.assertEqual(
            self.coordinator.inspect_task(task["id"])["task"]["workspace"], first_workspace
        )

    def test_another_round_drops_an_approval_it_would_invalidate(self) -> None:
        """Approval is bound to the tree that was reviewed.

        Carrying it into a round that is about to edit that tree would let the
        next round inherit a human's agreement to something they never saw.
        """
        root = self.repo("roundapproval")
        project = self.coordinator.register_project(
            "RoundApproval", str(root), project_id="roundapproval"
        )
        task = self.coordinator.create_task(project["id"], "write it")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )
        self.commit_on_task_branch(task)
        self.coordinator.record_worker_message(
            worker["id"], "result", "done", requested_status="completed"
        )
        self.coordinator.approve_task(task["id"], "looks right")
        self.assertIsNotNone(self.coordinator.inspect_task(task["id"])["task"]["approval"])

        reopened = self.coordinator.continue_task(task["id"], "one more change")

        self.assertIsNone(reopened["approval"])

    def test_a_round_never_reopens_a_task_a_human_still_has_to_read(self) -> None:
        """Failed, blocked, and approval-needed need a person, not a retry."""
        root = self.repo("roundguard")
        project = self.coordinator.register_project(
            "RoundGuard", str(root), project_id="roundguard"
        )
        task = self.coordinator.create_task(project["id"], "write it")
        worker = self.coordinator.launch_worker(
            task["id"], [sys.executable, "-c", ""], wait=False
        )

        # Still running: a round started over the top of a live worker would
        # have two agents in one directory.
        with self.assertRaisesRegex(SafetyError, r"still running"):
            self.coordinator.continue_task(task["id"], "again")

        self.coordinator.record_worker_message(
            worker["id"], "failure", "it broke", requested_status="failed"
        )
        with self.assertRaisesRegex(HelmError, r"cannot take another round from status failed"):
            self.coordinator.continue_task(task["id"], "again")

    def test_allocation_populates_submodules_so_no_agent_writes_outside_its_worktree(
        self,
    ) -> None:
        # `git worktree add` leaves submodules empty, and initializing them
        # from inside the worktree writes module metadata into the MAIN
        # repository's .git -- outside the workspace a worker is confined to.
        # An agent that respected that boundary could not build, while one with
        # its permissions bypassed could, so whether a review verified anything
        # or only read the diff depended on which runtime it drew.
        inner = self.repo("dependency")
        root = self.repo("with-submodules")
        # Git blocks the file transport for submodules by default. A real
        # project's come over https; this only makes a local fixture possible,
        # and Helm's own command stays exactly what it is in production.
        subprocess.run(
            ["git", "-C", str(root), "-c", "protocol.file.allow=always",
             "submodule", "add", str(inner), "vendor"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "add submodule"], check=True)
        project = self.coordinator.register_project(
            "Subs", str(root), project_id="with-submodules"
        )
        task = self.coordinator.create_task(project["id"], "build something")

        with mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}):
            allocated = self.coordinator.allocate_task(task["id"])
        workspace = Path(allocated["workspace"])
        # Populated on arrival: the file the submodule carries is really there.
        self.assertTrue((workspace / "vendor" / "README.txt").exists())
        pending = subprocess.run(
            ["git", "-C", str(workspace), "submodule", "status"],
            check=True, text=True, stdout=subprocess.PIPE,
        ).stdout.splitlines()
        self.assertTrue(pending)
        self.assertFalse([line for line in pending if line.startswith("-")])

    def test_a_review_diffs_from_where_the_task_started_not_the_merge_base(self) -> None:
        """A review measures from the base resolved and pinned at creation.

        Whatever the base branch's tip happened to be when `create_task`
        resolved it -- here it coincides with the project's own checked-out
        HEAD, since nothing has switched it -- can already carry work nobody
        has merged, and the merge-base sits behind it, so a naive diff would
        pick up commits the author never wrote. That is not theoretical: the
        first review that actually ran spent its whole verdict on a
        stranger's offline-recording commit, two commits and fourteen files
        where the author wrote one and four. Helm records the resolved base
        commit at task creation; the review measures from there, not from
        whatever the checkout is sitting on by the time review runs.
        """
        root = self.repo("based")
        project = self.coordinator.register_project("Based", str(root), project_id="based")
        # A commit on the project's HEAD that the base branch does not have --
        # somebody else's unmerged work, exactly the shape that caused this.
        def git(*args: str) -> str:
            proc = subprocess.run(
                ["git", "-C", str(root), *args],
                check=True, text=True, stdout=subprocess.PIPE,
            )
            return proc.stdout.strip()

        before_stranger = git("rev-parse", "HEAD")
        (root / "theirs.txt").write_text("not mine\n", encoding="utf-8")
        git("add", "theirs.txt")
        git("commit", "-qm", "someone else's change")
        stranger = git("rev-parse", "HEAD")

        task = self.coordinator.create_task(project["id"], "my own change")
        self.coordinator.allocate_task(task["id"])
        self.assertEqual(task["base_revision"], stranger)
        self.commit_on_task_branch(task, "my own change")

        adapter = HerdrAdapter(self.coordinator, FakeHerdr())
        data = self.coordinator.store.load()
        base = adapter._review_target(
            data["projects"][project["id"]], data["tasks"][task["id"]]
        )
        self.assertEqual(
            base, stranger, "the review must start where this task started"
        )

        # A rebase drops the recorded base off the branch while leaving it a
        # perfectly resolvable object. Checking only that it resolves put 425
        # commits and 3,222 files into one review of a four-file change, so the
        # test has to be ancestry, not existence.
        branch = data["tasks"][task["id"]]["branch"]
        workspace = Path(data["tasks"][task["id"]]["workspace"])
        # Onto the commit BEFORE the stranger: upstream superseded that work,
        # so the rebase drops it and the recorded base leaves the branch.
        subprocess.run(
            ["git", "-C", str(workspace), "rebase", "-q", "--onto", before_stranger, stranger],
            check=True,
        )
        self.assertNotEqual(
            git("merge-base", stranger, branch), stranger,
            "precondition: the recorded base is no longer on the branch",
        )
        data = self.coordinator.store.load()
        rebased_base = adapter._review_target(
            data["projects"][project["id"]], data["tasks"][task["id"]]
        )
        self.assertNotEqual(
            rebased_base, stranger, "a base the rebase dropped must not be used"
        )
        self.assertEqual(
            git("rev-list", "--count", f"{rebased_base}..{branch}"), "1",
            "the review must still see exactly this task's own commit",
        )
        # And the range therefore holds exactly the author's own commit.
        self.assertEqual(
            git("rev-list", "--count", f"{base}..{data['tasks'][task['id']]['branch']}"),
            "1",
        )

    def test_configured_base_branch_wins_over_whatever_is_checked_out(self) -> None:
        """The resolved base is the branch Helm was told about, not the checkout.

        A repository's own default is captured once at registration -- here a
        branch named `trunk`, provably not `main` or `develop` -- and a task
        must still resolve against it even when the project's own working copy
        has since been switched to something else entirely.
        """
        root = self._repo_on_branch("trunked", "trunk")
        project = self.coordinator.register_project("Trunked", str(root), project_id="trunked")
        self.assertEqual(project["base_branch"], "trunk")
        trunk_tip = self._run_git(root, "rev-parse", "trunk")

        # The project's own checkout moves to a different branch with a
        # commit `trunk` never had -- exactly the "whatever HEAD happens to
        # be" state this setting exists to stop mattering.
        self._run_git(root, "checkout", "-qb", "scratch")
        (root / "scratch.txt").write_text("not the task's base\n", encoding="utf-8")
        self._run_git(root, "add", "scratch.txt")
        self._run_git(root, "commit", "-qm", "scratch work")
        self.assertNotEqual(self._run_git(root, "rev-parse", "HEAD"), trunk_tip)

        task = self.coordinator.create_task(project["id"], "work against trunk")
        self.assertEqual(task["base_branch"], "trunk")
        self.assertEqual(task["base_revision"], trunk_tip)

    def test_explicit_base_branch_setting_overrides_repository_default(self) -> None:
        """`.helm/project.json` naming `base_branch` always wins."""
        root = self._repo_on_branch("explicitbase", "main")
        self._run_git(root, "checkout", "-qb", "release/2027")
        (root / "release.txt").write_text("release line\n", encoding="utf-8")
        self._run_git(root, "add", "release.txt")
        self._run_git(root, "commit", "-qm", "release work")
        release_tip = self._run_git(root, "rev-parse", "release/2027")
        self._run_git(root, "checkout", "-q", "main")

        (root / ".helm").mkdir()
        (root / ".helm" / "project.json").write_text(
            json.dumps({"base_branch": "release/2027"})
        )
        self._run_git(root, "add", "-A")
        self._run_git(root, "commit", "-qm", "configure base branch")
        project = self.coordinator.register_project(
            "Explicit", str(root), project_id="explicitbase"
        )
        self.assertEqual(project["base_branch"], "release/2027")

        task = self.coordinator.create_task(project["id"], "ship the release line")
        self.assertEqual(task["base_branch"], "release/2027")
        self.assertEqual(task["base_revision"], release_tip)

    def test_invalid_base_branch_setting_is_rejected(self) -> None:
        root = self.repo("badsetting")
        (root / ".helm").mkdir()
        (root / ".helm" / "project.json").write_text(
            json.dumps({"base_branch": "bad..name"})
        )
        with self.assertRaises(HelmError):
            self.coordinator.register_project("Bad", str(root), project_id="badsetting")

    def test_missing_configured_base_branch_fails_safely_at_task_creation(self) -> None:
        """A format-valid but nonexistent branch fails where it is used, not silently."""
        root = self.repo("ghostbase")
        (root / ".helm").mkdir()
        (root / ".helm" / "project.json").write_text(
            json.dumps({"base_branch": "does-not-exist"})
        )
        project = self.coordinator.register_project(
            "Ghost", str(root), project_id="ghostbase"
        )
        self.assertEqual(project["base_branch"], "does-not-exist")
        with self.assertRaisesRegex(HelmError, "does not exist"):
            self.coordinator.create_task(project["id"], "work against nothing")

    def test_detached_checkout_with_no_remote_default_fails_safely(self) -> None:
        root = self.repo("detachedbase")
        head = self._run_git(root, "rev-parse", "HEAD")
        self._run_git(root, "checkout", "-q", "--detach", head)
        with self.assertRaisesRegex(HelmError, "detached"):
            self.coordinator.register_project("Detached", str(root), project_id="detachedbase")

    def test_remote_without_recorded_default_never_falls_back_to_the_checkout(self) -> None:
        """The reported bug, reproduced exactly: `git init`, then `remote add`.

        No fetch, no clone, no `remote set-head` -- so nothing has ever
        recorded what the remote's own default branch is. Registering here
        while a `feature` branch happens to be checked out must not record
        `feature` as the project's base; it must refuse instead.
        """
        root = self._repo_on_branch("noremotedefault", "feature")
        bare = self._bare_remote("noremotedefault-remote")  # empty: no push yet
        self._run_git(root, "remote", "add", "origin", str(bare))
        with self.assertRaisesRegex(HelmError, "no unambiguous default branch"):
            self.coordinator.register_project(
                "NoRemoteDefault", str(root), project_id="noremotedefault"
            )

    def test_disagreeing_remote_defaults_require_an_explicit_base_branch(self) -> None:
        """Two remotes with two different defaults is exactly as unusable as none."""
        root = self._repo_on_branch("tworemotes", "feature")
        origin = self._bare_remote("tworemotes-origin", default_branch="main")
        upstream = self._bare_remote("tworemotes-upstream", default_branch="develop")
        # Give each bare something to report a symref for.
        self._run_git(root, "remote", "add", "origin", str(origin))
        self._run_git(root, "push", "-q", "origin", "feature:main")
        self._run_git(root, "remote", "add", "upstream", str(upstream))
        self._run_git(root, "push", "-q", "upstream", "feature:develop")
        with self.assertRaisesRegex(HelmError, "no unambiguous default branch"):
            self.coordinator.register_project(
                "TwoRemotes", str(root), project_id="tworemotes"
            )

    def test_repository_default_discovered_live_when_not_locally_recorded(self) -> None:
        """No cached remote HEAD symref -- resolved by asking the remote directly.

        `_tracked_repo` pushes with plain `push -u`, which never sets
        `refs/remotes/origin/HEAD` locally, so this exercises the read-only
        `ls-remote --symref` fallback rather than the cached-ref fast path.
        """
        root, _bare = self._tracked_repo("livequery", branch="trunk")
        project = self.coordinator.register_project(
            "LiveQuery", str(root), project_id="livequery"
        )
        self.assertEqual(project["base_branch"], "trunk")

    def test_repository_default_prefers_an_unambiguous_remote_symbolic_head(self) -> None:
        """A locally recorded remote default outranks the current checkout.

        This never touches the network: `refs/remotes/origin/HEAD` is set the
        way a real `git clone` sets it, without contacting anything.
        """
        root = self._repo_on_branch("symbolicdefault", "feature")
        bare = self._bare_remote("symbolicdefault-remote")
        self._run_git(root, "remote", "add", "origin", str(bare))
        self._run_git(root, "push", "-q", "origin", "feature:main")
        self._run_git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

        project = self.coordinator.register_project(
            "SymbolicDefault", str(root), project_id="symbolicdefault"
        )
        self.assertEqual(project["base_branch"], "main")

    def test_local_only_branch_uses_local_tip_as_source(self) -> None:
        root = self.repo("localonly")
        project = self.coordinator.register_project(
            "LocalOnly", str(root), project_id="localonly"
        )
        local_tip = self._run_git(root, "rev-parse", "HEAD")
        task = self.coordinator.create_task(project["id"], "local work")
        self.assertEqual(task["base_revision"], local_tip)
        self.assertEqual(task["base_source"], "local-only (project has no remote)")
        self.assertFalse(task["base_fetched"])
        self.assertIsNone(task["base_upstream"])

    def test_successful_no_op_fetch_is_still_a_verified_fresh_base(self) -> None:
        """Nothing to fetch is not the same as skipping the fetch."""
        root, _bare = self._tracked_repo("noopfetch")
        project = self.coordinator.register_project(
            "NoOpFetch", str(root), project_id="noopfetch"
        )
        local_tip = self._run_git(root, "rev-parse", "main")
        task = self.coordinator.create_task(project["id"], "quiet remote")
        self.assertEqual(task["base_revision"], local_tip)
        self.assertTrue(task["base_fetched"])
        self.assertEqual(task["base_source"], "upstream (equal)")
        self.assertEqual(task["base_upstream"], "origin/main")

    def test_remote_advancement_is_fetched_and_used(self) -> None:
        """A remote that moved since the last fetch is picked up, not cached."""
        root, bare = self._tracked_repo("advances")
        project = self.coordinator.register_project(
            "Advances", str(root), project_id="advances"
        )
        stale_tip = self._run_git(root, "rev-parse", "main")

        # Someone else pushes directly to the shared remote. `root`'s own
        # remote-tracking ref is untouched until Helm fetches it.
        clone = Path(self.temp.name) / "advances-clone"
        subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
        self._run_git(clone, "config", "user.name", "Someone Else")
        self._run_git(clone, "config", "user.email", "else@example.invalid")
        (clone / "theirs.txt").write_text("advanced\n", encoding="utf-8")
        self._run_git(clone, "add", "theirs.txt")
        self._run_git(clone, "commit", "-qm", "advance the remote")
        self._run_git(clone, "push", "-q", "origin", "main")
        advanced_tip = self._run_git(clone, "rev-parse", "main")
        self.assertNotEqual(advanced_tip, stale_tip)
        self.assertEqual(self._run_git(root, "rev-parse", "main"), stale_tip)

        task = self.coordinator.create_task(project["id"], "catch up to the remote")
        self.assertEqual(task["base_revision"], advanced_tip)
        self.assertEqual(task["base_source"], "upstream (behind)")
        self.assertTrue(task["base_fetched"])

    def test_fetch_failure_blocks_rather_than_using_cached_state(self) -> None:
        root, _bare = self._tracked_repo("brokenremote")
        project = self.coordinator.register_project(
            "BrokenRemote", str(root), project_id="brokenremote"
        )
        self._run_git(
            root, "remote", "set-url", "origin",
            str(Path(self.temp.name) / "no-such-remote.git"),
        )
        with self.assertRaisesRegex(HelmError, "refusing a stale base"):
            self.coordinator.create_task(project["id"], "work with no remote")
        self.assertEqual(self.coordinator.store.load()["tasks"], {})

    def test_local_branch_ahead_of_upstream_blocks_a_pr_project(self) -> None:
        """Under PR delivery, unpushed local commits are a real hazard.

        The branch is meant to reach a remote, so a baseline cut from commits
        no remote has seen is a task built on a foundation the reviewer cannot
        fetch.
        """
        root, _bare = self._tracked_repo("aheadlocal")
        project = self.coordinator.register_project(
            "AheadLocal", str(root), project_id="aheadlocal", delivery_policy="pr"
        )
        (root / "unpushed.txt").write_text("mine, not pushed\n", encoding="utf-8")
        self._run_git(root, "add", "unpushed.txt")
        self._run_git(root, "commit", "-qm", "local-only work")
        with self.assertRaisesRegex(HelmError, "ahead of its upstream"):
            self.coordinator.create_task(project["id"], "work on top of unpushed history")

    def test_local_delivery_may_start_a_task_on_its_own_unpushed_merge(self) -> None:
        """Being ahead is the normal state of a project that never pushes.

        `helm task merge` fast-forwards into the project's own checkout and
        nothing pushes, so the first merge leaves main ahead of its upstream --
        and refusing that blocked every following task until a human pushed by
        hand. A merge became a hidden precondition for the next piece of work,
        on exactly the projects that chose not to push at all.

        Nothing is guessed: the local tip strictly contains the upstream, so it
        is the newer of the two. Genuine divergence still refuses.
        """
        root, _bare = self._tracked_repo("aheadlocaldelivery")
        project = self.coordinator.register_project(
            "AheadLocalDelivery", str(root), project_id="aheadlocaldelivery"
        )
        self.assertEqual(project["delivery_policy"], "local")
        (root / "merged-locally.txt").write_text("delivered, not pushed\n", encoding="utf-8")
        self._run_git(root, "add", "merged-locally.txt")
        self._run_git(root, "commit", "-qm", "a task merged locally")
        local_tip = self._run_git(root, "rev-parse", "HEAD").strip()

        task = self.coordinator.create_task(project["id"], "the next piece of work")

        self.assertEqual(task["base_revision"], local_tip)
        self.assertIn("ahead of upstream", task["base_source"])
        self.assertTrue(task["base_fetched"])

    def test_local_branch_diverged_from_upstream_blocks(self) -> None:
        root, bare = self._tracked_repo("divergedlocal")
        project = self.coordinator.register_project(
            "DivergedLocal", str(root), project_id="divergedlocal"
        )
        clone = Path(self.temp.name) / "divergedlocal-clone"
        subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
        self._run_git(clone, "config", "user.name", "Someone Else")
        self._run_git(clone, "config", "user.email", "else@example.invalid")
        (clone / "theirs.txt").write_text("their side\n", encoding="utf-8")
        self._run_git(clone, "add", "theirs.txt")
        self._run_git(clone, "commit", "-qm", "their commit")
        self._run_git(clone, "push", "-q", "origin", "main")

        (root / "mine.txt").write_text("my side\n", encoding="utf-8")
        self._run_git(root, "add", "mine.txt")
        self._run_git(root, "commit", "-qm", "my commit")

        with self.assertRaisesRegex(HelmError, "diverged"):
            self.coordinator.create_task(project["id"], "reconcile me")

    def test_worktreeless_roles_never_fetch(self) -> None:
        """A foreman task must not pay -- or fail on -- a network round trip."""
        root, _bare = self._tracked_repo("noforemanfetch")
        project = self.coordinator.register_project(
            "NoForemanFetch", str(root), project_id="noforemanfetch"
        )
        # Break the remote so a fetch, if attempted, would fail loudly.
        self._run_git(
            root, "remote", "set-url", "origin",
            str(Path(self.temp.name) / "no-such-remote.git"),
        )
        task = self.coordinator.create_foreman_task(project["id"])
        self.assertEqual(task["role"], "foreman")
        self.assertFalse(task["base_fetched"])
        self.assertEqual(task["base_source"], "local (fetch skipped for this task's role)")

    def test_worktreeless_role_survives_a_missing_base_branch(self) -> None:
        """A true worktreeless bypass does not depend on the base resolving.

        Neither a foreman nor a reviewer produces a worktree of its own, so a
        renamed or deleted configured base branch -- not just an unreachable
        remote -- must not stop either from starting.
        """
        root = self.repo("noforemanbaseref")
        (root / ".helm").mkdir()
        (root / ".helm" / "project.json").write_text(
            json.dumps({"base_branch": "renamed-away"})
        )
        self._run_git(root, "add", "-A")
        self._run_git(root, "commit", "-qm", "configure a base branch that will vanish")
        project = self.coordinator.register_project(
            "NoForemanBaseRef", str(root), project_id="noforemanbaseref"
        )
        self.assertEqual(project["base_branch"], "renamed-away")
        # The branch never existed in the first place -- the same shape as a
        # rename or deletion after registration.
        task = self.coordinator.create_foreman_task(project["id"])
        self.assertEqual(task["role"], "foreman")
        self.assertIsNone(task["base_revision"])
        self.assertFalse(task["base_fetched"])
        self.assertIn("does not currently resolve", task["base_source"])
        # allocate_task must not need the base to resolve either -- a
        # worktreeless role gets a private directory, never a git worktree.
        allocated = self.coordinator.allocate_task(task["id"])
        self.assertEqual(allocated["status"], "allocated")

    def test_fetch_rejects_a_deleted_upstream_branch_rather_than_a_stale_cached_ref(self) -> None:
        """A plain, non-pruning fetch would leave a deleted branch resolvable.

        Fetching the exact configured branch by name instead makes the
        deletion a hard failure, exactly as it is for a real `git fetch
        <remote> <branch>` against a branch the remote no longer has.
        """
        root, bare = self._tracked_repo("deletedupstream")
        project = self.coordinator.register_project(
            "DeletedUpstream", str(root), project_id="deletedupstream"
        )
        self._run_git(bare, "branch", "-D", "main")
        with self.assertRaisesRegex(HelmError, "refusing a stale base"):
            self.coordinator.create_task(project["id"], "work against a vanished branch")

    def test_fetch_resolution_ignores_a_concurrently_overwritten_fetch_head(self) -> None:
        """The fetched SHA comes from a private ref, not the shared FETCH_HEAD.

        `FETCH_HEAD` is one file per repository; a concurrent fetch
        elsewhere in the same checkout -- another task, a human running
        `git fetch` by hand -- can overwrite it between this fetch
        finishing and a read that followed it. Resolution must not depend
        on that file's contents at all.
        """
        root, _bare = self._tracked_repo("fetchheadrace")
        project = self.coordinator.register_project(
            "FetchHeadRace", str(root), project_id="fetchheadrace"
        )
        expected = self._run_git(root, "rev-parse", "main")
        real_run = subprocess.run

        def poison_fetch_head_after_fetch(cmd, *args, **kwargs):
            result = real_run(cmd, *args, **kwargs)
            if isinstance(cmd, list) and "fetch" in cmd:
                fetch_head = root / ".git" / "FETCH_HEAD"
                fetch_head.write_text(
                    "0" * 40 + "\t\tbranch 'poison' of nowhere\n", encoding="utf-8"
                )
            return result

        with mock.patch("subprocess.run", side_effect=poison_fetch_head_after_fetch):
            task = self.coordinator.create_task(
                project["id"], "resolve despite a poisoned FETCH_HEAD"
            )
        self.assertEqual(task["base_revision"], expected)

    def test_fetch_refreshes_the_tracking_ref_even_when_the_remote_fetchspec_excludes_it(
        self,
    ) -> None:
        """The conventional tracking ref stays honest even past an excluding refspec.

        A plain `git fetch <remote>` obeying `remote.<name>.fetch` can leave
        `refs/remotes/<remote>/<branch>` stale forever once that refspec
        excludes the branch. Exact, by-name fetch avoids trusting that ref
        for the read that matters here, but the ref itself must still end
        up correct for anything that reads it afterward, such as a
        review's rebase-drop fallback.
        """
        root, bare = self._tracked_repo("excludedrefspec")
        self._run_git(root, "config", "--unset-all", "remote.origin.fetch")
        self._run_git(
            root, "config", "--add", "remote.origin.fetch",
            "+refs/heads/never-matches:refs/remotes/origin/never-matches",
        )
        clone = Path(self.temp.name) / "excludedrefspec-clone"
        subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
        self._run_git(clone, "config", "user.name", "Someone Else")
        self._run_git(clone, "config", "user.email", "else@example.invalid")
        (clone / "theirs.txt").write_text("advanced\n", encoding="utf-8")
        self._run_git(clone, "add", "theirs.txt")
        self._run_git(clone, "commit", "-qm", "advance the remote")
        self._run_git(clone, "push", "-q", "origin", "main")
        advanced_tip = self._run_git(clone, "rev-parse", "main")

        project = self.coordinator.register_project(
            "ExcludedRefspec", str(root), project_id="excludedrefspec"
        )
        task = self.coordinator.create_task(
            project["id"], "verify despite an excluding remote fetchspec"
        )
        self.assertEqual(task["base_revision"], advanced_tip)
        # The exact bug this guards against: a plain `git fetch origin`
        # here would succeed -- nothing in its configured refspec failed --
        # while leaving this ref exactly where it started.
        self.assertEqual(
            self._run_git(root, "rev-parse", "refs/remotes/origin/main"), advanced_tip
        )
        # No scratch ref left behind for a review or a human to trip over.
        leftover = self._run_git(root, "for-each-ref", "refs/helm/base-fetch")
        self.assertEqual(leftover, "")

    def test_untracked_branch_matching_is_bounded_and_blocks_on_an_unreachable_remote(
        self,
    ) -> None:
        """The no-upstream matching probe must not be able to hang the first `helm run`.

        Unlike the configured-upstream fetch path, this one runs `ls-remote`
        against every remote before a single fetch happens; each probe must
        be bounded the same way the fetch itself is, and a remote that
        never answers here must not resolve as "no match" -- that would be
        exactly as wrong as never checking it at all.
        """
        root, _bare = self._tracked_repo("hungmatch")
        self._run_git(root, "branch", "--unset-upstream", "main")
        project = self.coordinator.register_project(
            "HungMatch", str(root), project_id="hungmatch"
        )
        real_run = subprocess.run

        def hang_matching_probe(cmd, *args, **kwargs):
            if isinstance(cmd, list) and "ls-remote" in cmd and "--heads" in cmd:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout") or 15)
            return real_run(cmd, *args, **kwargs)

        start = time.monotonic()
        with mock.patch("subprocess.run", side_effect=hang_matching_probe):
            with self.assertRaises(HelmError):
                self.coordinator.create_task(
                    project["id"], "must not hang on an unreachable remote"
                )
        elapsed = time.monotonic() - start
        # The mock raises immediately rather than actually sleeping, so this
        # is really asserting there is no retry loop that would multiply a
        # per-call timeout into something unbounded.
        self.assertLess(elapsed, 5)
        self.assertEqual(self.coordinator.store.load()["tasks"], {})

    def test_tracking_ref_advance_never_rewinds_a_concurrent_newer_fetch(self) -> None:
        """A concurrent writer that landed a newer commit first is never undone.

        Simulates another Helm task or a human `git fetch` advancing the
        shared tracking ref in the gap between this task's own private
        fetch completing and its attempt to refresh that ref.
        """
        root, _bare = self._tracked_repo("norewind")
        project = self.coordinator.register_project(
            "NoRewind", str(root), project_id="norewind"
        )
        expected_tip = self._run_git(root, "rev-parse", "main")
        real_run = subprocess.run
        race: dict[str, str] = {}

        def race_after_private_fetch(cmd, *args, **kwargs):
            result = real_run(cmd, *args, **kwargs)
            if (
                isinstance(cmd, list)
                and "fetch" in cmd
                and any("refs/helm/base-fetch/" in str(item) for item in cmd)
            ):
                tree = self._run_git(root, "rev-parse", f"{expected_tip}^{{tree}}")
                concurrent_tip = self._run_git(
                    root, "commit-tree", tree, "-p", expected_tip,
                    "-m", "a concurrent fetch landed first",
                )
                self._run_git(root, "update-ref", "refs/remotes/origin/main", concurrent_tip)
                race["concurrent_tip"] = concurrent_tip
            return result

        with mock.patch("subprocess.run", side_effect=race_after_private_fetch):
            task = self.coordinator.create_task(
                project["id"], "must not rewind the race winner"
            )
        self.assertIn("concurrent_tip", race)
        self.assertEqual(task["base_revision"], expected_tip)
        # The shared ref stays at the concurrent, newer value -- this
        # task's own resolution never rewinds it to what it fetched.
        self.assertEqual(
            self._run_git(root, "rev-parse", "refs/remotes/origin/main"),
            race["concurrent_tip"],
        )
        self.assertEqual(task["base_notes"], [])

    def test_tracking_ref_update_failure_is_reported_not_silently_swallowed(self) -> None:
        root, bare = self._tracked_repo("updatefails")
        project = self.coordinator.register_project(
            "UpdateFails", str(root), project_id="updatefails"
        )
        # Exclude the branch from the remote's own default fetch refspec,
        # exactly as in the excluded-fetchspec case above -- otherwise
        # git's own fetch quietly advances refs/remotes/origin/main as a
        # side effect before Helm's own update-ref call ever runs, and
        # "already correct" would short-circuit before reaching it.
        self._run_git(root, "config", "--unset-all", "remote.origin.fetch")
        self._run_git(
            root, "config", "--add", "remote.origin.fetch",
            "+refs/heads/never-matches:refs/remotes/origin/never-matches",
        )
        clone = Path(self.temp.name) / "updatefails-clone"
        subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
        self._run_git(clone, "config", "user.name", "Someone Else")
        self._run_git(clone, "config", "user.email", "else@example.invalid")
        (clone / "theirs.txt").write_text("advanced\n", encoding="utf-8")
        self._run_git(clone, "add", "theirs.txt")
        self._run_git(clone, "commit", "-qm", "advance the remote")
        self._run_git(clone, "push", "-q", "origin", "main")
        expected = self._run_git(clone, "rev-parse", "main")
        real_run = subprocess.run

        def fail_tracking_update(cmd, *args, **kwargs):
            if (
                isinstance(cmd, list)
                and "update-ref" in cmd
                and "-d" not in cmd
                and "refs/remotes/origin/main" in cmd
            ):
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="fatal: cannot lock ref"
                )
            return real_run(cmd, *args, **kwargs)

        with mock.patch("subprocess.run", side_effect=fail_tracking_update):
            task = self.coordinator.create_task(
                project["id"], "survive a failed tracking-ref update"
            )
        # The task's own base is unaffected: it came from the private
        # fetch ref, not from the shared one this update tried to refresh.
        self.assertEqual(task["base_revision"], expected)
        self.assertTrue(
            any("could not advance" in note for note in task["base_notes"]),
            task["base_notes"],
        )

    def test_temp_ref_cleanup_failure_is_reported_not_silently_leaked(self) -> None:
        root, _bare = self._tracked_repo("cleanupfails")
        project = self.coordinator.register_project(
            "CleanupFails", str(root), project_id="cleanupfails"
        )
        expected = self._run_git(root, "rev-parse", "main")
        real_run = subprocess.run

        def fail_temp_ref_delete(cmd, *args, **kwargs):
            if (
                isinstance(cmd, list)
                and "update-ref" in cmd
                and "-d" in cmd
                and any("refs/helm/base-fetch/" in str(item) for item in cmd)
            ):
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="",
                    stderr="fatal: cannot lock ref for deletion",
                )
            return real_run(cmd, *args, **kwargs)

        with mock.patch("subprocess.run", side_effect=fail_temp_ref_delete):
            task = self.coordinator.create_task(
                project["id"], "survive a failed temporary-ref cleanup"
            )
        self.assertEqual(task["base_revision"], expected)
        self.assertTrue(
            any("could not delete temporary ref" in note for note in task["base_notes"]),
            task["base_notes"],
        )

    def test_remote_exists_but_branch_untracked_still_fetches_the_unambiguous_matching_branch(
        self,
    ) -> None:
        """No upstream configured is not permission to skip verification.

        Exactly one remote has a branch named the same as the configured
        base, so Helm fetches it and applies the same freshness comparison
        as a tracked branch -- it does not fall back to an unverified local
        tip just because `branch.<name>.merge` was never set.
        """
        root, bare = self._tracked_repo("untracked")
        stale_tip = self._run_git(root, "rev-parse", "main")
        # Someone advances the remote directly...
        clone = Path(self.temp.name) / "untracked-clone"
        subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
        self._run_git(clone, "config", "user.name", "Someone Else")
        self._run_git(clone, "config", "user.email", "else@example.invalid")
        (clone / "theirs.txt").write_text("advanced\n", encoding="utf-8")
        self._run_git(clone, "add", "theirs.txt")
        self._run_git(clone, "commit", "-qm", "advance the remote")
        self._run_git(clone, "push", "-q", "origin", "main")
        advanced_tip = self._run_git(clone, "rev-parse", "main")
        # ...and this branch's own tracking configuration is removed --
        # the exact shape that once let a real, related remote go
        # unchecked because nothing named it as an upstream.
        self._run_git(root, "branch", "--unset-upstream", "main")

        project = self.coordinator.register_project(
            "Untracked", str(root), project_id="untracked"
        )
        task = self.coordinator.create_task(project["id"], "catch up despite no tracking config")
        self.assertTrue(task["base_fetched"])
        self.assertEqual(task["base_source"], "upstream (behind)")
        self.assertEqual(task["base_upstream"], "origin/main")
        self.assertEqual(task["base_revision"], advanced_tip)
        self.assertNotEqual(task["base_revision"], stale_tip)

    def test_untracked_branch_blocks_when_no_remote_has_a_matching_branch_name(self) -> None:
        root = self._repo_on_branch("nomatch", "main")
        bare = self._bare_remote("nomatch-remote", default_branch="trunk")
        # Seed the remote under a name that never matches this project's
        # base branch, from an unrelated clone -- root's own "main" is
        # never derived from it.
        seed = Path(self.temp.name) / "nomatch-seed"
        subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True)
        self._run_git(seed, "config", "user.name", "Seed")
        self._run_git(seed, "config", "user.email", "seed@example.invalid")
        (seed / "seed.txt").write_text("seed\n", encoding="utf-8")
        self._run_git(seed, "add", "seed.txt")
        self._run_git(seed, "commit", "-qm", "seed the remote")
        self._run_git(seed, "push", "-q", "origin", "trunk")
        self._run_git(root, "remote", "add", "origin", str(bare))

        # Pin `base_branch` explicitly to "main" -- otherwise repository-
        # default resolution would itself pick up "trunk" from the remote,
        # which would not exercise the case this test is about.
        (root / ".helm").mkdir()
        (root / ".helm" / "project.json").write_text(json.dumps({"base_branch": "main"}))
        self._run_git(root, "add", "-A")
        self._run_git(root, "commit", "-qm", "configure base branch")
        project = self.coordinator.register_project(
            "NoMatch", str(root), project_id="nomatch"
        )
        with self.assertRaisesRegex(HelmError, "none of this project's remotes"):
            self.coordinator.create_task(project["id"], "work with nothing to verify against")

    def test_untracked_branch_blocks_when_multiple_remotes_have_a_matching_branch_name(
        self,
    ) -> None:
        root = self._repo_on_branch("ambiguousmatch", "main")
        origin = self._bare_remote("ambiguousmatch-origin", default_branch="main")
        upstream = self._bare_remote("ambiguousmatch-upstream", default_branch="main")
        self._run_git(root, "remote", "add", "origin", str(origin))
        self._run_git(root, "push", "-q", "origin", "main")
        self._run_git(root, "remote", "add", "upstream", str(upstream))
        self._run_git(root, "push", "-q", "upstream", "main")
        project = self.coordinator.register_project(
            "AmbiguousMatch", str(root), project_id="ambiguousmatch"
        )
        with self.assertRaisesRegex(HelmError, "each have a branch named"):
            self.coordinator.create_task(project["id"], "work with two candidates")

    def test_dirty_project_checkout_blocks_task_creation(self) -> None:
        root = self.repo("dirtycheckout")
        project = self.coordinator.register_project(
            "DirtyCheckout", str(root), project_id="dirtycheckout"
        )
        # An uncommitted edit to a *tracked* file -- an untracked scratch
        # file (a build artifact, an uncommitted `.helm/project.json`) is
        # deliberately not what this gate blocks on.
        (root / "README.txt").write_text("uncommitted edit\n", encoding="utf-8")
        with self.assertRaisesRegex(HelmError, "dirty project checkout"):
            self.coordinator.create_task(project["id"], "work while the checkout is dirty")
        self.assertEqual(self.coordinator.store.load()["tasks"], {})

    def test_untracked_files_in_the_project_checkout_do_not_block_task_creation(self) -> None:
        """An uncommitted `.helm/project.json` is the expected shape, not dirt."""
        root = self.repo("untrackedscratch")
        (root / ".helm").mkdir()
        (root / ".helm" / "project.json").write_text(json.dumps({"label": "Scratch"}))
        (root / "build-artifact.tmp").write_text("not tracked\n", encoding="utf-8")
        project = self.coordinator.register_project(
            "UntrackedScratch", str(root), project_id="untrackedscratch"
        )
        task = self.coordinator.create_task(project["id"], "work despite untracked files")
        self.assertEqual(task["status"], "created")

    def test_unresolved_merge_in_project_checkout_blocks_task_creation(self) -> None:
        root = self._repo_on_branch("unresolvedmerge", "main")
        self._run_git(root, "checkout", "-qb", "feature")
        (root / "README.txt").write_text("feature-side change\n", encoding="utf-8")
        self._run_git(root, "add", "README.txt")
        self._run_git(root, "commit", "-qm", "feature-side change")
        self._run_git(root, "checkout", "-q", "main")
        (root / "README.txt").write_text("main-side change\n", encoding="utf-8")
        self._run_git(root, "add", "README.txt")
        self._run_git(root, "commit", "-qm", "main-side change")
        # Both branches touch the same file differently: a real conflict,
        # not an auto-mergeable pair of unrelated changes.
        subprocess.run(
            ["git", "-C", str(root), "merge", "-q", "--no-ff", "feature"], check=False,
        )
        # A real conflict leaves MERGE_HEAD set even after the attempt
        # fails, exactly the mid-operation state this gate detects.
        self.assertTrue((root / ".git" / "MERGE_HEAD").exists())
        project = self.coordinator.register_project(
            "UnresolvedMerge", str(root), project_id="unresolvedmerge"
        )
        with self.assertRaisesRegex(HelmError, "dirty project checkout"):
            self.coordinator.create_task(project["id"], "work mid-conflict")

    def test_project_head_movement_after_creation_does_not_change_the_baseline(self) -> None:
        """Nothing that moves the project's own checkout may move the task.

        `allocate_task` must build the worktree from the commit `create_task`
        pinned, never from a fresh read of HEAD -- otherwise a commit landed
        on the project between those two calls silently becomes part of every
        task's baseline.
        """
        root = self.repo("moves")
        project = self.coordinator.register_project("Moves", str(root), project_id="moves")
        task = self.coordinator.create_task(project["id"], "pin me before anything moves")
        pinned = task["base_revision"]

        # The project's own checkout advances after the task was created but
        # before it is allocated.
        (root / "after.txt").write_text("landed after task creation\n", encoding="utf-8")
        self._run_git(root, "add", "after.txt")
        self._run_git(root, "commit", "-qm", "advance the project after task creation")
        moved_head = self._run_git(root, "rev-parse", "HEAD")
        self.assertNotEqual(moved_head, pinned)

        allocated = self.coordinator.allocate_task(task["id"])
        self.assertEqual(allocated["base_revision"], pinned)
        workspace_head = self._run_git(Path(allocated["workspace"]), "rev-parse", "HEAD")
        self.assertEqual(workspace_head, pinned)
        self.assertNotEqual(workspace_head, moved_head)

    def test_allocate_task_rejects_a_base_revision_that_no_longer_resolves(self) -> None:
        root = self.repo("goneref")
        project = self.coordinator.register_project("GoneRef", str(root), project_id="goneref")
        task = self.coordinator.create_task(project["id"], "work")
        with self.store_task(task["id"]) as record:
            record["base_revision"] = "0" * 40
        with self.assertRaisesRegex(HelmError, "no longer resolves"):
            self.coordinator.allocate_task(task["id"])

    def test_task_outcome_diffs_from_the_pinned_base_revision_not_the_moved_branch(self) -> None:
        """The reported commits/diffstat use `base_revision`, not the movable branch name."""
        root = self.repo("outcomepinned")
        project = self.coordinator.register_project(
            "OutcomePinned", str(root), project_id="outcomepinned"
        )
        task = self.coordinator.create_task(project["id"], "pin the outcome diff")
        pinned = task["base_revision"]
        self.coordinator.allocate_task(task["id"])
        self.commit_on_task_branch(task, "worker's own change")

        # The project's own branch advances after allocation -- exactly the
        # shape that once let commits nobody on this task wrote leak into a
        # reported diff or commit list.
        (root / "after.txt").write_text("landed after allocation\n", encoding="utf-8")
        self._run_git(root, "add", "after.txt")
        self._run_git(root, "commit", "-qm", "advance the project after allocation")
        moved_tip = self._run_git(root, "rev-parse", "HEAD")
        self.assertNotEqual(moved_tip, pinned)

        outcome = self.coordinator.task_outcome(task["id"])
        self.assertEqual(outcome["base_revision"], pinned)
        self.assertEqual(len(outcome["commits"]), 1)
        self.assertIn("worker's own change", "\n".join(outcome["commits"]))
        diff_text = "\n".join(outcome["diffstat"])
        self.assertIn("change.txt", diff_text)
        self.assertNotIn("after.txt", diff_text)

    def test_cli_outcome_prints_the_pinned_revision_in_the_full_diff_command(self) -> None:
        """The `helm task outcome` full-diff line must be copy-pasteable and correct.

        Printing `base_branch` there would tell a user to diff against a
        branch that may have moved since the task started -- the same
        defect `task_outcome()`'s own commits/diffstat fix already avoids,
        just one hop further out where a human actually runs the command.
        """
        root = self.repo("clioutcome")
        project = self.coordinator.register_project(
            "CliOutcome", str(root), project_id="clioutcome"
        )
        task = self.coordinator.create_task(project["id"], "pin the printed diff command")
        pinned = task["base_revision"]
        self.coordinator.allocate_task(task["id"])
        self.commit_on_task_branch(task)

        # The project's own branch advances after allocation.
        (root / "after.txt").write_text("landed after allocation\n", encoding="utf-8")
        self._run_git(root, "add", "after.txt")
        self._run_git(root, "commit", "-qm", "advance the project after allocation")
        moved_tip = self._run_git(root, "rev-parse", "HEAD")
        self.assertNotEqual(moved_tip, pinned)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                cli.main(
                    ["--state-dir", str(self.coordinator.store.directory), "task", "outcome", task["id"]]
                ),
                0,
            )
        printed = output.getvalue()
        self.assertIn(f"diff {pinned}...HEAD", printed)
        self.assertNotIn(f"diff {task['base_branch']}...HEAD", printed)
        self.assertNotIn(f"diff {moved_tip}...HEAD", printed)

    def test_review_target_fallback_uses_the_recorded_upstream_not_origin(self) -> None:
        """A dropped pinned base falls back to the recorded upstream -- not a
        guessed `origin/<branch>` that may not even exist for this project.
        """
        root = self._repo_on_branch("customremote", "main")
        bare = self._bare_remote("customremote-remote", default_branch="main")
        self._run_git(root, "remote", "add", "upstream", str(bare))
        self._run_git(root, "push", "-q", "-u", "upstream", "main")
        # There is no remote named `origin` anywhere in this project -- the
        # old hardcoded fallback would resolve against nothing.
        self.assertEqual(self._run_git(root, "remote"), "upstream")

        project = self.coordinator.register_project(
            "CustomRemote", str(root), project_id="customremote"
        )
        task = self.coordinator.create_task(project["id"], "work with a non-origin remote")
        self.assertEqual(task["base_upstream"], "upstream/main")
        self.coordinator.allocate_task(task["id"])
        self.commit_on_task_branch(task)

        # Simulate the pinned base having been dropped (a real rebase or a
        # remote history rewrite both produce this): a parentless commit
        # sharing no ancestry with anything is never an ancestor of the task
        # branch, exactly what a dropped base looks like to the ancestry
        # check, without needing to actually rewrite history here.
        dangling = self._run_git(
            root, "commit-tree", self._run_git(root, "rev-parse", "HEAD^{tree}"),
            "-m", "unrelated, parentless commit",
        )
        with self.store_task(task["id"]) as record:
            record["base_revision"] = dangling

        adapter = HerdrAdapter(self.coordinator, FakeHerdr())
        data = self.coordinator.store.load()
        base = adapter._review_target(
            data["projects"][project["id"]], data["tasks"][task["id"]]
        )
        expected = self._run_git(
            root, "merge-base", "upstream/main", data["tasks"][task["id"]]["branch"]
        )
        self.assertEqual(base, expected)

    def test_concurrent_base_branch_change_during_resolution_forces_a_retry(self) -> None:
        """The project's own configuration changing mid-resolution is not trusted silently.

        Phase 2 (the fetch/comparison) runs outside Helm's state lock, so
        another writer could edit the project's `base_branch` while it runs.
        Committing the task against the configuration that no longer applies
        would be a silent correctness bug; retrying against the current one
        is the only safe response.
        """
        import helm.core as helm_core_module

        root = self.repo("racybase")
        project = self.coordinator.register_project(
            "RacyBase", str(root), project_id="racybase"
        )
        self._run_git(root, "branch", "other")
        original = helm_core_module._resolve_task_base
        calls = {"count": 0}

        def racing(root_arg, base_branch, *, fetch, local_delivery=False):
            calls["count"] += 1
            if calls["count"] == 1:
                with self.coordinator.store.locked() as data:
                    data["projects"]["racybase"]["base_branch"] = "other"
            return original(
                root_arg, base_branch, fetch=fetch, local_delivery=local_delivery
            )

        with mock.patch("helm.core._resolve_task_base", side_effect=racing):
            task = self.coordinator.create_task(
                project["id"], "survive a racing config change"
            )
        self.assertEqual(calls["count"], 2)
        self.assertEqual(task["base_branch"], "other")

    def test_fetch_timeout_blocks_rather_than_hanging(self) -> None:
        root, _bare = self._tracked_repo("timeoutfetch")
        project = self.coordinator.register_project(
            "TimeoutFetch", str(root), project_id="timeoutfetch"
        )
        real_run = subprocess.run

        def selective_timeout(cmd, *args, **kwargs):
            if isinstance(cmd, list) and "fetch" in cmd:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout") or 120)
            return real_run(cmd, *args, **kwargs)

        with mock.patch("subprocess.run", side_effect=selective_timeout):
            with self.assertRaisesRegex(HelmError, "refusing a stale base"):
                self.coordinator.create_task(project["id"], "work while the remote hangs")
        self.assertEqual(self.coordinator.store.load()["tasks"], {})

    def test_local_delivery_starts_when_the_remote_has_no_such_branch_yet(self) -> None:
        """Naming an empty remote must not be worse than having none.

        With no remote at all Helm proceeds on the local tip. A brand-new
        project that adds its origin BEFORE the first push has a remote that
        carries no branch -- and refusing that blocked every task on the
        project, including read-only discovery, so adding the remote was
        strictly worse than leaving it off. There is no upstream to be fresher
        than the local tip, which is the same situation the local-only branch
        already accepts.
        """
        root = self.repo("unpushedremote")
        empty_remote = Path(self.temp.name) / "empty-remote.git"
        self._run_git(Path(self.temp.name), "init", "-q", "--bare", str(empty_remote))
        # Registered first, then the remote added -- the real sequence, and the
        # reason the project's base branch is already known.
        project = self.coordinator.register_project(
            "UnpushedRemote", str(root), project_id="unpushedremote"
        )
        self._run_git(root, "remote", "add", "origin", str(empty_remote))
        self.assertEqual(project["delivery_policy"], "local")
        local_tip = self._run_git(root, "rev-parse", "HEAD").strip()

        task = self.coordinator.create_task(project["id"], "the first piece of work")

        self.assertEqual(task["base_revision"], local_tip)
        self.assertIn("no remote carries this branch", task["base_source"])

    def test_a_populated_remote_missing_the_branch_still_blocks(self) -> None:
        """Empty and "missing this branch" are different, and only one is innocent.

        A remote that carries other branches but not the configured one is a
        likely misconfiguration -- a typo in base_branch, or a rename upstream
        -- and must keep failing loudly even under local delivery. Only a
        remote with nothing in it at all has simply never been pushed to.
        """
        root = self.repo("populatedremote")
        bare = Path(self.temp.name) / "populated-remote.git"
        self._run_git(Path(self.temp.name), "init", "-q", "--bare", str(bare))
        seed = Path(self.temp.name) / "seed-populated"
        self._run_git(Path(self.temp.name), "clone", "-q", str(bare), str(seed))
        self._run_git(seed, "config", "user.email", "seed@example.invalid")
        self._run_git(seed, "checkout", "-q", "-b", "trunk")
        (seed / "seed.txt").write_text("seed\n", encoding="utf-8")
        self._run_git(seed, "add", "seed.txt")
        self._run_git(seed, "commit", "-qm", "seed the remote")
        self._run_git(seed, "push", "-q", "origin", "trunk")

        project = self.coordinator.register_project(
            "PopulatedRemote", str(root), project_id="populatedremote"
        )
        self._run_git(root, "remote", "add", "origin", str(bare))

        with self.assertRaisesRegex(HelmError, "none of this project's remotes"):
            self.coordinator.create_task(project["id"], "work against a mismatched remote")
