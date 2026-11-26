# AGENTS.md

Instructions for coding agents (Codex, Claude Code, Copilot, Cursor and others) and anyone else working on wheelgap/wheels. [CONTRIBUTING.md](CONTRIBUTING.md) points here.

## What this repo does

It builds the wheels PyPI is missing and publishes them on the [gap wheel index](https://wheelgap.github.io/wheels/), the unofficial fallback that `wheelgap --fix` suggests when no official release has the wheel. Each wheel is built twice from PyPI's sdist, published only when both builds are byte-for-byte identical, and installed and imported on a fresh machine first. The [README](README.md) explains the pipeline and its threat model.

**Trust is the product.** A wheel that installs but is wrong is worse than no wheel. When a check can't be sure, it refuses.

## Setup and checks

You need [uv](https://docs.astral.sh/uv/). Run the tests exactly as CI does:

```bash
uvx --python 3.14 pytest@8.4.2
```

One file: `uvx --python 3.14 pytest@8.4.2 tests/test_gap_index.py`. The repo isn't a package; `pyproject.toml` only holds pytest settings.

## Code map

| File | What it does |
| --- | --- |
| `scripts/build_lab.py` | The build lab: does a package with no wheel for a platform build from its source there, and does the result work? |
| `scripts/gap_wheels.py` | Verifies which of the lab's clean builds are reproducible and install, and publishes them |
| `scripts/build_gap_index.py` | Publishes the index (a PEP 503 simple index and its page) from `data/gap-wheels.json` |
| `scripts/build_tool_lock.py` | Resolves and verifies build-tool wheels before either untrusted build starts |
| `scripts/library_recipes.py` | The reviewed allowlist of system libraries a gap-wheel build may compile and link in |
| `scripts/container_build.sh` | One gap-wheel build inside its target's image |
| `data/` | `gap-wheels.json` (what's published), `gap-wheel-requests.json`, `gap-wheels-optout.txt` |
| `tests/` | Offline tests for each script, plus `test_data.py`, which checks the published data itself |

## Tests

- **No network.** Pass a fake fetch function or mock `urllib.request.urlopen`, as `tests/test_gap_index.py` does.
- **Every behaviour change gets a test that fails without the change.** Check it by reverting your source change and running the test.
- Write test files with non-ASCII text using `encoding="utf-8"`.

## House rules

- Standard library only in `scripts/`.
- Changes under `.github/`, to `scripts/build_tool_lock.py`, to `scripts/library_recipes.py` or to anything that decides what gets published get extra scrutiny, so expect them to take longer.
- New library recipes are paused for now; fixes and tests for the existing ones are welcome.
- Match the surrounding code. Don't reformat or refactor what your change doesn't need.

## Picking work and opening a PR

- Start with the issues labelled [`good first issue`](https://github.com/wheelgap/wheels/labels/good%20first%20issue): each names the files to change, the tests to add and when it's done. The [Start here](https://github.com/wheelgap/wheelgap/issues/382) issue lists the open ones across all three wheelgap repos.
- **Claiming:** comment on the issue and it's assigned to you for 18 hours. Keep it by opening a PR that links the issue (a draft is fine) within that time. An issue nobody has asked for goes to the first PR opened for it.
- In the PR description, use `Closes #N` only if the PR finishes the issue; otherwise write `Part of #N`. One issue per PR.
- Leave "Allow edits by maintainers" on: the maintainer may push small fixes before merging.
- Say which AI tool helped (e.g. "Made with Codex"). The person opening the PR must have read and run the change.
