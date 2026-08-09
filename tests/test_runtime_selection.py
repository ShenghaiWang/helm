"""Choosing and launching the agent runtime and model for a task."""

from __future__ import annotations

import contextlib
import io
import os
import json
import shutil
import sys
from pathlib import Path
from unittest import mock

from helm import cli, runtimes
from helm.core import (
    Coordinator,
    HelmError,
    StateStore,
    worker_environment,
)

from tests.support import FakeHerdr, HelmTestCase, REPO_ROOT, SHIPPED_DOMAINS


class RuntimeSelectionTests(HelmTestCase):
    def _fake_agent_cli(self, *names: str) -> Path:
        """Put stub agent executables on PATH so runtime checks are hermetic."""
        bin_dir = Path(self.temp.name) / f"bin-{'-'.join(names)}"
        bin_dir.mkdir(exist_ok=True)
        for name in names:
            executable = bin_dir / name
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
        return bin_dir

    def _runtime_root(self, project_id: str, settings: dict[str, object] | None = None) -> Path:
        helm_root = self._helm_root(f"helm-{project_id}")
        project_root = self.repo(f"repo-{project_id}")
        destination = helm_root / "projects" / project_id
        shutil.move(str(project_root), str(destination))
        if settings is not None:
            (destination / ".helm").mkdir()
            (destination / ".helm" / "project.json").write_text(json.dumps(settings))
        return helm_root

    def test_agent_selection_validates_availability_and_records_reason(self) -> None:
        helm_root = self._helm_root()
        project_root = self.repo("agents")
        destination = helm_root / "projects" / "agents"
        shutil.move(str(project_root), str(destination))
        settings = destination / ".helm"
        settings.mkdir()
        (settings / "project.json").write_text(json.dumps({"domains": ["publishing"]}))
        profiles = {
            "agents": [
                {"id": "bad", "command": ["definitely-not-installed-helm-agent"], "domains": ["publishing"]},
                {
                    "id": "publishing-editor",
                    "command": [sys.executable, "-c", ""],
                    "domains": ["publishing"],
                    "capabilities": ["shorts"],
                },
                {"id": "generic", "command": [sys.executable, "-c", ""], "capacity": 2},
            ]
        }
        (helm_root / "agents.json").write_text(json.dumps(profiles))
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "agents")
        automatic = coordinator.create_task(project["id"], "Prepare the next artifact")
        worker = coordinator.launch_worker(automatic["id"], None)
        self.assertEqual(worker["agent_id"], "publishing-editor")
        self.assertIn("domain match", worker["agent_reason"])
        explicit = coordinator.create_task(project["id"], "Prepare the next artifact", agent="generic")
        selected = coordinator.launch_worker(explicit["id"], None)
        self.assertEqual(selected["agent_id"], "generic")
        self.assertIn("explicit", selected["agent_reason"])
        unavailable = coordinator.create_task(project["id"], "another task", agent="bad")
        with self.assertRaisesRegex(HelmError, r"bad is unavailable"):
            coordinator.launch_worker(unavailable["id"], None)

    def test_pi_runtime_approves_project_files_in_both_launch_modes(self) -> None:
        runtime = runtimes.builtin_runtime("pi")
        assert runtime is not None

        prompt = "assignment prompt"
        self.assertEqual(
            runtime.command(interactive=True),
            ["pi", "--approve", runtimes.PROMPT_PLACEHOLDER],
        )
        self.assertEqual(
            runtime.command(interactive=False),
            ["pi", "--approve", "--print", runtimes.PROMPT_PLACEHOLDER],
        )
        # Model insertion remains before the approval/print flags, and prompt
        # substitution still leaves the prompt as the final argument.
        self.assertEqual(
            runtime.with_model("pi/model", interactive=True),
            ["pi", "--model", "pi/model", "--approve", runtimes.PROMPT_PLACEHOLDER],
        )
        self.assertEqual(
            runtimes.apply_prompt(
                runtime.with_model("pi/model", interactive=False), prompt
            ),
            ["pi", "--model", "pi/model", "--approve", "--print", prompt],
        )

    def test_opencode_runtime_auto_approves_and_keeps_the_prompt_last(self) -> None:
        """A reviewer that cannot report is worse than no reviewer.

        opencode has no --add-dir analogue, so the flag that matters is
        `--auto`: without it the worker stops on a permission prompt nobody is
        watching. With it, writes outside the working directory succeed, which
        is what `helm worker message` needs to reach Helm's state.
        """
        runtime = runtimes.builtin_runtime("opencode")
        assert runtime is not None

        prompt = "assignment prompt"
        self.assertEqual(
            runtime.command(interactive=True),
            ["opencode", "--auto", "--prompt", runtimes.PROMPT_PLACEHOLDER],
        )
        self.assertEqual(
            runtime.command(interactive=False),
            ["opencode", "run", "--auto", runtimes.PROMPT_PLACEHOLDER],
        )
        # `with_model` inserts directly after argv[0], which puts --model
        # ahead of the `run` subcommand. opencode accepts it in that position;
        # the prompt still has to end up last so nothing swallows it.
        self.assertEqual(
            runtimes.apply_prompt(
                runtime.with_model("openrouter/~anthropic/claude-opus-latest", interactive=False),
                prompt,
            ),
            [
                "opencode",
                "--model",
                "openrouter/~anthropic/claude-opus-latest",
                "run",
                "--auto",
                prompt,
            ],
        )

    def test_worker_runtime_defaults_to_the_agent_this_helm_session_runs_under(self) -> None:
        helm_root = self._runtime_root("session")
        bin_dir = self._fake_agent_cli("claude")
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "session")
        task = coordinator.create_task(project["id"], "Draft the release note")
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CLAUDECODE": "1",
            "HELM_AGENT": "",
        }
        with mock.patch.dict(os.environ, env):
            worker = coordinator.prepare_external_worker(task["id"], None, execution="herdr")
        self.assertEqual(worker["agent_id"], "claude")
        self.assertIn("same runtime as this Helm session", worker["agent_reason"])
        # Interactive form in a pane, and the assignment reaches an agent CLI
        # as a prompt rather than as an unread environment variable.
        self.assertEqual(Path(worker["command"][0]).name, "claude")
        self.assertNotIn("--print", worker["command"])
        self.assertIn(worker["context_file"], worker["command"][-1])
        self.assertIn("Draft the release note", worker["command"][-1])

    def test_process_fallback_starts_a_runtime_in_its_non_interactive_form(self) -> None:
        helm_root = self._runtime_root("fallback")
        bin_dir = self._fake_agent_cli("claude")
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "fallback")
        task = coordinator.create_task(project["id"], "Draft the release note")
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CLAUDECODE": "1",
            "HELM_AGENT": "",
        }
        with mock.patch.dict(os.environ, env):
            worker = coordinator.prepare_external_worker(task["id"], None, execution="process")
        # Without a terminal a full-screen TUI would only write escape noise
        # into the log, so the same runtime is started in print mode.
        self.assertIn("--print", worker["command"])

    def test_a_task_runs_on_the_model_it_was_given(self) -> None:
        """Choosing a runtime is not choosing a model.

        Knowing which model suits a task is worth nothing if there is no way to
        say it, so the model has to survive all the way into the argv the
        worker is actually started with.
        """
        helm_root = self._runtime_root("modelled")
        bin_dir = self._fake_agent_cli("claude")
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "modelled")
        task = coordinator.create_task(
            project["id"], "Classify these tickets", model="claude-haiku-4-5"
        )
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CLAUDECODE": "1",
            "HELM_AGENT": "",
            "HELM_MODEL": "",
        }
        with mock.patch.dict(os.environ, env):
            worker = coordinator.prepare_external_worker(task["id"], None, execution="herdr")
        command = worker["command"]
        # Immediately after the executable, so a variadic option later in the
        # argv cannot swallow it, and the prompt still ends up last.
        self.assertEqual(command[1:3], ["--model", "claude-haiku-4-5"])
        self.assertIn("Classify these tickets", command[-1])
        self.assertIn("task names model claude-haiku-4-5", worker["agent_reason"])

    def test_model_resolution_prefers_the_task_then_the_project_then_the_root(self) -> None:
        """Same precedence as the runtime rules: stated beats inferred.

        There is deliberately no detection step. A wrong runtime guess fails
        loudly on a missing executable; a wrong model guess runs, bills, and
        answers, so the last resort is to say nothing at all.
        """
        helm_root = self._runtime_root("layered", {"agent": "claude", "model": "claude-sonnet-5"})
        bin_dir = self._fake_agent_cli("claude")
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "layered")
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CLAUDECODE": "1",
            "HELM_AGENT": "",
            "HELM_MODEL": "claude-opus-5",
        }

        # The project's pin outranks the root default.
        pinned = coordinator.create_task(project["id"], "Refactor the client")
        with mock.patch.dict(os.environ, env):
            worker = coordinator.prepare_external_worker(pinned["id"], None, execution="herdr")
        self.assertEqual(worker["command"][1:3], ["--model", "claude-sonnet-5"])

        # The task's own choice outranks both.
        named = coordinator.create_task(
            project["id"], "Port the parser", model="claude-fable-5"
        )
        with mock.patch.dict(os.environ, env):
            worker = coordinator.prepare_external_worker(named["id"], None, execution="herdr")
        self.assertEqual(worker["command"][1:3], ["--model", "claude-fable-5"])

        # With nothing stated anywhere, Helm sends no model and the runtime
        # keeps its own default rather than Helm guessing one.
        bare_root = self._runtime_root("bare", {"agent": "claude"})
        bare = Coordinator(StateStore(bare_root / "state", helm_root=bare_root))
        bare_project = bare.discover_project(bare_root, "bare")
        quiet = bare.create_task(bare_project["id"], "Tidy the changelog")
        with mock.patch.dict(os.environ, {**env, "HELM_MODEL": ""}):
            worker = bare.prepare_external_worker(quiet["id"], None, execution="herdr")
        self.assertNotIn("--model", worker["command"])

    def test_a_model_is_refused_rather_than_dropped_when_helm_cannot_place_it(self) -> None:
        """A custom command has no model flag Helm knows.

        Dropping it silently would leave the coordinator believing it had
        instructed a model it never sent, and the bill is the only place that
        difference would ever show up.
        """
        helm_root = self._runtime_root("custom")
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "custom")
        task = coordinator.create_task(
            project["id"], "Summarise the log", model="claude-haiku-4-5"
        )
        coordinator.allocate_task(task["id"])
        with self.assertRaisesRegex(HelmError, r"only built-in runtimes publish a model flag"):
            coordinator.prepare_external_worker(
                task["id"], ["/bin/echo", "{prompt}"], execution="process"
            )

    def test_a_project_pins_its_own_runtime_over_the_session_default(self) -> None:
        helm_root = self._runtime_root("pinned", {"agent": "codex"})
        bin_dir = self._fake_agent_cli("codex", "claude", "pi")
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "pinned")
        self.assertEqual(project["agent"], "codex")
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CLAUDECODE": "1",
            "HELM_AGENT": "",
        }
        task = coordinator.create_task(project["id"], "Prepare the migration")
        with mock.patch.dict(os.environ, env):
            worker = coordinator.prepare_external_worker(task["id"], None, execution="herdr")
        self.assertEqual(worker["agent_id"], "codex")
        self.assertIn("pins agent codex", worker["agent_reason"])
        # An agent named for this one task still outranks the project's pin.
        explicit = coordinator.create_task(project["id"], "Prepare the migration", agent="pi")
        with mock.patch.dict(os.environ, env):
            chosen = coordinator.prepare_external_worker(explicit["id"], None, execution="herdr")
        self.assertEqual(chosen["agent_id"], "pi")
        self.assertIn("explicit", chosen["agent_reason"])

    def test_launch_time_agent_override_may_name_a_built_in_runtime(self) -> None:
        helm_root = self._runtime_root("launch-agent")
        bin_dir = self._fake_agent_cli("pi")
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "launch-agent")
        task = coordinator.create_task(project["id"], "Prepare the migration")
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HELM_AGENT": ""}
        with mock.patch.dict(os.environ, env):
            worker = coordinator.prepare_external_worker(
                task["id"], None, execution="herdr", agent="pi"
            )
        self.assertEqual(worker["agent_id"], "pi")
        self.assertIn("explicit", worker["agent_reason"])

    def test_unknown_and_unavailable_runtimes_fail_with_the_known_agent_list(self) -> None:
        helm_root = self._runtime_root("unknown")
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "unknown")
        task = coordinator.create_task(project["id"], "Do the thing", agent="not-an-agent")
        with self.assertRaisesRegex(HelmError, r"unknown agent: not-an-agent.*claude.*codex.*pi"):
            coordinator.prepare_external_worker(task["id"], None)
        # A known runtime that is not installed is unavailable, never invented.
        missing = coordinator.create_task(project["id"], "Do the thing", agent="codex")
        with mock.patch.dict(os.environ, {"PATH": str(Path(self.temp.name) / "empty-bin")}):
            with self.assertRaisesRegex(HelmError, r"codex is unavailable"):
                coordinator.prepare_external_worker(missing["id"], None)
        # Nothing pinned, nothing configured, nothing detectable: Helm asks
        # instead of guessing a provider command.
        undetectable = coordinator.create_task(project["id"], "Do the thing")
        with mock.patch.dict(os.environ, {"HELM_AGENT": "none"}):
            with self.assertRaisesRegex(HelmError, r"no worker runtime is available"):
                coordinator.prepare_external_worker(undetectable["id"], None)

    def test_a_profile_may_inherit_a_built_in_runtime_by_name(self) -> None:
        helm_root = self._runtime_root("profiles", {"domains": ["publishing"]})
        bin_dir = self._fake_agent_cli("codex", "pi")
        (helm_root / "agents.json").write_text(json.dumps({
            "agents": [
                {"id": "shorts", "runtime": "codex", "domains": ["publishing"]},
                {"id": "pi", "capacity": 2},
            ]
        }))
        (helm_root / "domains" / "publishing").mkdir(parents=True)
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "profiles")
        task = coordinator.create_task(project["id"], "Prepare the next artifact")
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HELM_AGENT": ""}
        with mock.patch.dict(os.environ, env):
            worker = coordinator.prepare_external_worker(task["id"], None, execution="herdr")
        self.assertEqual(worker["agent_id"], "shorts")
        self.assertEqual(Path(worker["command"][0]).name, "codex")
        # A profile whose id is a runtime id needs no command either.
        named = coordinator.create_task(project["id"], "Prepare the next artifact", agent="pi")
        with mock.patch.dict(os.environ, env):
            chosen = coordinator.prepare_external_worker(named["id"], None, execution="herdr")
        self.assertEqual(Path(chosen["command"][0]).name, "pi")

    def test_runtime_credentials_pass_through_without_widening_the_scrub(self) -> None:
        helm_root = self._runtime_root("creds", {"agent": "claude"})
        bin_dir = self._fake_agent_cli("claude")
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "creds")
        task = coordinator.create_task(project["id"], "Draft the note")
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "ANTHROPIC_API_KEY": "sk-test-key",
            "UNRELATED_SECRET": "do-not-forward",
            "HELM_AGENT": "",
        }
        with mock.patch.dict(os.environ, env):
            worker = coordinator.prepare_external_worker(task["id"], None, execution="herdr")
        config = json.loads(Path(worker["config_file"]).read_text())
        self.assertEqual(config["worker_env"]["ANTHROPIC_API_KEY"], "sk-test-key")
        self.assertNotIn("UNRELATED_SECRET", config["worker_env"])
        # The scrub itself is untouched: only the runtime's declared names are
        # added back, for this one assignment.
        self.assertNotIn("UNRELATED_SECRET", worker_environment(os.environ | env))
        self.assertNotIn("ANTHROPIC_API_KEY", worker_environment(os.environ | env))

    def test_agent_check_reports_built_in_runtimes_when_nothing_is_configured(self) -> None:
        helm_root = self._runtime_root("builtins")
        bin_dir = self._fake_agent_cli("claude")
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        # Only the stub is on PATH, so availability reflects this machine
        # rather than whatever the developer happens to have installed.
        env = {"PATH": str(bin_dir), "CLAUDECODE": "1"}
        with mock.patch.dict(os.environ, env):
            report = {entry["id"]: entry for entry in coordinator.agent_availability()}
        self.assertEqual(set(report), {"claude", "codex", "pi", "opencode", "omp"})
        self.assertTrue(report["claude"]["available"])
        self.assertTrue(report["claude"]["detected"])
        self.assertFalse(report["codex"]["available"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.dict(os.environ, env):
            self.assertEqual(cli.main(["--root", str(helm_root), "agent", "check"]), 0)
        self.assertIn("claude (Claude Code) [built-in, available] <- this session", output.getvalue())

    def test_agent_check_reports_herdr_integration_inventory_without_paths(self) -> None:
        helm_root = self._runtime_root("herdr-integrations")
        bin_dir = self._fake_agent_cli("claude")
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        statuses = runtimes.parse_herdr_integration_status(
            "claude: current (v7) (/Users/test/.claude/hooks/herdr-agent-state.sh)\n"
            "copilot: current (v2) (/Users/test/.copilot/hooks/herdr-agent-state.sh)\n"
            "kimi: not installed (/Users/test/.kimi-code/hooks/herdr-agent-state.sh)\n"
        )
        self.assertEqual(statuses["claude"], "current")
        self.assertEqual(statuses["copilot"], "current")
        self.assertEqual(statuses["kimi"], "not installed")
        self.assertNotIn("/Users/test", " ".join(statuses.values()))

        env = {"PATH": str(bin_dir), "CLAUDECODE": "1"}
        with mock.patch.dict(os.environ, env), mock.patch(
            "helm.runtimes.herdr_integration_status", return_value=statuses
        ):
            report = {entry["id"]: entry for entry in coordinator.agent_availability()}
        self.assertEqual(report["claude"]["herdr_integration"], "current")
        self.assertTrue(report["claude"]["herdr_integrated"])

        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.dict(os.environ, env), mock.patch(
            "helm.runtimes.herdr_integration_status", return_value=statuses
        ):
            self.assertEqual(cli.main(["--root", str(helm_root), "agent", "check"]), 0)
        text = output.getvalue()
        self.assertIn("claude (Claude Code) [built-in, available, herdr=current]", text)
        self.assertIn("Herdr integrations not in Helm's built-ins: copilot (Herdr-only)", text)
        self.assertNotIn("/Users/test", text)

    def test_a_reviewer_is_never_the_agent_that_wrote_the_code(self) -> None:
        bin_dir = self._fake_agent_cli("claude", "codex")
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
            # Two runtimes installed: independence comes from the runtime.
            choice = self.coordinator.pick_reviewer_agent("claude")
            self.assertEqual(choice["agent"], "codex")
            self.assertEqual(choice["independence"], "different-runtime")
            self.assertEqual(self.coordinator.pick_reviewer_agent("codex")["agent"], "claude")

        only_one = self._fake_agent_cli("claude")
        with mock.patch.dict(os.environ, {"PATH": str(only_one)}):
            # One runtime and no model: refuse rather than let an agent review
            # its own work and call the result independent.
            with self.assertRaisesRegex(HelmError, r"no independent reviewer is available"):
                self.coordinator.pick_reviewer_agent("claude")
            # A different model on the same runtime is the documented fallback.
            fallback = self.coordinator.pick_reviewer_agent("claude", model="claude-sonnet-5")
            self.assertEqual(fallback["independence"], "different-model")
            self.assertIn("--model", fallback["command"])
            self.assertIn("claude-sonnet-5", fallback["command"])
            # The model flag must not land where a variadic option eats it.
            self.assertLess(
                fallback["command"].index("claude-sonnet-5"),
                fallback["command"].index(runtimes.PROMPT_PLACEHOLDER),
            )

    def test_an_excluded_runtime_is_never_started_for_any_task(self) -> None:
        """The exclusion is about starting a runtime, not only about reviewing.

        Scoping it to reviews left the expensive runtime one `--agent` away,
        one project pin away, or one lucky detection away.
        """
        root = self.repo("noexpensive")
        project = self.coordinator.register_project(
            "NoExpensive", str(root), project_id="noexpensive"
        )
        bin_dir = self._fake_agent_cli("claude", "codex")
        path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        with mock.patch.dict(
            os.environ, {"PATH": path, "HELM_EXCLUDE_AGENTS": "codex"}
        ):
            # Naming it explicitly is refused rather than silently substituted,
            # which would hide that the request was overridden.
            named = self.coordinator.create_task(project["id"], "work", agent="codex")
            with self.assertRaisesRegex(HelmError, r"codex is excluded from this Helm root"):
                self.coordinator.launch_worker(named["id"], None, wait=False)

            # A project pin is the same attempt by another route, and the
            # refusal has to name the source, because that is what a human
            # then has to go and change.
            with self.state.locked() as data:
                data["projects"][project["id"]]["agent"] = "codex"
            pinned = self.coordinator.create_task(project["id"], "work")
            with self.assertRaisesRegex(HelmError, r"pins agent codex"):
                self.coordinator.launch_worker(pinned["id"], None, wait=False)

    def test_a_runtime_the_root_excludes_is_never_picked_to_review(self) -> None:
        """Cost policy: some runtimes are too expensive to spend a review on.

        Independence still comes first -- an excluded runtime is skipped, not
        substituted for the author, so an exclusion can never quietly downgrade
        a review into the agent checking its own work.
        """
        bin_dir = self._fake_agent_cli("claude", "codex", "pi")
        with mock.patch.dict(
            os.environ, {"PATH": str(bin_dir), "HELM_REVIEW_EXCLUDE_AGENTS": "codex"}
        ):
            choice = self.coordinator.pick_reviewer_agent("claude")
            self.assertEqual(choice["agent"], "pi")
            self.assertEqual(choice["independence"], "different-runtime")
            # Naming it explicitly must not route around the policy.
            with self.assertRaisesRegex(HelmError, r"codex is excluded from reviews"):
                self.coordinator.pick_reviewer_agent("claude", explicit="codex")

        only_author_and_excluded = self._fake_agent_cli("claude", "codex")
        with mock.patch.dict(
            os.environ,
            {"PATH": str(only_author_and_excluded), "HELM_REVIEW_EXCLUDE_AGENTS": "codex"},
        ):
            # Refuse rather than fall back to the author reviewing itself, and
            # say that the exclusion is why nothing independent was left.
            with self.assertRaisesRegex(HelmError, r"Excluded from reviews in this root: codex"):
                self.coordinator.pick_reviewer_agent("claude")

    def test_codex_launch_permits_the_reporting_directory_it_names(self) -> None:
        # Codex refuses --add-dir outright unless the sandbox allows extra
        # writable roots, so omitting --sandbox did not keep the sandbox
        # narrow: it killed every codex worker at launch, which took the
        # independent reviewer with it and silently reduced code review to
        # same-runtime self-review.
        codex = runtimes.builtin_runtime("codex")
        assert codex is not None
        for command in (codex.interactive, codex.noninteractive):
            self.assertIn("--add-dir", command)
            self.assertIn("--sandbox", command)
            mode = command[command.index("--sandbox") + 1]
            # workspace-write is the narrower of the two modes that allow it;
            # danger-full-access would drop the sandbox entirely.
            self.assertEqual(mode, "workspace-write")
