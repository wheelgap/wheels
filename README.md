# wheelgap/wheels

Wheels PyPI doesn't have yet, for popular packages on platforms where pip would otherwise have to compile them. The
index is at **https://wheelgap.github.io/wheels/simple/** and the list of wheels, with how to use them, is at
https://wheelgap.github.io/wheels/. (The older address, https://wheelgap.github.io/tracker/wheels/simple/, keeps
working.)

```
pip install --extra-index-url https://wheelgap.github.io/wheels/simple/ PACKAGE
```

uv works too. Pin the packages you need to the index in `pyproject.toml`:

```toml
[[tool.uv.index]]
name = "wheelgap"
url = "https://wheelgap.github.io/wheels/simple/"
explicit = true

[tool.uv.sources]
tokenizers = { index = "wheelgap" }
```

uv takes all of a package's files from one index, so each project's page here lists every file PyPI has for it as well
(PyPI's own links and hashes, refreshed hourly), plus our wheel. A lockfile made this way has exactly PyPI's files for
that package, plus ours.

Today that's Windows on ARM wheels for Python 3.14 and 3.14t. Linux is being worked on in
[tracker#124](https://github.com/wheelgap/tracker/issues/124) and isn't published yet.

This repo holds only the pipeline that builds and publishes them, kept apart from
[wheelgap](https://github.com/wheelgap/wheelgap) and [the tracker](https://github.com/wheelgap/tracker), which take
lots of outside contributions. Changes here are made by the maintainers, and every publish is approved by one.

## How a wheel gets here

1. **Pick:** the tracker's build lab records which releases build cleanly from their sdist where PyPI has no wheel
   (`data/builds.json` there). The weekly batch (`.github/workflows/gap-wheels.yml`) plans pairs from that list.
2. **Build twice:** each pair is built in two separate workflow runs (`gap-wheel-build.yml`), from the sdist PyPI
   publishes (its sha256 is checked against PyPI's), unpacked to a fixed path with a fixed `SOURCE_DATE_EPOCH`. On
   Linux the wheel is repaired with a pinned `auditwheel` first.
3. **Verify:** a trusted job compares the two results. A pair counts only if both wheels are byte-identical and pip
   installs the wheel without compiling anything.
4. **Stage:** the trusted job takes the wheel file from a build run only if its sha256 is the verified one, and only
   for the one exact tag the target allows. The wheel can't need a DLL a user's machine may lack, and the release
   must name an OSI-approved licence (or the PSF licence) on PyPI.
5. **Install test:** each staged wheel is installed and imported on a fresh runner of its platform.
6. **Publish, after a maintainer approves:** the publish job waits on the `publish` environment's required reviewer.
   It then attests the files (`gh attestation verify FILE.whl -R wheelgap/wheels`), uploads them to the `gap-wheels`
   release, records them in `data/gap-wheels.json` and deploys the index.

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
- **A PyPI answer that would change what an installer sees.** The PyPI part of each page is copied from PyPI's JSON
  simple API: only `https://files.pythonhosted.org/packages/` links with PyPI's sha256, and every field must have the
  shape the spec gives. If PyPI can't be read for any project, or anything is off, nothing is deployed and the
  previous index stays up. It is never published with files missing.

**What it does not defend against**

- **A malicious sdist.** A wheel built from it is as malicious as the sdist. Installing the package from PyPI the
  normal way runs the same code. These wheels are only as trustworthy as the sdist PyPI serves.
- **A compromise of GitHub Actions or of a runner image.** Both builds of a pair run on the same runner image, so
  matching builds show that the build is deterministic there, not that two independent parties built it.
- **A stale PyPI list.** The index is rebuilt hourly, so for the packages here a release uploaded in the last hour
  may not be visible yet to uv users who pinned them to this index. pip users see PyPI directly and aren't affected.
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
