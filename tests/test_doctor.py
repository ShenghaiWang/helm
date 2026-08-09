"""`helm doctor`: the contract in docs/doctor.md, held test by test.

The command is a preflight, so its value is entirely in being trusted. Three
things have to stay true or nobody should run it: it changes nothing, a warning
never becomes an error (or the exit code stops meaning anything), and it never
reaches a credential. Each has its own case below.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

from helm import cli, doctor, preferences
from helm.core import Coordinator, StateStore, canonical

from tests.support import SHIPPED_DOMAINS, HelmTestCase


class DoctorTestCase(HelmTestCase):
    """A sound root with one committed project, plus the CLI plumbing."""

    def sound_root(self, name: str = "sound") -> tuple[Path, Coordinator]:
        helm_root = self._helm_root(name)
        (helm_root / "domains").mkdir(exist_ok=True)
        return helm_root, Coordinator(
            StateStore(helm_root / "state", helm_root=helm_root)
        )

    def add_project(self, helm_root: Path, name: str) -> Path:
        destination = helm_root / "projects" / name
        shutil.move(str(self.repo(name)), str(destination))
        return destination

    def report(self, helm_root: Path, project: str | None = None) -> doctor.Report:
        coordinator = Coordinator(StateStore(helm_root / "state", helm_root=helm_root))
        return doctor.run(coordinator, helm_root, project)

    def finding(self, report: doctor.Report, check_id: str) -> doctor.Finding:
        for entry in report.findings:
            if entry.id == check_id:
                return entry
        raise AssertionError(
            f"no {check_id} finding; got {[f.id for f in report.findings]}"
        )

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def watch_reads(self, opened: list[str]):
        """Record every path doctor actually opens.

        Asserting on the rendered output alone is not enough for the
        never-open-a-credential promise: a file can be read, understood, and
        merely not quoted back. What has to be true is that the read never
        happened, so the fixtures assert on this list.
        """
        real_open = Path.open

        def watched(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", watched):
            yield opened


class RootChecksTests(DoctorTestCase):
    def test_a_sound_root_raises_no_error_and_exits_zero(self) -> None:
        helm_root, _ = self.sound_root()
        report = self.report(helm_root)
        self.assertEqual(report.summary[doctor.ERROR], 0)
        self.assertNotEqual(report.status, doctor.ERROR)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(
            [f.id for f in report.findings][:3],
            ["root.configured", "root.layout", "root.symlinks"],
        )

    def test_doctor_changes_nothing_on_disk_or_in_state(self) -> None:
        """The whole point. A preflight that repairs cannot answer its question."""
        helm_root, coordinator = self.sound_root()
        self.add_project(helm_root, "unregistered")
        before = sorted(str(p.relative_to(helm_root)) for p in helm_root.rglob("*"))
        state_before = json.dumps(coordinator.store.load(), sort_keys=True)

        report = self.report(helm_root, "unregistered")

        self.assertEqual(report.exit_code, 0)
        after = sorted(str(p.relative_to(helm_root)) for p in helm_root.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual(state_before, json.dumps(coordinator.store.load(), sort_keys=True))
        # Specifically: inspecting a project never registers it.
        self.assertEqual(coordinator.store.load()["projects"], {})

    def test_findings_are_deterministic_across_runs(self) -> None:
        helm_root, _ = self.sound_root()
        self.add_project(helm_root, "alpha")
        first = doctor.render_json(self.report(helm_root, "alpha"))
        second = doctor.render_json(self.report(helm_root, "alpha"))
        self.assertEqual(first, second)

    def test_a_missing_required_layout_directory_is_an_error(self) -> None:
        helm_root, _ = self.sound_root()
        shutil.rmtree(helm_root / "projects")
        finding = self.finding(self.report(helm_root), "root.layout")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("projects/", finding.message)
        self.assertTrue(finding.remediation)

    def test_a_missing_optional_layout_directory_is_only_a_warning(self) -> None:
        helm_root, _ = self.sound_root()
        shutil.rmtree(helm_root / "agents")
        report = self.report(helm_root)
        self.assertEqual(self.finding(report, "root.layout").severity, doctor.WARNING)
        self.assertEqual(report.exit_code, 0)

    def test_a_symlinked_helm_owned_directory_is_an_error(self) -> None:
        helm_root, _ = self.sound_root()
        elsewhere = Path(self.temp.name) / "elsewhere-domains"
        elsewhere.mkdir()
        shutil.rmtree(helm_root / "domains")
        (helm_root / "domains").symlink_to(elsewhere)
        finding = self.finding(self.report(helm_root), "root.symlinks")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("domains/", finding.message)

    def test_tracked_local_state_is_an_error_and_an_untracked_root_is_a_warning(self) -> None:
        helm_root, _ = self.sound_root()
        # No repository at the root at all: the boundary is unverifiable, which
        # is worth saying but is not a fault.
        self.assertEqual(
            self.finding(self.report(helm_root), "root.boundaries").severity,
            doctor.WARNING,
        )

        subprocess.run(["git", "init", "-q", str(helm_root)], check=True)
        subprocess.run(["git", "-C", str(helm_root), "config", "user.name", "T"], check=True)
        subprocess.run(
            ["git", "-C", str(helm_root), "config", "user.email", "t@example.invalid"],
            check=True,
        )
        (helm_root / preferences.PREFERENCES_FILENAME).write_text(
            json.dumps({"version": preferences.PREFERENCES_VERSION}), encoding="utf-8"
        )
        subprocess.run(
            ["git", "-C", str(helm_root), "add", "-f", preferences.PREFERENCES_FILENAME],
            check=True,
        )
        subprocess.run(["git", "-C", str(helm_root), "commit", "-qm", "oops"], check=True)

        report = self.report(helm_root)
        finding = self.finding(report, "root.boundaries")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn(preferences.PREFERENCES_FILENAME, finding.message)
        self.assertEqual(report.exit_code, 1)

    def test_a_symlinked_preferences_file_is_refused_not_followed(self) -> None:
        helm_root, _ = self.sound_root()
        target = Path(self.temp.name) / "somebody-elses-preferences.json"
        target.write_text(
            json.dumps({"version": preferences.PREFERENCES_VERSION}), encoding="utf-8"
        )
        (helm_root / preferences.PREFERENCES_FILENAME).symlink_to(target)
        finding = self.finding(self.report(helm_root), "root.preferences")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("symlink", finding.message)

    def test_malformed_preferences_are_an_error_naming_the_fix(self) -> None:
        helm_root, _ = self.sound_root()
        (helm_root / preferences.PREFERENCES_FILENAME).write_text("{not json", encoding="utf-8")
        finding = self.finding(self.report(helm_root), "root.preferences")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("helm prefs keys", finding.remediation)

    def test_an_unknown_preference_key_is_an_error(self) -> None:
        helm_root, _ = self.sound_root()
        self.write_preferences(helm_root, secrets={"token": "shhh"})
        finding = self.finding(self.report(helm_root), "root.preferences")
        self.assertEqual(finding.severity, doctor.ERROR)

    def test_a_broken_domain_manifest_is_an_error(self) -> None:
        helm_root, _ = self.sound_root()
        broken = helm_root / "domains" / "broken"
        broken.mkdir(parents=True)
        (broken / "knowledge.md").write_text("k", encoding="utf-8")
        (broken / "domain.json").write_text(
            json.dumps({"extends": ["nowhere"]}), encoding="utf-8"
        )
        finding = self.finding(self.report(helm_root), "root.domains")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("broken", finding.message)

    def test_a_domain_without_knowledge_is_only_a_warning(self) -> None:
        helm_root, _ = self.sound_root()
        (helm_root / "domains" / "empty").mkdir(parents=True)
        report = self.report(helm_root)
        self.assertEqual(self.finding(report, "root.domains").severity, doctor.WARNING)
        self.assertEqual(report.exit_code, 0)

    def test_the_shipped_domain_pack_loads_cleanly(self) -> None:
        """Guards the repository's own domains, not a fixture's."""
        helm_root, _ = self.sound_root()
        shutil.rmtree(helm_root / "domains")
        shutil.copytree(SHIPPED_DOMAINS, helm_root / "domains")
        self.assertEqual(
            self.finding(self.report(helm_root), "root.domains").severity, doctor.OK
        )

    def test_a_configured_profile_with_a_missing_executable_is_an_error(self) -> None:
        helm_root, _ = self.sound_root()
        (helm_root / "agents.json").write_text(
            json.dumps(
                {"agents": [{"id": "configured", "command": ["helm-agent-not-installed"]}]}
            ),
            encoding="utf-8",
        )
        finding = self.finding(self.report(helm_root), "root.profiles")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("configured", finding.message)

    def test_a_named_runtime_with_no_executable_is_an_error(self) -> None:
        """A preference that names a runtime is a dependency, not a wish."""
        helm_root, _ = self.sound_root()
        self.write_preferences(helm_root, agent={"default": "codex"})
        with mock.patch("shutil.which", return_value=None):
            finding = self.finding(self.report(helm_root), "root.runtimes")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("agent.default", finding.message)

    def test_an_unnamed_runtime_that_is_absent_is_not_an_error(self) -> None:
        """Nobody depends on it, so its absence is at most worth knowing."""
        helm_root, _ = self.sound_root()
        with mock.patch("shutil.which", return_value=None):
            report = self.report(helm_root)
        finding = self.finding(report, "root.runtimes")
        self.assertEqual(finding.severity, doctor.WARNING)
        self.assertEqual(report.exit_code, 0)

    def test_a_runtime_the_root_excludes_cannot_also_be_its_default(self) -> None:
        helm_root, _ = self.sound_root()
        self.write_preferences(
            helm_root, agent={"default": "codex", "exclude": ["codex"]}
        )
        finding = self.finding(self.report(helm_root), "root.runtimes")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("excludes", finding.message)

    def test_herdr_absent_outside_a_herdr_session_is_informational(self) -> None:
        helm_root, _ = self.sound_root()
        with mock.patch.dict(os.environ, {"HERDR_ENV": "0"}), mock.patch(
            "shutil.which", return_value=None
        ):
            finding = self.finding(self.report(helm_root), "root.herdr")
        self.assertEqual(finding.severity, doctor.OK)
        self.assertIn("process launcher", finding.message)

    def test_herdr_declared_but_missing_is_a_broken_requirement(self) -> None:
        helm_root, _ = self.sound_root()
        with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}), mock.patch(
            "shutil.which", return_value=None
        ):
            finding = self.finding(self.report(helm_root), "root.herdr")
        self.assertEqual(finding.severity, doctor.ERROR)

    def test_no_resolvable_root_reports_the_root_and_stops(self) -> None:
        coordinator = Coordinator(self.state)
        report = doctor.run(coordinator, None, "anything")
        self.assertEqual(report.findings[0].id, "root.configured")
        self.assertEqual(report.findings[0].severity, doctor.ERROR)
        self.assertEqual(
            [f.id for f in report.findings[1:]], list(doctor.PROJECT_CHECKS)
        )
        self.assertEqual(report.exit_code, 1)
        self.assertIsNone(report.document()["root"])


class ProjectChecksTests(DoctorTestCase):
    def test_a_sound_project_adds_only_ok_findings(self) -> None:
        helm_root, _ = self.sound_root()
        self.add_project(helm_root, "alpha")
        report = self.report(helm_root, "alpha")
        project_findings = [f for f in report.findings if f.scope == "project"]
        self.assertTrue(project_findings)
        self.assertEqual(
            [f.id for f in project_findings],
            [
                "project.location",
                "project.git",
                "project.isolation",
                "project.config",
                "project.base_branch",
                "project.domains",
                "project.skills",
                "project.retained",
            ],
        )
        self.assertEqual(report.exit_code, 0)

    def test_root_checks_still_run_with_project_scope(self) -> None:
        helm_root, _ = self.sound_root()
        self.add_project(helm_root, "alpha")
        report = self.report(helm_root, "alpha")
        self.assertTrue(any(f.scope == "root" for f in report.findings))

    def test_a_project_that_is_not_a_git_repository_is_an_error(self) -> None:
        helm_root, _ = self.sound_root()
        (helm_root / "projects" / "bare").mkdir()
        finding = self.finding(self.report(helm_root, "bare"), "project.git")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("never initializes", finding.remediation)

    def test_a_project_with_no_commit_is_an_error(self) -> None:
        helm_root, _ = self.sound_root()
        empty = helm_root / "projects" / "empty"
        empty.mkdir()
        subprocess.run(["git", "init", "-q", str(empty)], check=True)
        finding = self.finding(self.report(helm_root, "empty"), "project.git")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("no commit", finding.message)

    def test_a_symlinked_project_is_an_error_and_stops_project_checks(self) -> None:
        helm_root, _ = self.sound_root()
        outside = self.repo("outside")
        (helm_root / "projects" / "linked").symlink_to(outside)
        report = self.report(helm_root, "linked")
        finding = self.finding(report, "project.location")
        self.assertEqual(finding.severity, doctor.ERROR)
        # The dependent checks are stated as unrun rather than dropped: a
        # missing id reads as "nothing to say", which is not what happened.
        rest = [f for f in report.findings if f.scope == "project"][1:]
        self.assertEqual([f.id for f in rest], list(doctor.PROJECT_CHECKS[1:]))
        for entry in rest:
            self.assertEqual(entry.severity, doctor.WARNING)
            self.assertTrue(entry.message.startswith("not checked: "))

    def test_invalid_project_settings_are_an_error(self) -> None:
        helm_root, _ = self.sound_root()
        project_root = self.add_project(helm_root, "alpha")
        (project_root / ".helm").mkdir()
        (project_root / ".helm" / "project.json").write_text(
            json.dumps({"delivery_policy": "whenever"}), encoding="utf-8"
        )
        finding = self.finding(self.report(helm_root, "alpha"), "project.config")
        self.assertEqual(finding.severity, doctor.ERROR)

    def test_a_declared_domain_that_does_not_exist_is_an_error(self) -> None:
        helm_root, _ = self.sound_root()
        project_root = self.add_project(helm_root, "alpha")
        (project_root / ".helm").mkdir()
        (project_root / ".helm" / "project.json").write_text(
            json.dumps({"domains": ["absent"]}), encoding="utf-8"
        )
        finding = self.finding(self.report(helm_root, "alpha"), "project.domains")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("absent", finding.message)

    def test_a_declared_domain_missing_guardrails_is_only_a_warning(self) -> None:
        helm_root, _ = self.sound_root()
        thin = helm_root / "domains" / "thin"
        thin.mkdir(parents=True)
        (thin / "knowledge.md").write_text("knowledge", encoding="utf-8")
        project_root = self.add_project(helm_root, "alpha")
        (project_root / ".helm").mkdir()
        (project_root / ".helm" / "project.json").write_text(
            json.dumps({"domains": ["thin"]}), encoding="utf-8"
        )
        report = self.report(helm_root, "alpha")
        self.assertEqual(self.finding(report, "project.domains").severity, doctor.WARNING)
        self.assertEqual(report.exit_code, 0)

    def test_a_pinned_skill_with_no_manifest_is_an_error(self) -> None:
        helm_root, _ = self.sound_root()
        project_root = self.add_project(helm_root, "alpha")
        (project_root / ".helm").mkdir()
        (project_root / ".helm" / "project.json").write_text(
            json.dumps({"skills": {"pin": ["release-build"]}}), encoding="utf-8"
        )
        finding = self.finding(self.report(helm_root, "alpha"), "project.skills")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("release-build", finding.message)

    def test_an_unreadable_skill_manifest_is_a_warning(self) -> None:
        helm_root, _ = self.sound_root()
        project_root = self.add_project(helm_root, "alpha")
        skill = project_root / ".agents" / "skills" / "vague"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("no frontmatter at all\n", encoding="utf-8")
        report = self.report(helm_root, "alpha")
        self.assertEqual(self.finding(report, "project.skills").severity, doctor.WARNING)
        self.assertEqual(report.exit_code, 0)

    def test_a_base_branch_that_does_not_resolve_is_an_error(self) -> None:
        helm_root, _ = self.sound_root()
        project_root = self.add_project(helm_root, "alpha")
        (project_root / ".helm").mkdir()
        (project_root / ".helm" / "project.json").write_text(
            json.dumps({"base_branch": "trunk"}), encoding="utf-8"
        )
        finding = self.finding(self.report(helm_root, "alpha"), "project.base_branch")
        self.assertEqual(finding.severity, doctor.ERROR)

    def test_a_project_with_a_remote_and_no_setting_warns_without_asking_it(self) -> None:
        """Doctor must not reach the network to answer this."""
        helm_root, _ = self.sound_root()
        project_root = self.add_project(helm_root, "alpha")
        subprocess.run(
            ["git", "-C", str(project_root), "remote", "add", "origin",
             "https://helm.invalid/nothing.git"],
            check=True,
        )
        finding = self.finding(self.report(helm_root, "alpha"), "project.base_branch")
        self.assertEqual(finding.severity, doctor.WARNING)
        self.assertIn("base_branch", finding.remediation)

    def test_retained_task_resources_are_reported_as_a_warning(self) -> None:
        helm_root, coordinator = self.sound_root()
        self.add_project(helm_root, "alpha")
        project = coordinator.discover_project(helm_root, "alpha")
        task = coordinator.create_task(project["id"], "do the thing")
        coordinator.allocate_task(task["id"])
        report = self.report(helm_root, "alpha")
        finding = self.finding(report, "project.retained")
        self.assertEqual(finding.severity, doctor.WARNING)
        self.assertIn("helm task cleanup", finding.remediation)
        self.assertEqual(report.exit_code, 0)

    def test_overlapping_project_roots_are_an_error(self) -> None:
        helm_root, coordinator = self.sound_root()
        self.add_project(helm_root, "alpha")
        # A second registered project whose root contains alpha's.
        with coordinator.store.locked() as data:
            data["projects"]["outer"] = {
                "id": "outer",
                "name": "outer",
                "root": str(helm_root / "projects"),
                "created_at": "2020-01-01T00:00:00Z",
                "delivery_policy": "local",
            }
        finding = self.finding(self.report(helm_root, "alpha"), "project.isolation")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("outer", finding.message)


class OutputAndExitStatusTests(DoctorTestCase):
    def test_text_output_names_every_finding_and_summarises(self) -> None:
        helm_root, _ = self.sound_root()
        shutil.rmtree(helm_root / "projects")
        lines = doctor.render_text(self.report(helm_root))
        self.assertTrue(lines[0].startswith("helm doctor: root "))
        self.assertTrue(any("root.layout" in line for line in lines))
        self.assertTrue(any(line.strip().startswith("-> ") for line in lines))
        self.assertRegex(lines[-1], r"^\d+ errors?, \d+ warnings?, \d+ ok$")

    def test_text_output_labels_the_project_section(self) -> None:
        helm_root, _ = self.sound_root()
        self.add_project(helm_root, "alpha")
        lines = doctor.render_text(self.report(helm_root, "alpha"))
        self.assertIn("project alpha", lines)

    def test_json_output_has_the_documented_shape(self) -> None:
        helm_root, _ = self.sound_root()
        self.add_project(helm_root, "alpha")
        document = json.loads(doctor.render_json(self.report(helm_root, "alpha")))
        self.assertEqual(document["version"], doctor.REPORT_VERSION)
        self.assertEqual(document["root"], str(canonical(helm_root)))
        self.assertEqual(document["project"], "alpha")
        self.assertEqual(
            set(document), {"version", "root", "project", "status", "summary", "findings"}
        )
        self.assertEqual(set(document["summary"]), {"ok", "warning", "error"})
        for finding in document["findings"]:
            self.assertEqual(
                set(finding), {"id", "scope", "severity", "message", "remediation"}
            )
            self.assertIn(finding["severity"], {"ok", "warning", "error"})
            self.assertIn(finding["scope"], {"root", "project"})
            if finding["severity"] == "ok":
                self.assertEqual(finding["remediation"], "")
            else:
                self.assertTrue(finding["remediation"])
        self.assertEqual(
            sum(document["summary"].values()), len(document["findings"])
        )

    def test_warnings_alone_still_exit_zero_and_errors_exit_one(self) -> None:
        helm_root, _ = self.sound_root()
        shutil.rmtree(helm_root / "agents")
        code, out, _ = self.run_cli("--root", str(helm_root), "doctor")
        self.assertEqual(code, 0)
        self.assertIn("warning", out)

        shutil.rmtree(helm_root / "projects")
        code, out, _ = self.run_cli("--root", str(helm_root), "doctor")
        self.assertEqual(code, 1)
        self.assertIn("error", out)

    def test_an_unknown_project_is_an_invalid_invocation(self) -> None:
        helm_root, _ = self.sound_root()
        code, _, err = self.run_cli("--root", str(helm_root), "doctor", "--project", "ghost")
        self.assertEqual(code, 2)
        self.assertIn("unknown project ghost", err)

    def test_json_mode_prints_only_the_document(self) -> None:
        helm_root, _ = self.sound_root()
        code, out, _ = self.run_cli("--root", str(helm_root), "doctor", "--json")
        self.assertEqual(code, 0)
        json.loads(out)  # nothing else on stdout, or this raises

    def test_doctor_is_available_to_an_agent_because_it_authorizes_nothing(self) -> None:
        helm_root, _ = self.sound_root()
        with mock.patch.dict(os.environ, {"HELM_WORKER_ID": "worker-1"}):
            code, _, err = self.run_cli("--root", str(helm_root), "doctor")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")


class SecretSafetyTests(DoctorTestCase):
    """The hard rule: no credential, and no environment value, reaches output."""

    def test_no_credential_file_is_opened_or_printed(self) -> None:
        helm_root, _ = self.sound_root()
        project_root = self.add_project(helm_root, "alpha")
        secret = "sk-helm-doctor-must-never-print-this"
        for name in ("auth.json", ".env", "credentials"):
            (project_root / name).write_text(f"token={secret}\n", encoding="utf-8")
        (helm_root / "auth.json").write_text(f"token={secret}\n", encoding="utf-8")

        opened: list[str] = []
        with self.watch_reads(opened):
            rendered = doctor.render_json(self.report(helm_root, "alpha"))

        self.assertNotIn(secret, rendered)
        self.assertFalse(
            [path for path in opened if Path(path).name in {"auth.json", ".env", "credentials"}],
            f"doctor opened a credential file: {opened}",
        )

    def test_environment_values_are_never_printed(self) -> None:
        helm_root, _ = self.sound_root()
        with mock.patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "sk-not-in-output", "HELM_AGENT": "claude"},
        ):
            rendered = doctor.render_json(self.report(helm_root))
        self.assertNotIn("sk-not-in-output", rendered)
        # The one variable a check is *about* may be named, never quoted back
        # with its value attached beyond the runtime id it selects.
        self.assertNotIn("ANTHROPIC_API_KEY", rendered)

    def test_no_provider_command_is_executed(self) -> None:
        """Runtime readiness here is executable presence, deliberately.

        Auth, status and catalogue commands print account state and cost money;
        running one from a preflight would make doctor the thing that leaks.
        """
        helm_root, _ = self.sound_root()
        self.write_preferences(helm_root, agent={"default": "claude"})
        (helm_root / "agents.json").write_text(
            json.dumps(
                {
                    "agents": [
                        {
                            "id": "configured",
                            "command": ["git", "--version"],
                            "check_command": ["git", "--version"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")):
            with mock.patch("helm.doctor._git_lines", return_value=[]):
                report = self.report(helm_root)
        self.assertEqual(
            self.finding(report, "root.profiles").severity, doctor.OK
        )
        self.assertIn(
            "not credential readiness",
            self.finding(report, "root.profiles").message,
        )


class ReviewRegressionTests(DoctorTestCase):
    """One case per finding from the independent review of this command.

    They are grouped rather than scattered because they share a subject: each
    is a way a preflight can look healthy while being wrong, which is the only
    failure mode that makes doctor worse than not running it.
    """

    # 1 -- state permissions must report what was found, not what Helm repaired.

    def test_exposed_state_permissions_survive_helms_own_repair(self) -> None:
        helm_root, _ = self.sound_root()
        state_dir = helm_root / "state"
        state_file = state_dir / "state.json"
        state_file.write_text(
            json.dumps(StateStore.empty() | {"config": {"helm_root": str(helm_root)}}),
            encoding="utf-8",
        )
        os.chmod(state_dir, 0o755)
        os.chmod(state_file, 0o644)

        report = self.report(helm_root)

        finding = self.finding(report, "root.state")
        self.assertEqual(finding.severity, doctor.WARNING)
        self.assertIn("readable beyond its owner", finding.message)
        self.assertIn("restricted it on open", finding.message)
        # Helm still repairs on open -- doctor simply no longer reads its own
        # repair back and calls the root healthy.
        self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)

    # 2 -- git probes must not execute a checkout's configured helpers.

    def _fsmonitor_repo(self, project_root: Path) -> Path:
        marker = Path(self.temp.name) / "fsmonitor-ran"
        helper = Path(self.temp.name) / "fsmonitor.sh"
        helper.write_text(
            f"#!/bin/sh\ntouch {marker}\nprintf '/\\0'\n", encoding="utf-8"
        )
        helper.chmod(0o755)
        subprocess.run(
            ["git", "-C", str(project_root), "config", "core.fsmonitor", str(helper)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(project_root), "config", "core.fsmonitorHookVersion", "2"],
            check=True,
        )
        return marker

    def test_a_repositorys_own_fsmonitor_helper_is_never_executed(self) -> None:
        helm_root, _ = self.sound_root()
        project_root = self.add_project(helm_root, "alpha")
        marker = self._fsmonitor_repo(project_root)

        report = self.report(helm_root, "alpha")

        self.assertFalse(
            marker.exists(),
            "doctor executed a helper configured by the repository it inspected",
        )
        self.assertIn(
            self.finding(report, "project.git").severity, {doctor.OK, doctor.WARNING}
        )

    def test_git_probes_do_not_rewrite_the_index(self) -> None:
        helm_root, _ = self.sound_root()
        project_root = self.add_project(helm_root, "alpha")
        index = project_root / ".git" / "index"
        before = index.read_bytes()
        os.utime(project_root / "README.txt", (0, 0))

        self.report(helm_root, "alpha")

        self.assertEqual(index.read_bytes(), before)

    # 3 -- configuration is read from the layout, never from a redirect.

    def test_a_preferences_env_override_is_reported_not_followed(self) -> None:
        helm_root, _ = self.sound_root()
        planted = Path(self.temp.name) / "auth.json"
        planted.write_text('{"version": 1, "token": "sk-secret"}', encoding="utf-8")
        with mock.patch.dict(os.environ, {preferences.PREFERENCES_ENV: str(planted)}):
            report = self.report(helm_root)
            rendered = doctor.render_json(report)
        finding = self.finding(report, "root.preferences")
        self.assertEqual(finding.severity, doctor.WARNING)
        self.assertIn(preferences.PREFERENCES_ENV, finding.message)
        self.assertNotIn("sk-secret", rendered)
        self.assertNotIn(str(planted), rendered)

    def test_an_agents_env_override_is_reported_not_followed(self) -> None:
        helm_root, _ = self.sound_root()
        planted = Path(self.temp.name) / "credentials.json"
        planted.write_text('{"agents": []}', encoding="utf-8")
        opened: list[str] = []
        with mock.patch.dict(os.environ, {"HELM_AGENTS_FILE": str(planted)}):
            with self.watch_reads(opened):
                report = self.report(helm_root)
        finding = self.finding(report, "root.profiles")
        self.assertEqual(finding.severity, doctor.WARNING)
        self.assertIn("HELM_AGENTS_FILE", finding.message)
        self.assertNotIn(str(planted), opened)

    def test_a_symlinked_project_settings_file_is_refused_not_resolved(self) -> None:
        """The link stays inside the project, so path resolution allows it."""
        helm_root, _ = self.sound_root()
        project_root = self.add_project(helm_root, "alpha")
        secret = project_root / "auth.json"
        secret.write_text('{"token": "sk-inside-the-project"}', encoding="utf-8")
        (project_root / ".helm").mkdir()
        (project_root / ".helm" / "project.json").symlink_to(secret)

        opened: list[str] = []
        with self.watch_reads(opened):
            report = self.report(helm_root, "alpha")

        finding = self.finding(report, "project.config")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("symlink", finding.message)
        self.assertNotIn(str(secret), opened)
        self.assertNotIn("sk-inside-the-project", doctor.render_json(report))

    def test_a_symlinked_agent_configuration_is_refused(self) -> None:
        helm_root, _ = self.sound_root()
        planted = Path(self.temp.name) / "agent-auth.json"
        planted.write_text('{"agents": []}', encoding="utf-8")
        (helm_root / "agents.json").symlink_to(planted)
        opened: list[str] = []
        with self.watch_reads(opened):
            finding = self.finding(self.report(helm_root), "root.profiles")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("symlink", finding.message)
        self.assertNotIn(str(planted), opened)

    def test_a_symlinked_domain_manifest_is_refused(self) -> None:
        helm_root, _ = self.sound_root()
        planted = Path(self.temp.name) / "domain-auth.json"
        planted.write_text('{"extends": []}', encoding="utf-8")
        domain = helm_root / "domains" / "linked"
        domain.mkdir(parents=True)
        (domain / "knowledge.md").write_text("k", encoding="utf-8")
        (domain / "domain.json").symlink_to(planted)
        opened: list[str] = []
        with self.watch_reads(opened):
            finding = self.finding(self.report(helm_root), "root.domains")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("symlink", finding.message)
        self.assertNotIn(str(planted), opened)

    # 4 -- an environment value never reaches output.

    def test_an_unknown_helm_agent_value_is_never_printed(self) -> None:
        helm_root, _ = self.sound_root()
        with mock.patch.dict(os.environ, {"HELM_AGENT": "sk-looks-like-a-runtime"}):
            report = self.report(helm_root)
            rendered = doctor.render_json(report)
        finding = self.finding(report, "root.runtimes")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertNotIn("sk-looks-like-a-runtime", rendered)
        self.assertIn("HELM_AGENT", finding.message)

    def test_a_helm_model_value_is_never_printed(self) -> None:
        helm_root, _ = self.sound_root()
        self.write_preferences(
            helm_root,
            agent={"default": "claude"},
            model={"runtimes": {"claude": ["pi"]}},
        )
        with mock.patch.dict(os.environ, {"HELM_MODEL": "claude-sk-secret-model"}):
            report = self.report(helm_root)
            rendered = doctor.render_json(report)
        self.assertEqual(self.finding(report, "root.runtimes").severity, doctor.ERROR)
        self.assertNotIn("claude-sk-secret-model", rendered)
        self.assertIn("HELM_MODEL", self.finding(report, "root.runtimes").message)

    # 5 -- a symlinked Helm-owned directory is not traversed.

    def test_a_symlinked_projects_directory_stops_every_project_check(self) -> None:
        helm_root, _ = self.sound_root()
        outside = Path(self.temp.name) / "outside-projects"
        outside.mkdir()
        external = outside / "alpha"
        shutil.move(str(self.repo("alpha")), str(external))
        marker = self._fsmonitor_repo(external)
        shutil.rmtree(helm_root / "projects")
        (helm_root / "projects").symlink_to(outside)

        report = self.report(helm_root, "alpha")

        self.assertEqual(self.finding(report, "root.symlinks").severity, doctor.ERROR)
        self.assertEqual(self.finding(report, "project.location").severity, doctor.ERROR)
        self.assertFalse(marker.exists(), "doctor traversed a linked projects/")
        self.assertNotIn("project.git", [
            f.id for f in report.findings if f.severity == doctor.OK
        ])

    def test_a_symlinked_domains_directory_is_not_read(self) -> None:
        helm_root, _ = self.sound_root()
        outside = Path(self.temp.name) / "outside-domains"
        (outside / "planted").mkdir(parents=True)
        planted = outside / "planted" / "domain.json"
        planted.write_text('{"extends": ["nowhere"]}', encoding="utf-8")
        shutil.rmtree(helm_root / "domains")
        (helm_root / "domains").symlink_to(outside)

        opened: list[str] = []
        with self.watch_reads(opened):
            finding = self.finding(self.report(helm_root), "root.domains")

        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("symlink", finding.message)
        self.assertNotIn(str(planted), opened)

    # 6 -- malformed input still produces a valid report.

    def test_unreadable_state_still_produces_a_report_and_exit_one(self) -> None:
        helm_root, _ = self.sound_root()
        (helm_root / "state" / "state.json").write_text("{not json", encoding="utf-8")
        code, out, err = self.run_cli("--root", str(helm_root), "doctor", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        document = json.loads(out)
        state = [f for f in document["findings"] if f["id"] == "root.state"]
        self.assertEqual([f["severity"] for f in state], ["error"])
        self.assertEqual(
            [f["id"] for f in document["findings"] if f["scope"] == "root"],
            list(doctor.ROOT_CHECKS),
        )

    def test_non_utf8_configuration_is_a_finding_not_a_traceback(self) -> None:
        helm_root, _ = self.sound_root()
        project_root = self.add_project(helm_root, "alpha")
        (project_root / ".helm").mkdir()
        (project_root / ".helm" / "project.json").write_bytes(b'{"label": "\xff\xfe"}')
        (helm_root / "agents.json").write_bytes(b'{"agents": [\xff]}')
        domain = helm_root / "domains" / "binary"
        domain.mkdir(parents=True)
        (domain / "knowledge.md").write_text("k", encoding="utf-8")
        (domain / "domain.json").write_bytes(b"\xff\xfe\x00")

        code, out, err = self.run_cli(
            "--root", str(helm_root), "doctor", "--project", "alpha", "--json"
        )

        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        document = json.loads(out)
        for check in ("root.domains", "root.profiles", "project.config"):
            entry = next(f for f in document["findings"] if f["id"] == check)
            self.assertEqual(entry["severity"], "error", check)

    def test_a_state_document_of_the_wrong_shape_is_a_finding(self) -> None:
        helm_root, _ = self.sound_root()
        self.add_project(helm_root, "alpha")
        broken = StateStore.empty() | {
            "config": {"helm_root": str(helm_root)},
            "tasks": [],
        }
        (helm_root / "state" / "state.json").write_text(
            json.dumps(broken), encoding="utf-8"
        )
        code, out, err = self.run_cli(
            "--root", str(helm_root), "doctor", "--project", "alpha", "--json"
        )
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        document = json.loads(out)
        entry = next(f for f in document["findings"] if f["id"] == "root.state")
        self.assertEqual(entry["severity"], "error")
        self.assertIn("shape", entry["message"])

    # 7 -- the effective configured launch is what gets checked.

    def test_a_missing_project_pinned_runtime_is_an_error(self) -> None:
        helm_root, _ = self.sound_root()
        project_root = self.add_project(helm_root, "alpha")
        (project_root / ".helm").mkdir()
        (project_root / ".helm" / "project.json").write_text(
            json.dumps({"agent": "codex"}), encoding="utf-8"
        )
        with mock.patch("shutil.which", return_value=None):
            report = self.report(helm_root, "alpha")
        finding = self.finding(report, "root.runtimes")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("the project's agent pin", finding.message)
        self.assertEqual(report.exit_code, 1)

    def test_a_forbidden_project_model_family_pairing_is_an_error(self) -> None:
        helm_root, _ = self.sound_root()
        self.write_preferences(helm_root, model={"runtimes": {"claude": ["pi"]}})
        project_root = self.add_project(helm_root, "alpha")
        (project_root / ".helm").mkdir()
        (project_root / ".helm" / "project.json").write_text(
            json.dumps({"agent": "codex", "model": "claude-opus-5"}), encoding="utf-8"
        )
        report = self.report(helm_root, "alpha")
        finding = self.finding(report, "root.runtimes")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("claude", finding.message)
        self.assertIn("pi", finding.message)

    def test_a_profile_availability_check_executable_must_exist(self) -> None:
        helm_root, _ = self.sound_root()
        (helm_root / "agents.json").write_text(
            json.dumps(
                {
                    "agents": [
                        {
                            "id": "configured",
                            "command": ["git"],
                            "check_command": ["helm-availability-not-installed"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        finding = self.finding(self.report(helm_root), "root.profiles")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("availability check", finding.message)

    def test_a_missing_ambient_worker_command_is_an_error_without_its_value(self) -> None:
        helm_root, _ = self.sound_root()
        with mock.patch.dict(
            os.environ, {"HELM_WORKER_COMMAND": "helm-secret-binary --key sk-secret"}
        ):
            report = self.report(helm_root)
            rendered = doctor.render_json(report)
        finding = self.finding(report, "root.profiles")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("HELM_WORKER_COMMAND", finding.message)
        self.assertNotIn("sk-secret", rendered)

    # 8 -- a probe that did not answer is not a clean checkout.

    def test_a_git_probe_that_fails_is_not_reported_as_clean(self) -> None:
        helm_root, _ = self.sound_root()
        project_root = self.add_project(helm_root, "alpha")
        real = doctor._git_lines

        def failing(root: Path, *args: str):
            if args[:1] == ("status",):
                return None
            return real(root, *args)

        with mock.patch("helm.doctor._git_lines", failing):
            finding = self.finding(self.report(helm_root, "alpha"), "project.git")
        self.assertEqual(finding.severity, doctor.WARNING)
        self.assertIn("could not report", finding.message)

    def test_an_unresolved_merge_is_reported(self) -> None:
        helm_root, _ = self.sound_root()
        project_root = self.add_project(helm_root, "alpha")
        (project_root / ".git" / "MERGE_HEAD").write_text(
            self._run_git(project_root, "rev-parse", "HEAD") + "\n", encoding="utf-8"
        )
        finding = self.finding(self.report(helm_root, "alpha"), "project.git")
        self.assertEqual(finding.severity, doctor.WARNING)
        self.assertIn("merge", finding.message)

    # 9 -- the boundary allowlist is exact paths, not basenames.

    def test_a_tracked_gitkeep_deeper_in_state_does_not_pass(self) -> None:
        helm_root, _ = self.sound_root()
        subprocess.run(["git", "init", "-q", str(helm_root)], check=True)
        subprocess.run(["git", "-C", str(helm_root), "config", "user.name", "T"], check=True)
        subprocess.run(
            ["git", "-C", str(helm_root), "config", "user.email", "t@example.invalid"],
            check=True,
        )
        planted = helm_root / "state" / "private"
        planted.mkdir()
        (planted / ".gitkeep").write_text("", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(helm_root), "add", "-f", "state/private/.gitkeep"], check=True
        )
        subprocess.run(["git", "-C", str(helm_root), "commit", "-qm", "oops"], check=True)

        finding = self.finding(self.report(helm_root), "root.boundaries")
        self.assertEqual(finding.severity, doctor.ERROR)
        self.assertIn("state/private/.gitkeep", finding.message)

    def test_the_shipped_placeholders_are_still_allowed(self) -> None:
        helm_root, _ = self.sound_root()
        subprocess.run(["git", "init", "-q", str(helm_root)], check=True)
        subprocess.run(["git", "-C", str(helm_root), "config", "user.name", "T"], check=True)
        subprocess.run(
            ["git", "-C", str(helm_root), "config", "user.email", "t@example.invalid"],
            check=True,
        )
        for name in ("projects", "agents", "state"):
            (helm_root / name / ".gitkeep").write_text("", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(helm_root), "add", "-f", f"{name}/.gitkeep"], check=True
            )
        subprocess.run(["git", "-C", str(helm_root), "commit", "-qm", "layout"], check=True)
        self.assertEqual(
            self.finding(self.report(helm_root), "root.boundaries").severity, doctor.OK
        )

    # 10 -- the retained scan is linear in the root's history.

    def test_retained_scanning_visits_each_worker_once(self) -> None:
        helm_root, coordinator = self.sound_root()
        self.add_project(helm_root, "alpha")
        project = coordinator.discover_project(helm_root, "alpha")
        with coordinator.store.locked() as data:
            for index in range(30):
                task_id = f"t-{index:04d}"
                data["tasks"][task_id] = {
                    "id": task_id,
                    "project_id": project["id"],
                    "status": "merged",
                    "holds": [],
                    "workspace": str(helm_root / "state" / "worktrees" / task_id),
                }
                worker_id = f"w-{index:04d}"
                data["workers"][worker_id] = {
                    "id": worker_id,
                    "task_id": task_id,
                    "status": "completed",
                }

        seen: list[str] = []
        real = type(coordinator)._task_workers

        def counted(data, task_id):  # type: ignore[no-untyped-def]
            seen.append(task_id)
            return real(data, task_id)

        with mock.patch.object(type(coordinator), "_task_workers", staticmethod(counted)):
            report = self.report(helm_root, "alpha")

        self.assertEqual(self.finding(report, "project.retained").severity, doctor.WARNING)
        # One lookup per task, each over only that task's own workers -- not
        # one lookup per task over every worker in the root.
        self.assertEqual(len(seen), 30)

    # 11 -- a locally recorded remote default counts as a local answer.

    def test_a_recorded_remote_head_resolves_the_base_branch(self) -> None:
        helm_root, _ = self.sound_root()
        project_root = self.add_project(helm_root, "alpha")
        branch = self._run_git(project_root, "symbolic-ref", "--short", "HEAD")
        subprocess.run(
            ["git", "-C", str(project_root), "remote", "add", "origin",
             "https://helm.invalid/nothing.git"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(project_root), "symbolic-ref",
             "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}"],
            check=True,
        )
        finding = self.finding(self.report(helm_root, "alpha"), "project.base_branch")
        self.assertEqual(finding.severity, doctor.OK)
        self.assertIn(branch, finding.message)

    def test_disagreeing_remote_heads_are_not_an_answer(self) -> None:
        helm_root, _ = self.sound_root()
        project_root = self.add_project(helm_root, "alpha")
        branch = self._run_git(project_root, "symbolic-ref", "--short", "HEAD")
        subprocess.run(
            ["git", "-C", str(project_root), "branch", "other"], check=True
        )
        for remote, head in (("origin", branch), ("mirror", "other")):
            subprocess.run(
                ["git", "-C", str(project_root), "remote", "add", remote,
                 f"https://helm.invalid/{remote}.git"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(project_root), "symbolic-ref",
                 f"refs/remotes/{remote}/HEAD", f"refs/remotes/{remote}/{head}"],
                check=True,
            )
        finding = self.finding(self.report(helm_root, "alpha"), "project.base_branch")
        self.assertEqual(finding.severity, doctor.WARNING)
