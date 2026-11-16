"""Publish the gap wheels index: a PEP 503 simple index and a page explaining it, from data/gap-wheels.json.

    python3 scripts/build_gap_index.py data/gap-wheels.json _site

Writes <site>/simple/index.html (every project), <site>/simple/<project>/index.html, <site>/index.html (what the index
is, how to use it with pip and uv, how the wheels are made and how a project opts out) and <site>/data/gap-wheels.json
(the transparency record). gap_wheels.py publish writes the JSON. Plain HTML, no JavaScript. The site is
wheelgap.github.io/wheels/; it shares wheelgap.github.io with the tracker, whose stylesheet it uses.

Each project page lists every file PyPI lists for that project (read from PyPI's JSON simple API, linked to
files.pythonhosted.org with PyPI's sha256, requires-python, yanked and core-metadata) plus our gap wheels (linked to
this repo's `gap-wheels` release with their sha256). The full list is what makes the index safe for uv: uv takes all of
a package's files from the first index that has it, so a page with only our wheel would hide PyPI's other releases.
A gap wheel drops out once PyPI has a wheel of the same release that installs where ours does. If PyPI can't be read
for any project, or answers in an unexpected shape, nothing is written and it exits 1, so the deploy stops and the
previous index stays up rather than a page with files missing.
"""

from __future__ import annotations

import html
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
        except (OSError, ValueError) as exc:
            problem = f"{url}: {exc}"
    else:
        raise PyPIError(problem)
    meta = data.get("meta") if isinstance(data, dict) else None
    if not isinstance(meta, dict) or not str(meta.get("api-version", "")).startswith("1."):
        raise PyPIError(f"{url}: not a version 1 simple API answer")
    files = data.get("files")
    if not isinstance(files, list):
        raise PyPIError(f"{url}: no files list")
    return files


def pypi_link(f: object) -> tuple[str, str]:
    """(file name, anchor) for one file of PyPI's list, carrying what PyPI says about it. Raises PyPIError on any
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
    if isinstance(yanked, str):
        attrs += f' data-yanked="{e(yanked)}"'
    elif yanked is True:
        attrs += ' data-yanked=""'
    elif yanked is not False:
        raise PyPIError(f"{name}: unexpected yanked {yanked!r:.80}")
    return name, f'<a href="{e(url)}#sha256={sha}"{attrs}>{e(name)}</a>'


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


def covers(theirs: tuple[str, str, str], ours: tuple[str, str, str]) -> bool:
    """Whether a wheel tagged `theirs` installs everywhere one tagged `ours` (a CPython wheel, cpXY-cpXY[t]-PLAT) does.
    Only cases that are certain count: the same tag, abi3 from an older or equal CPython for a non-free-threaded ABI,
    and pure Python wheels. Anything else keeps our wheel."""
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
    if tplat != plat:
        return False
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


def project_links(wheels: list[dict]) -> list[str]:
    e = html.escape
    links = []
    for w in wheels:
        rp = w.get("requires_python")
        attr = f' data-requires-python="{e(rp)}"' if isinstance(rp, str) and rp.strip() else ""
        links.append(f'<a href="{e(RELEASE_URL + w["wheel"])}#sha256={w["sha256"]}"{attr}>{e(w["wheel"])}</a>')
    return links


def human_page(by_project: dict[str, list[dict]], gone: set[str] | frozenset[str] = frozenset()) -> str:
    e = html.escape
    rows = []
    for project, wheels in by_project.items():
        for w in wheels:
            builds = [u for u in w.get("builds") or [] if isinstance(u, str) and u.startswith("https://github.com/")]
            links = ", ".join(f'<a href="{e(u)}">build {n}</a>' for n, u in enumerate(builds, 1))
            rows.append(
                f'          <tr><td><a href="{e(TRACKER)}p/{e(project)}/">{e(w["name"])}</a></td>'
                f"<td>{e(str(w.get('version', '')))}</td><td>{e(str(w.get('target', '')))}</td>"
                f"<td>{e(str(w.get('verified') or ''))}</td><td>{links}</td>"
                f'<td><a href="{e(RELEASE_URL + w["wheel"])}">wheel</a></td>'
                f"<td>{'withdrawn: PyPI has its own now' if w['wheel'] in gone else 'in the index'}</td></tr>"
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
    summary = "Windows on ARM wheels that PyPI doesn't have yet, for Python 3.14. Each one is built twice and only goes up if both builds match."
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
    <p class="lede">Some popular Python packages don't have a wheel on PyPI for Windows on ARM yet, mostly on Python 3.14. When that happens pip tries to build them from source, which needs Rust or a C compiler and often fails. These are the missing wheels. {count} so far.</p>
  </header>
  <main class="wrap">
    <section>
      <h2>How to use it</h2>
      <p>With pip, add this index next to PyPI:</p>
      <pre><code>pip install --extra-index-url {e(INDEX_URL)} PACKAGE</code></pre>
      <p>pip still gets everything else from PyPI. It only takes a wheel from here when PyPI doesn't have one for your machine.</p>
      <p>With uv, add the index to <code>pyproject.toml</code> and point only the packages you need at it:</p>
      <pre><code>[[tool.uv.index]]
name = "wheelgap"
url = "{e(INDEX_URL)}"
explicit = true

[tool.uv.sources]
tokenizers = {{ index = "wheelgap" }}</code></pre>
      <p>uv takes all of a package's files from one index, so each package's page here lists every file PyPI has for it too, with PyPI's own links and hashes, plus our wheel. Everything else still comes from PyPI. <code>uv pip install --extra-index-url {e(INDEX_URL)} PACKAGE</code> works the same way.</p>
      <p>The PyPI part of the list is refreshed every hour, so a release uploaded in the last hour may not show up yet for these packages. If PyPI can't be read, the index isn't updated at all, rather than going up with files missing.</p>
      <p>Each wheel is for one release. If a newer release comes out without a wheel, pip or uv will try to build that one instead. Add <code>--prefer-binary</code> (pip) to keep using the wheel here. When the release ships its own wheel for the same platform, ours drops out of the index.</p>
    </section>
    <section>
      <h2>How they're made</h2>
      <ul>
        <li>Built from the source file PyPI has for that release. Its hash is checked against PyPI's. Build tools come from PyPI too.</li>
        <li>Built twice, in two separate GitHub Actions runs. It only goes up if the two builds come out byte for byte the same.</li>
        <li>Tagged for the one Python and platform it was built and tested on, nothing wider. It can't rely on a DLL your machine might not have.</li>
        <li>Installed and imported on a fresh machine before it goes up. On free-threaded Python the GIL has to stay off. This checks that it works, it's not a security check.</li>
        <li>pip checks each file's hash when it downloads it. You can also check where a file came from with <code>gh attestation verify FILE.whl -R wheelgap/wheels</code>.</li>
        <li>Every wheel, its source hash and both of its build runs are listed in <a href="data/gap-wheels.json">gap-wheels.json</a>.</li>
      </ul>
      <p>One thing to know: a wheel here is only as safe as the source it was built from. Installing the package the normal way runs the same code. Only packages with an OSI-approved licence (or the PSF licence Python uses) go up. Windows only for now, Linux needs more work first.</p>
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


def build(index_path: Path, site: Path, fetch: Callable[[str], list] = fetch_pypi) -> int:
    data = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {"wheels": []}
    wheels = data.get("wheels") if isinstance(data, dict) and isinstance(data.get("wheels"), list) else []
    by_project = projects(wheels)
    pages: dict[str, list[str]] = {}
    gone: set[str] = set()
    try:
        for project in by_project:
            listed = dict(pypi_link(f) for f in fetch(project))
            ours = {w["wheel"]: w for w in by_project[project]}
            gone |= {name for name in ours if withdrawn(name, set(listed))}
            kept = [w for name, w in ours.items() if name not in gone]
            ours_links = dict(zip((w["wheel"] for w in kept), project_links(kept), strict=True))
            pages[project] = [link for _, link in sorted({**listed, **ours_links}.items())]
    except PyPIError as exc:
        print(f"gap wheels: PyPI's list can't be used, nothing written: {exc}", file=sys.stderr)
        return 1
    simple = site / "simple"
    simple.mkdir(parents=True, exist_ok=True)
    root_links = [f'<a href="{p}/">{p}</a>' for p in by_project]
    (simple / "index.html").write_text(simple_page("Simple index", root_links), encoding="utf-8")
    for project, links in pages.items():
        (simple / project).mkdir(exist_ok=True)
        (simple / project / "index.html").write_text(simple_page(f"Links for {project}", links), encoding="utf-8")
    (site / "index.html").write_text(human_page(by_project, gone), encoding="utf-8")
    (site / "data").mkdir(exist_ok=True)
    (site / "data" / "gap-wheels.json").write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    listed_count = sum(len(v) for v in by_project.values()) - len(gone)
    print(f"gap wheels: {listed_count} wheels of {len(by_project)} projects listed, {len(gone)} withdrawn")
    for name in sorted(gone):
        print(f"  withdrawn, PyPI has its own: {name}")
    return 0

if __name__ == "__main__":
    sys.exit(build(Path(sys.argv[1]), Path(sys.argv[2])))
