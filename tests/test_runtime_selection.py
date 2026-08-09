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

    def test_claude_family_identifiers_are_recognised_without_over_matching(self) -> None:
        """The classifier decides which launches are refused, so its edges matter.

        Too narrow and a gateway spelling walks straight through the boundary;
        too wide and an unrelated model is refused for a policy that was never
        about it.
        """
        claude = [
            "claude-opus-5",
            "claude-haiku-4-5",
            "claude-3-5-sonnet-20241022",
            "anthropic/claude-opus-5",
            "openrouter/anthropic/claude-3.5-sonnet",
            "bedrock/us.anthropic.claude-sonnet-4-v1:0",
            "vertex:claude-sonnet-4",
            # Gateways that flatten the provider into the model half of one
            # segment leave no `anthropic` path element to find.
            "azure/anthropic-claude-sonnet-4",
            "anthropic.claude-3-haiku",
            "gateway/anthropic-opus",
            "sonnet",
            "opus-4.1",
            "haiku",
            "sonnet-4-5",
            "fable-5",
            "CLAUDE-OPUS-5",
        ]
        for model in claude:
            with self.subTest(model=model):
                self.assertTrue(runtimes.is_claude_model(model))
        others = [
            "gpt-5.6-sol",
            "gemini-2.5-pro",
            "openai-codex/gpt-5.6-sol",
            # Substring lookalikes: refusing these would be a policy about
            # spelling rather than about which vendor runs the model.
            "opus-magnum",
            "sonnetize",
            "haiku-poet-9000",
            "claudette-1",
            "myanthropic-model",
            "anthropical-1",
            "unclaude",
            "",
            None,
        ]
        for model in others:
            with self.subTest(model=model):
                self.assertFalse(runtimes.is_claude_model(model))

    def test_a_claude_model_launches_on_claude_code_and_nowhere_else(self) -> None:
        """A cross-provider runtime would accept the name and bill for it.

        So the pairing is enforced where the model and the runtime meet, for
        every source that can name a model, rather than at whichever entry
        point somebody happened to notice.
        """
        bin_dir = self._fake_agent_cli("claude", "pi", "opencode", "omp", "codex")
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CLAUDECODE": "1",
            "HELM_AGENT": "",
            "HELM_MODEL": "",
        }

        # Accepted: the built-in claude runtime is the one that may run these.
        helm_root = self._runtime_root("claudeok")
        ok = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = ok.discover_project(helm_root, "claudeok")
        for model in ("claude-opus-5", "anthropic/claude-opus-5", "sonnet-4-5"):
            task = ok.create_task(project["id"], "Port the parser", model=model)
            with mock.patch.dict(os.environ, env):
                worker = ok.prepare_external_worker(
                    task["id"], None, execution="herdr", agent="claude"
                )
            self.assertEqual(worker["agent_id"], "claude")
            self.assertEqual(worker["command"][1:3], ["--model", model])

        # Refused on every other runtime, whichever source named the model,
        # and named rather than silently swapped for a runtime that may run it.
        for agent in ("pi", "opencode", "omp", "codex"):
            task = ok.create_task(
                project["id"], "Port the parser", model="claude-opus-5", agent=agent
            )
            with mock.patch.dict(os.environ, env), self.assertRaisesRegex(
                HelmError, r"may only be launched through Helm's built-in claude runtime"
            ) as caught:
                ok.prepare_external_worker(task["id"], None, execution="herdr")
            self.assertIn(agent, str(caught.exception))

        # A project pin and HELM_MODEL are the same attempt by another route.
        pinned_root = self._runtime_root(
            "claudepin", {"agent": "pi", "model": "anthropic/claude-opus-5"}
        )
        pinned = Coordinator(StateStore(pinned_root / "state", helm_root=pinned_root))
        pinned_project = pinned.discover_project(pinned_root, "claudepin")
        pinned_task = pinned.create_task(pinned_project["id"], "Port the parser")
        with mock.patch.dict(os.environ, env), self.assertRaisesRegex(
            HelmError, r"anthropic/claude-opus-5 is a Claude-family model"
        ):
            pinned.prepare_external_worker(pinned_task["id"], None, execution="herdr")

        ambient_root = self._runtime_root("claudeambient", {"agent": "opencode"})
        ambient = Coordinator(StateStore(ambient_root / "state", helm_root=ambient_root))
        ambient_project = ambient.discover_project(ambient_root, "claudeambient")
        ambient_task = ambient.create_task(ambient_project["id"], "Port the parser")
        with mock.patch.dict(
            os.environ, {**env, "HELM_MODEL": "opus-4.1"}
        ), self.assertRaisesRegex(HelmError, r"built-in claude runtime"):
            ambient.prepare_external_worker(ambient_task["id"], None, execution="herdr")

    def test_a_profile_inheriting_a_runtime_cannot_smuggle_a_claude_model(self) -> None:
        """A profile is a name for a runtime, not a way around its pairing."""
        helm_root = self._runtime_root("profiled")
        (helm_root / "agents.json").write_text(
            json.dumps({"agents": [{"id": "reviewer", "runtime": "pi"}]})
        )
        bin_dir = self._fake_agent_cli("pi", "claude")
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "profiled")
        task = coordinator.create_task(
            project["id"], "Review the parser", model="claude-opus-5", agent="reviewer"
        )
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HELM_MODEL": ""}
        with mock.patch.dict(os.environ, env), self.assertRaisesRegex(
            HelmError, r"may only be launched through Helm's built-in claude runtime"
        ):
            coordinator.prepare_external_worker(task["id"], None, execution="herdr")

    def test_a_command_that_bakes_a_claude_model_into_argv_is_refused(self) -> None:
        """A boundary checked only where Helm places a model is one argv walks past.

        A profile or a caller-supplied command can select the model itself, so
        the visible argv forms are inspected at launch rather than trusted.
        """
        helm_root = self._runtime_root("baked")
        (helm_root / "agents.json").write_text(
            json.dumps({
                "agents": [
                    {"id": "sneaky", "command": ["pi", "--model", "claude-opus-5", "{prompt}"]},
                    {"id": "inline", "command": ["opencode", "--model=anthropic/claude-opus-5", "{prompt}"]},
                    {"id": "honest", "command": ["pi", "--model", "gpt-5.6-sol", "{prompt}"]},
                    # A profile may call itself claude and start something
                    # else. The executable is what decides.
                    {"id": "claude", "command": ["pi", "--model", "claude-opus-5", "{prompt}"]},
                ]
            })
        )
        bin_dir = self._fake_agent_cli("pi", "opencode", "claude")
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        project = coordinator.discover_project(helm_root, "baked")
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HELM_MODEL": ""}

        for agent in ("sneaky", "inline", "claude"):
            task = coordinator.create_task(project["id"], "Port the parser", agent=agent)
            with mock.patch.dict(os.environ, env), self.assertRaisesRegex(
                HelmError, r"its launch command selects that model"
            ):
                coordinator.prepare_external_worker(task["id"], None, execution="herdr")

        # A non-Claude model in the same shape is ordinary configuration and
        # is left exactly alone.
        fine = coordinator.create_task(project["id"], "Port the parser", agent="honest")
        with mock.patch.dict(os.environ, env):
            worker = coordinator.prepare_external_worker(fine["id"], None, execution="herdr")
        self.assertIn("gpt-5.6-sol", worker["command"])

        # A caller-supplied command is the same route without a profile...
        direct = coordinator.create_task(project["id"], "Port the parser")
        coordinator.allocate_task(direct["id"])
        with mock.patch.dict(os.environ, env), self.assertRaisesRegex(
            HelmError, r"claude-opus-5 is a Claude-family model"
        ):
            coordinator.prepare_external_worker(
                direct["id"],
                ["pi", "--model", "claude-opus-5", "{prompt}"],
                execution="process",
            )

        # ...and Claude Code selecting a Claude model is the allowed pairing,
        # so an ordinary custom command still launches.
        allowed = coordinator.create_task(project["id"], "Port the parser")
        coordinator.allocate_task(allowed["id"])
        with mock.patch.dict(os.environ, env):
            worker = coordinator.prepare_external_worker(
                allowed["id"],
                ["claude", "--model", "claude-opus-5", "{prompt}"],
                execution="process",
            )
        self.assertIn("claude-opus-5", worker["command"])

    def test_an_opaque_command_is_a_stated_limit_not_a_silent_one(self) -> None:
        """Helm reads argv; it cannot introspect a wrapper script.

        A wrapper that chooses a model from its own config or environment is
        outside what any launch-time check can see, so the boundary says so in
        the code that implements it rather than implying a guarantee it has
        no way to keep.
        """
        self.assertIsNone(
            runtimes.claude_model_in_command(["/usr/local/bin/run-agent.sh", "{prompt}"])
        )
        self.assertIn("opaque wrapper", runtimes.claude_model_in_command.__doc__ or "")
        # And what is visible is still found, in both spellings.
        self.assertEqual(
            runtimes.claude_model_in_command(["pi", "--model", "sonnet-4-5"]), "sonnet-4-5"
        )
        self.assertEqual(
            runtimes.claude_model_in_command(["pi", "--model=claude-opus-5"]), "claude-opus-5"
        )
        self.assertIsNone(runtimes.claude_model_in_command(["pi", "--model", "gpt-5.6-sol"]))
        # `--model=` states an empty value; the argument after it is the
        # prompt, and reading it as a model would refuse a launch over
        # whatever the brief happened to mention.
        self.assertIsNone(
            runtimes.claude_model_in_command(
                ["pi", "--model=", "Rewrite the claude-opus-5 migration notes"]
            )
        )

    def test_launch_identity_comes_from_the_executable_not_the_profile_label(self) -> None:
        """Metadata claiming to be Claude Code is not Claude Code.

        A profile is a label; argv[0] is the program that will actually run. If
        the label won, the boundary would accept the one answer that lets a
        Claude model launch on another vendor's CLI.
        """
        claude_label = {"id": "claude", "builtin": True}
        for executable in ("pi", "opencode", "/usr/local/bin/omp"):
            with self.subTest(executable=executable):
                self.assertEqual(
                    Coordinator._launch_runtime_id(
                        claude_label, [executable, "--model", "claude-opus-5"]
                    ),
                    Path(executable).name,
                )
                with self.assertRaisesRegex(
                    HelmError, r"its launch command selects that model"
                ):
                    Coordinator._with_model(
                        claude_label, [executable, "--model", "claude-opus-5"], None, ""
                    )
        # A profile that names its runtime rather than being one loses to argv
        # in exactly the same way.
        self.assertEqual(
            Coordinator._launch_runtime_id(
                {"id": "house", "runtime": "claude"}, ["pi", "{prompt}"]
            ),
            "pi",
        )
        # The converse: non-Claude metadata over a command that visibly starts
        # Claude Code is Claude Code, so the allowed pairing still launches.
        pi_label = {"id": "pi", "builtin": True}
        self.assertEqual(
            Coordinator._launch_runtime_id(pi_label, ["claude", "--model", "claude-opus-5"]),
            "claude",
        )
        self.assertEqual(
            Coordinator._with_model(
                pi_label, ["claude", "--model", "claude-opus-5", "{prompt}"], None, ""
            ),
            ["claude", "--model", "claude-opus-5", "{prompt}"],
        )
        # An unrecognized program is evidence of nothing, so the profile is
        # what is left to go on.
        self.assertEqual(
            Coordinator._launch_runtime_id(pi_label, ["/usr/local/bin/wrapper.sh"]), "pi"
        )
        self.assertIsNone(
            Coordinator._launch_runtime_id({"id": "default"}, ["/usr/local/bin/wrapper.sh"])
        )

    def test_a_claude_reviewer_model_only_ever_runs_on_claude_code(self) -> None:
        """Review is the path most likely to reach for a cross-provider runtime.

        `--reviewer-model` exists precisely to buy independence through the
        model, so it is exactly where a Claude model would otherwise land on pi
        or opencode.
        """
        bin_dir = self._fake_agent_cli("claude", "pi", "opencode")
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
            # Named explicitly: refused, and the required runtime is named.
            with self.assertRaisesRegex(
                HelmError, r"may only be launched through Helm's built-in claude runtime"
            ):
                self.coordinator.pick_reviewer_agent(
                    "codex", explicit="pi", model="anthropic/claude-opus-5"
                )
            # Claude Code itself is the accepted pairing.
            choice = self.coordinator.pick_reviewer_agent(
                "pi", explicit="claude", model="claude-opus-5"
            )
            self.assertEqual(choice["agent"], "claude")
            self.assertIn("claude-opus-5", choice["command"])

            # Chosen automatically, a Claude reviewer model narrows the field
            # to Claude Code rather than landing on whatever is installed.
            automatic = self.coordinator.pick_reviewer_agent("pi", model="sonnet-4-5")
            self.assertEqual(automatic["agent"], "claude")
            self.assertEqual(automatic["independence"], "different-runtime")

        without_claude = self._fake_agent_cli("pi", "opencode")
        with mock.patch.dict(os.environ, {"PATH": str(without_claude)}):
            # No substitution: say the pairing cannot be honoured here.
            with self.assertRaisesRegex(
                HelmError, r"no installed reviewer runtime is Claude Code"
            ):
                self.coordinator.pick_reviewer_agent("pi", model="claude-opus-5")

        only_claude = self._fake_agent_cli("claude")
        with mock.patch.dict(os.environ, {"PATH": str(only_claude)}):
            # The documented same-runtime fallback still holds for Claude.
            fallback = self.coordinator.pick_reviewer_agent("claude", model="claude-opus-5")
            self.assertEqual(fallback["independence"], "different-model")

        only_pi = self._fake_agent_cli("pi")
        with mock.patch.dict(os.environ, {"PATH": str(only_pi)}):
            # ...but it must not quietly become pi running a Claude model.
            with self.assertRaisesRegex(HelmError, r"built-in claude runtime"):
                self.coordinator.pick_reviewer_agent("pi", model="claude-opus-5")

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
