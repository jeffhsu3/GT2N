"""Full KLayout DRC/LVS against the released cross-coupled decap schematic.

The DRC snapshot adds PR #23's VG/ACT overlap guard to the existing platform
deck. GATE.2 is accepted only for the two vertical edges of the gate array;
every other marker, including any extra GATE.2 marker, fails verification.
"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

import klayout.db as db
import klayout.rdb as rdb


def library_compatibility(layout, cell, lef):
    """Check released-library conventions absent from the DRC deck."""
    scale = layout.dbu * 1000

    def region(layer):
        return db.Region(cell.begin_shapes_rec(layout.layer(layer, 0))).merged()

    vbpr, bpr, sdcon = region(9), region(8), region(10)
    if not (vbpr - bpr).is_empty() or not (vbpr - sdcon).is_empty():
        raise ValueError("VBPR must be fully contained in BPR and source SDCON")
    # Every rail contact sits on the released VBPR rows; a cell may tie more
    # than one diffusion per rail (the staggered decap ties four).
    vbpr_y = sorted(
        {
            (round(p.bbox().bottom * scale, 9), round(p.bbox().top * scale, 9))
            for p in vbpr.each()
        }
    )
    if vbpr_y != [(5, 16), (128, 139)]:
        raise ValueError(f"Unexpected VBPR vertical positions: {vbpr_y}")
    vbpr_y = [list(pair) for pair in vbpr_y]
    m0_centers = []
    for polygon in region(20).each():
        box = polygon.bbox()
        center = round((box.bottom + box.top) * scale / 2, 9)
        if (
            polygon.area() != box.area()
            or round(box.height() * scale, 9) != 12
            or center not in (24, 48, 72, 96, 120)
        ):
            raise ValueError(f"M0 polygon is not on a released 24 nm track: {box}")
        m0_centers.append(center)
    if not m0_centers:
        raise ValueError("Missing M0 routes")
    classes = re.findall(r"^\s*CLASS\s+([^;]+);", lef.read_text(), re.M)
    if [c.strip() for c in classes] != ["CORE"]:
        raise ValueError(f"LEF must declare CLASS CORE: {classes}")
    return {
        "vbpr_contained_in_bpr_and_sdcon": True,
        "vbpr_y_nm": vbpr_y,
        "m0_track_centers_nm": sorted(set(m0_centers)),
        "lef_class": "CORE",
    }


def subcircuit(pdk, name, vt):
    text = (pdk / f"cdl/with4nodes/gt2_6t_w31_{vt}.cdl").read_text()
    match = re.search(
        rf"^\.subckt {re.escape(name)} .*?^\.ends {re.escape(name)}\s*$",
        text,
        re.I | re.M | re.S,
    )
    if match is None:
        raise ValueError(f"Missing released subcircuit {name}")
    return match[0] + "\n"


def abutment(layout, base, folder, pdk, vt, mixed):
    native = db.Layout()
    native.dbu = layout.dbu
    copied = native.create_cell(base.name)
    copied.copy_tree(base)
    refname = f"gt2_6t_decapcc_w31_{vt}"
    cdl = subcircuit(pdk, refname, vt).replace(refname, base.name)
    w, h = 336, 288
    stem = "mixed" if mixed else "abutment"
    top = native.create_cell(f"decapcc_{stem}_{vt}")
    row = [(copied, w, ())] * 3
    if mixed:
        ref = db.Layout()
        ref.read(str(pdk / f"gds/gt2_6t_std_cells_w31_{vt}.gds"))
        invname = f"gt2_6t_inv_x1_w31_{vt}"
        inv = native.create_cell(invname)
        inv.copy_tree(ref.cell(invname))
        invw = db.Region(inv.shapes(native.layer(235, 250))).bbox().width()
        row = [(inv, invw, ("A", "Y")), (copied, w, ()), (inv, invw, ("A", "Y"))]
        cdl += subcircuit(pdk, invname, vt)
    ports, instances = [], []
    for y in range(3):
        xoffset = 0
        for x, (master, cellw, signals) in enumerate(row):
            mx, my = x % 2, y % 2
            trans = db.Trans(2 * mx, bool(mx != my), xoffset + mx * cellw, (y + my) * h)
            top.insert(db.CellInstArray(master.cell_index(), trans))
            pins = [f"{pin}_{x}_{y}" for pin in signals]
            for pin, net in zip(signals, pins):
                found = False
                for index in native.layer_indexes():
                    info = native.get_info(index)
                    if info.datatype != 251 or info.layer not in (20, 25, 30):
                        continue
                    for shape in master.shapes(index).each():
                        if shape.is_text() and shape.text.string == pin:
                            position = shape.text.trans.disp
                            point = trans * db.Point(position.x, position.y)
                            top.shapes(index).insert(
                                db.Text(net, db.Trans(point.x, point.y))
                            )
                            found = True
                            break
                    if found:
                        break
                if not found:
                    raise ValueError(f"Missing released inverter pin {pin}")
            ports.extend(pins)
            instances.append(f"X{x}{y} {' '.join(pins)} vdd vss {master.name}")
            xoffset += cellw
    cdl += f".SUBCKT {top.name} {' '.join(ports)} vdd vss\n"
    cdl += "\n".join(instances) + f"\n.ENDS {top.name}\n"
    native.write(str(folder / f"{stem}.gds"))
    (folder / f"{stem}.cdl").write_text(cdl)
    return top.name


def run(deck, folder, stem, top, lvs=False):
    report = folder / (stem + (".lvsdb" if lvs else ".lyrdb"))
    args = [
        "klayout",
        "-b",
        "-r",
        str(deck),
        "-rd",
        f"in_gds={folder / (stem + '.gds')}",
        "-rd",
        f"topcell={top}",
        "-rd",
        f"report_file={report}",
    ]
    if lvs:
        cdl = folder / ("reference.cdl" if stem == "cell" else stem + ".cdl")
        args += [
            "-rd",
            f"cdl_file={cdl}",
            "-rd",
            f"target_netlist={folder / (stem + '_extracted.cdl')}",
        ]
    else:
        # SDCON.2 is DIFFERENT_NET in ICV; mirrored inverter supply contacts
        # are allowed within 20 nm because they join the same buried rail.
        args += ["-rd", "drc_mode=full", "-rd", "drc_use_nets=1"]
    log = report.with_suffix(report.suffix + ".log")
    with log.open("w") as f:
        subprocess.run(
            args, stdout=f, stderr=subprocess.STDOUT, check=True, timeout=120
        )
    if lvs:
        data = db.LayoutVsSchematic()
        data.read(str(report))
        matches = {
            pair.first().name: pair.status() == db.NetlistCrossReference.Match
            for pair in data.xref().each_circuit_pair()
            if pair.first() is not None
        }
        if top not in matches or not all(matches.values()):
            raise ValueError(f"LVS mismatch: {matches}")
        return matches
    counts = dict(Counter(re.findall(r"<category>'([^'<]+)'", report.read_text())))
    if counts != {"GATE.2": 2}:
        raise ValueError(f"Unexpected DRC on {top}: {counts}")
    # Do not blanket-waive a category: confirm both markers are the known
    # vertical outside edges, not internal pitch errors.
    database = rdb.ReportDatabase()
    database.load(str(report))
    layout = db.Layout()
    layout.read(str(folder / (stem + ".gds")))
    gate = (
        db.Region(layout.cell(top).begin_shapes_rec(layout.layer(3, 0)))
        .bbox()
        .to_dtype(layout.dbu)
    )
    outside = {round(gate.left, 9), round(gate.right, 9)}
    observed = set()
    for item in database.each_item():
        for value in item.each_value():
            if not value.is_edge():
                raise ValueError("Expected GATE.2 outside-edge marker")
            edge = value.edge()
            if abs(edge.p1.x - edge.p2.x) > 1e-9 or round(edge.p1.x, 9) not in outside:
                raise ValueError(f"GATE.2 marker is not on an outer gate edge: {edge}")
            observed.add(round(edge.p1.x, 9))
    if observed != outside:
        raise ValueError("Expected one GATE.2 marker at each outside edge")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--pdk-root", type=Path, default=Path(__file__).resolve().parents[3]
    )
    args = parser.parse_args()
    out, pdk = args.output_dir.resolve(), args.pdk_root.resolve()
    (out / "checks.json").unlink(missing_ok=True)
    decks = out / "decks"
    decks.mkdir(exist_ok=True)
    sources = pdk / "experimental/cpcell/w31_lvt/verification/decks"
    drc = (sources / "gt2n.lydrc").read_text()
    needle = '  vg.separation(act, 6.nm, euclidian).with_angle(0).output("VG.ACT.1", "VG.ACT.1 : VG and ACT vertical spacing >= 6 nm")'
    if drc.count(needle) != 1:
        raise ValueError("Cannot locate VG.ACT.1 in source DRC deck")
    drc = drc.replace(
        needle,
        needle
        + '\n  vg.interacting(act).output("VG.ACT.1a", "VG must not overlap or touch ACT")',
    )
    (decks / "gt2n_overlap.lydrc").write_text(drc)
    shutil.copyfile(sources / "gt2n_transistor.lylvs", decks / "gt2n_transistor.lylvs")
    solution = json.loads((out / "solution.json").read_text())
    report = {
        "status": "running",
        "scope": "KLayout full DRC and transistor LVS; no ICV or extracted RC",
        "drc_options": {"drc_mode": "full", "drc_use_nets": 1},
        "footprint_nm": [solution["width_nm"], solution["height_nm"]],
        "flavors": {},
        "library_compatibility": {},
    }
    jobs = []
    for vt in solution["flavors"]:
        folder = out / vt
        layout = db.Layout()
        layout.read(str(folder / "cell.gds"))
        base = layout.top_cell()
        report["library_compatibility"][vt] = library_compatibility(
            layout, base, folder / "cell.lef"
        )
        refname = f"gt2_6t_decapcc_w31_{vt}"
        (folder / "reference.cdl").write_text(
            subcircuit(pdk, refname, vt).replace(refname, base.name)
        )
        names = {"cell": base.name}
        for mixed in (False, True):
            names["mixed" if mixed else "abutment"] = abutment(
                layout, base, folder, pdk, vt, mixed
            )
        report["flavors"][vt] = {stem: {} for stem in names}
        for stem, top in names.items():
            for lvs in (False, True):
                jobs.append((vt, stem, lvs, folder, top))
    if not jobs:
        raise ValueError("No generated decap flavors to verify")

    def check(job):
        vt, stem, lvs, folder, top = job
        deck = decks / ("gt2n_transistor.lylvs" if lvs else "gt2n_overlap.lydrc")
        return vt, stem, "lvs" if lvs else "drc", run(deck, folder, stem, top, lvs)

    with ThreadPoolExecutor(max_workers=3) as executor:
        for vt, stem, kind, result in executor.map(check, jobs):
            report["flavors"][vt][stem][kind] = result
            print(vt, stem, kind, result, flush=True)
    report["status"] = "passed"
    inputs = [
        *decks.iterdir(),
        Path(__file__),
        Path(__file__).with_name("reproduce.py"),
    ]
    inputs += [pdk / f"cdl/with4nodes/gt2_6t_w31_{vt}.cdl" for vt in report["flavors"]]
    report["source_sha256"] = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs
    }
    artifacts = [out / "solution.json"]
    for vt in report["flavors"]:
        artifacts.extend(p for p in (out / vt).iterdir() if p.suffix != ".log")
    report["artifact_sha256"] = {
        str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(artifacts)
    }
    (out / "checks.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
