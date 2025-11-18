#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Parser for new [sudoku-permove] plugin logs (with userId, boot_preRevealIdxs, boot_current_state).

• Groups sessions by (userId, set, id).
• Captures boot snapshots and user events.
• Outputs per-session folders named: session_<NNN>__user=<userId>__set=<set>__id=<id>
  - setup.json              (boot snapshots)
  - user_events.csv/json    (normalized event rows)
  - initial_board.json      (from boot_current_state.rows if present)
  - meta.json
• Writes a top-level sessions_index.json listing all sessions.

USAGE (PowerShell):
  python parser_logs_v2.py "raw_logs/18.11.25_logs_v2.log" --out-dir parsed_v2 --only-user anir --ignore-id pluginandlogstests --debug
"""

import os, re, json, csv, argparse
from collections import defaultdict, Counter

# ---------- Regexes ----------
TAG_PERMOVE = re.compile(r"\[sudoku-permove\]")

# Lines (plugin):
RE_LOADED = re.compile(
    r"""\[sudoku-permove\]\s+loaded\s+id=(?P<id>\S+)\s+set=(?P<set>\S+)\s+w=(?P<w>\d+)\s+h=(?P<h>\d+).*?\buserId=(?P<user>\S+)""",
    re.I
)

RE_BOOT_PRI = re.compile(
    r"""\[sudoku-permove\]\s+boot_preRevealIdxs\b.*?\brows=(?P<rows>\[.*\])\s+userId=(?P<user>\S+)""",
    re.I
)

RE_BOOT_STATE = re.compile(
    r"""\[sudoku-permove\]\s+boot_current_state\b.*?\brows=(?P<rows>\[.*\])\s+userId=(?P<user>\S+)""",
    re.I
)

RE_SELECT = re.compile(
    r"""\[sudoku-permove\]\s+select(?:\s+r=(?P<r>\d+)\s+c=(?P<c>\d+))?.*?\bdt_ms=(?P<dt>\d+)\s+t_ms=(?P<t>\d+)\s+userId=(?P<user>\S+)""",
    re.I
)

RE_MOVE = re.compile(
    r"""\[sudoku-permove\]\s+move=(?P<m>\d+)\s+r=(?P<r>\d+)\s+c=(?P<c>\d+)\s+prev='(?P<prev>[^']*)'\s+next='(?P<next>[^']*)'\s+mode=(?P<mode>\w+)\s+dt_ms=(?P<dt>\d+)\s+t_ms=(?P<t>\d+)\s+userId=(?P<user>\S+)""",
    re.I
)

RE_MOVE_NODIFF = re.compile(
    r"""\[sudoku-permove\]\s+move=(?P<m>\d+)\s+no_diff\s+dt_ms=(?P<dt>\d+)\s+t_ms=(?P<t>\d+)\s+userId=(?P<user>\S+)""",
    re.I
)

RE_CLEARED = re.compile(
    r"""\[sudoku-permove\]\s+cleared\b.*?\bt_ms=(?P<t>\d+)""",
    re.I
)

# Non-plugin (server) lines sometimes include playId; we ignore for v2 (grouping is by user/set/id)

def _safe_json(s):
    try:
        return json.loads(s)
    except Exception:
        return None

def _cell_norm(x):
    # Treat space or empty as ""
    return "" if x is None or x == " " else str(x)

def _infer_move_type(prev, nxt, is_select=False):
    if is_select:
        return "select"
    prev = "" if prev in (None, " ") else str(prev)
    nxt  = "" if nxt  in (None, " ") else str(nxt)
    if prev == "" and nxt != "":
        return "write"
    if prev != "" and nxt == "":
        return "erase"
    if prev != "" and nxt != "" and prev != nxt:
        return "overwrite"
    return "write" if nxt != "" else "select"

def write_csv(path, rows, header):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)

def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def parse_file(log_path, out_dir, only_user=None, ignore_ids=None, debug=False):
    """
    Scan the file once, bucket lines by (userId, set, id), and build per-session outputs.
    """
    if ignore_ids is None:
        ignore_ids = set()

    # Collect all lines per (userId, set, id)
    buckets = defaultdict(list)
    counts  = Counter()

    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not TAG_PERMOVE.search(line):
                continue

            # Try to learn (user,set,id) from 'loaded' quickly
            m_loaded = RE_LOADED.search(line)
            if m_loaded:
                pid = m_loaded.group("id")
                pset = m_loaded.group("set")
                user = m_loaded.group("user")
                if only_user and user != only_user:
                    continue
                if pid in ignore_ids:
                    continue
                key = (user, pset, pid)
                buckets[key].append(line)
                counts["loaded"] += 1
                continue

            # If not a 'loaded' line, we still need the user & puzzle id:
            # Parse minimally to fish out userId first:
            # Try move/select/boot lines (they all end with userId=...)
            user = None
            for rx in (RE_MOVE, RE_MOVE_NODIFF, RE_SELECT, RE_BOOT_PRI, RE_BOOT_STATE):
                m = rx.search(line)
                if m:
                    user = m.group("user")
                    break

            if not user:
                # no user means we can't bucket reliably
                counts["skip_no_user"] += 1
                continue

            # Try to infer id/set from the same line (boot lines may not contain id/set)
            # If not present, we temporarily stash under a "pending" group and attach later
            # When we see the next LOADED for that user, we backfill. Simpler approach:
            # we'll place these lines into a "user:unknown" bucket for now.
            # Then during the second pass we reassign using nearest prior LOADED for that user.
            buckets[(user, None, None)].append(line)
            counts["user_no_setid"] += 1

    # Reattach "unknown" lines to a concrete (user,set,id) using latest LOADED seen before them (per user).
    # To do this, we re-scan lines in order and track last_loaded[(user)] = (set,id).
    reassigned = defaultdict(list)
    last_loaded = {}
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not TAG_PERMOVE.search(line):
                continue
            m_loaded = RE_LOADED.search(line)
            if m_loaded:
                pid = m_loaded.group("id"); pset = m_loaded.group("set"); user = m_loaded.group("user")
                if only_user and user != only_user: 
                    continue
                if pid in ignore_ids:
                    continue
                last_loaded[user] = (pset, pid)
                reassigned[(user, pset, pid)].append(line)
                continue
            # else: other line types → find user
            user = None
            for rx in (RE_MOVE, RE_MOVE_NODIFF, RE_SELECT, RE_BOOT_PRI, RE_BOOT_STATE):
                m = rx.search(line)
                if m:
                    user = m.group("user")
                    break
            if not user:
                continue
            if only_user and user != only_user:
                continue
            # map to last loaded (set,id) for this user if available
            if user in last_loaded:
                pset, pid = last_loaded[user]
                if pid in ignore_ids:
                    continue
                reassigned[(user, pset, pid)].append(line)
            else:
                counts["orphan_lines_no_loaded_yet"] += 1

    # Build sessions
    os.makedirs(out_dir, exist_ok=True)
    sessions_index = []
    sess_idx = 0

    for (user, pset, pid), lines in sorted(reassigned.items(), key=lambda kv: (kv[0][0], kv[0][1] or "", kv[0][2] or "")):
        if pset is None or pid is None:
            # still unknown — skip
            continue

        # Basic meta
        width = height = None
        setup = {
            "boot_preRevealIdxs": None,   # rows as bools if present
            "boot_current_state": None,   # rows as strings ("" for empty)
        }
        events = []  # normalized rows

        # Parse pass
        for ln in lines:
            m = RE_LOADED.search(ln)
            if m:
                width = int(m.group("w")); height = int(m.group("h"))
                continue

            m = RE_BOOT_PRI.search(ln)
            if m:
                rows_raw = m.group("rows")
                arr = _safe_json(rows_raw)
                if isinstance(arr, list):
                    setup["boot_preRevealIdxs"] = arr
                continue

            m = RE_BOOT_STATE.search(ln)
            if m:
                rows_raw = m.group("rows")
                arr = _safe_json(rows_raw)
                if isinstance(arr, list):
                    setup["boot_current_state"] = arr
                continue

            m = RE_SELECT.search(ln)
            if m:
                r = m.group("r"); c = m.group("c")
                row = int(r) if r else None
                col = int(c) if c else None
                dt = int(m.group("dt")); t = int(m.group("t"))
                events.append({
                    "move_type": "select",
                    "row": row, "column": col,
                    "time_ms": t, "dt_ms": dt,
                    "cell_before": "", "cell_after": "",
                    "mode": ""
                })
                continue

            m = RE_MOVE.search(ln)
            if m:
                prev = _cell_norm(m.group("prev"))
                nxt  = _cell_norm(m.group("next"))
                mvtype = _infer_move_type(prev, nxt, False)
                events.append({
                    "move_type": mvtype,
                    "row": int(m.group("r")),
                    "column": int(m.group("c")),
                    "time_ms": int(m.group("t")),
                    "dt_ms": int(m.group("dt")),
                    "cell_before": prev,
                    "cell_after": nxt,
                    "mode": m.group("mode")
                })
                continue

            m = RE_MOVE_NODIFF.search(ln)
            if m:
                # keep as a no-op note (still useful for timing)
                events.append({
                    "move_type": "no_diff",
                    "row": None, "column": None,
                    "time_ms": int(m.group("t")),
                    "dt_ms": int(m.group("dt")),
                    "cell_before": "", "cell_after": "",
                    "mode": ""
                })
                continue

            if RE_CLEARED.search(ln):
                # can be recorded as a special event if you like:
                # t = int(RE_CLEARED.search(ln).group("t"))
                # events.append({...})
                continue

        # Output
        sess_idx += 1
        sess_dir = os.path.join(
            out_dir,
            f"session_{sess_idx:03d}__user={user}__set={pset}__id={pid}"
        )
        os.makedirs(sess_dir, exist_ok=True)

        # initial board (if boot_current_state captured)
        initial_board = {"rows": setup["boot_current_state"], "width": width, "height": height}
        write_json(os.path.join(sess_dir, "initial_board.json"), initial_board)

        # preReveal mask (as given)
        write_json(os.path.join(sess_dir, "setup.json"), setup)

        # events CSV/JSON
        rows_csv = []
        for e in events:
            rows_csv.append([
                e["move_type"],
                "" if e["row"] is None else e["row"],
                "" if e["column"] is None else e["column"],
                e["time_ms"],
                e["cell_before"],
                e["cell_after"],
                e.get("mode",""),
                e.get("dt_ms","")
            ])
        write_csv(os.path.join(sess_dir, "user_events.csv"),
                  rows_csv,
                  ["move_type","row","column","time_ms","cell_before","cell_after","mode","dt_ms"])
        write_json(os.path.join(sess_dir, "user_events.json"), events)

        meta = {
            "userId": user,
            "set": pset,
            "id": pid,
            "width": width, "height": height,
            "n_events": len(events),
            "has_boot_state": setup["boot_current_state"] is not None,
            "has_boot_preRevealIdxs": setup["boot_preRevealIdxs"] is not None,
            "source_file": os.path.abspath(log_path),
            "session_dir": os.path.abspath(sess_dir),
        }
        write_json(os.path.join(sess_dir, "meta.json"), meta)
        sessions_index.append(meta)

    write_json(os.path.join(out_dir, "sessions_index.json"), sessions_index)
    return sessions_index


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log", help="Path to the raw log file")
    ap.add_argument("--out-dir", default="parsed_v2", help="Output directory")
    ap.add_argument("--only-user", help="Only include this userId")
    ap.add_argument("--ignore-id", action="append", help="Puzzle id to ignore (can repeat)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.log):
        raise FileNotFoundError(args.log)

    ignore = set(args.ignore_id) if args.ignore_id else set()
    sessions = parse_file(args.log, args.out_dir, only_user=args.only_user, ignore_ids=ignore, debug=args.debug)
    print(f"Done. Sessions written: {len(sessions)}")
    if args.debug:
        for s in sessions:
            print(f" - {s['userId']} {s['set']}/{s['id']} events={s['n_events']} "
                  f"boot_state={s['has_boot_state']} preReveal={s['has_boot_preRevealIdxs']}")


if __name__ == "__main__":
    main()
