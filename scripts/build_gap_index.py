"""Publish the gap wheels index: a PEP 503 simple index and a page explaining it, from data/gap-wheels.json.

    python3 scripts/build_gap_index.py data/gap-wheels.json _site

Writes <site>/simple/index.html (every project), <site>/simple/<project>/index.html, <site>/index.html (what the index
is, how to use it with pip and uv, how the wheels are made and how a project opts out; each package's rows have an
anchor, #<project>, and say who asked for it when data/gap-wheel-requests.json names someone, and which library a
wheel built with a library recipe bundles) and
<site>/data/gap-wheels.json (the transparency record), <site>/data/status.json (the current installability
state of each recorded wheel) and <site>/feed.xml (an Atom feed of the wheels in the index, newest first). gap_wheels.py publish writes the raw JSON. Plain HTML, no JavaScript. The site is
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
import datetime
import html
import http.client
import json
import re
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urlsplit

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
# The public form for a wheel that doesn't install, import or run (.github/ISSUE_TEMPLATE/broken-wheel.yml), and
# GitHub's private report for one that looks unsafe (SECURITY.md).
BROKEN_WHEEL_URL = "https://github.com/wheelgap/wheels/issues/new?template=broken-wheel.yml"
ADVISORY_URL = "https://github.com/wheelgap/wheels/security/advisories/new"


def report_url(wheel: str) -> str:
    """The broken-wheel form with this file name filled in: GitHub fills an issue form's field from a query parameter
    named after the field's id (`wheel` in broken-wheel.yml), and the title from `title`."""
    name = quote(wheel, safe="")
    return f"{BROKEN_WHEEL_URL}&wheel={name}&title=Broken%20wheel%3A%20{name}"


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


LIBRARY = re.compile(r"[a-z0-9][a-z0-9+._-]{0,49}", re.ASCII)
LIBRARY_VERSION = re.compile(r"[0-9][0-9A-Za-z.+_-]{0,39}", re.ASCII)
SPDX = re.compile(r"[A-Za-z0-9.+() -]{1,100}", re.ASCII)


def bundled(w: dict) -> str:
    """ "bundles libyaml 0.2.5 (MIT)" for a wheel built with library recipes (its entry's `recipes`: a pinned system
    library linked into it statically, see the README), "" for any other. An entry whose `recipes` isn't the shape
    publish writes says nothing here rather than something wrong."""
    recipes = w.get("recipes")
    if not isinstance(recipes, list) or not 0 < len(recipes) <= 5:
        return ""
    parts = []
    for r in recipes:
        if not (
            isinstance(r, dict)
            and all(isinstance(r.get(k), str) for k in ("library", "version", "licence"))
            and LIBRARY.fullmatch(r["library"])
            and LIBRARY_VERSION.fullmatch(r["version"])
            and SPDX.fullmatch(r["licence"])
        ):
            return ""
        parts.append(f"{r['library']} {r['version']} ({r['licence']})")
    return "bundles " + ", ".join(parts)


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


def install_command(project: str, wheel: str) -> str | None:
    """The pip command that installs this wheel's release from the index, pinned to the version in its file name
    (never the entry's `version`, which is free text); None when the file name isn't this project's or its version
    isn't plain."""
    parsed = wheel_tags(wheel)
    if parsed is None or parsed[0] != project or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.]*", parsed[1], re.ASCII):
        return None
    return f"pip install --extra-index-url {INDEX_URL} {project}=={parsed[1]}"


FEED_URL = f"{BASE_URL}feed.xml"
FEED_MAX = 50
PUBLISHED = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
# What XML 1.0 can't carry, even as a character reference: most control characters, lone surrogates, U+FFFE and U+FFFF.
NOT_XML = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def published_day(w: dict) -> str | None:
    """An entry's `published` if it is a real date written YYYY-MM-DD (as gap_wheels.py publish writes it), or None."""
    day = w.get("published")
    if not isinstance(day, str) or not PUBLISHED.fullmatch(day):
        return None
    try:
        datetime.date.fromisoformat(day)
    except ValueError:
        return None
    return day


def xml_text(text: str) -> str:
    """Text for an XML element or a quoted attribute."""
    return html.escape(NOT_XML.sub("", text))


def feed(by_project: dict[str, list[dict]], status: dict[tuple[str, str], str]) -> str:
    """feed.xml: an Atom 1.0 feed (RFC 4287) of the wheels in the index (`in-index` or `yanked` in `status`), newest
    first by `published`, at most FEED_MAX. An entry without a valid `published` is left out. Every date comes from the
    data, none from the clock, so the same inputs give the same bytes."""
    x = xml_text
    listed: dict[str, tuple[str, str, dict]] = {}  # by file name, which is the entry's id: the first record of a file
    days = []
    for project, wheels in by_project.items():
        for w in wheels:
            day = published_day(w)
            if day is None:
                continue
            days.append(day)
            if status.get((project, w["wheel"])) in ("in-index", "yanked"):
                listed.setdefault(w["wheel"], (day, project, w))
    newest = sorted(listed.values(), key=lambda item: (item[0], item[1], item[2]["wheel"]), reverse=True)[:FEED_MAX]
    updated = newest[0][0] if newest else max(days, default="1970-01-01")
    entries = []
    for day, project, w in newest:
        version, target = w.get("version"), w.get("target")
        title = f"{w['name']} {version} for {target}" if isinstance(version, str) and isinstance(target, str) else w["wheel"]
        command = install_command(project, w["wheel"])
        summary = "Unofficial wheel, built by wheelgap from the PyPI sdist; not from the package's maintainers. "
        summary += (f"Install: {command} " if command else "") + f"sha256: {w['sha256']}"
        entries.append(
            "  <entry>\n"
            f"    <id>{x(RELEASE_URL + w['wheel'])}</id>\n"
            f"    <title>{x(title)}</title>\n"
            f'    <link href="{x(BASE_URL + "#" + project)}"/>\n'
            f"    <updated>{day}T00:00:00Z</updated>\n"
            f"    <summary>{x(summary)}</summary>\n"
            "  </entry>\n"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        f"  <id>{x(FEED_URL)}</id>\n"
        "  <title>wheelgap gap wheels</title>\n"
        "  <subtitle>Unofficial wheels PyPI doesn't have yet, built by wheelgap from the PyPI sdist; not from the "
        "packages' maintainers.</subtitle>\n"
        f"  <updated>{updated}T00:00:00Z</updated>\n"
        "  <author><name>wheelgap</name></author>\n"
        f'  <link rel="self" type="application/atom+xml" href="{x(FEED_URL)}"/>\n'
        f'  <link rel="alternate" type="text/html" href="{x(BASE_URL)}"/>\n' + "".join(entries) + "</feed>\n"
    )


def human_page(
    by_project: dict[str, list[dict]],
    gone: dict[tuple[str, str], str] | None = None,
    requests: dict[str, dict[str, str]] | None = None,
    yanked: dict[tuple[str, str], str] | None = None,
) -> str:
    """The page. `gone`: why a wheel isn't in the index, and `yanked`: PyPI's reason ("" when it gave none) for a
    wheel that is in the index but whose release PyPI has yanked, both by (project, wheel file name) as build() keys
    them."""
    gone = gone or {}
    requests = requests or {}
    yanked = yanked or {}
    e = html.escape
    rows = []
    for project, wheels in by_project.items():
        asked = requests.get(canonical(project))
        note = (
            f'<br><small>Built at the request of <a href="{e(asked["url"])}" rel="nofollow ugc">{e(asked["by"])}</a></small>' if asked else ""
        )
        for n, w in enumerate(wheels):
            builds = [u for u in w.get("builds") or [] if isinstance(u, str) and u.startswith("https://github.com/")]
            links = ", ".join(
                f'<a href="{e(u)}" aria-label="build {i} of {e(w["wheel"])}">build {i}</a>'
                for i, u in enumerate(builds, 1)
            )
            anchor = f' id="{e(project)}"' if n == 0 else ""
            command = install_command(project, w["wheel"])
            install = f"<code>{e(command)}</code>" if (project, w["wheel"]) not in gone and command is not None else ""
            library = f"<br><small>{e(text)}</small>" if (text := bundled(w)) else ""
            report = (
                f'<br><a href="{e(report_url(w["wheel"]))}" aria-label="report a problem with {e(w["wheel"])}">report</a>'
                if (project, w["wheel"]) not in gone
                else ""
            )
            key = (project, w["wheel"])
            if key in gone:
                state = gone[key]
            elif key in yanked:
                # Still served, and the Install cell's == pin is the one case where pip and uv take a yanked file.
                # The reason is the package owner's text: one line of printable characters, and not a page of it.
                why = " ".join("".join(c for c in yanked[key] if c.isprintable() or c.isspace()).split())
                if len(why) > 200:
                    why = why[:200].rstrip() + "…"
                state = "in the index, but PyPI has yanked this release" + (f": {why}" if why else "")
            else:
                state = "in the index"
            rows.append(
                f'          <tr{anchor}><td><a href="{e(TRACKER)}p/{e(project)}/">{e(w["name"])}</a>{note}</td>'
                f"<td>{e(str(w.get('version', '')))}</td><td>{e(str(w.get('target', '')))}</td>"
                f"<td>{e(str(w.get('verified') or ''))}</td><td>{links}</td>"
                f'<td><a href="{e(RELEASE_URL + w["wheel"])}" aria-label="wheel {e(w["wheel"])}">wheel</a>'
                f'<br><small title="{e(w["sha256"])}">{e(w["sha256"][:12])}</small>{library}{report}</td>'
                f"<td>{install}</td>"
                f"<td>{e(state)}</td></tr>"
            )
    count = sum(len(v) for v in by_project.values()) - len(gone)
    # A heading's id has an underscore so no package row's id (a canonical name: a-z, 0-9 and -) can clash with it.
    table = (
        '        <div class="matrix-scroll" tabindex="0" role="region" aria-labelledby="published_wheels"><table class="matrix">\n'
        '          <thead><tr><th scope="col">Package</th><th scope="col">Version</th>'
        '<th scope="col">Target</th><th scope="col">Verified</th><th scope="col">Builds</th>'
        '<th scope="col">File</th><th scope="col">Install</th><th scope="col">Status</th></tr></thead>\n'
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
  <link rel="alternate" type="application/atom+xml" href="feed.xml" title="New gap wheels">
</head>
<body>
  <header class="wrap">
    <p class="eyebrow"><a href="/tracker/">wheelgap tracker</a> · <a href="https://github.com/wheelgap/wheels">source and threat model</a></p>
    <h1>Gap wheels</h1>
    <p class="lede">Some popular Python packages don't have a wheel on PyPI yet for Windows on ARM, for free-threaded Python 3.14t, for Alpine Linux on ARM or for Python 3.15. When that happens pip tries to build them from source, which needs Rust or a C compiler and often fails. These are the missing wheels. {count} so far.</p>
  </header>
  <main class="wrap">
    <p>These wheels are unofficial. wheelgap builds each one from the sdist PyPI has for that release. The package's maintainers didn't build them and haven't reviewed them. If one doesn't work or looks wrong, <a href="#report_a_problem">report it here</a>, not to the package.</p>
    <section>
      <h2>How to use it</h2>
      <p>Let <a href="https://github.com/wheelgap/wheelgap">wheelgap</a> 0.14.0 or later check your requirements file or lockfile:</p>
      <pre><code>uvx wheelgap@latest --fix requirements.txt</code></pre>
      <p>For each gap, <a href="https://wheelgap.github.io/wheelgap/cli/#fix"><code>--fix</code></a> names an upgrade to an official release first, where a newer release has the wheel. Where none has, or if you can't upgrade, it says which gaps a wheel here fills and prints the lines to add for pip or uv.</p>
      <p>Or by hand. With pip, add this index next to PyPI:</p>
      <pre><code>pip install --extra-index-url {e(INDEX_URL)} PACKAGE</code></pre>
      <p>Where PyPI is pip's other index, pip still gets everything else from PyPI, and only takes a wheel from here when PyPI doesn't have one for your machine. pip treats every index alike, so do this only if the packages below come from PyPI: not if a private index (set in <code>pip.conf</code>, say) has packages of the same names.</p>
      <p>If your requirements file pins hashes (<code>--require-hashes</code>), add the wheel's hash to the package's existing requirement line, or pip won't use the wheel: <code>--hash=sha256:…</code>, with the wheel's full sha256, after the hashes the line has (inside its <code>\\</code>-continued block, if it has one), keeping them. On a line of its own, a second requirement for the same package, pip won't use it. The full sha256 is in <a href="data/status.json">status.json</a>, as the <code>sha256</code> of the row whose <code>wheel</code> is the file name. The table below shows only its first 12 characters.</p>
      <p>With uv, add the index to <code>pyproject.toml</code> and point only the packages you need at it. uv only applies <code>[tool.uv.sources]</code> to your direct dependencies, so if a package comes in through another one (tokenizers through transformers, say), add it to <code>dependencies</code> as well:</p>
      <pre><code>[[tool.uv.index]]
name = "wheelgap"
url = "{e(INDEX_URL)}"
explicit = true

[tool.uv.sources]
tokenizers = {{ index = "wheelgap" }}</code></pre>
      <p>Then run <code>uv lock</code>. If <code>pyproject.toml</code> already has a <code>[tool.uv.sources]</code> table, add the package's line to it instead of a second one.</p>
      <p>uv takes all of a package's files from one index, so each package's page here lists every file PyPI has for it too, with PyPI's own links and hashes, plus our wheel. Everything else still comes from PyPI. <code>uv pip install --extra-index-url {e(INDEX_URL)} PACKAGE</code> works too.</p>
      <p>Other lockfiles (Poetry, PDM and the like) pin each file, so add <code>{e(INDEX_URL)}</code> as an index (for these packages only, if the tool allows it) where that lockfile is made, then lock again. An index that isn't limited to these packages has the same risk as with pip: don't add it if a private index has packages of the same names.</p>
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
        <li>A few packages need a C library the build image doesn't have (PyYAML needs libyaml). For those the library is built first, from one fixed source file whose hash is checked, and linked into the wheel, with its licence text included. The table says which library and version such a wheel bundles, and the install test checks it's really the one in use.</li>
        <li>Installed and imported on a fresh machine before it goes up. On free-threaded Python the GIL has to stay off. This checks that it works, it's not a security check.</li>
        <li>pip checks each file's hash when it downloads it. You can also check where a file came from with <code>gh attestation verify FILE.whl -R wheelgap/wheels</code>.</li>
        <li>Every wheel, its source hash and both build runs are listed in <a href="data/gap-wheels.json">gap-wheels.json</a>; the machine-readable <a href="data/status.json">status.json</a> shows which ones are currently in the index, yanked or withdrawn. New wheels are announced in an <a href="feed.xml">Atom feed</a>.</li>
      </ul>
      <p>One thing to know: a wheel here is only as safe as the source it was built from. Installing the package the normal way runs the same code. Only packages with an OSI-approved licence (or the PSF licence Python uses) go up. Some Windows wheels also carry Microsoft's Visual C++ runtime DLLs (in a <code>.libs</code> folder), redistributed under Visual Studio's terms, as many wheels on PyPI do.</p>
      <p>The platforms: Windows on ARM for Python 3.14 and 3.14t, Windows x64 for Python 3.15, Linux x86_64 (glibc 2.28 or newer: <code>manylinux_2_28</code>) for Python 3.14t and 3.15, and Alpine Linux on ARM (<code>musllinux_1_2</code>) for Python 3.14. Every wheel goes up only after a wheelgap maintainer approves a publish run that includes it; the table below shows which are here.</p>
    </section>
    <section>
      <h2 id="report_a_problem">Problems with a wheel</h2>
      <p>These wheels come from wheelgap, not from the package's maintainers, so a problem with one belongs here, not in the package's issue tracker.</p>
      <ul>
        <li>If a wheel doesn't install, doesn't import or fails when you use it, fill in the <a href="{e(BROKEN_WHEEL_URL)}">broken-wheel form</a>. The "report" link in a wheel's row below opens the form with its file name filled in.</li>
        <li>If a wheel looks unsafe or tampered with, <a href="{e(ADVISORY_URL)}">report it privately</a>, never in a public issue. A wheel reported this way is taken down first and investigated after: it's removed from the index and the release while we look.</li>
      </ul>
    </section>
    <section>
      <h2>If it's your package</h2>
      <p>These are meant as a stopgap until your release ships its own wheel. Once it does, ours drops out of the index within the hour, and the table below says so. If you'd rather we didn't host any wheels for your project, <a href="{e(OPT_OUT_URL)}">open an opt-out issue</a> and they come down, and later batches skip it. Each row below links to both build runs if you want to see the build that worked.</p>
    </section>
    <section>
      <h2 id="published_wheels">Published wheels</h2>
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
    # Keyed by (project, wheel file name): two projects can record the same file, and one project's state must
    # not overwrite the other's (see #23).
    gone: dict[tuple[str, str], str] = {}
    status: dict[tuple[str, str], str] = {}
    yanked: dict[tuple[str, str], str] = {}
    for project in by_project:
        try:
            files = [pypi_link(f) for f in fetch(project)]
        except PyPIError as exc:
            if not omit_failing:
                print(f"gap wheels: PyPI's list can't be used, nothing written: {exc}", file=sys.stderr)
                return 1
            print(f"::warning::gap wheels: {project} left out of the index, PyPI's list can't be used: {exc}")
            for w in by_project[project]:
                gone[(project, w["wheel"])] = "left out for now: PyPI couldn't be read"
                status[(project, w["wheel"])] = "left-out-pypi-unreadable"
            continue
        listed = {name: link for name, link, _ in files}
        names_yanks = [(name, reason) for name, _, reason in files]
        kept = []
        for w in by_project[project]:
            mine = wheel_tags(w["wheel"])
            on_pypi, reason = release_yank(mine[1], project, names_yanks) if mine else (False, None)
            if not on_pypi:
                gone[(project, w["wheel"])] = "withdrawn: the release isn't on PyPI"
                status[(project, w["wheel"])] = "withdrawn-release-gone"
            elif withdrawn(w["wheel"], set(listed)):
                gone[(project, w["wheel"])] = "withdrawn: PyPI has its own now"
                status[(project, w["wheel"])] = "withdrawn-pypi-has-wheel"
            else:
                kept.append(w)
                status[(project, w["wheel"])] = "yanked" if reason is not None else "in-index"
                if reason is not None:
                    yanked[(project, w["wheel"])] = reason
        project_yanked = {wheel: why for (p, wheel), why in yanked.items() if p == project}
        ours_links = dict(zip((w["wheel"] for w in kept), project_links(kept, project_yanked), strict=True))
        pages[project] = [link for _, link in sorted({**listed, **ours_links}.items())]
    simple = site / "simple"
    simple.mkdir(parents=True, exist_ok=True)
    root_links = [f'<a href="{p}/">{p}</a>' for p in pages]
    (simple / "index.html").write_text(simple_page("Simple index", root_links), encoding="utf-8")
    for project, links in pages.items():
        (simple / project).mkdir(exist_ok=True)
        (simple / project / "index.html").write_text(simple_page(f"Links for {project}", links), encoding="utf-8")
    requests = read_requests(index_path.parent / "gap-wheel-requests.json")
    (site / "index.html").write_text(human_page(by_project, gone, requests, yanked), encoding="utf-8")
    (site / "data").mkdir(exist_ok=True)
    (site / "data" / "gap-wheels.json").write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    # Each status is set where build() decides it, never parsed back out of `gone`'s text. `usable` doesn't check
    # `version` or `target`, so anything but a string is null rather than a KeyError that would stop a takedown.
    wheel_status = {
        "schema": 1,
        "index": INDEX_URL,
        "wheels": sorted(
            (
                {
                    "project": project,
                    "version": w["version"] if isinstance(w.get("version"), str) else None,
                    "target": w["target"] if isinstance(w.get("target"), str) else None,
                    "wheel": w["wheel"],
                    "sha256": w["sha256"],
                    "url": RELEASE_URL + w["wheel"],
                    "status": status[(project, w["wheel"])],
                    "yank_reason": yanked.get((project, w["wheel"])),
                }
                for project, records in by_project.items()
                for w in records
            ),
            key=lambda row: (row["project"], row["wheel"]),
        ),
    }
    (site / "data" / "status.json").write_text(json.dumps(wheel_status, indent=1) + "\n", encoding="utf-8")
    (site / "feed.xml").write_text(feed(by_project, status), encoding="utf-8")
    listed_count = sum(1 for p in pages for w in by_project[p] if (p, w["wheel"]) not in gone)
    print(f"gap wheels: {listed_count} wheels of {len(pages)} projects listed, {len(gone)} withdrawn")
    for (project, name), why in sorted(gone.items()):
        print(f"  {why}: {project}: {name}")
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
