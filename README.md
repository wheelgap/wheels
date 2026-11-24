# wheelgap/wheels

Wheels PyPI doesn't have yet, for popular packages on platforms where pip would otherwise have to compile them. The
index is at **https://wheelgap.github.io/wheels/simple/** and the list of wheels, with how to use them, is at
https://wheelgap.github.io/wheels/. (The older address, https://wheelgap.github.io/tracker/wheels/simple/, keeps
working.)

Let [wheelgap](https://github.com/wheelgap/wheelgap) 0.14.0 or later check your requirements file or lockfile:

```
uvx wheelgap --fix requirements.txt
```

For each gap, [`--fix`](https://wheelgap.github.io/wheelgap/cli/#fix) names an upgrade to an official release first,
where a newer release has the wheel. Where none has, or if you can't upgrade, it says which gaps a wheel here fills and
prints the lines to add for pip or uv.

Or by hand. With pip, add this index next to PyPI:

```
pip install --extra-index-url https://wheelgap.github.io/wheels/simple/ PACKAGE
```

pip treats every index alike, so do this only if the packages you install from here come from PyPI: not if a private
index (set in `pip.conf`, say) has packages of the same names.

If your requirements file pins hashes (`--require-hashes`), add the wheel's hash to the package's existing requirement
line, or pip won't use the wheel: `--hash=sha256:…`, with the wheel's full sha256, after the hashes the line has
(inside its `\`-continued block, if it has one), keeping them. On a line of its own, a second requirement for the same
package, pip won't use it. The full sha256 is in
[`data/status.json`](https://wheelgap.github.io/wheels/data/status.json), as the `sha256` of the row whose `wheel` is
the file name.

uv works too. Pin the packages you need to the index in `pyproject.toml`. uv only applies `[tool.uv.sources]` to direct
dependencies, so a package that comes in through another one (tokenizers through transformers) must also be listed
in `dependencies`:

```toml
[[tool.uv.index]]
name = "wheelgap"
url = "https://wheelgap.github.io/wheels/simple/"
explicit = true

[tool.uv.sources]
tokenizers = { index = "wheelgap" }
```

Then run `uv lock`. If `pyproject.toml` already has a `[tool.uv.sources]` table, add the package's line to it instead
of a second one.

Other lockfiles (Poetry, PDM and the like) pin each file, so add `https://wheelgap.github.io/wheels/simple/` as an
index (for these packages only, if the tool allows it) where that lockfile is made, then lock again. An index that
isn't limited to these packages has the same risk as with pip: don't add it if a private index has packages of the
same names.

uv takes all of a package's files from one index, so each project's page here lists every file PyPI has for it as well
(PyPI's own links and hashes, refreshed hourly), plus our wheel. A lockfile made this way has the same files and hashes
for that package as one made from PyPI, plus ours (without PyPI's upload times, which HTML pages don't carry, so
uv's `exclude-newer` finds nothing for these packages here). Our wheel is marked yanked when PyPI yanks its release,
and leaves the index when the release ships its own wheel for that platform or is gone from PyPI.

The pipeline publishes wheels for these targets, each with exactly one tag:

| Platform | Python | Tag |
|---|---|---|
| Windows on ARM | 3.14, 3.14t | `win_arm64` |
| Windows x64 | 3.15 | `win_amd64` |
| Linux x86_64, glibc 2.28 or newer | 3.14t, 3.15 | `manylinux_2_28_x86_64` |
| Alpine Linux (musl) on ARM | 3.14 | `musllinux_1_2_aarch64` |

The wheels published so far are listed at https://wheelgap.github.io/wheels/. Linux wheels
are built and repaired with a pinned auditwheel in the pinned `manylinux_2_28` or Alpine image, and a Linux wheel
goes up only if every library it links against is bundled in it, where the loader finds it, or on that platform's
standard list, as auditwheel's policy gives it. Every wheel is published only after a maintainer approves its
publish run ([tracker#124](https://github.com/wheelgap/tracker/issues/124)).

This repo holds only the pipeline that builds and publishes them, kept apart from
[wheelgap](https://github.com/wheelgap/wheelgap) and [the tracker](https://github.com/wheelgap/tracker), which take
lots of outside contributions. Changes here are made by the maintainers, and every publish is approved by one.

## How a wheel gets here

1. **Pick:** the tracker's build lab records which releases build cleanly from their sdist where PyPI has no wheel
   (`data/builds.json` there). The weekly batch (`.github/workflows/gap-wheels.yml`) plans pairs from that list: a
   build with a compiled extension module, or one whose wheel carries native code without one (tagged
   `py3-none-<platform>`, such as LightGBM's DLL, which it loads with `ctypes`). A package whose extension needs a
   system library the build image doesn't have (PyYAML needs libyaml) is planned only where it has a
   [library recipe](#library-recipes); elsewhere the plan skips it and says why. The lab's runner may have had the
   library, so its clean build there doesn't count.
2. **Build twice:** before dispatch, the trusted coordinator reads the verified PyPI sdist's static
   `pyproject.toml` requirements without running package code and resolves wheels-only build dependencies, including
   an exact pip version. It records their names, versions, source URLs and SHA-256 hashes once per pair. Both builds
   install only those checked wheels into isolated environments and run with `--no-build-isolation --no-index`.
   Unsupported or unresolvable build requirements cause the pair to be skipped. Each pair is then built in two
   separate workflow runs (`gap-wheel-build.yml`), from the sdist PyPI publishes (its sha256 is checked against
   PyPI's), unpacked to a fixed path with a fixed `SOURCE_DATE_EPOCH`. On
   Linux the wheel is repaired with a pinned `auditwheel` first. On Windows, if its binaries import VC++ runtime DLLs
   a user's machine may lack (`msvcp140.dll`, `vcomp140.dll`…), a pinned `delvewheel` bundles those, and only those
   (by name), into `<dist>.libs/` under names carrying their hash. A `py3-none-<platform>` wheel whose own tag is the
   target's platform is first retagged with a pinned `wheel` to the target's one exact tag, so it installs only on the
   Python it was built and tested on. All of this happens before hashing, in both builds, and both tools are pinned by
   version and sha256. For a pair with a library recipe, each build first builds the library from its pinned source
   archive, links it into the extension statically and adds its licence text to the wheel, also before hashing.
3. **Verify:** a trusted job compares the two results, the coordinator's immutable build-tool lock and the recorded
   Python patch version, compiler/linker versions and runner image. A pair counts only when the toolchains match,
   both wheels are byte-identical and pip installs the wheel without compiling anything. A pair with a library
   recipe counts only when both builds also record exactly the recipe the plan gave it.
4. **Stage:** the trusted job takes the wheel file from a build run only if its sha256 is the verified one, and only
   for the one exact tag the target allows, in its filename and in its own `.dist-info/WHEEL` file, and on Windows only
   if every binary's PE header is for the target's CPU. The wheel can't need a DLL a user's machine may lack (on Windows, read
   from each `.pyd`/`.dll`/`.exe`'s imports; only the VC++ runtime counts as bundled from `<dist>.libs/`, only under a
   hashed name, which no other copy on the system can answer to, and only for a `.pyd`/`.dll`'s normal imports, not
   an `.exe`'s or a delay-load's) or, on Linux, link against a library that is neither bundled in the wheel
   (where its `$ORIGIN` RUNPATH or RPATH points) nor on the `manylinux_2_28` or `musllinux_1_2` list in auditwheel's
   policy, or need a glibc symbol version newer than 2.28 (read from each ELF file's dynamic section and version
   needs). Libraries a package loads by name at run time (`dlopen`) aren't checked. The release must name an
   OSI-approved licence (or the PSF licence) on PyPI, or, when PyPI's metadata says nothing about a licence at
   all, in a PEP 639 `License-Expression` in the wheel's own METADATA (which the sdist's build wrote). A wheel built
   with a library recipe must record this commit's recipe for its pair and carry the library's licence text, with
   the sha256 the recipe pins. Staging only reads the files, never runs them.
5. **Install test:** each staged wheel is installed and imported on a fresh runner of its platform. A wheel without
   an extension module, or on Windows one with a bundled `<dist>.libs/`, must also load every native library it ships
   outside `<dist>.libs/` (with `ctypes`, after importing the package), which proves the bundled DLLs are found. Linux wheels are
   tested in a minimal container of their libc, pinned by digest, with the target's exact CPython (`python:3.14-alpine`,
   `python:3.15-slim-trixie`, and `debian:trixie-slim` with a pinned, sha256-checked python-build-standalone 3.14t),
   not on GitHub's runner image, which has many libraries a user's system doesn't. A wheel built with a library
   recipe must also pass the recipe's own check there, which fails unless the library is the one in use (for
   PyYAML: `yaml.__with_libyaml__` is true, libyaml reports the pinned version, and `CSafeLoader` loads a document).
6. **Publish, after a maintainer approves:** the publish job waits on the `publish` environment's required reviewer.
   It then attests the files (`gh attestation verify FILE.whl -R wheelgap/wheels`), uploads them to the `gap-wheels`
   release, records them in `data/gap-wheels.json` and deploys the index.

**Machine-readable status.** [`data/status.json`](https://wheelgap.github.io/wheels/data/status.json) is rebuilt
with the index on every deploy and gives each wheel recorded in `data/gap-wheels.json` its current state, as
`{"schema": 1, "index": ..., "wheels": [...]}` sorted by project and file name. Each entry has the canonical
`project`, `version`, `target`, `wheel`, `sha256`, the release download `url`, `status` and `yank_reason`. `status` is
`in-index`; `yanked` (still in the index, but PyPI has yanked the whole release, so pip and uv skip it unless it's
pinned with `==`); `withdrawn-pypi-has-wheel` (PyPI now has a wheel that installs in its place);
`withdrawn-release-gone` (PyPI no longer lists the release); or `left-out-pypi-unreadable` (left out of this deploy
because PyPI couldn't be read, back at the next good build). `yank_reason` is PyPI's reason for a yanked wheel (`""`
when it gave none) and `null` otherwise. A wheel that was taken down or opted out has no entry.
[`feed.xml`](https://wheelgap.github.io/wheels/feed.xml), built with it, is an Atom feed of the wheels in the index
(`in-index` or `yanked`), newest first by publish date, at most 50.

**Bundled Microsoft DLLs.** Some Windows wheels carry Microsoft Visual C++ runtime files (`msvcp140.dll`,
`vcomp140.dll` and the like) in `<dist>.libs/`. They're Microsoft's, not the package's, redistributed under the
"Distributable Code" terms of Visual Studio, as many wheels on PyPI do (numpy's Windows wheels, for one). The
package's own licence, which staging checks, doesn't cover them.

### Library recipes

Some extensions need a C library that the pinned build image doesn't have. PyYAML is one: without libyaml's
headers its build quietly skips the extension and makes a wheel with no compiled code, which auditwheel refuses,
rightly. Installing the distro's package in the image isn't the answer, because `dnf install` and `apk add` aren't
pinned and what they fetch changes under the same image digest.

A recipe is an entry in [`scripts/library_recipes.py`](scripts/library_recipes.py), reviewed like any other
change here. It says: for this package, on these targets, build this library at this version from this exact
source archive (URL and sha256), with these fixed steps, as a static library, and show it to the package's build
with these environment variables. There is one so far:

| Package | Library | Targets | Source (sha256) | Licence |
|---|---|---|---|---|
| PyYAML | libyaml 0.2.6 | 3.14t and 3.15 on Linux x86_64 (glibc) | [yaml-0.2.6.tar.gz](https://github.com/yaml/libyaml/releases/download/0.2.6/yaml-0.2.6.tar.gz) (`43b393a117bd4fe39812fddc94e3d654359a3ae05653dcf0110a1122d1747e61`) | MIT |

What happens with it, and what is checked:

- **In the build**, before the package's own build and in the same image and run: the archive is downloaded and
  its sha256 checked before anything is unpacked. A mismatch stops the build. It's unpacked to a fixed path and
  built with the recipe's steps, which are argument lists in this repo (no shell, nothing from the package or from
  a workflow input), with the same fixed `SOURCE_DATE_EPOCH` as everything else. The steps must leave a static
  library and no shared one, so the extension links the library in: the wheel needs no new library at run time and
  auditwheel has nothing to bundle. For PyYAML the build also sets `PYYAML_FORCE_LIBYAML=1`, which turns a missing
  libyaml into a build failure where PyYAML would otherwise fall back to pure Python. The package's build still
  has no index to install from.
- **The licence.** A recipe names the library's SPDX licence, which must pass the same OSI check as a package's,
  and the sha256 of the licence text in the archive. The build copies that text into the wheel at
  `<dist>.dist-info/licenses/bundled/<library>-<version>/LICENSE`, lists it in `RECORD` and, where the wheel's
  metadata is version 2.4 or later (PEP 639), names it in a `License-File` line. Staging refuses a wheel that
  doesn't carry it with that sha256.
- **What is recorded.** Each build's result names the library, version, source URL and sha256, licence, and the
  sha256 of the whole recipe definition (steps, flags, environment and check included). The trusted job takes it
  only if it is exactly what the plan gave that pair, in both builds, and a result that names a recipe for a pair
  that has none is rejected. The record goes into the manifest and the wheel's entry in `data/gap-wheels.json`
  (`recipes`), and the [wheels page](https://wheelgap.github.io/wheels/) says "bundles libyaml 0.2.6 (MIT)".
- **The install test** runs the recipe's check, as above, in a container with no compiler and no libyaml.

A package known to need a library is never built without a recipe: PyYAML on Alpine or Windows, and psycopg2,
mysqlclient and h5py anywhere, are skipped by the plan with "no library recipe for this target".

## Checking a wheel yourself

To see whether the index serves a wheel now, find the row for its file name in
[`data/status.json`](https://wheelgap.github.io/wheels/data/status.json): it does when the row's `status` is
`in-index` or `yanked`. A yanked wheel is still served, but pip and uv only take it with an exact `==` pin.
[`data/gap-wheels.json`](data/gap-wheels.json) is the full record, with each wheel's `sha256`, `sdist_sha256` and
`builds`. It doesn't say whether a wheel is current: an entry stays there after its wheel drops out of the index
because PyPI shipped its own or the release is gone from PyPI. The examples below print the status first, then
check the file's hash against the record. They use
`lightgbm-4.7.0-cp314-cp314-win_arm64.whl` and `lz4-4.4.5-cp314-cp314t-manylinux_2_28_x86_64.whl`;
substitute another filename for your platform. The attestation check needs the
[GitHub CLI](https://cli.github.com/), signed in (`gh auth login`), and the Linux example also needs `jq`.

**Windows (PowerShell):**

```powershell
$wheel = "lightgbm-4.7.0-cp314-cp314-win_arm64.whl"
$status = @()
$status = @((Invoke-RestMethod "https://wheelgap.github.io/wheels/data/status.json").wheels | Where-Object { $_.wheel -eq $wheel } | ForEach-Object { $_.status })
if ($status.Count -eq 1 -and $status[0] -in "in-index", "yanked") { "status: $status" } else { Write-Warning "the index doesn't serve $wheel now (status: $(if ($status) { $status -join ', ' } else { 'no entry in status.json' }))" }
Invoke-WebRequest "https://github.com/wheelgap/wheels/releases/download/gap-wheels/$wheel" -OutFile $wheel -UseBasicParsing
$index = Invoke-RestMethod "https://raw.githubusercontent.com/wheelgap/wheels/main/data/gap-wheels.json"
$expected = @($index.wheels | Where-Object { $_.wheel -eq $wheel })
$actual = (Get-FileHash $wheel -Algorithm SHA256).Hash.ToLowerInvariant()
if ($expected.Count -eq 1 -and $actual -eq $expected[0].sha256) { "sha256 OK"; gh attestation verify $wheel -R wheelgap/wheels } else { Write-Error "sha256 mismatch, or the wheel isn't in data/gap-wheels.json" }
```

**Linux (shell):**

```sh
wheel="lz4-4.4.5-cp314-cp314t-manylinux_2_28_x86_64.whl"
state="$(curl -fsSL https://wheelgap.github.io/wheels/data/status.json \
  | jq -r --arg wheel "$wheel" '[.wheels[] | select(.wheel == $wheel) | .status] | if length == 0 then "no entry in status.json" else join(", ") end')"
case "$state" in
  in-index | yanked) echo "status: $state" ;;
  "") echo "status.json could not be read" >&2 ;;
  *) echo "the index doesn't serve $wheel now (status: $state)" >&2 ;;
esac
curl -fL "https://github.com/wheelgap/wheels/releases/download/gap-wheels/$wheel" -o "$wheel"
expected="$(curl -fsSL https://raw.githubusercontent.com/wheelgap/wheels/main/data/gap-wheels.json \
  | jq -r --arg wheel "$wheel" '[.wheels[] | select(.wheel == $wheel) | .sha256] | if length == 1 then .[0] else empty end')"
actual="$(sha256sum "$wheel" | cut -d ' ' -f1)"
if [ -n "$expected" ] && [ "$actual" = "$expected" ]; then
  echo "sha256 OK" && gh attestation verify "$wheel" -R wheelgap/wheels
else
  echo "sha256 mismatch, or the wheel isn't in data/gap-wheels.json" >&2
fi
```

The hash comparison shows you downloaded the exact file recorded in this repo. pip also checks the
`#sha256=` fragment in the index's link on every download. The attestation shows that a workflow in this repo
attested that exact file, and names the workflow and commit: `gap-wheels.yml` for wheels published here. Wheels
whose `publish_run` is in wheelgap/tracker were published before the pipeline moved to this repo; their
attestation here names `migrate.yml`, and the original publish attestation is in the tracker
(`gh attestation verify FILE.whl -R wheelgap/tracker`). Neither shows that the sdist or its dependencies are
safe; see the [threat model](#threat-model).

You can check the source separately. The entry's `sdist_sha256` should match the sha256 PyPI lists for its
`sdist` file:

```sh
curl -fsSL https://pypi.org/pypi/lz4/4.4.5/json | jq -r '.urls[] | select(.filename == "lz4-4.4.5.tar.gz") | .digests.sha256'
```

The entry's two `builds` links are the separate workflow runs that built the pair; their logs show both builds
(GitHub keeps run logs for 90 days). The logs and the attestation help audit where a wheel came from, not
whether its source is safe.

## Threat model

**What the pipeline defends against**

- **A build that substitutes another package's files, or forges another build's result.** Each build runs in its
  own workflow run, so its artifact token can only write its own artifacts. The coordinator takes results only from
  the run with that build's exact title, started from `main` at its own commit, and refuses duplicates.
- **A build whose output or toolchain differs between independent runs.** The coordinator resolves a single
  hash-verified lock for the PEP 517 build backend, all resolved dependencies and pip. Both builds must use
  that lock. If the Python patch version, compiler/linker versions or recorded runner image differ, the pair is
  rejected even when its wheel bytes match. Missing provenance also prevents publication.
- **A build that picks up a library other than the reviewed one by accident.** A [library recipe](#library-recipes)
  pins the library's source archive by sha256 and its build steps by the recipe's own sha256. Both builds must
  record the recipe the coordinator planned for the pair, and no other pair may record one. A missing library can't
  turn into a wheel without it: where the package has a switch for that the recipe sets it, and the install test
  checks the library is in use. These are our build script's own steps and records, made inside the build run: they
  show what the pipeline did at this commit, not what the package's build code then did with the library. A package
  whose build ignores or replaces it is a malicious sdist (below); the control that remains is that the two builds
  must still be byte-identical.
- **A build that tampers with what gets published.** The publish job never runs package code. It trusts only the
  staging job's own output for names and hashes, and re-hashes every file it uploads.
- **Wrong-platform installs.** A wheel carries exactly one tag, the Python and platform it was built and tested on.
  A Linux wheel's tag is auditwheel's repaired one, never `linux_*`, and staging checks again, from the wheel's own
  ELF files, that the libraries it links against and the glibc symbol versions it needs are within that tag's policy.
- **A PyPI answer that would change what an installer sees.** The PyPI part of each page is copied from PyPI's JSON
  simple API: only `https://files.pythonhosted.org/packages/` links with PyPI's sha256, and every field must have the
  shape the spec gives. If PyPI can't be read for any project, or anything is off, the hourly rebuild deploys nothing
  and the previous index stays up, so it is never published with files missing. A push or a manual deploy (a
  publish, takedown or opt-out) goes out anyway and leaves such a project out of the index until the next good
  hourly run, so removals never wait on PyPI. While it's out, pip falls back to PyPI and uv reports it not found.

**What it does not defend against**

- **A malicious sdist.** A wheel built from it is as malicious as the sdist. Installing the package from PyPI the
  normal way runs the same code. These wheels are only as trustworthy as the sdist PyPI serves.
- **A compromise of a build toolchain.** Build backends and their resolved dependencies are now pinned by wheel
  SHA-256 in a coordinator-generated lock, as is the build environment's pip version. Linux builds use a digest-pinned
  image and a version-pinned Rust toolchain (rustup-init itself is SHA-256 checked); auditwheel, patchelf
  and their Python dependencies are included in the coordinator's SHA-256-pinned wheel lock.
  Windows builds use SHA-256-pinned wheel bundling and retagging tools, but the GitHub-hosted runner image and the
  exact installed MSVC/CPython patch versions still float. The pipeline records those versions and refuses to compare
  results made with different ones. Matching builds under the same tools **cannot** detect a malicious sdist, a
  compromised upstream tool, or an attack that gives the same compromised inputs to both builds.
- **A malicious library source, or a mistake in a recipe.** A wheel built with a library recipe contains that
  library's code. Each recipe adds one more pinned source archive to what these wheels trust (for PyYAML,
  libyaml 0.2.6's release tarball) and our build steps for it. The sha256 shows both builds used the archive a
  maintainer reviewed, not that the archive is safe; the library's `configure` and `make` run in the build as the
  package's own build code does. It is also our build of the library, not the project's or a distro's: a
  security fix in a newer libyaml reaches these wheels only when the recipe is updated and the wheel rebuilt.
  The Windows runner image is not pinned to an immutable digest: an image refresh may cause a pair to be rejected,
  which is safer than silently treating the runs as equivalent.
- **A compromise of GitHub Actions or of a runner image.** Both builds of a pair run on the same runner image, so
  matching builds show that the build is deterministic there, not that two independent parties built it.
- **A stale PyPI list.** The index is rebuilt hourly, so for the packages here a release uploaded in the last hour
  may not be visible yet to uv users (`uv pip --extra-index-url` too, which puts this index first). pip users see
  PyPI directly and aren't affected.
- **A compromised maintainer account.** Publishing needs a maintainer's approval, so a stolen account could
  approve a bad publish. Attestations name the workflow and commit that published each file.

**If something goes wrong**

A wheel is withdrawn by removing its entry from `data/gap-wheels.json` and its file from the release. The index
redeploys without it within minutes. Report unsafe wheels or potential security issues
**privately** as described in [SECURITY.md](SECURITY.md). For an install, import or runtime
failure without a suspected security problem, use the
[public broken-wheel report](https://github.com/wheelgap/wheels/issues/new?template=broken-wheel.yml).

## Adding a recipe

A recipe is data in [`scripts/library_recipes.py`](scripts/library_recipes.py) (`RECIPES`, and `NEEDS` for a
package that needs a library and has no recipe yet), plus a row in the [table above](#library-recipes). The
field-by-field description is at the top of that file. A maintainer reviews each one, and the bar is:

- **The source** is the library's official release archive, over https. Compute its sha256 from your own
  download and compare it with a second, independent source (the project's site and its GitHub release, or a
  distro's recipe), and say in the pull request what you compared. No `git clone`, no distro package, no archive
  GitHub generates from a tag.
- **The build is static and self-contained**: a static, position-independent library in the recipe's prefix, and
  no shared one (the build checks). If the library needs another library that isn't on the platform's standard
  list, that one needs a recipe too, built first.
- **The steps** are fixed argument lists: no shell, no downloads, no `-g` (debug information carries build paths),
  nothing that reads the clock or the host name.
- **The package's switch**, if it has one, that makes a missing library a build failure, set in `env`.
- **The licence** is OSI-approved, named as an SPDX expression, with the path and sha256 of its text in the
  archive. Check that static linking is something the licence allows on these terms.
- **The import check** fails unless the library is really in use, and checks the version the library reports.
- **Targets**: only ones you have built for. Before the first publish, build the pair twice in the pinned image,
  compare the two sha256s, and run the check in the install-test image.
- **The tests** in `tests/test_gap_wheels.py` pass, and cover the new entry.

Not covered yet: Windows (a recipe there needs MSVC build steps and a way to show the library to the package's
build, and `./configure` isn't one) and musl (the same recipe may well work in the Alpine image, but nobody has
built and tested it there, so it isn't listed).

## Reporting a wheel that doesn't work

If a published wheel fails to install or import, or crashes while the same source
release works, [open a broken-wheel issue](https://github.com/wheelgap/wheels/issues/new?template=broken-wheel.yml).
Include the wheel filename, Python version, OS details, installer version, exact
command and full error, and whether a source build works on the same machine.
For pip, you can try `python -m pip install --no-binary NAME NAME==VERSION`
in a fresh environment (only if you trust the package and can build it there).
Do **not** post a suspected unsafe or malicious wheel publicly: use
[private vulnerability reporting](SECURITY.md) instead.

## Opting out

If you maintain a package here and would rather we didn't publish wheels for it,
[open an issue](https://github.com/wheelgap/wheels/issues/new?title=Opt-out%3A%20PROJECT) and its wheels come down.
It's then listed in `data/gap-wheels-optout.txt`, and no later batch builds it. When your own release ships a
wheel that installs where ours does, ours drops out of the index at the next hourly rebuild.

## How this repo is built

The pipeline code was written with AI assistance (Claude), reviewed by AI agents and a human, and every change and
every publish is approved by a maintainer.
