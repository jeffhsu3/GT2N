"""Combine per-flavor split-gate verification into one checks.json.

Reads <dir>/<vt>/checks.json written by CPCell's examples/gt2n_split_gate/verify.py,
re-runs the released-library compatibility checks from ../verify.py on each
flavor's cell.gds and cell.lef, and writes <dir>/checks.json.

    python3 collect_checks.py w31_126x144
"""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import klayout.db as db

ROOT = Path(__file__).resolve().parent
FLAVORS = ("lvt", "elvt", "ulvt", "svt", "hvt")


def main(argv):
    out = ROOT / argv[1]
    checker = ROOT.parent / "verify.py"
    spec = importlib.util.spec_from_file_location("decapcc_verify", checker)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    first = json.loads((out / FLAVORS[0] / "solution.json").read_text())
    width, height = first["width_nm"], first["height_nm"]
    report = {
        "status": "running",
        "footprint_nm": [width, height],
        "area_um2": width * height / 1e6,
        "width_reduction_from_168_percent": round(100 * (1 - width / 168)),
        "arrangement": "staggered adjacent active N/P columns; partial dummy markers; no middle GCUT",
        "scope": "KLayout DRC and released-CDL LVS; standalone, 3x3 mirrored and mixed inverter rows; no ICV",
        "flavors": {},
    }
    for vt in FLAVORS:
        folder = out / vt
        checks = json.loads((folder / "checks.json").read_text())
        if checks["status"] != "passed":
            raise ValueError(f"{vt}: verification did not pass")
        layout = db.Layout()
        layout.read(str(folder / "cell.gds"))
        name = json.loads((folder / "solution.json").read_text())["design"]["name"]
        report["flavors"][vt] = {
            "checks_sha256": hashlib.sha256((folder / "checks.json").read_bytes()).hexdigest(),
            "library_compatibility": module.library_compatibility(
                layout, layout.cell(name), folder / "cell.lef"
            ),
            **{stem: checks[stem] for stem in ("cell", "array", "mixed", "rowend")},
        }
    report["status"] = "passed"
    report["compatibility_checker_sha256"] = hashlib.sha256(checker.read_bytes()).hexdigest()
    (out / "checks.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({vt: f["library_compatibility"] for vt, f in report["flavors"].items()}, indent=1))


if __name__ == "__main__":
    main(sys.argv)
