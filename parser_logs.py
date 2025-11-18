#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parser_logs.py

Parses Sudoku client/server logs (plain .log or .log.gz) that contain:
  [Log:/sudoku] - [sudoku-permove] loaded ...
  [Log:/sudoku] - [sudoku-permove] move=... r=... c=... prev=' ' next='6' ...
  [Log:/sudoku] - [sudoku-permove] select r=... c=... ...
and server lines:
  COMPLETED Request ... "playId":"<64-hex>"

Features
- Sessionize by "loaded" lines (handles multiple reloads/games per file).
- Split setup vs user inside each session (COMPLETED boundary, else dt gap).
- NEW: Ignore sessions whose puzzle `id` matches any of the --ignore-id/--ignore-ids.
- Emits per-session artifacts under <out_dir>/session_###_(playId[:8])/:
  * setup_events.csv, user_events.csv  (move_type,row,column,time_ms,cell_before,cell_after)
  * state.csv (setup writes/overwrites), state_grid.csv (9x9)
  * setup_events.json, user_events.json, meta.json

Usage:
  python parser_logs.py 17.11.25_logs.log --out-dir out_sessions --debug
  python parser_logs.py pmm--...25.log.gz --out-dir out_sessions --ignore-id pluginandlogstests
"""

import os, re, csv, json, gzip, argparse
from dataclasses import dataclass, field
from typing import List, Optional, Set
from collections import Counter

# ---------- Regexes ----------
TAG_PERMOVE = re.compile(r"\[sudoku-permove\]")
TAG_CLIENT  = re.compile(r"\[Log:/sudoku\]\s*-\s*\[sudoku-permove\]")

RE_LOADED   = re.compile(r"\[sudoku-permove\]\s+loaded\b", re.I)

RE_MOVE = re.compile(
    r"""\[sudoku-permove\]\s+move=(?P<move_id>\d+)
        (?:\s+r=(?P<r>\d+)\s+c=(?P<c>\d+))?
        (?:\s+prev='(?P<prev>[^']*)'\s+next='(?P<next>[^']*)')?
        (?:\s+mode=(?P<mode>\w+))?
        .*?\bdt_ms=(?P<dt_ms>\d+)\s+t_ms=(?P<t_ms>\d+)
    """, re.VERBOSE)

RE_SELECT = re.compile(
    r"""\[sudoku-permove\]\s+select
        (?:\s+r=(?P<r>\d+)\s+c=(?P<c>\d+))?
        .*?\bdt_ms=(?P<dt_ms>\d+)\s+t_ms=(?P<t_ms>\d+)
    """, re.VERBOSE)

RE_CLEARED = re.compile(r"""\[sudoku-permove\]\s+cleared\b.*?\bt_ms=(?P<t_ms>\d+)""")

# tokens often present on the same line
RE_PLAYLINE       = re.compile(r"\bplayId:\s*([0-9a-f]{64})", re.I)
RE_UIDLINE        = re.compile(r"\buid:\s*([^\s]+)")
RE_USERID_EQUALS  = re.compile(r"\buserId=([^\s]+)")
RE_COMPLETED      = re.compile(r"""COMPLETED Request.*?"playId":"([0-9a-f]{64})""", re.I)

# puzzle id / series tokens (both '=' and ':' styles appear in your logs)
RE_ID_EQ          = re.compile(r"\bid=([^\s]+)")
RE_ID_COLON       = re.compile(r"\bid:\s*([^\s]+)")
RE_SET_EQ         = re.compile(r"\bset=([^\s]+)")
RE_SERIES_COLON   = re.compile(r"\bseries:\s*([^\s]+)")

# ---------- Helpers ----------
def open_maybe_gz(path):
    return gzip.open(path, "rt", encoding="utf-8", errors="ignore") if path.endswith(".gz") \
           else open(path, "r", encoding="utf-8", errors="ignore")

def infer_move_type(prev: Optional[str], nxt: Optional[str], is_select: bool) -> str:
    if is_select:
        return "select"
    p = "" if prev in (None, " ") else prev
    n = "" if nxt  in (None, " ") else nxt
    if p == "" and n != "": return "write"
    if p != "" and n == "": return "erase"
    if p != "" and n != "" and p != n: return "overwrite"
    return "write" if n != "" else "select"

def empty_board():
    return [[" "]*9 for _ in range(9)]

def apply_event(board, ev):
    if ev["move_type"] == "select":
        return
    r, c = ev.get("row"), ev.get("column")
    if r is None or c is None: return
    v = ev.get("cell_after")
    board[r-1][c-1] = " " if (v in (None, "")) else v

# ---------- Data structures ----------
@dataclass
class Event:
    move_type: str
    row: Optional[int]
    column: Optional[int]
    time_ms: int
    dt_ms: int
    cell_before: Optional[str]
    cell_after: Optional[str]
    mode: Optional[str]
    raw: str
    lineno: int

@dataclass
class Session:
    idx: int
    start_lineno: int
    play_id: Optional[str] = None
    uid: Optional[str] = None
    user_id: Optional[str] = None
    puzzle_id: Optional[str] = None  # NEW
    series: Optional[str] = None     # NEW
    events: List[Event] = field(default_factory=list)
    completed_at_lineno: Optional[int] = None
    first_permove_skipped: bool = False
    ignored: bool = False            # NEW

# ---------- Parser ----------
def parse_file(
    log_path: str,
    out_dir: str,
    accept_nonlog: bool = False,
    gap_ms: int = 1000,
    debug: bool = False,
    max_lines: Optional[int] = None,
    ignore_ids: Optional[Set[str]] = None,
):
    os.makedirs(out_dir, exist_ok=True)
    counts = Counter()
    reasons = Counter()
    ignore_ids = ignore_ids or set()

    sessions: List[Session] = []
    cur: Optional[Session] = None

    def is_permove_line(line: str) -> bool:
        return bool(TAG_PERMOVE.search(line)) if accept_nonlog else bool(TAG_CLIENT.search(line))

    with open_maybe_gz(log_path) as f:
        for lineno, line in enumerate(f, 1):
            if max_lines and lineno > max_lines:
                break

            # Start of a new session
            if is_permove_line(line) and RE_LOADED.search(line):
                cur = Session(idx=len(sessions), start_lineno=lineno)
                # capture playId/uid/puzzle id/series on this same line if available
                mp = RE_PLAYLINE.search(line); mu = RE_UIDLINE.search(line)
                if mp: cur.play_id = mp.group(1)
                if mu: cur.uid = mu.group(1)
                meq = RE_USERID_EQUALS.search(line)
                if meq: cur.user_id = meq.group(1)

                mid = RE_ID_EQ.search(line) or RE_ID_COLON.search(line)
                if mid: cur.puzzle_id = mid.group(1)
                mset = RE_SET_EQ.search(line) or RE_SERIES_COLON.search(line)
                if mset: cur.series = mset.group(1)

                # mark ignored if puzzle_id matches
                if cur.puzzle_id and cur.puzzle_id in ignore_ids:
                    cur.ignored = True
                    counts["sessions_ignored"] += 1
                sessions.append(cur)
                counts["sessions_started"] += 1
                if debug:
                    print(f"[session] start idx={cur.idx} @line={lineno} playId={cur.play_id} uid={cur.uid} "
                          f"id={cur.puzzle_id} series={cur.series} ignored={cur.ignored}")
                continue

            if cur and RE_COMPLETED.search(line):
                pid = RE_COMPLETED.search(line).group(1)
                if (cur.play_id is None) or (pid == cur.play_id):
                    cur.completed_at_lineno = lineno
                continue

            if not is_permove_line(line):
                counts["non_permove_lines"] += 1
                continue

            # If we see permove before any "loaded", create implicit session
            if cur is None:
                cur = Session(idx=len(sessions), start_lineno=lineno)
                sessions.append(cur)
                counts["sessions_started"] += 1

            # backfill ids if missing
            if cur.play_id is None:
                mp = RE_PLAYLINE.search(line)
                if mp: cur.play_id = mp.group(1)
            if cur.uid is None:
                mu = RE_UIDLINE.search(line)
                if mu: cur.uid = mu.group(1)
            if cur.user_id is None:
                meq = RE_USERID_EQUALS.search(line)
                if meq: cur.user_id = meq.group(1)
            if cur.puzzle_id is None:
                mid = RE_ID_EQ.search(line) or RE_ID_COLON.search(line)
                if mid:
                    cur.puzzle_id = mid.group(1)
                    if cur.puzzle_id in ignore_ids:
                        cur.ignored = True
                        counts["sessions_ignored"] += 1
                        if debug:
                            print(f"[session] idx={cur.idx} marked ignored later (id={cur.puzzle_id})")
            if cur.series is None:
                mset = RE_SET_EQ.search(line) or RE_SERIES_COLON.search(line)
                if mset: cur.series = mset.group(1)

            # Switch session if playId suddenly changes mid-stream
            mp_any = RE_PLAYLINE.search(line)
            if mp_any and cur.play_id and mp_any.group(1) != cur.play_id:
                cur = Session(idx=len(sessions), start_lineno=lineno, play_id=mp_any.group(1))
                mu = RE_UIDLINE.search(line)
                if mu: cur.uid = mu.group(1)
                meq = RE_USERID_EQUALS.search(line)
                if meq: cur.user_id = meq.group(1)
                sessions.append(cur)
                if debug:
                    print(f"[session] split due to playId change → idx={cur.idx} @line={lineno} playId={cur.play_id}")

            # Parse events (skip 'cleared' as move row)
            if RE_CLEARED.search(line):
                counts["cleared_seen"] += 1
                continue

            m_move = RE_MOVE.search(line)
            m_sel  = RE_SELECT.search(line)
            if not (m_move or m_sel):
                reasons["no_move_select_match"] += 1
                continue

            # Skip first permove per session (plugin init noise)
            if not cur.first_permove_skipped:
                cur.first_permove_skipped = True
                reasons["first_permove_skipped_per_session"] += 1
                continue

            # If session is ignored, don't accumulate events
            if cur.ignored:
                continue

            # Carry forward last r/c if missing
            last_r = cur.events[-1].row if cur.events else None
            last_c = cur.events[-1].column if cur.events else None

            if m_move:
                r = m_move.group("r"); c = m_move.group("c")
                r_i = int(r) if r else last_r
                c_i = int(c) if c else last_c
                prev = m_move.group("prev"); nxt = m_move.group("next")
                ev = Event(
                    move_type  = infer_move_type(prev, nxt, False),
                    row        = r_i,
                    column     = c_i,
                    time_ms    = int(m_move.group("t_ms")),
                    dt_ms      = int(m_move.group("dt_ms")),
                    cell_before= None if (prev in (None, " ")) else prev,
                    cell_after = None if (nxt  in (None, " ")) else nxt,
                    mode       = m_move.group("mode"),
                    raw        = line.strip()[:700],
                    lineno     = lineno,
                )
            else:
                r = m_sel.group("r"); c = m_sel.group("c")
                r_i = int(r) if r else last_r
                c_i = int(c) if c else last_c
                ev = Event(
                    move_type  = "select",
                    row        = r_i,
                    column     = c_i,
                    time_ms    = int(m_sel.group("t_ms")),
                    dt_ms      = int(m_sel.group("dt_ms")),
                    cell_before= None, cell_after=None,
                    mode       = None,
                    raw        = line.strip()[:700],
                    lineno     = lineno,
                )

            cur.events.append(ev)
            counts["permove_events"] += 1

    # ---------- Split + write per session ----------
    def wcsv(path, header, rows):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(header); w.writerows(rows)

    def wjson(path, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    index = []
    for s in sessions:
        # skip ignored or empty sessions
        if s.ignored or not s.events:
            continue

        # Determine cut: COMPLETED boundary (first event after), else first dt gap
        cut_at = None
        if s.completed_at_lineno is not None:
            for i, ev in enumerate(s.events):
                if ev.lineno > s.completed_at_lineno:
                    cut_at = i; break
        if cut_at is None:
            for i in range(1, len(s.events)):
                if s.events[i].dt_ms >= gap_ms:
                    cut_at = i; break

        setup_ev = s.events[:cut_at] if cut_at is not None else []
        user_ev  = s.events[cut_at:] if cut_at is not None else s.events

        # Build starting board from setup
        board0 = empty_board()
        for ev in setup_ev:
            apply_event(board0, {
                "move_type": ev.move_type,
                "row": ev.row, "column": ev.column,
                "cell_after": ev.cell_after
            })

        pid_short = (s.play_id or "noid")[:8]
        sdir = os.path.join(out_dir, f"session_{s.idx:03d}_{pid_short}")
        os.makedirs(sdir, exist_ok=True)

        header = ["move_type","row","column","time_ms","cell_before","cell_after"]
        def rowify(ev: Event):
            return [
                ev.move_type,
                ev.row or "",
                ev.column or "",
                ev.time_ms,
                "" if ev.cell_before is None else ev.cell_before,
                "" if ev.cell_after  is None else ev.cell_after,
            ]

        wcsv(os.path.join(sdir, "setup_events.csv"), header, [rowify(e) for e in setup_ev])
        wcsv(os.path.join(sdir, "user_events.csv"),  header, [rowify(e) for e in user_ev])

        state_rows = [
            [e.row, e.column, e.cell_after, e.time_ms, e.move_type]
            for e in setup_ev
            if e.move_type in ("write","overwrite") and e.cell_after
        ]
        wcsv(os.path.join(sdir, "state.csv"),
             ["row","column","value","time_ms","move_type"], state_rows)

        grid_rows = [[("" if c==" " else c) for c in row] for row in board0]
        wcsv(os.path.join(sdir, "state_grid.csv"),
             [f"c{j+1}" for j in range(9)], grid_rows)

        wjson(os.path.join(sdir, "setup_events.json"), [e.__dict__ for e in setup_ev])
        wjson(os.path.join(sdir, "user_events.json"),  [e.__dict__ for e in user_ev])
        wjson(os.path.join(sdir, "meta.json"), {
            "session_idx": s.idx,
            "start_lineno": s.start_lineno,
            "playId": s.play_id,
            "uid": s.uid,
            "user_id": s.user_id,
            "puzzle_id": s.puzzle_id,
            "series": s.series,
            "completed_at_lineno": s.completed_at_lineno,
            "setup_events": len(setup_ev),
            "user_events": len(user_ev),
            "state_rows": len(state_rows),
        })

        index.append({
            "session_idx": s.idx,
            "playId": s.play_id,
            "uid": s.uid,
            "user_id": s.user_id,
            "puzzle_id": s.puzzle_id,
            "series": s.series,
            "setup_events": len(setup_ev),
            "user_events": len(user_ev),
        })

    with open(os.path.join(out_dir, "sessions_index.json"), "w", encoding="utf-8") as f:
        json.dump({
            "file": log_path,
            "out_dir": out_dir,
            "counts": dict(counts),
            "reasons": dict(reasons),
            "sessions_written": len(index),
            "ignored_ids": sorted(ignore_ids),
            "sessions": index,
        }, f, ensure_ascii=False, indent=2)

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log", help="Path to .log or .log.gz")
    ap.add_argument("--out-dir", default="./out_sessions")
    ap.add_argument("--gap-ms", type=int, default=1000)
    ap.add_argument("--accept-nonlog", action="store_true")
    ap.add_argument("--debug", action="store_true")
    # NEW ignore options
    ap.add_argument("--ignore-id", action="append", default=[],
                    help="Puzzle id to ignore (repeatable). Example: --ignore-id pluginandlogstests")
    ap.add_argument("--ignore-ids", type=str, default="",
                    help="Comma-separated puzzle ids to ignore. Example: --ignore-ids id1,id2")

    ap.add_argument("--max-lines", type=int)
    args = ap.parse_args()

    if not os.path.exists(args.log):
        raise FileNotFoundError(args.log)

    ignore_ids: Set[str] = set(s for s in (args.ignore_id or []) if s)
    if args.ignore_ids:
        ignore_ids.update([s.strip() for s in args.ignore_ids.split(",") if s.strip()])

    parse_file(
        log_path=args.log,
        out_dir=args.out_dir,
        accept_nonlog=args.accept_nonlog,
        gap_ms=args.gap_ms,
        debug=args.debug,
        max_lines=args.max_lines,
        ignore_ids=ignore_ids,
    )

if __name__ == "__main__":
    main()
