# wheelgap/wheels

Wheels PyPI doesn't have yet, for popular packages on platforms where pip would otherwise have to compile them. The
index is at **https://wheelgap.github.io/wheels/simple/** and the list of wheels, with how to use them, is at
https://wheelgap.github.io/wheels/. (The older address, https://wheelgap.github.io/tracker/wheels/simple/, keeps
working.)

```
pip install --extra-index-url https://wheelgap.github.io/wheels/simple/ PACKAGE
```

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

The wheels published so far are Windows on ARM ones for Python 3.14 and 3.14t. Linux wheels are built and repaired
with a pinned auditwheel in the pinned `manylinux_2_28` or Alpine image, and a Linux wheel goes up only if every
library it links against is bundled in it, where the loader finds it, or on that platform's standard list, as
auditwheel's policy gives it. None is published until a maintainer approves a publish run that includes one
([tracker#124](https://github.com/wheelgap/tracker/issues/124)).

This repo holds only the pipeline that builds and publishes them, kept apart from
[wheelgap](https://github.com/wheelgap/wheelgap) and [the tracker](https://github.com/wheelgap/tracker), which take
lots of outside contributions. Changes here are made by the maintainers, and every publish is approved by one.

## How a wheel gets here

1. **Pick:** the tracker's build lab records which releases build cleanly from their sdist where PyPI has no wheel
   (`data/builds.json` there). The weekly batch (`.github/workflows/gap-wheels.yml`) plans pairs from that list: a
   build with a compiled extension module, or one whose wheel carries native code without one (tagged
   `py3-none-<platform>`, such as LightGBM's DLL, which it loads with `ctypes`).
2. **Build twice:** each pair is built in two separate workflow runs (`gap-wheel-build.yml`), from the sdist PyPI
   publishes (its sha256 is checked against PyPI's), unpacked to a fixed path with a fixed `SOURCE_DATE_EPOCH`. On
   Linux the wheel is repaired with a pinned `auditwheel` first. On Windows, if its binaries import VC++ runtime DLLs
   a user's machine may lack (`msvcp140.dll`, `vcomp140.dll`…), a pinned `delvewheel` bundles those, and only those
   (by name), into `<dist>.libs/` under names carrying their hash. A `py3-none-<platform>` wheel whose own tag is the
   target's platform is first retagged with a pinned `wheel` to the target's one exact tag, so it installs only on the
   Python it was built and tested on. All of this happens before hashing, in both builds, and both tools are pinned by
   version and sha256.
3. **Verify:** a trusted job compares the two results. A pair counts only if both wheels are byte-identical and pip
   installs the wheel without compiling anything.
4. **Stage:** the trusted job takes the wheel file from a build run only if its sha256 is the verified one, and only
   for the one exact tag the target allows, in its filename and in its own `.dist-info/WHEEL` file, and on Windows only
   if every binary's PE header is for the target's CPU. The wheel can't need a DLL a user's machine may lack (on Windows, read
   from each `.pyd`/`.dll`/`.exe`'s imports; only the VC++ runtime counts as bundled from `<dist>.libs/`, only under a
   hashed name, which no other copy on the system can answer to, and only for a `.pyd`/`.dll`'s normal imports, not
   an `.exe`'s or a delay-load's) or, on Linux, link against a library that is neither bundled in the wheel
   (where its `$ORIGIN` RUNPATH or RPATH points) nor on the `manylinux_2_28` or `musllinux_1_2` list in auditwheel's
   policy, or need a glibc symbol version newer than 2.28 (read from each ELF file's dynamic section and version
   needs). Libraries a package loads by name at run time (`dlopen`) aren't checked. The release must name an
   OSI-approved licence (or the PSF licence) on PyPI. Staging only reads the files, never runs them.
5. **Install test:** each staged wheel is installed and imported on a fresh runner of its platform. A wheel without
   an extension module, or on Windows one with a bundled `<dist>.libs/`, must also load every native library it ships
   outside `<dist>.libs/` (with `ctypes`, after importing the package), which proves the bundled DLLs are found. Linux wheels are
   tested in a minimal container of their libc, pinned by digest, with the target's exact CPython (`python:3.14-alpine`,
   `python:3.15-slim-trixie`, and `debian:trixie-slim` with a pinned, sha256-checked python-build-standalone 3.14t),
   not on GitHub's runner image, which has many libraries a user's system doesn't.
6. **Publish, after a maintainer approves:** the publish job waits on the `publish` environment's required reviewer.
   It then attests the files (`gh attestation verify FILE.whl -R wheelgap/wheels`), uploads them to the `gap-wheels`
   release, records them in `data/gap-wheels.json` and deploys the index.

**Bundled Microsoft DLLs.** Some Windows wheels carry Microsoft Visual C++ runtime files (`msvcp140.dll`,
`vcomp140.dll` and the like) in `<dist>.libs/`. They're Microsoft's, not the package's, redistributed under the
"Distributable Code" terms of Visual Studio, as many wheels on PyPI do (numpy's Windows wheels, for one). The
package's own licence, which staging checks, doesn't cover them.

## Threat model

**What the pipeline defends against**

- **A build that substitutes another package's files, or forges another build's result.** Each build runs in its
  own workflow run, so its artifact token can only write its own artifacts. The coordinator takes results only from
  the run with that build's exact title, started from `main` at its own commit, and refuses duplicates.
- **A build whose output differs from what the sdist gives.** Something changed in one run, or the build isn't
  reproducible: two builds must match byte for byte, or nothing is published.
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
- **A compromise of a build toolchain.** Linux builds run in a pinned image (`quay.io/pypa/manylinux_2_28` or
  `python:3.14-alpine`, by digest) with a pinned Rust (rustup-init checked by sha256, then Rust's own channel
  manifest over TLS). The wheel tools that change a built wheel (auditwheel and patchelf on Linux; delvewheel and
  `wheel` for bundling and retagging, pinned by version and sha256) are part of it too, and so are the VC++ runtime
  DLLs delvewheel bundles from the runner image. Build backends (setuptools, Cython…) aren't pinned yet (#4). Both
  builds of a pair use the same toolchain, so matching builds can't catch a bad one.
- **A compromise of GitHub Actions or of a runner image.** Both builds of a pair run on the same runner image, so
  matching builds show that the build is deterministic there, not that two independent parties built it.
- **A stale PyPI list.** The index is rebuilt hourly, so for the packages here a release uploaded in the last hour
  may not be visible yet to uv users (`uv pip --extra-index-url` too, which puts this index first). pip users see
  PyPI directly and aren't affected.
- **A compromised maintainer account.** Publishing needs a maintainer's approval, so a stolen account could
  approve a bad publish. Attestations name the workflow and commit that published each file.

**If something goes wrong**

A wheel is withdrawn by removing its entry from `data/gap-wheels.json` and its file from the release. The index
redeploys without it within minutes. Report a problem as described in [SECURITY.md](SECURITY.md).

## Opting out

If you maintain a package here and would rather we didn't publish wheels for it,
[open an issue](https://github.com/wheelgap/wheels/issues/new?title=Opt-out%3A%20PROJECT) and its wheels come down.
It's then listed in `data/gap-wheels-optout.txt`, and no later batch builds it. When your own release ships a
wheel that installs where ours does, ours drops out of the index at the next hourly rebuild.

## How this repo is built

The pipeline code was written with AI assistance (Claude), reviewed by AI agents and a human, and every change and
every publish is approved by a maintainer.
