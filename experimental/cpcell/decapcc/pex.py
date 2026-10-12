"""Open-source partial PEX for the verified decap.

FasterCap solves M0/V0/M1 and BPR capacitance, with explicit GT2 dielectric
interfaces. Device capacitance stays in BSIM-CMG. Additional FEOL/contact
capacitance is excluded to avoid silently double-counting calibrated device
fringing. GATE/SDCON/BPR/M0/M1 and contact resistances are layout-derived.
"""

import argparse
from collections import defaultdict
import hashlib
import itertools
import json
import math
from pathlib import Path
import re
import shutil
import subprocess

import klayout.db as db
import numpy as np

LAYERS = {
    "BPR": 8,
    "VBPR": 9,
    "SDCON": 10,
    "VSD": 11,
    "VG": 12,
    "M0": 20,
    "V0": 22,
    "M1": 25,
}
SCOPE = (
    "Partial PEX: 3D M0/V0/M1/BPR Maxwell capacitance; GT2 dielectric interfaces; "
    "GATE/SDCON/BPR/M0/M1 sheet and VG/VSD/V0/VBPR contact resistance. "
    "BSIM-CMG supplies intrinsic/overlap/fringe device C. Additional FEOL, "
    "VG/VSD/VBPR C, diffusion spreading R, neighboring cells, and chip/package "
    "environment are not extracted. Far-field reference is tied to vss."
)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def technology(pdk):
    itf = "\n".join(
        line.split("$")[0] for line in (pdk / "nxtgrd/GT2.itf").read_text().splitlines()
    )
    blocks = {
        (k, n): dict(re.findall(r"(\w+)\s*=\s*([^\s}]+)", body))
        for k, n, body in re.findall(
            r"(CONDUCTOR|DIELECTRIC|VIA)\s+(\w+)\s*\{([^}]+)\}", itf
        )
    }
    sheet = {
        n: float(blocks["CONDUCTOR", n]["RPSQ"])
        for n in ("GATE", "SDCON", "BPR", "M0", "M1")
    }
    vias = {
        n: {
            "lower": blocks["VIA", n]["FROM"],
            "upper": blocks["VIA", n]["TO"],
            "ohm": float(blocks["VIA", n]["RPV"]),
            "area_um2": float(blocks["VIA", n]["AREA"]),
        }
        for n in ("VG", "VSD", "V0", "VBPR")
    }
    ict = (pdk / "qrc/GT2.ict").read_text()
    ict_blocks = {
        (k, n): dict(re.findall(r"(\w+)\s+([^\s{}]+)", body))
        for k, n, body in re.findall(
            r"(conductor|dielectric)\s+(\w+)\s*\{([^}]+)\}", ict
        )
    }
    stack = {}
    for n in ("M0", "M1"):
        b = ict_blocks["conductor", n]
        z = float(b["height"]) * 1000
        stack[n] = [z, z + float(b["thickness"]) * 1000]
    stack["V0"] = [stack["M0"][1], stack["M1"][0]]
    box_depth = float(ict_blocks["dielectric", "BOX_diel"]["depth"]) * 1000
    bpr_t = float(blocks["CONDUCTOR", "BPR"]["THICKNESS"]) * 1000
    stack["BPR"] = [-box_depth - bpr_t, -box_depth]
    dielectric = {
        n: float(ict_blocks["dielectric", n + "_diel"]["dielectric_constant"])
        for n in ("BOX", "GATE", "M0")
    }
    return dict(
        sheet_ohm=sheet,
        vias=vias,
        z_nm=stack,
        er=dielectric,
        bpr_position="BPR top at bottom of 23 nm BOX; ITF 90 nm thickness",
        dielectric_interfaces_nm=[0, stack["M0"][0]],
    )


def geometry(source):
    lvs = db.LayoutVsSchematic()
    lvs.read(str(source / "cell.lvsdb"))
    circuit = lvs.netlist().top_circuit()
    pairs = list(lvs.xref().each_circuit_pair())
    if len(pairs) != 1 or pairs[0].status() != db.NetlistCrossReference.Match:
        raise ValueError("Source cell must pass transistor LVS")
    indices = {
        name: i
        for name, number in LAYERS.items()
        for i in lvs.layer_indexes()
        if lvs.layer_info(i).layer == number and lvs.layer_info(i).datatype == 0
    }
    # Recover derived poly by its actual extracted gate terminals, avoiding
    # dependence on KLayout's generated layer names (l1, l2, ...).
    gates = {
        i
        for d in circuit.each_device()
        for t in d.device_class().terminal_definitions()
        if t.name == "G"
        for i in lvs.shapes_of_terminal(d.terminal_ref(t.id()))
    }
    if len(gates) != 1:
        raise ValueError("Cannot identify one extracted poly layer")
    indices["GATE"] = gates.pop()
    names = {
        n.cluster_id: lvs.xref().other_net_for(n).expanded_name().lower()
        for n in circuit.each_net()
    }
    if set(names.values()) != {"net7", "net12", "vdd", "vss"}:
        raise ValueError(f"Unexpected extracted net topology: {names}")
    regions = {
        names[n.cluster_id]: {
            layer: lvs.polygons_of_net(n, i, True).merged()
            for layer, i in indices.items()
        }
        for n in circuit.each_net()
    }
    devices = []
    for d in circuit.each_device():
        terminals = {}
        for t in d.device_class().terminal_definitions():
            reg = db.Region()
            for r in lvs.shapes_of_terminal(d.terminal_ref(t.id())).values():
                reg += r
            terminals[t.name] = dict(
                net=names[d.net_for_terminal(t.id()).cluster_id], region=reg.merged()
            )
        if abs(d.parameter("W") - 0.031) > 1e-9 or abs(d.parameter("L") - 0.014) > 1e-9:
            raise ValueError("Unexpected device sizing")
        devices.append(
            dict(
                name=f"M{d.id()}",
                model=d.device_class().name,
                width_um=d.parameter("W"),
                length_um=d.parameter("L"),
                terminals=terminals,
            )
        )
    if len(devices) != 2:
        raise ValueError("Expected two extracted transistors")
    return regions, devices, lvs.internal_layout().dbu


def rectangles(region):
    for p in region.merged().decompose_trapezoids_to_region().each():
        b = p.bbox()
        if p.area() != b.area():
            raise ValueError("Expected Manhattan rectangles")
        yield b


def fastercap_input(regions, tech, dbu, folder, margin_nm):
    """Mesh the boundary of the union, omitting internal metal/via faces."""
    folder.mkdir(parents=True, exist_ok=True)
    factor = dbu * 1000  # GDS integer coordinate -> numeric nm for FasterCap
    panels = defaultdict(list)
    stack = tech["z_nm"]
    zs = sorted({z for interval in stack.values() for z in interval})

    def quad(net, points, er):
        panels[er].append(
            "Q " + net + " " + " ".join(f"{v:.12g}" for p in points for v in p)
        )

    def planar(net, region, z, er):
        for b in rectangles(region):
            x0, y0, x1, y1 = (v * factor for v in (b.left, b.bottom, b.right, b.top))
            quad(net, [(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)], er)

    for net in sorted(regions):
        slabs = []
        for lo, hi in itertools.pairwise(zs):
            r = db.Region()
            for layer, (a, b) in stack.items():
                if a <= lo and hi <= b:
                    r += regions[net].get(layer, db.Region())
            slabs.append(r.merged())
        for i, z in enumerate(zs):
            below = slabs[i - 1] if i else db.Region()
            above = slabs[i] if i < len(slabs) else db.Region()
            planar(
                net, below - above, z, tech["er"]["BOX"] if z < 0 else tech["er"]["M0"]
            )
            er_below = (
                tech["er"]["BOX"]
                if z < 0
                else tech["er"]["GATE"]
                if z == stack["M0"][0]
                else tech["er"]["M0"]
            )
            planar(net, above - below, z, er_below)
        for (lo, hi), r in zip(itertools.pairwise(zs), slabs):
            er = tech["er"]["BOX"] if hi < 0 else tech["er"]["M0"]
            for e in r.edges().each():
                x0, y0, x1, y1 = (v * factor for v in (e.p1.x, e.p1.y, e.p2.x, e.p2.y))
                quad(net, [(x0, y0, lo), (x1, y1, lo), (x1, y1, hi), (x0, y0, hi)], er)
    entries = ["* GT2N partial PEX, numeric coordinates in nm; scale output F by 1e-9"]
    for i, (er, lines) in enumerate(sorted(panels.items())):
        name = f"conductors_k{er:g}.qui"
        (folder / name).write_text("* Boundary panels\n" + "\n".join(lines) + "\n")
        entries.append(f"C {name} {er:g} 0 0 0" + (" +" if i + 1 < len(panels) else ""))
    extent = db.Region()
    for rs in regions.values():
        for r in rs.values():
            extent += r
    bounds = extent.bbox().enlarged(round(margin_nm / factor))
    for i, (z, upper, lower) in enumerate(
        (
            (0, tech["er"]["GATE"], tech["er"]["BOX"]),
            (stack["M0"][0], tech["er"]["M0"], tech["er"]["GATE"]),
        )
    ):
        interface = db.Region(bounds)
        if z == stack["M0"][0]:
            for rs in regions.values():
                interface -= rs["M0"]
        lines = []
        for b in rectangles(interface):
            x0, y0, x1, y1 = (v * factor for v in (b.left, b.bottom, b.right, b.top))
            points = [(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)]
            lines.append(
                "Q interface " + " ".join(f"{v:.12g}" for p in points for v in p)
            )
        name = f"interface_{i}.qui"
        (folder / name).write_text(
            "* Truncated infinite planar interface\n" + "\n".join(lines) + "\n"
        )
        entries.append(f"D {name} {upper:g} {lower:g} 0 0 0 0 0 {z + 1:g}")
    path = folder / "geometry.lst"
    path.write_text("\n".join(entries) + "\n")
    return path


def field_solve(folder, error=0.01):
    binary = shutil.which("FasterCap")
    if binary is None:
        raise FileNotFoundError("FasterCap is not on PATH")
    log = folder / "fastercap.log"
    with log.open("w") as f:
        subprocess.run(
            [binary, "-b", "geometry.lst", f"-a{error:g}", "-t0.0001", "-e"],
            cwd=folder,
            stdout=f,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=600,
        )
    text = log.read_text()
    matches = list(
        re.finditer(r"Capacitance matrix is:\s*(?:\r?\n)(.*?)\r?\n\s*\r?\n", text, re.S)
    )
    if not matches:
        raise ValueError(f"Missing FasterCap matrix: {log}")
    rows = matches[-1][1].strip().splitlines()
    parsed = [line.split() for line in rows if "_" in line and len(line.split()) == 5]
    names = [
        next(n for n in ("net7", "net12", "vdd", "vss") if row[0].endswith("_" + n))
        for row in parsed
    ]
    matrix = np.array([[float(x) for x in row[1:]] for row in parsed]) * 1e-9
    if matrix.shape != (4, 4) or len(set(names)) != 4:
        raise ValueError(f"Unexpected field solver matrix: {rows}")
    order = sorted(names)
    matrix = matrix[
        np.ix_([names.index(n) for n in order], [names.index(n) for n in order])
    ]
    asymmetry = float(np.max(np.abs(matrix - matrix.T)) / np.max(np.diag(matrix)))
    if asymmetry > 0.02:
        raise ValueError(f"Capacitance matrix reciprocity error {asymmetry}")
    matrix = (matrix + matrix.T) / 2
    if np.linalg.eigvalsh(matrix).min() <= 0 or min(matrix.sum(axis=1)) < 0:
        raise ValueError("Non-passive Maxwell matrix")
    if np.max(matrix - np.diag(np.diag(matrix))) > 1e-24:
        raise ValueError("Positive off-diagonal Maxwell entry")
    result = dict(
        nets=order,
        maxwell_f=matrix.tolist(),
        symmetry_error=asymmetry,
        margin_nm=json.loads((folder / "settings.json").read_text())["margin_nm"],
        error=error,
        input_sha256={
            p.name: digest(p) for p in folder.iterdir() if p.suffix in (".qui", ".lst")
        },
    )
    (folder / "matrix.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def resistance(regions, devices, tech, dbu, mesh_nm):
    """Finite-volume sheet mesh and area-scaled via/contact resistors."""
    step = max(1, round(mesh_nm / (dbu * 1000)))
    meshes, resistors, terminals, representatives, aliases = {}, [], {}, {}, {}
    for net, rs in sorted(regions.items()):
        layers = {}
        for layer, sheet in tech["sheet_ohm"].items():
            boxes = []
            for b in rectangles(rs.get(layer, db.Region())):
                nx, ny = math.ceil(b.width() / step), math.ceil(b.height() / step)
                xs = [b.left + round(i * b.width() / nx) for i in range(nx + 1)]
                ys = [b.bottom + round(i * b.height() / ny) for i in range(ny + 1)]
                boxes += [
                    db.Box(a, c, b, d)
                    for a, b in itertools.pairwise(xs)
                    for c, d in itertools.pairwise(ys)
                ]
            nodes = [f"{net}_{layer}_{i}" for i in range(len(boxes))]
            layers[layer] = list(zip(boxes, nodes))
            for i, a in enumerate(boxes):
                for j, b in enumerate(boxes[i + 1 :], i + 1):
                    if a.right == b.left or b.right == a.left:
                        overlap = min(a.top, b.top) - max(a.bottom, b.bottom)
                        if overlap > 0:
                            resistors.append(
                                (
                                    nodes[i],
                                    nodes[j],
                                    sheet * (a.width() + b.width()) / (2 * overlap),
                                )
                            )
                    if a.top == b.bottom or b.top == a.bottom:
                        overlap = min(a.right, b.right) - max(a.left, b.left)
                        if overlap > 0:
                            resistors.append(
                                (
                                    nodes[i],
                                    nodes[j],
                                    sheet * (a.height() + b.height()) / (2 * overlap),
                                )
                            )
        meshes[net] = layers

        def landing(layer, region):
            candidates = [
                (b, n)
                for b, n in layers[layer]
                if not (db.Region(b) & region).is_empty()
            ]
            if not candidates:
                raise ValueError(f"No {net}/{layer} landing")
            return min(
                candidates,
                key=lambda bn: (
                    (bn[0].center() - region.bbox().center()).sq_abs(),
                    bn[1],
                ),
            )[1]

        for layer, model in tech["vias"].items():
            for p in rs.get(layer, db.Region()).each():
                region = db.Region(p)
                r = model["ohm"] * model["area_um2"] / (p.area() * dbu**2)
                resistors.append(
                    (
                        landing(model["lower"], region),
                        landing(model["upper"], region),
                        r,
                    )
                )
        for device in devices:
            for name, t in device["terminals"].items():
                if t["net"] != net:
                    continue
                layer = "GATE" if name == "G" else "BPR" if name == "B" else "SDCON"
                region = t["region"] if name != "B" else rs["BPR"]
                terminals[device["name"], name] = (
                    net if name == "B" else landing(layer, region)
                )
        if net in ("vdd", "vss"):
            pin = landing("BPR", rs["BPR"])
            aliases[pin] = net
            representatives[net] = pin
        else:
            representatives[net] = landing("M1", rs["M1"])
    # All meshed conductors must connect to a device or external supply.
    graph = defaultdict(set)
    for a, b, r in resistors:
        if r <= 0 or not math.isfinite(r):
            raise ValueError("Invalid resistance")
        graph[a].add(b)
        graph[b].add(a)
    for net, layers in meshes.items():
        expected = {n for values in layers.values() for _, n in values}
        visited = set()
        todo = [representatives[net]]
        while todo:
            n = todo.pop()
            if n not in visited:
                visited.add(n)
                todo.extend(graph[n] - visited)
        if visited != expected:
            raise ValueError(f"Disconnected or shorted mesh for {net}")
    return dict(
        resistors=[(aliases.get(a, a), aliases.get(b, b), r) for a, b, r in resistors],
        terminals={f"{d}:{t}": aliases.get(n, n) for (d, t), n in terminals.items()},
        representatives={net: aliases.get(n, n) for net, n in representatives.items()},
        mesh_nm=mesh_nm,
        nodes=len(graph),
    )


def write_circuits(devices, rc, cap, out):
    for kind in ("ideal", "rc"):
        lines = [f"* {SCOPE}", ".SUBCKT decap vdd vss"]
        for d in devices:
            nodes = [
                d["terminals"][t]["net"]
                if kind == "ideal"
                else rc["terminals"][d["name"] + ":" + t]
                for t in ("D", "G", "S", "B")
            ]
            lines.append(
                f"{d['name']} {' '.join(nodes)} {d['model']} L={d['length_um']:g}u"
            )
        if kind == "rc":
            for i, (a, b, r) in enumerate(rc["resistors"]):
                lines.append(f"R{i} {a} {b} {r:.12g}")
            m = np.array(cap["maxwell_f"])
            names = cap["nets"]
            for i, a in enumerate(names):
                node = rc["representatives"][a]
                for j in range(i + 1, len(names)):
                    lines.append(
                        f"C{i}_{j} {node} {rc['representatives'][names[j]]} {-m[i, j]:.12g}"
                    )
                # Maxwell row sum is capacitance to the remote reference.
                if node != "vss":
                    lines.append(f"C{i}_ref {node} vss {sum(m[i]):.12g}")
        lines.append(".ENDS decap")
        (out / (kind + ".cir")).write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).parent / "w31/lvt"
    )
    parser.add_argument(
        "--pdk-root", type=Path, default=Path(__file__).resolve().parents[3]
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--margin-nm", type=float, default=400)
    parser.add_argument("--mesh-nm", type=float, default=7)
    parser.add_argument("--error", type=float, default=0.01)
    args = parser.parse_args()
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "extraction.json").unlink(missing_ok=True)
    regions, devices, dbu = geometry(args.source)
    tech = technology(args.pdk_root)
    fastercap_input(regions, tech, dbu, out, args.margin_nm)
    (out / "settings.json").write_text(
        json.dumps(
            dict(margin_nm=args.margin_nm, mesh_nm=args.mesh_nm, error=args.error),
            indent=2,
        )
        + "\n"
    )
    cap = field_solve(out, args.error)
    rc = resistance(regions, devices, tech, dbu, args.mesh_nm)
    write_circuits(devices, rc, cap, out)
    report = dict(
        status="passed",
        scope=SCOPE,
        technology=tech,
        capacitance=cap,
        resistance=rc,
        devices=[{k: v for k, v in d.items() if k != "terminals"} for d in devices],
        sha256={
            str(p.resolve()): digest(p)
            for p in (
                args.source / "cell.gds",
                args.source / "cell.lvsdb",
                args.pdk_root / "nxtgrd/GT2.itf",
                args.pdk_root / "qrc/GT2.ict",
                Path(__file__),
            )
        },
    )
    (out / "extraction.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            dict(
                status="passed",
                nets=cap["nets"],
                maxwell_af=(np.array(cap["maxwell_f"]) * 1e18).tolist(),
                nodes=rc["nodes"],
                resistors=len(rc["resistors"]),
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
