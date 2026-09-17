# Experiments

Research scripts kept so the numbers in `docs/` can be reproduced. Concordance
itself doesn't use any of them, and they are rougher than the package code.

| Script | What it does |
| --- | --- |
| `evaluate.py` | Compares the old ordinal matcher (`anchor_ordinal.py`) with the boundary matcher across every paired book. Reads the Calibre and ABS databases directly, read-only. Needs `CALIBRE_ROOT` and `ABS_DB` (path to ABS's `absdatabase.sqlite`) |
| `classify.py` | Prototype classifier for non-content chapters (EPUB `epub:type`, guide entries and headings; ABS chapter titles). Not adopted: it helped less than matching boundaries did |
| `slicing_test.py` | Emissions with and without slicing, at batch sizes 4 and 1, one process per mode, comparing peak memory and word timings. Runs in the aligner image with `/work/params.json` |
| `bench.py` | ctc-forced-aligner benchmark: emissions, whole-chapter alignment, passage search and a negative control. Runs in a `python:3.12-slim` container with `/work` mounted |

Results and the reasoning behind them are in [`docs/design.md`](../../docs/design.md)
and [`docs/aligner.md`](../../docs/aligner.md).
