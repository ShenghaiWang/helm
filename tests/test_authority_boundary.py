"""Every root-only command is refused in core, not only in CLI dispatch."""

from __future__ import annotations

import inspect
import os
from unittest import mock

from helm import cli, preferences
from helm.core import Coordinator
from helm.errors import SafetyError
from tests.support import HelmTestCase

#: What core method each root-only CLI command reaches, and which are refused
#: by the CLI alone because they act before a root exists or spawn nothing an
#: agent may spawn. Anything in `_ROOT_ONLY_COMMANDS` must appear in one map.
CORE_SEAMS: dict[tuple[str, str | None], str] = {
    ("prefs", "set"): "write_preferences",
    ("prefs", "unset"): "write_preferences",
    ("prefs", "migrate"): "write_preferences",
    ("project", "add"): "register_project",
    ("project", "remove"): "remove_project",
    ("state", "archive"): "archive_tasks",
    ("task", "approve"): "approve_task",
    ("task", "merge"): "merge_task",
    ("task", "pr"): "publish_task_branch",
    ("approval", "grant"): "grant_approval",
    ("approval", "revoke"): "revoke_approval_grant",
    ("approval", "release"): "release_task_hold",
    ("approval", "repair"): "repair_task_hold",
    ("gate", "decide"): "decide_gate",
    ("authority", None): "configure_authority",
    ("learning", "approve"): "approve_learning_proposal",
    ("learning", "reject"): "reject_learning_proposal",
    ("learning", "apply"): "apply_learning_proposal",
}
CLI_ONLY: dict[tuple[str, str | None], str] = {
    ("init", None): "creates a root; there is no store to identify a caller against yet",
    ("eval", None): "the commander's own experiment; it spawns through the gated launch path",
    ("foreman", None): "spawning is refused for any caller that is not the root or a foreman at launch",
    ("route", None): "same launch path",
}


class RootOnlyBoundaryTests(HelmTestCase):
    def test_every_root_only_command_has_a_core_check_or_a_stated_reason(self) -> None:
        listed = set(cli._ROOT_ONLY_COMMANDS)
        mapped = set(CORE_SEAMS) | set(CLI_ONLY)
        self.assertEqual(listed - mapped, set(), "root-only commands with no core seam and no stated reason")
        self.assertEqual(mapped - listed, set(), "mapped commands the CLI no longer lists as root-only")
        for command, method_name in CORE_SEAMS.items():
            method = getattr(Coordinator, method_name)
            source = inspect.getsource(method)
            self.assertIn("self.authority(", source, f"{command} reaches {method_name}, which never obtains authority")

    def test_configuring_authority_and_writing_preferences_refuse_an_agent(self) -> None:
        root = self.repo("guarded")
        project = self.coordinator.register_project("Guarded", str(root), project_id="guarded")
        task = self.coordinator.create_task(project["id"], "x")
        worker = self.coordinator.launch_worker(task["id"], ["true"], wait=False)
        with mock.patch.dict(os.environ, {"HELM_WORKER_ID": worker["id"]}):
            with self.assertRaisesRegex(SafetyError, "configuring the root"):
                self.coordinator.configure_authority("x" * 40)
            with self.assertRaisesRegex(SafetyError, "writing a preference"):
                self.coordinator.write_preferences(preferences.EMPTY)
            with self.assertRaisesRegex(SafetyError, "registering a project"):
                self.coordinator.register_project("Other", str(self.repo("other")), project_id="other")
            with self.assertRaisesRegex(SafetyError, "removing a project"):
                self.coordinator.remove_project(project["id"])
            with self.assertRaisesRegex(SafetyError, "archiving"):
                self.coordinator.archive_tasks()
        # The root can still do all of it, and a first configuration needs no capability.
        path = self.coordinator.configure_authority("y" * 40)
        self.assertTrue(path.exists())
        # And once configured, a second configuration needs the capability in force.
        with self.assertRaisesRegex(SafetyError, "requires this root's authorization capability"):
            self.coordinator.configure_authority("z" * 40)
        with mock.patch.dict(os.environ, {"HELM_AUTHORITY": "y" * 40}):
            self.coordinator.configure_authority("z" * 40)
