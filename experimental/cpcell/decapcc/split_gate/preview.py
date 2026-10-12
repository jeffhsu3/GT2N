"""Compare actual released, full-dummy, and staggered W31 GDS geometry."""

import os
from pathlib import Path

import klayout.db as db


def main():
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/gt2n_decapcc_mpl")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch, Polygon

    root = Path(__file__).resolve().parent
    pdk = root.parents[3]
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
    entries = [
        (
            pdk / "gds/gt2_6t_std_cells_w31_lvt.gds",
            "gt2_6t_decapcc_w31_lvt",
            "Released: 84 × 144 nm\nVG overlaps ACT",
        ),
        (
            root.parent / "w31/lvt/cell.gds",
            "gt2_6t_decapcc_cp_w31_lvt",
            "Full dummy: 168 × 144 nm\nChecked prototype",
        ),
        (
            root / "w31_126x144/lvt/cell.gds",
            "gt2_6t_decapcc_sg_w31_lvt",
            "Staggered: 126 × 144 nm\n25% narrower; checked candidate",
        ),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(13, 5.6), constrained_layout=True)
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
        width = db.Region(cell.shapes(layout.layer(235, 250))).bbox().width()
        width *= layout.dbu * 1000
        ax.plot([0, width, width, 0, 0], [0, 0, 144, 144, 0], "k--", lw=0.8)
        ax.set(
            xlim=(-12, 184),
            ylim=(-22, 166),
            aspect="equal",
            title=title,
            xlabel="x (nm)",
            ylabel="y (nm)",
        )
        ax.spines[["top", "right"]].set_visible(False)
    fig.legend(
        handles=[Patch(color=c, alpha=a, label=n) for _, n, c, a in layers],
        loc="outside lower center",
        ncol=10,
        frameon=False,
    )
    fig.suptitle(
        "W31 cross-coupled decap — same devices, same 144 nm row height", fontsize=14
    )
    fig.savefig(root / "w31_126x144/comparison.png", dpi=180)


if __name__ == "__main__":
    main()
