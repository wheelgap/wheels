import http.client
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


def no_network(project):
    raise AssertionError(f"a test fetched {project} from PyPI")


class Index(unittest.TestCase):
    def build(self, wheels, pypi=None, expect=0, sdists=True, **kw):
        """Build with a fake PyPI. Unless `sdists` is false, PyPI also lists each wheel's release (its sdist), as it
        does for every release we build from."""
        tmp = Path(tempfile.mkdtemp())
        (tmp / "gap-wheels.json").write_text(json.dumps({"schema": 1, "wheels": wheels}))
        self.fetched = []

        def fetch(project):
            self.fetched.append(project)
            files = list((pypi or {}).get(project, []))
            if sdists:
                for w in wheels:
                    if build_gap_index.canonical(w["name"]) == project:
                        name, version = w["wheel"].split("-")[:2]
                        if not any(f.get("filename") == f"{name}-{version}.tar.gz" for f in files if isinstance(f, dict)):
                            files.append(pypi_file(f"{name}-{version}.tar.gz"))
            return files

        got = build_gap_index.build(tmp / "gap-wheels.json", tmp / "site", fetch=fetch, **kw)
        self.assertEqual(got, expect)
        return tmp / "site"

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
