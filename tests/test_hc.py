"""The hc presentation client: Helm's layout mapped onto Herdr Core terminals.

These tests drive `HcHerdrClient` against a fake `hc` executable that keeps its
terminals in a JSON file, so no real terminal is ever started.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from helm.herdr import (
    HcHerdrClient,
    HerdrNotFound,
    SubprocessHerdrClient,
    default_client,
)

FAKE_HC = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    db = os.environ["FAKE_HC_DB"]
    state = json.load(open(db)) if os.path.exists(db) else {{"terminals": {{}}, "calls": [], "n": 0}}
    args = [a for a in sys.argv[1:]]
    args = args[:1] + [a for a in args[1:2] if a != "--json"] + args[2:] if len(args) > 1 and args[1] == "--json" else args
    state["calls"].append(args)
    def save():
        json.dump(state, open(db, "w"))
    def fail(kind, message, code=3):
        save(); print(json.dumps({{"error": {{"kind": kind, "message": message}}}})); sys.exit(code)
    def find(name):
        t = state["terminals"].get(name)
        if t is None:
            fail("not_found", "no terminal " + name)
        return t
    cmd, rest = args[0], args[1:]
    if cmd == "run":
        labels, name, cwd, i = {{}}, None, None, 0
        while i < len(rest):
            a = rest[i]
            if a == "--":
                argv = rest[i + 1:]; break
            if a in ("--name", "--cwd", "--label"):
                v = rest[i + 1]; i += 2
                if a == "--name": name = v
                elif a == "--cwd": cwd = v
                else:
                    k, _, val = v.partition("="); labels[k] = val
                continue
            i += 1
        state["n"] += 1
        state["terminals"][name] = {{"address": "t%d" % state["n"], "name": name, "labels": labels,
                                     "state": "live", "status": None, "cwd": cwd, "argv": argv, "sent": []}}
        save(); print(json.dumps({{"address": "t%d" % state["n"], "name": name, "state": "live"}}))
    elif cmd == "ls":
        want, show_all = {{}}, "--all" in rest
        for j, a in enumerate(rest):
            if a == "--label":
                k, _, v = rest[j + 1].partition("="); want[k] = v
        out = [t for t in state["terminals"].values()
               if all(t["labels"].get(k) == v for k, v in want.items())
               and (show_all or t["state"] == "live")]
        save(); print(json.dumps({{"terminals": out}}))
    elif cmd == "get":
        t = find(rest[0]); save(); print(json.dumps(t))
    elif cmd == "send":
        t = find(rest[0]); t["sent"].append(rest[1]); save(); print("{{}}")
    elif cmd == "keys":
        t = find(rest[0]); t["sent"].append("<" + " ".join(rest[1:]) + ">"); save(); print("{{}}")
    elif cmd == "label":
        t = find(rest[0])
        for kv in rest[1:]:
            k, _, v = kv.partition("="); t["labels"][k] = v
        save(); print("{{}}")
    elif cmd == "kill":
        t = find(rest[0]); t["state"] = "finished"; save(); print("{{}}")
    elif cmd == "rm":
        find(rest[0]); del state["terminals"][rest[0]]; save(); print("{{}}")
    else:
        fail("usage", "unknown " + cmd, 2)
    """
)


class HcClientTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.db = base / "hc.json"
        exe = base / "hc"
        exe.write_text(FAKE_HC.format(python=sys.executable))
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        env = mock.patch.dict(
            os.environ,
            {"FAKE_HC_DB": str(self.db), "HELM_HC": "1", "HERDR_ENV": "0"},
        )
        env.start()
        self.addCleanup(env.stop)
        self.client = HcHerdrClient(str(exe))

    def state(self) -> dict:
        return json.loads(self.db.read_text())

    def terminal(self, name: str) -> dict:
        return self.state()["terminals"][name]

    def test_layout_maps_onto_labelled_terminals(self) -> None:
        space = self.client.workspace_create("🟦 media", cwd="/tmp")["result"]
        workspace_id = space["workspace"]["workspace_id"]
        overview = space["root_pane"]["pane_id"]
        tab = self.client.tab_create(workspace_id, "ticket A", "/tmp")["result"]
        worker = tab["root_pane"]["pane_id"]
        split = self.client.pane_split(worker, "right", "/tmp")["result"]["pane"]["pane_id"]

        self.assertEqual(len({overview, worker, split}), 3)
        for name in (overview, worker, split):
            labels = self.terminal(name)["labels"]
            self.assertEqual(labels["helm.owner"], "helm")
            self.assertEqual(labels["helm.workspace"], workspace_id)
        self.assertEqual(self.terminal(split)["labels"]["helm.tab"], tab["tab"]["tab_id"])
        # The shell skips interactive update prompts, like a Herdr tab's does.
        self.assertIn("DISABLE_AUTO_UPDATE=true", self.terminal(worker)["argv"])
        # Started detached from the caller, so it neither dies with it nor
        # types notes into it.
        run = next(c for c in self.state()["calls"] if c[0] == "run")
        # Flags after `--` would reach the shell, not hc.
        self.assertEqual(self.terminal(worker)["argv"][-1], "-l")
        self.assertIn("--no-parent", run)
        self.assertIn("--bg", run)

    def test_typed_text_is_submitted_by_the_enter_that_follows_it(self) -> None:
        space = self.client.workspace_create("p")["result"]
        pane = space["root_pane"]["pane_id"]
        self.client.pane_send_text(pane, "hello")
        self.assertEqual(self.terminal(pane)["sent"], [])
        self.client.pane_send_keys(pane, "Enter")
        self.assertEqual(self.terminal(pane)["sent"], ["hello"])
        # A retried Enter with nothing pending is a bare keypress.
        self.client.pane_send_keys(pane, "Enter")
        self.client.pane_send_keys(pane, "Escape")
        self.assertEqual(self.terminal(pane)["sent"], ["hello", "<enter>", "<esc>"])

    def test_status_reports_liveness_and_agent_state(self) -> None:
        pane = self.client.workspace_create("p")["result"]["root_pane"]["pane_id"]
        state = self.state()
        state["terminals"][pane]["status"] = "working"
        self.db.write_text(json.dumps(state))
        reply = self.client.pane_status(pane)["result"]["pane"]
        self.assertTrue(reply["alive"])
        self.assertEqual(reply["agent_status"], "working")
        self.client.pane_close(pane)
        with self.assertRaises(HerdrNotFound):
            self.client.pane_status(pane)

    def test_closing_a_workspace_removes_only_its_own_terminals(self) -> None:
        first = self.client.workspace_create("a")["result"]["workspace"]["workspace_id"]
        second = self.client.workspace_create("b")["result"]["workspace"]["workspace_id"]
        self.client.tab_create(first, "t", "/tmp")
        self.client.workspace_close(first)
        with self.assertRaises(HerdrNotFound):
            self.client.workspace_get(first)
        self.client.workspace_get(second)
        with self.assertRaises(HerdrNotFound):
            self.client.tab_create(first, "late", "/tmp")

    def test_a_terminal_helm_did_not_start_is_never_touched(self) -> None:
        state = {"terminals": {"mine": {"address": "t1", "name": "mine", "labels": {},
                                        "state": "live", "status": None, "sent": []}},
                 "calls": [], "n": 1}
        self.db.write_text(json.dumps(state))
        for call in (
            lambda: self.client.pane_run("mine", "rm -rf ~"),
            lambda: self.client.pane_send_keys("mine", "Enter"),
            lambda: self.client.pane_close("mine"),
            lambda: self.client.pane_status("mine"),
        ):
            with self.assertRaises(HerdrNotFound):
                call()
        self.assertEqual(self.terminal("mine")["sent"], [])
        self.assertEqual(self.terminal("mine")["state"], "live")


class HcAvailabilityTests(unittest.TestCase):
    def test_installed_is_not_opted_in(self) -> None:
        with mock.patch.dict(os.environ, {"HELM_HC": "", "HC_TERMINAL_ID": ""}), \
                mock.patch("helm.herdr.shutil.which", return_value="/bin/hc"):
            self.assertFalse(HcHerdrClient().available())

    def test_inside_an_hc_terminal_opts_in_unless_turned_off(self) -> None:
        with mock.patch("helm.herdr.shutil.which", return_value="/bin/hc"):
            with mock.patch.dict(os.environ, {"HELM_HC": "", "HC_TERMINAL_ID": "t3"}):
                self.assertTrue(HcHerdrClient().available())
            with mock.patch.dict(os.environ, {"HELM_HC": "0", "HC_TERMINAL_ID": "t3"}):
                self.assertFalse(HcHerdrClient().available())

    def test_default_client_prefers_herdr_then_hc(self) -> None:
        with mock.patch("helm.herdr.shutil.which", return_value="/bin/x"):
            with mock.patch.dict(os.environ, {"HERDR_ENV": "1", "HELM_HC": "1"}):
                self.assertIsInstance(default_client(), SubprocessHerdrClient)
            with mock.patch.dict(os.environ, {"HERDR_ENV": "0", "HELM_HC": "1"}):
                self.assertIsInstance(default_client(), HcHerdrClient)
            with mock.patch.dict(os.environ, {"HERDR_ENV": "0", "HELM_HC": "0"}):
                client = default_client()
                self.assertIsInstance(client, SubprocessHerdrClient)
                self.assertFalse(client.available())


if __name__ == "__main__":
    unittest.main()
