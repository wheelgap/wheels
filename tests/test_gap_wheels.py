"""Offline tests for scripts/gap_wheels.py: the plan, the merge job's checks on untrusted results, and the verdicts."""

import hashlib
import io
import json
import platform
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts import gap_wheels


def row(name, version, target, **over):
    r = {"name": name, "version": version, "target": target, "outcome": "built", "import": "ok", "extensions": 2,
         "gil": "kept-off" if target.split()[0].endswith("t") else None}  # fmt: skip
    r.update(over)
    return r


BUILDS = {
    "results": [
        row("tiktoken", "0.14.0", "3.14 win-arm64"),
        row("lz4", "4.4.5", "3.14t win-arm64"),
        row("zstandard", "0.25.0", "3.14 musl-aarch64"),
        row("broken", "1.0", "3.14 win-arm64", outcome="failed"),
        row("noimport", "1.0", "3.14 win-arm64", **{"import": "failed"}),
        row("pureish", "1.0", "3.14 win-arm64", extensions=0),  # nothing compiled: PyPI's pure wheel would do
        row("gilon", "1.0", "3.14t win-arm64", gil="re-enabled"),
        row("weird", "1.0; rm -rf /", "3.14 win-arm64"),
        row("elsewhere", "1.0", "3.14 linux-x86_64"),  # not a lab target
    ]
}


def good(job, **over):
    platform = {"3.14 win-arm64": "cp314-cp314-win_arm64", "3.14t win-arm64": "cp314-cp314t-win_arm64",
                "3.14 musl-aarch64": "cp314-cp314-musllinux_1_2_aarch64"}[job["target"]]  # fmt: skip
    r = {
        "schema": 1,
        "name": job["name"],
        "version": job["version"],
        "target": job["target"],
        "attempt": job["attempt"],
        "runner": job["runner"],
        "sdist": f"{job['name']}-{job['version']}.tar.gz",
        "sdist_sha256": "a" * 64,
        "wheel": f"{job['name']}-{job['version']}-{platform}.whl",
        "sha256": "b" * 64,
        "installs": True if job["attempt"] == 1 else None,
    }
    r.update(over)
    return r


class Plan(unittest.TestCase):
    def test_only_clean_builds_on_lab_targets(self):
        pairs = {(p["name"], p["target"]) for p in gap_wheels.clean_pairs(BUILDS)}
        self.assertEqual(pairs, {("tiktoken", "3.14 win-arm64"), ("lz4", "3.14t win-arm64"),
                                 ("zstandard", "3.14 musl-aarch64")})  # fmt: skip

    def test_two_builds_per_pair_and_docker_for_alpine(self):
        jobs = gap_wheels.plan(BUILDS, 10)
        self.assertEqual(len(jobs), 6)
        self.assertEqual({(j["id"], j["attempt"]) for j in jobs}, {(i, a) for i in "012" for a in (1, 2)})
        alpine = [j for j in jobs if j["target"] == "3.14 musl-aarch64"]
        self.assertTrue(alpine and all(j["image"].startswith("python:3.14-alpine@sha256:") for j in alpine))

    def test_linux_targets_build_in_a_pinned_image_to_one_exact_platform(self):
        for target, spec in gap_wheels.build_lab.TARGETS.items():
            with self.subTest(target):
                if "linux" not in spec["platform"]:
                    self.assertNotIn("image", spec)
                    continue
                self.assertRegex(spec["image"], r"@sha256:[0-9a-f]{64}$")  # by digest, never a moving tag
                self.assertTrue(spec["interpreter"])
                musl = spec.get("musl") == "yes"
                self.assertRegex(spec["plat"], r"^musllinux_1_2_" if musl else r"^manylinux_2_28_")
                self.assertRegex(spec["plat"], spec["wheel"])
                if not musl:
                    self.assertTrue(spec["image"].startswith(f"quay.io/pypa/{spec['plat']}@sha256:"))
                    abi = "cp" + spec["python"].replace(".", "")
                    self.assertEqual(spec["interpreter"], f"/opt/python/{abi.removesuffix('t')}-{abi}/bin/python")

    def test_max_and_only(self):
        self.assertEqual(len(gap_wheels.plan(BUILDS, 1)), 2)
        self.assertEqual({j["name"] for j in gap_wheels.plan(BUILDS, 10, {"lz4"})}, {"lz4"})


class Verify(unittest.TestCase):
    def setUp(self):
        self.jobs = gap_wheels.plan(BUILDS, 1, {"tiktoken"})
        self.published = {}
        self.one, self.two = self.jobs

    def verdict(self, first, second):
        results = {}
        if first is not None:
            results[("0", 1)] = first
        if second is not None:
            results[("0", 2)] = second
        published = {("tiktoken", "0.14.0"): {"tiktoken-0.14.0.tar.gz": "a" * 64}}
        published |= self.published
        verified, report = gap_wheels.verify(self.jobs, results, lambda name, version: published[(name, version)])
        return verified, report[0]["verdict"]

    def test_identical_and_installs(self):
        verified, verdict = self.verdict(good(self.one), good(self.two))
        self.assertEqual(verdict, "verified")
        self.assertEqual(verified[0]["wheel"], "tiktoken-0.14.0-cp314-cp314-win_arm64.whl")
        self.assertEqual(verified[0]["sha256"], "b" * 64)

    def test_builds_differ(self):
        self.assertEqual(self.verdict(good(self.one), good(self.two, sha256="c" * 64))[1], "the two builds differ")

    def test_different_sdists(self):
        self.assertEqual(self.verdict(good(self.one), good(self.two, sdist_sha256="c" * 64))[1],
                         "the two builds differ")  # fmt: skip

    def test_doesnt_install(self):
        self.assertEqual(self.verdict(good(self.one, installs=False), good(self.two))[1],
                         "pip couldn't install it without compiling")  # fmt: skip

    def test_the_sdist_must_be_pypis(self):
        both = (good(self.one, sdist_sha256="c" * 64), good(self.two, sdist_sha256="c" * 64))
        self.assertEqual(self.verdict(*both)[1], "the sdist isn't the one PyPI publishes")
        self.published = {("tiktoken", "0.14.0"): None}
        self.assertEqual(self.verdict(good(self.one), good(self.two))[1], "couldn't check the sdist against PyPI")

    def test_a_wheel_without_its_sdist_is_rejected(self):
        for over in ({"sdist": None}, {"sdist_sha256": None}):
            with self.subTest(over):
                verdict = self.verdict(good(self.one, **over), good(self.two, **over))[1]
                self.assertTrue(verdict.startswith("rejected"), verdict)

    def test_normalized_versions_and_compressed_tags(self):
        for wheel in ("tiktoken-0.14.0-cp314-cp314-win_arm64.win_amd64.whl", "tiktoken-0.14.0-cp314-cp314-WIN_ARM64.whl"):
            with self.subTest(wheel):
                self.assertTrue(self.verdict(good(self.one, wheel=wheel), good(self.two, wheel=wheel))[1].startswith(
                    "verified" if "win_amd64" in wheel else "rejected"))  # fmt: skip
        self.jobs[0]["version"] = self.jobs[1]["version"] = "0.14.0RC1"
        wheel = "tiktoken-0.14.0rc1-cp314-cp314-win_arm64.whl"
        published = {("tiktoken", "0.14.0RC1"): {"tiktoken-0.14.0RC1.tar.gz": "a" * 64}}
        results = {("0", 1): good(self.jobs[0], wheel=wheel, sdist="tiktoken-0.14.0RC1.tar.gz"),
                   ("0", 2): good(self.jobs[1], wheel=wheel, sdist="tiktoken-0.14.0RC1.tar.gz")}  # fmt: skip
        report = gap_wheels.verify(self.jobs, results, lambda n, v: published[(n, v)])[1]
        self.assertEqual(report[0]["verdict"], "verified")

    def test_missing_result(self):
        self.assertEqual(self.verdict(good(self.one), None)[1], "a build has no result")

    def test_no_wheel(self):
        self.assertEqual(self.verdict(good(self.one), good(self.two, wheel=None, sha256=None))[1], "a build made no wheel")

    def test_a_result_for_another_job_is_rejected(self):
        for over in ({"name": "numpy"}, {"version": "0.13.0"}, {"target": "3.14t win-arm64"}, {"attempt": 1},
                     {"runner": "ubuntu-24.04"}):  # fmt: skip
            with self.subTest(over):
                verified, verdict = self.verdict(good(self.one), good(self.two, **over))
                self.assertTrue(verdict.startswith("rejected"), verdict)
                self.assertEqual(verified, [])

    def test_a_wheel_for_another_project_version_or_platform_is_rejected(self):
        for wheel in ("numpy-0.14.0-cp314-cp314-win_arm64.whl", "tiktoken-0.13.0-cp314-cp314-win_arm64.whl",
                      "tiktoken-0.14.0-cp314-cp314-win_amd64.whl", "tiktoken-0.14.0-py3-none-any.whl",
                      "../evil.whl", "tiktoken.whl"):  # fmt: skip
            with self.subTest(wheel):
                verdict = self.verdict(good(self.one, wheel=wheel), good(self.two, wheel=wheel))[1]
                self.assertTrue(verdict.startswith("rejected"), verdict)

    def test_malformed_results_are_rejected(self):
        for bad in ("not JSON", [], {**good(self.two), "extra": 1}, good(self.two, sha256="B" * 64),
                    good(self.two, installs="yes"), good(self.two, installs=True), good(self.two, schema=2),
                    good(self.two, sdist="x;y.tar.gz"), good(self.two, sha256=None)):  # fmt: skip
            with self.subTest(bad):
                self.assertTrue(self.verdict(good(self.one), bad)[1].startswith("rejected"))


class Merge(unittest.TestCase):
    def test_writes_manifest_and_summary(self):
        jobs = gap_wheels.plan(BUILDS, 2)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "plan.json").write_text(json.dumps(jobs))
            for job in jobs:
                d = tmp / "results" / f"result-{job['id']}-{job['attempt']}"
                d.mkdir(parents=True)
                sha = "c" * 64 if job["id"] == "1" and job["attempt"] == 2 else "b" * 64
                (d / "result.json").write_text(json.dumps(good(job, sha256=sha)))
            big = tmp / "results" / "result-9-1"
            big.mkdir()
            (big / "result.json").write_text("x" * 20_000)  # not in the plan: never read
            sdists = {"lz4-4.4.5.tar.gz": "a" * 64, "tiktoken-0.14.0.tar.gz": "a" * 64}
            with mock.patch.object(gap_wheels, "pypi_sdists", lambda name, version: sdists):
                gap_wheels.main(["merge", str(tmp / "results"), "--plan", str(tmp / "plan.json"), "--out",
                                 str(tmp / "manifest.json"), "--summary", str(tmp / "summary.md")])  # fmt: skip
            manifest = json.loads((tmp / "manifest.json").read_text())
            summary = (tmp / "summary.md").read_text()
        self.assertEqual([w["name"] for w in manifest["wheels"]], ["lz4"])
        self.assertIn("**1 verified** of 2 pairs", summary)
        self.assertIn("the two builds differ", summary)

    def test_an_oversized_result_is_not_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            path.write_text(" " * 20_000 + "{}")
            self.assertEqual(gap_wheels.read_result(path), "too big")


class Unpack(unittest.TestCase):
    def test_refuses_paths_outside(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            sdist = tmp / "evil-1.0.tar.gz"
            with tarfile.open(sdist, "w:gz") as t:
                info = tarfile.TarInfo("../outside.txt")
                info.size = 1
                t.addfile(info, io.BytesIO(b"x"))
            with self.assertRaises(tarfile.TarError):
                gap_wheels.unpack(sdist, tmp / "dest")
            self.assertFalse((tmp / "outside.txt").exists())
            z = tmp / "evil-1.0.zip"
            with zipfile.ZipFile(z, "w") as f:
                f.writestr("../outside.txt", "x")
            with self.assertRaises(ValueError):
                gap_wheels.unpack(z, tmp / "dest")

    def test_one_top_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            z = tmp / "ok-1.0.zip"
            with zipfile.ZipFile(z, "w") as f:
                f.writestr("ok-1.0/setup.py", "")
            self.assertEqual(gap_wheels.unpack(z, tmp / "dest").name, "ok-1.0")


SHA = "1" * 40


def build_run(job, batch="99-1", **over):
    run = {"id": 7, "display_title": gap_wheels.title(batch, job), "event": "workflow_dispatch", "head_branch": "main",
           "head_sha": SHA, "path": ".github/workflows/gap-wheel-build.yml", "created_at": "2026-11-16T02:00:05Z",
           "status": "completed"}  # fmt: skip
    run.update(over)
    return run


class Runs(unittest.TestCase):
    def setUp(self):
        self.jobs = gap_wheels.plan(BUILDS, 1, {"tiktoken"})
        self.one = self.jobs[0]

    def pick(self, runs):
        return gap_wheels.pick_runs(self.jobs, runs, "99-1", SHA, "2026-11-16T02:00:00Z")[("0", 1)]

    def test_title_matches_the_build_workflows_run_name(self):
        workflow = (Path(__file__).parent.parent / ".github/workflows/gap-wheel-build.yml").read_text()
        self.assertIn("run-name: gap-wheel ${{ inputs.batch }} ${{ inputs.name }} ${{ inputs.version }} "
                      "${{ inputs.target }} build ${{ inputs.attempt }}", workflow)  # fmt: skip
        self.assertEqual(gap_wheels.title("99-1", self.one), "gap-wheel 99-1 tiktoken 0.14.0 3.14 win-arm64 build 1")

    def test_the_one_run_this_batch_started(self):
        self.assertEqual(self.pick([build_run(self.one)])["id"], 7)

    def test_a_run_another_job_made_doesnt_count(self):
        for over in ({"event": "push"}, {"head_branch": "evil"}, {"head_sha": "2" * 40},
                     {"path": ".github/workflows/other.yml"}, {"created_at": "2026-11-16T01:59:59Z"}):  # fmt: skip
            with self.subTest(over):
                self.assertEqual(self.pick([build_run(self.one, **over)]), "the build run isn't one this batch started")

    def test_two_runs_with_one_title_both_lose(self):
        self.assertEqual(self.pick([build_run(self.one), build_run(self.one, id=8)]),
                         "more than one build run with its title")  # fmt: skip

    def test_another_batch_or_attempt_isnt_this_one(self):
        self.assertEqual(self.pick([build_run(self.one, batch="98-1"), build_run(self.jobs[1])]), "no build run")

    def test_unfinished(self):
        self.assertEqual(self.pick([build_run(self.one, status="in_progress")]), "the build run hasn't finished")

    def test_fetch_result_reads_only_a_small_result_json(self):
        def zipped(files):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for name, data in files.items():
                    z.writestr(name, data)
            return buf.getvalue()

        listing = json.dumps({"artifacts": [{"id": 5, "name": "result", "size_in_bytes": 300}]}).encode()
        for archive, expected in ((zipped({"result.json": "{}"}), (b"{}", None)), (zipped({"other.json": "{}"}), None),
                                  (zipped({"result.json": "{}", "members.json": "[]"}), (b"{}", b"[]")),
                                  (zipped({"result.json": "{}", "members.json": "x" * 2_000_001}), (b"{}", None)),
                                  (zipped({"result.json": "x" * 20_000}), None), (b"not a zip", None)):  # fmt: skip
            with self.subTest(expected), mock.patch.object(gap_wheels, "gh", side_effect=[listing, archive]):
                self.assertEqual(gap_wheels.fetch_result("o/r", {"id": 7}), expected)
        two = json.dumps({"artifacts": [{"id": 5, "name": "result"}, {"id": 6, "name": "result"}]}).encode()
        with mock.patch.object(gap_wheels, "gh", side_effect=[two]):
            self.assertIsNone(gap_wheels.fetch_result("o/r", {"id": 7}))

    def test_collect_writes_results_and_run_urls_for_merge(self):
        runs = [build_run(job, id=10 + job["attempt"]) for job in self.jobs]
        results = {11: good(self.jobs[0]), 12: good(self.jobs[1])}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "plan.json").write_text(json.dumps(self.jobs))
            with (
                mock.patch.object(gap_wheels, "list_runs", return_value=runs),
                mock.patch.object(gap_wheels, "fetch_result", lambda repo, run: (json.dumps(results[run["id"]]).encode(), b"{}")),
            ):
                gap_wheels.main(["collect", "--plan", str(tmp / "plan.json"), "--batch", "99-1", "--repo", "o/r",
                                 "--sha", SHA, "--since", "2026-11-16T02:00:00Z", "--results", str(tmp / "r")])  # fmt: skip
            self.assertEqual(json.loads((tmp / "r" / "runs.json").read_text()),
                             {"0-1": "https://github.com/o/r/actions/runs/11",
                              "0-2": "https://github.com/o/r/actions/runs/12"})  # fmt: skip
            self.assertEqual(json.loads((tmp / "r" / "result-0-2" / "result.json").read_text()), results[12])
            with mock.patch.object(gap_wheels, "pypi_sdists", lambda n, v: {"tiktoken-0.14.0.tar.gz": "a" * 64}):
                gap_wheels.main(["merge", str(tmp / "r"), "--plan", str(tmp / "plan.json"), "--out", str(tmp / "m.json")])
            wheel = json.loads((tmp / "m.json").read_text())["wheels"][0]
        self.assertEqual(wheel["builds"], ["https://github.com/o/r/actions/runs/11", "https://github.com/o/r/actions/runs/12"])

    def test_collect_refuses_odd_arguments(self):
        for flag, value in (("--batch", "1; rm"), ("--since", "yesterday"), ("--repo", "o/r?x"), ("--sha", "abc")):
            args = {"--batch": "99-1", "--since": "2026-11-16T02:00:00Z", "--repo": "o/r", "--sha": SHA, flag: value}
            with self.subTest(flag), self.assertRaises(SystemExit):
                gap_wheels.main(["collect", "--plan", "x", "--results", "r", *[x for kv in args.items() for x in kv]])


class Members(unittest.TestCase):
    def test_wheel_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "x-1.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as z:
                z.writestr("x/__init__.py", "")
                z.writestr("x/", "")
            self.assertEqual(gap_wheels.wheel_members(wheel), {"x/__init__.py": hashlib.sha256(b"").hexdigest()})

    def test_differing_names_the_files(self):
        a, b, c = "a" * 64, "b" * 64, "c" * 64
        first = {"pkg/_x.so": a, "pkg/__init__.py": b, "pkg-1.dist-info/RECORD": c}
        second = {"pkg/_x.so": b, "pkg/__init__.py": b, "pkg-1.dist-info/RECORD": a, "pkg/extra.py": a}
        self.assertEqual(gap_wheels.differing(first, second),
                         ", in `pkg-1.dist-info/RECORD`, `pkg/_x.so`, `pkg/extra.py`")  # fmt: skip
        many = {f"f{i}": a for i in range(8)}
        self.assertTrue(gap_wheels.differing(many, {}).endswith("(+3 more)"))

    def test_an_unreadable_wheel_has_no_members_but_still_a_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "x-1.0-py3-none-any.whl"
            wheel.write_bytes(b"not a zip")
            self.assertEqual(gap_wheels.wheel_members(wheel), {})

    def test_deeply_nested_members_json_doesnt_crash_merge(self):
        jobs = gap_wheels.plan(BUILDS, 1, {"tiktoken"})
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "plan.json").write_text(json.dumps(jobs))
            for job in jobs:
                d = tmp / "r" / f"result-{job['id']}-{job['attempt']}"
                d.mkdir(parents=True)
                (d / "result.json").write_text(json.dumps(good(job)))
                (d / "members.json").write_text("[" * 200_000 + "]" * 200_000)
            with mock.patch.object(gap_wheels, "pypi_sdists", lambda n, v: {"tiktoken-0.14.0.tar.gz": "a" * 64}):
                gap_wheels.main(["merge", str(tmp / "r"), "--plan", str(tmp / "plan.json"), "--out", str(tmp / "m.json")])
            self.assertEqual(len(json.loads((tmp / "m.json").read_text())["wheels"]), 1)

    def test_differing_ignores_untrusted_junk(self):
        good = {"pkg/_x.so": "a" * 64}
        for bad in (None, [], {"a|b": "a" * 64}, {"x": "nope"}, {"`x`": "a" * 64}, {"x" * 201: "a" * 64}):
            with self.subTest(bad):
                self.assertEqual(gap_wheels.differing(good, bad), "")

    def test_merge_reports_which_files_differ(self):
        jobs = gap_wheels.plan(BUILDS, 1, {"tiktoken"})
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "plan.json").write_text(json.dumps(jobs))
            for job, sha, so in ((jobs[0], "b" * 64, "1" * 64), (jobs[1], "c" * 64, "2" * 64)):
                d = tmp / "r" / f"result-{job['id']}-{job['attempt']}"
                d.mkdir(parents=True)
                (d / "result.json").write_text(json.dumps(good(job, sha256=sha)))
                (d / "members.json").write_text(json.dumps({"tiktoken/_tiktoken.pyd": so, "tiktoken/core.py": "3" * 64}))
            with mock.patch.object(gap_wheels, "pypi_sdists", lambda n, v: {"tiktoken-0.14.0.tar.gz": "a" * 64}):
                gap_wheels.main(["merge", str(tmp / "r"), "--plan", str(tmp / "plan.json"), "--out", str(tmp / "m.json"),
                                 "--summary", str(tmp / "s.md")])  # fmt: skip
            self.assertIn("the two builds differ, in `tiktoken/_tiktoken.pyd` |", (tmp / "s.md").read_text())


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.jobs = gap_wheels.plan(BUILDS, 2)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plan = Path(self.tmp.name) / "plan.json"
        self.plan.write_text(json.dumps(self.jobs))
        self.calls = []
        sleep = mock.patch.object(gap_wheels.time, "sleep", lambda s: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def run_dispatch(self, tips, runs=list):
        def gh(args):
            self.calls.append(args)
            return (next(tips) + "\n").encode() if args[0] == "api" else b""

        with mock.patch.object(gap_wheels, "gh", gh), mock.patch.object(gap_wheels, "list_runs", lambda r, s: runs()):
            gap_wheels.main(["dispatch", "--plan", str(self.plan), "--batch", "99-1", "--repo", "o/r", "--sha", SHA,
                             "--since", "2026-11-16T02:00:00Z", "--parallel", "2"])  # fmt: skip

    def started(self):
        return [c for c in self.calls if c[:2] == ["workflow", "run"]]

    def test_starts_one_run_per_build_from_main_with_the_plans_values(self):
        self.run_dispatch(iter([SHA] * 4))
        started = self.started()
        self.assertEqual(len(started), 4)
        self.assertIn("main", started[0])
        self.assertIn("target=3.14t win-arm64", started[0])
        self.assertIn("batch=99-1", started[0])

    def test_an_image_build_gets_the_images_interpreter(self):
        jobs = [j for j in gap_wheels.plan(BUILDS, 10) if j["target"] == "3.14 musl-aarch64"]
        self.plan.write_text(json.dumps(jobs))
        self.run_dispatch(iter([SHA] * len(jobs)))
        started = self.started()
        self.assertTrue(started)
        for command in started:
            self.assertIn("python=python", command)  # Alpine's own python, not setup-python's "3.14"
            self.assertTrue(any(x.startswith("image=python:3.14-alpine@sha256:") for x in command))

    def test_stops_loudly_when_main_moves(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_dispatch(iter([SHA, SHA, "2" * 40]))
        self.assertIn("after 2 of 4", str(raised.exception))
        self.assertEqual(len(self.started()), 2)

    def test_waits_while_the_batch_has_parallel_builds_running(self):
        busy = [{"display_title": "gap-wheel 99-1 a 1 t build 1", "status": "in_progress"}] * 2
        polls = iter([[], [], busy, busy, [], [], []])
        self.run_dispatch(iter([SHA] * 4), runs=lambda: next(polls))
        self.assertEqual(len(self.started()), 4)

    def test_a_start_that_failed_but_made_the_run_isnt_repeated(self):
        made = []

        def gh(args):
            if args[:2] == ["workflow", "run"]:
                made.append(args)
                raise subprocess.CalledProcessError(502, "gh")
            return (SHA + "\n").encode()

        def runs(repo, since):
            return [{"display_title": gap_wheels.title("99-1", j), "status": "queued"} for j in self.jobs[: len(made)]]

        with mock.patch.object(gap_wheels, "gh", gh), mock.patch.object(gap_wheels, "list_runs", runs):
            gap_wheels.main(["dispatch", "--plan", str(self.plan), "--batch", "99-1", "--repo", "o/r", "--sha", SHA,
                             "--since", "2026-11-16T02:00:00Z", "--parallel", "10"])  # fmt: skip
        self.assertEqual(len(made), 4)  # one POST per build, none repeated

    def test_retry(self):
        calls = iter([subprocess.CalledProcessError(1, "gh"), b"ok"])

        def flaky():
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value

        self.assertEqual(gap_wheels.retry(flaky), b"ok")
        with self.assertRaises(subprocess.CalledProcessError):
            gap_wheels.retry(lambda: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "gh")), attempts=2)


class Rotation(unittest.TestCase):
    def test_a_capped_batch_reaches_every_pair_in_turn(self):
        names = lambda start: [j["name"] for j in gap_wheels.plan(BUILDS, 1, start=start) if j["attempt"] == 1]
        everyone = {p["name"] for p in gap_wheels.clean_pairs(BUILDS)}
        self.assertEqual({n for week in range(len(everyone)) for n in names(week)}, everyone)
        self.assertEqual(names(0), names(len(everyone)))  # wraps around

    def test_members_too_big_to_list_still_leave_the_result(self):
        def fake_run_build(name, version, attempt, work, members, keep=None, plat=None, tag=None):
            members.update({f"pkg/f{i}.py": "a" * 64 for i in range(gap_wheels.MAX_MEMBERS + 1)})
            return {"sdist": None, "sdist_sha256": None, "wheel": None, "sha256": None, "installs": None}

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "result.json"
            with (
                mock.patch.object(gap_wheels, "run_build", fake_run_build),
                mock.patch.object(gap_wheels.build_lab, "interpreter_problem", lambda target: None),
            ):
                gap_wheels.main(["build", "--name", "tiktoken", "--version", "0.14.0", "--target", "3.14 win-arm64",
                                 "--attempt", "1", "--runner", "windows-11-arm", "--out", str(out)])  # fmt: skip
            self.assertEqual(json.loads(out.read_text())["name"], "tiktoken")
            self.assertEqual(json.loads((Path(tmp) / "members.json").read_text()), {})



# --- stage, install-test and publish -------------------------------------------------------------------------------

WIN = "tiktoken-0.14.0-cp314-cp314-win_arm64.whl"
GLIBC_T = "lz4-0.14.0-cp314-cp314t-manylinux_2_28_x86_64.whl"
MUSL = "lz4-0.14.0-cp314-cp314-musllinux_1_2_aarch64.whl"


def verified(name="tiktoken", wheel=WIN, sha="c" * 64, target="3.14 win-arm64"):
    return {"name": name, "version": "0.14.0", "target": target, "wheel": wheel, "sha256": sha,
            "sdist": f"{name}-0.14.0.tar.gz", "sdist_sha256": "a" * 64,
            "builds": ["https://github.com/wheelgap/tracker/actions/runs/11",
                       "https://github.com/wheelgap/tracker/actions/runs/12"]}  # fmt: skip


MIT = {"license_expression": "MIT", "requires_python": ">=3.9"}


def zipped(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


class Stage(unittest.TestCase):
    def test_only_wheels_with_their_tag_a_licence_and_no_opt_out(self):
        self.assertIsNone(gap_wheels.stage_reason(verified(), MIT, set()))
        linux = verified(wheel="lz4-4.4.5-cp314-cp314-linux_aarch64.whl", target="3.14 musl-aarch64")
        self.assertEqual(gap_wheels.stage_reason(linux, MIT, set()), "not tagged exactly "
                         "cp314-cp314-musllinux_1_2_aarch64 (a linux_* wheel is one auditwheel didn't repair)")  # fmt: skip
        for wheel, target in ((MUSL, "3.14 musl-aarch64"), (GLIBC_T, "3.14t linux-x86_64"),
                              ("lz4-4.4.5-cp315-cp315-manylinux_2_28_x86_64.whl", "3.15 linux-x86_64")):  # fmt: skip
            with self.subTest(wheel):
                self.assertIsNone(gap_wheels.stage_reason(verified(name="lz4", wheel=wheel, target=target), MIT, set()))
        older = verified(name="lz4", wheel="lz4-4.4.5-cp314-cp314t-manylinux_2_17_x86_64.whl", target="3.14t linux-x86_64")
        self.assertEqual(gap_wheels.stage_reason(older, MIT, set()), "not tagged exactly cp314-cp314t-manylinux_2_28_x86_64")
        mixed = verified(name="lz4", wheel=GLIBC_T, target="3.14t linux-x86_64")
        self.assertEqual(gap_wheels.stage_reason(mixed, MIT, {"lz4"}), "the project opted out")
        self.assertEqual(gap_wheels.stage_reason(verified(), MIT, {"tiktoken"}), "the project opted out")
        self.assertEqual(gap_wheels.stage_reason(verified(), None, set()), "couldn't ask PyPI for the licence")
        self.assertIn("licence", gap_wheels.stage_reason(verified(), {"license": "see LICENSE"}, set()))
        plus = verified(wheel="tiktoken-0.14.0+local-cp314-cp314-win_arm64.whl")
        self.assertIn("release asset", gap_wheels.stage_reason(plus, MIT, set()))

    def test_only_the_targets_one_exact_tag(self):
        self.assertTrue(gap_wheels.tag_ok("x-1.0-cp314-cp314-win_arm64.whl", "3.14 win-arm64"))
        self.assertTrue(gap_wheels.tag_ok("x-1.0-cp314-cp314t-win_arm64.whl", "3.14t win-arm64"))
        for wheel, target in (
            ("x-1.0-cp314-cp314-win_arm64.win_amd64.whl", "3.14 win-arm64"),  # an ARM64 .pyd on x64
            ("x-1.0-cp314-cp314-win_arm64.linux_aarch64.whl", "3.14 win-arm64"),
            ("x-1.0-cp38-abi3-win_arm64.whl", "3.14 win-arm64"),  # other Pythons would take it untested
            ("x-1.0-py3-none-win_arm64.whl", "3.14 win-arm64"),
            ("x-1.0-cp314-cp314-win_arm64.whl", "3.14t win-arm64"),
            ("x-1.0-cp314-cp314t-linux_x86_64.whl", "3.14t linux-x86_64"),  # pip wheel's tag: any libc, any version
            ("x-1.0-cp314-cp314-WIN_ARM64.whl", "3.14 win-arm64"),
            # Linux: only auditwheel's one repaired tag, for that target's Python, libc and architecture.
            ("x-1.0-cp314-cp314t-manylinux_2_28_x86_64.manylinux2014_x86_64.whl", "3.14t linux-x86_64"),
            ("x-1.0-cp314-cp314t-manylinux2014_x86_64.manylinux_2_28_x86_64.whl", "3.14t linux-x86_64"),
            ("x-1.0-cp314-cp314t-manylinux_2_17_x86_64.whl", "3.14t linux-x86_64"),  # a glibc it wasn't checked for
            ("x-1.0-cp314-cp314t-manylinux2014_x86_64.whl", "3.14t linux-x86_64"),
            ("x-1.0-cp314-cp314t-manylinux_2_34_x86_64.whl", "3.14t linux-x86_64"),
            ("x-1.0-cp314-cp314-manylinux_2_28_x86_64.whl", "3.14t linux-x86_64"),  # the GIL build
            ("x-1.0-cp314-cp314t-manylinux_2_28_aarch64.whl", "3.14t linux-x86_64"),
            ("x-1.0-cp314-cp314t-musllinux_1_2_x86_64.whl", "3.14t linux-x86_64"),
            ("x-1.0-cp314-cp314-musllinux_1_1_aarch64.whl", "3.14 musl-aarch64"),
            ("x-1.0-cp314-cp314-musllinux_1_2_aarch64.musllinux_1_1_aarch64.whl", "3.14 musl-aarch64"),
            ("x-1.0-cp314-cp314-manylinux_2_28_aarch64.whl", "3.14 musl-aarch64"),
            ("x-1.0-cp314-cp314-linux_aarch64.whl", "3.14 musl-aarch64"),
            ("x-1.0-cp315-cp315-linux_x86_64.whl", "3.15 linux-x86_64"),
            ("x-1.0-cp315-abi3-manylinux_2_28_x86_64.whl", "3.15 linux-x86_64"),
            ("x-1.0-cp314-cp314t-manylinux_2_28_x86_64.whl", "3.15 linux-x86_64"),
        ):
            with self.subTest(wheel):
                self.assertFalse(gap_wheels.tag_ok(wheel, target))
        for wheel, target in (("x-1.0-cp314-cp314t-manylinux_2_28_x86_64.whl", "3.14t linux-x86_64"),
                              ("x-1.0-cp314-cp314-musllinux_1_2_aarch64.whl", "3.14 musl-aarch64"),
                              ("x-1.0-cp315-cp315-manylinux_2_28_x86_64.whl", "3.15 linux-x86_64")):  # fmt: skip
            with self.subTest(wheel):
                self.assertTrue(gap_wheels.tag_ok(wheel, target))

    def test_each_linux_tag_is_the_one_its_build_repairs_to_with_a_policy_and_a_test_image(self):
        linux = [t for t, spec in gap_wheels.build_lab.TARGETS.items() if "plat" in spec]
        self.assertEqual(sorted(linux), sorted(t for t in gap_wheels.PUBLISH_TAGS if t in linux))  # every one
        for target in linux:
            with self.subTest(target):
                spec = gap_wheels.build_lab.TARGETS[target]
                self.assertTrue(gap_wheels.PUBLISH_TAGS[target].endswith("-" + spec["plat"]))
                self.assertIn(spec["plat"], gap_wheels.LINUX_POLICIES)
                test = gap_wheels.LINUX_TESTS[target]
                self.assertRegex(test["image"], r"^[a-z0-9.:/_-]+@sha256:[0-9a-f]{64}$")  # pinned by digest
                self.assertEqual(test["runner"], "ubuntu-24.04-arm" if target.endswith("aarch64") else "ubuntu-latest")
                if "python_url" in test:
                    self.assertTrue(test["python_url"].startswith("https://github.com/astral-sh/python-build-standalone/releases/download/"))
                    self.assertRegex(test["python_sha256"], r"^[0-9a-f]{64}$")
                    self.assertTrue(test["interpreter"].startswith("/opt/python/"))
        self.assertEqual(gap_wheels.LINUX_POLICIES["manylinux_2_28_x86_64"]["machine"], 62)
        self.assertEqual(gap_wheels.LINUX_POLICIES["musllinux_1_2_aarch64"]["machine"], 183)
        self.assertEqual(gap_wheels.REPAIR_TOOLS[0], "auditwheel==6.8.2")  # the version the policies are copied from

    def test_licence(self):
        self.assertEqual(gap_wheels.licence({"license_expression": "Apache-2.0 OR MIT"}), "Apache-2.0 OR MIT")
        self.assertIsNone(gap_wheels.licence({"license_expression": "LicenseRef-Proprietary"}))
        self.assertIsNone(gap_wheels.licence({"license_expression": "MIT<script>"}))
        for not_osi in ("BUSL-1.1", "CC-BY-NC-4.0", "SSPL-1.0", "Elastic-2.0", "MIT AND BUSL-1.1", "CC0-1.0",
                        "MIT WITH BUSL-1.1", "Apache-2.0 WITH LicenseRef-NC", "AND", "MIT AND", "(MIT", "MIT)",
                        "MIT OR OR Apache-2.0"):  # fmt: skip
            with self.subTest(not_osi):
                self.assertIsNone(gap_wheels.licence({"license_expression": not_osi}))
        for osi in ("(MIT OR Apache-2.0) AND BSD-3-Clause", "GPL-2.0-or-later WITH Classpath-exception-2.0",
                    "apache-2.0", "LGPL-2.1+"):  # fmt: skip
            with self.subTest(osi):
                self.assertEqual(gap_wheels.licence({"license_expression": osi}), osi)
        osi = {"classifiers": ["License :: OSI Approved :: BSD License", "Programming Language :: Python"]}
        self.assertEqual(gap_wheels.licence(osi), "BSD License")
        # The legacy field: one line, an SPDX expression or an unambiguous name.
        self.assertEqual(gap_wheels.licence({"license": "BSD-3-Clause", "classifiers": []}), "BSD-3-Clause")
        self.assertEqual(gap_wheels.licence({"license": " Apache License 2.0 "}), "Apache-2.0")
        for name, spdx in gap_wheels.LICENCE_NAMES.items():
            self.assertEqual(gap_wheels.licence({"license": name.upper()}), spdx)
        self.assertEqual(gap_wheels.licence({"license": "MIT\tOR  Apache-2.0"}), "MIT OR Apache-2.0")
        self.assertEqual(gap_wheels.licence({"license_expression": "MIT AND PSF-2.0"}), "MIT AND PSF-2.0")
        for refused in ("MIT License\n\nCopyright (c) 2022 Someone\nPermission is hereby granted...",
                        "Apache License\n  Version 2.0, January 2004", "BSD", "Proprietary", "",
                        "see LICENSE", "MIT-ish", "x" * 150, "MIT License, no commercial use",
                        "PSF-2.0 OR Proprietary", "MIT/Apache-2.0"):  # fmt: skip
            with self.subTest(refused):
                self.assertIsNone(gap_wheels.licence({"license": refused}))
        self.assertIsNone(gap_wheels.licence("nope"))

    def test_unwrap_takes_only_the_verified_bytes(self):
        data = b"wheel bytes"
        sha = hashlib.sha256(data).hexdigest()
        self.assertEqual(gap_wheels.unwrap_wheel(zipped({WIN: data}), WIN, sha), data)
        self.assertIsNone(gap_wheels.unwrap_wheel(zipped({WIN: b"other"}), WIN, sha))  # substituted bytes
        self.assertIsNone(gap_wheels.unwrap_wheel(zipped({WIN: data, "extra.txt": b""}), WIN, sha))
        self.assertIsNone(gap_wheels.unwrap_wheel(zipped({"other.whl": data}), WIN, sha))
        self.assertIsNone(gap_wheels.unwrap_wheel(b"not a zip", WIN, sha))

    def test_stage_writes_the_files_and_outputs(self):
        data = b"wheel bytes"
        good_one = verified(sha=hashlib.sha256(data).hexdigest())
        linux = verified(name="lz4", wheel="lz4-4.4.5-cp314-cp314-linux_aarch64.whl", target="3.14 musl-aarch64")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "m.json").write_text(json.dumps({"date": "2026-11-16", "run": "R", "wheels": [good_one, linux]}))
            out = tmp / "out.txt"
            args = gap_wheels.main.__globals__["argparse"].Namespace(
                manifest=str(tmp / "m.json"), staged=str(tmp / "staged"), repo="wheelgap/tracker",
                optout=str(tmp / "none"), summary=None, github_output=str(out))  # fmt: skip
            with mock.patch.object(gap_wheels, "pypi_info", return_value=MIT), \
                 mock.patch.object(gap_wheels, "missing_dlls", return_value=[]), \
                 mock.patch.object(gap_wheels, "fetch_wheel", return_value=zipped({WIN: data})) as fetch:  # fmt: skip
                gap_wheels.cmd_stage(args)
            fetch.assert_called_once_with("wheelgap/tracker", 11)  # build 1's run
            self.assertEqual((tmp / "staged" / WIN).read_bytes(), data)
            lines = dict(line.split("=", 1) for line in out.read_text().splitlines())
            staged = json.loads(lines["staged"])
            self.assertEqual([w["wheel"] for w in staged], [WIN])
            self.assertEqual(staged[0]["licence"], "MIT")
            self.assertEqual(staged[0]["requires_python"], ">=3.9")
            self.assertEqual(gap_wheels.trusted_staged(lines["staged"]), staged)
            self.assertEqual(json.loads(lines["tests"]), [{"target": "3.14 win-arm64", "runner": "windows-11-arm",
                                                           "python": "3.14", "slug": "3.14-win-arm64", "image": "",
                                                           "interpreter": "", "python_url": "",
                                                           "python_sha256": ""}])  # fmt: skip

    def test_a_linux_wheel_is_staged_after_its_elf_check_and_tested_in_a_container(self):
        ext = elf(["libc.so.6", "libgomp-24e2ab19.so.1.0.0"], {"libc.so.6": ["GLIBC_2.2.5", "GLIBC_2.17"]},
                  runpath="$ORIGIN/../lz4.libs")
        lib = elf(["libc.so.6"], {"libc.so.6": ["GLIBC_2.28"]})
        ok = whl({"lz4/_block.cpython-314t-x86_64-linux-gnu.so": ext, "lz4.libs/libgomp-24e2ab19.so.1.0.0": lib})
        new = whl({"lz4/_block.cpython-314t-x86_64-linux-gnu.so": elf(["libc.so.6"], {"libc.so.6": ["GLIBC_2.34"]})})
        musl = whl({"lz4/_block.cpython-314-aarch64-linux-musl.so": elf(["libc.musl-aarch64.so.1"], machine=183)})
        wheels = [verified(name="lz4", wheel=GLIBC_T, sha=hashlib.sha256(ok).hexdigest(), target="3.14t linux-x86_64"),
                  verified(name="zstandard", wheel="zstandard-0.14.0-cp314-cp314t-manylinux_2_28_x86_64.whl",
                           sha=hashlib.sha256(new).hexdigest(), target="3.14t linux-x86_64"),
                  verified(name="lz4", wheel=MUSL, sha=hashlib.sha256(musl).hexdigest(), target="3.14 musl-aarch64")]  # fmt: skip
        archives = {GLIBC_T: zipped({GLIBC_T: ok}), MUSL: zipped({MUSL: musl}),
                    wheels[1]["wheel"]: zipped({wheels[1]["wheel"]: new})}  # fmt: skip
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "m.json").write_text(json.dumps({"wheels": wheels}))
            out = tmp / "out.txt"
            args = gap_wheels.main.__globals__["argparse"].Namespace(
                manifest=str(tmp / "m.json"), staged=str(tmp / "staged"), repo="r/r", optout=str(tmp / "none"),
                summary=str(tmp / "summary.md"), github_output=str(out))  # fmt: skip
            calls = iter([archives[w["wheel"]] for w in wheels])
            with mock.patch.object(gap_wheels, "pypi_info", return_value=MIT), \
                 mock.patch.object(gap_wheels, "missing_dlls", side_effect=AssertionError("PE check on Linux")), \
                 mock.patch.object(gap_wheels, "fetch_wheel", side_effect=lambda repo, rid: next(calls)):  # fmt: skip
                gap_wheels.cmd_stage(args)
            lines = dict(line.split("=", 1) for line in out.read_text().splitlines())
            self.assertEqual([w["wheel"] for w in json.loads(lines["staged"])], [GLIBC_T, MUSL])
            self.assertIn("needs libraries or symbol versions a manylinux_2_28_x86_64 system may not have: GLIBC_2.34",
                          (tmp / "summary.md").read_text())  # fmt: skip
            tests = {t["target"]: t for t in json.loads(lines["tests"])}
            self.assertEqual(tests["3.14 musl-aarch64"], {
                "target": "3.14 musl-aarch64", "runner": "ubuntu-24.04-arm", "python": "3.14", "slug": "3.14-musl-aarch64",
                "image": gap_wheels.LINUX_TESTS["3.14 musl-aarch64"]["image"], "interpreter": "python",
                "python_url": "", "python_sha256": ""})  # fmt: skip
            self.assertTrue(tests["3.14 musl-aarch64"]["image"].startswith("python:3.14-alpine@sha256:"))
            glibc = tests["3.14t linux-x86_64"]
            self.assertEqual((glibc["runner"], glibc["interpreter"]), ("ubuntu-latest", "/opt/python/bin/python3.14t"))
            self.assertTrue(glibc["image"].startswith("debian:trixie-slim@sha256:"))
            self.assertIn("x86_64-unknown-linux-gnu-freethreaded-install_only.tar.gz", glibc["python_url"])
            self.assertEqual(glibc["python_sha256"], gap_wheels.LINUX_TESTS["3.14t linux-x86_64"]["python_sha256"])

    def test_install_test_jobs(self):
        self.assertEqual(gap_wheels.install_test_job("3.15 win-amd64")["image"], "")
        job = gap_wheels.install_test_job("3.15 linux-x86_64")
        self.assertEqual((job["runner"], job["interpreter"], job["python_url"]), ("ubuntu-latest", "python", ""))
        self.assertTrue(job["image"].startswith("python:3.15-slim-trixie@sha256:"))
        for target in gap_wheels.build_lab.TARGETS:
            self.assertEqual(set(gap_wheels.install_test_job(target)), {"target", "runner", "python", "slug", "image",
                             "interpreter", "python_url", "python_sha256"})  # fmt: skip

    def test_a_build_run_with_other_bytes_falls_back_to_the_other_build(self):
        data = b"wheel bytes"
        one = verified(sha=hashlib.sha256(data).hexdigest())
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "m.json").write_text(json.dumps({"wheels": [one]}))
            args = gap_wheels.main.__globals__["argparse"].Namespace(
                manifest=str(tmp / "m.json"), staged=str(tmp / "staged"), repo="r/r", optout=str(tmp / "none"),
                summary=None, github_output=None)  # fmt: skip
            archives = {11: zipped({WIN: b"forged"}), 12: zipped({WIN: data})}
            with mock.patch.object(gap_wheels, "pypi_info", return_value=MIT), \
                 mock.patch.object(gap_wheels, "missing_dlls", return_value=[]), \
                 mock.patch.object(gap_wheels, "fetch_wheel", side_effect=lambda repo, rid: archives[rid]):  # fmt: skip
                gap_wheels.cmd_stage(args)
            self.assertEqual((tmp / "staged" / WIN).read_bytes(), data)


class Publish(unittest.TestCase):
    def staged(self, tmp, data=b"wheel bytes"):
        w = {**verified(sha=hashlib.sha256(data).hexdigest()), "licence": "MIT", "requires_python": None,
             "verified": "2026-11-16", "batch": "R"}  # fmt: skip
        (tmp / "files").mkdir(exist_ok=True)
        (tmp / "files" / WIN).write_bytes(data)
        return w

    def mark_tested(self, tmp, target="3.14 win-arm64", ok=True, slug="3.14-win-arm64"):
        (tmp / "tested" / f"tested-{slug}").mkdir(parents=True, exist_ok=True)
        (tmp / "tested" / f"tested-{slug}" / "tested.json").write_text(json.dumps({"target": target, "tested": {WIN: ok}}))

    def publish(self, tmp, staged, optout=""):
        (tmp / "optout.txt").write_text(optout)
        args = gap_wheels.main.__globals__["argparse"].Namespace(
            staged_json=json.dumps(staged), files=str(tmp / "files"), tested=str(tmp / "tested"),
            index=str(tmp / "index.json"), release_dir=str(tmp / "release"), optout=str(tmp / "optout.txt"),
            run="RUN", summary=None)  # fmt: skip
        gap_wheels.cmd_publish(args)
        return json.loads((tmp / "index.json").read_text())

    def test_a_tested_verified_wheel_publishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            w = self.staged(tmp)
            self.mark_tested(tmp)
            index = self.publish(tmp, [w])
            self.assertEqual([x["wheel"] for x in index["wheels"]], [WIN])
            self.assertEqual(index["wheels"][0]["publish_run"], "RUN")
            self.assertTrue((tmp / "release" / WIN).is_file())
            # Again: already published, nothing new to upload.
            (tmp / "release" / WIN).unlink()
            index = self.publish(tmp, [w])
            self.assertEqual(len(index["wheels"]), 1)
            self.assertFalse((tmp / "release" / WIN).exists())

    def test_untested_failed_or_swapped_wheels_dont_publish(self):
        for case in ("untested", "failed", "swapped", "another target's answer", "an artifact for no target"):
            with self.subTest(case), tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                w = self.staged(tmp)
                if case == "failed":
                    self.mark_tested(tmp, ok=False)
                elif case == "swapped":
                    self.mark_tested(tmp)
                    (tmp / "files" / WIN).write_bytes(b"forged by a test job")
                elif case == "another target's answer":  # 3.14t's test job answering for 3.14
                    self.mark_tested(tmp, slug="3.14t-win-arm64")
                elif case == "an artifact for no target":
                    self.mark_tested(tmp, slug="forged")
                index = self.publish(tmp, [w])
                self.assertEqual(index["wheels"], [])
                self.assertFalse((tmp / "release" / WIN).exists())

    def test_an_answer_counts_for_the_target_its_artifact_is_named_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.mark_tested(tmp, target="3.14t win-arm64")  # in tested-3.14-win-arm64, naming another target
            self.mark_tested(tmp, target="3.14 musl-aarch64", slug="3.14-musl-aarch64")
            self.mark_tested(tmp, slug="forged")
            self.assertEqual(gap_wheels.read_tested(tmp / "tested"),
                             {"3.14 win-arm64": {WIN: True}, "3.14 musl-aarch64": {WIN: True}})  # fmt: skip

    def test_only_the_staging_jobs_output_is_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            w = self.staged(tmp)
            for bad in ([{**w, "wheel": "../x.whl"}], [{**w, "extra": 1}], [{**w, "sha256": "zz"}],
                        [{**w, "wheel": "lz4-1-cp314-cp314-linux_aarch64.whl"}], {"not": "a list"}):  # fmt: skip
                with self.subTest(bad), self.assertRaises(SystemExit):
                    gap_wheels.trusted_staged(json.dumps(bad))

    def test_an_opted_out_project_is_withdrawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            w = self.staged(tmp)
            self.mark_tested(tmp)
            self.publish(tmp, [w])
            index = self.publish(tmp, [], optout="# comment\nTikToken\n")
            self.assertEqual(index["wheels"], [])
            self.assertEqual((tmp / "release" / "withdraw.txt").read_text(), f"{WIN}\n")


class KeepTheWheel(unittest.TestCase):
    def test_build_passes_the_wheel_dir_through(self):
        seen = {}

        def fake_run_build(name, version, attempt, work, members, keep=None, plat=None, tag=None):
            seen["keep"] = keep
            return {"sdist": None, "sdist_sha256": None, "wheel": None, "sha256": None, "installs": None}

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(gap_wheels, "run_build", fake_run_build),
                mock.patch.object(gap_wheels.build_lab, "interpreter_problem", lambda target: None),
            ):
                gap_wheels.main(["build", "--name", "tiktoken", "--version", "0.14.0", "--target", "3.14 win-arm64",
                                 "--attempt", "2", "--runner", "windows-11-arm", "--out", str(Path(tmp) / "r.json"),
                                 "--wheel-dir", str(Path(tmp) / "wheel")])  # fmt: skip
        self.assertEqual(seen["keep"], Path(tmp) / "wheel")


def tiny_wheel(folder, body, extra=None):
    """A pure wheel pip installs offline: demo-1.0, one module whose import runs `body`, plus `extra` files."""
    path = folder / "demo-1.0-py3-none-any.whl"
    files = {"demo.py": body, **(extra or {}),
             "demo-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n",
             "demo-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}  # fmt: skip
    record = "".join(f"{n},,\n" for n in files) + "demo-1.0.dist-info/RECORD,,\n"
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
        z.writestr("demo-1.0.dist-info/RECORD", record)
    return path


class InstallAndImport(unittest.TestCase):
    def test_a_wheel_that_imports_passes_and_one_that_raises_doesnt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "ok").mkdir()
            (tmp / "bad").mkdir()
            ok = tiny_wheel(tmp / "ok", "VALUE = 1\n")
            bad = tiny_wheel(tmp / "bad", "raise RuntimeError('broken on import')\n")
            self.assertIsNone(gap_wheels.install_and_import(ok, "3.14 win-arm64", tmp / "w1"))
            self.assertEqual(gap_wheels.install_and_import(bad, "3.14 win-arm64", tmp / "w2"), "the top import check failed")

    def test_a_native_library_that_wont_load_fails_the_test(self):
        # A wheel with no extension module must load every native library it ships, not just import cleanly.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "lib").mkdir()
            broken = tiny_wheel(tmp / "lib", "VALUE = 1\n", {"demo_libs/libdemo.so": "not a library"})
            self.assertEqual(gap_wheels.install_and_import(broken, "3.14 win-arm64", tmp / "w3"),
                             "its native libraries didn't load")


def pe(dlls, delay=()):
    """A minimal PE32+ file importing `dlls` (and delay-loading `delay`): what pe_imports reads, nothing more."""
    raw, va, size = 0x200, 0x1000, 0x400
    section = bytearray(size)
    names_at = 0x200
    imp_desc, delay_desc = 0, 0x100
    for n, name in enumerate(dlls):
        rva = va + names_at
        section[names_at : names_at + len(name) + 1] = name.encode() + b"\0"
        names_at += len(name) + 1
        section[imp_desc + n * 20 + 12 : imp_desc + n * 20 + 16] = rva.to_bytes(4, "little")
    for n, name in enumerate(delay):
        rva = va + names_at
        section[names_at : names_at + len(name) + 1] = name.encode() + b"\0"
        names_at += len(name) + 1
        section[delay_desc + n * 32 + 4 : delay_desc + n * 32 + 8] = rva.to_bytes(4, "little")
    head = bytearray(raw)
    head[0:2] = b"MZ"
    head[0x3C:0x40] = (0x40).to_bytes(4, "little")
    head[0x40:0x44] = b"PE\0\0"
    head[0x46:0x48] = (1).to_bytes(2, "little")  # one section
    head[0x54:0x56] = (240).to_bytes(2, "little")  # SizeOfOptionalHeader
    opt = 0x58
    head[opt : opt + 2] = (0x20B).to_bytes(2, "little")
    dirs = opt + 112
    head[dirs - 4 : dirs] = (16).to_bytes(4, "little")
    if dlls:
        head[dirs + 8 : dirs + 12] = (va + imp_desc).to_bytes(4, "little")
    if delay:
        head[dirs + 8 * 13 : dirs + 8 * 13 + 4] = (va + delay_desc).to_bytes(4, "little")
    sec = opt + 240
    head[sec : sec + 8] = b".rdata\0\0"
    head[sec + 8 : sec + 12] = size.to_bytes(4, "little")
    head[sec + 12 : sec + 16] = va.to_bytes(4, "little")
    head[sec + 16 : sec + 20] = size.to_bytes(4, "little")
    head[sec + 20 : sec + 24] = raw.to_bytes(4, "little")
    return bytes(head + section)


class Dlls(unittest.TestCase):
    def test_reads_imports_and_delay_loads(self):
        data = pe(["KERNEL32.dll", "python314.dll"], delay=["MSVCP140.dll"])
        self.assertEqual(gap_wheels.pe_imports(data), {"kernel32.dll", "python314.dll", "msvcp140.dll"})

    def test_garbage_and_truncated_files_are_unreadable(self):
        good_pe = pe(["kernel32.dll"])
        for bad in (b"", b"MZ", b"not a pe" * 100, good_pe[:0x150], good_pe[:0x210]):
            with self.subTest(len(bad)):
                self.assertIsNone(gap_wheels.pe_imports(bad))

    def test_a_wheel_needing_msvcp140_isnt_self_contained(self):
        system = pe(["python314.dll", "VCRUNTIME140.dll", "api-ms-win-crt-runtime-l1-1-0.dll", "KERNEL32.dll", "pdh.dll"])
        self.assertEqual(gap_wheels.missing_dlls(zipped({"pkg/_ext.cp314-win_arm64.pyd": system})), [])
        cpp = pe(["python314.dll", "MSVCP140.dll", "vcomp140.dll"])
        self.assertEqual(gap_wheels.missing_dlls(zipped({"pkg/_ext.pyd": cpp})), ["msvcp140.dll", "vcomp140.dll"])
        beside = zipped({"pkg/_ext.pyd": cpp, "pkg/msvcp140.dll": system, "pkg/vcomp140.dll": system})
        self.assertEqual(gap_wheels.missing_dlls(beside), [])
        # Elsewhere in the wheel isn't where Windows looks for the .pyd's DLLs.
        elsewhere = zipped({"pkg/_ext.pyd": cpp, "junk/msvcp140.dll": system, "pkg/vcomp140.dll": system})
        self.assertEqual(gap_wheels.missing_dlls(elsewhere), ["msvcp140.dll"])
        many = zipped({**{f"pkg/m{i}.pyd": system for i in range(500)}, "pkg/last.pyd": cpp})
        self.assertIsNone(gap_wheels.missing_dlls(many))  # too many to check: refused, not half-checked
        self.assertIsNone(gap_wheels.missing_dlls(zipped({"pkg/_ext.pyd": b"MZ junk"})))

    def test_an_unreadable_wheel_costs_only_itself(self):
        # A corrupt LZMA member raises lzma.LZMAError, which nothing expects: that wheel is skipped, the batch goes on.
        with mock.patch.object(gap_wheels, "fetch_wheel", return_value=b"x"), \
             mock.patch.object(gap_wheels, "unwrap_wheel", return_value=b"bytes"), \
             mock.patch.object(gap_wheels, "missing_dlls", side_effect=__import__("lzma").LZMAError("corrupt")):  # fmt: skip
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                (tmp / "m.json").write_text(json.dumps({"wheels": [verified()]}))
                args = gap_wheels.main.__globals__["argparse"].Namespace(
                    manifest=str(tmp / "m.json"), staged=str(tmp / "staged"), repo="r/r", optout=str(tmp / "none"),
                    summary=None, github_output=None)  # fmt: skip
                with mock.patch.object(gap_wheels, "pypi_info", return_value=MIT):
                    self.assertEqual(gap_wheels.cmd_stage(args), 0)
                self.assertEqual(json.loads((tmp / "staged" / "staged.json").read_text()), [])


DYN_AT, STR_AT = 0x140, 0x340  # where elf() puts the dynamic section and the strings, in the file


def elf(needed=(), versions=None, machine=62, ident=b"\x7fELF\x02\x01\x01", dynamic=True, runpath=None, rpath=None,
        shift=0, loads=()):  # fmt: skip
    """A minimal 64-bit ELF shared object needing the libraries `needed` (DT_NEEDED), from each file in `versions`
    those symbol versions (a version-needs table), with a DT_RUNPATH or DT_RPATH: what elf_needs reads, nothing more.
    Two PT_LOADs: the headers at address 0, and the rest at its file offset plus `shift`; `loads` adds more, as
    (file offset, vaddr, size)."""
    versions = versions or {}
    strtab = bytearray(b"\0")

    def add(text):
        strtab.extend(text.encode() + b"\0")
        return len(strtab) - len(text.encode()) - 1

    need = [add(n) for n in needed]
    verneed = bytearray()
    for i, (file, names) in enumerate(versions.items()):
        last = i == len(versions) - 1
        verneed += struct.pack("<HHIII", 1, len(names), add(file), 16, 0 if last else 16 + 16 * len(names))
        for j, name in enumerate(names):
            verneed += struct.pack("<IHHII", 0, 0, 0, add(name), 0 if j == len(names) - 1 else 16)
    ver_at = STR_AT + (len(strtab) + 64 + 7) // 8 * 8  # room for the search paths' strings
    tags = [(1, o) for o in need]
    if runpath is not None:
        tags.append((29, add(runpath)))
    if rpath is not None:
        tags.append((15, add(rpath)))
    tags += [(5, STR_AT + shift), (10, len(strtab))]
    if versions:
        tags += [(0x6FFFFFFE, ver_at + shift), (0x6FFFFFFF, len(versions))]
    dyn = b"".join(struct.pack("<qQ", t, v) for t, v in [*tags, (0, 0)])
    assert len(dyn) <= STR_AT - DYN_AT and STR_AT + len(strtab) <= ver_at
    size = ver_at + len(verneed)
    phdrs = [struct.pack("<IIQQQQQQ", 1, 4, 0, 0, 0, DYN_AT, DYN_AT, 0x1000),  # PT_LOAD: the headers
             struct.pack("<IIQQQQQQ", 1, 5, DYN_AT, DYN_AT + shift, DYN_AT + shift, size - DYN_AT, size - DYN_AT,
                         0x1000)]  # PT_LOAD: the rest  # fmt: skip
    phdrs += [struct.pack("<IIQQQQQQ", 1, 4, off, va, va, n, n, 0x1000) for off, va, n in loads]
    if dynamic:
        phdrs.append(struct.pack("<IIQQQQQQ", 2, 6, DYN_AT, DYN_AT + shift, DYN_AT + shift, len(dyn), len(dyn), 8))
    assert 64 + 56 * len(phdrs) <= DYN_AT
    out = bytearray(size)
    out[0:64] = ident.ljust(16, b"\0") + struct.pack("<HHIQQQIHHHHHH", 3, machine, 1, 0, 64, 0, 0, 64, 56, len(phdrs),
                                                    64, 0, 0)  # fmt: skip
    out[64 : 64 + 56 * len(phdrs)] = b"".join(phdrs)
    out[DYN_AT : DYN_AT + len(dyn)] = dyn
    out[STR_AT : STR_AT + len(strtab)] = strtab
    out[ver_at:] = verneed
    return bytes(out)


def dyn_value(n):
    """The file offset of the value of elf()'s n-th dynamic entry."""
    return DYN_AT + 16 * n + 8


GLIBC_POLICY = "manylinux_2_28_x86_64"


def whl(members, dist="pkg-1.0"):
    """A wheel's bytes: the members, plus its one .dist-info folder."""
    return zipped({**members, f"{dist}.dist-info/METADATA": b""})


class Elf(unittest.TestCase):
    def test_reads_needed_libraries_symbol_versions_and_search_paths(self):
        data = elf(["libc.so.6", "libstdc++.so.6"], {"libc.so.6": ["GLIBC_2.2.5", "GLIBC_2.17"],
                                                    "libstdc++.so.6": ["GLIBCXX_3.4.21", "CXXABI_1.3.9"],
                                                    "ld-linux-x86-64.so.2": ["GLIBC_2.3"]},
                   runpath="$ORIGIN/../pkg.libs:/usr/lib")  # fmt: skip
        libs, versions, runpath = gap_wheels.elf_needs(data, 62)
        self.assertEqual(libs, {"libc.so.6", "libstdc++.so.6"})
        self.assertEqual(versions, {"GLIBC_2.2.5", "GLIBC_2.17", "GLIBCXX_3.4.21", "CXXABI_1.3.9", "GLIBC_2.3"})
        self.assertEqual(runpath, ["$ORIGIN/../pkg.libs", "/usr/lib"])
        self.assertEqual(gap_wheels.elf_needs(elf(["libc.musl-aarch64.so.1"], machine=183), 183),
                         ({"libc.musl-aarch64.so.1"}, set(), []))  # fmt: skip
        self.assertEqual(gap_wheels.elf_needs(elf(dynamic=False), 62), (set(), set(), []))  # static: needs nothing
        # DT_RPATH counts only without a DT_RUNPATH, as in glibc.
        self.assertEqual(gap_wheels.elf_needs(elf(["libc.so.6"], rpath="$ORIGIN"), 62)[2], ["$ORIGIN"])
        self.assertEqual(gap_wheels.elf_needs(elf(["libc.so.6"], runpath="$ORIGIN/a", rpath="$ORIGIN/b"), 62)[2],
                         ["$ORIGIN/a"])  # fmt: skip
        # Read by address, not file offset: the tables sit in a segment loaded 64 KiB past its file offset.
        shifted = elf(["libc.so.6"], {"libc.so.6": ["GLIBC_2.17"], "libm.so.6": ["GLIBC_2.27"]}, shift=0x10000)
        self.assertEqual(gap_wheels.elf_needs(shifted, 62), ({"libc.so.6"}, {"GLIBC_2.17", "GLIBC_2.27"}, []))

    def test_garbage_truncated_32_bit_big_endian_and_other_machines_are_unreadable(self):
        good_elf = elf(["libc.so.6"], {"libc.so.6": ["GLIBC_2.17"]})
        self.assertIsNotNone(gap_wheels.elf_needs(good_elf, 62))
        for label, bad in (
            ("empty", b""),
            ("magic only", b"\x7fELF"),
            ("garbage", b"not an elf" * 100),
            ("a PE file", pe(["kernel32.dll"])),
            ("headers only", good_elf[:0x80]),
            ("no strings", good_elf[:STR_AT]),
            ("half a version table", good_elf[:-8]),
            ("32-bit", elf(["libc.so.6"], ident=b"\x7fELF\x01\x01\x01")),
            ("big-endian", elf(["libc.so.6"], ident=b"\x7fELF\x02\x02\x01")),
            ("aarch64 for x86_64", elf(["libc.so.6"], machine=183)),
            ("not ASCII", elf(["libc\u00e9.so.6"])),
            ("a library name with a path", elf(["/tmp/libc.so.6"])),
            ("a library name with markup", elf(["lib<b>.so"])),
            ("a version name with a space", elf(["libc.so.6"], {"libc.so.6": ["GLIBC 2.17"]})),
            ("a search path with a newline", elf(["libc.so.6"], runpath="$ORIGIN\n")),
        ):
            with self.subTest(label):
                self.assertIsNone(gap_wheels.elf_needs(bad, 62))
        self.assertIsNone(gap_wheels.elf_needs(elf(["libc.so.6"]), 183))  # x86_64 for aarch64

    def test_odd_tables_are_unreadable(self):
        # dynamic entries: DT_NEEDED, DT_STRTAB, DT_STRSZ, DT_VERNEED, DT_VERNEEDNUM, DT_NULL
        data = bytearray(elf(["libc.so.6"], {"libc.so.6": ["GLIBC_2.17", "GLIBC_2.28"]}))
        for label, at, value in (
            ("a string table past the file", dyn_value(2), 1 << 20),
            ("a needed name past the strings", dyn_value(0), 1 << 16),
            ("a dynamic segment past the file", 64 + 56 * 2 + 16, 1 << 32),
            ("more version records than there are", dyn_value(4), 2),
            ("no version records", dyn_value(4), 0),
        ):
            with self.subTest(label):
                bad = bytearray(data)
                bad[at : at + 8] = struct.pack("<Q", value)
                self.assertIsNone(gap_wheels.elf_needs(bytes(bad), 62))
        no_null = bytearray(data)
        no_null[DYN_AT + 16 * 5 : DYN_AT + 16 * 6] = struct.pack("<qQ", 0x6FFFFFF0, 0)  # DT_NULL replaced
        self.assertIsNone(gap_wheels.elf_needs(bytes(no_null), 62))

    def test_version_needs_are_walked_as_the_loader_walks_them(self):
        # glibc follows each record's next link to 0 and doesn't stop at DT_VERNEEDNUM or vn_cnt, so a need past a
        # count is still checked at load time: such a file is refused, not read short.
        two_files = bytearray(elf(["libc.so.6"], {"libm.so.6": ["GLIBC_2.2.5"], "libc.so.6": ["GLIBC_2.34"]}))
        self.assertEqual(gap_wheels.elf_needs(bytes(two_files), 62)[1], {"GLIBC_2.2.5", "GLIBC_2.34"})
        two_files[dyn_value(4) : dyn_value(4) + 8] = struct.pack("<Q", 1)  # DT_VERNEEDNUM 1, a second record linked
        self.assertIsNone(gap_wheels.elf_needs(bytes(two_files), 62))
        two_versions = bytearray(elf(["libc.so.6"], {"libc.so.6": ["GLIBC_2.17", "GLIBC_2.34"]}))
        ver_at = struct.unpack("<Q", two_versions[dyn_value(3) : dyn_value(3) + 8])[0]
        two_versions[ver_at + 2 : ver_at + 4] = struct.pack("<H", 1)  # vn_cnt 1, a second Vernaux linked
        self.assertIsNone(gap_wheels.elf_needs(bytes(two_versions), 62))
        two_versions[ver_at + 2 : ver_at + 4] = struct.pack("<H", 3)  # a count longer than the chain
        self.assertIsNone(gap_wheels.elf_needs(bytes(two_versions), 62))
        # A link past the loaded bytes ends the read, not the walk.
        far = bytearray(elf(["libc.so.6"], {"libc.so.6": ["GLIBC_2.17"]}))
        ver_at = struct.unpack("<Q", far[dyn_value(3) : dyn_value(3) + 8])[0]
        far[ver_at + 16 + 12 : ver_at + 16 + 16] = struct.pack("<I", 0x7FFFFFFF)  # vna_next
        self.assertIsNone(gap_wheels.elf_needs(bytes(far), 62))

    def test_segments_mapped_over_each_other_are_unreadable(self):
        self.assertIsNotNone(gap_wheels.elf_needs(elf(["libc.so.6"], loads=[(0, 0x20000, 0x40)]), 62))  # a page apart
        self.assertIsNotNone(gap_wheels.elf_needs(elf(["libc.so.6"], shift=0x10000), 62))
        # Pages are 4 KiB on x86_64 but can be 64 KiB on aarch64: 4 KiB apart shares an aarch64 page.
        self.assertIsNotNone(gap_wheels.elf_needs(elf(["libc.so.6"], shift=0x1000), 62))
        self.assertIsNone(gap_wheels.elf_needs(elf(["libc.musl-aarch64.so.1"], machine=183, shift=0x1000), 183))
        self.assertIsNotNone(gap_wheels.elf_needs(elf(["libc.musl-aarch64.so.1"], machine=183, shift=0x10000), 183))
        for label, data in (
            # The loader would map the headers' bytes over the tables' page; offset() would read the tables' bytes.
            ("over the tables, from other bytes", elf(["libc.so.6"], shift=0x1000, loads=[(0, 0x1000 + DYN_AT, 0x40)])),
            ("out of order", elf(["libc.so.6"], loads=[(0, 0x80, 0x40)])),
        ):
            with self.subTest(label):
                self.assertIsNone(gap_wheels.elf_needs(data, 62))

    @unittest.skipUnless(sys.platform == "linux" and platform.machine() == "x86_64", "a real x86_64 glibc ELF file")
    def test_a_real_interpreter(self):
        needs = gap_wheels.elf_needs(Path(sys.executable).resolve().read_bytes(), 62)
        self.assertIsNotNone(needs)
        self.assertIn("libc.so.6", needs[0])
        self.assertTrue(any(v.startswith("GLIBC_2.") for v in needs[1]))

    def test_libraries_must_be_on_the_policy_or_where_the_file_finds_them(self):
        ext = "pkg/_ext.cpython-314t-x86_64-linux-gnu.so"
        allowed = elf(["libc.so.6", "libm.so.6", "libstdc++.so.6", "ld-linux-x86-64.so.2"],
                      {"libc.so.6": ["GLIBC_2.2.5", "GLIBC_2.28"], "libstdc++.so.6": ["GLIBCXX_3.4.24", "CXXABI_TM_1"],
                       "libgcc_s.so.1": ["GCC_7.0.0"], "ld-linux-x86-64.so.2": ["GLIBC_2.3"]})  # fmt: skip
        self.assertEqual(gap_wheels.missing_libs(whl({ext: allowed}), GLIBC_POLICY), [])
        # auditwheel's layout: the extension's RUNPATH points at <dist>.libs, a bundled library's at its own folder.
        bundled = elf(["libc.so.6", "libgomp-24e2ab19.so.1.0.0"], runpath="$ORIGIN/../pkg.libs")
        gomp = elf(["libc.so.6", "libnuma-1a2b.so.1"], {"libc.so.6": ["GLIBC_2.17"], "libcrypto-abc.so.3": ["OPENSSL_3.0.0"]},
                   runpath="$ORIGIN")  # fmt: skip
        numa = elf(["libc.so.6"])
        libs = {"pkg.libs/libgomp-24e2ab19.so.1.0.0": gomp, "pkg.libs/libnuma-1a2b.so.1": numa}
        self.assertEqual(gap_wheels.missing_libs(whl({ext: bundled, **libs}), GLIBC_POLICY), [])
        self.assertEqual(gap_wheels.missing_libs(whl({ext: bundled}), GLIBC_POLICY), ["libgomp-24e2ab19.so.1.0.0"])
        braced = elf(["libc.so.6", "libgomp-24e2ab19.so.1.0.0"], rpath="${ORIGIN}/../pkg.libs")
        self.assertEqual(gap_wheels.missing_libs(whl({ext: braced, **libs}), GLIBC_POLICY), [])
        # The bundled library has no RUNPATH: glibc doesn't look in its own folder, so its dependency isn't found.
        no_runpath = {**libs, "pkg.libs/libgomp-24e2ab19.so.1.0.0": elf(["libc.so.6", "libnuma-1a2b.so.1"])}
        self.assertEqual(gap_wheels.missing_libs(whl({ext: bundled, **no_runpath}), GLIBC_POLICY),
                         ["libnuma-1a2b.so.1"])  # fmt: skip
        elsewhere = {"pkg.libs/libgomp-24e2ab19.so.1.0.0": elf(["libc.so.6", "libnuma-1a2b.so.1"]),
                     "other.libs/libnuma-1a2b.so.1": numa}  # fmt: skip
        self.assertEqual(gap_wheels.missing_libs(whl({ext: bundled, **elsewhere}), GLIBC_POLICY), ["libnuma-1a2b.so.1"])
        # Without a RUNPATH a file of that name anywhere in the wheel doesn't count, even beside the extension: the
        # loader doesn't look there (a package preloading it with ctypes would work, but isn't published).
        plain = elf(["libc.so.6", "libfoo.so.1"])
        for where in ("pkg-1.0.data/data/lib/libfoo.so.1", "other/libfoo.so.1", "pkg.libs/libfoo.so.1", "pkg/libfoo.so.1"):
            with self.subTest(where):
                self.assertEqual(gap_wheels.missing_libs(whl({ext: plain, where: numa}), GLIBC_POLICY), ["libfoo.so.1"])
        beside = elf(["libc.so.6", "libfoo.so.1"], runpath="$ORIGIN")
        self.assertEqual(gap_wheels.missing_libs(whl({ext: beside, "pkg/libfoo.so.1": numa}), GLIBC_POLICY), [])
        # A RUNPATH reaches only into the wheel: not above it, not into another install scheme, not by absolute path.
        for runpath, where in (("$ORIGIN/../..", "libfoo.so.1"), ("/usr/lib", "usr/lib/libfoo.so.1"),
                               ("$ORIGIN/../pkg-1.0.data/data/lib", "pkg-1.0.data/data/lib/libfoo.so.1"),
                               ("$LIB", "pkg/$LIB/libfoo.so.1"), ("lib", "pkg/lib/libfoo.so.1")):  # fmt: skip
            with self.subTest(runpath):
                data = elf(["libc.so.6", "libfoo.so.1"], runpath=runpath)
                self.assertEqual(gap_wheels.missing_libs(whl({ext: data, where: numa}), GLIBC_POLICY), ["libfoo.so.1"])
        # Only the wheel's own <name>-<version>.data folder (its .dist-info's name) is installed by scheme: another
        # folder ending in .data installs as it is, so a library in it isn't where $ORIGIN points.
        at_root = elf(["libc.so.6", "libfoo.so.1"], runpath="$ORIGIN")
        root = "_e.cpython-314t-x86_64-linux-gnu.so"
        self.assertEqual(gap_wheels.missing_libs(whl({root: at_root, "pkg-1.0.data/platlib/libfoo.so.1": numa}),
                                                 GLIBC_POLICY), [])  # fmt: skip
        self.assertEqual(gap_wheels.missing_libs(whl({root: at_root, "bogus.data/platlib/libfoo.so.1": numa}),
                                                 GLIBC_POLICY), ["libfoo.so.1"])  # fmt: skip
        self.assertEqual(gap_wheels.missing_libs(whl({root: at_root, "other-1.0.data/platlib/libfoo.so.1": numa}),
                                                 GLIBC_POLICY), ["libfoo.so.1"])  # fmt: skip
        # Which .data folder pip reads is known only with exactly one .dist-info: otherwise the wheel is refused.
        self.assertIsNone(gap_wheels.missing_libs(zipped({root: at_root}), GLIBC_POLICY))
        two = zipped({root: at_root, "pkg-1.0.dist-info/METADATA": b"", "other-1.0.dist-info/METADATA": b""})
        self.assertIsNone(gap_wheels.missing_libs(two, GLIBC_POLICY))
        # platlib under <dist>.data installs beside the root's files.
        inside = elf(["libc.so.6", "libfoo.so.1"], runpath="$ORIGIN/../pkg.libs")
        self.assertEqual(gap_wheels.missing_libs(whl({ext: inside, "pkg-1.0.data/platlib/pkg.libs/libfoo.so.1": numa}),
                                                 GLIBC_POLICY), [])  # fmt: skip
        missing = elf(["libc.so.6", "libssl.so.3", "libpython3.14t.so.1.0"])
        self.assertEqual(gap_wheels.missing_libs(whl({ext: missing}), GLIBC_POLICY),
                         ["libpython3.14t.so.1.0", "libssl.so.3"])  # fmt: skip
        # Any ELF file, whatever its name, and a shared object, whatever's in it.
        self.assertEqual(gap_wheels.missing_libs(whl({"pkg-1.0.data/scripts/tool": missing}), GLIBC_POLICY),
                         ["libpython3.14t.so.1.0", "libssl.so.3"])  # fmt: skip
        self.assertIsNone(gap_wheels.missing_libs(whl({ext: b"not an elf"}), GLIBC_POLICY))
        self.assertIsNone(gap_wheels.missing_libs(whl({"pkg/libfoo.so.1": b"\x7fELF junk"}), GLIBC_POLICY))
        self.assertEqual(gap_wheels.missing_libs(whl({"pkg/data.bin": b"plain bytes", "pkg/__init__.py": b""}),
                                                 GLIBC_POLICY), [])  # fmt: skip
        many = whl({f"pkg/m{i}.so": allowed for i in range(501)})
        self.assertIsNone(gap_wheels.missing_libs(many, GLIBC_POLICY))  # too many to check: refused, not half-checked
        with mock.patch.object(gap_wheels, "MAX_ELF_BYTES", 3 * len(allowed)):  # too much to read: refused too
            self.assertEqual(gap_wheels.missing_libs(whl({f"pkg/m{i}.so": allowed for i in range(3)}), GLIBC_POLICY), [])
            self.assertIsNone(gap_wheels.missing_libs(whl({f"pkg/m{i}.so": allowed for i in range(4)}), GLIBC_POLICY))

    def test_glibc_symbol_versions_above_the_policy_are_refused(self):
        ext = "pkg/_ext.cpython-314t-x86_64-linux-gnu.so"
        for needs, missing in (
            ({"libc.so.6": ["GLIBC_2.17", "GLIBC_2.34"]}, ["GLIBC_2.34"]),
            ({"libc.so.6": ["GLIBC_2.29"]}, ["GLIBC_2.29"]),
            ({"libc.so.6": ["GLIBC_PRIVATE"]}, ["GLIBC_PRIVATE"]),
            ({"libc.so.6": ["GLIBC_ABI_DT_RELR"]}, ["GLIBC_ABI_DT_RELR"]),
            ({"libstdc++.so.6": ["GLIBCXX_3.4.30"]}, ["GLIBCXX_3.4.30"]),
            ({"libstdc++.so.6": ["CXXABI_1.3.13"]}, ["CXXABI_1.3.13"]),
            ({"libgcc_s.so.1": ["GCC_12.0.0"]}, ["GCC_12.0.0"]),
            ({"libz.so.1": ["ZLIB_1.2.12"]}, ["ZLIB_1.2.12"]),
            ({"libstdc++.so.6": ["GLIBCXX_3.4.24"], "libc.so.6": ["GLIBC_2.28"]}, []),
            # Needs from the dynamic loader itself count too (libgcc's unwinder built against glibc 2.35+).
            ({"ld-linux-x86-64.so.2": ["GLIBC_2.3", "GLIBC_2.35"]}, ["GLIBC_2.35"]),
            ({"ld-linux-x86-64.so.2": ["GLIBC_PRIVATE"]}, ["GLIBC_PRIVATE"]),
        ):
            with self.subTest(needs):
                data = elf(["libc.so.6", "libstdc++.so.6", "libgcc_s.so.1", "libz.so.1"], needs)
                self.assertEqual(gap_wheels.missing_libs(whl({ext: data}), GLIBC_POLICY), missing)

    def test_musl_checks_libraries_only(self):
        ext = "pkg/_ext.cpython-314-aarch64-linux-musl.so"
        gcc = elf(["libc.musl-aarch64.so.1"], machine=183)
        self.assertEqual(gap_wheels.missing_libs(whl({ext: elf(["libc.musl-aarch64.so.1"], machine=183)}),
                                                 "musllinux_1_2_aarch64"), [])  # fmt: skip
        ok = elf(["libc.musl-aarch64.so.1", "libz.so.1", "libgcc_s-0366c7ba.so.1"], machine=183, runpath="$ORIGIN/../pkg.libs")
        self.assertEqual(gap_wheels.missing_libs(whl({ext: ok, "pkg.libs/libgcc_s-0366c7ba.so.1": gcc}),
                                                 "musllinux_1_2_aarch64"), [])  # fmt: skip
        glibc = elf(["libc.so.6", "libstdc++.so.6"], machine=183)
        self.assertEqual(gap_wheels.missing_libs(whl({ext: glibc}), "musllinux_1_2_aarch64"),
                         ["libc.so.6", "libstdc++.so.6"])  # fmt: skip
        self.assertIsNone(gap_wheels.missing_libs(whl({ext: elf(["libc.so.6"])}), "musllinux_1_2_aarch64"))  # x86_64

    def test_stage_uses_the_targets_check(self):
        data = whl({"pkg/_ext.cpython-314t-x86_64-linux-gnu.so": elf(["libc.so.6", "libssl.so.3"])})
        wheel = verified(name="lz4", wheel=GLIBC_T, sha=hashlib.sha256(data).hexdigest(), target="3.14t linux-x86_64")
        with mock.patch.object(gap_wheels, "fetch_wheel", return_value=zipped({GLIBC_T: data})), \
             mock.patch.object(gap_wheels, "missing_dlls", side_effect=AssertionError("PE check on Linux")):  # fmt: skip
            self.assertEqual(gap_wheels.take_wheel("r/r", wheel), (None, "needs libraries or symbol versions a "
                             "manylinux_2_28_x86_64 system may not have: libssl.so.3"))  # fmt: skip
        junk = zipped({"pkg/_ext.cpython-314t-x86_64-linux-gnu.so": b"junk"})
        wheel = verified(name="lz4", wheel=GLIBC_T, sha=hashlib.sha256(junk).hexdigest(), target="3.14t linux-x86_64")
        with mock.patch.object(gap_wheels, "fetch_wheel", return_value=zipped({GLIBC_T: junk})):
            self.assertEqual(gap_wheels.take_wheel("r/r", wheel), (None, "couldn't read its ELF files' needs"))
        # A Windows wheel still gets the PE check, which knows nothing of ELF.
        win = zipped({"pkg/_ext.pyd": pe(["python314.dll", "MSVCP140.dll"])})
        wheel = verified(sha=hashlib.sha256(win).hexdigest())
        with mock.patch.object(gap_wheels, "fetch_wheel", return_value=zipped({WIN: win})), \
             mock.patch.object(gap_wheels, "missing_libs", side_effect=AssertionError("ELF check on Windows")):  # fmt: skip
            self.assertEqual(gap_wheels.take_wheel("r/r", wheel)[1], "needs DLLs a user's machine may not have: msvcp140.dll")


class InstallTestLinux(unittest.TestCase):
    def test_tests_only_its_targets_wheels_with_the_containers_python(self):
        staged = [verified(name="lz4", wheel=MUSL, target="3.14 musl-aarch64"), verified()]
        seen = []

        def fake(wheel, target, work):
            seen.append((wheel.name, target))
            return None

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "staged.json").write_text(json.dumps(staged))
            with mock.patch.object(gap_wheels.build_lab, "interpreter_problem", return_value=None), \
                 mock.patch.object(gap_wheels, "install_and_import", fake):  # fmt: skip
                self.assertEqual(gap_wheels.main(["install-test", "--staged", str(tmp), "--target", "3.14 musl-aarch64",
                                                  "--out", str(tmp / "tested.json")]), 0)  # fmt: skip
            self.assertEqual(seen, [(MUSL, "3.14 musl-aarch64")])
            self.assertEqual(json.loads((tmp / "tested.json").read_text()),
                             {"target": "3.14 musl-aarch64", "tested": {MUSL: True}})  # fmt: skip
            with mock.patch.object(gap_wheels.build_lab, "interpreter_problem", return_value="not a musl libc"):
                self.assertEqual(gap_wheels.main(["install-test", "--staged", str(tmp), "--target", "3.14 musl-aarch64",
                                                  "--out", str(tmp / "t2.json")]), 3)  # fmt: skip


class Repair(unittest.TestCase):
    def fake_run(self, exit_code=0, writes=("x-1.0-cp314-cp314-manylinux_2_39_x86_64.whl",)):
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if "env" in kwargs:
                self.assertEqual(kwargs["env"]["SOURCE_DATE_EPOCH"], "1700000000")
            if "--wheel-dir" in cmd:
                out = Path(cmd[cmd.index("--wheel-dir") + 1])
                out.mkdir(parents=True, exist_ok=True)
                for name in writes:
                    (out / name).write_bytes(b"repaired")
            return subprocess.CompletedProcess(cmd, exit_code if "--wheel-dir" in cmd else 0, "", "")

        return run, calls

    def test_the_repaired_wheel_with_pinned_tools_and_a_fixed_epoch(self):
        run, calls = self.fake_run()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(gap_wheels.subprocess, "run", run):
            got = gap_wheels.repair(Path(tmp) / "x-1.0-cp314-cp314-linux_x86_64.whl", Path(tmp))
            self.assertEqual(got.name, "x-1.0-cp314-cp314-manylinux_2_39_x86_64.whl")
        install = next(c for c in calls if "install" in c)
        self.assertTrue(all(t in install for t in gap_wheels.REPAIR_TOOLS))
        self.assertTrue(all("==" in t for t in gap_wheels.REPAIR_TOOLS))  # pinned

    def test_no_wheel_or_two_wheels_or_a_failure_is_none(self):
        for exit_code, writes in ((1, ("x-1.0-cp314-cp314-manylinux_2_39_x86_64.whl",)), (0, ()),
                                  (0, ("a-1-cp314-cp314-manylinux_2_39_x86_64.whl", "b-1-cp314-cp314-manylinux_2_39_x86_64.whl"))):  # fmt: skip
            with self.subTest(exit_code=exit_code, writes=writes):
                run, _ = self.fake_run(exit_code, writes)
                with tempfile.TemporaryDirectory() as tmp, mock.patch.object(gap_wheels.subprocess, "run", run):
                    self.assertIsNone(gap_wheels.repair(Path(tmp) / "x.whl", Path(tmp)))



class RepairPlatform(unittest.TestCase):
    def test_repairs_to_exactly_the_targets_platform(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if "repair" in cmd:
                out = Path(cmd[cmd.index("--wheel-dir") + 1])
                out.mkdir(parents=True)
                (out / "x-1.0-cp314-cp314t-manylinux_2_28_x86_64.whl").write_bytes(b"w")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(gap_wheels.subprocess, "run", run):
            got = gap_wheels.repair(Path(tmp) / "x.whl", Path(tmp), "manylinux_2_28_x86_64")
            self.assertEqual(got.name, "x-1.0-cp314-cp314t-manylinux_2_28_x86_64.whl")
        repair = next(c for c in calls if "repair" in c)
        self.assertEqual(repair[repair.index("--plat") + 1], "manylinux_2_28_x86_64")
        self.assertIn("--only-plat", repair)  # no wider tag, such as manylinux2014, added on top

if __name__ == "__main__":
    unittest.main()


class NativeWithoutExtensions(unittest.TestCase):
    """LightGBM's kind of wheel: no extension module, a DLL loaded with ctypes, tagged py3-none-<platform>."""

    def test_a_native_py3_none_build_is_a_pair(self):
        builds = {"results": [
            row("lightgbm", "4.7.0", "3.14 win-arm64", extensions=0, wheel="py3-none-win_arm64"),
            row("lightgbm", "4.7.0", "3.14t win-arm64", extensions=0, wheel="py3-none-win_arm64", gil=None),
            row("ninja", "1.0", "3.14 musl-aarch64", extensions=0, wheel="py3-none-musllinux_1_2_aarch64"),
            row("pure", "1.0", "3.14 win-arm64", extensions=0, wheel="py3-none-any"),
            row("x64", "1.0", "3.14 win-arm64", extensions=0, wheel="py3-none-win_amd64"),
            row("both", "1.0", "3.14 win-arm64", extensions=0, wheel="py3-none-win_arm64.win_amd64"),
            row("abi", "1.0", "3.14 win-arm64", extensions=0, wheel="cp39-abi3-win_arm64"),
            row("notag", "1.0", "3.14 win-arm64", extensions=0),
            row("noimport", "1.0", "3.14 win-arm64", extensions=0, wheel="py3-none-win_arm64", **{"import": "failed"}),
        ]}  # fmt: skip
        pairs = {(p["name"], p["target"]) for p in gap_wheels.clean_pairs(builds)}
        self.assertEqual(pairs, {("lightgbm", "3.14 win-arm64"), ("lightgbm", "3.14t win-arm64"),
                                 ("ninja", "3.14 musl-aarch64")})  # fmt: skip

    def test_which_wheel_files_get_retagged(self):
        self.assertTrue(gap_wheels.pure_platform_tag("lightgbm-4.7.0-py3-none-win_arm64.whl"))
        self.assertTrue(gap_wheels.pure_platform_tag("x-1.0-1-py2.py3-none-musllinux_1_2_aarch64.whl"))
        self.assertFalse(gap_wheels.pure_platform_tag("x-1.0-py3-none-any.whl"))
        self.assertFalse(gap_wheels.pure_platform_tag("x-1.0-cp314-cp314-win_arm64.whl"))
        self.assertFalse(gap_wheels.pure_platform_tag("x-1.0-cp39-abi3-win_arm64.whl"))

    def test_the_vc_runtime_bundled_by_delvewheel_counts_and_nothing_else_does(self):
        lib = pe(["MSVCP140-13f01359eefd96cadb3a8ee645921e00.dll", "vcomp140-49e628b226e7b7d51c9b7e9d11d85efe.dll",
                  "kernel32.dll"])  # fmt: skip
        runtime = pe(["kernel32.dll"])
        ok = whl({"lightgbm/bin/lib_lightgbm.dll": lib,
                  "lightgbm.libs/msvcp140-13f01359eefd96cadb3a8ee645921e00.dll": runtime,
                  "lightgbm.libs/vcomp140-49e628b226e7b7d51c9b7e9d11d85efe.dll": runtime})  # fmt: skip
        self.assertEqual(gap_wheels.missing_dlls(ok), [])
        ssl = pe(["libssl-0123456789abcdef0123456789abcdef.dll"])
        third_party = whl({"pkg/_ext.pyd": ssl, "pkg.libs/libssl-0123456789abcdef0123456789abcdef.dll": runtime})
        self.assertEqual(gap_wheels.missing_dlls(third_party), ["libssl-0123456789abcdef0123456789abcdef.dll"])
        plain = whl({"pkg/_ext.pyd": pe(["msvcp140.dll"]), "pkg.libs/msvcp140.dll": runtime})  # not mangled
        self.assertEqual(gap_wheels.missing_dlls(plain), ["msvcp140.dll"])
        nested = whl({"pkg/_ext.pyd": pe(["msvcp140-13f01359eefd96cadb3a8ee645921e00.dll"]),
                      "pkg/sub.libs/msvcp140-13f01359eefd96cadb3a8ee645921e00.dll": runtime})  # fmt: skip
        self.assertEqual(gap_wheels.missing_dlls(nested), ["msvcp140-13f01359eefd96cadb3a8ee645921e00.dll"])
        exe = whl({"pkg/bin/tool.exe": pe(["msvcp140.dll"])})  # an executable's DLLs are checked too
        self.assertEqual(gap_wheels.missing_dlls(exe), ["msvcp140.dll"])

    def test_native_libraries_are_the_files_to_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp) / "x.whl"
            w.write_bytes(whl({"lightgbm/__init__.py": b"", "lightgbm/bin/lib_lightgbm.dll": b"",
                               "lightgbm.libs/vcomp140-49e628b226e7b7d51c9b7e9d11d85efe.dll": b"",
                               "tool/libfoo.so.1.2": b"", "tool/libbar.so": b"", "tool/notes.so.txt": b"",
                               "x-1.0.data/scripts/odd.dll": b""}, dist="x-1.0"))  # fmt: skip
            self.assertEqual(gap_wheels.native_libraries(w), [
                "lightgbm.libs/vcomp140-49e628b226e7b7d51c9b7e9d11d85efe.dll", "lightgbm/bin/lib_lightgbm.dll",
                "tool/libbar.so", "tool/libfoo.so.1.2"])  # fmt: skip

    def test_finish_bundles_only_the_vc_runtime_and_retags_py3_none(self):
        built = Path("lightgbm-4.7.0-py3-none-win_arm64.whl")
        bundled = Path("bundled/lightgbm-4.7.0-py3-none-win_arm64.whl")
        final = Path("retagged/lightgbm-4.7.0-cp314-cp314-win_arm64.whl")
        work = Path("work")
        with mock.patch.object(gap_wheels.sys, "platform", "win32"), mock.patch.object(gap_wheels.os, "name", "nt"), \
             mock.patch.object(Path, "read_bytes", return_value=b""), \
             mock.patch.object(gap_wheels, "sha256", return_value="0" * 64), \
             mock.patch.object(gap_wheels, "missing_dlls", return_value=["msvcp140.dll", "vcomp140.dll"]), \
             mock.patch.object(gap_wheels, "bundle_dlls", return_value=bundled) as bundle, \
             mock.patch.object(gap_wheels, "retag", return_value=final) as tag:  # fmt: skip
            self.assertEqual(gap_wheels.finish_wheel(built, work, None, "cp314-cp314-win_arm64"), final)
            bundle.assert_called_once_with(built, work)
            tag.assert_called_once_with(bundled, work, "cp314-cp314-win_arm64")
        with mock.patch.object(gap_wheels.sys, "platform", "win32"), mock.patch.object(gap_wheels.os, "name", "nt"), \
             mock.patch.object(Path, "read_bytes", return_value=b""), \
             mock.patch.object(gap_wheels, "missing_dlls", return_value=["msvcp140.dll", "libssl-3.dll"]), \
             mock.patch.object(gap_wheels, "bundle_dlls", side_effect=AssertionError("bundled a third-party DLL")), \
             mock.patch.object(gap_wheels, "retag", return_value=final):  # fmt: skip
            # Not bundled: stage refuses it for libssl-3.dll.
            self.assertEqual(gap_wheels.finish_wheel(built, work, None, "cp314-cp314-win_arm64"), final)
        ext = Path("tiktoken-0.14.0-cp314-cp314-win_arm64.whl")
        with mock.patch.object(gap_wheels.sys, "platform", "win32"), mock.patch.object(gap_wheels.os, "name", "nt"), \
             mock.patch.object(Path, "read_bytes", return_value=b""), \
             mock.patch.object(gap_wheels, "missing_dlls", return_value=[]), \
             mock.patch.object(gap_wheels, "bundle_dlls", side_effect=AssertionError("nothing to bundle")), \
             mock.patch.object(gap_wheels, "retag", side_effect=AssertionError("already exact")):  # fmt: skip
            self.assertEqual(gap_wheels.finish_wheel(ext, work, None, "cp314-cp314-win_arm64"), ext)
        with mock.patch.object(gap_wheels.sys, "platform", "win32"), mock.patch.object(gap_wheels.os, "name", "nt"), \
             mock.patch.object(Path, "read_bytes", return_value=b""), \
             mock.patch.object(gap_wheels, "sha256", return_value="0" * 64), \
             mock.patch.object(gap_wheels, "missing_dlls", return_value=["vcomp140.dll"]), \
             mock.patch.object(gap_wheels, "bundle_dlls", return_value=None):  # fmt: skip
            self.assertIsNone(gap_wheels.finish_wheel(built, work, None, "cp314-cp314-win_arm64"))  # no result

    def test_retag_runs_the_pinned_wheel_tool_and_checks_the_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            src = work / "lightgbm-4.7.0-py3-none-win_arm64.whl"
            src.write_bytes(b"w")

            def fake_run(cmd, **kw):
                self.assertEqual(cmd[1:4], ["-m", "wheel", "tags"])
                self.assertEqual(cmd[4:10], ["--python-tag", "cp314", "--abi-tag", "cp314t", "--platform-tag",
                                             "win_arm64"])  # fmt: skip
                self.assertEqual(kw["env"]["SOURCE_DATE_EPOCH"], "1700000000")
                copy = Path(cmd[-1])
                copy.rename(copy.with_name("lightgbm-4.7.0-cp314-cp314t-win_arm64.whl"))
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(gap_wheels, "tool_venv", return_value=work / "bin") as venv, \
                 mock.patch.object(gap_wheels.subprocess, "run", side_effect=fake_run):  # fmt: skip
                got = gap_wheels.retag(src, work, "cp314-cp314t-win_arm64")
            venv.assert_called_once_with(work, "retag-tools", ("wheel==0.48.0", "packaging==26.3"))
            self.assertEqual(got.name, "lightgbm-4.7.0-cp314-cp314t-win_arm64.whl")

            def wrong_tag(cmd, **kw):
                Path(cmd[-1]).rename(Path(cmd[-1]).with_name("lightgbm-4.7.0-cp314-cp314-win_amd64.whl"))
                return subprocess.CompletedProcess(cmd, 0, "", "")

            shutil_dir = work / "retagged"
            for f in shutil_dir.glob("*"):
                f.unlink()
            with mock.patch.object(gap_wheels, "tool_venv", return_value=work / "bin"), \
                 mock.patch.object(gap_wheels.subprocess, "run", side_effect=wrong_tag):  # fmt: skip
                self.assertIsNone(gap_wheels.retag(src, work, "cp314-cp314t-win_arm64"))
