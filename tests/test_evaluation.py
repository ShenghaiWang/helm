"""The evaluation's records: corpus, runs, the judge's verdict, the report."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from helm.errors import HelmError
from helm.evaluation import ARMS, REPLAY_RULE, Evaluation
from tests.support import HelmTestCase


class CorpusAndRunsTests(HelmTestCase):
    def _evaluation(self) -> Evaluation:
        return Evaluation(Path(self.temp.name) / "evaluation")

    def test_a_ticket_is_registered_with_its_base_and_merge_and_brief(self) -> None:
        evaluation = self._evaluation()
        entry = evaluation.add_ticket(
            "T-1", base="a" * 40, merge="b" * 40, brief="Fix the thing.", title="Thing", pr=12
        )
        self.assertEqual(entry["id"], "T-1")
        self.assertEqual(evaluation.ticket("T-1")["merge"], "b" * 40)
        # Re-adding replaces rather than duplicating.
        evaluation.add_ticket("T-1", base="a" * 40, merge="c" * 40, brief="Fix it better.")
        self.assertEqual(len(evaluation.load_corpus()["tickets"]), 1)
        self.assertEqual(evaluation.ticket("T-1")["merge"], "c" * 40)
        with self.assertRaisesRegex(HelmError, "commit shas"):
            evaluation.add_ticket("T-2", base="main", merge="b" * 40, brief="x")
        with self.assertRaisesRegex(HelmError, "brief is empty"):
            evaluation.add_ticket("T-2", base="a" * 40, merge="b" * 40, brief="  ")
        with self.assertRaisesRegex(HelmError, "not in the corpus"):
            evaluation.ticket("T-9")

    def test_a_run_is_recorded_per_ticket_and_arm_and_cannot_be_started_twice(self) -> None:
        evaluation = self._evaluation()
        evaluation.add_ticket("T-1", base="a" * 40, merge="b" * 40, brief="Fix.")
        run = evaluation.new_run("T-1", "helm", task_ids=["t-1"])
        self.assertEqual(run["status"], "started")
        self.assertEqual(run["base"], "a" * 40)
        self.assertTrue(evaluation.run_file("T-1", "helm").is_file())
        with self.assertRaisesRegex(HelmError, "already running"):
            evaluation.new_run("T-1", "helm")
        finished = evaluation.finish_run("T-1", "helm", status="completed", tip="c" * 40, metrics={"wall_seconds": 90})
        self.assertEqual(finished["tip"], "c" * 40)
        self.assertIsNotNone(finished["ended_at"])
        # A finished run can be replaced by a fresh one.
        evaluation.new_run("T-1", "helm")
        with self.assertRaisesRegex(HelmError, "unknown arm"):
            evaluation.run_dir("T-1", "foreman")

    def test_the_diffs_the_judge_reads_come_from_one_base(self) -> None:
        repo = self.repo("judged")
        (repo / "base.txt").write_text("the base\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()
        (repo / "fix.txt").write_text("shipped fix\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "shipped"], check=True)
        merge = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()
        subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "candidate", base], check=True)
        (repo / "fix.txt").write_text("candidate fix\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "candidate"], check=True)
        tip = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()

        evaluation = self._evaluation()
        evaluation.add_ticket("T-1", base=base, merge=merge, brief="Write the fix.", title="Fix")
        paths = evaluation.write_diffs("T-1", "single", repo=repo, tip=tip)
        self.assertIn("candidate fix", Path(paths["candidate"]).read_text())
        self.assertIn("shipped fix", Path(paths["shipped"]).read_text())
        self.assertIn("Write the fix.", Path(paths["brief"]).read_text())
        brief = evaluation.judge_brief("T-1", "single", paths)
        self.assertIn(paths["candidate"], brief)
        self.assertIn('"score"', brief)
        self.assertIn("READ-ONLY", brief)

    def test_the_verdict_is_the_first_line_of_json_or_nothing(self) -> None:
        good = '{"score": 2, "solves_ticket": true, "defects": [], "missing": [], "rationale": "fine"}\nThen prose.'
        self.assertEqual(Evaluation.parse_verdict(good)["score"], 2)
        self.assertEqual(Evaluation.parse_verdict("\n  " + good)["score"], 2)
        self.assertIsNone(Evaluation.parse_verdict("APPROVED\n" + good))
        self.assertIsNone(Evaluation.parse_verdict('{"score": "high"}'))
        self.assertIsNone(Evaluation.parse_verdict('{"score": 7}'))
        self.assertIsNone(Evaluation.parse_verdict('{"rationale": "no score"}'))

    def test_the_report_lays_every_arm_beside_every_ticket(self) -> None:
        evaluation = self._evaluation()
        evaluation.add_ticket("T-1", base="a" * 40, merge="b" * 40, brief="Fix.")
        evaluation.add_ticket("T-2", base="a" * 40, merge="b" * 40, brief="Fix two.")
        evaluation.new_run("T-1", "helm")
        evaluation.finish_run(
            "T-1", "helm", status="completed", tip="c" * 40,
            judge={"score": 2}, checks=[{"command": "tsc", "ok": True}, {"command": "jest", "ok": False}],
            metrics={
                "interventions": {"authorization": 2, "ambiguity": 1},
                "review_catches": 1, "wall_seconds": 1500,
                "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 30},
            },
        )
        report = evaluation.report()
        rows = [line for line in report.splitlines() if line.startswith("| T-")]
        self.assertEqual(len(rows), 2 * len(ARMS))
        helm_row = next(line for line in rows if line.startswith("| T-1 | helm |"))
        self.assertIn("| 2 |", helm_row)
        self.assertIn("1/2 pass", helm_row)
        self.assertIn("authorization 2, ambiguity 1", helm_row)
        self.assertIn("25m", helm_row)
        self.assertIn("10/20/30", helm_row)
        self.assertIn("| T-2 | single | — |", report)
        # A note rides along in the last column, beside the numbers it qualifies.
        run = evaluation.load_run("T-1", "helm")
        run["notes"] = [{"at": "x", "text": "laptop slept"}]
        evaluation.save_run(run)
        self.assertIn("| laptop slept |", evaluation.report())


import contextlib
import io
import os
import sys
import time
from unittest import mock

from helm import cli, costs
from helm.evaluation import Runner
from helm.herdr import HerdrAdapter
from tests.support import FakeHerdr


class RunnerTests(HelmTestCase):
    """The arms, collected from the coordinator's own records."""

    def _fake_bin(self) -> Path:
        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir(exist_ok=True)
        claude = bin_dir / "claude"
        # The lone agent: commits one file and reports the way `claude -p
        # --output-format json` does. Its cwd is the worktree.
        claude.write_text(
            "#!/bin/sh\n"
            "echo fixed > fix.txt\n"
            "git add fix.txt >/dev/null 2>&1\n"
            "git -c user.email=a@b.c -c user.name=a commit -qm 'fix it' >/dev/null 2>&1\n"
            "printf '%s' '{\"result\":\"Did it. DONE.\",\"session_id\":\"s-1\","
            "\"usage\":{\"input_tokens\":5,\"output_tokens\":7,\"cache_read_input_tokens\":1,"
            "\"cache_creation_input_tokens\":2},\"num_turns\":3,\"duration_ms\":1234,"
            "\"total_cost_usd\":0.5}'\n"
        )
        claude.chmod(0o755)
        cursor = bin_dir / "cursor-agent"
        cursor.write_text("#!/bin/sh\nexit 0\n")
        cursor.chmod(0o755)
        return bin_dir

    def _corpus_repo(self, name: str) -> tuple[Path, str, str]:
        repo = self.repo(name)
        (repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()
        (repo / "fix.txt").write_text("shipped\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "shipped"], check=True)
        merge = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()
        return repo, base, merge

    def _setup(self, name: str) -> tuple[Evaluation, Runner, Path, dict]:
        repo, base, merge = self._corpus_repo(name)
        project = self.coordinator.register_project(name.title(), str(repo), project_id=name)
        evaluation = Evaluation(self.state.directory / "evaluation")
        evaluation.add_ticket("T-1", base=base, merge=merge, brief="Write fix.txt.", title="Fix")
        corpus = evaluation.load_corpus()
        corpus["settings"] = {
            "repo": str(repo),
            "projects": {"firstmate": name, "helm": name},
            "judge": {"agent": "cursor", "model": None},
            "checks": ["test -f fix.txt", "false", "git diff --quiet {base} {tip} -- nothing-changed-here"],
            "effort": "medium",
        }
        evaluation.save_corpus(corpus)
        return evaluation, Runner(self.coordinator, evaluation), repo, project

    def test_the_single_agent_runs_alone_and_its_report_is_collected(self) -> None:
        evaluation, runner, repo, _ = self._setup("solo")
        # A previous run's exit code in the ticket's directory must not read
        # as this run having finished.
        stale = evaluation.run_dir("T-1", "single") / "exit"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("7\n")
        with mock.patch.dict(os.environ, {"PATH": f"{self._fake_bin()}{os.pathsep}{os.environ['PATH']}"}):
            record = runner.start_single("T-1")
        self.assertEqual(record["status"], "running")
        exit_file = Path(record["exit_file"])
        self.assertNotEqual(exit_file.read_text() if exit_file.is_file() else "", "7\n")
        for _ in range(100):
            if exit_file.is_file():
                break
            time.sleep(0.1)
        self.assertTrue(exit_file.is_file(), "the fake agent never finished")
        collected = runner.collect("T-1", "single")
        self.assertEqual(collected["status"], "completed")
        self.assertNotEqual(collected["tip"], record["base"])
        self.assertEqual(collected["metrics"]["cost_usd"], 0.5)
        self.assertEqual(collected["metrics"]["usage"]["output_tokens"], 7)
        self.assertTrue(collected["metrics"]["declared_done"])
        self.assertEqual(collected["metrics"]["interventions"], {"authorization": 0, "ambiguity": 0, "escalation": 0})
        # The checks run in the candidate's own checkout, one result each.
        results = runner.run_checks("T-1", "single")
        self.assertEqual([r["ok"] for r in results], [True, False, True])
        self.assertIn(collected["tip"], results[2]["command"])
        # Collecting again changes nothing.
        self.assertEqual(runner.collect("T-1", "single")["status"], "completed")

    def test_firstmate_is_one_launched_worker_and_counts_what_the_human_decided(self) -> None:
        evaluation, runner, repo, project = self._setup("mate")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        with mock.patch.dict(os.environ, {"PATH": f"{self._fake_bin()}{os.pathsep}{os.environ['PATH']}"}):
            record = runner.start_firstmate("T-1", adapter=adapter)
        task_id = record["task_ids"][0]
        task = self.state.load()["tasks"][task_id]
        # The worker knows the ticket by its alias, never its real id.
        self.assertEqual(task["ticket"], "EVAL-1")
        self.assertEqual(task["base_branch"], "eval/EVAL-1-base")
        self.assertIn(REPLAY_RULE, task["brief"])
        self.assertNotIn("T-1", task["brief"])
        # Every arm thinks as hard as the others: the effort is stated.
        self.assertEqual(task["effort"], "medium")
        self.assertTrue(herdr.runs, "no worker was launched")
        self.assertIsNone(runner.collect("T-1", "firstmate")["ended_at"])  # still running
        # The commander was asked one thing; that is the intervention counted.
        self.coordinator.record_commander_ask("ambiguity", "which file?", project_id=project["id"])
        worker = next(w for w in self.state.load()["workers"].values() if w["task_id"] == task_id)
        workspace = Path(task["workspace"])
        (workspace / "fix.txt").write_text("candidate\n")
        subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
        subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "candidate"], check=True)
        self.coordinator.record_worker_message(worker["id"], "result", "done")
        collected = runner.collect("T-1", "firstmate")
        self.assertEqual(collected["status"], "completed")
        self.assertIsNotNone(collected["tip"])
        self.assertEqual(collected["metrics"]["interventions"]["ambiguity"], 1)
        self.assertEqual(collected["metrics"]["review_rounds"], 0)

    def test_helm_is_routed_and_its_foremans_task_and_reviews_are_found(self) -> None:
        evaluation, runner, repo, project = self._setup("full")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        routed: list[tuple[str, str]] = []

        def route(project_id: str, text: str) -> None:
            routed.append((project_id, text))

        record = runner.start_helm("T-1", route=route)
        self.assertEqual(routed[0][0], project["id"])
        self.assertIn("--ticket EVAL-1 --base eval/EVAL-1-base --new", routed[0][1])
        self.assertIn("TICKET EVAL-1", routed[0][1])
        self.assertIn(REPLAY_RULE, routed[0][1])
        self.assertNotIn("T-1", routed[0][1])
        # The foreman creates the task; the run finds it by alias and time.
        with mock.patch.dict(os.environ, {"PATH": f"{self._fake_bin()}{os.pathsep}{os.environ['PATH']}"}):
            task = self.coordinator.create_task(project["id"], "do EVAL-1", ticket="EVAL-1", base="eval/EVAL-1-base", agent="claude")
            adapter.launch_task(task["id"], None, wait=False, agent="claude")
            self.assertIsNone(runner.collect("T-1", "helm")["ended_at"])
            worker = next(w for w in self.state.load()["workers"].values() if w["task_id"] == task["id"])
            workspace = Path(task["workspace"])
            (workspace / "fix.txt").write_text("candidate\n")
            subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
            subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "candidate"], check=True)
            self.coordinator.record_worker_message(worker["id"], "result", "done")
            # A review round that caught something, then one that did not.
            for verdict in ("CHANGES-REQUESTED: missing test", "APPROVED"):
                review = self.coordinator.create_task(
                    project["id"], "review", role="reviewer", reviews=task["id"], read_only=True, agent="cursor"
                )
                adapter.launch_task(review["id"], None, wait=False, agent="cursor")
                reviewer = next(w for w in self.state.load()["workers"].values() if w["task_id"] == review["id"])
                self.coordinator.record_worker_message(reviewer["id"], "result", verdict)
        collected = runner.collect("T-1", "helm")
        self.assertEqual(collected["status"], "completed")
        self.assertEqual(collected["task_ids"], [task["id"]])
        self.assertEqual(collected["metrics"]["review_rounds"], 2)
        self.assertEqual(collected["metrics"]["review_catches"], 1)

    def test_the_judge_is_a_read_only_task_whose_first_line_is_the_score(self) -> None:
        evaluation, runner, repo, project = self._setup("judged")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        # A finished single run to judge: any tip with a diff from base.
        subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "cand", evaluation.ticket("T-1")["base"]], check=True)
        (repo / "fix.txt").write_text("candidate\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "cand"], check=True)
        tip = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=False)
        evaluation.new_run("T-1", "single")
        evaluation.finish_run("T-1", "single", status="completed", tip=tip)
        with mock.patch.dict(os.environ, {"PATH": f"{self._fake_bin()}{os.pathsep}{os.environ['PATH']}"}):
            record = runner.start_judge("T-1", "single", adapter=adapter)
            judge_task = self.state.load()["tasks"][record["judge_task_id"]]
            self.assertTrue(judge_task["read_only"])
            self.assertIn("candidate.patch", judge_task["brief"])
            self.assertIsNone(runner.collect_judge("T-1", "single")["judge"])
            judge_worker = next(w for w in self.state.load()["workers"].values() if w["task_id"] == judge_task["id"])
            self.coordinator.record_worker_message(
                judge_worker["id"], "result",
                '{"score": 1, "solves_ticket": false, "defects": ["x"], "missing": [], "rationale": "partial"}\nProse.',
            )
        judged = runner.collect_judge("T-1", "single")
        self.assertEqual(judged["judge"]["score"], 1)
        self.assertEqual(judged["judge"]["recovered_from"], "result")
        self.assertIn("Prose.", judged["judge_text"])
        self.assertIn("| T-1 | single | completed | 1 |", evaluation.report())

    def test_a_score_printed_in_the_pane_but_never_pushed_still_counts(self) -> None:
        evaluation, runner, repo, project = self._setup("pane-judged")
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "cand2", evaluation.ticket("T-1")["base"]], check=True)
        (repo / "fix.txt").write_text("candidate\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "cand"], check=True)
        tip = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=False)
        evaluation.new_run("T-1", "single")
        evaluation.finish_run("T-1", "single", status="completed", tip=tip)
        with mock.patch.dict(os.environ, {"PATH": f"{self._fake_bin()}{os.pathsep}{os.environ['PATH']}"}):
            record = runner.start_judge("T-1", "single", adapter=adapter)
        worker = next(w for w in self.state.load()["workers"].values() if w["task_id"] == record["judge_task_id"])
        # The brief's own template never matches: its score is a placeholder.
        Path(worker["log_file"]).write_text(
            '{"score": <0-3>, "solves_ticket": <true|false>}\n'
            'thinking...\n'
            '\x1b[2m{"score": 2, "solves_ticket": true, "defects": [], "missing": [], "rationale": "same fix"}\x1b[22m\n',
            encoding="utf-8",
        )
        judged = runner.collect_judge("T-1", "single")
        self.assertEqual(judged["judge"]["score"], 2)
        self.assertEqual(judged["judge"]["recovered_from"], "pane")
        self.assertIn("| T-1 | single | completed | 2 |", evaluation.report())

    def test_the_cli_registers_lists_and_reports(self) -> None:
        repo, base, merge = self._corpus_repo("clirepo")
        brief = Path(self.temp.name) / "brief.md"
        brief.write_text("Write fix.txt.\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main([
                "--state-dir", str(self.state.directory), "eval", "add", "T-9",
                "--base", base, "--merge", merge, "--brief-file", str(brief), "--title", "Nine",
            ]), 0)
            self.assertEqual(cli.main(["--state-dir", str(self.state.directory), "eval", "list"]), 0)
            self.assertEqual(cli.main(["--state-dir", str(self.state.directory), "eval", "report"]), 0)
        text = out.getvalue()
        self.assertIn("Registered T-9", text)
        self.assertIn("T-9        as EVAL-1", text)
        self.assertIn("single=-", text)
        self.assertIn("| T-9 | helm | — |", text)

    def test_the_arm_knows_the_ticket_by_an_alias_and_hears_the_replay_rule(self) -> None:
        evaluation, runner, repo, project = self._setup("aliased")
        bin_dir = self._fake_bin()
        # A lone agent that only writes down what it was told.
        (bin_dir / "claude").write_text(
            "#!/bin/sh\n"
            'for arg; do last="$arg"; done\n'
            'printf "%s" "$last" > prompt.txt\n'
            "printf '%s' '{\"result\":\"DONE.\",\"session_id\":\"s-2\",\"num_turns\":1,\"duration_ms\":5}'\n"
        )
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}):
            record = runner.start_single("T-1")
        for _ in range(100):
            if Path(record["exit_file"]).is_file():
                break
            time.sleep(0.1)
        prompt = (Path(record["worktree"]) / "prompt.txt").read_text()
        self.assertIn("EVAL-1", prompt)
        self.assertIn(REPLAY_RULE, prompt)
        self.assertNotIn("T-1", prompt)
        self.assertEqual(record["alias"], "EVAL-1")
        self.assertEqual(record["branch"], "eval/EVAL-1/single")
        # The checkout's own path is read too, so it carries the alias.
        self.assertIn("EVAL-1", record["worktree"])
        self.assertNotIn("T-1", record["worktree"])
        # The alias is the corpus's, stable across re-registration.
        evaluation.add_ticket("T-1", base=record["base"], merge=record["merge"], brief="Again.")
        self.assertEqual(evaluation.alias("T-1"), "EVAL-1")
        evaluation.add_ticket("T-2", base=record["base"], merge=record["merge"], brief="Two.")
        self.assertEqual(evaluation.alias("T-2"), "EVAL-2")

    def _origin_for(self, repo: Path) -> Path:
        origin = Path(self.temp.name) / f"{repo.name}-origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(origin)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)
        subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin"], check=True)
        return origin

    def _refs(self, repo: Path, *patterns: str) -> list[str]:
        out = subprocess.run(
            ["git", "-C", str(repo), "for-each-ref", "--format=%(refname)", *patterns],
            text=True, capture_output=True, check=True,
        ).stdout.split()
        return sorted(out)

    def test_a_run_strips_the_repository_to_the_history_the_ticket_starts_from(self) -> None:
        evaluation, runner, repo, project = self._setup("stripped")
        entry = evaluation.ticket("T-1")
        base, merge = entry["base"], entry["merge"]
        git = lambda *args: subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)  # noqa: E731
        # What a clone of the project carries: a tag and a release branch on
        # the shipped commit, an origin with more of the same, and an earlier
        # run's checkout on its eval branch.
        git("tag", "v1", merge)
        git("branch", "release", merge)
        earlier = Path(self.temp.name) / "earlier-run"
        git("worktree", "add", "-q", "-b", "eval/EVAL-9/single", str(earlier), base)
        origin = self._origin_for(repo)
        subprocess.run(["git", "-C", str(origin), "branch", "extra", merge], check=True)
        self.assertIn("refs/remotes/origin/release", self._refs(repo, "refs/remotes"))

        branch = runner.prepare_base("T-1", "single")

        self.assertEqual(branch, "eval/EVAL-1-base")
        self.assertEqual(self._refs(repo, "refs/tags"), [])
        self.assertEqual(self._refs(repo, "refs/heads"), ["refs/heads/eval/EVAL-1-base", "refs/heads/main"])
        self.assertEqual(self._refs(repo, "refs/remotes"), ["refs/remotes/origin/eval/EVAL-1-base"])
        self.assertEqual(self._refs(origin), ["refs/heads/eval/EVAL-1-base"])
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "main"], text=True, capture_output=True, check=True).stdout.strip()
        self.assertEqual(head, base, "the primary checkout sits at the ticket's base")
        self.assertFalse(earlier.exists(), "an earlier run's checkout is gone")
        # `git log --all` -- the lookup that found the real fix -- now shows
        # nothing past the base.
        everything = subprocess.run(["git", "-C", str(repo), "log", "--all", "--format=%H"], text=True, capture_output=True, check=True).stdout.split()
        self.assertNotIn(merge, everything)

        # A Helm task branch is a task's until cleanup: one that reaches past
        # the base is refused by name, one that does not is left alone.
        git("branch", "helm/stripped/EVAL-1-t-abc", merge)
        git("branch", "helm/stripped/old-t-def", base)
        data = self.state.load()
        data["tasks"]["t-abc"] = {"id": "t-abc", "project_id": project["id"], "branch": "helm/stripped/EVAL-1-t-abc"}
        self.state.save(data)
        with self.assertRaisesRegex(HelmError, r"helm/stripped/EVAL-1-t-abc \(task t-abc\).*helm task cleanup"):
            runner.prepare_base("T-1", "single")
        self.assertIn("refs/heads/helm/stripped/old-t-def", self._refs(repo, "refs/heads"))
        git("branch", "-D", "helm/stripped/EVAL-1-t-abc")

        # An origin that is not the evaluation's own bare repository is never stripped.
        other = self.repo("someone-elses")
        git("remote", "set-url", "origin", str(other))
        with self.assertRaisesRegex(HelmError, "not a local bare repository"):
            runner.prepare_base("T-1", "single")
        self.assertIn("refs/heads/main", self._refs(other, "refs/heads"))

    def test_a_candidate_that_copied_the_shipped_change_is_contaminated_not_scored(self) -> None:
        evaluation, runner, repo, project = self._setup("copied")
        entry = evaluation.ticket("T-1")
        bin_dir = self._fake_bin()
        (bin_dir / "merge.txt").write_text(entry["merge"])
        # The agent that found the answer: it cherry-picks the shipped commit.
        (bin_dir / "claude").write_text(
            "#!/bin/sh\n"
            'git -c user.email=a@b.c -c user.name=a cherry-pick -x "$(cat "$(dirname "$0")/merge.txt")" >/dev/null 2>&1\n'
            "printf '%s' '{\"result\":\"DONE.\",\"session_id\":\"s-3\",\"num_turns\":1,\"duration_ms\":5}'\n"
        )
        config = Path(self.temp.name) / "claude-config"
        with mock.patch.dict(os.environ, {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "CLAUDE_CONFIG_DIR": str(config),
        }):
            record = runner.start_single("T-1")
            for _ in range(100):
                if Path(record["exit_file"]).is_file():
                    break
                time.sleep(0.1)
            # Its transcript names the real ticket, which it was never told.
            transcript_dir = costs.transcript_dir(record["worktree"])
            transcript_dir.mkdir(parents=True)
            (transcript_dir / "s-3.jsonl").write_text('{"type":"user","message":{"content":"look up T-1"}}\n')
            collected = runner.collect("T-1", "single")
            self.assertEqual(collected["status"], "contaminated")
            reasons = " | ".join(collected["contamination"])
            self.assertIn("cherry-pick", reasons)
            self.assertIn(f"the shipped commit {entry['merge'][:10]}", reasons)
            self.assertIn("identical to the shipped diff", reasons)
            self.assertIn("the real ticket id T-1", reasons)
            self.assertIn("CONTAMINATED", evaluation.report())
            herdr = FakeHerdr()
            adapter = HerdrAdapter(self.coordinator, herdr)
            with self.assertRaisesRegex(HelmError, "contaminated"):
                runner.start_judge("T-1", "single", adapter=adapter)
            self.assertIsNotNone(runner.start_judge("T-1", "single", adapter=adapter, anyway=True)["judge_task_id"])
        # The patches the judge reads were written at collection, so they
        # survive the branch.
        self.assertTrue((evaluation.run_dir("T-1", "single") / "judge" / "candidate.patch").is_file())

    def test_a_replaced_run_is_kept_aside(self) -> None:
        evaluation, runner, repo, project = self._setup("kept")
        evaluation.new_run("T-1", "single")
        evaluation.finish_run("T-1", "single", status="contaminated", tip="c" * 40, contamination=["copied"])
        evaluation.new_run("T-1", "single")
        aside = list(evaluation.run_dir("T-1", "single").glob("run-superseded-*.json"))
        self.assertEqual(len(aside), 1)
        self.assertEqual(json.loads(aside[0].read_text())["status"], "contaminated")
        self.assertEqual(evaluation.load_run("T-1", "single")["status"], "started")


    def test_a_run_past_the_cap_is_stopped_and_recorded_as_timed_out(self) -> None:
        evaluation, runner, repo, project = self._setup("capped")
        corpus = evaluation.load_corpus()
        corpus["settings"]["max_minutes"] = 0.0001
        evaluation.save_corpus(corpus)
        bin_dir = self._fake_bin()
        # An agent that never finishes and never commits.
        (bin_dir / "claude").write_text("#!/bin/sh\nsleep 60\n")
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}):
            record = runner.start_single("T-1")
        pid = record["pid"]
        os.kill(pid, 0)  # alive
        collected = runner.collect("T-1", "single")
        self.assertEqual(collected["status"], "timed-out")
        self.assertTrue(collected["timed_out"])
        # Nothing committed is no candidate, short base sha or not.
        self.assertIsNone(collected["tip"])
        self.assertIn("120-minute cap", "".join(n["text"] for n in collected["notes"]).replace("0.0001", "120"))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertIn("| T-1 | single | timed-out |", evaluation.report())

        # The delegated arms end their sessions the way `helm worker stop`
        # does, and a helm run's foreman goes with it.
        herdr = FakeHerdr()
        adapter = HerdrAdapter(self.coordinator, herdr)
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}):
            record = runner.start_firstmate("T-1", adapter=adapter)
        task_id = record["task_ids"][0]
        worker = next(w for w in self.state.load()["workers"].values() if w["task_id"] == task_id)
        self.assertEqual(worker["status"], "running")
        collected = runner.collect("T-1", "firstmate", adapter=adapter)
        self.assertEqual(collected["status"], "timed-out")
        self.assertIsNone(collected["tip"])
        self.assertNotEqual(self.state.load()["workers"][worker["id"]]["status"], "running")
