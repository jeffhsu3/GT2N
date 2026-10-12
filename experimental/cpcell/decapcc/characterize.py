"""Sweep layout-extracted decap AC admittance and model-limited DC leakage.

Run pex.py first. Xyce's MPI build may require execution outside a sandbox
that prohibits local sockets. All decks, model adaptations and CSV results
are retained, including signed currents near the numerical noise floor.
"""

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import json
import math
import operator
import os
from pathlib import Path
import re
import shutil
import subprocess

import numpy as np

FLAVORS = ("elvt", "ulvt", "lvt", "svt", "hvt")
VOLTAGES = (0.5, 0.6, 0.7, 0.8, 0.9)
TEMPERATURES = (-40, 25, 85, 125)
UNSUPPORTED = {"VERSION", "CAPMOD", "COREMOD", "PRWG"}


def number(expression):
    """Evaluate numeric HSPICE expressions, without executing Python code."""
    operations = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
    }

    def evaluate(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in operations:
            return operations[type(node.op)](evaluate(node.left), evaluate(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            return (-1 if isinstance(node.op, ast.USub) else 1) * evaluate(node.operand)
        raise ValueError(f"Unsupported model expression {expression!r}")

    value = evaluate(ast.parse(expression, mode="eval").body)
    if not math.isfinite(value):
        raise ValueError("Nonfinite model parameter")
    return value


def models(pdk, vt, out):
    source = pdk / f"device/tt/gt2_w31_{vt}_tt.sp"
    text = source.read_text()
    header = text[: text.upper().index(".LIB")]
    lines = ["* " + line.lstrip("/* ") for line in header.splitlines()]
    lines += [
        "* Xyce uses BSIM-CMG 110; source HSPICE card specifies 111.",
        "* Omitted unsupported model parameters are recorded in models.json.",
    ]
    active = False
    kind = None
    omitted = {}
    switches = {}
    for line in text.splitlines():
        if line.upper().startswith((".LIB", ".ENDL")):
            active = False
        elif line.upper().startswith(".MODEL"):
            kind = line.split()[1].lower()
            if kind not in ("nmos", "pmos"):
                raise ValueError("Unknown model")
            active = True
            omitted[kind] = {}
            switches[kind] = {}
            lines.append(
                f".MODEL {kind}_{vt} {kind.upper()} " + " ".join(line.split()[3:])
            )
        elif active and line.startswith("+"):
            match = re.fullmatch(r"\+\s*(\w+)\s*=\s*(.*?)\s*", line)
            if not match:
                raise ValueError(f"Malformed card line: {line}")
            key, raw = match.groups()
            key = key.upper()
            value = number(raw.strip('"'))
            if key in UNSUPPORTED:
                omitted[kind][key] = value
                continue
            if key == "LEVEL":
                if value != 72:
                    raise ValueError("Expected HSPICE BSIM-CMG level 72")
                value = 110
            if key in (
                "IGCMOD",
                "IGBMOD",
                "GIDLMOD",
                "BULKMOD",
                "CGEOMOD",
                "NFIN",
                "HFIN",
                "TFIN",
                "TNOM",
            ):
                switches[kind][key] = value
            lines.append(f"+ {key}={value:.15g}")
    if set(omitted) != {"nmos", "pmos"}:
        raise ValueError("Expected N/P models")
    path = out / f"models_{vt}.inc"
    path.write_text("\n".join(lines) + "\n")
    return dict(
        source=str(source),
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        adapted_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        omitted=omitted,
        switches=switches,
    )


def read_csv(path):
    with path.open() as f:
        data = list(csv.DictReader(f))
    if not data:
        raise ValueError(f"Empty simulation data {path}")
    result = {k: np.array([float(row[k]) for row in data]) for k in data[0]}
    if any(not np.all(np.isfinite(v)) for v in result.values()):
        raise ValueError(f"Nonfinite simulation data in {path}")
    return result


def bench(out, pex, vt, voltage, temp, gmin=1e-15, tag="sweep", run=True):
    folder = out / "benches" / f"{tag}_{vt}_v{voltage:g}_t{temp:g}_g{gmin:g}"
    folder.mkdir(parents=True, exist_ok=True)
    text = "GT2N decap extracted capacitance and model leakage\n"
    text += f'.INCLUDE "{out / ("models_" + vt + ".inc")}"\n'
    for kind in ("ideal", "rc"):
        cir = (pex / (kind + ".cir")).read_text().replace("decap", f"decap_{kind}")
        cir = re.sub(r"\b(nmos|pmos)_lvt\b", lambda m: m[1] + "_" + vt, cir)
        text += cir + "\n"
    text += f"""
XI ideal_vdd 0 decap_ideal
XR rc_vdd 0 decap_rc
VIDEAL ideal_vdd 0 DC {voltage:g} AC 1
VRC rc_vdd 0 DC {voltage:g} AC 1
.OPTIONS DEVICE TEMP={temp:g} GMIN={gmin:g}
.OPTIONS NONLIN ABSTOL=1e-15 RELTOL=1e-8 RHSTOL=1e-14
.OP
.AC DEC 10 1e6 1e12
.PRINT AC FORMAT=CSV PRECISION=15 IR(VIDEAL) II(VIDEAL) IR(VRC) II(VRC)
.PRINT AC_IC FORMAT=CSV PRECISION=15 I(VIDEAL) I(VRC) V(XI:net7) V(XI:net12)
.END
"""
    path = folder / "bench.cir"
    path.write_text(text)
    if not run:
        return str(path)
    with (folder / "xyce.log").open("w") as f:
        subprocess.run(
            [shutil.which("Xyce"), str(path)],
            cwd=folder,
            stdout=f,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=90,
        )
    log = (folder / "xyce.log").read_text()
    if re.search(
        r"Netlist (?:warning|error)|Simulation aborted|not recognized|parameter ignored",
        log,
        re.I,
    ):
        raise ValueError(f"Simulation warnings need review: {folder / 'xyce.log'}")
    ac = read_csv(folder / "bench.cir.FD.csv")
    dc = read_csv(folder / "bench.cir.TD.csv")
    freq = ac["FREQ"]
    if len(freq) != 61 or not np.all(np.diff(freq) > 0):
        raise ValueError("Incomplete AC sweep")
    result = dict(
        vt=vt,
        voltage_v=voltage,
        temperature_c=temp,
        gmin_s=gmin,
        tag=tag,
        frequency_hz=freq.tolist(),
        models={},
    )
    for kind, source in (("ideal", "VIDEAL"), ("rc", "VRC")):
        admittance = -(ac[f"IR({source})"] + 1j * ac[f"II({source})"])
        ceff = admittance.imag / (2 * np.pi * freq)
        z = 1 / admittance
        if not np.all(ceff > 0):
            raise ValueError("Non-capacitive response")
        result["models"][kind] = dict(
            capacitance_ff=(ceff * 1e15).tolist(),
            impedance_ohm=np.abs(z).tolist(),
            esr_ohm=z.real.tolist(),
            leakage_a=float(-dc[f"I({source})"][-1]),
        )
    if abs(dc["V(XI:NET7)"][-1]) > 1e-6 or abs(dc["V(XI:NET12)"][-1] - voltage) > 1e-6:
        raise ValueError("Unexpected cross-coupled DC bias")
    result["dc_internal_v"] = {
        n: float(dc[f"V(XI:{n.upper()})"][-1]) for n in ("net7", "net12")
    }
    result["deck_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    result["csv_sha256"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.glob("*.csv")
    }
    (folder / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def plot(out, results):
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/gt2n_decapcc_mpl")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.3), constrained_layout=True)
    for vt in [vt for vt in FLAVORS if any(r["vt"] == vt for r in results)]:
        nominal = next(
            r
            for r in results
            if r["tag"] == "sweep"
            and r["vt"] == vt
            and r["voltage_v"] == 0.7
            and r["temperature_c"] == 25
        )
        axes[0].semilogx(
            nominal["frequency_hz"],
            nominal["models"]["rc"]["capacitance_ff"],
            label=vt.upper(),
        )
        vs = sorted(
            [
                r
                for r in results
                if r["tag"] == "sweep" and r["vt"] == vt and r["temperature_c"] == 25
            ],
            key=lambda r: r["voltage_v"],
        )
        axes[1].plot(
            [r["voltage_v"] for r in vs],
            [r["models"]["rc"]["capacitance_ff"][0] for r in vs],
            marker="o",
            label=vt.upper(),
        )
    axes[0].set(
        xlabel="Frequency (Hz)",
        ylabel="Effective capacitance (fF)",
        title="TT, 0.7 V, 25 °C",
    )
    axes[1].set(
        xlabel="Supply voltage (V)",
        ylabel="Effective capacitance (fF)",
        title="TT, 1 MHz, 25 °C",
    )
    for ax in axes:
        ax.grid(alpha=0.2)
        ax.legend()
        ax.spines[["top", "right"]].set_visible(False)
    fig.suptitle("W31 decap: BSIM-CMG devices + extracted partial wiring RC")
    fig.savefig(out / "capacitance.png", dpi=180)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pex-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--pdk-root", type=Path, default=Path(__file__).resolve().parents[3]
    )
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--vt",
        choices=FLAVORS,
        action="append",
        help="Restrict the full sweep to these flavors (default: all five)",
    )
    args = parser.parse_args()
    flavors = tuple(args.vt) if args.vt else FLAVORS
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    pex = args.pex_dir.resolve()
    (out / "characterization.json").unlink(missing_ok=True)
    model_report = {vt: models(args.pdk_root.resolve(), vt, out) for vt in FLAVORS}
    (out / "models.json").write_text(json.dumps(model_report, indent=2) + "\n")
    cases = (
        [("lvt", 0.7, 25, 1e-15, "sweep")]
        if args.smoke
        else [
            (vt, v, t, 1e-15, "sweep")
            for vt in flavors
            for v in VOLTAGES
            for t in TEMPERATURES
        ]
    )
    if not args.smoke:
        cases += [
            (vt, 0.7, t, g, "floor")
            for vt in flavors
            for t in (25, 125)
            for g in (1e-12, 1e-18)
        ]

    def run(case):
        return bench(out, pex, *case, run=not args.prepare_only)

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = []
        for result in executor.map(run, cases):
            results.append(result)
            if isinstance(result, dict):
                print(
                    result["vt"],
                    result["voltage_v"],
                    result["temperature_c"],
                    result["tag"],
                    "C@1MHz_fF=",
                    round(result["models"]["rc"]["capacitance_ff"][0], 6),
                    "signed_DC_A=",
                    result["models"]["rc"]["leakage_a"],
                    flush=True,
                )
    if args.prepare_only:
        print("\n".join(results))
        return
    summary = []
    for r in results:
        if r["tag"] != "sweep" or r["voltage_v"] != 0.7 or r["temperature_c"] != 25:
            continue
        index = {int(round(f)): i for i, f in enumerate(r["frequency_hz"])}
        summary.append(
            dict(
                vt=r["vt"],
                ideal_capacitance_ff=r["models"]["ideal"]["capacitance_ff"][0],
                extracted_capacitance_ff={
                    str(f): r["models"]["rc"]["capacitance_ff"][index[f]]
                    for f in (10**6, 10**9, 10**10, 10**11)
                },
                esr_ohm_1ghz=r["models"]["rc"]["esr_ohm"][index[10**9]],
                signed_model_leakage_a=r["models"]["rc"]["leakage_a"],
            )
        )
    report = dict(
        status="passed",
        process_corner="TT only",
        temperatures_c=sorted({r["temperature_c"] for r in results}),
        voltages_v=sorted({r["voltage_v"] for r in results}),
        scope=json.loads((pex / "extraction.json").read_text())["scope"],
        model_translation="HSPICE BSIM-CMG111 to Xyce BSIM-CMG110; VERSION, CAPMOD, COREMOD, PRWG omitted; see models.json",
        leakage_interpretation="Model-only numerical residual: gate tunneling and GIDL disabled; no physical leakage bound.",
        cases=results,
        nominal=summary,
        max_absolute_dc_residual_a=max(
            abs(r["models"][k]["leakage_a"]) for r in results for k in ("ideal", "rc")
        ),
        source_sha256={
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (
                Path(__file__),
                pex / "extraction.json",
                pex / "ideal.cir",
                pex / "rc.cir",
            )
        },
    )
    (out / "characterization.json").write_text(json.dumps(report, indent=2) + "\n")
    if not args.smoke:
        plot(out, results)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
