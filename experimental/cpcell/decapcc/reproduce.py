"""Generate a W31 cross-coupled decap using CPCellUCSD's BandRouter.

The two transistor columns have disjoint active regions and uncut gates.
CP-SAT chooses the two cross-coupling routes on M0/M1/M2. Only device
placement and legal contact sites are specified here; no wires are prescribed.
"""

import argparse
from collections import defaultdict
from itertools import pairwise
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import subprocess


def problem():
    from cpcell.core.band_router import BandRouter, RouteEdge
    from cpcell.tech.gt2n_thin import box

    frame, edges = [], []
    terminals = defaultdict(list)
    width, height = 168, 144

    def add(layer, *coords):
        frame.append(box(layer, *coords))

    add("BOUNDARY", 0, 0, width, height)
    add("NSEL", 0, 0, width, 72)
    add("PSEL", 0, 72, width, height)
    add("NWELL", 0, 72, width, height)
    for y in (0, height):
        add("BPR", 0, y - 16, width, y + 16)
        add("GCUT", -7, y - 5, width + 7, y + 5)
    for x in range(0, width + 1, 42):
        add("GATE", x - 7, 0, x + 7, height)
        if x in (0, 84, 168):
            add("DUMMY", x - 7, 0, x + 7, height)
    add("ACT", 0, 26, 84, 57)
    add("ACT", 84, 87, 168, 118)
    # NMOS: left source=vss, gate=net12, right drain=net7.
    # PMOS: left drain=net12, gate=net7, right source=vdd.
    for x, lo, hi in ((21, 5, 62), (63, 21, 62), (105, 82, 123), (147, 82, 139)):
        add("SDCON", x - 8, lo, x + 8, hi)
    # Match the released library: VBPR is fully inside both SDCON and BPR.
    add("VBPR", 13, 5, 29, 16)
    add("VBPR", 139, 128, 155, 139)

    def node(layer, x, y):
        return (layer, 2 * y, 2 * x)

    def edge(u, v, shapes, cost, owner=None):
        edges.append(RouteEdge(u, v, tuple(shapes), cost, owner))

    # Interior tracks on the released 24 nm M0 grid. Restrict the candidate
    # graph itself, including contact landing pads, before solving routes.
    rows = tuple(range(48, 120, 24))
    columns = (21, 63, 105, 147)
    for layer, metal in ((1, "M0"), (3, "M2")):
        for y in rows:
            for a, b in pairwise(range(21, 148, 21)):
                edge(
                    node(layer, a, y),
                    node(layer, b, y),
                    [box(metal, a - 12, y - 6, b + 12, y + 6)],
                    2 * (b - a),
                )
    for x in columns:
        for a, b in pairwise(rows):
            edge(
                node(2, x, a),
                node(2, x, b),
                [box("M1", x - 7, a - 10, x + 7, b + 10)],
                2 * (b - a),
            )
        for y in rows:
            for layer, metal, via in ((1, "M0", "V0"), (3, "M2", "V1")):
                edge(
                    node(layer, x, y),
                    node(2, x, y),
                    [
                        box(via, x - 7, y - 6, x + 7, y + 6),
                        box(metal, x - 12, y - 6, x + 12, y + 6),
                        box("M1", x - 7, y - 10, x + 7, y + 10),
                    ],
                    100,
                )

    for net, kind, x, y in (
        ("net12", "VG", 42, 72),
        ("net12", "VSD", 105, 96),
        ("net7", "VG", 126, 72),
        ("net7", "VSD", 63, 48),
    ):
        terminal = ("T", str(sum(map(len, terminals.values()))))
        terminals[net].append(terminal)
        half = 7 if kind == "VG" else 6.5
        edge(
            terminal,
            node(1, x, y),
            [
                box(kind, x - half, y - 6, x + half, y + 6),
                box("M0", x - 12, y - 6, x + 12, y + 6),
            ],
            100,
            net,
        )
    for candidate in edges:
        for shape in candidate.shapes:
            if shape.layer == "M0" and shape.box[1] + shape.box[3] not in {
                4 * y for y in rows
            }:
                raise ValueError("M0 candidate is not centered on an allowed track")
    router = BandRouter(
        edges,
        dict(terminals),
        {},
        spacing={
            "M0": dict(horizontal=34, vertical=24, corner=30),
            "M1": dict(horizontal=28, vertical=40, corner=32),
            "M2": dict(horizontal=34, vertical=24, corner=30),
            "V0": 28,
            "V1": 28,
            "VG": 28,
            "VSD": 28,
        },
    )
    return router, frame, width, height


def schematic(name, vt):
    return (
        f".SUBCKT {name} vdd vss\n"
        f"MM1 net7 net12 vss vss nmos_{vt} W=0.031u L=0.014u M=1\n"
        f"MM0 net12 net7 vdd vdd pmos_{vt} W=0.031u L=0.014u M=1\n"
        f".ENDS {name}\n"
    )


def export(router, frame, solution, width, height, vt, folder):
    import klayout.db as db
    from cpcell.gds.gds_GT2N_SH import LAYERS
    from cpcell.tech.gt2n_thin import box

    name = f"gt2_6t_decapcc_cp_w31_{vt}"
    router.validate(solution["routes"])
    layout = db.Layout()
    layout.dbu = 0.0005
    cell = layout.create_cell(name)
    shapes = list(frame)
    if vt != "lvt":
        shapes.append(box(vt.upper(), 0, 0, width, height))
    for route in solution["routes"]:
        shapes.extend(router.edges[route["edge"]].shapes)
    for s in shapes:
        cell.shapes(
            layout.layer(LAYERS[s.layer], 250 if s.layer == "BOUNDARY" else 0)
        ).insert(db.Box(*s.box))
    for index in layout.layer_indexes():
        if layout.get_info(index).datatype == 0:
            dest = cell.shapes(index)
            merged = db.Region(dest).merged()
            dest.clear()
            dest.insert(merged)
    for y, net in ((0, "vss"), (height, "vdd")):
        dest = cell.shapes(layout.layer(LAYERS["BPR"], 251))
        dest.insert(db.Box(0, 2 * (y - 16), 2 * width, 2 * (y + 16)))
        dest.insert(db.Text(net, db.Trans(width, 2 * y)))
    folder.mkdir(parents=True, exist_ok=True)
    layout.write(str(folder / "cell.gds"))
    (folder / "cell.cdl").write_text(schematic(name, vt))
    lines = [
        "VERSION 5.8 ;",
        'BUSBITCHARS "[]" ;',
        'DIVIDERCHAR "/" ;',
        f"MACRO {name}",
        "  CLASS CORE ;",
        "  ORIGIN 0 0 ;",
        "  SYMMETRY X Y ;",
        "  SITE gt2_6t ;",
        f"  SIZE {width / 1000:g} BY 0.144 ;",
    ]
    for y, net, use in ((0, "vss", "GROUND"), (height, "vdd", "POWER")):
        lines += [
            f"  PIN {net}",
            "    DIRECTION INOUT ;",
            f"    USE {use} ;",
            "    PORT",
            "      LAYER BPR ;",
            f"        RECT 0 {(y - 16) / 1000:g} {width / 1000:g} {(y + 16) / 1000:g} ;",
            "    END",
            f"  END {net}",
        ]
    lines.append("  OBS")
    for layer in ("M0", "M1", "M2"):
        reg = db.Region(cell.shapes(layout.layer(LAYERS[layer], 0))).merged()
        if not reg.is_empty():
            lines.append(f"    LAYER {layer} ;")
            for p in reg.each():
                if p.area() != p.bbox().area():
                    raise ValueError("LEF route must be rectangular")
                b = p.bbox()
                coords = " ".join(
                    f"{v * layout.dbu:g}" for v in (b.left, b.bottom, b.right, b.top)
                )
                lines.append(f"      RECT {coords} ;")
    lines += ["  END", f"END {name}", "END LIBRARY", ""]
    (folder / "cell.lef").write_text("\n".join(lines))
    return layout, cell


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cpcell-root", type=Path, default=Path.home() / "iv4/repos/CPCellUCSD"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument(
        "--vt",
        nargs="+",
        choices=("lvt", "elvt", "ulvt", "svt", "hvt"),
        default=["lvt"],
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    cp_root = args.cpcell_root.resolve()
    sys.path.insert(0, str(cp_root))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # A failed new solve must not leave an earlier validation marked passed.
    for filename in ("checks.json", "solution.json"):
        (args.output_dir / filename).unlink(missing_ok=True)
    router, frame, width, height = problem()
    solution = router.solve(args.timeout)
    source_files = [
        "cpcell/core/band_router.py",
        "cpcell/tech/gt2n_thin.py",
        "cpcell/gds/gds_GT2N_SH.py",
    ]
    solution.update(
        format="cpcell.decapcc.v1",
        width_nm=width,
        height_nm=height,
        flavors=list(dict.fromkeys(args.vt)),
        cpcell_commit=subprocess.check_output(
            ["git", "-C", str(cp_root), "rev-parse", "HEAD"], text=True
        ).strip(),
        source_sha256={
            p: hashlib.sha256((cp_root / p).read_bytes()).hexdigest()
            for p in source_files
        },
        versions={
            package: importlib.metadata.version(package)
            for package in ("ortools", "klayout")
        },
    )
    (args.output_dir / "solution.json").write_text(
        json.dumps(solution, indent=2) + "\n"
    )
    for vt in solution["flavors"]:
        export(router, frame, solution, width, height, vt, args.output_dir / vt)
    print(json.dumps({k: v for k, v in solution.items() if k != "routes"}, indent=2))


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    main()
