"""Publish the gap wheels index: a PEP 503 simple index and a page explaining it, from data/gap-wheels.json.

    python3 scripts/build_gap_index.py data/gap-wheels.json _site

Writes <site>/simple/index.html (every project), <site>/simple/<project>/index.html, <site>/index.html (what the index
is, how to use it with pip and uv, how the wheels are made and how a project opts out; each package's rows have an
anchor, #<project>, and say who asked for it when data/gap-wheel-requests.json names someone) and
<site>/data/gap-wheels.json (the transparency record). gap_wheels.py publish writes the JSON. Plain HTML, no JavaScript. The site is
wheelgap.github.io/wheels/; it shares wheelgap.github.io with the tracker, whose stylesheet it uses.

Each project page lists every file PyPI lists for that project (read from PyPI's JSON simple API, linked to
files.pythonhosted.org with PyPI's sha256, requires-python, yanked and core-metadata) plus our gap wheels (linked to
this repo's `gap-wheels` release with their sha256). The full list is what makes the index safe for uv: uv takes all of
a package's files from the first index that has it, so a page with only our wheel would hide PyPI's other releases.
A gap wheel drops out once PyPI has a wheel of the same release that installs where ours does, or once the release is
gone from PyPI; it is marked yanked when PyPI has yanked the release. If PyPI can't be read for any project, or answers
in an unexpected shape, nothing is written and it exits 1, so the deploy stops and the previous index stays up rather
than a page with files missing. With --omit-failing (for deploys a maintainer starts, such as a takedown) such a project
is left out of the index instead, with a warning.
"""

from __future__ import annotations

import argparse
import html
import http.client
import json
import re
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

BASE_URL = "https://wheelgap.github.io/wheels/"
TRACKER = "https://wheelgap.github.io/tracker/"
CSP = "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; base-uri 'none'; form-action 'none'"
INDEX_URL = f"{BASE_URL}simple/"
RELEASE_URL = "https://github.com/wheelgap/wheels/releases/download/gap-wheels/"
ASSET = re.compile(r"[A-Za-z0-9._-]{1,200}\.whl", re.ASCII)
SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
PROJECT = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.ASCII)
PYPI_SIMPLE = "https://pypi.org/simple/"
PYPI_FILES = "https://files.pythonhosted.org/packages/"
SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"
# A file name PyPI lists: printable ASCII with nothing that could break out of a URL path or an attribute.
PYPI_FILENAME = re.compile(r"[A-Za-z0-9._+!-]{1,255}", re.ASCII)
OPT_OUT_URL = (
    "https://github.com/wheelgap/wheels/issues/new?title=Opt-out%3A%20PROJECT"
    "&body=Please%20stop%20publishing%20gap%20wheels%20for%20PROJECT."
)


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def usable(w: object) -> bool:
    """An entry the index can link safely: everything that goes into a URL or a page has the expected shape."""
    return (
        isinstance(w, dict)
        and isinstance(w.get("name"), str)
        and PROJECT.fullmatch(canonical(w["name"])) is not None
        and isinstance(w.get("wheel"), str)
        and ASSET.fullmatch(w["wheel"]) is not None
        and isinstance(w.get("sha256"), str)
        and SHA256.fullmatch(w["sha256"]) is not None
    )


def projects(wheels: list) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for w in wheels:
        if usable(w):
            out.setdefault(canonical(w["name"]), []).append(w)
    return {k: sorted(v, key=lambda w: w["wheel"]) for k, v in sorted(out.items())}


class PyPIError(Exception):
    """PyPI couldn't be read for a project, or answered in a shape this script doesn't trust."""


def fetch_pypi(project: str) -> list[dict]:
    """The files PyPI's JSON simple API (PEP 691) lists for a project. Retries twice; raises PyPIError."""
    url = f"{PYPI_SIMPLE}{project}/"
    req = urllib.request.Request(url, headers={"Accept": SIMPLE_JSON, "User-Agent": "wheelgap-wheels-index"})
    problem = ""
    for attempt in range(3):
        if attempt:
            time.sleep(5 * attempt)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.geturl() != url or r.headers.get_content_type() != SIMPLE_JSON:
                    raise PyPIError(f"{url}: answered from {r.geturl()} as {r.headers.get_content_type()}")
                data = json.load(r)
            break
        except (OSError, ValueError, http.client.HTTPException) as exc:
            problem = f"{url}: {exc!r}"
    else:
        raise PyPIError(problem)
    meta = data.get("meta") if isinstance(data, dict) else None
    if not isinstance(meta, dict) or not str(meta.get("api-version", "")).startswith("1."):
        raise PyPIError(f"{url}: not a version 1 simple API answer")
    files = data.get("files")
    if not isinstance(files, list):
        raise PyPIError(f"{url}: no files list")
    return files


def pypi_link(f: object) -> tuple[str, str, str | None]:
    """(file name, anchor, yank reason or None) for one file of PyPI's list, carrying what PyPI says about it. Raises PyPIError on any
    field that isn't the shape PEP 691 and PEP 714 give, rather than leave a file out (which would change what an
    installer sees)."""
    if not isinstance(f, dict):
        raise PyPIError(f"a file entry that isn't an object: {f!r:.80}")
    name, url, hashes = f.get("filename"), f.get("url"), f.get("hashes")
    if not isinstance(name, str) or not PYPI_FILENAME.fullmatch(name):
        raise PyPIError(f"unexpected file name {name!r:.80}")
    if not isinstance(url, str) or not url.startswith(PYPI_FILES) or not urlsplit(url).path.endswith("/" + name):
        raise PyPIError(f"{name}: unexpected URL {url!r:.120}")
    if urlsplit(url).query or urlsplit(url).fragment or not PYPI_FILENAME.fullmatch(url[len(PYPI_FILES) :].replace("/", "")):
        raise PyPIError(f"{name}: unexpected URL {url!r:.120}")
    sha = hashes.get("sha256") if isinstance(hashes, dict) else None
    if not isinstance(sha, str) or not SHA256.fullmatch(sha):
        raise PyPIError(f"{name}: no sha256")
    e = html.escape
    attrs = ""
    rp = f.get("requires-python")
    if rp is not None:
        if not isinstance(rp, str):
            raise PyPIError(f"{name}: requires-python isn't a string")
        if rp.strip():
            attrs += f' data-requires-python="{e(rp)}"'
    meta = f.get("core-metadata", f.get("data-dist-info-metadata", False))
    if isinstance(meta, dict):
        msha = meta.get("sha256")
        if not isinstance(msha, str) or not SHA256.fullmatch(msha):
            raise PyPIError(f"{name}: core-metadata without a sha256")
        attrs += f' data-dist-info-metadata="sha256={msha}" data-core-metadata="sha256={msha}"'
    elif meta is True:
        attrs += ' data-dist-info-metadata="true" data-core-metadata="true"'
    elif meta is not False and meta is not None:
        raise PyPIError(f"{name}: unexpected core-metadata {meta!r:.80}")
    yanked = f.get("yanked", False)
    reason: str | None = None
    if isinstance(yanked, str):
        reason = yanked
    elif yanked is True:
        reason = ""
    elif yanked is not False:
        raise PyPIError(f"{name}: unexpected yanked {yanked!r:.80}")
    if reason is not None:
        attrs += f' data-yanked="{e(reason)}"'
    return name, f'<a href="{e(url)}#sha256={sha}"{attrs}>{e(name)}</a>', reason


SDIST = (".tar.gz", ".zip")


def file_version(filename: str, project: str) -> str | None:
    """The version in a wheel or sdist file name of `project` (lowercased, as written), or None."""
    if parsed := wheel_tags(filename):
        return parsed[1] if parsed[0] == project else None
    for suffix in SDIST:
        if filename.endswith(suffix):
            name, _, version = filename[: -len(suffix)].rpartition("-")
            return version.lower() if name and canonical(name) == project else None
    return None


def release_yank(version: str, project: str, files: list[tuple[str, str | None]]) -> tuple[bool, str | None]:
    """(whether PyPI lists the release at all, its yank reason if every one of its files is yanked)."""
    reasons = [reason for name, reason in files if file_version(name, project) == version]
    if not reasons:
        return False, None
    if all(r is not None for r in reasons):
        return True, next((r for r in reasons if r), "")
    return True, None


def wheel_tags(filename: str) -> tuple[str, str, set[tuple[str, str, str]]] | None:
    """(canonical name, version, tags) of a wheel file name, or None for anything else."""
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) not in (5, 6):
        return None
    py, abi, plat = parts[-3:]
    tags = {(p, a, pl) for p in py.split(".") for a in abi.split(".") for pl in plat.split(".")}
    return canonical(parts[0]), parts[1].lower(), tags


CPYTHON = re.compile(r"cp(\d)(\d+)", re.ASCII)
LINUX = re.compile(r"(manylinux|musllinux)_(\d{1,3})_(\d{1,3})_([a-z0-9_]{1,20})", re.ASCII)
LEGACY_MANYLINUX = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}  # PEP 600's aliases


def linux_platform(plat: str) -> tuple[str, int, int, str] | None:
    """(manylinux or musllinux, the libc's major and minor version, architecture) of a PEP 600 or PEP 656 platform tag,
    or of a legacy manylinux one (manylinux2014_x86_64 is manylinux_2_17_x86_64); None for anything else."""
    if m := LINUX.fullmatch(plat):
        return m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
    for alias, (major, minor) in LEGACY_MANYLINUX.items():
        if plat.startswith(alias + "_") and re.fullmatch(r"[a-z0-9_]{1,20}", arch := plat[len(alias) + 1 :]):
            return "manylinux", major, minor, arch
    return None


def platform_covers(theirs: str, ours: str) -> bool:
    """Whether every system that takes platform tag `ours` takes `theirs`: the same tag, or on Linux the same libc and
    architecture with an older or equal libc version (a glibc 2.28 system installs manylinux_2_17 wheels too)."""
    if theirs == ours:
        return True
    t, o = linux_platform(theirs), linux_platform(ours)
    return t is not None and o is not None and (t[0], t[3]) == (o[0], o[3]) and t[1:3] <= o[1:3]


def covers(theirs: tuple[str, str, str], ours: tuple[str, str, str]) -> bool:
    """Whether a wheel tagged `theirs` installs everywhere one tagged `ours` (a CPython wheel, cpXY-cpXY[t]-PLAT) does.
    Only cases that are certain count: the same Python and ABI on a platform that covers ours (the same tag, or an
    older manylinux or musllinux of the same architecture), py3-none on such a platform, abi3 from an older or equal
    CPython for a non-free-threaded ABI, and pure Python wheels. Anything else keeps our wheel."""
    if theirs == ours:
        return True
    py, abi, plat = ours
    m = CPYTHON.fullmatch(py)
    if m is None or abi not in (py, py + "t"):
        return False
    major, minor = int(m.group(1)), int(m.group(2))
    tpy, tabi, tplat = theirs
    if tabi == "none" and tplat == "any":
        return tpy in (f"py{major}", f"py{major}{minor}", py)
    if not platform_covers(tplat, plat):
        return False
    if (tpy, tabi) == (py, abi):
        return True
    if tabi == "none":  # native code without a Python ABI (py3-none-win_arm64): every CPython of that major takes it
        return tpy in (f"py{major}", f"py{major}{minor}", py)
    if tabi == "abi3" and abi == py:
        tm = CPYTHON.fullmatch(tpy)
        return tm is not None and int(tm.group(1)) == major and int(tm.group(2)) <= minor
    return False


def withdrawn(ours: str, pypi_names: set[str]) -> bool:
    """Whether PyPI now has our wheel's file name, or a wheel of the same release that installs wherever ours does."""
    if ours in pypi_names:
        return True
    mine = wheel_tags(ours)
    if mine is None:
        return False
    name, version, tags = mine
    for other in pypi_names:
        theirs = wheel_tags(other)
        if theirs is None or theirs[:2] != (name, version):
            continue
        if all(any(covers(t, o) for t in theirs[2]) for o in tags):
            return True
    return False


def simple_page(title: str, links: list[str]) -> str:
    body = "\n".join(f"    {link}<br>" for link in links)
    return f"""<!doctype html>
<html>
  <head>
    <meta name="pypi:repository-version" content="1.0">
    <title>{html.escape(title)}</title>
  </head>
  <body>
{body}
  </body>
</html>
"""


def project_links(wheels: list[dict], yanked: dict[str, str] | None = None) -> list[str]:
    e = html.escape
    links = []
    for w in wheels:
        rp = w.get("requires_python")
        attr = f' data-requires-python="{e(rp)}"' if isinstance(rp, str) and rp.strip() else ""
        if yanked and w["wheel"] in yanked:
            attr += f' data-yanked="{e(yanked[w["wheel"]])}"'
        links.append(f'<a href="{e(RELEASE_URL + w["wheel"])}#sha256={w["sha256"]}"{attr}>{e(w["wheel"])}</a>')
    return links


# Who asked for a package's gap wheels, shown on its rows: data/gap-wheel-requests.json, {"project": {"by": "u/name",
# "url": "https://www.reddit.com/user/name/"}}. A maintainer adds an entry when building on someone's public request.
# Only a short handle and a link to a Reddit or GitHub profile are shown; anything else is left out.
REQUEST_BY = re.compile(r"(?:u/|@)?[A-Za-z0-9_-]{1,39}", re.ASCII)
REQUEST_URL = re.compile(r"https://(?:www\.reddit\.com/user|github\.com)/[A-Za-z0-9_-]{1,39}/?", re.ASCII)


def read_requests(path: Path) -> dict[str, dict[str, str]]:
    """The well-formed entries of data/gap-wheel-requests.json, by canonical project name ({} if there's no file)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    if isinstance(raw, dict):
        for project, r in raw.items():
            if (
                isinstance(project, str)
                and isinstance(r, dict)
                and isinstance(r.get("by"), str)
                and REQUEST_BY.fullmatch(r["by"])
                and isinstance(r.get("url"), str)
                and REQUEST_URL.fullmatch(r["url"])
            ):
                out[canonical(project)] = {"by": r["by"], "url": r["url"]}
    return out


def human_page(
    by_project: dict[str, list[dict]],
    gone: dict[str, str] | None = None,
    requests: dict[str, dict[str, str]] | None = None,
) -> str:
    gone = gone or {}
    requests = requests or {}
    e = html.escape
    rows = []
    for project, wheels in by_project.items():
        asked = requests.get(canonical(project))
        note = (
            f'<br><small>Built at the request of <a href="{e(asked["url"])}">{e(asked["by"])}</a></small>' if asked else ""
        )
        for n, w in enumerate(wheels):
            builds = [u for u in w.get("builds") or [] if isinstance(u, str) and u.startswith("https://github.com/")]
            links = ", ".join(f'<a href="{e(u)}">build {n}</a>' for n, u in enumerate(builds, 1))
            anchor = f' id="{e(project)}"' if n == 0 else ""
            rows.append(
                f'          <tr{anchor}><td><a href="{e(TRACKER)}p/{e(project)}/">{e(w["name"])}</a>{note}</td>'
                f"<td>{e(str(w.get('version', '')))}</td><td>{e(str(w.get('target', '')))}</td>"
                f"<td>{e(str(w.get('verified') or ''))}</td><td>{links}</td>"
                f'<td><a href="{e(RELEASE_URL + w["wheel"])}">wheel</a></td>'
                f"<td>{e(gone.get(w['wheel'], 'in the index'))}</td></tr>"
            )
    count = sum(len(v) for v in by_project.values()) - len(gone)
    table = (
        '        <div class="matrix-scroll"><table class="matrix">\n'
        "          <thead><tr><th>Package</th><th>Version</th><th>Target</th><th>Verified</th><th>Builds</th>"
        "<th>File</th><th>Status</th></tr></thead>\n"
        "          <tbody>\n" + "\n".join(rows) + "\n          </tbody>\n"
        "        </table></div>\n"
        if rows
        else '        <p class="note">No wheels published yet.</p>\n'
    )
    summary = "Wheels PyPI doesn't have yet, for Windows and Linux on new Pythons (3.14, 3.14t, 3.15). Each one is built twice and only goes up if both builds match."
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="{CSP}">
  <title>Gap wheels · wheelgap tracker</title>
  <meta name="description" content="{e(summary)}">
  <link rel="canonical" href="{e(BASE_URL)}">
  <link rel="stylesheet" href="/tracker/style.css">
  <link rel="icon" href="/tracker/favicon.svg" type="image/svg+xml">
</head>
<body>
  <header class="wrap">
    <p class="eyebrow"><a href="/tracker/">wheelgap tracker</a> · <a href="https://github.com/wheelgap/wheels">source and threat model</a></p>
    <h1>Gap wheels</h1>
    <p class="lede">Some popular Python packages don't have a wheel on PyPI yet for Windows on ARM, for free-threaded Python 3.14t, for Alpine Linux on ARM or for Python 3.15. When that happens pip tries to build them from source, which needs Rust or a C compiler and often fails. These are the missing wheels. {count} so far.</p>
  </header>
  <main class="wrap">
    <section>
      <h2>How to use it</h2>
      <p>With pip, add this index next to PyPI:</p>
      <pre><code>pip install --extra-index-url {e(INDEX_URL)} PACKAGE</code></pre>
      <p>pip still gets everything else from PyPI. It only takes a wheel from here when PyPI doesn't have one for your machine.</p>
      <p>With uv, add the index to <code>pyproject.toml</code> and point only the packages you need at it. uv only applies <code>[tool.uv.sources]</code> to your direct dependencies, so if a package comes in through another one (tokenizers through transformers, say), add it to <code>dependencies</code> as well:</p>
      <pre><code>[[tool.uv.index]]
name = "wheelgap"
url = "{e(INDEX_URL)}"
explicit = true

[tool.uv.sources]
tokenizers = {{ index = "wheelgap" }}</code></pre>
      <p>uv takes all of a package's files from one index, so each package's page here lists every file PyPI has for it too, with PyPI's own links and hashes, plus our wheel. Everything else still comes from PyPI. <code>uv pip install --extra-index-url {e(INDEX_URL)} PACKAGE</code> works too.</p>
      <p>The PyPI part of the list is refreshed every hour, so with uv a release uploaded in the last hour may not show up yet for these packages. If PyPI can't be read, the index isn't updated at all, rather than going up with files missing. The pages are plain HTML, which has no upload times, so uv's <code>exclude-newer</code> finds no files for these packages here.</p>
      <p>Each wheel is for one release. If a newer release comes out without a wheel, pip or uv will try to build that one instead. Add <code>--prefer-binary</code> (pip) to keep using the wheel here. When the release ships its own wheel for the same platform, ours drops out of the index.</p>
    </section>
    <section>
      <h2>How they're made</h2>
      <ul>
        <li>Built from the source file PyPI has for that release. Its hash is checked against PyPI's. Build tools come from PyPI too.</li>
        <li>Built twice, in two separate GitHub Actions runs. It only goes up if the two builds come out byte for byte the same.</li>
        <li>Tagged for the one Python and platform it was built and tested on, nothing wider. It can't rely on a DLL your machine might not have.</li>
        <li>On Linux, built in a pinned manylinux_2_28 or Alpine image and repaired there with a pinned auditwheel, which bundles the libraries it needs. It only goes up if every other library it links against is on that platform's standard list (and, for glibc, nothing newer than glibc 2.28), and it's install-tested in a minimal container, not on a build machine.</li>
        <li>Installed and imported on a fresh machine before it goes up. On free-threaded Python the GIL has to stay off. This checks that it works, it's not a security check.</li>
        <li>pip checks each file's hash when it downloads it. You can also check where a file came from with <code>gh attestation verify FILE.whl -R wheelgap/wheels</code>.</li>
        <li>Every wheel, its source hash and both of its build runs are listed in <a href="data/gap-wheels.json">gap-wheels.json</a>.</li>
      </ul>
      <p>One thing to know: a wheel here is only as safe as the source it was built from. Installing the package the normal way runs the same code. Only packages with an OSI-approved licence (or the PSF licence Python uses) go up.</p>
      <p>The platforms: Windows on ARM for Python 3.14 and 3.14t, Windows x64 for Python 3.15, Linux x86_64 (glibc 2.28 or newer: <code>manylinux_2_28</code>) for Python 3.14t and 3.15, and Alpine Linux on ARM (<code>musllinux_1_2</code>) for Python 3.14. Linux wheels go up only once a maintainer approves a publish run that includes them; the table below shows which are here.</p>
    </section>
    <section>
      <h2>If it's your package</h2>
      <p>These are meant as a stopgap until your release ships its own wheel. Once it does, ours drops out of the index within the hour, and the table below says so. If you'd rather we didn't host any wheels for your project, <a href="{e(OPT_OUT_URL)}">open an opt-out issue</a> and they come down, and later batches skip it. Each row below links to both build runs if you want to see the build that worked.</p>
    </section>
    <section>
      <h2>Published wheels</h2>
{table}    </section>
  </main>
</body>
</html>
"""


def build(
    index_path: Path, site: Path, fetch: Callable[[str], list] = fetch_pypi, *, omit_failing: bool = False
) -> int:
    """Write the site. A project PyPI can't be read for stops the build (nothing written, exit 1), or with
    `omit_failing` is left out of the index (a warning, exit 0): for a deploy a maintainer started, such as a
    takedown, which mustn't wait on PyPI."""
    data = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {"wheels": []}
    wheels = data.get("wheels") if isinstance(data, dict) and isinstance(data.get("wheels"), list) else []
    by_project = projects(wheels)
    pages: dict[str, list[str]] = {}
    gone: dict[str, str] = {}
    for project in by_project:
        try:
            files = [pypi_link(f) for f in fetch(project)]
        except PyPIError as exc:
            if not omit_failing:
                print(f"gap wheels: PyPI's list can't be used, nothing written: {exc}", file=sys.stderr)
                return 1
            print(f"::warning::gap wheels: {project} left out of the index, PyPI's list can't be used: {exc}")
            for w in by_project[project]:
                gone[w["wheel"]] = "left out for now: PyPI couldn't be read"
            continue
        listed = {name: link for name, link, _ in files}
        names_yanks = [(name, reason) for name, _, reason in files]
        yanked: dict[str, str] = {}
        kept = []
        for w in by_project[project]:
            mine = wheel_tags(w["wheel"])
            on_pypi, reason = release_yank(mine[1], project, names_yanks) if mine else (False, None)
            if not on_pypi:
                gone[w["wheel"]] = "withdrawn: the release isn't on PyPI"
            elif withdrawn(w["wheel"], set(listed)):
                gone[w["wheel"]] = "withdrawn: PyPI has its own now"
            else:
                kept.append(w)
                if reason is not None:
                    yanked[w["wheel"]] = reason
        ours_links = dict(zip((w["wheel"] for w in kept), project_links(kept, yanked), strict=True))
        pages[project] = [link for _, link in sorted({**listed, **ours_links}.items())]
    simple = site / "simple"
    simple.mkdir(parents=True, exist_ok=True)
    root_links = [f'<a href="{p}/">{p}</a>' for p in pages]
    (simple / "index.html").write_text(simple_page("Simple index", root_links), encoding="utf-8")
    for project, links in pages.items():
        (simple / project).mkdir(exist_ok=True)
        (simple / project / "index.html").write_text(simple_page(f"Links for {project}", links), encoding="utf-8")
    requests = read_requests(index_path.parent / "gap-wheel-requests.json")
    (site / "index.html").write_text(human_page(by_project, gone, requests), encoding="utf-8")
    (site / "data").mkdir(exist_ok=True)
    (site / "data" / "gap-wheels.json").write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    listed_count = sum(1 for p in pages for w in by_project[p] if w["wheel"] not in gone)
    print(f"gap wheels: {listed_count} wheels of {len(pages)} projects listed, {len(gone)} withdrawn")
    for name, why in sorted(gone.items()):
        print(f"  {why}: {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("index", type=Path)
    parser.add_argument("site", type=Path)
    parser.add_argument("--omit-failing", action="store_true", help="leave out projects PyPI can't be read for")
    args = parser.parse_args(argv)
    return build(args.index, args.site, omit_failing=args.omit_failing)

if __name__ == "__main__":
    sys.exit(main())
