"""provenance.py — VENDORED COPY of DermArena/dataset_collection/provenance.py.
Kept identical; PMC-Patients is a separate repo so it cannot import DermArena
(that would be a circular cross-repo dependency). Sync if the DermArena original changes.

ORIGINAL DOCSTRING:
provenance.py — append-only per-row pipeline trace.

The idea: as a row moves down the pipeline, every step appends ONE entry
recording what it evaluated and decided. Reading `row['provenance']['pipeline_trace']`
top-to-bottom then replays the entire logic flow for that row — no archaeology
across intermediate files or sidecars.

Each entry is `{stage, action, ts, **detail}`:
  - stage  : which step ("filter_2", "leakage", "bind", "final_output_rdc", ...)
  - action : kept | dropped | transformed | flagged
  - ts     : ISO timestamp
  - detail : the decision — gate name, verdict, sidecar used, counts, etc.

Usage (every pipeline script):
    from provenance import stamp
    stamp(row, "final_output_rdc", "kept",
          gate="image_leakage_exam", sidecar="leakage_audit_dx_10krerun.jsonl",
          matched=True, verdict="no", mask_status="ok")

Design rules:
  - APPEND-ONLY. Never rewrite or reorder prior entries.
  - Log DECISIONS, not bulk data. The verdict fields already live on the row;
    the trace records that a gate was evaluated, its verdict, and the action.
  - PRESERVE on the way through. A step that reads one file and writes another
    must carry `provenance.pipeline_trace` forward (use `carry_forward`) before
    appending, or the trace truncates.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def stamp(row: dict, stage: str, action: str, **detail: Any) -> dict:
    """Append one provenance entry to row['provenance']['pipeline_trace']. Returns row.

    One call per pipeline STEP (bundled): pass all of that step's gate verdicts as
    **detail in a single entry, e.g.
        stamp(row, "leakage", "kept",
              case_leakage="no", result_leakage="no", image_leakage="no",
              zero_signal="no", image_leakage_exam="no")
    """
    entry = {"stage": stage, "action": action, "ts": _ts(), **detail}
    row.setdefault("provenance", {}).setdefault("pipeline_trace", []).append(entry)
    return row


def reject(row: dict, stage: str, reason: str, rejects_path: str | Path | None = None,
           **detail: Any) -> dict:
    """Stamp a DROP decision and (if rejects_path given) append the stamped row to a
    `*.rejects.jsonl`, so dropped rows stay traceable after they leave the pipeline.

    `reason` is the gate/condition that killed the row (e.g. "case_leakage==yes").
    Pass the same verdict **detail you would have stamped on a survivor.
    """
    stamp(row, stage, "dropped", reason=reason, **detail)
    if rejects_path is not None:
        p = Path(rejects_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def rejects_path_for(output_path: str | Path) -> Path:
    """Canonical rejects sidecar next to a stage's output: foo.jsonl -> foo.rejects.jsonl."""
    p = Path(output_path)
    return p.with_suffix(".rejects.jsonl")


def carry_forward(dst: dict, src: dict) -> dict:
    """Copy an existing pipeline_trace from `src` into `dst` so later stamps append
    rather than start fresh. Use when a step projects/derives a new row from a
    source row (e.g. final_output building a benchmark row from a case)."""
    trace = (src.get("provenance") or {}).get("pipeline_trace")
    if trace:
        dst.setdefault("provenance", {})["pipeline_trace"] = list(trace)
    return dst
