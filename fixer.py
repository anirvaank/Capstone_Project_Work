#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Fixer: move the first 'painting' move from setup → user for all sessions.

- For each <root>/<session_glob>/:
    - load setup_events.csv, user_events.csv
    - if setup not empty:
        - pop first row from setup
        - prepend to user
        - rewrite both CSVs
        - rebuild state.csv and state_grid.csv from NEW setup
        - patch meta.json counts (if present)
    - mirror the move in setup_events.json / user_events.json if present

Usage:
  python fix_first_paint_move.py 15.11.25_parsed_logs 17.11.25_parsed_logs \
         --session-glob "session_*" --dry-run

Remove --dry-run to apply changes.
"""

import os
import csv
import json
import argparse
from glob import glob

CSV_SETUP = "setup_events.csv"
CSV_USER  = "user_events.csv"
CSV_STATE = "state.csv"
CSV_GRID  = "state_grid.csv"
JSON_META = "meta.json"
JSON_SETUP= "setup_events.json"
JSON_USER = "user_events.json"

HEADER_EVENTS = ["move_type","row","column","time_ms","cell_before","cell_after"]

def read_csv_rows(path):
    rows = []
    if not os.path.exists(path): return rows
    with open(path, "r", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        for r in rd:
            rows.append({
                "move_type": r.get("move_type",""),
                "row": _to_int(r.get("row")),
                "column": _to_int(r.get("column")),
                "time_ms": _to_int(r.get("time_ms"), 0),
                "cell_before": _norm_cell(r.get("cell_before")),
                "cell_after":  _norm_cell(r.get("cell_after")),
            })
    return rows

def write_csv_rows(path, rows, header=HEADER_EVENTS, dry=False):
    if dry: return
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(header)
        for r in rows:
            wr.writerow([
                r.get("move_type",""),
                r.get("row","") or "",
                r.get("column","") or "",
                r.get("time_ms",0),
                "" if r.get("cell_before") in (None,"") else r["cell_before"],
                "" if r.get("cell_after")  in (None,"") else r["cell_after"],
            ])

def read_json(path):
    if not os.path.exists(path): return None
    with open(path,"r",encoding="utf-8") as f:
        return json.load(f)

def write_json(path, obj, dry=False):
    if dry: return
    with open(path,"w",encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def _to_int(v, default=None):
    try:
        if v is None or v=="":
            return default
        return int(v)
    except Exception:
        return default

def _norm_cell(s):
    # keep '' as empty, map ' ' (space) to ''
    if s is None: return ""
    s = str(s)
    return "" if s.strip()=="" else s

def apply_to_board(board, row, col, val):
    if row is None or col is None: return
    if val in ("", None): return
    r = int(row)-1; c = int(col)-1
    if 0 <= r < 9 and 0 <= c < 9:
        board[r][c] = val

def rebuild_state_files(session_dir, setup_rows, dry=False):
    # state.csv: only writes/overwrites with a value
    state_rows = []
    board = [[" "]*9 for _ in range(9)]
    for r in setup_rows:
        if r["move_type"] in ("write","overwrite") and r.get("cell_after"):
            state_rows.append([r["row"], r["column"], r["cell_after"], r.get("time_ms",0), r["move_type"]])
            apply_to_board(board, r["row"], r["column"], r["cell_after"])

    # write state.csv
    write_csv_rows(
        os.path.join(session_dir, CSV_STATE),
        [{"move_type":sr[4], "row":sr[0], "column":sr[1], "time_ms":sr[3], "cell_before":"", "cell_after":sr[2]} for sr in state_rows],
        header=["row","column","value","time_ms","move_type"], dry=dry
    )

    # write state_grid.csv ("" for blanks)
    if not dry:
        with open(os.path.join(session_dir, CSV_GRID),"w",newline="",encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow([f"c{j+1}" for j in range(9)])
            for r in range(9):
                wr.writerow([("" if board[r][c]==" " else board[r][c]) for c in range(9)])

def patch_json_lists(session_dir, moved_first, dry=False):
    # If JSON mirrors exist, pop first from setup JSON and prepend to user JSON
    sp = os.path.join(session_dir, JSON_SETUP)
    up = os.path.join(session_dir, JSON_USER)
    if not os.path.exists(sp) or not os.path.exists(up):
        return
    try:
        s_j = read_json(sp) or []
        u_j = read_json(up) or []
        if s_j:
            # Best-effort: move first JSON object too
            s_first = s_j.pop(0)
            # keep fields roughly aligned with CSV—we don't enforce schema
            u_j.insert(0, s_first)
            write_json(sp, s_j, dry)
            write_json(up, u_j, dry)
    except Exception:
        pass  # don't let JSON quirks block CSV fixes

def patch_meta_counts(session_dir, n_setup, n_user, n_state_rows, dry=False):
    mp = os.path.join(session_dir, JSON_META)
    meta = read_json(mp)
    if not isinstance(meta, dict):
        return
    meta["setup_events"] = n_setup
    meta["user_events"]  = n_user
    meta["state_rows"]   = n_state_rows
    write_json(mp, meta, dry)

def process_session(session_dir, dry=False, verbose=False):
    setup_p = os.path.join(session_dir, CSV_SETUP)
    user_p  = os.path.join(session_dir, CSV_USER)
    if not (os.path.exists(setup_p) and os.path.exists(user_p)):
        return (False, "missing CSVs")

    setup = read_csv_rows(setup_p)
    user  = read_csv_rows(user_p)

    if not setup:
        return (False, "no setup rows")

    # Move first setup row → user (prepend)
    moved = setup.pop(0)
    user.insert(0, moved)

    if verbose:
        print(f"[fix] {session_dir}: moved first setup row → user: {moved}")

    # Write back CSVs
    write_csv_rows(setup_p, setup, dry=dry)
    write_csv_rows(user_p,  user,  dry=dry)

    # Rebuild state.csv and state_grid.csv from NEW setup
    rebuild_state_files(session_dir, setup, dry=dry)

    # Patch JSON mirrors (best-effort)
    patch_json_lists(session_dir, moved, dry=dry)

    # Update meta.json counts (best-effort)
    # Recompute state_rows length to reflect new setup
    n_state_rows = sum(1 for r in setup if r["move_type"] in ("write","overwrite") and r.get("cell_after"))
    patch_meta_counts(session_dir, len(setup), len(user), n_state_rows, dry=dry)

    return (True, "ok")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+", help="Parsed log root(s), e.g., 15.11.25_parsed_logs 17.11.25_parsed_logs")
    ap.add_argument("--session-glob", default="session_*", help="Glob for session folders under each root")
    ap.add_argument("--dry-run", action="store_true", help="Show what would change; do not write files")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    total = fixed = skipped = 0
    for root in args.roots:
        pattern = os.path.join(root, args.session_glob)
        for sdir in sorted(glob(pattern)):
            if not os.path.isdir(sdir): continue
            total += 1
            ok, msg = process_session(sdir, dry=args.dry_run, verbose=args.verbose)
            if ok:
                fixed += 1
            else:
                skipped += 1
                if args.verbose:
                    print(f"[skip] {sdir}: {msg}")

    print(f"\nDone. Sessions seen={total}, fixed={fixed}, skipped={skipped}. "
          f"{'(dry-run)' if args.dry_run else ''}")

if __name__ == "__main__":
    main()
