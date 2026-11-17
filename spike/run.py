"""Spike (not for main): can a LightGBM win_arm64 build be made publishable, reproducibly?

Builds lightgbm's sdist the way gap_wheels.py does, then bundles the VC++ runtime DLLs it needs with a pinned
delvewheel (names mangled), retags py3-none-win_arm64 to the exact cp314 tag with a pinned `wheel`, and reports each
step's sha256, the wheel's DLL check, and whether it installs and trains with the bundled DLLs actually loaded.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import gap_wheels

NAME, VERSION = sys.argv[1], sys.argv[2]
PY_TAG, ABI_TAG, PLAT = sys.argv[3], sys.argv[4], sys.argv[5]  # e.g. cp314 cp314 win_arm64
TOOLS = ("delvewheel==1.13.2", "wheel==0.48.0")
work = Path(os.environ["RUNNER_TEMP"]) / "spike"
report: dict = {}


def h(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    kw.setdefault("check", False)
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", **kw)  # noqa: PLW1510
    print((r.stdout + r.stderr)[-6000:], flush=True)
    return r


fields = gap_wheels.run_build(NAME, VERSION, 2, work / "build", keep=work / "w0")
report["build"] = {k: fields[k] for k in ("sdist_sha256", "wheel", "sha256")}
built = work / "w0" / fields["wheel"]

tools = work / "tools"
run([sys.executable, "-m", "venv", str(tools)], check=True)
tpy = str(tools / "Scripts" / "python.exe")
run([tpy, "-m", "pip", "install", "--no-cache-dir", "--only-binary", ":all:", *TOOLS], check=True)
env = gap_wheels.build_env()
report["missing_before"] = gap_wheels.missing_dlls(built.read_bytes())
run([tpy, "-m", "delvewheel", "show", "--analyze-existing", str(built)], env=env)
r = run([tpy, "-m", "delvewheel", "repair", "--analyze-existing", "-w", str(work / "w1"), str(built)], env=env)
repaired = sorted((work / "w1").glob("*.whl"))
report["delvewheel_exit"] = r.returncode
if len(repaired) == 1:
    report["repaired"] = {"wheel": repaired[0].name, "sha256": h(repaired[0])}
    r = run([tpy, "-m", "wheel", "tags", "--python-tag", PY_TAG, "--abi-tag", ABI_TAG, "--platform-tag", PLAT,
             "--remove", str(repaired[0])], env=env)  # fmt: skip
    final = sorted((work / "w1").glob("*.whl"))
    report["retag_exit"] = r.returncode
    if len(final) == 1:
        f = final[0]
        report["final"] = {"wheel": f.name, "sha256": h(f)}
        with zipfile.ZipFile(f) as z:
            report["members"] = sorted(n for n in z.namelist() if n.lower().endswith((".dll", ".pyd", "__init__.py")))
            report["zip_dates"] = sorted({str(i.date_time) for i in z.infolist()})
        report["missing_after"] = gap_wheels.missing_dlls(f.read_bytes())
        venv = work / "fresh"
        run([sys.executable, "-m", "venv", str(venv)], check=True)
        vpy = str(venv / "Scripts" / "python.exe")
        i = run([vpy, "-m", "pip", "install", "--no-cache-dir", "--only-binary", ":all:", str(f)])
        report["installs"] = i.returncode == 0
        probe = (
            "import ctypes, numpy as np, lightgbm as lgb\n"
            "X = np.random.default_rng(0).random((500, 5)); y = X @ np.arange(5)\n"
            "b = lgb.train({'objective': 'regression', 'verbose': -1, 'num_threads': 4}, lgb.Dataset(X, y), 10)\n"
            "k = ctypes.WinDLL('kernel32'); k.GetModuleHandleW.restype = ctypes.c_void_p\n"
            "import sys, os\n"
            "loaded = {n: bool(k.GetModuleHandleW(n)) for n in ('vcomp140.dll', 'msvcp140.dll')}\n"
            "libs = [p for p in os.listdir(os.path.join(os.path.dirname(lgb.__file__), '..', 'lightgbm.libs'))] "
            "if os.path.isdir(os.path.join(os.path.dirname(lgb.__file__), '..', 'lightgbm.libs')) else []\n"
            "mangled = {n: bool(k.GetModuleHandleW(n)) for n in libs}\n"
            "print('PROBE', lgb.__version__, round(float(b.predict(X[:1])[0]), 3), loaded, mangled)\n"
        )
        p = run([vpy, "-c", probe])
        report["probe"] = [ln for ln in p.stdout.splitlines() if ln.startswith("PROBE")]
        report["probe_exit"] = p.returncode

print("REPORT " + json.dumps(report, indent=1))
Path("report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
