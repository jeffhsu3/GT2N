"""Plot original and generated W31 geometry from their actual GDS polygons."""

import argparse
import os
from pathlib import Path

import klayout.db as db


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--pdk-root", type=Path, default=Path(__file__).resolve().parents[3]
    )
    args = parser.parse_args()
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/gt2n_decapcc_mpl")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon, Patch

    layers = [
        (8, "BPR", "#9eadd4", 0.6),
        (2, "ACT", "#81bd80", 0.7),
        (3, "GATE", "#e5b458", 0.6),
        (4, "DUMMY", "#d1d5db", 0.8),
        (5, "GCUT", "#ffffff", 1),
        (10, "SDCON", "#515b6d", 0.5),
        (9, "VBPR", "#264c91", 0.95),
        (20, "M0", "#70b7d6", 0.65),
        (25, "M1", "#b998cf", 0.65),
        (12, "VG", "#d34942", 0.95),
    ]
    fig, axes = plt.subplots(1, 2, figsize=(11, 6), constrained_layout=True)
    entries = [
        (
            args.pdk_root / "gds/gt2_6t_std_cells_w31_lvt.gds",
            "gt2_6t_decapcc_w31_lvt",
            "Released W31: 84 × 144 nm",
        ),
        (
            args.output_dir / "lvt/cell.gds",
            "gt2_6t_decapcc_cp_w31_lvt",
            "CPCell W31: 168 × 144 nm",
        ),
    ]
    for ax, (path, name, title) in zip(axes, entries):
        layout = db.Layout()
        layout.read(str(path))
        cell = layout.cell(name)
        for number, _, color, alpha in layers:
            region = db.Region(cell.begin_shapes_rec(layout.layer(number, 0)))
            for poly in region.each():
                points = [
                    (p.x * layout.dbu * 1000, p.y * layout.dbu * 1000)
                    for p in poly.each_point_hull()
                ]
                ax.add_patch(
                    Polygon(
                        points,
                        facecolor=color,
                        edgecolor=color,
                        alpha=alpha,
                        linewidth=0.6,
                    )
                )
        boundary = db.Region(cell.shapes(layout.layer(235, 250))).bbox()
        width = boundary.width() * layout.dbu * 1000
        ax.plot([0, width, width, 0, 0], [0, 0, 144, 144, 0], "k--", lw=0.8)
        ax.set(
            xlim=(-12, 184),
            ylim=(-22, 168),
            aspect="equal",
            title=title,
            xlabel="x (nm)",
            ylabel="y (nm)",
        )
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].annotate(
        "VG overlaps ACT by 4 nm",
        xy=(42, 27),
        xytext=(10, -20),
        color="#b52c26",
        fontsize=9,
        arrowprops=dict(arrowstyle="->", color="#b52c26"),
    )
    axes[1].annotate(
        "VG at y = 66–78 nm\n9 nm ACT clearance",
        xy=(42, 72),
        xytext=(10, 156),
        fontsize=9,
        arrowprops=dict(arrowstyle="->", color="#333333"),
    )
    fig.legend(
        handles=[
            Patch(color=color, alpha=alpha, label=name)
            for _, name, color, alpha in layers
        ],
        loc="outside lower center",
        ncol=10,
        frameon=False,
    )
    fig.suptitle(
        "Same cross-coupled NMOS/PMOS pair, separate gate columns", fontsize=15
    )
    fig.savefig(args.output_dir / "comparison.png", dpi=180)


if __name__ == "__main__":
    main()
