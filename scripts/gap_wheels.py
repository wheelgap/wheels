"""Gap wheels: verify which of the build lab's clean builds are reproducible and install, and publish them.

    python3 scripts/gap_wheels.py plan data/builds.json [--max N] [--only a,b] --github-output FILE
    python scripts/gap_wheels.py build --name NAME --version VERSION --target TARGET --attempt 1|2 --runner R --out FILE
    python3 scripts/gap_wheels.py dispatch --plan PLAN_JSON --batch RUNID-ATTEMPT
    python3 scripts/gap_wheels.py collect --plan PLAN_JSON --batch B --repo R --sha SHA --since TIME --results DIR
    python3 scripts/gap_wheels.py merge RESULTS_DIR --plan PLAN_JSON --out MANIFEST [--summary FILE]
    python3 scripts/gap_wheels.py stage --manifest MANIFEST --staged DIR --repo R [--github-output FILE]
    python scripts/gap_wheels.py install-test --staged DIR --target TARGET --out FILE
    python3 scripts/gap_wheels.py publish --staged-json JSON --files DIR --tested DIR --index data/gap-wheels.json \
        --release-dir DIR

A *pair* is a release and lab target the build lab built cleanly (data/builds.json: built, imported, a compiled
extension module or native code without one in a py3-none-<platform> wheel, and on free-threaded Python the GIL kept
off), so PyPI has no wheel for it there. Each pair is built
twice, in separate jobs on separate runners:

- from the sdist alone (`pip download --no-binary NAME`: build tools still come as wheels, as pip installs them),
- unpacked into a fixed folder, so the build path baked into a binary is the same both times,
- with SOURCE_DATE_EPOCH fixed, and on Windows with `-Brepro` for MSVC's compiler and linker and `codegen-units=1`
  for Rust (spelt `-Brepro`, since Git Bash rewrites a leading `/` into a Windows path).
- then made into the wheel that's published (finish_wheel): on Linux repaired with a pinned auditwheel, on Windows
  with the VC++ runtime DLLs it needs bundled by a pinned delvewheel, and a py3-none-<platform> wheel (native code
  without an extension module, such as LightGBM's ctypes DLL) retagged to its target's one exact tag.
- for a package whose extension needs a system library the build image lacks (PyYAML and libyaml), with that library
  built first from a pinned, sha256-checked source archive by a reviewed recipe (scripts/library_recipes.py) and
  linked in statically, and its licence text added to the wheel. Each build records the recipes it used; merge
  takes them only if they are the ones the plan gave the pair, in both builds.

A wheel is *verified* when both builds give byte-identical wheels from the sdist PyPI publishes (merge checks its
sha256 against PyPI's), and pip installs that wheel file, with `--only-binary :all:` (compiling nothing), into a fresh
virtual environment on the first build's runner. The merge job writes a manifest of the verified wheels: filename,
sha256, the sdist's name and sha256, and the run that built them.

Build jobs run each package's own build code with the runner's admin rights, so they're untrusted: they get a
read-only token and no secrets, and merge accepts a result only if it is exactly what `build` writes, for the pair
and attempt the plan gave that job. Two matching builds catch a build that isn't reproducible; they don't stop a
malicious package from shipping a malicious wheel *of itself* (its sdist would do the same on install).

What they must not be able to do is speak for another package. Build code with admin rights can read its job's
artifact token, and that token can write any artifact name in its own workflow run. So each build runs in a
workflow run of its own (`dispatch` starts one run of gap-wheel-build.yml per build, titled with its batch, pair and
attempt), and `collect` takes each result only from the one run with that exact title, started from main at the
coordinator's commit by workflow_dispatch after the batch began; two runs with the same title mean neither counts.
A build can then forge nothing but its own result. Each build run also keeps its wheel as its own `wheel` artifact;
stage takes a file from it only if its sha256 is the one both builds reported, so a build can't substitute bytes
either. See the stage/install-test/publish section below for what is made public, and how.
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import email.parser
import hashlib
import io
import json
import os
import platform
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_lab  # TARGETS, interpreter_problem, the name and version rules
import build_tool_lock
import library_recipes  # the allowlist of libraries a build may compile and link in, and for which pairs

SCHEMA = 1
SDIST = re.compile(r"[A-Za-z0-9._-]{1,120}\.(?:tar\.gz|zip)", re.ASCII)
WHEEL = re.compile(r"[A-Za-z0-9._+!-]{1,200}\.whl", re.ASCII)
SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
RESULT_KEYS = frozenset(
    {"schema", "name", "version", "target", "attempt", "runner", "sdist", "sdist_sha256", "wheel", "sha256", "installs", "tools"}
)
MAX_RESULT_BYTES = 64_000  # pair locks are bounded before dispatch by the coordinator
BUILD_TIMEOUT = build_lab.BUILD_TIMEOUT
INSTALL_TIMEOUT = build_lab.INSTALL_TIMEOUT


def native_without_extensions(r: dict) -> bool:
    """A lab build with no extension module whose wheel still carries native code for the target: tagged
    py3-none-<the target's platform>, as LightGBM's is (a DLL it loads with ctypes). A pure-Python wheel (py3-none-any)
    or one for another platform doesn't count. Such a wheel is retagged to the target's one exact tag when it's built
    (`retag`), so it's published, like every gap wheel, only for the Python it was built and tested on."""
    tag = r.get("wheel")
    if not isinstance(tag, str) or r.get("target") not in build_lab.TARGETS:
        return False
    parts = tag.split("-")
    if len(parts) != 3 or parts[0] not in ("py3", "py2.py3") or parts[1] != "none":
        return False
    pattern = build_lab.TARGETS[r["target"]]["wheel"]
    return all(re.fullmatch(pattern, p, re.ASCII) for p in parts[2].split("."))


def clean_pairs(builds: dict) -> list[dict]:
    """The lab's clean builds: (name, version, target) with a compiled module that imported, or native code without
    one (native_without_extensions), GIL off where it applies."""
    out = []
    for r in builds.get("results", []):
        if (
            r.get("outcome") == "built"
            and r.get("import") == "ok"
            and isinstance(r.get("extensions"), int)
            and (r["extensions"] > 0 or (r["extensions"] == 0 and native_without_extensions(r)))
            and r.get("gil") != "re-enabled"
            and r.get("target") in build_lab.TARGETS
            and build_lab.NAME.fullmatch(str(r.get("name")))
            and build_lab.VERSION.fullmatch(str(r.get("version")))
        ):
            out.append({"name": r["name"], "version": r["version"], "target": r["target"]})
    return sorted(out, key=lambda p: (build_lab.canonical(p["name"]), p["target"]))


def plan(builds: dict, limit: int, only: set[str] | None = None, start: int = 0,
         skipped: list[tuple[dict, str]] | None = None) -> list[dict]:  # fmt: skip
    """Two build jobs per pair, at most `limit` pairs, each with the runner (and Docker image) of its target. `start`
    rotates the (sorted) pairs first, so a weekly batch capped at `limit` reaches the end of the list too.

    A pair whose package has a library recipe for its target (scripts/library_recipes.py) is planned with it: both
    jobs carry `recipes`, what each build must record. A package known to need a library, with no recipe for the
    target, isn't planned at all (and goes into `skipped` with why): the lab may have built it on a runner that
    happens to have the library, which the pinned build image doesn't."""
    jobs = []
    pairs = []
    for p in clean_pairs(builds):
        if only is not None and build_lab.canonical(p["name"]) not in only:
            continue
        if (why := library_recipes.missing(p["name"], p["target"])) is not None:
            if skipped is not None:
                skipped.append((p, why))
            continue
        pairs.append(p)
    if pairs:
        offset = start % len(pairs)
        pairs = pairs[offset:] + pairs[:offset]
    pairs = pairs[:limit]
    for i, pair in enumerate(pairs):
        spec = build_lab.TARGETS[pair["target"]]
        recipes = library_recipes.records(pair["name"], pair["target"])
        for attempt in (1, 2):
            job = {**pair, "id": str(i), "attempt": attempt, "runner": spec["runner"], "python": spec["python"]}
            if "image" in spec:
                job["image"] = spec["image"]
            if recipes:  # only then: a pair without a recipe is planned exactly as before
                job["recipes"] = recipes
            jobs.append(job)
    return jobs


def cmd_plan(args: argparse.Namespace) -> int:
    builds = json.loads(Path(args.builds).read_text(encoding="utf-8"))
    only = {build_lab.canonical(n.strip()) for n in args.only.split(",") if n.strip()} if args.only else None
    # --rotate: start each week `--max` pairs further on, so a capped weekly batch covers every pair in turn. Weeks are
    # counted without resetting (an ISO week number jumps back to 1 each January).
    start = dt.date.today().toordinal() // 7 * args.max if args.rotate else 0
    no_recipe: list[tuple[dict, str]] = []
    jobs = plan(builds, args.max, only, start, no_recipe)
    for pair, why in no_recipe:
        print(f"{pair['name']} {pair['version']} {pair['target']}: skipped: {why}", file=sys.stderr)
    if args.lock_out:
        report: list[tuple[str, str]] = [(f"{p['name']} {p['version']} {p['target']}", f"skipped: {why}")
                                         for p, why in no_recipe]  # fmt: skip
        planned = len(jobs) // 2 + len(no_recipe)
        jobs, bundle = build_tool_lock.lock_plan(jobs, report)
        Path(args.lock_out).write_text(json.dumps(bundle, sort_keys=True) + "\n", encoding="utf-8")
        if getattr(args, "summary", None):
            lines = [f"**{len(jobs) // 2} locked, {planned - len(jobs) // 2} skipped** of {planned} pairs",
                     "", "| package / version / target | resolution |", "|---|---|"]
            lines += [f"| {name.replace('|', '&#124;')} | {status.replace('|', '&#124;')} |"
                      for name, status in report]
            with open(args.summary, "a", encoding="utf-8") as stream:
                stream.write("\n".join(lines) + "\n")
    hosted = [j for j in jobs if "image" not in j]
    docker = [j for j in jobs if "image" in j]
    print(f"{len(jobs) // 2} pairs, {len(jobs)} builds", file=sys.stderr)
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as f:
            f.write(f"hosted={json.dumps(hosted)}\nhosted_count={len(hosted)}\n")
            f.write(f"docker={json.dumps(docker)}\ndocker_count={len(docker)}\n")
            f.write(f"all={json.dumps(jobs)}\n")
    else:
        print(json.dumps(jobs, indent=1))
    return 0


# --- build (inside the untrusted job) ------------------------------------------------------------------------------


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unpack(sdist: Path, dest: Path) -> Path:
    """Unpack an sdist into `dest` (emptied first) and return its one top-level folder. tarfile's `data` filter refuses
    absolute paths, `..`, links out of the folder and device files; a zip member that would land outside is refused."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    if sdist.name.endswith(".zip"):
        with zipfile.ZipFile(sdist) as z:
            for member in z.namelist():
                if not (dest / member).resolve().is_relative_to(dest.resolve()):
                    raise ValueError(f"{member}: outside the sdist's folder")
            z.extractall(dest)
    else:
        with tarfile.open(sdist) as t:
            t.extractall(dest, filter="data")
    tops = list(dest.iterdir())
    if len(tops) != 1 or not tops[0].is_dir():
        raise ValueError("the sdist doesn't unpack into one folder")
    return tops[0]


def build_env() -> dict[str, str]:
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1", SOURCE_DATE_EPOCH="1700000000",
               PYTHONHASHSEED="0")  # fmt: skip
    if os.name == "nt":
        env.update(CL="-Brepro", LINK="-Brepro", RUSTFLAGS="-C link-arg=-Brepro -C codegen-units=1")
    return env


def wheel_members(wheel: Path) -> dict[str, str]:
    """Each file in the wheel, by name: its sha256 (streamed), or {} if the wheel can't be read. For saying *which*
    files differ when two builds don't agree; a wheel that isn't a readable zip still gets its result."""
    try:
        with zipfile.ZipFile(wheel) as z:
            return {
                i.filename: hashlib.file_digest(z.open(i), "sha256").hexdigest() for i in z.infolist() if not i.is_dir()
            }
    except Exception as e:  # noqa: BLE001  (diagnostics only: never cost the build its result)
        print(f"gap_wheels: couldn't list the wheel's files: {e}", file=sys.stderr)
        return {}


# Pinned, and installed into a venv of their own (not the build's): auditwheel bundles a wheel's libraries and
# patchelf rewrites their paths. SOURCE_DATE_EPOCH (build_env) fixes the repaired zip's timestamps.
REPAIR_TOOLS = build_lab.REPAIR_TOOLS
# The Windows counterpart: delvewheel bundles the DLLs a wheel's binaries import that a user's machine may not have
# (the VC++ runtime's msvcp140.dll, vcomp140.dll) into <dist>.libs/, each under a name with its hash, rewrites the
# imports to those names and adds the folder to the DLL search path in the package's __init__.py. A mangled name
# can't be met by a copy already on the system or loaded in the process, so the install test proves the bundled one
# loads. `wheel` retags a py3-none-<platform> wheel to its target's exact tag. Each with its dependencies pinned and
# installed with --no-deps and --require-hashes (PyPI lets a maintainer add files to an existing version), so nothing
# else is resolved or taken at build time. SOURCE_DATE_EPOCH fixes both tools' zips.
WINDOWS_REPAIR_TOOLS = ("delvewheel==1.13.2", "pefile==2024.8.26")
RETAG_TOOLS = ("wheel==0.48.0", "packaging==26.3")
TOOL_HASHES = {  # sha256 of each pinned tool's one wheel on PyPI
    "delvewheel==1.13.2": "acdb03e1d7040982db65e525065f08163e9dedad54eeb8c7038be61f76c08816",
    "pefile==2024.8.26": "76f8b485dcd3b1bb8166f1128d395fa3d87af26360c2358fb75b80019b957c6f",
    "wheel==0.48.0": "3217dcc807155e45db462d7ef2431f5ddda0d7273b700d05a67b271ceb1287ab",
    "packaging==26.3": "d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c",
}


def tool_venv(work: Path, name: str, tools: tuple[str, ...]) -> Path | None:
    """A fresh venv under WORK/NAME with exactly `tools` installed (wheels only, no dependency resolution): its
    scripts folder, or None (said why)."""
    venv = work / name
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=INSTALL_TIMEOUT)
    bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    py = bin_dir / ("python.exe" if os.name == "nt" else "python")
    pins = work / f"{name}.txt"
    pins.write_text("".join(f"{t} --hash=sha256:{TOOL_HASHES[t]}\n" for t in tools), encoding="utf-8")
    i = subprocess.run([str(py), "-m", "pip", "install", "--no-cache-dir", "--no-deps", "--only-binary", ":all:",
                        "--require-hashes", "-r", str(pins)], check=False, env=build_env(), capture_output=True,
                       text=True, errors="replace", timeout=INSTALL_TIMEOUT)  # fmt: skip
    if i.returncode != 0:
        print((i.stdout + i.stderr)[-3000:])
        print(f"gap_wheels: couldn't install {', '.join(tools)}", file=sys.stderr)
        return None
    return bin_dir


def one_wheel(folder: Path) -> Path | None:
    found = sorted(folder.glob("*.whl")) if folder.is_dir() else []
    return found[0] if len(found) == 1 and WHEEL.fullmatch(found[0].name) else None


def bundle_dlls(wheel: Path, work: Path) -> Path | None:
    """`delvewheel repair` of a Windows wheel into WORK/bundled, the DLLs loaded with ctypes (--analyze-existing) as
    well as the extension modules': the one wheel it writes, or None (said why)."""
    bin_dir = tool_venv(work, "bundle-tools", WINDOWS_REPAIR_TOOLS)
    if bin_dir is None:
        return None
    out = work / "bundled"
    try:
        r = subprocess.run([str(bin_dir / "python.exe"), "-m", "delvewheel", "repair", "--analyze-existing",
                            "-w", str(out), str(wheel)], check=False, env=build_env(), capture_output=True,
                           text=True, errors="replace", timeout=BUILD_TIMEOUT)  # fmt: skip
    except subprocess.TimeoutExpired:
        print("gap_wheels: delvewheel repair timed out", file=sys.stderr)
        return None
    print((r.stdout + r.stderr)[-4000:])
    found = one_wheel(out)
    if r.returncode != 0 or found is None:
        print(f"gap_wheels: delvewheel repair gave no single wheel (exit {r.returncode})", file=sys.stderr)
        return None
    return found


def pure_platform_tag(wheel_name: str) -> bool:
    """Whether a wheel file is tagged py3-none-<a platform> (native code, but no Python ABI in its tag)."""
    parts = wheel_name[: -len(".whl")].split("-")
    return len(parts) in (5, 6) and parts[-3] in ("py3", "py2.py3") and parts[-2] == "none" and parts[-1] != "any"


def retag(wheel: Path, work: Path, tag: str) -> Path | None:
    """The wheel retagged to exactly `tag` (python-abi-platform) in WORK/retagged, or None (said why)."""
    bin_dir = tool_venv(work, "retag-tools", RETAG_TOOLS)
    if bin_dir is None:
        return None
    py_tag, abi_tag, plat_tag = tag.split("-", 2)
    out = work / "retagged"
    out.mkdir(parents=True, exist_ok=True)
    copy = out / wheel.name
    shutil.copyfile(wheel, copy)
    r = subprocess.run([str(bin_dir / ("python.exe" if os.name == "nt" else "python")), "-m", "wheel", "tags",
                        "--python-tag", py_tag, "--abi-tag", abi_tag, "--platform-tag", plat_tag, "--remove",
                        str(copy)], check=False, env=build_env(), capture_output=True, text=True, errors="replace",
                       timeout=INSTALL_TIMEOUT)  # fmt: skip
    print((r.stdout + r.stderr)[-2000:])
    found = one_wheel(out)
    if r.returncode != 0 or found is None or not found.name.endswith(f"-{tag}.whl"):
        print(f"gap_wheels: retagging to {tag} failed (exit {r.returncode})", file=sys.stderr)
        return None
    return found


def repair(wheel: Path, work: Path, plat: str | None = None, locked_python: Path | None = None) -> Path | None:
    """`auditwheel repair` of a Linux wheel into WORK/repaired: the one wheel it writes, or None (said why). With
    `plat`, to exactly that platform tag (auditwheel refuses if the wheel needs a newer glibc or musl)."""
    if locked_python is None:
        print("gap_wheels: refusing auditwheel without the coordinator's repair lock", file=sys.stderr)
        return None
    # A separate venv keeps repair dependencies outside untrusted build hooks.
    bin_dir = locked_python.parent
    if not (bin_dir / "auditwheel").is_file():
        print("gap_wheels: locked auditwheel is missing", file=sys.stderr)
        return None
    env = dict(build_env(), PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    out = work / "repaired"
    try:
        plat_args = ["--plat", plat, "--only-plat"] if plat else []  # exactly that tag, no wider (older) one
        r = subprocess.run([str(bin_dir / "auditwheel"), "repair", *plat_args, "--wheel-dir", str(out), str(wheel)],
                           check=False,
                           env=env, capture_output=True, text=True, errors="replace",
                           timeout=BUILD_TIMEOUT)  # fmt: skip
    except subprocess.TimeoutExpired:
        print("gap_wheels: auditwheel repair timed out", file=sys.stderr)
        return None
    print((r.stdout + r.stderr)[-4000:])
    found = sorted(out.glob("*.whl")) if out.is_dir() else []
    if r.returncode != 0 or len(found) != 1 or not WHEEL.fullmatch(found[0].name):
        print(f"gap_wheels: auditwheel repair gave no single wheel (exit {r.returncode})", file=sys.stderr)
        return None
    return found[0]


def finish_wheel(
    wheel: Path, work: Path, plat: str | None, tag: str | None, platform_pattern: str | None = None,
    locked_python: Path | None = None,
) -> Path | None:
    """The built wheel as it will be compared and published, or None (said why): on Linux repaired to `plat`
    (auditwheel); a py3-none-<platform> wheel retagged to `tag`, its target's one publishable tag, but only if every
    platform in its own tag is the target's (`platform_pattern`, build_lab.TARGETS' `wheel`); then on Windows with
    the VC++ runtime DLLs it needs and a user may lack bundled (delvewheel), and only those, by name (any other
    missing DLL keeps it unpublishable, and stage says so). Retagged before bundling: delvewheel leaves Python's own
    vcruntime140 alone only for a wheel with a CPython tag."""
    wheels = [wheel]
    if sys.platform == "linux":
        # pip wheel tags a Linux build linux_*, which pip installs on any libc: auditwheel bundles the libraries it
        # needs and gives it the manylinux or musllinux tag it really meets. Before hashing, so both builds are
        # compared as repaired.
        # Logged for the reproducibility spike (#124): a pair that differs after repair but matched before points
        # at auditwheel or patchelf, not the compiler.
        print(f"gap_wheels: before repair: {wheels[0].name} sha256 {sha256(wheels[0])}")
        repaired = repair(wheels[0], work, plat, locked_python=locked_python)
        if repaired is None:
            print("gap_wheels: repair failed (the wheel built; auditwheel couldn't repair it)", file=sys.stderr)
            return None
        wheels = [repaired]
    if tag is not None and pure_platform_tag(wheels[0].name):
        platforms = wheels[0].name[: -len(".whl")].split("-")[-1].split(".")
        if platform_pattern is None or not all(re.fullmatch(platform_pattern, p, re.ASCII) for p in platforms):
            print(f"gap_wheels: {wheels[0].name} isn't for this target's platform; not retagged", file=sys.stderr)
            return None
        retagged = retag(wheels[0], work, tag)
        if retagged is None:
            return None
        wheels = [retagged]
    needs = missing_dlls(wheels[0].read_bytes()) if os.name == "nt" else None
    if needs and set(needs) <= VC_RUNTIME_DLLS:  # anything else keeps it unpublishable: stage says so, nothing bundled
        if (spaces := namespace_folders(wheels[0])):
            # delvewheel would add an __init__.py there, turning a shared namespace into a package of its own.
            print(f"gap_wheels: not bundled: namespace package folders {', '.join(spaces)}", file=sys.stderr)
            return wheels[0]
        print(f"gap_wheels: before bundling {', '.join(needs)}: {wheels[0].name} sha256 {sha256(wheels[0])}")
        bundled = bundle_dlls(wheels[0], work)
        if bundled is None:
            return None
        if added := sorted(init_files(bundled) - init_files(wheels[0])):
            # delvewheel adds an __init__.py to any top-level folder without one: a namespace package (or a stray
            # folder) would become a regular package and could break other distributions sharing it.
            print(f"gap_wheels: bundling added {', '.join(added[:5])}; not published", file=sys.stderr)
            return None
        wheels = [bundled]
    return wheels[0]


def init_files(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as z:
        return {n for n in z.namelist() if n.rpartition("/")[2] == "__init__.py"}


def namespace_folders(wheel: Path) -> list[str]:
    """The wheel's top-level folders that hold a .pyd or .dll but no __init__.py (a namespace package)."""
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
    tops = {n.split("/", 1)[0] for n in names if "/" in n and n.lower().endswith((".pyd", ".dll"))}
    tops = {t for t in tops if not t.endswith((".dist-info", ".data", ".libs"))}
    return sorted(t for t in tops if f"{t}/__init__.py" not in names)


# --- library recipes: a pinned system library, built before the package and linked into it statically ----------------
#
# For a pair with recipes (scripts/library_recipes.py: PyYAML needs libyaml), the build first makes each library, in
# the same image and run as the package: the source archive is fetched and checked against the recipe's sha256 before
# anything is unpacked, unpacked to a fixed path (as the sdist is, so no build path differs between the two builds),
# built with the recipe's fixed argv steps under build_env() into one prefix, and must leave a static library there
# and no shared one. The package's build then sees the prefix through the recipe's environment variables and links
# the library into its extension: the wheel gains no DT_NEEDED entry and auditwheel has nothing to graft. Each
# library's licence text, from the checked archive and itself checked against the recipe's sha256, is added to the
# finished wheel (add_bundled_licences), before hashing, so both builds are compared as published.
RecipeError = library_recipes.RecipeError
LIBRARY_ROOT = Path("C:/gw-lib" if os.name == "nt" else "/tmp/gw-lib")
MAX_LIBRARY_SOURCE_BYTES = 100_000_000
MAX_LICENCE_BYTES = 200_000


def fetch_library_source(recipe: dict, path: Path) -> None:
    """Download a recipe's source archive to `path`, or raise RecipeError. Nothing is written, so nothing can be
    unpacked, unless the bytes have the recipe's sha256."""
    request = urllib.request.Request(recipe["url"], headers={"User-Agent": "wheelgap-library-recipe/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(MAX_LIBRARY_SOURCE_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise RecipeError(f"couldn't download {recipe['url']}: {exc}") from exc
    if len(data) > MAX_LIBRARY_SOURCE_BYTES:
        raise RecipeError(f"{recipe['url']} is bigger than a library source may be")
    if hashlib.sha256(data).hexdigest() != recipe["sha256"]:
        raise RecipeError(f"{recipe['url']} isn't the archive the recipe pins (sha256 mismatch)")
    path.write_bytes(data)


def build_libraries(recipes: list[dict], work: Path, root: Path | None = None) -> tuple[dict[str, str], list[bytes]]:
    """Build each recipe's library, in order, into ROOT/prefix (ROOT emptied first; LIBRARY_ROOT, a fixed path, unless
    given): (the environment variables that show them to the package's build, each library's licence text). Raises
    RecipeError (or what unpack and subprocess raise) if anything isn't as the recipe says."""
    root = LIBRARY_ROOT if root is None else root
    shutil.rmtree(root, ignore_errors=True)
    prefix = root / "prefix"
    env: dict[str, str] = {}
    licences = []
    for recipe in recipes:
        if (why := library_recipes.problem(recipe)) is not None:
            raise RecipeError(f"not a recipe: {why}")
        label = f"{recipe['library']} {recipe['version']}"
        archive = work / "library-sources" / recipe["url"].rsplit("/", 1)[-1]
        archive.parent.mkdir(parents=True, exist_ok=True)
        fetch_library_source(recipe, archive)
        src = unpack(archive, root / "src" / f"{recipe['library']}-{recipe['version']}")
        licence = src / recipe["licence_file"]
        if licence.is_symlink() or not licence.is_file() or licence.stat().st_size > MAX_LICENCE_BYTES:
            raise RecipeError(f"{label}: no licence file {recipe['licence_file']}")
        text = licence.read_bytes()
        if hashlib.sha256(text).hexdigest() != recipe["licence_sha256"]:
            raise RecipeError(f"{label}: its licence text isn't the one the recipe pins")
        for step in recipe["steps"]:
            argv = [arg.replace("{prefix}", str(prefix)) for arg in step]
            done = subprocess.run(argv, cwd=src, check=False, env=build_env(), capture_output=True, text=True,
                                  errors="replace", timeout=BUILD_TIMEOUT)  # fmt: skip
            print((done.stdout + done.stderr)[-2000:])
            if done.returncode != 0:
                raise RecipeError(f"{label}: `{' '.join(step)}` failed (exit {done.returncode})")
        for rel in recipe["provides"]:
            if not (prefix / rel).is_file():
                raise RecipeError(f"{label}: the build left no {rel}")
        if shared := sorted(p.name for p in prefix.rglob("*") if re.search(r"\.so(\.[0-9]+)*$", p.name)):
            raise RecipeError(f"{label}: the build left a shared library ({', '.join(shared[:3])}); it must be static")
        env.update({k: v.replace("{prefix}", str(prefix)) for k, v in recipe["env"].items()})
        licences.append(text)
        print(f"gap_wheels: built {label} from {recipe['url']} (sha256 {recipe['sha256']})")
    return env, licences


def record_hash(data: bytes) -> str:
    """A file's hash as a wheel's RECORD writes it."""
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def dist_info_folder(names: list[str]) -> str:
    """The wheel's one .dist-info folder (RecipeError if there isn't exactly one)."""
    found = {n.partition("/")[0] for n in names if "/" in n and n.partition("/")[0].endswith(".dist-info")}
    if len(found) != 1:
        raise RecipeError("the wheel hasn't exactly one .dist-info folder")
    return found.pop()


def metadata_version(headers: email.message.Message) -> tuple[int, int] | None:
    found = re.fullmatch(r"(\d{1,3})\.(\d{1,3})", str(headers.get("Metadata-Version", "")).strip(), re.ASCII)
    return (int(found.group(1)), int(found.group(2))) if found else None


def with_licence_files(metadata: bytes, paths: list[str]) -> bytes:
    """A wheel's METADATA with a `License-File` line for each of `paths` (relative to .dist-info/licenses/), added as
    the last headers, before the description. Only for Metadata-Version 2.4 and later, where PEP 639 defines the
    field and that folder; older metadata is returned as it is (the file is still added to the wheel, in the same
    place, but that metadata has no standard field to name it in)."""
    text = metadata.decode("utf-8")
    version = metadata_version(email.parser.HeaderParser().parsestr(text))
    if version is None or "\r" in text:
        raise RecipeError("the wheel's METADATA isn't in a shape a licence file can be added to")
    if version < (2, 4):
        return metadata
    head, blank, body = text.partition("\n\n")
    lines = "".join(f"\nLicense-File: {p}" for p in paths)
    return (head.rstrip("\n") + lines + (blank + body if blank else "\n")).encode("utf-8")


def add_bundled_licences(wheel: Path, out_dir: Path, records: list[dict], texts: list[bytes]) -> Path:
    """The wheel, rewritten into OUT_DIR, with each recipe's licence text at library_recipes.licence_member, named in
    METADATA (with_licence_files) and listed in RECORD, whose METADATA line is updated too. Every other member keeps
    its bytes, order, timestamp and mode, and the new ones take METADATA's, so the result depends on nothing but the
    wheel and the texts: both builds, given equal wheels, write equal files. Raises RecipeError if the wheel isn't
    one this can be done to."""
    with zipfile.ZipFile(wheel) as z:
        infos = z.infolist()
        names = [i.filename for i in infos]
        folder = dist_info_folder(names)
        meta_name, record_name = f"{folder}/METADATA", f"{folder}/RECORD"
        if len(set(names)) != len(names) or meta_name not in names or record_name not in names:
            raise RecipeError("the wheel's members aren't what a wheel has")
        contents = {i.filename: z.read(i) for i in infos}
    by_name = {i.filename: i for i in infos}
    added: dict[str, bytes] = {}
    for rec, text in zip(records, texts, strict=True):
        member = library_recipes.licence_member(folder, rec)
        if member in contents or member in added:
            raise RecipeError(f"the wheel already has {member}")
        added[member] = text
    contents[meta_name] = with_licence_files(contents[meta_name], [m[len(f"{folder}/licenses/"):] for m in added])
    rows = [row for row in csv.reader(io.StringIO(contents[record_name].decode("utf-8"))) if row]
    if sum(row[0] == meta_name for row in rows) != 1 or sum(row[0] == record_name for row in rows) != 1:
        raise RecipeError("the wheel's RECORD doesn't list METADATA and itself once each")
    listed = [[meta_name, record_hash(contents[meta_name]), str(len(contents[meta_name]))] if row[0] == meta_name
              else row for row in rows if row[0] != record_name]  # fmt: skip
    listed += [[member, record_hash(text), str(len(text))] for member, text in added.items()]
    listed.append([record_name, "", ""])
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(listed)
    contents[record_name] = buf.getvalue().encode("utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / wheel.name
    with zipfile.ZipFile(out, "w") as z:

        def put(name: str, like: zipfile.ZipInfo, data: bytes) -> None:
            info = zipfile.ZipInfo(name, like.date_time)
            info.external_attr, info.compress_type, info.create_system = (
                like.external_attr, like.compress_type, like.create_system)  # fmt: skip
            z.writestr(info, data)

        for i in infos:
            if i.filename != record_name:
                put(i.filename, i, contents[i.filename])
        for member, text in added.items():
            put(member, by_name[meta_name], text)
        put(record_name, by_name[record_name], contents[record_name])  # last, as wheels have it
    return out


def bundled_licence_problem(wheel: bytes, records: list[dict]) -> str | None:
    """Why a wheel built with library recipes doesn't carry each library's licence text as its record says (None if it
    does): the file is where add_bundled_licences puts it, exactly once, with the recipe's sha256; RECORD lists it
    with that hash and size; and METADATA 2.4 or later names it in a License-File line. Trusted side: it only reads
    the wheel's bytes, and anything it can't read is a reason."""
    try:
        with zipfile.ZipFile(io.BytesIO(wheel)) as z:
            names = z.namelist()
            folder = dist_info_folder(names)
            meta_name, record_name = f"{folder}/METADATA", f"{folder}/RECORD"
            if names.count(meta_name) != 1 or names.count(record_name) != 1:
                return "couldn't read its bundled libraries' licence files"
            metadata, record = bounded(z, meta_name, 1_000_000), bounded(z, record_name, 20_000_000)
            if metadata is None or record is None:
                return "couldn't read its bundled libraries' licence files"
            headers = email.parser.HeaderParser().parsestr(metadata.decode("utf-8"))
            version = metadata_version(headers)
            named = {str(v).strip() for v in headers.get_all("License-File") or []}
            rows = {row[0]: row[1:] for row in csv.reader(io.StringIO(record.decode("utf-8"))) if row}
            for rec in records:
                label = f"{rec['library']} {rec['version']}"
                member = library_recipes.licence_member(folder, rec)
                text = bounded(z, member, MAX_LICENCE_BYTES) if names.count(member) == 1 else None
                if text is None:
                    return f"doesn't carry {label}'s licence text"
                if hashlib.sha256(text).hexdigest() != rec["licence_sha256"]:
                    return f"{label}'s licence text isn't the one its recipe pins"
                if rows.get(member) != [record_hash(text), str(len(text))]:
                    return f"its RECORD doesn't list {label}'s licence text"
                if version is None or (version >= (2, 4) and member[len(f"{folder}/licenses/"):] not in named):
                    return f"its METADATA doesn't name {label}'s licence file"
    except Exception:  # noqa: BLE001  (an odd zip, member, encoding or CSV: a reason, never a crashed batch)
        return "couldn't read its bundled libraries' licence files"
    return None


def grafted_library_problem(wheel: bytes) -> str | None:
    """Why a wheel built with library recipes isn't linked the way a recipe says (None if it is): a recipe's library
    is static, so auditwheel has nothing to bundle and the wheel has no <dist>.libs/ folder. One there means the
    extension linked a shared library from somewhere other than the recipe's checked source (a shared library that
    isn't bundled is refused by missing_libs already). The folder is found by where it's installed
    (build_lab.site_packages_paths), so one under <dist>.data/platlib/ or purelib/ counts too. Trusted side: it only
    reads the wheel's member names."""
    try:
        with zipfile.ZipFile(io.BytesIO(wheel)) as z:
            names = build_lab.site_packages_paths(z.namelist())
        grafted = any("/" in n and n.partition("/")[0].endswith(".libs") for n in names)
    except Exception:  # noqa: BLE001  (an odd zip: a reason, never a crashed batch)
        return "couldn't read its members"
    return "bundles a shared library in a .libs folder; a recipe's library must be linked in statically" if grafted else None


def _tool_version(cmd: list[str], *, include_path: bool = True) -> str:
    """Record the tool version; omit the ephemeral venv path for hash-locked tools."""
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, errors="replace", timeout=20)
        version = " ".join((result.stdout + "\n" + result.stderr).split())
        if not include_path:
            return version[:450]
        command = str(Path(cmd[0]).resolve()) if Path(cmd[0]).is_file() else (shutil.which(cmd[0]) or cmd[0])
        return (command + ": " + version)[:450]
    except (OSError, subprocess.SubprocessError):
        return ""


def _msvc_tools(target: str) -> tuple[str, str]:
    """Activate one chosen MSVC installation before backend hooks can run.

    PATH alone is insufficient: setuptools may call vcvarsall and Rust cc-rs
    may discover cl.exe through vswhere. Import the selected vcvarsall
    environment and tell setuptools and Cargo to use its exact tools.
    """
    arch = "arm64" if "win-arm64" in target else "x64"
    host = "Hostarm64" if platform.machine().lower() in ("arm64", "aarch64") else "Hostx64"
    roots = [Path(v) for k in ("ProgramFiles", "ProgramFiles(x86)", "VSINSTALLDIR")
             if (v := os.environ.get(k))]
    candidates: list[Path] = []
    for root in roots:
        candidates.extend(root.glob("Microsoft Visual Studio/*/*/VC/Tools/MSVC/*/bin/Host*/" + arch + "/cl.exe"))
        candidates.extend(root.glob("VC/Tools/MSVC/*/bin/Host*/" + arch + "/cl.exe"))
    if not candidates:
        return "", ""

    def installed_version(path: Path) -> tuple[int, tuple[int, ...], bool]:
        parts = path.parts
        release = next(
            (parts[i + 1] for i in range(len(parts) - 1) if parts[i] == "Microsoft Visual Studio"), ""
        )
        visual_studio = {"2022": 17, "2019": 16, "2017": 15}.get(release)
        if visual_studio is None:
            visual_studio = int(release) if release.isdigit() else 0
        toolset = next(
            (parts[i + 1] for i in range(len(parts) - 1) if parts[i] == "MSVC"), "0"
        )
        versions = tuple(int(x) for x in toolset.split(".") if x.isdigit())
        return visual_studio, versions, host.lower() in str(path).lower()

    # Resolve VS 18 ahead of VS 2022/17; prefer the actual host architecture.
    compiler = max(set(candidates), key=installed_version)
    linker = compiler.with_name("link.exe")
    vcvars = next(
        (parent / "Auxiliary" / "Build" / "vcvarsall.bat"
         for parent in compiler.parents
         if (parent / "Auxiliary" / "Build" / "vcvarsall.bat").is_file()),
        None,
    )
    if not linker.is_file() or vcvars is None:
        return "", ""

    source_host = "arm64" if "hostarm64" in str(compiler).lower() else "x64"
    tool_arch = arch if source_host == arch else f"{source_host}_{arch}"
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / "msvc-setup.cmd"
        script.write_text(
            f'@echo off\ncall "{vcvars}" {tool_arch} >nul\n'
            'if errorlevel 1 exit /b 1\nset\n', encoding="utf-8"
        )
        try:
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", str(script)], capture_output=True,
                text=True, errors="replace", timeout=90, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return "", ""
    if result.returncode:
        return "", ""

    active = {}
    for line in result.stdout.splitlines():
        if "=" in line and not line.startswith("="):
            key, value = line.split("=", 1)
            if key and "\0" not in key + value:
                active[key] = value
    if not active.get("VCINSTALLDIR") or not active.get("VCToolsInstallDir"):
        return "", ""
    # vcvarsall may default to a different toolset inside the same VS instance.
    if Path(active["VCToolsInstallDir"]).resolve() != compiler.parents[3].resolve():
        return "", ""
    os.environ.update(active)
    os.environ["DISTUTILS_USE_SDK"] = "1"
    os.environ["MSSdk"] = "1"
    os.environ["CC"] = str(compiler)
    os.environ["CXX"] = str(compiler)
    os.environ["CARGO_TARGET_AARCH64_PC_WINDOWS_MSVC_LINKER" if arch == "arm64"
               else "CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER"] = str(linker)
    os.environ["PATH"] = f"{compiler.parent}{os.pathsep}{os.environ.get('PATH', '')}"
    if Path(shutil.which("cl.exe") or "").resolve() != compiler.resolve():
        return "", ""
    if Path(shutil.which("link.exe") or "").resolve() != linker.resolve():
        return "", ""
    return _tool_version([str(compiler), "/Bv"]), _tool_version([str(linker), "/?"])


def expected_repair_tools(target: str) -> list[dict[str, str]]:
    """The separately hash-verified bundling and retagging tools on each platform."""
    tools = WINDOWS_REPAIR_TOOLS + RETAG_TOOLS if "win-" in target else RETAG_TOOLS
    # Linux auditwheel/patchelf and their dependencies are separately checked
    # against the coordinator lock in repair_packages.
    return [{"requirement": package, "sha256": TOOL_HASHES[package]}
            for package in sorted(set(tools))]


def build_tool_provenance(
    lock_sha256: str, packages: list[dict], target: str, build_bin: Path | None = None
) -> dict:
    """Record tools before the sdist's build backend is executed."""
    image = build_lab.TARGETS[target].get("image") or os.environ.get("ImageVersion", "")
    if os.name == "nt":
        # vcvarsall sets its own PATH. Put the hash-locked venv back first so
        # the backend cannot use Visual Studio's bundled cmake or ninja.
        cc, link = _msvc_tools(target)
        if build_bin is not None:
            os.environ["PATH"] = f"{build_bin}{os.pathsep}{os.environ.get('PATH', '')}"
    else:
        cc = _tool_version(["cc", "--version"])
        link = _tool_version(["ld", "--version"])
    rustc = _tool_version(["rustc", "--version", "--verbose"])
    # cmake's executable lives under a random build-venv path. Its wheel
    # identity is already pinned by lock_sha256, so record just its version.
    cmake = _tool_version(["cmake", "--version"], include_path=False)
    return {
        "lock_sha256": lock_sha256,
        "packages": [{"name": w["name"], "version": w["version"], "sha256": w["sha256"],
                      "filename": w["filename"]} for w in packages],
        "repair_packages": [],
        "repair_tools": expected_repair_tools(target),
        "python": platform.python_version(),
        "python_build": sys.version,
        "runner_image": image,
        "compiler": {"cc": cc, "link": link, "rustc": rustc, "cmake": cmake},
    }


def run_build(
    name: str,
    version: str,
    attempt: int,
    work: Path,
    members: dict[str, str] | None = None,
    keep: Path | None = None,
    plat: str | None = None,
    tag: str | None = None,
    platform_pattern: str | None = None,
    lock: dict | None = None,
    lock_sha256: str | None = None,
    recipes: list[dict] | None = None,
) -> dict:
    """Download the sdist, build the wheel from a fixed folder, hash it, and (first attempt) install it from a local
    folder into a fresh venv with nothing compiled. On Linux the wheel is repaired to `plat` (auditwheel); on Windows
    the DLLs it needs that a user may lack are bundled (delvewheel); a py3-none-<platform> wheel is retagged to `tag`,
    the target's one publishable tag. All before hashing, so both builds are compared as published. Fields left None
    mean that step didn't happen.

    With `recipes` (the pair's library recipes), each library is built first (build_libraries), the package's build
    gets the recipes' environment, the finished wheel gets the libraries' licence texts, and the result has a
    `recipes` field: what was built, or None if that step didn't happen. Without, nothing here changes."""
    fields: dict = {"sdist": None, "sdist_sha256": None, "wheel": None, "sha256": None,
                    "installs": None, "tools": None}
    if recipes:
        fields["recipes"] = None
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1")
    if lock is None or not build_tool_lock.validate(lock) or build_tool_lock.digest(lock) != lock_sha256:
        print("gap_wheels: no trusted build-tool lock; refusing unpinned build", file=sys.stderr)
        return fields
    if (lock["name"], lock["version"]) != (build_lab.canonical(name), version):
        print("gap_wheels: tool lock is for another package or version", file=sys.stderr)
        return fields
    sdists = work / "sdist"
    sdists.mkdir(parents=True, exist_ok=True)
    found = [sdists / lock["sdist"]]
    try:
        build_tool_lock.download_locked({"url": lock["sdist_url"], "sha256": lock["sdist_sha256"]}, found[0])
        fields.update(sdist=found[0].name, sdist_sha256=sha256(found[0]))
        py, packages = build_tool_lock.create_build_environment(lock, work)
        # Build backends may invoke a locked Python-supplied cmake/ninja executable.
        # This PATH is also used by the compiler version probes and build process.
        os.environ["PATH"] = f"{py.parent}{os.pathsep}{os.environ.get('PATH', '')}"
        tools = build_tool_provenance(lock_sha256, packages, lock["target"], build_bin=py.parent)
        if not tools["compiler"]["cc"] or not tools["compiler"]["link"] or not tools["runner_image"]:
            raise build_tool_lock.LockError("compiler or runner image version cannot be identified")
        fields["tools"] = tools
    except (build_tool_lock.LockError, OSError, subprocess.SubprocessError) as exc:
        print(f"gap_wheels: locked build setup failed: {exc}", file=sys.stderr)
        return fields
    library_env: dict[str, str] = {}
    licences: list[bytes] = []
    if recipes:
        # Before the package's build and apart from it: the only download is each recipe's pinned archive.
        try:
            library_env, licences = build_libraries(recipes, work)
        except (ValueError, tarfile.TarError, zipfile.BadZipFile, OSError, subprocess.SubprocessError) as exc:
            print(f"gap_wheels: library recipe failed: {exc}", file=sys.stderr)
            return fields
        fields["recipes"] = [library_recipes.record(r) for r in recipes]
    fixed = Path("C:/gw" if os.name == "nt" else "/tmp/gw")
    try:
        src = unpack(found[0], fixed)
    except (ValueError, tarfile.TarError, zipfile.BadZipFile, OSError) as e:
        print(f"gap_wheels: can't unpack the sdist: {e}", file=sys.stderr)
        return fields
    out = work / "out"
    try:
        p = subprocess.run([str(py), "-m", "pip", "wheel", "--no-build-isolation", "--no-index", "--no-deps",
                            "--no-cache-dir", "-w", str(out), str(src)], check=False,
                           env=dict(build_env(), PIP_NO_INDEX="1", **library_env), capture_output=True,
                           text=True, errors="replace", timeout=BUILD_TIMEOUT)  # fmt: skip
        print((p.stdout + p.stderr)[-6000:])
    except subprocess.TimeoutExpired:
        print("gap_wheels: the build timed out", file=sys.stderr)
        return fields
    wheels = sorted(out.glob("*.whl")) if out.is_dir() else []
    if p.returncode != 0 or len(wheels) != 1 or not WHEEL.fullmatch(wheels[0].name):
        print(f"gap_wheels: expected one wheel, found {len(wheels)}", file=sys.stderr)
        return fields
    repair_py = None
    if sys.platform == "linux":
        try:
            repair_environment = build_tool_lock.create_repair_environment(lock, work)
            if repair_environment is None:
                raise build_tool_lock.LockError("no coordinator-locked Linux repair tools")
            repair_py, repair_packages = repair_environment
            fields["tools"]["repair_packages"] = expected_packages({"wheels": repair_packages})
        except (build_tool_lock.LockError, OSError, subprocess.SubprocessError) as exc:
            print(f"gap_wheels: locked repair environment failed: {exc}", file=sys.stderr)
            return fields
    finished = finish_wheel(wheels[0], work, plat, tag, platform_pattern, locked_python=repair_py)
    if finished is None:
        return fields
    if recipes:
        # After the repair and before hashing, in both builds: a statically linked library's notice ships with it.
        try:
            finished = add_bundled_licences(finished, work / "licensed", fields["recipes"], licences)
        except (ValueError, KeyError, csv.Error, zipfile.BadZipFile, OSError) as exc:
            print(f"gap_wheels: couldn't add the bundled libraries' licence texts: {exc}", file=sys.stderr)
            return fields
    wheels = [finished]
    fields.update(wheel=wheels[0].name, sha256=sha256(wheels[0]))
    if keep is not None:  # uploaded as this run's `wheel` artifact; stage takes it only if its sha256 is verified
        keep.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(wheels[0], keep / wheels[0].name)
    if members is not None:
        members.update(wheel_members(wheels[0]))
    if attempt == 1:
        venv = work / "fresh"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=INSTALL_TIMEOUT)
        py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        try:
            # The file itself, not NAME==VERSION: PyPI may have a wheel of its own by now, which pip could prefer.
            i = subprocess.run([str(py), "-m", "pip", "install", "--only-binary", ":all:", "--no-cache-dir",
                                str(wheels[0])], check=False, env=env,
                               capture_output=True, text=True, errors="replace",
                               timeout=INSTALL_TIMEOUT)  # fmt: skip
            print((i.stdout + i.stderr)[-3000:])
            fields["installs"] = i.returncode == 0
        except subprocess.TimeoutExpired:
            fields["installs"] = False
    return fields


def cmd_build(args: argparse.Namespace) -> int:
    target = args.target
    if (
        target not in build_lab.TARGETS
        or not build_lab.NAME.fullmatch(args.name)
        or not build_lab.VERSION.fullmatch(args.version)
        or args.attempt not in (1, 2)
    ):
        print(f"gap_wheels: not a gap-wheels job: {args.name} {args.version} {target}", file=sys.stderr)
        return 2
    if (why := build_lab.interpreter_problem(target)) is not None:
        print(f"gap_wheels: this Python isn't {target}'s: {why}", file=sys.stderr)
        return 3
    if (why := library_recipes.missing(args.name, target)) is not None:
        # The plan skips such a pair; a build started for one anyway must not make a wheel without the library.
        print(f"gap_wheels: {args.name} {why}", file=sys.stderr)
        return 2
    members: dict[str, str] = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        keep = Path(args.wheel_dir) if args.wheel_dir else None
        plat = build_lab.TARGETS[args.target].get("plat")
        job = {"name": args.name, "version": args.version, "target": target}
        try:
            lock = build_tool_lock.load_lock(Path(args.lock_bundle), args.pair_id, args.lock_sha256, job)
        except (ValueError, OSError, KeyError) as exc:
            print(f"gap_wheels: refusing build without coordinator lock: {exc}", file=sys.stderr)
            return 2
        # The recipes are this commit's own, looked up here: never taken from a workflow input or from the package.
        recipes = library_recipes.for_pair(args.name, target)
        fields = run_build(args.name, args.version, args.attempt, Path(tmp), members, keep, plat=plat,
                           tag=PUBLISH_TAGS.get(target), platform_pattern=build_lab.TARGETS[target]["wheel"],
                           lock=lock, lock_sha256=args.lock_sha256, **({"recipes": recipes} if recipes else {}))
    result = {"schema": SCHEMA, "name": args.name, "version": args.version, "target": target,
              "attempt": args.attempt, "runner": args.runner, **fields}  # fmt: skip
    Path(args.out).write_text(json.dumps(result) + "\n", encoding="utf-8")
    # Beside the result, only to say which files differ when two builds don't agree; never part of a verdict.
    text = json.dumps(members, sort_keys=True) + "\n"
    if len(members) > MAX_MEMBERS or len(text) > MAX_MEMBERS_BYTES:
        text = "{}\n"  # too big to list: no diff listing, but the result itself must still arrive
    Path(args.out).with_name("members.json").write_text(text, encoding="utf-8")
    print(json.dumps(result, indent=1))
    return 0


# --- merge (trusted) -----------------------------------------------------------------------------------------------


def wheel_problem(wheel: object, job: dict) -> str | None:
    """Why a result's wheel filename can't be this pair's (None if it can): the project, version and platform tag."""
    if not isinstance(wheel, str) or not WHEEL.fullmatch(wheel):
        return "wheel"
    parts = wheel[: -len(".whl")].split("-")
    if len(parts) not in (5, 6):
        return "wheel name"
    name, version, platform = parts[0], parts[1], parts[-1]
    if build_lab.canonical(name) != build_lab.canonical(job["name"]):
        return "wheel is for another project"
    if version.lower() not in {job["version"].lower(), job["version"].lower().replace("-", "_")}:
        return "wheel is for another version"
    tag = build_lab.TARGETS[job["target"]]["wheel"]
    if not any(re.fullmatch(tag, p, re.ASCII) for p in platform.split(".")):  # a compressed tag set: one must match
        return "wheel isn't for the target"
    return None



def tool_provenance_valid(value: object) -> bool:
    """The provenance schema required for every new, publishable wheel."""
    if not isinstance(value, dict) or set(value) != {
        "lock_sha256", "packages", "repair_packages", "repair_tools",
        "python", "python_build", "runner_image", "compiler"
    }:
        return False
    if not (isinstance(value["lock_sha256"], str) and SHA256.fullmatch(value["lock_sha256"])
            and isinstance(value["python"], str)
            and re.fullmatch(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:\.dev\d+)?", value["python"])
            and isinstance(value["python_build"], str) and re.fullmatch(r"[ -~]{1,500}", value["python_build"])
            and isinstance(value["runner_image"], str) and re.fullmatch(r"[ -~]{1,500}", value["runner_image"])):
        return False
    compiler = value["compiler"]
    if not isinstance(compiler, dict) or set(compiler) != {"cc", "link", "rustc", "cmake"}:
        return False
    if not all(isinstance(c, str) and re.fullmatch(r"[ -~]{0,500}", c) for c in compiler.values()):
        return False
    if not compiler["cc"] or not compiler["link"]:
        return False
    pkgs = value["packages"]
    repair_pkgs = value["repair_packages"]
    if not isinstance(pkgs, list) or not 0 < len(pkgs) <= 100:
        return False
    if not isinstance(repair_pkgs, list) or len(repair_pkgs) > 100:
        return False
    repair_tools = value["repair_tools"]
    if not isinstance(repair_tools, list) or len(repair_tools) > 10:
        return False
    for item in repair_tools:
        if (not isinstance(item, dict) or set(item) != {"requirement", "sha256"}
                or not isinstance(item["requirement"], str)
                or re.fullmatch(r"[A-Za-z0-9._-]+==[A-Za-z0-9._+-]+", item["requirement"]) is None
                or not isinstance(item["sha256"], str) or SHA256.fullmatch(item["sha256"]) is None):
            return False
    # The build and repair virtual environments are independent. Packages
    # such as pip and packaging legitimately occur in both locked groups;
    # reject duplicates *within* either group, not across the groups.
    for group in (pkgs, repair_pkgs):
        names = set()
        for p in group:
            if not isinstance(p, dict) or set(p) != {"name", "version", "filename", "sha256"}:
                return False
            if not (isinstance(p["name"], str) and build_tool_lock.NAME.fullmatch(p["name"])
                    and p["name"] not in names and isinstance(p["version"], str)
                    and 0 < len(p["version"]) <= 100 and isinstance(p["filename"], str)
                    and build_tool_lock.WHEEL.fullmatch(p["filename"])
                    and isinstance(p["sha256"], str) and SHA256.fullmatch(p["sha256"])):
                return False
            names.add(p["name"])
    return True


def recipe_records_valid(value: object) -> bool:
    """The shape of a `recipes` field, wherever untrusted or stored data carries one (a build's result, the manifest,
    staged.json): a short list of what library_recipes.record writes, no library twice, each licence an OSI-approved
    SPDX expression. Shape only: whether they are the pair's recipes is checked against the plan and this commit's
    allowlist (problems, verify, cmd_stage)."""
    if not isinstance(value, list) or not 0 < len(value) <= library_recipes.MAX_RECIPES:
        return False
    seen = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != library_recipes.RECORD_KEYS:
            return False
        if not all(isinstance(v, str) for v in item.values()):
            return False
        if not (library_recipes.LIBRARY.fullmatch(item["library"]) and item["library"] not in seen
                and library_recipes.LIBRARY_VERSION.fullmatch(item["version"])
                and library_recipes.SOURCE_URL.fullmatch(item["url"]) and ".." not in item["url"]
                and all(SHA256.fullmatch(item[k]) for k in ("sha256", "licence_sha256", "recipe_sha256"))
                and len(item["licence"]) <= 100 and osi_expression(item["licence"])):  # fmt: skip
            return False
        seen.add(item["library"])
    return True


def expected_packages(lock: dict) -> list[dict]:
    return [{"name": w["name"], "version": w["version"], "sha256": w["sha256"],
             "filename": w["filename"]} for w in lock["wheels"]]


def problems(result: object, job: dict) -> list[str]:
    """What's wrong with an untrusted result for `job` (empty when it's exactly what `build` writes for that job)."""
    if not isinstance(result, dict):
        return ["not an object"]
    planned = job.get("recipes")  # the plan's library recipes for this pair, or nothing
    if set(result) != (RESULT_KEYS | {"recipes"} if planned else RESULT_KEYS):
        if set(result) == RESULT_KEYS | {"recipes"}:
            return ["library recipes on a pair that has none"]
        if planned and set(result) == RESULT_KEYS:
            return ["its library recipes are missing"]
        return ["keys"]
    out = []
    if result["schema"] != SCHEMA:
        out.append("schema")
    for key in ("name", "version", "target", "attempt", "runner"):
        if result[key] != job[key]:
            out.append(f"{key} isn't the planned one")
    if result["sdist"] is not None and not (isinstance(result["sdist"], str) and SDIST.fullmatch(result["sdist"])):
        out.append("sdist")
    for key in ("sdist_sha256", "sha256"):
        if result[key] is not None and not (isinstance(result[key], str) and SHA256.fullmatch(result[key])):
            out.append(key)
    if result["wheel"] is not None and (why := wheel_problem(result["wheel"], job)):
        out.append(why)
    if (result["wheel"] is None) != (result["sha256"] is None):
        out.append("wheel and sha256 go together")
    if result["wheel"] is not None and (result["sdist"] is None or result["sdist_sha256"] is None):
        out.append("a wheel needs its sdist and sdist_sha256")
    if result["installs"] is not None and not isinstance(result["installs"], bool):
        out.append("installs")
    if job["attempt"] == 2 and result["installs"] is not None:
        out.append("only the first build installs")
    if not tool_provenance_valid(result["tools"]):
        out.append("build tools are missing or invalid")
    elif job.get("lock_sha256") and result["tools"]["lock_sha256"] != job["lock_sha256"]:
        out.append("build tools do not match the planned lock")
    if planned:
        if result["recipes"] is None:
            if result["wheel"] is not None:
                out.append("a wheel needs its library recipes")
        elif not recipe_records_valid(result["recipes"]):
            out.append("library recipes are invalid")
        elif result["recipes"] != planned:
            out.append("library recipes are not the planned ones")
    return out


MEMBER = re.compile(r"[A-Za-z0-9._+/-]{1,200}", re.ASCII)
MAX_MEMBERS = 10_000
MAX_MEMBERS_BYTES = 2_000_000


def differing(first: object, second: object, shown: int = 5) -> str:
    """", in NAME, NAME (+N more)": the wheel files whose sha256 differs between two untrusted members.json, or ""
    when either is missing or malformed. Diagnostic text only: it never changes a verdict."""
    def ok(m: object) -> bool:
        return (isinstance(m, dict) and len(m) <= MAX_MEMBERS
                and all(isinstance(k, str) and MEMBER.fullmatch(k) and isinstance(v, str) and SHA256.fullmatch(v)
                        for k, v in m.items()))  # fmt: skip

    if not (ok(first) and ok(second)):
        return ""
    names = sorted(k for k in first.keys() | second.keys() if first.get(k) != second.get(k))
    if not names:
        return ""
    more = f" (+{len(names) - shown} more)" if len(names) > shown else ""
    return ", in " + ", ".join(f"`{n}`" for n in names[:shown]) + more


def verify(
    jobs: list[dict],
    results: dict[tuple[str, int], object],
    published: Callable[[str, str], dict[str, str] | None],
    members: dict[tuple[str, int], object] | None = None,
    locks: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """(verified wheels, every pair with its verdict) from the plan's jobs, the results by (id, attempt), and a lookup
    of PyPI's sdists for a release, {filename: sha256} or None if PyPI couldn't be asked. It's only asked about pairs
    whose two builds agree."""
    members = members or {}
    verified, report = [], []
    by_id: dict[str, dict[int, dict]] = {}
    for job in jobs:
        by_id.setdefault(job["id"], {})[job["attempt"]] = job
    for pid, attempts in sorted(by_id.items(), key=lambda kv: int(kv[0])):
        job = attempts[1]
        row = {"name": job["name"], "version": job["version"], "target": job["target"]}
        got = {a: results.get((pid, a)) for a in (1, 2)}
        bad = [f"build {a}: {p}" for a in (1, 2) if got[a] is not None for p in problems(got[a], attempts[a])]
        first, second = got[1], got[2]
        if bad:
            verdict = "rejected: " + "; ".join(bad)
        elif first is None or second is None:
            verdict = "a build has no result"
        elif first["sha256"] is None or second["sha256"] is None:
            verdict = "a build made no wheel"
        elif (first["wheel"], first["sha256"], first["sdist_sha256"]) != (
            second["wheel"],
            second["sha256"],
            second["sdist_sha256"],
        ):
            verdict = "the two builds differ" + differing(members.get((pid, 1)), members.get((pid, 2)))
        elif locks is not None and (
            not isinstance(locks, dict)
            or not build_tool_lock.validate(locks.get("pairs", {}).get(pid))
            or build_tool_lock.digest(locks["pairs"][pid]) != job.get("lock_sha256")
            or first["tools"]["packages"] != expected_packages(locks["pairs"][pid])
            or second["tools"]["packages"] != expected_packages(locks["pairs"][pid])
            or first["tools"]["repair_packages"]
            != expected_packages({"wheels": locks["pairs"][pid]["repair_wheels"]})
            or second["tools"]["repair_packages"]
            != expected_packages({"wheels": locks["pairs"][pid]["repair_wheels"]})
            or first["tools"]["repair_tools"] != expected_repair_tools(job["target"])
            or second["tools"]["repair_tools"] != expected_repair_tools(job["target"])
            or first["sdist"] != locks["pairs"][pid]["sdist"]
            or second["sdist"] != locks["pairs"][pid]["sdist"]
            or first["sdist_sha256"] != locks["pairs"][pid]["sdist_sha256"]
            or second["sdist_sha256"] != locks["pairs"][pid]["sdist_sha256"]
        ):
            verdict = "the coordinator's build-tool lock does not match the builds"
        elif first["tools"] != second["tools"]:
            verdict = "the two builds used different toolchains"
        elif (why := library_recipes.missing(job["name"], job["target"])) is not None:
            verdict = why  # the plan skips such a pair and the build refuses it: never verified without its library
        elif job.get("recipes") != (library_recipes.records(job["name"], job["target"]) or None):
            # The plan's own record, against this commit's allowlist: also a pair with a recipe, planned without it.
            verdict = "the planned library recipes are not this commit's"
        elif first.get("recipes") != second.get("recipes"):
            verdict = "the two builds used different library recipes"
        elif first.get("recipes") != job.get("recipes"):
            verdict = "the builds' library recipes are not the planned ones"
        elif (sdists := published(job["name"], job["version"])) is None:
            verdict = "couldn't check the sdist against PyPI"
        elif sdists.get(first["sdist"]) != first["sdist_sha256"]:
            verdict = "the sdist isn't the one PyPI publishes"
        elif first["installs"] is not True:
            verdict = "pip couldn't install it without compiling"
        else:
            verdict = "verified"
            verified.append({**row, "wheel": first["wheel"], "sha256": first["sha256"], "sdist": first["sdist"],
                             "sdist_sha256": first["sdist_sha256"], "tools": first["tools"]})  # fmt: skip
            if job.get("recipes"):
                verified[-1]["recipes"] = first["recipes"]
        report.append({**row, "verdict": verdict})
    return verified, report


def pypi_sdists(name: str, version: str) -> dict[str, str] | None:
    """The release's sdists on PyPI, {filename: sha256}, or None if PyPI couldn't be asked."""
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.load(r)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("urls"), list):
        return None
    return {
        u["filename"]: u.get("digests", {}).get("sha256")
        for u in data["urls"]
        if isinstance(u, dict) and u.get("packagetype") == "sdist" and isinstance(u.get("filename"), str)
    }


def read_result(path: Path) -> object:
    if path.stat().st_size > MAX_RESULT_BYTES:
        return "too big"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "not JSON"


def cmd_merge(args: argparse.Namespace) -> int:
    jobs = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    results: dict[tuple[str, int], object] = {}
    members: dict[tuple[str, int], object] = {}
    for job in jobs:
        path = Path(args.results) / f"result-{job['id']}-{job['attempt']}" / "result.json"
        if path.is_file():
            results[(job["id"], job["attempt"])] = read_result(path)
        mpath = path.with_name("members.json")
        if mpath.is_file() and mpath.stat().st_size <= MAX_MEMBERS_BYTES:
            try:
                members[(job["id"], job["attempt"])] = json.loads(mpath.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001,S110  (untrusted diagnostics, e.g. nesting deep enough for RecursionError)
                pass
    if not args.locks:
        raise SystemExit("gap_wheels: merge requires the coordinator's immutable build-tool locks")
    lock_path = Path(args.locks)
    if not lock_path.is_file() or lock_path.stat().st_size > 4_000_000:
        raise SystemExit("gap_wheels: missing or oversized build-tool lock bundle")
    locks = json.loads(lock_path.read_text(encoding="utf-8"))
    if not isinstance(locks, dict) or set(locks) != {"schema", "pairs"} or locks["schema"] != 1:
        raise SystemExit("gap_wheels: invalid coordinator lock bundle")
    verified, report = verify(jobs, results, pypi_sdists, members, locks)
    runs_file = Path(args.results) / "runs.json"
    if runs_file.is_file():  # written by collect, which is trusted
        urls = json.loads(runs_file.read_text(encoding="utf-8"))
        ids = {(j["name"], j["version"], j["target"]): j["id"] for j in jobs}
        for wheel in verified:
            pid = ids[(wheel["name"], wheel["version"], wheel["target"])]
            wheel["builds"] = [urls.get(f"{pid}-1"), urls.get(f"{pid}-2")]
    run = ""
    if all(os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")):
        run = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    date = dt.datetime.now(dt.UTC).date().isoformat()
    manifest = {"schema": SCHEMA, "date": date, "run": run, "wheels": verified}
    Path(args.out).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    head = f"**{len(verified)} verified** of {len(report)} pairs (byte-identical twice, and pip installs it with nothing compiled)"
    lines = [head, "", "| package | version | target | verdict |", "|---|---|---|---|"]
    lines += [f"| {r['name']} | {r['version']} | {r['target']} | {r['verdict']} |" for r in report]
    text = "\n".join(lines) + "\n"
    print(text)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(text)
    return 0


# --- dispatch and collect: one workflow run per build (trusted) ------------------------------------------------------

BUILD_WORKFLOW = "gap-wheel-build.yml"
BATCH = re.compile(r"[0-9]{1,20}-[0-9]{1,4}", re.ASCII)
SINCE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.ASCII)  # as the API writes created_at
REPO = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}", re.ASCII)
SHA = re.compile(r"[0-9a-f]{40}", re.ASCII)
MAX_ARTIFACT_BYTES = 3_000_000


def title(batch: str, job: dict) -> str:
    """The build run's title; gap-wheel-build.yml's run-name gives the same string from its inputs."""
    return f"gap-wheel {batch} {job['name']} {job['version']} {job['target']} build {job['attempt']}"


def gh(args: list[str]) -> bytes:
    return subprocess.run(["gh", *args], check=True, capture_output=True, timeout=120).stdout


def cmd_dispatch(args: argparse.Namespace) -> int:
    check_args(args)
    jobs = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    lock_path = Path(args.locks)
    if not lock_path.is_file() or lock_path.stat().st_size > 4_000_000:
        raise SystemExit("gap_wheels: no trusted coordinator locks for dispatch")
    bundle = json.loads(lock_path.read_text(encoding="utf-8"))
    if not isinstance(bundle, dict) or set(bundle) != {"schema", "pairs"} or bundle["schema"] != 1:
        raise SystemExit("gap_wheels: invalid coordinator lock bundle")
    for n, job in enumerate(jobs):
        # At most --parallel of this batch's builds at once, so the batch doesn't hold every runner the account has.
        while sum(r.get("status") != "completed" for r in this_batch(retry(lambda: list_runs(args.repo, args.since)),
                                                                      args.batch)) >= args.parallel:  # fmt: skip
            time.sleep(60)
        # A commit on main mid-batch would start the rest at another commit, which collect rightly refuses: stop loudly.
        # Checked last thing before starting the build, so the window is seconds, not the wait above.
        if (tip := retry(lambda: gh(["api", f"repos/{args.repo}/commits/main", "--jq", ".sha"])).decode().strip()) != args.sha:
            raise SystemExit(f"gap_wheels: main moved to {tip} after {n} of {len(jobs)} builds started; run the batch again")
        spec = build_lab.TARGETS[job["target"]]  # the plan's own values, checked again: they become a run's inputs
        if not (build_lab.NAME.fullmatch(job["name"]) and build_lab.VERSION.fullmatch(job["version"])):
            raise SystemExit(f"gap_wheels: not a plan job: {job!r}")
        if not build_tool_lock.SHA.fullmatch(job.get("lock_sha256", "")):
            raise SystemExit("gap_wheels: no trusted lock digest on planned build")
        lock = bundle["pairs"].get(str(job["id"]))
        if (not build_tool_lock.validate(lock) or build_tool_lock.digest(lock) != job["lock_sha256"]
                or (lock["name"], lock["version"], lock["target"])
                != (build_lab.canonical(job["name"]), job["version"], job["target"])):
            raise SystemExit("gap_wheels: dispatch lock does not match the planned pair")
        # Every build receives the same one-pair lock as an input. Do not grant
        # untrusted builds permissions to download coordinator artifacts.
        lock_json = json.dumps({"schema": 1, "pairs": {"0": lock}}, separators=(",", ":"), sort_keys=True)
        if len(lock_json.encode("utf-8")) > 30_000:
            raise SystemExit("gap_wheels: pair lock exceeds the dispatch input limit")
        fields = {"batch": args.batch, "name": job["name"], "version": job["version"], "target": job["target"],
                  "attempt": str(job["attempt"]), "runner": spec["runner"],
                  "lock_sha256": job["lock_sha256"], "lock_json": lock_json,
                  # In an image the build runs that image's interpreter for the target, not setup-python's.
                  "python": spec.get("interpreter", spec["python"]) if "image" in spec else spec["python"],
                  "image": spec.get("image", "")}  # fmt: skip
        command = ["workflow", "run", BUILD_WORKFLOW, "--repo", args.repo, "--ref", "main",
                   *[x for k, v in fields.items() for x in ("-f", f"{k}={v}")]]  # fmt: skip
        start(command, title(args.batch, job), args)
        print(f"started {title(args.batch, job)}", flush=True)
        time.sleep(2)  # well under GitHub's limit on write requests
    return 0


def start(command: list[str], name: str, args: argparse.Namespace, attempts: int = 4) -> None:
    """Start one build run. Starting is a POST, so a failed attempt may still have made the run: look for it before
    trying again, since a second run with the same title would make collect refuse both."""
    for attempt in range(attempts):
        try:
            gh(command)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            time.sleep(30 * (attempt + 1))
            if any(r.get("display_title") == name for r in retry(lambda: list_runs(args.repo, args.since))):
                return
    raise SystemExit(f"gap_wheels: couldn't start {name}")


def retry[T](call: Callable[[], T], attempts: int = 4) -> T:
    """`call`, retried with backoff on a failed gh command (GitHub's API has the odd 5xx and secondary rate limit)."""
    for attempt in range(attempts):
        try:
            return call()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if attempt == attempts - 1:
                raise
            time.sleep(30 * (attempt + 1))
    raise AssertionError("unreachable")


def this_batch(runs: list[dict], batch: str) -> list[dict]:
    prefix = f"gap-wheel {batch} "
    return [r for r in runs if isinstance(r, dict) and str(r.get("display_title", "")).startswith(prefix)]


def check_args(args: argparse.Namespace) -> None:
    if not (BATCH.fullmatch(args.batch) and SINCE.fullmatch(args.since) and REPO.fullmatch(args.repo)
            and SHA.fullmatch(args.sha)):  # fmt: skip
        raise SystemExit("gap_wheels: bad --batch, --since, --repo or --sha")


def pick_runs(jobs: list[dict], runs: list[dict], batch: str, sha: str, since: str) -> dict[tuple[str, int], dict | str]:
    """By (id, attempt): the one build run that can speak for that job, or why there isn't one."""
    by_title: dict[str, list[dict]] = {}
    for run in runs:
        if isinstance(run, dict) and isinstance(run.get("display_title"), str):
            by_title.setdefault(run["display_title"], []).append(run)
    out: dict[tuple[str, int], dict | str] = {}
    for job in jobs:
        found = by_title.get(title(batch, job), [])
        if not found:
            out[(job["id"], job["attempt"])] = "no build run"
        elif len(found) > 1:
            out[(job["id"], job["attempt"])] = "more than one build run with its title"
        elif not (
            found[0].get("event") == "workflow_dispatch"
            and found[0].get("head_branch") == "main"
            and found[0].get("head_sha") == sha
            and found[0].get("path") == f".github/workflows/{BUILD_WORKFLOW}"
            and str(found[0].get("created_at", "")) >= since
        ):
            out[(job["id"], job["attempt"])] = "the build run isn't one this batch started"
        elif found[0].get("status") != "completed":
            out[(job["id"], job["attempt"])] = "the build run hasn't finished"
        else:
            out[(job["id"], job["attempt"])] = found[0]
    return out


def list_runs(repo: str, since: str) -> list[dict]:
    query = f"event=workflow_dispatch&branch=main&created=%3E%3D{since}&per_page=100"
    pages = gh(["api", "--paginate", "--slurp", f"repos/{repo}/actions/workflows/{BUILD_WORKFLOW}/runs?{query}"])
    return [run for page in json.loads(pages) for run in page.get("workflow_runs", [])]


def fetch_result(repo: str, run: dict) -> tuple[bytes, bytes | None] | None:
    """The run's `result` artifact's result.json and members.json (None if it has none), or None."""
    listing = json.loads(gh(["api", f"repos/{repo}/actions/runs/{int(run['id'])}/artifacts?name=result"]))
    artifacts = [a for a in listing.get("artifacts", []) if a.get("name") == "result" and not a.get("expired")]
    if len(artifacts) != 1 or int(artifacts[0].get("size_in_bytes", 0)) > MAX_ARTIFACT_BYTES:
        return None
    archive = gh(["api", f"repos/{repo}/actions/artifacts/{int(artifacts[0]['id'])}/zip"])
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            result = bounded(z, "result.json", MAX_RESULT_BYTES)
            if result is None:
                return None
            try:
                members = bounded(z, "members.json", MAX_MEMBERS_BYTES) if "members.json" in z.namelist() else None
            except Exception:  # noqa: BLE001  (a bad members.json never costs the result)
                members = None
            return result, members
    except (zipfile.BadZipFile, KeyError, zlib.error, EOFError, RuntimeError, NotImplementedError, OSError):
        return None


def bounded(z: zipfile.ZipFile, name: str, limit: int) -> bytes | None:
    """The member's bytes, reading at most limit + 1 whatever size it declares (a zip bomb stops there), or None
    if it's bigger than limit."""
    with z.open(name) as f:
        data = f.read(limit + 1)
    return None if len(data) > limit else data


def cmd_collect(args: argparse.Namespace) -> int:
    """Wait for the batch's build runs, then put each result where merge looks: RESULTS/result-ID-ATTEMPT/result.json,
    and RESULTS/runs.json: by "ID-ATTEMPT", the build run's URL."""
    check_args(args)
    jobs = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    deadline = time.monotonic() + args.wait * 60
    while True:
        picked = pick_runs(jobs, retry(lambda: list_runs(args.repo, args.since)), args.batch, args.sha, args.since)
        waiting = [k for k, v in picked.items() if v in ("no build run", "the build run hasn't finished")]
        print(f"{len(picked) - len(waiting)} of {len(picked)} builds done", file=sys.stderr)
        if not waiting or time.monotonic() > deadline:
            break
        time.sleep(60)
    out = Path(args.results)
    urls = {}
    for (pid, attempt), run in picked.items():
        if isinstance(run, str):
            print(f"build {pid}-{attempt}: {run}", file=sys.stderr)
            continue
        urls[f"{pid}-{attempt}"] = f"https://github.com/{args.repo}/actions/runs/{int(run['id'])}"
        if (got := retry(lambda run=run: fetch_result(args.repo, run))) is not None:
            (out / f"result-{pid}-{attempt}").mkdir(parents=True, exist_ok=True)
            (out / f"result-{pid}-{attempt}" / "result.json").write_bytes(got[0])
            if got[1] is not None:
                (out / f"result-{pid}-{attempt}" / "members.json").write_bytes(got[1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "runs.json").write_text(json.dumps(urls) + "\n", encoding="utf-8")
    return 0


# --- stage, install-test and publish: making verified wheels public ---------------------------------------------------
#
# stage (trusted, in the coordinator): for each verified wheel, take the file from its build run's `wheel` artifact and
#   keep it only if its sha256 is the one both builds reported, it carries exactly its target's one tag, it needs
#   nothing a user's system may lack (on Windows a DLL, read from its PE imports; on Linux a library or a glibc symbol
#   version outside the manylinux_2_28 or musllinux_1_2 policy, read from its ELF files), PyPI names an OSI-approved
#   licence for the release, and the project hasn't opted out. A wheel built with library recipes must also record
#   exactly this commit's recipes for its pair and carry each library's licence text, with the recipe's sha256
#   (bundled_licence_problem). It only parses the wheel's bytes, never runs them.
# install-test (untrusted: it imports package code): on a fresh runner of the target, install each staged wheel
#   into a new venv and import it as the build lab does; a Linux wheel in a minimal container of its libc with the
#   target's exact CPython (LINUX_TESTS), not on the runner itself. A wheel built with library recipes must also pass
#   each recipe's import check there (library_checks: for PyYAML, that it runs on libyaml, not its pure-Python
#   fallback). Its answers only decide whether a byte-verified wheel is worth publishing; a package's code could lie
#   about them, but never change which bytes are published.
# publish (trusted, never runs package code): re-check every file against the sha256 stage passed it, then add it to
#   data/gap-wheels.json; the workflow attests the files, uploads them to the `gap-wheels` release and deploys the
#   index the site builds from that file (scripts/build_gap_index.py).

# The one tag a published wheel may carry for its target: the Python and platform its builds and install test ran on,
# nothing wider. Not abi3 or py3-none (other Pythons would take it untested), and not a compressed tag set (win_arm64.
# win_amd64 would put an ARM64 .pyd on x64; manylinux_2_28_x86_64.manylinux2014_x86_64 would claim a glibc it wasn't
# checked against). On Linux it's the tag auditwheel repairs to in the target's pinned image (build_lab.TARGETS'
# `plat`: manylinux_2_28 in the manylinux_2_28 image, musllinux_1_2 in Alpine), never linux_*, the tag `pip wheel`
# gives, which pip installs on any libc and version.
PUBLISH_TAGS = {
    "3.14 win-arm64": "cp314-cp314-win_arm64",
    "3.14t win-arm64": "cp314-cp314t-win_arm64",
    "3.15 win-amd64": "cp315-cp315-win_amd64",
    "3.14t linux-x86_64": "cp314-cp314t-manylinux_2_28_x86_64",
    "3.14 musl-aarch64": "cp314-cp314-musllinux_1_2_aarch64",
    "3.15 linux-x86_64": "cp315-cp315-manylinux_2_28_x86_64",
}
# DLLs every Windows machine with that CPython has: the interpreter's, the VC++ runtime Python's installer ships
# (vcruntime140 only, not msvcp140 or vcomp140), the Universal CRT and core system DLLs. An extension importing any
# other DLL that the wheel doesn't carry works on GitHub's images (they have the full VC++ redistributable) and fails
# on a user's machine with "DLL load failed", so it isn't published, unless it's the VC++ runtime, which the build
# bundles (finish_wheel, delvewheel) before hashing.
SYSTEM_DLLS = frozenset({
    "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll", "ucrtbase.dll", "kernel32.dll", "kernelbase.dll",
    "ntdll.dll", "user32.dll", "gdi32.dll", "advapi32.dll", "shell32.dll", "shlwapi.dll", "ole32.dll",
    "oleaut32.dll", "ws2_32.dll", "mswsock.dll", "bcrypt.dll", "bcryptprimitives.dll", "ncrypt.dll", "crypt32.dll",
    "secur32.dll", "sspicli.dll", "userenv.dll", "iphlpapi.dll", "dnsapi.dll", "dbghelp.dll", "psapi.dll",
    "version.dll", "winmm.dll", "rpcrt4.dll", "comctl32.dll", "comdlg32.dll", "setupapi.dll", "cfgmgr32.dll",
    "powrprof.dll", "pdh.dll", "netapi32.dll", "wldap32.dll", "winhttp.dll", "wininet.dll", "normaliz.dll", "msvcrt.dll",
    "imm32.dll", "propsys.dll", "dwmapi.dll", "uxtheme.dll", "opengl32.dll", "d3d11.dll", "dxgi.dll",
})  # fmt: skip
SYSTEM_DLL = re.compile(r"api-ms-win-[a-z0-9-]+\.dll|ext-ms-win-[a-z0-9-]+\.dll|python3[0-9]{1,2}t?\.dll", re.ASCII)
# The Linux counterpart, by the platform tag a target repairs to: the libraries every system of that tag has (PEP 600,
# PEP 656) and, for glibc, the symbol versions they may be asked for, by prefix (GLIBC_2.28 is "GLIBC" "2.28"). Copied
# from auditwheel's own policy, src/auditwheel/policy/manylinux-policy.json ("manylinux_2_28", x86_64) and
# musllinux-policy.json ("musllinux_1_2", whose "libc.so" auditwheel reads as the arch's libc.musl-*.so.1), at
# auditwheel 6.8.2, the version REPAIR_TOOLS pins. Bump them together. As in auditwheel, a symbol version whose prefix
# the policy doesn't list (OPENSSL_3.0.0 from a bundled library) isn't checked, the dynamic loader (ld-linux-*) is
# always there, and musl has no symbol versioning, so only its library list applies. Stricter than auditwheel: the
# versions a file needs from the loader are checked too (_dl_find_object@GLIBC_2.35 isn't in glibc 2.28). Not
# re-checked here: auditwheel's blacklist of zlib's internal symbols (the repair in the build refuses those). This
# covers the libraries a file links against, not ones its code dlopen()s by name at run time.
LINUX_POLICIES: dict[str, dict] = {
    "manylinux_2_28_x86_64": {
        "machine": 62,  # EM_X86_64
        "libs": frozenset({
            "libatomic.so.1", "libgcc_s.so.1", "libstdc++.so.6", "libm.so.6", "libmvec.so.1", "libanl.so.1",
            "libdl.so.2", "librt.so.1", "libc.so.6", "libnsl.so.1", "libutil.so.1", "libpthread.so.0", "libX11.so.6",
            "libXext.so.6", "libXrender.so.1", "libICE.so.6", "libSM.so.6", "libGL.so.1", "libgobject-2.0.so.0",
            "libgthread-2.0.so.0", "libglib-2.0.so.0", "libresolv.so.2", "libexpat.so.1", "libz.so.1",
            "ld-linux-x86-64.so.2",  # the dynamic loader: not in the policy's list, but auditwheel always allows it
        }),
        "versions": {
            "CXXABI": frozenset({"1.3", "1.3.1", "1.3.2", "1.3.3", "1.3.4", "1.3.5", "1.3.6", "1.3.7", "1.3.8",
                                 "1.3.9", "1.3.10", "1.3.11", "FLOAT128", "TM_1"}),
            "GCC": frozenset({"3.0", "3.3", "3.3.1", "3.4", "3.4.2", "3.4.4", "4.0.0", "4.2.0", "4.3.0", "4.7.0",
                              "4.8.0", "7.0.0"}),
            "GLIBC": frozenset({"2.2.5", "2.2.6", "2.3", "2.3.2", "2.3.3", "2.3.4", "2.4", "2.5", "2.6", "2.7", "2.8",
                                "2.9", "2.10", "2.11", "2.12", "2.13", "2.14", "2.15", "2.16", "2.17", "2.18", "2.22",
                                "2.23", "2.24", "2.25", "2.26", "2.27", "2.28"}),
            "GLIBCXX": frozenset({"3.4", "3.4.1", "3.4.2", "3.4.3", "3.4.4", "3.4.5", "3.4.6", "3.4.7", "3.4.8",
                                  "3.4.9", "3.4.10", "3.4.11", "3.4.12", "3.4.13", "3.4.14", "3.4.15", "3.4.16",
                                  "3.4.17", "3.4.18", "3.4.19", "3.4.20", "3.4.21", "3.4.22", "3.4.23", "3.4.24"}),
            "LIBATOMIC": frozenset({"1.0", "1.1", "1.2"}),
            "ZLIB": frozenset({"1.2.0", "1.2.0.2", "1.2.0.8", "1.2.2", "1.2.2.3", "1.2.2.4", "1.2.3.3", "1.2.3.4",
                               "1.2.3.5", "1.2.5.1", "1.2.5.2", "1.2.7.1", "1.2.9"}),
        },
    },
    "musllinux_1_2_aarch64": {
        "machine": 183,  # EM_AARCH64
        "libs": frozenset({"libc.musl-aarch64.so.1", "libz.so.1"}),
        "versions": {},
    },
}  # fmt: skip
# Where a Linux target's staged wheels are install-tested: `docker run` of a minimal image of its libc, pinned by
# digest, that has the target's exact CPython and little else, on a GitHub runner of its architecture. Not on the
# runner itself (GitHub's image has hundreds of libraries a user's system may lack) and not in the build image
# (manylinux's has compilers and -devel libraries). Each is `image`, the `interpreter` in it for the target and, for a
# CPython the image doesn't have, a python-build-standalone archive (`python_url`, `python_sha256`) that the job checks
# against that sha256 on the runner, unpacks and mounts read-only at /opt/python.
# - 3.14 musl: the official python:3.14-alpine (CPython 3.14.10 on Alpine's musl), the image the build uses.
# - 3.15 glibc: the official python:3.15-slim-trixie (CPython 3.15.2 on Debian 13's slim base, glibc 2.41).
# - 3.14t glibc: no official image has a free-threaded CPython, and Debian's own python3 is neither 3.14t nor
#   relocatable. So Debian 13's slim base image with python-build-standalone's 3.14t: the relocatable CPython builds uv
#   installs (Astral), self-contained apart from glibc, pinned by release and sha256 (from that release's SHA256SUMS).
#   Fetched by the runner, not in the container, so no curl or CA store is added to the image (pip brings certifi).
# The digests are each multi-arch image index (Docker Hub, 2026-11-17).
LINUX_TESTS: dict[str, dict[str, str]] = {
    "3.14t linux-x86_64": {
        "runner": "ubuntu-latest",
        "image": "debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2",
        "interpreter": "/opt/python/bin/python3.14t",
        "python_url": "https://github.com/astral-sh/python-build-standalone/releases/download/20261110/"
        "cpython-3.14.10%2B20261110-x86_64-unknown-linux-gnu-freethreaded-install_only.tar.gz",
        "python_sha256": "5cdd0e655393fa3ac2c361665fcc6bf518f075ac8d82dfa1d74c278a1aa59e7c",
    },
    "3.14 musl-aarch64": {
        "runner": "ubuntu-24.04-arm",
        "image": "python:3.14-alpine@sha256:5862d971d1f0ae26b258cefa4e893556b1d3aba81bd2ff30888704ad378bab67",
        "interpreter": "python",
    },
    "3.15 linux-x86_64": {
        "runner": "ubuntu-latest",
        "image": "python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928",
        "interpreter": "python",
    },
}
# OSI-approved licences by SPDX id (the common ones on PyPI); an expression passes only if every id is one of these.
OSI = frozenset(s.lower() for s in (
    "0BSD", "AFL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later", "Apache-1.1", "Apache-2.0", "APSL-2.0", "Artistic-2.0",
    "BSD-1-Clause", "BSD-2-Clause", "BSD-2-Clause-Patent", "BSD-3-Clause", "BSL-1.0", "CDDL-1.0", "CECILL-2.1",
    "ECL-2.0", "EFL-2.0", "EPL-1.0", "EPL-2.0", "EUPL-1.2", "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only",
    "GPL-3.0-or-later", "HPND", "ISC", "LGPL-2.0-only", "LGPL-2.0-or-later", "LGPL-2.1-only", "LGPL-2.1-or-later",
    "LGPL-3.0-only", "LGPL-3.0-or-later", "LPL-1.02", "MIT", "MIT-0", "MPL-1.1", "MPL-2.0", "MS-PL", "MS-RL", "MulanPSL-2.0",
    "NCSA", "OFL-1.1", "OSL-3.0", "PostgreSQL", "Python-2.0", "UPL-1.0", "Unicode-3.0", "Unicode-DFS-2016",
    "Unlicense", "W3C", "Zlib", "ZPL-2.0", "ZPL-2.1",
    # Deprecated SPDX ids still common on PyPI ("GPL-2.0+" is GPL-2.0-or-later).
    "AGPL-3.0", "GPL-2.0", "GPL-3.0", "LGPL-2.0", "LGPL-2.1", "LGPL-3.0",
    # Not on SPDX's OSI list, but the licence CPython itself is released under (OSI-approved as Python-2.0).
    "PSF-2.0",
))  # fmt: skip
# The legacy free-text `license` field is read only when it's one line that is an SPDX expression or exactly one of
# these unambiguous names; a pasted licence text or anything else is refused rather than guessed at.
LICENCE_NAMES = {
    "mit license": "MIT",
    "the mit license": "MIT",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license 2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "bsd 3-clause license": "BSD-3-Clause",
    "bsd 2-clause license": "BSD-2-Clause",
    "mozilla public license 2.0": "MPL-2.0",
    "isc license": "ISC",
}
ASSET = re.compile(r"[A-Za-z0-9._-]{1,200}\.whl", re.ASCII)  # release asset names: GitHub rewrites other characters
RUN_URL = re.compile(r"https://github\.com/[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}/actions/runs/([0-9]{1,20})")
MAX_WHEEL_BYTES = 500_000_000
MAX_TESTED_BYTES = 200_000
RELEASE = "gap-wheels"
INDEX_SCHEMA = 1


# Standard SPDX exceptions that only add permissions (what may follow WITH).
SPDX_EXCEPTIONS = frozenset(s.lower() for s in (
    "Autoconf-exception-2.0", "Autoconf-exception-3.0", "Bison-exception-2.2", "Classpath-exception-2.0",
    "Font-exception-2.0", "GCC-exception-2.0", "GCC-exception-3.1", "LGPL-3.0-linking-exception",
    "Linux-syscall-note", "LLVM-exception", "Qt-LGPL-exception-1.1", "Swift-exception",
    "Universal-FOSS-exception-1.0", "WxWindows-exception-3.1",
))  # fmt: skip


def osi_expression(expr: str) -> bool:
    """A well-formed SPDX expression in which every licence is OSI-approved and every WITH names a standard
    exception that only adds permissions. Grammar: or := and (OR and)*; and := one (AND one)*;
    one := "(" or ")" | ID["+"] [WITH EXCEPTION]."""
    tokens = re.findall(r"[A-Za-z0-9.+-]+|[()]", expr)
    if not tokens or "".join(tokens) != re.sub(r"\s+", "", expr) or len(tokens) > 100:
        return False
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def one() -> bool:
        nonlocal pos
        t = peek()
        if t is None or t.upper() in ("AND", "OR", "WITH") or t == ")":
            return False
        pos += 1
        if t == "(":
            if not either() or peek() != ")":
                return False
            pos += 1
            return True
        if t.lower().removesuffix("+") not in OSI:
            return False
        if (w := peek()) is not None and w.upper() == "WITH":
            pos += 1
            exc = peek()
            if exc is None or exc.lower() not in SPDX_EXCEPTIONS:
                return False
            pos += 1
        return True

    def joined(part, op: str) -> bool:
        nonlocal pos
        if not part():
            return False
        while (t := peek()) is not None and t.upper() == op:
            pos += 1
            if not part():
                return False
        return True

    def either() -> bool:
        return joined(lambda: joined(one, "AND"), "OR")

    return either() and pos == len(tokens)


def licence(info: object) -> str | None:
    """The release's licence when PyPI's metadata says it's OSI-approved (or the PSF licence): a PEP 639 SPDX
    expression whose every id is on the list, OSI-approved classifiers, or a one-line legacy `license` field that is
    such an expression or an unambiguous name ("Apache License 2.0"). None otherwise: a pasted licence text isn't
    read, so refuse rather than guess."""
    if not isinstance(info, dict):
        return None
    expr = info.get("license_expression")
    if isinstance(expr, str) and expr.strip():
        expr = expr.strip()
        return " ".join(expr.split()) if len(expr) <= 200 and osi_expression(expr) else None
    classifiers = info.get("classifiers") if isinstance(info.get("classifiers"), list) else []
    osi = [c.removeprefix("License :: OSI Approved :: ") for c in classifiers
           if isinstance(c, str) and c.startswith("License :: OSI Approved :: ")]  # fmt: skip
    if osi:
        return "; ".join(osi)[:200]
    text = info.get("license")
    if not isinstance(text, str) or "\n" in text.strip() or len(text.strip()) > 100:
        return None  # nothing, or a pasted licence text: not read
    text = text.strip()
    if (name := LICENCE_NAMES.get(re.sub(r"\s+", " ", text).lower())) is not None:
        return name
    return " ".join(text.split()) if osi_expression(text) else None


def pypi_info(name: str, version: str) -> dict | None:
    """PyPI's `info` for the release, or None if PyPI couldn't be asked."""
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=10) as r:
            data = json.load(r)
    except (OSError, ValueError):
        return None
    return data.get("info") if isinstance(data, dict) and isinstance(data.get("info"), dict) else None


def tag_ok(wheel: str, target: str) -> bool:
    """The wheel carries exactly its target's one publishable tag."""
    tag = PUBLISH_TAGS.get(target)
    return tag is not None and "-".join(wheel[: -len(".whl")].split("-")[-3:]) == tag


def pe_imports(data: bytes) -> set[str] | None:
    """The DLLs a PE file (.pyd/.dll) imports, normal and delay-loaded, lowercased; None if it can't be read."""
    info = pe_info(data)
    return None if info is None else info[1] | info[2]


def pe_info(data: bytes) -> tuple[int, set[str], set[str]] | None:
    """A PE file's machine (0xAA64 ARM64, 0x8664 x64) and the DLLs it imports, normal and delay-loaded apart,
    lowercased; None if it can't be read. Reads untrusted bytes, so every offset is bounds-checked and anything odd is
    None (the wheel isn't published)."""

    def u16(o: int) -> int:
        if o < 0 or o + 2 > len(data):
            raise ValueError
        return int.from_bytes(data[o : o + 2], "little")

    def u32(o: int) -> int:
        if o < 0 or o + 4 > len(data):
            raise ValueError
        return int.from_bytes(data[o : o + 4], "little")

    try:
        if data[:2] != b"MZ":
            return None
        pe = u32(0x3C)
        if data[pe : pe + 4] != b"PE\0\0":
            return None
        machine, sections, opt_size = u16(pe + 4), u16(pe + 6), u16(pe + 20)
        opt = pe + 24
        magic = u16(opt)
        if magic not in (0x10B, 0x20B):
            return None
        dirs = opt + (96 if magic == 0x10B else 112)
        count = u32(dirs - 4)
        table = opt + opt_size
        spans = [(u32(table + 40 * i + 12), max(u32(table + 40 * i + 8), u32(table + 40 * i + 16)),
                  u32(table + 40 * i + 20)) for i in range(min(sections, 96))]  # fmt: skip

        def offset(rva: int) -> int:
            for va, size, raw in spans:
                if va <= rva < va + size:
                    return raw + rva - va
            raise ValueError

        def name_at(rva: int) -> str:
            o = offset(rva)
            end = data.find(b"\0", o, o + 256)
            if end < 0:
                raise ValueError
            return data[o:end].decode("ascii").lower()

        found: tuple[set[str], set[str]] = (set(), set())
        for names, index, step, name_field in ((found[0], 1, 20, 12), (found[1], 13, 32, 4)):  # delay: name at +4
            if index >= count:
                continue
            rva = u32(dirs + 8 * index)
            if not rva:
                continue
            o = offset(rva)
            for n in range(4096):
                entry = data[o + n * step : o + (n + 1) * step]
                if len(entry) < step:
                    raise ValueError
                if not any(entry):
                    break
                names.add(name_at(u32(o + n * step + name_field)))
            else:
                raise ValueError
        return machine, found[0], found[1]
    except (ValueError, UnicodeDecodeError):
        return None


# A DLL delvewheel bundled: its name with a hash of its contents, so nothing else on the system answers to it.
MANGLED_DLL = re.compile(r"([a-z0-9_.+]+)-[0-9a-f]{32}\.dll", re.ASCII)
# The only DLLs a gap wheel may bundle: Microsoft's redistributable VC++ runtime (the "Distributable Code" list for
# Visual Studio), which a user's machine may lack and which Python's installer doesn't ship. Anything else a binary
# needs (a third-party library on the build runner) keeps the wheel out: its licence and provenance aren't checked.
VC_RUNTIME_DLLS = frozenset({
    "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll",
    "vcomp140.dll", "concrt140.dll", "vcruntime140.dll", "vcruntime140_1.dll",
})  # fmt: skip


def bundled_vc_runtime(name: str) -> bool:
    """Whether a file name (lower case) is a VC++ runtime DLL as delvewheel bundles it: mangled with its hash."""
    m = MANGLED_DLL.fullmatch(name)
    return m is not None and f"{m.group(1)}.dll" in VC_RUNTIME_DLLS


def missing_dlls(wheel: bytes) -> list[str] | None:
    """The DLLs the wheel's .pyd/.dll/.exe files import that a user's machine may not have and that the wheel doesn't
    carry beside the importing file (Windows looks in that file's own folder, not elsewhere in the wheel) or, for the
    VC++ runtime only and under a mangled name, in a top-level <dist>.libs/ folder (delvewheel's, which the package
    adds to the DLL search path; the install test proves it loads, since no other copy can answer to a mangled name);
    empty when it's self-contained, None if a file can't be read or there are too many to check."""
    with zipfile.ZipFile(io.BytesIO(wheel)) as z:
        binaries = [i for i in z.infolist() if i.filename.lower().endswith((".pyd", ".dll", ".exe"))]
        if len(binaries) > 500:
            return None
        beside: dict[str, set[str]] = {}
        for i in binaries:
            folder, _, name = i.filename.lower().rpartition("/")
            beside.setdefault(folder, set()).add(name)
        bundled = {
            name
            for folder, names in beside.items()
            if folder.endswith(".libs") and "/" not in folder
            for name in names
            if bundled_vc_runtime(name)
        }
        missing: set[str] = set()
        for info in binaries:
            data = bounded(z, info.filename, 200_000_000)
            read = pe_info(data) if data is not None else None
            if read is None:
                return None
            _, normal, delayed = read
            here = beside[info.filename.lower().rpartition("/")[0]]
            # The package's DLL search path (delvewheel's add_dll_directory) serves a .pyd/.dll's normal imports.
            # Not an .exe's (its own process) nor a delay-load (MSVC's helper calls LoadLibraryExA(name, NULL, 0)).
            served = here | bundled if not info.filename.lower().endswith(".exe") else here
            missing |= {n for n in normal if n not in served and n not in SYSTEM_DLLS and not SYSTEM_DLL.fullmatch(n)}
            missing |= {n for n in delayed if n not in here and n not in SYSTEM_DLLS and not SYSTEM_DLL.fullmatch(n)}
    return sorted(missing)


ELF_NAME = re.compile(r"[A-Za-z0-9._+-]{1,255}", re.ASCII)  # a library or version name: it ends up in reason text
ELF_SEARCH_PATH = re.compile(r"[A-Za-z0-9._+/${}:-]{0,4096}", re.ASCII)  # a DT_RUNPATH or DT_RPATH
PAGES = {62: 4096, 183: 65536}  # the largest page a kernel uses: x86_64 has only 4 KiB, aarch64 can have 64 KiB


def elf_needs(data: bytes, machine: int) -> tuple[set[str], set[str], list[str]] | None:
    """(the libraries a 64-bit little-endian ELF file for `machine` needs, its DT_NEEDED entries; the symbol versions
    it needs from them, from its version-needs table, .gnu.version_r: "GLIBC_2.17", "GLIBCXX_3.4.21"; and the folders
    the loader searches for them, its DT_RUNPATH, else its DT_RPATH, split at ":"), or None if it can't be read. Read
    as glibc's loader reads it: from the program headers and the dynamic segment, not the section headers, following
    the version-needs records' next links (as _dl_check_map_versions does) and requiring their counts to agree with
    DT_VERNEEDNUM and each vn_cnt. Versions needed from the loader itself (ld-linux-*) count like any other: glibc
    checks them too. A file with no dynamic segment (statically linked, or an object file) needs nothing. Reads
    untrusted bytes, so every offset is bounds-checked and anything odd is None (the wheel isn't published): another
    class, byte order or machine, loaded segments out of order or mapped over each other, a table that runs off the
    file or its segment, a name outside [A-Za-z0-9._+-]."""

    def u16(o: int) -> int:
        if o < 0 or o + 2 > len(data):
            raise ValueError
        return int.from_bytes(data[o : o + 2], "little")

    def u32(o: int) -> int:
        if o < 0 or o + 4 > len(data):
            raise ValueError
        return int.from_bytes(data[o : o + 4], "little")

    def u64(o: int) -> int:
        if o < 0 or o + 8 > len(data):
            raise ValueError
        return int.from_bytes(data[o : o + 8], "little")

    try:
        if data[:7] != b"\x7fELF\x02\x01\x01":  # ELFCLASS64, ELFDATA2LSB, EV_CURRENT
            return None
        if u16(16) not in (1, 2, 3) or u16(18) != machine:  # ET_REL, ET_EXEC or ET_DYN, for the target's machine
            return None
        phoff, phentsize, phnum = u64(0x20), u16(0x36), u16(0x38)
        if phnum and (phentsize != 56 or phnum > 256):  # 256 is far more than a linker writes
            return None
        loads, dynamic = [], None
        for i in range(phnum):
            p = phoff + 56 * i
            kind = u32(p)
            if kind == 1:  # PT_LOAD: (vaddr, size in the file, file offset, size in memory)
                loads.append((u64(p + 16), u64(p + 32), u64(p + 8), u64(p + 40)))
            elif kind == 2:  # PT_DYNAMIC: (vaddr, size)
                if dynamic is not None:
                    return None
                dynamic = (u64(p + 16), u64(p + 32))
        if dynamic is None:
            return set(), set(), []
        # The loader maps each segment over what came before, so offset() below, which reads a vaddr from the one
        # segment holding it, is only right when no two segments share a page with different file offsets. The ELF
        # spec has them in ascending vaddr order; anything else is refused rather than guessed at.
        page = PAGES.get(machine, 65536)
        for n, (va, filesz, off, memsz) in enumerate(loads):
            if memsz < filesz or (n and va < loads[n - 1][0]):
                return None
            for va2, _, off2, memsz2 in loads[:n]:
                overlap = va // page * page < -(-(va2 + memsz2) // page) * page
                if overlap and va - off != va2 - off2:
                    return None

        def offset(vaddr: int, size: int) -> int:
            """The file offset of `size` bytes at `vaddr`, all inside one loaded segment's bytes in the file."""
            for va, filesz, off, _ in loads:
                if va <= vaddr and vaddr + size <= va + filesz and off + (vaddr - va) + size <= len(data):
                    return off + (vaddr - va)
            raise ValueError

        if dynamic[1] % 16 or dynamic[1] // 16 > 4096:
            return None
        start = offset(dynamic[0], dynamic[1])
        needed, tags = [], {}
        for n in range(dynamic[1] // 16):
            tag, value = u64(start + 16 * n), u64(start + 16 * n + 8)
            if tag == 0:  # DT_NULL
                break
            if tag == 1:  # DT_NEEDED
                needed.append(value)
            elif tag in (5, 10, 15, 29, 0x6FFFFFFE, 0x6FFFFFFF):
                # DT_STRTAB, DT_STRSZ, DT_RPATH, DT_RUNPATH, DT_VERNEED, DT_VERNEEDNUM
                if tag in tags:
                    return None
                tags[tag] = value
        else:
            return None  # no DT_NULL
        if not needed and 0x6FFFFFFE not in tags:
            return set(), set(), []
        if 5 not in tags or 10 not in tags:
            return None
        strsz = tags[10]
        strtab = offset(tags[5], strsz)

        def string(o: int, pattern: re.Pattern[str] = ELF_NAME, limit: int = 256) -> str:
            if o >= strsz:
                raise ValueError
            end = data.find(b"\0", strtab + o, strtab + min(strsz, o + limit))
            if end < 0:
                raise ValueError
            text = data[strtab + o : end].decode("ascii")
            if not pattern.fullmatch(text):
                raise ValueError
            return text

        versions: set[str] = set()
        if 0x6FFFFFFE in tags:
            count = tags.get(0x6FFFFFFF)
            if count is None:
                return None
            # Elf64_Verneed: version, cnt, file, aux, next; Elf64_Vernaux: hash, flags, other, name, next. Both links
            # are offsets from their own record in memory; the walk ends at a 0 link, as the loader's does.
            v, files, seen = tags[0x6FFFFFFE], 0, 0
            while True:
                o = offset(v, 16)
                files += 1
                if u16(o) != 1 or files > count:
                    return None
                string(u32(o + 4))  # the library's name must be readable too
                a, auxes = v + u32(o + 8), 0
                while True:
                    seen += 1
                    if seen > 10_000:
                        return None
                    ao = offset(a, 16)
                    auxes += 1
                    versions.add(string(u32(ao + 8)))
                    if not u32(ao + 12):
                        break
                    a += u32(ao + 12)
                if auxes != u16(o + 2):
                    return None
                if not u32(o + 12):
                    break
                v += u32(o + 12)
            if files != count:
                return None
        search = tags.get(29, tags.get(15))  # DT_RUNPATH; DT_RPATH only when there's no DT_RUNPATH, as in glibc
        runpath = string(search, ELF_SEARCH_PATH, 4097).split(":") if search is not None else []
        return {string(o) for o in needed}, versions, [p for p in runpath if p]
    except (ValueError, UnicodeDecodeError):
        return None


SHARED_OBJECT = re.compile(r"[^/]*\.so(?:\.[0-9]+)*", re.ASCII)  # _ext.cpython-314t-x86_64-linux-gnu.so, libz-1a2b.so.1
MAX_ELF_BYTES = 1_000_000_000  # read from one wheel's ELF files in all


def installed(member: str, data_dir: str) -> tuple[str, str]:
    """Where a wheel member is installed: (scheme, path in it). `data_dir` is the wheel's one <name>-<version>.data
    folder, the one pip reads (named after its .dist-info folder); any other top-level folder is installed as is.
    purelib and platlib (data_dir/platlib/ or the wheel's root) are one place; data_dir/data/, scripts/ and headers/
    each another, which a RUNPATH can't reach."""
    top, _, rest = member.partition("/")
    if top == data_dir and "/" in rest:
        scheme, _, path = rest.partition("/")
        return ("lib" if scheme in ("purelib", "platlib") else scheme), path
    return "lib", member


def search_dirs(member: str, runpath: list[str], data_dir: str) -> list[tuple[str, str]]:
    """The installed folders in the wheel where glibc's loader finds a wheel ELF file's libraries: each RUNPATH (or
    RPATH) entry that is $ORIGIN or under it and stays inside its scheme. Not the file's own folder, which the loader
    doesn't search: a wheel that works only because the package preloads a library (with ctypes) isn't published.
    Other entries (/usr/lib, $LIB, a relative path) can't point into the wheel, so they don't count."""
    scheme, path = installed(member, data_dir)
    here = posixpath.dirname(path)
    dirs = []
    for entry in runpath:
        for origin in ("$ORIGIN", "${ORIGIN}"):
            if entry == origin or entry.startswith(origin + "/"):
                rest = entry[len(origin) :]
                break
        else:
            continue
        if "$" in rest:
            continue
        folder = posixpath.normpath(posixpath.join(here, "." + rest)) if here else posixpath.normpath("." + rest)
        if folder == ".." or folder.startswith("../"):
            continue
        dirs.append((scheme, "" if folder == "." else folder))
    return dirs


PE_MACHINES = {"win_arm64": 0xAA64, "win_amd64": 0x8664}


def foreign_binaries(wheel: bytes, plat: str) -> list[str] | None:
    """The wheel's .pyd/.dll/.exe files built for another CPU than the Windows platform tag `plat` says; None if one
    can't be read. A filename's tag is only a label: this reads each file's own PE header."""
    want = PE_MACHINES.get(plat)
    if want is None:
        return None
    with zipfile.ZipFile(io.BytesIO(wheel)) as z:
        binaries = [i.filename for i in z.infolist() if i.filename.lower().endswith((".pyd", ".dll", ".exe"))]
        if len(binaries) > 500:
            return None
        wrong = []
        for name in binaries:
            data = bounded(z, name, 200_000_000)
            info = pe_info(data) if data is not None else None
            if info is None:
                return None
            if info[0] != want:
                wrong.append(name)
    return wrong


def wheel_file_tags(wheel: bytes) -> list[str] | None:
    """The `Tag:` lines of the wheel's one .dist-info/WHEEL file, in order; None if there isn't exactly one, or it
    can't be read. What an installer reads, as against the filename, which the retag also rewrites."""
    with zipfile.ZipFile(io.BytesIO(wheel)) as z:
        found = [n for n in z.namelist() if re.fullmatch(r"[^/]+\.dist-info/WHEEL", n)]
        if len(found) != 1:
            return None
        data = bounded(z, found[0], 100_000)
    if data is None:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # As installers read it: email headers, so case-insensitive names and folded lines.
    return [t.strip() for t in email.parser.HeaderParser().parsestr(text).get_all("Tag") or []]


def wheel_tags_problem(wheel: bytes, tag: str | None) -> str | None:
    """Why the wheel's own WHEEL file doesn't say exactly `tag`, its target's one tag (None if it does)."""
    try:
        tags = wheel_file_tags(wheel)
    except zipfile.BadZipFile:
        tags = None
    if tag is None or tags != [tag]:
        return f"its WHEEL file's tags aren't exactly {tag}: {', '.join(tags or ['unreadable'])[:200]}"
    return None


def missing_libs(wheel: bytes, plat: str) -> list[str] | None:
    """What the wheel's ELF files (every shared object, by name, and any other file that is ELF) need that a system of
    the platform tag `plat` may not have: libraries that are neither on its policy's list (LINUX_POLICIES) nor in the
    wheel where the loader finds them from that file (search_dirs: auditwheel bundles them in <dist>.libs/ and points
    RUNPATH there with $ORIGIN), and for glibc symbol versions the policy doesn't allow (GLIBC_2.34 for manylinux_2_28). Empty when
    it needs nothing else; None if a file can't be read (elf_needs), there are too many or too much to check, or the
    wheel hasn't exactly one .dist-info folder (which names the .data folder pip installs from)."""
    policy = LINUX_POLICIES[plat]
    with zipfile.ZipFile(io.BytesIO(wheel)) as z:
        files = [i for i in z.infolist() if not i.is_dir()]
        dist_info = {i.filename.partition("/")[0] for i in files if i.filename.partition("/")[0].endswith(".dist-info")}
        if len(dist_info) != 1:
            return None
        data_dir = dist_info.pop()[: -len(".dist-info")] + ".data"
        where = {installed(i.filename, data_dir) for i in files}
        binaries = []
        for i in files:
            if SHARED_OBJECT.fullmatch(i.filename.rpartition("/")[2]):
                binaries.append(i)
            else:
                with z.open(i) as f:
                    if f.read(4) == b"\x7fELF":
                        binaries.append(i)
            if len(binaries) > 500:
                return None
        missing: set[str] = set()
        budget = MAX_ELF_BYTES
        for info in binaries:
            data = bounded(z, info.filename, min(200_000_000, budget))
            needs = elf_needs(data, policy["machine"]) if data is not None else None
            if needs is None:
                return None
            budget -= len(data)
            libs, versions, runpath = needs
            dirs = search_dirs(info.filename, runpath, data_dir)
            for lib in libs:
                if lib not in policy["libs"] and not any((s, posixpath.join(d, lib)) in where for s, d in dirs):
                    missing.add(lib)
            for v in versions:
                prefix, _, version = v.partition("_")
                if prefix in policy["versions"] and version not in policy["versions"][prefix]:
                    missing.add(v)
    return sorted(missing)


def unwrap_wheel(archive: bytes, filename: str, digest: str) -> bytes | None:
    """The wheel from a `wheel` artifact zip: exactly one member, named `filename`, whose sha256 is `digest`."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            if z.namelist() != [filename]:
                return None
            data = bounded(z, filename, MAX_WHEEL_BYTES)
    except (zipfile.BadZipFile, KeyError, zlib.error, EOFError, RuntimeError, NotImplementedError, OSError):
        return None
    return data if data is not None and hashlib.sha256(data).hexdigest() == digest else None


def fetch_wheel(repo: str, run_id: int) -> bytes | None:
    """The zip of a build run's `wheel` artifact, or None."""
    listing = json.loads(gh(["api", f"repos/{repo}/actions/runs/{run_id}/artifacts?name=wheel"]))
    artifacts = [a for a in listing.get("artifacts", []) if a.get("name") == "wheel" and not a.get("expired")]
    if len(artifacts) != 1 or int(artifacts[0].get("size_in_bytes", 0)) > MAX_WHEEL_BYTES:
        return None
    return subprocess.run(["gh", "api", f"repos/{repo}/actions/artifacts/{int(artifacts[0]['id'])}/zip"],
                          check=True, capture_output=True, timeout=600).stdout  # fmt: skip


def opted_out(path: Path) -> set[str]:
    """Canonical project names from the opt-out file (one per line, # comments)."""
    if not path.is_file():
        return set()
    names = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        name = line.split("#", 1)[0].strip()
        if not name:
            continue
        if not build_lab.NAME.fullmatch(name):
            raise ValueError(f"{path}:{line_number}: invalid project name {name!r}")
        names.add(build_lab.canonical(name))
    return names


def stage_reason(wheel: dict, info: dict | None, optout: set[str]) -> str | None:
    """Why a verified wheel isn't staged (None: stage it)."""
    if not ASSET.fullmatch(wheel["wheel"]):
        return "its filename can't be a release asset name"
    if not tag_ok(wheel["wheel"], wheel["target"]):
        unrepaired = wheel["wheel"].rpartition("-")[2].startswith("linux_")
        return (f"not tagged exactly {PUBLISH_TAGS.get(wheel['target'], 'a publishable tag')}"
                + (" (a linux_* wheel is one auditwheel didn't repair)" if unrepaired else ""))  # fmt: skip
    if build_lab.canonical(wheel["name"]) in optout:
        return "the project opted out"
    if info is None:
        return "couldn't ask PyPI for the licence"
    if licence(info) is None and not licence_unstated(info):
        return "PyPI names no OSI-approved licence for it"
    return None  # PyPI says nothing at all: the wheel's own METADATA must (cmd_stage, metadata_licence)


def licence_unstated(info: dict) -> bool:
    """Whether PyPI's metadata for the release says nothing about its licence: no expression, no licence classifier,
    no legacy field. (PyPI's JSON can miss a PEP 639 License-Expression the upload's METADATA has: LightGBM 4.7.0.)"""
    classifiers = info.get("classifiers") if isinstance(info.get("classifiers"), list) else []
    return (
        not (isinstance(info.get("license_expression"), str) and info["license_expression"].strip())
        and not any(isinstance(c, str) and c.startswith("License ::") for c in classifiers)
        and not (isinstance(info.get("license"), str) and info["license"].strip())
    )


def metadata_licence(wheel: bytes) -> str | None:
    """The wheel's own METADATA License-Expression (PEP 639) when there's exactly one and every id in it is
    OSI-approved (or the PSF licence); None otherwise. Its METADATA comes from the sdist PyPI publishes (the pair is
    built from it and byte-identical), so it's the author's own declaration, as PyPI's metadata is. Only an SPDX
    expression counts here, never a free-text License field."""
    try:
        with zipfile.ZipFile(io.BytesIO(wheel)) as z:
            found = [n for n in z.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)]
            data = bounded(z, found[0], 1_000_000) if len(found) == 1 else None
        headers = email.parser.HeaderParser().parsestr(data.decode("utf-8")) if data is not None else None
    except Exception:  # noqa: BLE001  (an odd member, compression or encoding: no licence, never a crashed batch)
        return None
    if headers is None:
        return None
    exprs = headers.get_all("License-Expression") or []
    # PEP 639: an expression replaces the License field and licence classifiers; METADATA with both is refused.
    classifiers = [str(c) for c in headers.get_all("Classifier") or []]
    if len(exprs) != 1 or headers.get_all("License") or any(c.startswith("License ::") for c in classifiers):
        return None
    expr = " ".join(str(exprs[0]).split())
    return expr if 0 < len(expr) <= 200 and osi_expression(expr) else None


def take_wheel(repo: str, wheel: dict) -> tuple[bytes | None, str | None]:
    """(the verified bytes, None) from build 1's run, else build 2's (both reported this sha256), or (None, why not)."""
    ids = [int(m.group(1)) for u in wheel.get("builds") or [] if isinstance(u, str) and (m := RUN_URL.fullmatch(u))]
    data = None
    for run_id in ids:
        archive = retry(lambda run_id=run_id: fetch_wheel(repo, run_id))
        if archive is not None and (data := unwrap_wheel(archive, wheel["wheel"], wheel["sha256"])):
            break
    if data is None:
        return None, "no build run kept a wheel with the verified sha256"
    tag = PUBLISH_TAGS.get(wheel["target"])
    if (why := wheel_tags_problem(data, tag)) is not None:
        return None, why
    plat = build_lab.TARGETS[wheel["target"]].get("plat")
    if plat is None:  # Windows: the CPU its binaries are for, and the DLLs they import (PE)
        if (wrong := foreign_binaries(data, tag.rsplit("-", 1)[-1])) is None:
            return None, "couldn't read its binaries' headers"
        if wrong:
            return None, f"has binaries built for another CPU: {', '.join(wrong[:5])}"
        if (missing := missing_dlls(data)) is None:
            return None, "couldn't read its extension modules' imports"
        if missing:
            return None, f"needs DLLs a user's machine may not have: {', '.join(missing[:5])}"
    elif plat not in LINUX_POLICIES:
        return None, f"no library policy for {plat}"
    else:  # Linux: the libraries and symbol versions its ELF files need
        if (missing := missing_libs(data, plat)) is None:
            return None, "couldn't read its ELF files' needs"
        if missing:
            return None, f"needs libraries or symbol versions a {plat} system may not have: {', '.join(missing[:5])}"
    return data, None


def cmd_stage(args: argparse.Namespace) -> int:
    """Stage verified wheels and write STAGED/staged.json with complete provenance.

    GitHub outputs contain only the staged-file SHA-256, staged count and test matrix,
    not the potentially large wheel/provenance records.
    """
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    optout = opted_out(Path(args.optout))
    out = Path(args.staged)
    out.mkdir(parents=True, exist_ok=True)
    staged, lines = [], []
    for wheel in manifest["wheels"]:
        if not tool_provenance_valid(wheel.get("tools")):
            lines.append(
                f"| {wheel['name']} | {wheel['version']} | {wheel['target']} | no verified toolchain |"
            )
            continue
        recipes = wheel.get("recipes")
        if (
            library_recipes.missing(wheel["name"], wheel["target"]) is not None
            or recipes != (library_recipes.records(wheel["name"], wheel["target"]) or None)
            or (recipes is not None and not recipe_records_valid(recipes))
        ):
            # Like the toolchain: before the wheel file is fetched. Also a pair that should have had a recipe.
            lines.append(
                f"| {wheel['name']} | {wheel['version']} | {wheel['target']} | no verified library recipe |"
            )
            continue
        info = pypi_info(wheel["name"], wheel["version"])
        reason = stage_reason(wheel, info, optout)
        data = None
        if reason is None:
            try:
                data, reason = take_wheel(args.repo, wheel)
            except Exception as e:  # noqa: BLE001  (one unreadable wheel costs only itself, not the week's batch)
                data, reason = None, f"couldn't read it ({type(e).__name__})"
        stated = licence(info) if info is not None else None
        if reason is None and stated is None and (stated := metadata_licence(data)) is None:
            reason = "neither PyPI nor its METADATA names an OSI-approved licence"
        if reason is None and recipes:
            reason = bundled_licence_problem(data, recipes) or grafted_library_problem(data)
        lines.append(f"| {wheel['name']} | {wheel['version']} | {wheel['target']} | {reason or 'staged'} |")
        if reason is not None:
            continue
        (out / wheel["wheel"]).write_bytes(data)
        staged.append({**{k: wheel[k] for k in ("name", "version", "target", "wheel", "sha256", "sdist",
                                                "sdist_sha256", "builds", "tools")},
                       "licence": stated, "requires_python": info.get("requires_python") or None,
                       "verified": manifest.get("date"), "batch": manifest.get("run")})  # fmt: skip
        if recipes:  # carried into data/gap-wheels.json, so the site can say what the wheel bundles
            staged[-1]["recipes"] = recipes
    staged_file = out / "staged.json"
    staged_file.write_text(json.dumps(staged, indent=1) + "\n", encoding="utf-8")
    staged_sha256 = sha256(staged_file)
    matrix = [install_test_job(t) for t in sorted({w["target"] for w in staged})]
    text = "\n".join([f"**{len(staged)} staged** of {len(manifest['wheels'])} verified", "",
                      "| package | version | target | |", "|---|---|---|---|", *lines]) + "\n"  # fmt: skip
    print(text)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(text)
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as f:
            # Only a digest and count cross the job-output boundary. The full
            # provenance stays in staged.json and is checked against this digest.
            f.write(f"staged_sha256={staged_sha256}\n")
            f.write(f"staged_count={len(staged)}\n")
            f.write(f"tests={json.dumps(matrix, separators=(',', ':'))}\n")
    return 0


def install_test_job(target: str) -> dict[str, str]:
    """The install-test matrix entry for a target. Every key is always there (empty when it doesn't apply), so the
    workflow's conditions compare strings: Windows runs on the runner with setup-python (`python`); Linux runs `docker
    run` of `image` with its `interpreter`, after fetching `python_url` when there is one (LINUX_TESTS)."""
    spec = build_lab.TARGETS[target]
    job = {"target": target, "runner": spec["runner"], "python": spec["python"], "slug": target.replace(" ", "-"),
           "image": "", "interpreter": "", "python_url": "", "python_sha256": ""}  # fmt: skip
    if "plat" in spec:
        job.update(LINUX_TESTS[target])
    return job


def library_checks(staged: dict) -> list[str]:
    """The import checks for a staged wheel built with library recipes (each recipe's fixed `import_check`, from this
    commit's allowlist); [] for any other wheel. ValueError if the staged record isn't what this commit's recipes
    give: the checks would be for another build."""
    recorded = staged.get("recipes")
    recipes = library_recipes.for_pair(staged["name"], staged["target"])
    if recorded != ([library_recipes.record(r) for r in recipes] or None):
        raise ValueError("the staged library recipes aren't this commit's")
    return [r["import_check"] for r in recipes]


def install_and_import(wheel: Path, target: str, work: Path, checks: list[str] | None = None) -> str | None:
    """Install the wheel into a fresh venv (dependencies as pip resolves them) and import it as the build lab does;
    on free-threaded Python the GIL must stay off. `checks` (library_checks) then run in that venv, each of which
    fails unless the wheel's bundled library is really in use. None if it works, else why not."""
    venv = work / "venv"
    shutil.rmtree(venv, ignore_errors=True)
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=INSTALL_TIMEOUT)
    py = str(venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1")
    try:
        i = subprocess.run([py, "-m", "pip", "install", "--no-cache-dir", str(wheel)], check=False, env=env,
                           capture_output=True, text=True, errors="replace", timeout=INSTALL_TIMEOUT)  # fmt: skip
    except subprocess.TimeoutExpired:
        return "install timed out"
    print((i.stdout + i.stderr)[-3000:])
    if i.returncode != 0:
        return "pip couldn't install it"
    modules, extension_modules, extensions = build_lab.wheel_modules(wheel)
    ours = work / "wheel-modules.json"
    ours.write_text(json.dumps(build_lab.wheel_names(wheel)), encoding="utf-8")
    answers = []
    for mode, names in (("top", modules), ("extensions", extension_modules)):
        if mode == "extensions" and not extensions:
            break
        check = build_lab._import_check(work, env, mode, ours, names, python=py)
        if check is None or check[0] != 0 or check[1] is None or not check[1]["loaded"]:
            return f"the {mode} import check failed"
        answers.append(check[1])
    if not answers:
        return "nothing to import"
    if build_lab.TARGETS[target]["python"].endswith("t") and any(a.get("gil_enabled") for a in answers):
        return "importing it turned the GIL back on"
    # Native code the package loads itself (ctypes), maybe only later: load each library now. Every one in a wheel
    # without extension modules; on Windows also in a wheel with a bundled .libs folder, since those DLLs' imports
    # rely on the package's DLL search path, which only loading proves.
    native = native_libraries(wheel) if not extensions or (os.name == "nt" and has_libs_folder(wheel)) else []
    if native:
        spec = work / "native.json"
        spec.write_text(json.dumps({"modules": modules, "native": native}), encoding="utf-8")
        try:
            r = subprocess.run([py, "-I", "-c", NATIVE_LOAD, str(spec)], check=False, env=env, capture_output=True,
                               text=True, errors="replace", timeout=INSTALL_TIMEOUT, cwd=work)  # fmt: skip
        except subprocess.TimeoutExpired:
            return "loading its native libraries timed out"
        print((r.stdout + r.stderr)[-3000:])
        if r.returncode != 0 or f"native-ok {len(native)}" not in r.stdout:
            return "its native libraries didn't load"
    for check in checks or []:
        try:
            r = subprocess.run([py, "-I", "-c", check], check=False, env=env, capture_output=True, text=True,
                               errors="replace", timeout=INSTALL_TIMEOUT, cwd=work)  # fmt: skip
        except subprocess.TimeoutExpired:
            return "its bundled library check timed out"
        print((r.stdout + r.stderr)[-3000:])
        if r.returncode != 0:
            return "its bundled library isn't in use"
    return None


# Imports the package (which sets up its DLL search path, as delvewheel's patch does), then loads each native library
# the wheel installed, from the venv's site-packages. Run in the install test's venv with -I.
NATIVE_LOAD = """
import ctypes, importlib, json, os, sys, sysconfig
spec = json.load(open(sys.argv[1], encoding="utf-8"))
for m in spec["modules"]:
    importlib.import_module(m)
base = sysconfig.get_paths()["platlib"]
for rel in spec["native"]:
    path = os.path.join(base, *rel.split("/"))
    if not os.path.exists(path):
        path = os.path.join(sysconfig.get_paths()["purelib"], *rel.split("/"))
    ctypes.CDLL(path)
print("native-ok", len(spec["native"]))
"""


def native_libraries(wheel: Path) -> list[str]:
    """The native libraries a wheel installs for this system (.dll on Windows, .so and .so.N elsewhere), by path in
    site-packages (build_lab.site_packages_paths), not extension modules: the files the package loads itself. Not in
    .dist-info or .data's other folders, nor in a top-level .libs folder: loading a bundled library first would let it
    answer for its mangled name whatever the search path, so those are loaded only as the others' dependencies."""
    with zipfile.ZipFile(wheel) as z:
        names = [n for n in build_lab.site_packages_paths(z.namelist()) if not n.endswith("/")]
    keep = []
    for n in names:
        if "/" in n and n.split("/", 1)[0].endswith(".libs"):
            continue
        if os.name == "nt":
            if n.lower().endswith(".dll"):
                keep.append(n)
        elif re.search(r"\.so(\.[0-9]+)*$", n) and not re.search(r"\.(cpython-[^/]+|abi3t?|cp3[0-9]+t?-[^/]+)\.so$", n):
            keep.append(n)
    return sorted(keep)


def has_libs_folder(wheel: Path) -> bool:
    """Whether the wheel installs a top-level .libs folder into site-packages (build_lab.site_packages_paths)."""
    with zipfile.ZipFile(wheel) as z:
        names = build_lab.site_packages_paths(z.namelist())
    return any("/" in n and n.split("/", 1)[0].endswith(".libs") for n in names)


def cmd_install_test(args: argparse.Namespace) -> int:
    if (why := build_lab.interpreter_problem(args.target)) is not None:
        print(f"gap_wheels: this Python isn't {args.target}'s: {why}", file=sys.stderr)
        return 3
    staged = json.loads((Path(args.staged) / "staged.json").read_text(encoding="utf-8"))
    tested = {}
    for w in staged:
        if w["target"] != args.target:
            continue
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            try:
                checks = library_checks(w)
                why = install_and_import(Path(args.staged) / w["wheel"], args.target, Path(tmp),
                                         **({"checks": checks} if checks else {}))  # fmt: skip
            except Exception as e:  # noqa: BLE001  (one bad wheel fails itself, not its target's whole test)
                why = f"the test itself failed ({type(e).__name__})"
        print(f"{w['wheel']}: {why or 'ok'}")
        tested[w["wheel"]] = why is None
    Path(args.out).write_text(json.dumps({"target": args.target, "tested": tested}) + "\n", encoding="utf-8")
    return 0


def read_tested(folder: Path) -> tuple[dict[str, dict[str, bool]], dict[str, set[str]]]:
    """By target: {wheel: passed} from the untrusted install-test artifacts. Each answer counts only for the target its
    artifact is named for (tested-<slug>/tested.json, the slug of a lab target), whatever target its JSON names, so a
    test job can't answer for another target by writing that target's name into its own artifact.

    With it, for the summary's wording only (nothing publishes because of it): by target whose artifact was read, the
    wheels it answered `false` for. A target missing from this second dict had no readable result at all."""
    slugs = {t.replace(" ", "-"): t for t in build_lab.TARGETS}
    out: dict[str, dict[str, bool]] = {}
    failed: dict[str, set[str]] = {}
    paths = sorted(folder.glob("tested-*/tested.json")) if folder.is_dir() else []
    for path in paths:
        target = slugs.get(path.parent.name.removeprefix("tested-"))
        if target is None or path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > MAX_TESTED_BYTES:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("tested"), dict):
            continue
        out[target] = {k: v for k, v in data["tested"].items() if isinstance(k, str) and v is True}
        failed[target] = {k for k, v in data["tested"].items() if isinstance(k, str) and v is False}
    return out, failed


NO_RESULT = "no install-test result"


def why_untested(name: str, target: str, failed: dict[str, set[str]]) -> str:
    """What to say about a staged wheel whose install test didn't pass. `name` and `target` are the staging job's;
    nothing from a tested.json is put into the text."""
    if target not in failed:
        return f"{NO_RESULT} for {target} (the test job didn't finish or its result couldn't be read)"
    if name in failed[target]:
        return "failed the install test on a fresh runner"
    return f"the install test for {target} gave no answer for it"


def select(staged: list[dict], tested: dict[str, dict[str, bool]], folder: Path,
           published: dict[str, dict], failed: dict[str, set[str]]) -> tuple[list[dict], list[str]]:  # fmt: skip
    """(entries to publish now, a line saying what happened to each staged wheel). `staged` is the staging job's own
    output, the only thing trusted here: a wheel publishes when its file in `folder` has that sha256 and its
    target's install test passed. A filename already published keeps its first bytes. `failed` (read_tested's second
    value) only picks the words for a wheel `tested` has no pass for."""
    new, lines = [], []
    for w in staged:
        name = w["wheel"]
        path = folder / name
        if not tool_provenance_valid(w.get("tools")):
            lines.append(f"{name}: no verified build-tool provenance; not published")
        elif "recipes" in w and not recipe_records_valid(w["recipes"]):
            lines.append(f"{name}: no verified library recipe; not published")
        elif not path.is_file() or path.is_symlink() or sha256(path) != w["sha256"]:
            lines.append(f"{name}: the staged file isn't the verified one; not published")
        elif not tested.get(w["target"], {}).get(name):
            lines.append(f"{name}: {why_untested(name, w['target'], failed)}; not published")
        elif name in published:
            same = published[name].get("sha256") == w["sha256"]
            lines.append(f"{name}: {'already published' if same else 'published before with other bytes; kept those'}")
        else:
            new.append(w)
            lines.append(f"{name}: published")
    return new, lines


MAX_STAGED_BYTES = 10_000_000


def trusted_staged(text: str) -> list[dict]:
    """The staging job's `staged` output, checked for shape (it's ours, but a bad one must stop, not half-publish)."""
    staged = json.loads(text)
    keys = {"name", "version", "target", "wheel", "sha256", "sdist", "sdist_sha256", "builds", "licence",
            "requires_python", "verified", "batch", "tools"}  # fmt: skip
    if not isinstance(staged, list) or not all(
        # `recipes` only on a wheel built with library recipes: one without is exactly what stage wrote before them.
        isinstance(w, dict) and set(w) in (keys, keys | {"recipes"})
        and isinstance(w["wheel"], str) and ASSET.fullmatch(w["wheel"])
        and isinstance(w["sha256"], str) and SHA256.fullmatch(w["sha256"]) and w["target"] in build_lab.TARGETS
        and tag_ok(w["wheel"], w["target"]) and tool_provenance_valid(w["tools"])
        and ("recipes" not in w or recipe_records_valid(w["recipes"]))
        for w in staged
    ):
        raise SystemExit("gap_wheels: --staged-json isn't what stage writes")
    return staged


def cmd_publish(args: argparse.Namespace) -> int:
    """Copy the wheels to publish into RELEASE_DIR and add them to the index file; list in RELEASE_DIR/withdraw.txt
    the published wheels of projects that have since opted out (removed from the index here)."""
    if getattr(args, "staged_file", None):
        path = Path(args.staged_file)
        if (not args.staged_sha256 or not SHA256.fullmatch(args.staged_sha256)
                or not path.is_file() or path.is_symlink()
                or path.stat().st_size > MAX_STAGED_BYTES
                or sha256(path) != args.staged_sha256):
            raise SystemExit("gap_wheels: staged file is missing, modified or not digest-verified")
        staged_text = path.read_text(encoding="utf-8")
    else:
        staged_text = args.staged_json
    staged = trusted_staged(staged_text)
    staged_dir = Path(args.files)
    index_path = Path(args.index)
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {"schema": INDEX_SCHEMA, "wheels": []}
    published = {w["wheel"]: w for w in index["wheels"]}
    tested, failed = read_tested(Path(args.tested))
    new, lines = select(staged, tested, staged_dir, published, failed)
    optout = opted_out(Path(args.optout))
    release = Path(args.release_dir)
    release.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now(dt.UTC).date().isoformat()
    for w in new:
        shutil.copyfile(staged_dir / w["wheel"], release / w["wheel"])
        index["wheels"].append({**w, "published": today, "publish_run": args.run})
    withdrawn = [w["wheel"] for w in index["wheels"] if build_lab.canonical(w["name"]) in optout]
    index["wheels"] = sorted((w for w in index["wheels"] if w["wheel"] not in withdrawn),
                             key=lambda w: (build_lab.canonical(w["name"]), w["wheel"]))  # fmt: skip
    (release / "withdraw.txt").write_text("".join(f"{n}\n" for n in withdrawn), encoding="utf-8")
    index_path.write_text(json.dumps(index, indent=1) + "\n", encoding="utf-8")
    lines += [f"{n}: withdrawn (the project opted out)" for n in withdrawn]
    text = f"**{len(new)} published**, {len(index['wheels'])} in the index\n\n" + "".join(f"- {ln}\n" for ln in lines)
    if (missing := sum(f": {NO_RESULT} for " in ln for ln in lines)) > 1:
        text = f"{missing} wheels have {NO_RESULT}: re-run the failed install-test jobs, then this job.\n\n" + text
    print(text)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("builds")
    p.add_argument("--max", type=int, default=100)
    p.add_argument("--only")
    p.add_argument("--rotate", action="store_true", help="start further on each week (for the scheduled batch)")
    p.add_argument("--github-output")
    p.add_argument("--lock-out", help="coordinator-only build-tool lock bundle; skip unpinned pairs")
    p.add_argument("--summary", help="append locked and skipped pairs to the GitHub step summary")
    p.set_defaults(func=cmd_plan)
    b = sub.add_parser("build")
    for flag in ("--name", "--version", "--target", "--runner", "--out"):
        b.add_argument(flag, required=True)
    b.add_argument("--attempt", type=int, required=True)
    b.add_argument("--wheel-dir", help="copy the built wheel here (the build run uploads it as `wheel`)")
    for flag in ("--lock-bundle", "--lock-sha256", "--pair-id"):
        b.add_argument(flag, required=True)
    b.set_defaults(func=cmd_build)
    m = sub.add_parser("merge")
    m.add_argument("results")
    m.add_argument("--plan", required=True)
    m.add_argument("--locks", required=True, help="trusted build-tool locks created before dispatch")
    m.add_argument("--out", required=True)
    m.add_argument("--summary")
    m.set_defaults(func=cmd_merge)
    d = sub.add_parser("dispatch")
    for flag in ("--plan", "--batch", "--repo", "--sha", "--since", "--locks"):
        d.add_argument(flag, required=True)
    d.add_argument("--parallel", type=int, default=20, help="most of this batch's builds running at once")
    d.set_defaults(func=cmd_dispatch)
    c = sub.add_parser("collect")
    for flag in ("--plan", "--batch", "--repo", "--sha", "--since", "--results"):
        c.add_argument(flag, required=True)
    c.add_argument("--wait", type=int, default=240, help="minutes to wait for the build runs")
    c.set_defaults(func=cmd_collect)
    st = sub.add_parser("stage")
    for flag in ("--manifest", "--staged", "--repo"):
        st.add_argument(flag, required=True)
    st.add_argument("--optout", default="data/gap-wheels-optout.txt")
    st.add_argument("--summary")
    st.add_argument("--github-output")
    st.set_defaults(func=cmd_stage)
    t = sub.add_parser("install-test")
    for flag in ("--staged", "--target", "--out"):
        t.add_argument(flag, required=True)
    t.set_defaults(func=cmd_install_test)
    pb = sub.add_parser("publish")
    for flag in ("--files", "--tested", "--index", "--release-dir"):
        pb.add_argument(flag, required=True)
    stage_input = pb.add_mutually_exclusive_group(required=True)
    stage_input.add_argument("--staged-json")
    stage_input.add_argument("--staged-file")
    pb.add_argument("--staged-sha256", help="SHA-256 of the trusted stage job's staged.json output")
    pb.add_argument("--optout", default="data/gap-wheels-optout.txt")
    pb.add_argument("--run", default="")
    pb.add_argument("--summary")
    pb.set_defaults(func=cmd_publish)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
