# Experimental CPCell GT2N logic batch

Frozen NAND4, NOR4, AOI221 and OAI221 views for w31/LVT, 14 nm gates,
three-sheet devices and a 144 nm standard-cell row. Devices are uniformly
sized; the names intentionally make no x1 drive-strength claim.

| Cells | Width × height | Area |
| --- | --- | --- |
| NAND4, NOR4 | 252 × 144 nm | 0.036288 µm² |
| AOI221, OAI221 | 294 × 144 nm | 0.042336 µm² |

Load `cells.lef` alongside GT2N's technology LEF and released w31/LVT LEF
(which defines the `gt2_6t` site). Load `cells_tt.lib` alongside the compatible
released TT library. `cells.gds`, `cells.cdl` and `cells.v` provide the other
physical, transistor and functional views. This extension has distinct cell
names and does not replace the released libraries.

## Validation and limits

All four cells pass transistor LVS individually and in mirrored 2 × 2 arrays.
DRC retains `GATE.2` and `SDCON.ACT.1` findings; counts and full databases are
under `verification/`. These checks do not claim zero DRC findings or signoff.
All 96 static SPICE vectors and 900 propagation-delay measurements passed.
OpenROAD loaded the LEF/Liberty and evaluated timing; Yosys linked the cells
and checked the functional Verilog. The CPCell suite passed 41 tests and 66
subtests. Evidence is archived in `verification/`.

The Liberty is **experimental schematic-only TT, 0.7 V, 25 °C**. It has no
extracted wiring RC, power tables, additional corners or variation models.
Input slew is 10/30/80 ps (20–80%); load is 0.2/1/3 fF. Each timing entry is
the worst measured sensitizing assignment. See
[generation notes](verification/generation-notes.md) for methodology.

## Provenance and regeneration

- CPCellUCSD: `8aa150b88d377eb06ceab72f9ea261aaf9b068ac` (branch `gt2n`).
- GT2N base: `699e2c10b2b494a120745515f59df8815f4f0175`.
- Local chipforge_gt2n verification helpers: `2d94a9ca96a6186d922d2ccb411121d450284946`. That checkout
  has no configured remote; regeneration with `--spice` requires access to it.
- `manifest.json` records hashes for every snapshot payload file and source
  revisions. Original reports retain the generating machine's absolute paths.

From the pinned CPCellUCSD checkout, with the pinned helper checkout beside it:

```bash
PYTHONDONTWRITEBYTECODE=1 ../chipforge_gt2n/.venv/bin/python \
  examples/gt2n_logic/reproduce.py --spice
```

Set `GT2N_ROOT` if the PDK is elsewhere. The helper flow expects the DRC deck
at `Openroad_example/gt2n_openroad_run/OpenROAD-flow-scripts/flow/platforms/gt2n/gt2n.lydrc` within the PDK. This deck was present locally but untracked
at the GT2N base revision; its exact contents are saved as
`verification/decks/gt2n.lydrc`. In a fresh PDK checkout, provision that file
at the expected path before running the helper flow. The exact LVS deck is
also archived. Both decks can be run directly with KLayout using `in_gds`,
`topcell`, `report_file`, and (for LVS) `cdl_file`; DRC uses `drc_mode=full`.
Self-contained Xyce benches are saved under each cell's verification folder.

The solver may choose a different equally optimal layout on regeneration.
`solver/config/` and `solver/result/` preserve the exact solutions exported
in this snapshot. `OPTIMAL` applies to the configured model and objective.
