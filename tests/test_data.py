"""Validate the published index and request data, not fixture copies.

Malformed entries are rejected here instead of silently disappearing from the
generated index and human page. This never edits production data files.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
import tempfile
import unittest
from pathlib import Path

from scripts import build_gap_index, gap_wheels

ROOT = Path(__file__).resolve().parents[1] / "data"
WHEELS = ROOT / "gap-wheels.json"
REQUESTS = ROOT / "gap-wheel-requests.json"
OPTOUT = ROOT / "gap-wheels-optout.txt"

ENTRY_KEYS = {
    "name", "version", "target", "wheel", "sha256", "sdist", "sdist_sha256",
    "builds", "licence", "requires_python", "verified", "batch", "published", "publish_run",
}
SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
# A target dropped from PUBLISH_TAGS (say "3.14 win-arm64", later): its wheels stay published until withdrawn.
RETIRED_TARGET = re.compile(r"3\.([0-9]{1,2})(t?) [a-z0-9]+(?:-[a-z0-9_]+)+", re.ASCII)


def run_url(value: object) -> bool:
    """Require the project's own wheel or older tracker Actions run."""
    return (
        isinstance(value, str)
        and gap_wheels.RUN_URL.fullmatch(value) is not None
        and value.startswith(("https://github.com/wheelgap/wheels/actions/runs/",
                              "https://github.com/wheelgap/tracker/actions/runs/"))
    )


def versions(version: str) -> set[str]:
    """The spellings of `version` a wheel or sdist file name may carry, as gap_wheels.wheel_problem accepts them."""
    return {version.lower(), version.lower().replace("-", "_")}


def sdist_ok(sdist: object, project: str, version: str) -> bool:
    """A .tar.gz or .zip named `<project>-<version>` (any spelling of the name, a version spelling wheels may use)."""
    if not isinstance(sdist, str) or not sdist.endswith((".tar.gz", ".zip")):
        return False
    stem = sdist.lower().removesuffix(".tar.gz").removesuffix(".zip")
    return any(stem.endswith(f"-{v}") and build_gap_index.canonical(stem[: -len(v) - 1]) == project
               for v in versions(version))


def target_ok(filename: str, target: object) -> bool:
    """The wheel carries its target's one publishable tag; for a retired target, one plain CPython tag of its version."""
    if not isinstance(target, str):
        return False
    if target in gap_wheels.PUBLISH_TAGS:
        return gap_wheels.tag_ok(filename, target)
    m = RETIRED_TARGET.fullmatch(target)
    parts = filename[: -len(".whl")].split("-")
    if m is None or len(parts) not in (5, 6) or "." in "".join(parts[-3:]):
        return False
    py = f"cp3{m.group(1)}"
    return parts[-3] == py and parts[-2] == py + m.group(2)


TARGET_PYTHON = re.compile(r"3\.([0-9]{1,2})t? ", re.ASCII)  # "3.14t win-arm64" and "3.14 musl-aarch64" are both 3.14
CLAUSE = re.compile(r"(>=|<=|==|!=|~=|>|<)\s*([0-9]+(?:\.[0-9]+)*)(\.\*)?", re.ASCII)


def target_python(target: object) -> tuple[int, int, int] | None:
    """The Python a target (current or retired) is for, as pip compares it: (3, 14, 0) for 3.14 and for 3.14t."""
    m = TARGET_PYTHON.match(target) if isinstance(target, str) else None
    return (3, int(m.group(1)), 0) if m else None


def excluding_clause(requires_python: str, python: tuple[int, ...]) -> str | None:
    """The first clause of a requires-python value that leaves `python` out, or None if every clause admits it.
    Raises ValueError for a value this can't read: it knows the comparison operators with plain dotted-number
    releases, `.*` after `==` and `!=`, and `~=`, and nothing else (no `===`, no pre-releases, no empty clause)."""
    for clause in requires_python.split(","):
        m = CLAUSE.fullmatch(clause.strip())
        if m is None:
            raise ValueError(clause)
        op, release, wildcard = m.group(1), tuple(int(n) for n in m.group(2).split(".")), m.group(3) is not None
        if wildcard and op not in ("==", "!="):
            raise ValueError(clause)
        if op == "~=" and len(release) < 2:  # "~=3" has no last number to let vary
            raise ValueError(clause)
        width = max(len(release), len(python))
        ours = python + (0,) * (width - len(python))
        theirs = release + (0,) * (width - len(release))  # 3.10 is 3.10.0
        prefix = release[:-1] if op == "~=" else release  # ~=3.9.0 is >=3.9.0, ==3.9.*
        same_prefix = ours[: len(prefix)] == prefix
        admits = {
            ">=": ours >= theirs,
            "<=": ours <= theirs,
            ">": ours > theirs,
            "<": ours < theirs,
            "==": same_prefix if wildcard else ours == theirs,
            "!=": not same_prefix if wildcard else ours != theirs,
            "~=": ours >= theirs and same_prefix,
        }[op]
        if not admits:
            return clause.strip()
    return None


def requires_python_error(value: object, target: object) -> str | None:
    """What's wrong with an entry's requires_python, or None: it must be null (no limit) or a value that can be read
    and that admits the target's own Python. An installer skips a file whose requires-python leaves its Python out.
    The target's Python is checked twice: as X.Y.0 (how pip compares a pre-release of a new Python) and as X.Y.99,
    a patch release later than any real one, so a value like <3.14.1 or ==3.14.0 can't leave out every later 3.14."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return f"requires_python must be null or a non-empty string, not {value!r}"
    python = target_python(target)
    try:
        clause = excluding_clause(value, python or (3, 0, 0))
    except ValueError:
        return f"can't read requires_python {value!r}"
    if python is None:  # a target it can't read is reported by the target check
        return None
    if clause is not None:
        return f"requires_python {clause} excludes Python {python[0]}.{python[1]}, its target"
    if (clause := excluding_clause(value, (python[0], python[1], 99))) is not None:
        later = f"{python[0]}.{python[1]}.99"
        return f"requires_python {clause} excludes Python {later}, a later patch release of its target"
    return None


def published_date(value: object) -> dt.date | None:
    if not isinstance(value, str) or not DATE.fullmatch(value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def validation_errors(data: object, optout: set[str]) -> list[str]:
    """One actionable message per violated rule, identifying the affected wheel."""
    if not isinstance(data, dict) or data.get("schema") != gap_wheels.INDEX_SCHEMA:
        return ["data/gap-wheels.json: invalid index schema"]
    if not isinstance(data.get("wheels"), list):
        return ["data/gap-wheels.json: wheels is not a list"]
    errors: list[str] = []
    seen: set[str] = set()
    for i, entry in enumerate(data["wheels"]):
        if not isinstance(entry, dict):
            errors.append(f"entry {i}: not a wheel object")
            continue
        filename = entry.get("wheel")
        label = filename if isinstance(filename, str) else f"entry {i}"
        if missing := sorted(ENTRY_KEYS - set(entry)):  # a field added later is fine; one dropped or misspelt isn't
            errors.append(f"{label}: missing {missing}")
        if not build_gap_index.usable(entry):
            errors.append(f"{label}: build_gap_index.usable rejected the entry")
        if not isinstance(filename, str):
            continue
        if filename in seen:
            errors.append(f"{label}: duplicate wheel filename")
        seen.add(filename)
        if problem := requires_python_error(entry.get("requires_python"), entry.get("target")):
            errors.append(f"{label}: {problem}")

        name, version = entry.get("name"), entry.get("version")
        project = build_gap_index.canonical(name) if isinstance(name, str) else ""
        if not isinstance(name, str) or not project or project in optout:
            errors.append(f"{label}: invalid project or project opted out")
        if not isinstance(version, str):
            errors.append(f"{label}: invalid version")
            continue
        parsed = build_gap_index.wheel_tags(filename)
        if parsed is None or parsed[0] != project or parsed[1] not in versions(version):
            errors.append(f"{label}: wheel filename differs from name or version")
        if not target_ok(filename, entry.get("target")):
            errors.append(f"{label}: filename tag does not match its publish target")
        if not sdist_ok(entry.get("sdist"), project, version):
            errors.append(f"{label}: sdist filename does not match the project and version")
        for key in ("sha256", "sdist_sha256"):
            val = entry.get(key)
            if not isinstance(val, str) or not SHA256.fullmatch(val):
                errors.append(f"{label}: invalid lowercase {key}")

        # Only a wheel built with a library recipe has `recipes`; when it does, it is what publish writes.
        if "recipes" in entry and not gap_wheels.recipe_records_valid(entry["recipes"]):
            errors.append(f"{label}: invalid library recipes")

        builds = entry.get("builds")
        if not isinstance(builds, list) or len(builds) != 2 or not all(run_url(u) for u in builds):
            errors.append(f"{label}: builds must contain exactly two valid project Actions URLs")
        elif builds[0] == builds[1]:
            errors.append(f"{label}: two builds point at the same run")
        for key in ("batch", "publish_run"):
            if not run_url(entry.get(key)):
                errors.append(f"{label}: invalid {key} Actions URL")

        verified, published = published_date(entry.get("verified")), published_date(entry.get("published"))
        if verified is None:
            errors.append(f"{label}: verified must be a YYYY-MM-DD date")
        if published is None:
            errors.append(f"{label}: published must be a YYYY-MM-DD date")
        if verified is not None and published is not None and published < verified:
            errors.append(f"{label}: published date is before verified date")
    return errors


class PublishedData(unittest.TestCase):
    def test_real_wheel_index_is_well_formed(self):
        raw = json.loads(WHEELS.read_text(encoding="utf-8"))
        failures = validation_errors(raw, gap_wheels.opted_out(OPTOUT))
        self.assertEqual(failures, [], "\n".join(failures))

    def test_real_requests_are_never_silently_dropped(self):
        raw = json.loads(REQUESTS.read_text(encoding="utf-8"))
        self.assertIsInstance(raw, dict)
        result = build_gap_index.read_requests(REQUESTS)
        seen: dict[str, str] = {}
        for project in raw:
            canonical = build_gap_index.canonical(project)
            self.assertNotIn(
                canonical, seen, f"{project}: duplicate canonical project name also used by {seen.get(canonical)}"
            )
            seen[canonical] = project
            self.assertIn(
                canonical, result, f"{project}: request was rejected (check by and url fields)"
            )
        self.assertEqual(len(result), len(raw), f"{REQUESTS.name}: request count differs from source")

    def test_bad_request_handle_or_link_is_dropped_by_the_site_parser(self):
        original = {"example": {"by": "u/example", "url": "https://github.com/example"}}
        project = "example"
        for field, bad in (("by", "u/has space"), ("url", "https://evil.example/user")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as folder:
                damaged = copy.deepcopy(original)
                damaged[project][field] = bad
                copied = Path(folder) / "gap-wheel-requests.json"
                copied.write_text(json.dumps(damaged), encoding="utf-8")
                self.assertNotEqual(len(build_gap_index.read_requests(copied)), len(damaged),
                                    f"{project}: invalid {field} was not rejected")

    def test_mutations_are_rejected_with_the_wheel_named(self):
        data = json.loads(WHEELS.read_text(encoding="utf-8"))
        sample = next(w for w in data["wheels"] if w["target"].startswith("3.14"))  # the requires_python cases need 3.14
        cases = [
            ("missing key", lambda w: w.pop("sdist_sha256")),
            ("unusable", lambda w: w.update(sha256="invalid")),
            ("target", lambda w: w.update(target=next(t for t in gap_wheels.PUBLISH_TAGS if t != w["target"]))),
            ("retired target", lambda w: w.update(target="3.13 linux-riscv64")),
            ("wheel version", lambda w: w.update(wheel=w["wheel"].replace(w["version"], "999", 1))),
            ("sdist", lambda w: w.update(sdist="another-999.zip")),
            ("sdist hash", lambda w: w.update(sdist_sha256="F" * 64)),
            ("duplicate", lambda w: None),
            ("same run", lambda w: w.update(builds=[w["builds"][0]] * 2)),
            ("foreign run", lambda w: w.update(batch="https://github.com/other/repo/actions/runs/1")),
            ("bad date", lambda w: w.update(verified="2026-99-99")),
            ("date order", lambda w: w.update(published="2020-01-01")),
            ("recipes", lambda w: w.update(recipes=[{"library": "libyaml", "version": "0.2.5"}])),
            ("python too new", lambda w: w.update(requires_python=">=3.99")),
            ("python too old", lambda w: w.update(requires_python="<3.14")),
            ("python compatible release", lambda w: w.update(requires_python="~=3.9.0")),
            ("python wildcard", lambda w: w.update(requires_python="!=3.14.*")),
            ("python one bad clause", lambda w: w.update(requires_python=">=3.9,<3.14")),
            ("python number", lambda w: w.update(requires_python=42)),
            ("python no operator", lambda w: w.update(requires_python="42")),
            ("python words", lambda w: w.update(requires_python=">=three")),
            ("python empty", lambda w: w.update(requires_python="")),
            # Each admits 3.14.0 and leaves out a later 3.14: the X.Y.99 check names that Python.
            ("python late patch <", lambda w: w.update(requires_python="<3.14.1")),
            ("python late patch !=", lambda w: w.update(requires_python="!=3.14.99")),
            ("python late patch ==", lambda w: w.update(requires_python="==3.14.0")),
            ("python late patch ~=", lambda w: w.update(requires_python="~=3.14.0,<3.14.5")),
        ]
        for description, mutate in cases:
            with self.subTest(description=description):
                entry = copy.deepcopy(sample)
                mutate(entry)
                changed = {"schema": gap_wheels.INDEX_SCHEMA,
                           "wheels": [entry, copy.deepcopy(entry)] if description == "duplicate" else [entry]}
                errors = validation_errors(changed, set())
                self.assertTrue(errors, description)
                self.assertTrue(any(str(entry["wheel"]) in error for error in errors), errors)
                if description.startswith("python"):
                    self.assertEqual(len(errors), 1, errors)
                    self.assertIn("requires_python", errors[0])
                if description.startswith("python late patch"):
                    self.assertIn("excludes Python 3.14.99, a later patch release of its target", errors[0])
        for value in (">=3.10", "<4,>=3.9", "==3.14.*", "~=3.14.0"):  # admit 3.14.0 and every later 3.14
            with self.subTest(value=value):
                entry = copy.deepcopy(sample)
                entry["requires_python"] = value
                self.assertEqual(validation_errors({"schema": gap_wheels.INDEX_SCHEMA, "wheels": [entry]}, set()), [])

    def test_requires_python_must_admit_the_targets_own_python(self):
        wheel = "demo-1.0-cp314-cp314-win_arm64.whl"
        later = "a later patch release of its target"
        for target, value, expected in (
            ("3.14 musl-aarch64", ">=3.15", "requires_python >=3.15 excludes Python 3.14, its target"),
            ("3.14t win-arm64", "<3.14", "requires_python <3.14 excludes Python 3.14, its target"),
            ("3.15 win-amd64", "<3.15", "requires_python <3.15 excludes Python 3.15, its target"),
            ("3.15 win-amd64", ">=3.10, ==3.14.*", "requires_python ==3.14.* excludes Python 3.15, its target"),
            ("3.13 linux-riscv64", ">=3.14", "requires_python >=3.14 excludes Python 3.13, its target"),  # retired
            ("3.9 win-arm64", ">=3.10", "requires_python >=3.10 excludes Python 3.9, its target"),  # numbers, not text
            ("3.14 win-arm64", ">=3.9,===3.14", "can't read requires_python '>=3.9,===3.14'"),
            ("3.14 win-arm64", ">=3.14.0rc1", "can't read requires_python '>=3.14.0rc1'"),
            ("3.14 win-arm64", ">=3.9,", "can't read requires_python '>=3.9,'"),
            ("3.14 win-arm64", ">=3.*", "can't read requires_python '>=3.*'"),
            ("3.14 win-arm64", "~=3", "can't read requires_python '~=3'"),
            ("3.14 win-arm64", "  ", "requires_python must be null or a non-empty string, not '  '"),
            ("3.14 win-arm64", [">=3.9"], "requires_python must be null or a non-empty string, not ['>=3.9']"),
            ("not a target", ">=three", "can't read requires_python '>=three'"),
            ("3.14 musl-aarch64", "<3.14.1", f"requires_python <3.14.1 excludes Python 3.14.99, {later}"),
            ("3.14t win-arm64", "==3.14.0", f"requires_python ==3.14.0 excludes Python 3.14.99, {later}"),
            ("3.15 win-amd64", "==3.15.0", f"requires_python ==3.15.0 excludes Python 3.15.99, {later}"),
            ("3.14 win-arm64", "<=3.14", f"requires_python <=3.14 excludes Python 3.14.99, {later}"),  # <=3.14.0
            ("3.14 win-arm64", ">=3.9,<3.14.5", f"requires_python <3.14.5 excludes Python 3.14.99, {later}"),
            # Both checks fail: X.Y.0's message, as before.
            ("3.14 win-arm64", "<3.14.1,>=3.15", "requires_python >=3.15 excludes Python 3.14, its target"),
        ):
            with self.subTest(target=target, value=value):
                self.assertEqual(requires_python_error(value, target), expected)
                entry = {"wheel": wheel, "target": target, "requires_python": value}
                errors = validation_errors({"schema": gap_wheels.INDEX_SCHEMA, "wheels": [entry]}, set())
                self.assertIn(f"{wheel}: {expected}", errors)
        for target, value in (
            ("3.14 win-arm64", None),
            ("3.14 win-arm64", ">=3.14"),
            ("3.15 win-amd64", ">=3.15"),  # 3.15.0, as pip compares it, so an alpha of 3.15 is admitted
            ("3.14t linux-x86_64", "<4,>=3.9"),
            ("3.14 win-arm64", " >= 3.9 , < 4 "),
            ("3.14 win-arm64", "==3.14.*"),
            ("3.14t win-arm64", "~=3.14.0"),
            ("3.15 win-amd64", "~=3.15.0, <3.16"),
            ("not a target", ">=3.99"),  # the target check reports this entry
        ):
            with self.subTest(target=target, value=value):
                self.assertIsNone(requires_python_error(value, target))

    def test_every_target_names_its_python(self):
        for target in gap_wheels.PUBLISH_TAGS:
            with self.subTest(target=target):
                python = target_python(target)
                self.assertIsNotNone(python)
                self.assertEqual(f"{python[0]}.{python[1]}", target.split()[0].removesuffix("t"))
                self.assertEqual(python[2], 0)
        self.assertEqual(target_python("3.13 linux-riscv64"), (3, 13, 0))
        self.assertIsNone(target_python("pypy3.11 win-arm64"))
        self.assertIsNone(target_python(314))

    def test_requires_python_clauses(self):
        py314, py315 = (3, 14, 0), (3, 15, 0)
        for value, python, excluded in (
            (">=3.10", py314, None),
            (">=3.10", py315, None),
            ("<4.0.0,>=3.10.0", py314, None),
            ("<4.0.0,>=3.10.0", py315, None),
            ("==3.14.*", py314, None),
            ("==3.14.*", py315, "==3.14.*"),
            ("~=3.9", py314, None),  # >=3.9, ==3.*
            ("~=3.9", (4, 0, 0), "~=3.9"),
            ("~=3.9.0", py314, "~=3.9.0"),  # >=3.9.0, ==3.9.*
            ("~=3.14.0", py314, None),
            ("~=3.14.1", py314, "~=3.14.1"),
            ("~=3.14.0", py315, "~=3.14.0"),
            (">=3.9", py314, None),  # 3.9 is less than 3.14: numbers, not text
            ("<3.9", py314, "<3.9"),
            (">=3.14", py314, None),  # 3.14 is 3.14.0
            (">=3.14.0", py314, None),
            ("<=3.14", py314, None),
            ("==3.14", py314, None),
            ("==3.14.0.0", py314, None),
            ("==3.14", (3, 14, 1), "==3.14"),
            (">3.14", py314, ">3.14"),
            (">3.14", py315, None),
            ("<3.14", py314, "<3.14"),
            ("<=3.13", py314, "<=3.13"),
            (">=3.14.1", py314, ">=3.14.1"),
            ("!=3.14", py314, "!=3.14"),
            ("!=3.14.0", py314, "!=3.14.0"),
            ("!=3.14.1", py314, None),
            ("!=3.14.*", py314, "!=3.14.*"),
            ("!=3.14.*", py315, None),
            ("!=3.14.*", (3, 14, 1), "!=3.14.*"),  # every 3.14, not only 3.14.0
            ("==3.14.*", (3, 14, 1), None),
            ("!=3.1.*", py314, None),  # 3.1.* is not a text prefix match on 3.14
            ("==3.*", py315, None),
            (">=3.9, !=3.13.*, <3.15", py314, None),
            (">=3.9, !=3.13.*, <3.15", py315, "<3.15"),
            (">= 3.10", py314, None),
        ):
            with self.subTest(value=value, python=python):
                self.assertEqual(excluding_clause(value, python), excluded)
        for value in ("", "42", "3.14", ">=three", ">=3.x", "=>3.9", "===3.14", ">=3.14rc1", ">=3.14.dev0", "~=3",
                      ">=3.*", "~=3.9.*", ">=3.9,", ",>=3.9", ">=3.9;<4", ">=3.9 <4", "==*", ">=\u0663.9"):  # fmt: skip
            with self.subTest(value=value), self.assertRaises(ValueError):
                excluding_clause(value, py314)


if __name__ == "__main__":
    unittest.main()
