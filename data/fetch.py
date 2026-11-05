"""QF5313 Q3 — Phase 2: raw data acquisition from AkShare `macro_china_*` interfaces.

Contract (AGENTS.md §3):
- argparse-driven, `--seed 42` default;
- raw data persisted under `data/raw/`, one file per interface, never deleted;
- offline grading support: `--offline` verifies every committed raw snapshot
  exists and matches its recorded SHA-256;
- failures are reported verbatim, never silently skipped;
- no modelling, no cleaning here (cleaning lives in `data/build_panel.py`).

Usage:
    uv run python data/fetch.py --seed 42            # live fetch (idempotent)
    uv run python data/fetch.py --offline            # verify snapshots only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw"
MANIFEST_PATH = RAW_DIR / "manifest.json"
console = Console()

# ---------------------------------------------------------------------------
# Interface selection (per results/data_source_audit.md §8 and group decisions):
# fresh Eastmoney + Sina interfaces only; jin10 used solely as a cross-check
# source for the hgjck unit resolution (never as panel data); mofcom TSF kept
# with a documented gap; NBS / Caixin / market-margin / insurance excluded.
# ---------------------------------------------------------------------------
INTERFACES: list[dict] = [
    # --- quarterly targets -------------------------------------------------
    {"name": "gdp", "ak": "macro_china_gdp", "role": "panel-quarterly",
     "note": "Quarterly GDP absolute value + YoY by industry; kept quarterly."},
    {"name": "enterprise_boom", "ak": "macro_china_enterprise_boom_index",
     "role": "panel-quarterly",
     "note": "Quarterly boom index; 信心指数 NaN in recent quarters (audit §4.20)."},
    # --- prices -------------------------------------------------------------
    {"name": "cpi", "ak": "macro_china_cpi", "role": "panel",
     "note": "\"当月\" is an implied index (100+yoy); use 同比 columns."},
    {"name": "ppi", "ak": "macro_china_ppi", "role": "panel"},
    {"name": "cgpi", "ak": "macro_china_qyspjg", "role": "panel",
     "note": "PBOC corporate goods price index; check rebasing history."},
    # --- activity surveys / quantities --------------------------------------
    {"name": "pmi", "ak": "macro_china_pmi", "role": "panel",
     "note": "Label source (制造业-指数). \"同比\" columns meaningless for PMI."},
    {"name": "industrial_va", "ak": "macro_china_gyzjz", "role": "panel",
     "note": "发布时间 column is a period marker, NOT a release date (audit §5.3)."},
    {"name": "retail_sales", "ak": "macro_china_consumer_goods_retail",
     "role": "panel", "note": "January absent (Jan–Feb combined)."},
    {"name": "fai", "ak": "macro_china_gdzctz", "role": "panel",
     "note": "当月 derived from cumulative; 2026-09 YoY sanity-checked in build step."},
    {"name": "consumer_confidence", "ak": "macro_china_xfzxx", "role": "panel",
     "note": "Source-code bug overwrites 预期指数; only 信心指数-指数值 usable."},
    {"name": "lpi", "ak": "macro_china_lpi_index", "role": "panel"},
    {"name": "electricity", "ak": "macro_china_society_electricity", "role": "panel",
     "note": "Consumption (万千瓦时), not generation — approved substitution."},
    {"name": "traffic", "ak": "macro_china_society_traffic_volume", "role": "panel",
     "note": "Long format; 合计 rows only; column-name typos normalised in build."},
    # --- money & credit ------------------------------------------------------
    {"name": "money_supply", "ak": "macro_china_money_supply", "role": "panel",
     "note": "M1 definition change 2024-01 — structural break documented."},
    {"name": "new_loans", "ak": "macro_china_new_financial_credit", "role": "panel"},
    {"name": "tsf", "ak": "macro_china_shrzgm", "role": "panel",
     "note": "mofcom source STALE (ends 2026-04); kept with a documented gap."},
    {"name": "central_bank_balance", "ak": "macro_china_central_bank_balance",
     "role": "panel", "note": "Base money (储备货币) source."},
    # --- rates ----------------------------------------------------------------
    {"name": "lpr", "ak": "macro_china_lpr", "role": "panel",
     "note": "LPR1Y/5Y post-2019; RATE_1/RATE_2 are the old benchmark (different regime)."},
    {"name": "shibor", "ak": "macro_china_shibor_all", "role": "panel",
     "note": "Daily O/N…1Y tenors; monthly means in build step."},
    # --- external sector --------------------------------------------------------
    {"name": "customs", "ak": "macro_china_hgjck", "role": "panel",
     "note": "Unit unlabelled — resolved via jin10 trade-balance cross-check."},
    {"name": "trade_balance_xcheck", "ak": "macro_china_trade_balance",
     "role": "cross-check",
     "note": "jin10, frozen ≤2025-08: cross-check ONLY, never panel data."},
    {"name": "fx_gold", "ak": "macro_china_fx_gold", "role": "panel",
     "note": "FX reserves (亿美元); gold unit UNVERIFIED → gold excluded from panel."},
    # --- fiscal ---------------------------------------------------------------
    {"name": "fiscal_revenue", "ak": "macro_china_czsr", "role": "panel"},
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {"version": 1, "retrieved_utc": None, "interfaces": {}}


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def archive_existing(path: Path) -> str | None:
    """Never delete data: keep a checksum-labelled backup before replacing."""
    if not path.exists():
        return None
    previous = sha256_file(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.stem}.bak-{stamp}{path.suffix}")
    path.replace(backup)
    console.print(
        f"[yellow]replaced[/yellow] {path.name} (prior sha256 {previous[:12]}… "
        f"kept as {backup.name})"
    )
    return previous


def fetch_one(spec: dict, retries: int = 2) -> dict:
    """Fetch a single interface; return a manifest entry (status ok/failed)."""
    import akshare as ak

    name, fn_name = spec["name"], spec["ak"]
    entry: dict = {
        "interface": fn_name,
        "role": spec["role"],
        "note": spec.get("note", ""),
        "file": f"{name}.csv",
    }
    fn = getattr(ak, fn_name)

    last_error = None
    for attempt in range(1, retries + 2):
        try:
            df = fn()
            if df is None or len(df) == 0:
                raise ValueError("interface returned an empty frame")
            dest = RAW_DIR / f"{name}.csv"
            archive_existing(dest)
            df.to_csv(dest, index=False, encoding="utf-8-sig")
            first = df.iloc[:, 0].astype(str).tolist()
            entry.update(
                {
                    "status": "ok",
                    "attempt": attempt,
                    "sha256": sha256_file(dest),
                    "n_rows": int(len(df)),
                    "n_cols": int(df.shape[1]),
                    "columns": [str(c) for c in df.columns],
                    "first_value_sample": first[:2],
                }
            )
            return entry
        except Exception as exc:  # noqa: BLE001 — audit policy: report verbatim
            last_error = repr(exc)
            if attempt <= retries:
                time.sleep(2 * attempt)
    entry.update({"status": "failed", "error": last_error})
    return entry


def offline_verify(manifest: dict) -> bool:
    """Grading support: every manifest entry must be committed and checksum-match."""
    ok = True
    table = Table(title="Offline snapshot verification")
    table.add_column("file")
    table.add_column("status")
    table.add_column("sha256 (12)")
    for name, entry in manifest["interfaces"].items():
        path = RAW_DIR / entry["file"]
        if not path.exists():
            table.add_row(entry["file"], "[red]MISSING[/red]", "-")
            ok = False
        elif sha256_file(path) != entry.get("sha256"):
            table.add_row(entry["file"], "[red]CHECKSUM MISMATCH[/red]", "-")
            ok = False
        else:
            table.add_row(entry["file"], "[green]ok[/green]", entry["sha256"][:12])
    console.print(table)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42, help="global seed (unused)")
    parser.add_argument(
        "--offline", action="store_true",
        help="verify committed snapshots exist and checksum-match; no network calls",
    )
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()

    if args.offline:
        ok = offline_verify(manifest)
        console.print(
            Panel(
                "[green]all snapshots verified[/green]" if ok
                else "[red]snapshot verification FAILED[/red]",
                title="--offline",
            )
        )
        return 0 if ok else 1

    console.print(
        Panel(
            f"{len(INTERFACES)} interfaces selected "
            f"(Eastmoney/Sina backbone + mofcom TSF-gap + jin10 cross-checks); "
            f"excluded by decision: NBS, Caixin, insurance, market margins.",
            title=f"QF5313 Q3 raw fetch — seed {args.seed}",
        )
    )

    manifest["retrieved_utc"] = datetime.now(timezone.utc).isoformat()
    manifest.setdefault("global_seed", args.seed)
    n_ok = n_fail = 0
    for spec in INTERFACES:
        console.print(f"[cyan]→[/cyan] {spec['name']} ({spec['ak']})", end=" ")
        entry = fetch_one(spec, retries=args.retries)
        manifest["interfaces"][spec["name"]] = entry
        if entry["status"] == "ok":
            n_ok += 1
            console.print(
                f"[green]ok[/green] {entry['n_rows']}×{entry['n_cols']} "
                f"→ {entry['file']}"
            )
        else:
            n_fail += 1
            console.print(f"[red]FAILED[/red] {entry['error']}")
    save_manifest(manifest)

    if n_fail:
        console.print(
            Panel(
                f"{n_fail} interface(s) FAILED — verbatim errors recorded in "
                f"{MANIFEST_PATH}. Failed interfaces must either be fixed or "
                "excluded with documentation before grading (offline grading "
                "requires a committed snapshot per used interface).",
                title="Fetch summary", border_style="red",
            )
        )
    else:
        console.print(
            Panel(
                f"{n_ok}/{len(INTERFACES)} interfaces fetched → data/raw/ "
                f"(manifest: {MANIFEST_PATH.name})",
                title="Fetch summary", border_style="green",
            )
        )
    return 0 if n_fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
