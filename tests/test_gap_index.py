import html
import http.client
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree

from scripts import build_gap_index

SHA = "c" * 64


def wheel(name="tiktoken", file="tiktoken-0.14.0-cp314-cp314-win_arm64.whl", **over):
    w = {"name": name, "version": "0.14.0", "target": "3.14 win-arm64", "wheel": file, "sha256": SHA,
         "requires_python": ">=3.9", "verified": "2026-11-16",
         "builds": ["https://github.com/wheelgap/tracker/actions/runs/1", "javascript:alert(1)"]}  # fmt: skip
    w.update(over)
    return w


PYPI_SHA = "a" * 64
META_SHA = "b" * 64


def pypi_file(filename, **over):
    f = {"filename": filename, "url": f"https://files.pythonhosted.org/packages/ab/cd/ef/{filename}",
         "hashes": {"sha256": PYPI_SHA}, "requires-python": ">=3.9", "yanked": False,
         "core-metadata": {"sha256": META_SHA}, "data-dist-info-metadata": {"sha256": META_SHA}}  # fmt: skip
    f.update(over)
    return f


OURS = "releases/download/gap-wheels/"
ANCHOR = re.compile(r"<a\s([^>]*)>")
ATTRIBUTE = re.compile(r'([a-z-]+)="([^"]*)"')


def simple_links(page):
    """Each link of a simple index page as a dict of its attributes, unescaped (so `href` is the URL an installer
    fetches). Read back from the HTML with regexes of its own, not with build_gap_index's formatting code."""
    return [{name: html.unescape(value) for name, value in ATTRIBUTE.findall(attrs)}
            for attrs in ANCHOR.findall(page.read_text(encoding="utf-8"))]  # fmt: skip


def no_network(project):
    raise AssertionError(f"a test fetched {project} from PyPI")


class FileNames(unittest.TestCase):
    """Direct, offline coverage for the file-name and platform helpers."""

    def test_wheel_tags_five_and_six_parts(self):
        expected = {("cp314", "cp314", "win_arm64")}
        self.assertEqual(build_gap_index.wheel_tags("Foo_Bar-1.0RC1-cp314-cp314-win_arm64.whl"),
                         ("foo-bar", "1.0rc1", expected))
        self.assertEqual(build_gap_index.wheel_tags("Foo_Bar-1.0RC1-1-cp314-cp314-win_arm64.whl"),
                         ("foo-bar", "1.0rc1", expected))

    def test_wheel_tags_expand_compressed_sets(self):
        filename = "foo_bar-1.0-cp314.cp315-abi3.none-win_arm64.manylinux_2_28_x86_64.whl"
        parsed = build_gap_index.wheel_tags(filename)
        self.assertIsNotNone(parsed)
        name, version, tags = parsed
        self.assertEqual((name, version), ("foo-bar", "1.0"))
        self.assertEqual(tags, {(py, abi, plat)
                                for py in ("cp314", "cp315")
                                for abi in ("abi3", "none")
                                for plat in ("win_arm64", "manylinux_2_28_x86_64")})
        self.assertIsNone(build_gap_index.wheel_tags("foo-1.0.tar.gz"))
        self.assertIsNone(build_gap_index.wheel_tags("foo-1.0-cp314-win_arm64.whl"))

    def test_file_version_for_wheels_and_sdists(self):
        cases = {
            "tiktoken-0.14.0RC1-cp314-cp314-win_arm64.whl": "0.14.0rc1",
            "tiktoken-0.14.0RC1.tar.gz": "0.14.0rc1",
            "tiktoken-0.14.0RC1.zip": "0.14.0rc1",
            "tiktoken-0.14.0-py3-none-any.whl": "0.14.0",
            "other-1.0.tar.gz": None,
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                self.assertEqual(build_gap_index.file_version(filename, "tiktoken"), expected)
        self.assertIsNone(build_gap_index.file_version("foo-bar-1.0.tar.gz", "foo"))

    def test_release_yank_absent_mixed_and_all_yanked(self):
        fn1 = "foo-1.0.tar.gz"
        fn2 = "foo-1.0-py3-none-any.whl"
        self.assertEqual(build_gap_index.release_yank("1.0", "foo", [(fn1, None)]), (True, None))
        self.assertEqual(build_gap_index.release_yank("2.0", "foo", [(fn1, "reason")]), (False, None))
        self.assertEqual(build_gap_index.release_yank("1.0", "foo", [(fn1, "reason"), (fn2, None)]),
                         (True, None))
        self.assertEqual(build_gap_index.release_yank("1.0", "foo", [(fn1, "reason"), (fn2, "other")]),
                         (True, "reason"))
        self.assertEqual(build_gap_index.release_yank("1.0", "foo", [(fn1, ""), (fn2, "")]), (True, ""))

    def test_linux_platform_standard_and_legacy_tags(self):
        self.assertEqual(build_gap_index.linux_platform("manylinux_2_28_x86_64"),
                         ("manylinux", 2, 28, "x86_64"))
        self.assertEqual(build_gap_index.linux_platform("musllinux_1_2_aarch64"),
                         ("musllinux", 1, 2, "aarch64"))
        for name, version, arch in (("manylinux1", 5, "i686"),
                                    ("manylinux2010", 12, "x86_64"),
                                    ("manylinux2014", 17, "x86_64")):
            self.assertEqual(build_gap_index.linux_platform(f"{name}_{arch}"),
                             ("manylinux", 2, version, arch))
        for invalid in ("linux_x86_64", "win_arm64", "manylinux2014_x86_64.evil"):
            self.assertIsNone(build_gap_index.linux_platform(invalid))

    def test_platform_covers_same_libc_and_architecture(self):
        self.assertTrue(build_gap_index.platform_covers("manylinux_2_28_x86_64",
                                                        "manylinux_2_28_x86_64"))
        self.assertTrue(build_gap_index.platform_covers("manylinux2014_x86_64",
                                                        "manylinux_2_28_x86_64"))
        self.assertTrue(build_gap_index.platform_covers("manylinux_2_17_x86_64",
                                                        "manylinux_2_28_x86_64"))
        self.assertFalse(build_gap_index.platform_covers("manylinux_2_28_x86_64",
                                                         "manylinux_2_17_x86_64"))
        self.assertFalse(build_gap_index.platform_covers("musllinux_1_2_x86_64",
                                                         "manylinux_2_28_x86_64"))
        self.assertFalse(build_gap_index.platform_covers("manylinux_2_17_aarch64",
                                                         "manylinux_2_28_x86_64"))


class Index(unittest.TestCase):
    def build(self, wheels, pypi=None, expect=0, sdists=True, requests=None, **kw):
        """Build with a fake PyPI. Unless `sdists` is false, PyPI also lists each wheel's release (its sdist), as it
        does for every release we build from; `sdists` can also be a set of project names to leave without theirs.
        `requests` is written as data/gap-wheel-requests.json."""
        tmp = Path(tempfile.mkdtemp())
        (tmp / "gap-wheels.json").write_text(json.dumps({"schema": 1, "wheels": wheels}))
        if requests is not None:
            (tmp / "gap-wheel-requests.json").write_text(json.dumps(requests))
        self.fetched = []
        self.pypi_listed = {}

        def fetch(project):
            self.fetched.append(project)
            files = list((pypi or {}).get(project, []))
            if sdists and (sdists is True or project not in sdists):
                for w in wheels:
                    if build_gap_index.canonical(w["name"]) == project:
                        name, version = w["wheel"].split("-")[:2]
                        if not any(f.get("filename") == f"{name}-{version}.tar.gz" for f in files if isinstance(f, dict)):
                            files.append(pypi_file(f"{name}-{version}.tar.gz"))
            self.pypi_listed[project] = [f.get("filename") for f in files if isinstance(f, dict)]
            return files

        got = build_gap_index.build(tmp / "gap-wheels.json", tmp / "site", fetch=fetch, **kw)
        self.assertEqual(got, expect)
        return tmp / "site"

    def assert_status_matches_index(self, site, pypi_listed=None):
        """data/status.json (what programs read) and the simple pages (what pip and uv read) describe the same
        decisions: every wheel status.json calls in-index or yanked is linked exactly once from its project's page,
        nothing else of ours is linked anywhere, and the human page counts the same wheels. `pypi_listed` is the
        file names the fake PyPI gave for each project, by default those of the last self.build()."""
        pypi_listed = self.pypi_listed if pypi_listed is None else pypi_listed
        simple = site / "simple"
        rows = json.loads((site / "data/status.json").read_text(encoding="utf-8"))["wheels"]
        known = {"in-index", "yanked", "withdrawn-pypi-has-wheel", "withdrawn-release-gone", "left-out-pypi-unreadable"}
        root = [link["href"] for link in simple_links(simple / "index.html")]
        expected: dict[str, set[str]] = {}  # project -> the links to our release its page should have
        for row in rows:
            project, name, state = row["project"], row["wheel"], row["status"]
            where = f"{project}: {name} ({state})"
            self.assertIn(state, known, where)
            self.assertEqual(row["url"], build_gap_index.RELEASE_URL + name, where)
            page = simple / project / "index.html"
            if state == "left-out-pypi-unreadable":
                self.assertFalse((simple / project).exists(), f"{where}: its project has a simple/ folder")
                self.assertNotIn(f"{project}/", root, f"{where}: its project is on simple/index.html")
                self.assertIsNone(row["yank_reason"], where)
                continue
            self.assertTrue(page.is_file(), f"{where}: its project has no simple page")
            self.assertEqual(root.count(f"{project}/"), 1, f"{where}: its project isn't once on simple/index.html")
            links = simple_links(page)
            mine = [link for link in links if link["href"].split("#")[0] == row["url"]]
            if state in ("in-index", "yanked"):
                self.assertEqual([link["href"] for link in mine], [f"{row['url']}#sha256={row['sha256']}"],
                                 f"{where}: not linked exactly once, with its hash")  # fmt: skip
                expected.setdefault(project, set()).add(mine[0]["href"])
                if state == "yanked":
                    self.assertIsInstance(row["yank_reason"], str, where)
                    self.assertEqual(mine[0].get("data-yanked"), row["yank_reason"], f"{where}: data-yanked differs")
                else:
                    self.assertIsNone(row["yank_reason"], where)
                    self.assertNotIn("data-yanked", mine[0], f"{where}: its link is marked yanked")
            else:
                self.assertEqual(mine, [], f"{where}: still linked from its project's page")
                self.assertIsNone(row["yank_reason"], where)
                # The page is still PyPI's list for the project: each file PyPI gave, from PyPI's own host.
                theirs = sorted(link["href"].split("#")[0].rsplit("/", 1)[1] for link in links
                                if link["href"].startswith(build_gap_index.PYPI_FILES))  # fmt: skip
                self.assertEqual(theirs, sorted(pypi_listed[project]), f"{where}: PyPI's files aren't all listed")
        # No link to our release is left over: each one on any simple page belongs to an in-index or yanked row.
        self.assertEqual(sorted(f"{p.name}/" for p in simple.iterdir() if p.is_dir()), sorted(root))
        for page in simple.glob("*/index.html"):
            ours = sorted(link["href"] for link in simple_links(page) if OURS in link["href"])
            self.assertEqual(ours, sorted(expected.get(page.parent.name, ())), f"{page.parent.name}: a link without a row")
        self.assertEqual([link["href"] for link in simple_links(simple / "index.html") if OURS in link["href"]], [])
        listed = sum(len(links) for links in expected.values())
        human = (site / "index.html").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"(\d+) so far", human), [str(listed)])

    def test_status_json_and_the_simple_index_agree_when_one_build_mixes_every_status(self):
        t314 = "tiktoken-0.14.0-cp314-cp314t-win_arm64.whl"
        lz4_old, lz4_new = "lz4-4.4.4-cp314-cp314-win_arm64.whl", "lz4-4.4.5-cp314-cp314-win_arm64.whl"
        wheels = [
            wheel(),  # in-index
            wheel(file=t314, target="3.14t win-arm64", sha256="1" * 64),  # PyPI has this one now
            wheel(name="lz4", file=lz4_old, version="4.4.4", sha256="2" * 64),  # in-index, beside a yanked release
            wheel(name="lz4", file=lz4_new, version="4.4.5", sha256="3" * 64),  # yanked, with a reason to escape
            wheel(name="regex", file="regex-2026.1.1-cp314-cp314-win_arm64.whl", version="2026.1.1", sha256="4" * 64),
            wheel(name="zstandard", file="zstandard-0.25.0-cp314-cp314-win_arm64.whl", version="0.25.0", sha256="5" * 64),
            wheel(name="Zope.Interface", file="zope_interface-8.7-cp314-cp314-win_arm64.whl", version="8.7", sha256="6" * 64),
            wheel(name="broken", file="broken-1.0-cp314-cp314-win_arm64.whl", version="1.0", sha256="7" * 64),
            wheel(name="broken", file="broken-1.0-cp314-cp314t-win_arm64.whl", version="1.0", sha256="8" * 64),
        ]  # fmt: skip
        pypi = {
            "tiktoken": [pypi_file(t314)],
            "lz4": [pypi_file("lz4-4.4.5.tar.gz", yanked='bad "build" <1>')],
            "regex": [pypi_file("regex-2026.1.1.tar.gz", yanked=True)],  # yanked without a reason
            "zstandard": [pypi_file("zstandard-0.26.0.tar.gz")],  # the release ours was built from is gone
            "zope-interface": [pypi_file("zope.interface-8.7-cp314-cp314-win_arm64.whl")],  # PyPI's spelling of ours
            "broken": [pypi_file("broken-1.0.tar.gz", url="https://untrusted.example/broken-1.0.tar.gz")],
        }
        site = self.build(wheels, pypi, sdists={"zstandard"}, omit_failing=True)
        self.assert_status_matches_index(site)
        rows = json.loads((site / "data/status.json").read_text(encoding="utf-8"))["wheels"]
        self.assertEqual(
            [(row["project"], row["wheel"], row["status"], row["yank_reason"]) for row in rows],
            [
                ("broken", "broken-1.0-cp314-cp314-win_arm64.whl", "left-out-pypi-unreadable", None),
                ("broken", "broken-1.0-cp314-cp314t-win_arm64.whl", "left-out-pypi-unreadable", None),
                ("lz4", lz4_old, "in-index", None),
                ("lz4", lz4_new, "yanked", 'bad "build" <1>'),
                ("regex", "regex-2026.1.1-cp314-cp314-win_arm64.whl", "yanked", ""),
                ("tiktoken", "tiktoken-0.14.0-cp314-cp314-win_arm64.whl", "in-index", None),
                ("tiktoken", t314, "withdrawn-pypi-has-wheel", None),
                ("zope-interface", "zope_interface-8.7-cp314-cp314-win_arm64.whl", "withdrawn-pypi-has-wheel", None),
                ("zstandard", "zstandard-0.25.0-cp314-cp314-win_arm64.whl", "withdrawn-release-gone", None),
            ],
        )  # every status is there, so the helper had each kind of row to check
        self.assertIn("4 so far", (site / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(sorted(p.name for p in (site / "simple").iterdir()),
                         ["index.html", "lz4", "regex", "tiktoken", "zope-interface", "zstandard"])  # fmt: skip

    def test_the_status_helper_reads_links_as_an_installer_would(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "index.html").write_text(
            '<a href="https://example.test/a&amp;b.whl#sha256=00" data-yanked="">a</a><br>\n'
            '<a href="https://example.test/c.whl#sha256=11" data-requires-python="&gt;=3.9" '
            'data-yanked="a &quot;b&quot; &lt;c&gt;">c</a>\n<a href="d/">d</a>', encoding="utf-8")  # fmt: skip
        self.assertEqual(simple_links(folder / "index.html"), [
            {"href": "https://example.test/a&b.whl#sha256=00", "data-yanked": ""},
            {"href": "https://example.test/c.whl#sha256=11", "data-requires-python": ">=3.9",
             "data-yanked": 'a "b" <c>'},
            {"href": "d/"},
        ])  # fmt: skip

    def test_pep_503_pages_with_hashes(self):
        site = self.build([wheel(), wheel(name="Zope.Interface", file="zope_interface-8.7-cp314-cp314-win_arm64.whl")])
        root = (site / "simple/index.html").read_text()
        self.assertIn('<a href="tiktoken/">tiktoken</a>', root)
        self.assertIn('<a href="zope-interface/">zope-interface</a>', root)  # normalised as PEP 503 says
        page = (site / "simple/tiktoken/index.html").read_text()
        self.assertIn(
            '<a href="https://github.com/wheelgap/wheels/releases/download/gap-wheels/'
            f'tiktoken-0.14.0-cp314-cp314-win_arm64.whl#sha256={SHA}" data-requires-python="&gt;=3.9">',
            page,
        )
        self.assertIn('name="pypi:repository-version"', page)

    def test_entries_that_cant_be_linked_safely_are_left_out(self):
        site = self.build([wheel(file="x.whl\"><script>"), wheel(sha256="nothex"), wheel(name="../evil")])
        self.assertEqual(list((site / "simple").iterdir()), [site / "simple/index.html"])

    def test_the_human_page_escapes_and_links_only_github_builds(self):
        site = self.build([wheel(version="<b>1</b>", requires_python='"><x')])
        page = (site / "index.html").read_text()
        self.assertIn("&lt;b&gt;1&lt;/b&gt;", page)
        self.assertNotIn("javascript:", page)
        self.assertIn("https://github.com/wheelgap/tracker/actions/runs/1", page)
        self.assertIn("gh attestation verify FILE.whl -R wheelgap/wheels", page)
        self.assertNotIn("-R wheelgap/tracker", page)
        self.assertNotIn("unsafe-best-match", page)
        project = (site / "simple/tiktoken/index.html").read_text()
        self.assertIn('data-requires-python="&quot;&gt;&lt;x"', project)

    def test_published_wheel_has_install_command_and_hash(self):
        page = (self.build([wheel()]) / "index.html").read_text()
        self.assertIn('<th scope="col">Install</th>', page)
        self.assertIn(
            '<code>pip install --extra-index-url '
            'https://wheelgap.github.io/wheels/simple/ tiktoken==0.14.0</code>',
            page,
        )
        self.assertIn(f'<small title="{SHA}">{SHA[:12]}</small>', page)
        self.assertIn('<div class="matrix-scroll" tabindex="0" role="region" aria-labelledby="published_wheels">', page)
        self.assertNotIn("<script", page)

    def test_a_wheel_built_with_a_library_recipe_says_what_it_bundles(self):
        libyaml = {"library": "libyaml", "version": "0.2.5", "licence": "MIT", "sha256": "c" * 64,
                   "url": "https://github.com/yaml/libyaml/releases/download/0.2.5/yaml-0.2.5.tar.gz",
                   "licence_sha256": "d" * 64, "recipe_sha256": "e" * 64}  # fmt: skip
        with_recipe = wheel(name="pyyaml", file="pyyaml-6.0.3-cp315-cp315-manylinux_2_28_x86_64.whl",
                            version="6.0.3", target="3.15 linux-x86_64", recipes=[libyaml])  # fmt: skip
        page = (self.build([with_recipe, wheel()]) / "index.html").read_text()
        self.assertEqual(page.count("<small>bundles libyaml 0.2.5 (MIT)</small>"), 1)
        row = next(line for line in page.splitlines() if 'id="pyyaml"' in line)
        self.assertIn(f'<small title="{SHA}">{SHA[:12]}</small><br><small>bundles libyaml 0.2.5 (MIT)</small><br><a href=', row)
        self.assertNotIn("bundles", next(line for line in page.splitlines() if 'id="tiktoken"' in line))
        self.assertEqual(build_gap_index.bundled(wheel()), "")
        two = [libyaml, {**libyaml, "library": "zlib-ng", "version": "2.2.4", "licence": "Zlib"}]
        self.assertEqual(build_gap_index.bundled(wheel(recipes=two)), "bundles libyaml 0.2.5 (MIT), zlib-ng 2.2.4 (Zlib)")
        # An entry whose record isn't what publish writes says nothing, and nothing of it reaches the page.
        for bad in ([], "libyaml", [{**libyaml, "library": "<script>alert(1)</script>"}], [{**libyaml, "version": 5}],
                    [{**libyaml, "licence": '"><img src=x>'}], [libyaml] * 6, [{"library": "libyaml"}]):  # fmt: skip
            with self.subTest(bad):
                self.assertEqual(build_gap_index.bundled(wheel(recipes=bad)), "")
                page = (self.build([wheel(recipes=bad)]) / "index.html").read_text()
                self.assertNotIn("bundles", page.split('id="published_wheels"', 1)[1])
                self.assertNotIn("<script", page)
                self.assertNotIn("<img", page)

    def test_published_table_has_unique_accessible_links_and_keyboard_scrolling(self):
        wheels = [
            wheel(builds=["https://github.com/wheelgap/tracker/actions/runs/1",
                          "https://github.com/wheelgap/tracker/actions/runs/2"]),
            wheel(name="lz4", file="lz4-4.4.5-cp314-cp314-win_arm64.whl", version="4.4.5",
                  builds=["https://github.com/wheelgap/tracker/actions/runs/3",
                          "https://github.com/wheelgap/tracker/actions/runs/4"]),
        ]
        page = (self.build(wheels) / "index.html").read_text(encoding="utf-8")
        heading = '<h2 id="published_wheels">Published wheels</h2>'
        self.assertIn(heading, page)
        self.assertEqual(page.count('id="published_wheels"'), 1)
        self.assertIn(
            '<div class="matrix-scroll" tabindex="0" role="region" aria-labelledby="published_wheels">',
            page,
        )
        thead = page.split("<thead>", 1)[1].split("</thead>", 1)[0]
        headers = re.findall(r"<th\b[^>]*>", thead)
        self.assertEqual(len(headers), 8)
        self.assertTrue(all('scope="col"' in header for header in headers))

        links = re.findall(r'<a href="[^"]+" aria-label="([^"]+)">(wheel|build [12])</a>', page)
        self.assertEqual(len(links), 6)
        self.assertEqual(len({label for label, _ in links}), len(links))
        for label, visible_text in links:
            self.assertTrue(label.startswith(visible_text + " "), label)
        self.assertNotIn("<script", page)

    def test_current_published_data_has_accessible_links_without_scripts(self):
        path = Path(__file__).resolve().parents[1] / "data" / "gap-wheels.json"
        published = json.loads(path.read_text(encoding="utf-8"))["wheels"]
        page = build_gap_index.human_page(build_gap_index.projects(published))
        for entry in published:
            with self.subTest(wheel=entry["wheel"]):
                self.assertIn(f'aria-label="wheel {entry["wheel"]}"', page)
                for n in (1, 2):
                    self.assertIn(f'aria-label="build {n} of {entry["wheel"]}"', page)
        self.assertEqual(page.count('scope="col"'), 8)
        self.assertIn('aria-labelledby="published_wheels"', page)
        self.assertNotIn("<script", page)

    def test_withdrawn_wheel_has_no_install_command(self):
        filename = "tiktoken-0.14.0-cp314-cp314-win_arm64.whl"
        page = (self.build([wheel()], {"tiktoken": [pypi_file(filename)]}) / "index.html").read_text()
        row = page.split('<tr id="tiktoken">', 1)[1].split("</tr>", 1)[0]
        self.assertIn("withdrawn: PyPI has its own now", row)
        self.assertIn("<td></td>", row)
        self.assertNotIn("pip install", row)

    def test_the_page_says_the_wheels_are_unofficial_and_where_to_report(self):
        page = (self.build([wheel()]) / "index.html").read_text(encoding="utf-8")
        intro = page.split("</header>", 1)[1].split("<section>", 1)[0]
        self.assertIn("These wheels are unofficial.", intro)
        self.assertIn("The package's maintainers didn't build them and haven't reviewed them.", intro)
        self.assertIn("a wheelgap maintainer approves a publish run", page)
        self.assertNotIn("after a maintainer approves", page)
        section = page.split('<h2 id="report_a_problem">Problems with a wheel</h2>', 1)[1].split("</section>", 1)[0]
        self.assertIn('<a href="https://github.com/wheelgap/wheels/issues/new?template=broken-wheel.yml">', section)
        self.assertIn('<a href="https://github.com/wheelgap/wheels/security/advisories/new">', section)
        self.assertIn("taken down first and investigated after", section)
        self.assertIn('href="#report_a_problem"', intro)
        self.assertEqual(page.count('id="report_a_problem"'), 1)
        self.assertIn("Content-Security-Policy", page)
        self.assertNotIn("<script", page)

    def test_each_row_in_the_index_links_the_report_form_with_its_file_name(self):
        lz4 = "lz4-4.4.5-cp314-cp314-win_arm64.whl"
        page = (self.build([wheel(), wheel(name="lz4", file=lz4, version="4.4.5")]) / "index.html").read_text()
        for project, file in (("tiktoken", wheel()["wheel"]), ("lz4", lz4)):
            with self.subTest(project):
                row = page.split(f'<tr id="{project}">', 1)[1].split("</tr>", 1)[0]
                self.assertIn(
                    '<br><a href="https://github.com/wheelgap/wheels/issues/new?template=broken-wheel.yml'
                    f'&amp;wheel={file}&amp;title=Broken%20wheel%3A%20{file}" '
                    f'aria-label="report a problem with {file}">report</a></td>',
                    row,
                )
                self.assertEqual(row.count(">report</a>"), 1)
                self.assertNotIn("&wheel=", row)
        labels = re.findall(r'aria-label="(report a problem with [^"]+)"', page)
        self.assertEqual(len(labels), len(set(labels)), labels)
        # Everything but letters, digits and "_.-~" is percent-encoded, so a file name can't add a query parameter.
        self.assertEqual(
            build_gap_index.report_url("a&b=c d+e-1.0.whl"),
            "https://github.com/wheelgap/wheels/issues/new?template=broken-wheel.yml"
            "&wheel=a%26b%3Dc%20d%2Be-1.0.whl&title=Broken%20wheel%3A%20a%26b%3Dc%20d%2Be-1.0.whl",
        )

    def test_withdrawn_wheel_has_no_report_link(self):
        filename = "tiktoken-0.14.0-cp314-cp314-win_arm64.whl"
        page = (self.build([wheel()], {"tiktoken": [pypi_file(filename)]}) / "index.html").read_text()
        row = page.split('<tr id="tiktoken">', 1)[1].split("</tr>", 1)[0]
        self.assertIn("withdrawn: PyPI has its own now", row)
        self.assertNotIn("report", row)
        self.assertNotIn("issues/new", row)

    def test_install_command_ignores_untrusted_version_field(self):
        page = (self.build([wheel(version="1.0; rm -rf ~")]) / "index.html").read_text()
        row = page.split('<tr id="tiktoken">', 1)[1].split("</tr>", 1)[0]
        self.assertIn("tiktoken==0.14.0</code>", row)
        self.assertNotIn("1.0; rm -rf ~", row.split("<code>", 1)[1].split("</code>", 1)[0])
        self.assertNotIn("<script", page)

    def test_install_command_requires_matching_filename_project(self):
        mismatched = wheel(name="tiktoken", file="another-0.14.0-cp314-cp314-win_arm64.whl")
        page = build_gap_index.human_page(build_gap_index.projects([mismatched]))
        row = page.split('<tr id="tiktoken">', 1)[1].split("</tr>", 1)[0]
        self.assertNotIn("pip install", row)

    def test_the_same_file_recorded_twice_keeps_each_projects_state(self):
        # tiktoken sorts before tiktokn, so tiktokn's withdrawal used to overwrite tiktoken's in-index state.
        site = self.build([wheel(), wheel(name="tiktokn")])
        self.assert_status_matches_index(site)
        status = json.loads((site / "data/status.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [(w["project"], w["status"]) for w in status["wheels"]],
            [("tiktoken", "in-index"), ("tiktokn", "withdrawn-release-gone")],
        )
        page = (site / "index.html").read_text(encoding="utf-8")
        row = page.split('<tr id="tiktoken">', 1)[1].split("</tr>", 1)[0]
        self.assertIn("in the index", row)
        self.assertIn("tiktoken==0.14.0</code>", row)
        project = (site / "simple/tiktoken/index.html").read_text(encoding="utf-8")
        self.assertEqual(project.count("releases/download/gap-wheels/tiktoken-0.14.0-cp314-cp314-win_arm64.whl"), 1)

    def test_one_projects_yank_reason_doesnt_leak_into_an_others_page(self):
        # tiktok sorts before tiktoken, so tiktok's yank reason used to mark tiktoken's link data-yanked.
        site = self.build(
            [wheel(name="tiktok"), wheel()],
            {"tiktok": [pypi_file("tiktok-0.14.0-cp314-cp314-win_arm64.whl", yanked="bad build")]},
        )
        self.assert_status_matches_index(site)
        status = json.loads((site / "data/status.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [(w["project"], w["status"], w["yank_reason"]) for w in status["wheels"]],
            [("tiktok", "yanked", "bad build"), ("tiktoken", "in-index", None)],
        )
        project = (site / "simple/tiktoken/index.html").read_text(encoding="utf-8")
        self.assertEqual(project.count("releases/download/gap-wheels/tiktoken-0.14.0-cp314-cp314-win_arm64.whl"), 1)
        self.assertNotIn("data-yanked", project)
        page = (site / "index.html").read_text(encoding="utf-8")
        row = page.split('<tr id="tiktoken">', 1)[1].split("</tr>", 1)[0]
        self.assertIn("in the index", row)
        self.assertIn("tiktoken==0.14.0</code>", row)


    def test_no_file_yet(self):
        tmp = Path(tempfile.mkdtemp())
        self.assertEqual(build_gap_index.build(tmp / "missing.json", tmp / "site", fetch=no_network), 0)
        self.assertIn("No wheels published yet", (tmp / "site/index.html").read_text())

    def test_project_pages_list_pypi_files_too(self):
        pypi = {"tiktoken": [
            pypi_file("tiktoken-0.14.0.tar.gz", **{"core-metadata": False, "data-dist-info-metadata": False}),
            pypi_file("tiktoken-0.14.0-cp314-cp314-win_amd64.whl"),
            pypi_file("tiktoken-0.13.0-cp313-cp313-win_arm64.whl", yanked="broken build", **{"requires-python": None}),
            pypi_file("tiktoken-0.12.0-py3-none-any.whl", yanked=True),
        ]}  # fmt: skip
        site = self.build([wheel()], pypi)
        self.assertEqual(self.fetched, ["tiktoken"])
        page = (site / "simple/tiktoken/index.html").read_text()
        links = [line.strip() for line in page.splitlines() if line.strip().startswith("<a ")]
        self.assertEqual(len(links), 5)  # PyPI's four files and ours, sorted by file name
        self.assertEqual(
            [link.rsplit(">", 2)[-2].removesuffix("</a") for link in links],
            sorted(link.rsplit(">", 2)[-2].removesuffix("</a") for link in links),
        )
        self.assertIn(
            '<a href="https://files.pythonhosted.org/packages/ab/cd/ef/tiktoken-0.14.0-cp314-cp314-win_amd64.whl'
            f'#sha256={PYPI_SHA}" data-requires-python="&gt;=3.9" data-dist-info-metadata="sha256={META_SHA}" '
            f'data-core-metadata="sha256={META_SHA}">tiktoken-0.14.0-cp314-cp314-win_amd64.whl</a>',
            page,
        )
        self.assertIn(
            f'tiktoken-0.14.0.tar.gz#sha256={PYPI_SHA}" data-requires-python="&gt;=3.9">tiktoken-0.14.0.tar.gz</a>', page
        )
        self.assertIn(f'win_arm64.whl#sha256={PYPI_SHA}" data-dist-info-metadata=', page)  # no requires-python
        self.assertIn('data-yanked="broken build"', page)
        self.assertIn('data-yanked="">tiktoken-0.12.0-py3-none-any.whl', page)
        self.assertIn(f"releases/download/gap-wheels/tiktoken-0.14.0-cp314-cp314-win_arm64.whl#sha256={SHA}", page)

    def test_pypi_shapes_it_doesnt_trust_stop_the_build(self):
        bad = [
            pypi_file("tiktoken-0.14.0.tar.gz", url="https://evil.example/tiktoken-0.14.0.tar.gz"),
            pypi_file("tiktoken-0.14.0.tar.gz", url="https://files.pythonhosted.org/packages/x/other.tar.gz"),
            pypi_file("tiktoken-0.14.0.tar.gz", url="https://files.pythonhosted.org/packages/x/tiktoken-0.14.0.tar.gz?a"),
            pypi_file('tiktoken"><script>.tar.gz'),
            pypi_file("tiktoken-0.14.0.tar.gz", hashes={"md5": "x"}),
            pypi_file("tiktoken-0.14.0.tar.gz", yanked=1),
            pypi_file("tiktoken-0.14.0.tar.gz", **{"core-metadata": {"sha256": "short"}}),
            pypi_file("tiktoken-0.14.0.tar.gz", **{"requires-python": 3}),
            "not a file",
        ]
        for f in bad:
            with self.subTest(f=f):
                site = self.build([wheel()], {"tiktoken": [pypi_file("tiktoken-0.13.0.tar.gz"), f]}, expect=1)
                self.assertFalse(site.exists())  # nothing written, so the deploy stops and the old index stays

    def test_a_failed_fetch_stops_the_build(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "gap-wheels.json").write_text(json.dumps({"schema": 1, "wheels": [wheel()]}))

        def fetch(project):
            raise build_gap_index.PyPIError("down")

        self.assertEqual(build_gap_index.build(tmp / "gap-wheels.json", tmp / "site", fetch=fetch), 1)
        self.assertFalse((tmp / "site").exists())

    def test_our_wheel_drops_out_once_pypi_has_one_that_installs_in_its_place(self):
        ours = "tiktoken-0.14.0-cp314-cp314-win_arm64.whl"
        withdrawn = {
            "same file": ours,
            "same tag, build number": "tiktoken-0.14.0-1-cp314-cp314-win_arm64.whl",
            "abi3 from older CPython": "tiktoken-0.14.0-cp39-abi3-win_arm64.whl",
            "pure Python": "tiktoken-0.14.0-py3-none-any.whl",
            "multi-tag": "tiktoken-0.14.0-cp313.cp314-cp313.cp314-win_arm64.whl",
        }
        kept = {
            "other version": "tiktoken-0.15.0-cp314-cp314-win_arm64.whl",
            "other platform": "tiktoken-0.14.0-cp314-cp314-win_amd64.whl",
            "abi3 from newer CPython": "tiktoken-0.14.0-cp315-abi3-win_arm64.whl",
            "other Python": "tiktoken-0.14.0-cp313-cp313-win_arm64.whl",
            "py2 only": "tiktoken-0.14.0-py2-none-any.whl",
            "sdist": "tiktoken-0.14.0.tar.gz",
        }
        for label, other in [*withdrawn.items(), *kept.items()]:
            with self.subTest(label):
                site = self.build([wheel(file=ours)], {"tiktoken": [pypi_file(other)]})
                page = (site / "simple/tiktoken/index.html").read_text()
                human = (site / "index.html").read_text()
                if label in withdrawn:
                    self.assertNotIn("releases/download", page)
                    self.assertIn("withdrawn: PyPI has its own now", human)
                else:
                    self.assertIn(f"releases/download/gap-wheels/{ours}", page)
                    self.assertNotIn("withdrawn: PyPI has its own now", human)

    def test_a_linux_wheel_drops_out_once_pypi_has_one_for_an_older_or_equal_libc(self):
        cases = {
            "lz4-4.4.5-cp314-cp314t-manylinux_2_28_x86_64.whl": (
                ["manylinux_2_28_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64", "manylinux1_x86_64",
                 "manylinux_2_17_x86_64.manylinux2014_x86_64", "manylinux_2_5_x86_64.manylinux1_x86_64",
                 "manylinux_2_17_x86_64.manylinux_2_34_aarch64"],  # one of its tags is enough
                ["manylinux_2_34_x86_64", "manylinux_2_17_aarch64", "musllinux_1_1_x86_64", "linux_x86_64",
                 "manylinux2014_x86_64x", "manylinux_2_34_x86_64.manylinux_2_17_aarch64"],
            ),
            "lz4-4.4.5-cp314-cp314-musllinux_1_2_aarch64.whl": (
                ["musllinux_1_2_aarch64", "musllinux_1_1_aarch64"],
                ["musllinux_1_3_aarch64", "manylinux_2_17_aarch64", "musllinux_1_1_x86_64"],
            ),
        }  # fmt: skip
        for ours, (withdrawn, kept) in cases.items():
            py, abi = ours.split("-")[2:4]
            for plat in withdrawn + kept:
                with self.subTest(ours=ours, theirs=plat):
                    theirs = f"lz4-4.4.5-{py}-{abi}-{plat}.whl"
                    site = self.build([wheel(name="lz4", file=ours, version="4.4.5")], {"lz4": [pypi_file(theirs)]})
                    page = (site / "simple/lz4/index.html").read_text()
                    self.assertEqual("releases/download" not in page, plat in withdrawn)
        # The GIL build doesn't cover 3.14t, however old its glibc.
        ours = "lz4-4.4.5-cp314-cp314t-manylinux_2_28_x86_64.whl"
        site = self.build([wheel(name="lz4", file=ours, version="4.4.5")],
                          {"lz4": [pypi_file("lz4-4.4.5-cp314-cp314-manylinux_2_17_x86_64.whl")]})  # fmt: skip
        self.assertIn(f"gap-wheels/{ours}", (site / "simple/lz4/index.html").read_text())
        self.assertTrue(build_gap_index.covers(("cp39", "abi3", "manylinux_2_17_x86_64"),
                                               ("cp315", "cp315", "manylinux_2_28_x86_64")))  # fmt: skip

    def test_the_page_names_the_platforms(self):
        page = (self.build([wheel()]) / "index.html").read_text()
        self.assertNotIn("Windows only for now", page)
        for text in ("manylinux_2_28", "musllinux_1_2", "auditwheel", "approves a publish run"):
            self.assertIn(text, page)

    def test_free_threaded_wheels_arent_replaced_by_abi3(self):
        ours = "tiktoken-0.14.0-cp314-cp314t-win_arm64.whl"
        site = self.build([wheel(file=ours)], {"tiktoken": [pypi_file("tiktoken-0.14.0-cp39-abi3-win_arm64.whl")]})
        self.assertIn(f"releases/download/gap-wheels/{ours}", (site / "simple/tiktoken/index.html").read_text())
        site = self.build([wheel(file=ours)], {"tiktoken": [pypi_file("tiktoken-0.14.0-py3-none-any.whl")]})
        self.assertNotIn("releases/download", (site / "simple/tiktoken/index.html").read_text())

    def test_the_page_explains_uv(self):
        page = (self.build([wheel()]) / "index.html").read_text()
        self.assertIn("[[tool.uv.index]]", page)
        self.assertIn('url = "https://wheelgap.github.io/wheels/simple/"', page)
        self.assertIn("explicit = true", page)
        self.assertIn('tokenizers = { index = "wheelgap" }', page)

    # Mirrors what `wheelgap --fix` prints next to its link to this page: PIP_CAVEAT, LOCK_CAVEAT, UV_MERGE and
    # text_lines() in wheelgap's src/wheelgap/gapwheels.py (wheelgap/wheelgap). This repo doesn't depend on wheelgap,
    # so the phrases are copied here: change one side and the other needs the same change. The README's opening
    # section says the same four things.
    def test_the_page_says_what_wheelgap_fix_says(self):
        page = (self.build([wheel()]) / "index.html").read_text(encoding="utf-8")
        how = page.split("<h2>How to use it</h2>", 1)[1].split("</section>", 1)[0]
        text = html.unescape(re.sub(r"<[^>]+>", "", how))
        index = "https://wheelgap.github.io/wheels/simple/"
        # 1. A private index (PIP_CAVEAT).
        self.assertIn("pip treats every index alike, so do this only if the packages below come from PyPI: not if a "
                      "private index (set in pip.conf, say) has packages of the same names.", text)
        # 2. Hash-pinned requirements (HASHES_HOW): on the package's existing line, keeping its hashes, never a line of
        # its own; and where the full sha256 is.
        self.assertIn("If your requirements file pins hashes (--require-hashes), add the wheel's hash to the package's "
                      "existing requirement line", text)
        self.assertIn("--hash=sha256:…, with the wheel's full sha256, after the hashes the line has (inside its "
                      "\\-continued block, if it has one), keeping them. On a line of its own, a second requirement "
                      "for the same package, pip won't use it.", text)
        self.assertNotIn("NAME==VERSION --hash=sha256:", text)
        self.assertRegex(how, r'<a href="data/status\.json">status\.json</a>, as the <code>sha256</code> of the row '
                              r"whose <code>wheel</code> is the file name")
        # 3. uv's next steps (text_lines' "then run uv lock", and UV_MERGE).
        after_config = text.split('tokenizers = { index = "wheelgap" }', 1)[1]
        self.assertTrue(after_config.lstrip().startswith("Then run uv lock."), after_config[:80])
        self.assertIn("If pyproject.toml already has a [tool.uv.sources] table, add the package's line to it instead "
                      "of a second one.", text)
        # 4. Other lockfiles (text_lines' sentence, and LOCK_CAVEAT).
        self.assertIn(f"pin each file, so add {index} as an index (for these packages only, if the tool allows it) "
                      "where that lockfile is made, then lock again.", text)
        self.assertIn("An index that isn't limited to these packages has the same risk as with pip: don't add it if "
                      "a private index has packages of the same names.", text)
        # The commands themselves are as they were.
        self.assertIn(f"<pre><code>pip install --extra-index-url {index} PACKAGE</code></pre>", how)

    def test_the_page_leads_with_wheelgap_fix(self):
        # --fix is in wheelgap 0.14.0: the first thing under "How to use it", before the pip paragraph, with the
        # version, its docs, and what it does first (an upgrade to an official release, these wheels as the fallback).
        page = (self.build([wheel()]) / "index.html").read_text(encoding="utf-8")
        how = page.split("<h2>How to use it</h2>", 1)[1].split("</section>", 1)[0]
        text = html.unescape(re.sub(r"<[^>]+>", "", how))
        # @latest: a bare `uvx wheelgap` runs an older wheelgap installed with `uv tool install`, which has no --fix (#56).
        command = "<pre><code>uvx wheelgap@latest --fix requirements.txt</code></pre>"
        self.assertIn(command, how)
        self.assertNotIn("uvx wheelgap --fix", page)
        self.assertLess(how.index(command), how.index("<pre><code>pip install --extra-index-url"))
        self.assertIn("wheelgap 0.14.0 or later", text)
        self.assertIn('<a href="https://wheelgap.github.io/wheelgap/cli/#fix"><code>--fix</code></a>', how)
        self.assertIn("names an upgrade to an official release first, where a newer release has the wheel. Where none "
                      "has, or if you can't upgrade, it says which gaps a wheel here fills", text)
        self.assertIn("Or by hand. With pip, add this index next to PyPI:", text)
        self.assertNotIn("not in a release yet", page)
        self.assertNotIn("on its main branch", page)
        # The README says the same.
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        self.assertLess(readme.index("uvx wheelgap@latest --fix requirements.txt"), readme.index("pip install --extra-index-url"))
        self.assertIn("wheelgap/wheelgap) 0.14.0 or later", readme)
        self.assertNotIn("uvx wheelgap --fix", readme)
        self.assertNotIn("not in a release yet", readme)

    def test_our_wheel_follows_pypi_yanking_its_release(self):
        sdist = pypi_file("tiktoken-0.14.0.tar.gz", yanked="security issue")
        both = [sdist, pypi_file("tiktoken-0.14.0-cp314-cp314-win_amd64.whl", yanked="security issue")]
        page = (self.build([wheel()], {"tiktoken": both}) / "simple/tiktoken/index.html").read_text()
        self.assertIn('data-yanked="security issue">tiktoken-0.14.0-cp314-cp314-win_arm64.whl</a>', page)
        partly = [sdist, pypi_file("tiktoken-0.14.0-cp314-cp314-win_amd64.whl")]  # not the whole release
        page = (self.build([wheel()], {"tiktoken": partly}) / "simple/tiktoken/index.html").read_text()
        self.assertNotIn('data-yanked="security issue">tiktoken-0.14.0-cp314-cp314-win_arm64', page)
        other = [pypi_file("tiktoken-0.13.0.tar.gz", yanked=True)]  # another release's yank
        page = (self.build([wheel()], {"tiktoken": other}) / "simple/tiktoken/index.html").read_text()
        self.assertIn(f'#sha256={SHA}" data-requires-python="&gt;=3.9">tiktoken-0.14.0-cp314', page)

    def test_the_page_shows_a_yanked_wheel_as_yanked_with_pypis_reason(self):
        lz4 = wheel(name="lz4", file="lz4-4.4.5-cp314-cp314-win_arm64.whl", version="4.4.5")

        def rows(reason):
            site = self.build([wheel(), lz4], {"tiktoken": [pypi_file("tiktoken-0.14.0.tar.gz", yanked=reason)]})
            page = (site / "index.html").read_text(encoding="utf-8")
            return [page.split(f'<tr id="{p}">', 1)[1].split("</tr>", 1)[0] for p in ("tiktoken", "lz4")], page

        (yanked, other), page = rows("broken release")
        self.assertTrue(yanked.endswith("<td>in the index, but PyPI has yanked this release: broken release</td>"))
        self.assertIn("tiktoken==0.14.0</code>", yanked)  # the == pin is how pip and uv still take a yanked file
        self.assertIn(">report</a>", yanked)
        self.assertTrue(other.endswith("<td>in the index</td>"), other)
        self.assertIn(" 2 so far.", page)  # a yanked wheel is still in the index
        (yanked, _), page = rows("<b>bad</b>")
        self.assertTrue(yanked.endswith("yanked this release: &lt;b&gt;bad&lt;/b&gt;</td>"), yanked)
        self.assertNotIn("<b>", page)
        for no_reason in (True, ""):
            with self.subTest(no_reason):
                (yanked, other), _ = rows(no_reason)
                self.assertTrue(yanked.endswith("<td>in the index, but PyPI has yanked this release</td>"), yanked)
                self.assertTrue(other.endswith("<td>in the index</td>"), other)

    def test_our_wheel_leaves_when_pypi_no_longer_has_the_release(self):
        site = self.build([wheel()], {"tiktoken": [pypi_file("tiktoken-0.15.0.tar.gz")]}, sdists=False)
        self.assertNotIn("releases/download", (site / "simple/tiktoken/index.html").read_text())
        self.assertIn("withdrawn: the release isn&#x27;t on PyPI", (site / "index.html").read_text())

    def test_omit_failing_deploys_without_the_project_pypi_cant_give(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "gap-wheels.json").write_text(json.dumps({"schema": 1, "wheels": [
            wheel(), wheel(name="lz4", file="lz4-4.4.5-cp314-cp314-win_arm64.whl", version="4.4.5")]}))  # fmt: skip

        def fetch(project):
            if project == "tiktoken":
                raise build_gap_index.PyPIError("404")
            return [pypi_file("lz4-4.4.5.tar.gz")]

        self.assertEqual(build_gap_index.build(tmp / "gap-wheels.json", tmp / "a", fetch=fetch), 1)
        self.assertFalse((tmp / "a").exists())
        self.assertEqual(build_gap_index.build(tmp / "gap-wheels.json", tmp / "b", fetch=fetch, omit_failing=True), 0)
        self.assertEqual(sorted(p.name for p in (tmp / "b/simple").iterdir()), ["index.html", "lz4"])
        self.assertNotIn("tiktoken", (tmp / "b/simple/index.html").read_text())
        human = (tmp / "b/index.html").read_text()
        self.assertIn("left out for now: PyPI couldn&#x27;t be read", human)
        self.assertIn("1 so far", human)

    def test_more_cases_that_keep_our_wheel(self):
        ours_t = "tiktoken-0.14.0-cp314-cp314t-win_arm64.whl"
        site = self.build([wheel(file=ours_t)], {"tiktoken": [pypi_file("tiktoken-0.14.0-cp314-cp314-win_arm64.whl")]})
        self.assertIn(ours_t, (site / "simple/tiktoken/index.html").read_text())  # GIL build doesn't cover 3.14t
        ours = "tiktoken-1.0-cp314-cp314-win_arm64.whl"
        site = self.build([wheel(file=ours, version="1.0")], {"tiktoken": [pypi_file("tiktoken-1.0.tar.gz"),
                          pypi_file("tiktoken-1.0.0-cp314-cp314-win_arm64.whl")]})  # fmt: skip
        self.assertIn(f"gap-wheels/{ours}", (site / "simple/tiktoken/index.html").read_text())  # spelled differently
        site = self.build([wheel()], {"tiktoken": [pypi_file("tiktoken-0.14.0-cp314-abi3-win_arm64.whl")]})
        self.assertNotIn("releases/download", (site / "simple/tiktoken/index.html").read_text())  # same-version abi3


    def test_status_json_is_sorted_and_has_a_stable_schema(self):
        wheels = [
            wheel(name="Zope.Interface", file="zope_interface-8.7-cp314-cp314-win_arm64.whl", version="8.7"),
            wheel(),
        ]
        site = self.build(wheels)
        self.assert_status_matches_index(site)
        status = json.loads((site / "data/status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["schema"], 1)
        self.assertEqual(status["index"], build_gap_index.INDEX_URL)
        self.assertEqual([row["project"] for row in status["wheels"]], ["tiktoken", "zope-interface"])
        self.assertEqual(len(status["wheels"]), 2)
        expected_keys = {"project", "version", "target", "wheel", "sha256", "url", "status", "yank_reason"}
        for row in status["wheels"]:
            self.assertEqual(set(row), expected_keys)
            self.assertEqual(row["sha256"], SHA)
            self.assertEqual(row["url"], build_gap_index.RELEASE_URL + row["wheel"])
            self.assertEqual(row["status"], "in-index")
            self.assertIsNone(row["yank_reason"])
        self.assertIn('href="data/status.json"', (site / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(
            json.loads((site / "data/gap-wheels.json").read_text())["wheels"],
            wheels,
        )

    def test_status_json_yanked_release_preserves_reason_including_empty(self):
        source = "tiktoken-0.14.0.tar.gz"
        for reason, expected in (("broken release", "broken release"), (True, "")):
            with self.subTest(reason=reason):
                site = self.build([wheel()], {"tiktoken": [pypi_file(source, yanked=reason)]})
                self.assert_status_matches_index(site)
                entry = json.loads((site / "data/status.json").read_text())["wheels"][0]
                self.assertEqual(entry["status"], "yanked")
                self.assertEqual(entry["yank_reason"], expected)
                page = (site / "simple/tiktoken/index.html").read_text()
                self.assertIn(f'data-yanked="{expected}"', page)

    def test_status_json_withdrawn_when_pypi_has_matching_wheel(self):
        filename = "tiktoken-0.14.0-cp314-cp314-win_arm64.whl"
        site = self.build([wheel()], {"tiktoken": [pypi_file(filename)]})
        self.assert_status_matches_index(site)
        entry = json.loads((site / "data/status.json").read_text())["wheels"][0]
        self.assertEqual(entry["status"], "withdrawn-pypi-has-wheel")
        self.assertIsNone(entry["yank_reason"])
        self.assertNotIn("releases/download/gap-wheels/", (site / "simple/tiktoken/index.html").read_text())

    def test_status_json_withdrawn_when_release_is_gone(self):
        site = self.build([wheel()], sdists=False)
        self.assert_status_matches_index(site)
        entry = json.loads((site / "data/status.json").read_text())["wheels"][0]
        self.assertEqual(entry["status"], "withdrawn-release-gone")
        self.assertIsNone(entry["yank_reason"])
        self.assertNotIn("releases/download/gap-wheels/", (site / "simple/tiktoken/index.html").read_text())

    def test_status_json_marks_unreadable_pypi_only_for_explicit_omit(self):
        bad = pypi_file("tiktoken-0.14.0.tar.gz", url="https://untrusted.example/sdist.tar.gz")
        site = self.build([wheel()], {"tiktoken": [bad]}, omit_failing=True)
        self.assert_status_matches_index(site)
        entry = json.loads((site / "data/status.json").read_text())["wheels"][0]
        self.assertEqual(entry["status"], "left-out-pypi-unreadable")
        self.assertIsNone(entry["yank_reason"])
        self.assertFalse((site / "simple/tiktoken").exists())
        # The default failure path must not create a partial site or status.json.
        site = self.build([wheel()], {"tiktoken": [bad]}, expect=1)
        self.assertFalse(site.exists())
        self.assertFalse((site / "data/status.json").exists())

    def test_status_json_is_identical_for_identical_inputs(self):
        wheels = [wheel()]
        site = self.build(wheels)
        self.assert_status_matches_index(site)
        a = (site / "data/status.json").read_bytes()
        b = (self.build(wheels) / "data/status.json").read_bytes()
        self.assertEqual(a, b)
        self.assertTrue(a.endswith(b"\n"))
        self.assertNotIn(b"generated_at", a)

    def test_status_json_keeps_yank_reasons_across_projects(self):
        wheels = [wheel(), wheel(name="lz4", file="lz4-4.4.5-cp314-cp314-win_arm64.whl", version="4.4.5")]
        site = self.build(wheels, {"lz4": [pypi_file("lz4-4.4.5.tar.gz", yanked="bad build")]})
        self.assert_status_matches_index(site)
        rows = {row["project"]: row for row in json.loads((site / "data/status.json").read_text())["wheels"]}
        self.assertEqual((rows["lz4"]["status"], rows["lz4"]["yank_reason"]), ("yanked", "bad build"))
        self.assertEqual((rows["tiktoken"]["status"], rows["tiktoken"]["yank_reason"]), ("in-index", None))

    def test_status_json_nulls_version_and_target_that_arent_strings(self):
        odd = wheel(target=3)
        del odd["version"]
        site = self.build([odd])
        self.assert_status_matches_index(site)
        row = json.loads((site / "data/status.json").read_text())["wheels"][0]
        self.assertEqual((row["version"], row["target"], row["status"]), (None, None, "in-index"))

    def test_status_json_omits_entries_the_index_rejects(self):
        site = self.build([wheel(), wheel(name="../unsafe"), wheel(sha256="invalid")])
        self.assert_status_matches_index(site)
        rows = json.loads((site / "data/status.json").read_text())["wheels"]
        self.assertEqual([row["wheel"] for row in rows], [wheel()["wheel"]])

    ATOM = {"a": "http://www.w3.org/2005/Atom"}

    def feed(self, site):
        """feed.xml parsed, after the checks every feed must pass: Atom's required elements, RFC 3339 dates and
        unique entry ids."""
        root = ElementTree.fromstring((site / "feed.xml").read_bytes())
        self.assertEqual(root.tag, "{http://www.w3.org/2005/Atom}feed")
        date = re.compile(r"\d{4}-\d{2}-\d{2}T00:00:00Z")
        entries = root.findall("a:entry", self.ATOM)
        for node in [root, *entries]:
            for required in ("id", "title", "updated"):
                self.assertEqual(len(node.findall(f"a:{required}", self.ATOM)), 1, required)
            self.assertRegex(node.findtext("a:updated", namespaces=self.ATOM), date)
        ids = [entry.findtext("a:id", namespaces=self.ATOM) for entry in entries]
        self.assertEqual(len(ids), len(set(ids)), ids)
        return root, entries

    def test_feed_lists_the_wheels_in_the_index_newest_first(self):
        lz4 = "lz4-4.4.5-cp314-cp314-win_arm64.whl"
        gone = "regex-1.0-cp314-cp314-win_arm64.whl"
        wheels = [
            wheel(published="2026-11-16"),
            wheel(name="lz4", file=lz4, version="4.4.5", published="2026-11-17"),
            wheel(name="regex", file=gone, version="1.0", published="2026-11-18"),  # PyPI has its own now
            wheel(name="zstd", file="zstd-1.0-cp314-cp314-win_arm64.whl", version="1.0", published="2026-11-19"),
            wheel(name="Zope.Interface", file="zope_interface-8.7-cp314-cp314-win_arm64.whl", version="8.7",
                  published="2026-11-17"),
        ]  # fmt: skip
        pypi = {"regex": [pypi_file(gone)], "zstd": [pypi_file("zstd-1.0.tar.gz", yanked="bad build")]}
        site = self.build(wheels, pypi)
        root, entries = self.feed(site)
        text = lambda node, tag: node.findtext(f"a:{tag}", namespaces=self.ATOM)
        self.assertEqual(text(root, "id"), "https://wheelgap.github.io/wheels/feed.xml")
        self.assertEqual(text(root, "title"), "wheelgap gap wheels")
        self.assertEqual(root.findtext("a:author/a:name", namespaces=self.ATOM), "wheelgap")
        self.assertIn("Unofficial", text(root, "subtitle"))
        self.assertEqual(root.find("a:link[@rel='self']", self.ATOM).get("href"), "https://wheelgap.github.io/wheels/feed.xml")
        self.assertEqual(text(root, "updated"), "2026-11-19T00:00:00Z")  # the newest entry's
        # Newest first, then by project and file name, reversed. The yanked one is in; the withdrawn one isn't.
        release = "https://github.com/wheelgap/wheels/releases/download/gap-wheels/"
        self.assertEqual(
            [text(entry, "id") for entry in entries],
            [release + name for name in ("zstd-1.0-cp314-cp314-win_arm64.whl",
                                         "zope_interface-8.7-cp314-cp314-win_arm64.whl", lz4, wheel()["wheel"])],
        )
        entry = entries[2]
        self.assertEqual(text(entry, "title"), "lz4 4.4.5 for 3.14 win-arm64")
        self.assertEqual(entry.find("a:link", self.ATOM).get("href"), "https://wheelgap.github.io/wheels/#lz4")
        self.assertEqual(text(entry, "updated"), "2026-11-17T00:00:00Z")
        summary = text(entry, "summary")
        self.assertIn("pip install --extra-index-url https://wheelgap.github.io/wheels/simple/ lz4==4.4.5", summary)
        self.assertIn(f"sha256: {SHA}", summary)
        self.assertIn("Unofficial wheel, built by wheelgap from the PyPI sdist", summary)
        # The page links the feed, in its head and next to status.json, and each entry's link is a row's anchor.
        page = (site / "index.html").read_text(encoding="utf-8")
        head = page.split("</head>", 1)[0]
        self.assertIn('<link rel="alternate" type="application/atom+xml" href="feed.xml" title="New gap wheels">', head)
        self.assertIn('<a href="feed.xml">Atom feed</a>', page.split("</head>", 1)[1])
        self.assertIn('<tr id="lz4">', page)
        # Left out with --omit-failing: not in the feed either.
        bad = pypi_file("lz4-4.4.5.tar.gz", url="https://untrusted.example/sdist.tar.gz")
        _, entries = self.feed(self.build(wheels[:2], {"lz4": [bad]}, omit_failing=True))
        self.assertEqual([text(entry, "id") for entry in entries], [release + wheel()["wheel"]])

    def test_feed_is_capped_at_50_entries(self):
        wheels = [
            wheel(name=f"pkg{n}", file=f"pkg{n}-1.0-cp314-cp314-win_arm64.whl", published=f"2026-10-{n % 28 + 1:02d}")
            for n in range(60)
        ]
        root, entries = self.feed(self.build(wheels))
        self.assertEqual(len(entries), 50)
        days = [entry.findtext("a:updated", namespaces=self.ATOM) for entry in entries]
        self.assertEqual(days, sorted(days, reverse=True))
        self.assertEqual(days[0], "2026-10-28T00:00:00Z")
        self.assertEqual(root.findtext("a:updated", namespaces=self.ATOM), days[0])
        self.assertGreater(days[-1], "2026-10-03T00:00:00Z")  # the ten oldest are the ones dropped

    def test_feed_skips_an_entry_without_a_valid_published_date(self):
        names = ("none", "day", "notpadded", "number", "time", "markup", "trailing")
        values = (None, "2026-02-30", "2026-1-5", 20261116, "2026-11-16T00:00:00Z", "<x>", "2026-11-16\n")
        wheels = [wheel(published="2026-11-16")]
        for name, value in zip(names, values, strict=True):
            w = wheel(name=name, file=f"{name}-1.0-cp314-cp314-win_arm64.whl")
            if value is not None:
                w["published"] = value
            wheels.append(w)
        site = self.build(wheels)
        _, entries = self.feed(site)
        self.assertEqual([entry.findtext("a:title", namespaces=self.ATOM) for entry in entries],
                         ["tiktoken 0.14.0 for 3.14 win-arm64"])
        # They are still in the index and on the page: only the feed needs the date.
        self.assertEqual(len(json.loads((site / "data/status.json").read_text())["wheels"]), 8)

    def test_feed_escapes_free_text(self):
        odd = wheel(target='<x>&"', version="1.0]]>\x00\x0b<!--", published="2026-11-16")
        site = self.build([odd, wheel(name="lz4", file="lz4-4.4.5-cp314-cp314-win_arm64.whl", version=None, target=3,
                                      published="2026-11-15")])  # fmt: skip
        raw = (site / "feed.xml").read_text(encoding="utf-8")
        self.assertIn("for &lt;x&gt;&amp;&quot;</title>", raw)
        self.assertNotIn("<x>", raw)
        self.assertNotIn("\x00", raw)
        _, entries = self.feed(site)
        # Control characters XML can't carry are dropped; everything else comes back as it was written.
        self.assertEqual(entries[0].findtext("a:title", namespaces=self.ATOM), 'tiktoken 1.0]]><!-- for <x>&"')
        # A version or target that isn't a string: the title is the file name.
        self.assertEqual(entries[1].findtext("a:title", namespaces=self.ATOM), "lz4-4.4.5-cp314-cp314-win_arm64.whl")

    def test_feed_updated_comes_from_the_data_never_the_clock(self):
        gone = "regex-1.0-cp314-cp314-win_arm64.whl"
        withdrawn = wheel(name="regex", file=gone, version="1.0", published="2026-11-18")
        updated = lambda site: self.feed(site)[0].findtext("a:updated", namespaces=self.ATOM)
        # No entries: the newest `published` of any record, or the epoch when there is none.
        site = self.build([withdrawn, wheel(name="lz4", file="lz4-1.0-cp314-cp314-win_arm64.whl", published="2026-11-01"),
                           wheel()], {"regex": [pypi_file(gone)], "lz4": [pypi_file("lz4-1.0-cp314-cp314-win_arm64.whl")]})  # fmt: skip
        self.assertEqual(updated(site), "2026-11-18T00:00:00Z")
        self.assertEqual(self.feed(site)[1], [])
        self.assertEqual(updated(self.build([wheel()])), "1970-01-01T00:00:00Z")
        self.assertEqual(updated(self.build([])), "1970-01-01T00:00:00Z")
        # With entries: the newest entry's, not a newer withdrawn record's.
        site = self.build([withdrawn, wheel(published="2026-11-16")], {"regex": [pypi_file(gone)]})
        self.assertEqual(updated(site), "2026-11-16T00:00:00Z")

    def test_feed_is_identical_for_identical_inputs(self):
        wheels = [wheel(published="2026-11-16"),
                  wheel(name="lz4", file="lz4-4.4.5-cp314-cp314-win_arm64.whl", version="4.4.5", published="2026-11-16")]
        a = (self.build(wheels) / "feed.xml").read_bytes()
        b = (self.build(list(reversed(wheels))) / "feed.xml").read_bytes()
        self.assertEqual(a, b)
        self.assertTrue(a.startswith(b'<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">\n'))
        self.assertTrue(a.endswith(b"</feed>\n"))

    def test_feed_lists_a_file_recorded_twice_once(self):
        _, entries = self.feed(self.build([wheel(published="2026-11-16"), wheel(published="2026-11-17")]))
        self.assertEqual(len(entries), 1)

    def test_a_failed_fetch_writes_no_feed(self):
        bad = pypi_file("tiktoken-0.14.0.tar.gz", url="https://untrusted.example/sdist.tar.gz")
        site = self.build([wheel(published="2026-11-16")], {"tiktoken": [bad]}, expect=1)
        self.assertFalse((site / "feed.xml").exists())
        self.assertFalse(site.exists())

    def test_current_published_data_makes_a_feed_of_every_entry_or_the_newest_50(self):
        path = Path(__file__).resolve().parents[1] / "data" / "gap-wheels.json"
        published = json.loads(path.read_text(encoding="utf-8"))["wheels"]
        by_project = build_gap_index.projects(published)
        status = {(project, w["wheel"]): "in-index" for project, wheels in by_project.items() for w in wheels}
        root = ElementTree.fromstring(build_gap_index.feed(by_project, status))
        entries = root.findall("a:entry", self.ATOM)
        self.assertEqual(len(entries), min(len(published), 50))
        self.assertEqual(root.findtext("a:updated", namespaces=self.ATOM),
                         max(w["published"] for w in published) + "T00:00:00Z")


class Requests(Index):
    LGBM = "lightgbm-4.7.0-cp314-cp314-win_arm64.whl"

    def test_a_requested_package_names_who_asked_and_has_an_anchor(self):
        wheels = [wheel(name="lightgbm", file=self.LGBM, version="4.7.0"),
                  wheel(name="lightgbm", file="lightgbm-4.7.0-cp314-cp314t-win_arm64.whl", version="4.7.0",
                        target="3.14t win-arm64"),
                  wheel()]  # fmt: skip
        asked = {"LightGBM": {"by": "u/TumbleweedNo8956", "url": "https://www.reddit.com/user/TumbleweedNo8956/"}}
        page = (self.build(wheels, requests=asked) / "index.html").read_text()
        self.assertIn('<tr id="lightgbm">', page)
        self.assertEqual(page.count('id="lightgbm"'), 1)  # one anchor per package, on its first row
        self.assertIn('<tr id="tiktoken">', page)
        self.assertEqual(page.count(
            'Built at the request of <a href="https://www.reddit.com/user/TumbleweedNo8956/" rel="nofollow ugc">'
            "u/TumbleweedNo8956</a>"), 2)
        self.assertNotIn("Built at the request of", page.split('<tr id="tiktoken">')[1])

    def test_requests_it_doesnt_trust_are_left_out(self):
        for bad in ({"lightgbm": {"by": "<b>x</b>", "url": "https://github.com/x"}},
                    {"lightgbm": {"by": "x", "url": "https://evil.example/x"}},
                    {"lightgbm": {"by": "x", "url": "javascript:alert(1)"}},
                    {"lightgbm": {"by": "x"}}, {"lightgbm": "x"}, ["lightgbm"], "nonsense"):  # fmt: skip
            with self.subTest(bad=bad):
                page = (self.build([wheel(name="lightgbm", file=self.LGBM, version="4.7.0")], requests=bad)
                        / "index.html").read_text()
                self.assertNotIn("Built at the request of", page)
        tmp = Path(tempfile.mkdtemp())
        (tmp / "r.json").write_text("{not json")
        self.assertEqual(build_gap_index.read_requests(tmp / "r.json"), {})
        self.assertEqual(build_gap_index.read_requests(tmp / "missing.json"), {})

    def test_a_py3_none_wheel_for_the_platform_replaces_ours(self):
        ours = ("cp314", "cp314", "win_arm64")
        self.assertTrue(build_gap_index.covers(("py3", "none", "win_arm64"), ours))
        self.assertTrue(build_gap_index.covers(("py3", "none", "win_arm64"), ("cp314", "cp314t", "win_arm64")))
        self.assertFalse(build_gap_index.covers(("py3", "none", "win_amd64"), ours))
        self.assertFalse(build_gap_index.covers(("py2", "none", "win_arm64"), ours))
        self.assertTrue(build_gap_index.covers(("py3", "none", "manylinux_2_17_x86_64"),
                                               ("cp314", "cp314t", "manylinux_2_28_x86_64")))  # fmt: skip


class FetchPyPI(unittest.TestCase):
    def answer(self, body=b'{"meta": {"api-version": "1.4"}, "files": []}', ctype=build_gap_index.SIMPLE_JSON,
               url="https://pypi.org/simple/tiktoken/"):  # fmt: skip
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = url
        response.headers.get_content_type.return_value = ctype
        response.read.return_value = body
        return response

    def fetch(self, *answers):
        with mock.patch.object(build_gap_index.urllib.request, "urlopen", side_effect=list(answers)) as urlopen, \
             mock.patch.object(build_gap_index.time, "sleep"):  # fmt: skip
            try:
                return build_gap_index.fetch_pypi("tiktoken"), urlopen
            except build_gap_index.PyPIError as exc:
                return exc, urlopen

    def test_reads_the_files(self):
        got, urlopen = self.fetch(self.answer(b'{"meta": {"api-version": "1.4"}, "files": [{"filename": "x"}]}'))
        self.assertEqual(got, [{"filename": "x"}])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://pypi.org/simple/tiktoken/")
        self.assertEqual(request.get_header("Accept"), build_gap_index.SIMPLE_JSON)

    def test_refuses_what_it_doesnt_trust(self):
        for label, answer in {
            "redirect": self.answer(url="https://evil.example/simple/tiktoken/"),
            "content type": self.answer(ctype="text/html"),
            "api version": self.answer(b'{"meta": {"api-version": "2.0"}, "files": []}'),
            "no files": self.answer(b'{"meta": {"api-version": "1.4"}}'),
        }.items():
            with self.subTest(label):
                got, _ = self.fetch(answer)
                self.assertIsInstance(got, build_gap_index.PyPIError)

    def test_retries_then_gives_up(self):
        got, urlopen = self.fetch(OSError("down"), http.client.IncompleteRead(b""), self.answer(b"{not json"))
        self.assertIsInstance(got, build_gap_index.PyPIError)
        self.assertEqual(urlopen.call_count, 3)
        got, urlopen = self.fetch(OSError("down"), self.answer())
        self.assertEqual(got, [])
