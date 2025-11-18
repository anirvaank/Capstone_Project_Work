#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
New-plugin Sudoku parser (dimension-agnostic).

Key features
------------
- Groups by (userId, puzzleId) so logs never mix across puzzles for the same user.
- Uses new boot lines:
    [sudoku-permove] boot_preReveal ... maskFlat=...
    [sudoku-permove] boot_state ... boardFlat=... rows=...
- Classifies 'setup' vs 'user':
    setup := first writes that fill cells marked 1 in preReveal mask (engine paint)
    user  := everything else (including selects, erases, writes beyond mask)
- Exports per-session folder: <out_dir>/<puzzleId>__<userId>[__k]/ with:
    - setup_events.csv
    - user_events.csv
    - state_grid.csv (from boot_state)
    - initial_board.json (from boot_state)
    - preReveal_mask.json (if present)
    - meta.json

Event CSV schema (both files)
-----------------------------
move_type,row,column,time_ms,cell_before,cell_after

Usage
-----
python parser_new_plugin.py pmm--pmm-a007ed6-18969cc3acc0.log \
  --out-dir parsed_new \
  --only-user anir \
  --ignore-id pluginandlogstests \
  --debug

Options
-------
--only-user <uid>       : parse only this userId
--only-id <puzzleId>    : parse only this puzzle id
--ignore-id <puzzleId>  : skip this puzzle id entirely
--accept-nonlog         : accept lines with [sudoku-permove] even w/o [Log:/sudoku] prefix
--debug                 : verbose prints
"""

import os, re, csv, json, gzip, argparse
from collections import defaultdict
from typing import Optional, Dict, Tuple, List

# ---------- patterns (tolerant) ----------
TAG_PERMOVE   = re.compile(r"\[sudoku-permove\]")

# lines we care about
RE_LOADED   = re.compile(r"\[sudoku-permove\]\s+loaded\b.*?\bid=(?P<pid>\S+)\b.*?\bw=(?P<w>\d+)\b.*?\bh=(?P<h>\d+)\b", re.I)
RE_BOOT_PRE = re.compile(r"\[sudoku-permove\]\s+boot_preReveal(?:Idxs)?\b", re.I)
RE_BOOT_ST  = re.compile(r"\[sudoku-permove\]\s+boot_state\b", re.I)
RE_MOVE = re.compile(
    r"""\[sudoku-permove\]\s+move=(?P<move_id>\d+)
        (?:\s+r=(?P<r>\d+)\s+c=(?P<c>\d+))?
        (?:\s+prev='(?P<prev>[^']*)'\s+next='(?P<next>[^']*)')?
        (?:\s+mode=(?P<mode>\w+))?
        .*?\bdt_ms=(?P<dt_ms>\d+)\s+t_ms=(?P<t_ms>\d+)
    """, re.VERBOSE | re.I)
RE_SELECT = re.compile(
    r"""\[sudoku-permove\]\s+select
        (?:\s+r=(?P<r>\d+)\s+c=(?P<c>\d+))?
        .*?\bdt_ms=(?P<dt_ms>\d+)\s+t_ms=(?P<t_ms>\d+)
    """, re.VERBOSE | re.I)
RE_CLEARED = re.compile(r"\[sudoku-permove\]\s+cleared\b.*?\bt_ms=(?P<t_ms>\d+)", re.I)

# generic extractors
RE_USERID = re.compile(r"\buserId=([^\s]+)")
RE_BOARD_FLAT = re.compile(r"\bboardFlat=(\S+)")
RE_MASK_FLAT  = re.compile(r"\bmask(?:Flat|81)=(\S+)")
RE_ROWS_JSON  = re.compile(r"\brows=(\[.*\])")  # JSON array at end (best-effort)

def open_maybe_gz(path):
    return gzip.open(path, "rt", encoding="utf-8", errors="ignore") if path.endswith(".gz") \
           else open(path, "r", encoding="utf-8", errors="ignore")

def is_permove(line, accept_nonlog=False):
    # New plugin always prints [sudoku-permove]; many server lines don't.
    return bool(TAG_PERMOVE.search(line)) if accept_nonlog else bool(TAG_PERMOVE.search(line))

def to_int(x: Optional[str], default: Optional[int]=None) -> Optional[int]:
    try:
        if x is None or x == "": return default
        return int(x)
    except Exception:
        return default

def norm_cell(s: Optional[str]) -> str:
    if s is None: return ""
    s = str(s)
    # treat single space as empty
    return "" if s.strip() == "" else s

def infer_move_type(prev: Optional[str], nxt: Optional[str], is_select: bool) -> str:
    if is_select: return "select"
    p = "" if prev in (None, " ") else prev
    n = "" if nxt  in (None, " ") else nxt
    if p == "" and n != "": return "write"
    if p != "" and n == "": return "erase"
    if p != "" and n != "" and p != n: return "overwrite"
    return "write" if n != "" else "select"

# ---------- session model ----------
class Session:
    def __init__(self, user_id: str, puzzle_id: str, w: int, h: int):
        self.user_id = user_id
        self.puzzle_id = puzzle_id
        self.w, self.h = w, h
        self.pre_mask_flat: Optional[str] = None     # e.g., "0100..."; len = w*h
        self.boot_board_flat: Optional[str] = None   # e.g., ".2.1...."; len = w*h
        self.boot_rows: Optional[List[List[str]]] = None  # optional 2D rows
        self.setup_events: List[Dict] = []
        self.user_events: List[Dict] = []
        self._last_r: Optional[int] = None
        self._last_c: Optional[int] = None
        self._seen_any_event = False

    def mask_is_given(self, r: int, c: int) -> bool:
        """r,c are 1-based; True iff mask marks (r,c) as given."""
        if not self.pre_mask_flat or self.w <= 0 or self.h <= 0:
            return False
        idx = (r - 1) * self.w + (c - 1)
        if idx < 0 or idx >= len(self.pre_mask_flat): return False
        return self.pre_mask_flat[idx] == "1"

    def classify_and_add(self, ev: Dict):
        """Classify engine paint vs user based on preReveal mask."""
        self._seen_any_event = True
        mt = ev.get("move_type")
        r, c = ev.get("row"), ev.get("column")
        before, after = ev.get("cell_before"), ev.get("cell_after")

        is_paint_like = (mt in ("write","overwrite") and before in ("", None) and after not in ("", None))
        goes_on_mask  = (r is not None and c is not None and self.mask_is_given(r, c))

        if is_paint_like and goes_on_mask:
            self.setup_events.append(ev)
        else:
            self.user_events.append(ev)

    def remember_rc(self, r: Optional[int], c: Optional[int]):
        if r is not None: self._last_r = r
        if c is not None: self._last_c = c

    def fill_missing_rc(self, r: Optional[str], c: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
        r_i = to_int(r, self._last_r)
        c_i = to_int(c, self._last_c)
        self.remember_rc(r_i, c_i)
        return r_i, c_i

# ---------- helpers ----------
def ensure_unique_dir(base_dir: str) -> str:
    """Return a non-conflicting directory path (append __2, __3, ... if exists)."""
    if not os.path.exists(base_dir):
        return base_dir
    k = 2
    while True:
        alt = f"{base_dir}__{k}"
        if not os.path.exists(alt):
            return alt
        k += 1

def write_csv_rows(path, rows, header):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f); wr.writerow(header)
        for e in rows:
            wr.writerow([
                e.get("move_type",""),
                e.get("row","") or "",
                e.get("column","") or "",
                e.get("time_ms",0),
                "" if e.get("cell_before") in (None,"") else e["cell_before"],
                "" if e.get("cell_after")  in (None,"") else e["cell_after"],
            ])

def flat_to_grid(flat: str, w: int, h: int) -> List[List[str]]:
    grid = [[""]*w for _ in range(h)]
    if not flat: return grid
    for r in range(h):
        for c in range(w):
            idx = r*w + c
            if idx < len(flat):
                v = flat[idx]
                grid[r][c] = "" if v == "." else v
    return grid

def write_board_csv(path, grid):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow([f"c{j+1}" for j in range(len(grid[0]) if grid else 0)])
        for row in grid:
            wr.writerow([cell if cell else "" for cell in row])

# ---------- main parse ----------
def parse(log_path: str, out_dir: str, only_user: Optional[str], only_id: Optional[str],
          ignore_id: Optional[str], accept_nonlog: bool, debug: bool):

    sessions: Dict[Tuple[str,str,int], Session] = {}  # key=(userId,puzzleId,seq)
    active_key_by_userid: Dict[str, Tuple[str,str,int]] = {}  # (userId) -> latest key
    seq_counter: Dict[Tuple[str,str], int] = defaultdict(int)

    def start_session(user_id: str, puzzle_id: str, w: int, h: int) -> Tuple[str,str,int]:
        seq_counter[(user_id, puzzle_id)] += 1
        seq = seq_counter[(user_id, puzzle_id)]
        key = (user_id, puzzle_id, seq)
        sessions[key] = Session(user_id, puzzle_id, w, h)
        active_key_by_userid[user_id] = key
        if debug:
            print(f"[session] START user={user_id} id={puzzle_id} w={w} h={h} seq={seq}")
        return key

    def current_session_for_user(user_id: Optional[str]) -> Optional[Session]:
        if not user_id: return None
        key = active_key_by_userid.get(user_id)
        return sessions.get(key) if key else None

    with open_maybe_gz(log_path) as f:
        for lineno, line in enumerate(f, 1):
            if not is_permove(line, accept_nonlog):
                continue

            # Extract userId (always appended by your plugin)
            m_uid = RE_USERID.search(line)
            user_id = m_uid.group(1) if m_uid else None

            # respect filters early
            if only_user and user_id and user_id != only_user:
                continue

            # (1) LOADED: begin a new session for (userId, puzzleId)
            if RE_LOADED.search(line):
                m = RE_LOADED.search(line)
                pid = m.group("pid")
                if ignore_id and pid == ignore_id:
                    continue
                if only_id and pid != only_id:
                    continue
                w = int(m.group("w")); h = int(m.group("h"))
                if not user_id:
                    if debug: print(f"[warn] loaded@{lineno} but no userId=... found; skipping")
                    continue
                start_session(user_id, pid, w, h)
                continue

            # find session context
            sess = current_session_for_user(user_id)
            if not sess:
                # skip lines until we see a 'loaded' for this user
                continue

            # (2) boot_preReveal: record mask
            if RE_BOOT_PRE.search(line):
                if "none" in line:
                    sess.pre_mask_flat = None
                else:
                    mf = RE_MASK_FLAT.search(line)
                    if mf:
                        sess.pre_mask_flat = mf.group(1)
                # optionally capture rows JSON (best-effort)
                continue

            # (3) boot_state: record initial board (flat and/or rows)
            if RE_BOOT_ST.search(line):
                bf = RE_BOARD_FLAT.search(line)
                if bf:
                    sess.boot_board_flat = bf.group(1)
                rj = RE_ROWS_JSON.search(line)
                if rj:
                    try:
                        sess.boot_rows = json.loads(rj.group(1))
                    except Exception:
                        sess.boot_rows = None
                continue

            # (4) cleared: ignore for CSVs you want
            if RE_CLEARED.search(line):
                continue

            # (5) moves/selects -> classify & store
            mm = RE_MOVE.search(line)
            ms = RE_SELECT.search(line)

            if mm:
                r_i, c_i = sess.fill_missing_rc(mm.group("r"), mm.group("c"))
                prev = norm_cell(mm.group("prev"))
                nxt  = norm_cell(mm.group("next"))
                ev = {
                    "move_type": infer_move_type(prev, nxt, False),
                    "row": r_i,
                    "column": c_i,
                    "time_ms": int(mm.group("t_ms")),
                    "cell_before": "" if prev == "" else prev,
                    "cell_after":  "" if nxt  == "" else nxt,
                }
                sess.classify_and_add(ev)
                continue

            if ms:
                r_i, c_i = sess.fill_missing_rc(ms.group("r"), ms.group("c"))
                ev = {
                    "move_type": "select",
                    "row": r_i,
                    "column": c_i,
                    "time_ms": int(ms.group("t_ms")),
                    "cell_before": "",
                    "cell_after":  "",
                }
                sess.classify_and_add(ev)
                continue

            # otherwise ignore

    # ---------- write sessions ----------
    os.makedirs(out_dir, exist_ok=True)
    summary = []
    for (uid, pid, seq), sess in sessions.items():
        if only_user and uid != only_user: continue
        if only_id and pid != only_id: continue
        if ignore_id and pid == ignore_id: continue

        base = os.path.join(out_dir, f"{pid}__{uid}")
        sdir = ensure_unique_dir(base) if seq > 1 or os.path.exists(base) else base
        os.makedirs(sdir, exist_ok=True)

        # setup_events.csv / user_events.csv
        header = ["move_type","row","column","time_ms","cell_before","cell_after"]
        write_csv_rows(os.path.join(sdir, "setup_events.csv"), sess.setup_events, header)
        write_csv_rows(os.path.join(sdir, "user_events.csv"),  sess.user_events,  header)

        # state_grid.csv + initial_board.json from boot_state
        grid = None
        if sess.boot_rows and isinstance(sess.boot_rows, list):
            grid = sess.boot_rows
        elif sess.boot_board_flat:
            grid = flat_to_grid(sess.boot_board_flat, sess.w, sess.h)
        else:
            grid = [[""]*sess.w for _ in range(sess.h)]  # fallback all empty

        write_board_csv(os.path.join(sdir, "state_grid.csv"), grid)
        with open(os.path.join(sdir, "initial_board.json"), "w", encoding="utf-8") as f:
            json.dump({"w": sess.w, "h": sess.h, "rows": grid}, f, ensure_ascii=False, indent=2)

        # preReveal mask
        with open(os.path.join(sdir, "preReveal_mask.json"), "w", encoding="utf-8") as f:
            json.dump({"maskFlat": sess.pre_mask_flat, "w": sess.w, "h": sess.h}, f, ensure_ascii=False, indent=2)

        # meta
        meta = {
            "userId": uid,
            "puzzleId": pid,
            "seq": seq,
            "w": sess.w,
            "h": sess.h,
            "events": {
                "setup": len(sess.setup_events),
                "user":  len(sess.user_events),
                "total": len(sess.setup_events) + len(sess.user_events),
            },
            "sources": {
                "boot_state": bool(sess.boot_board_flat or sess.boot_rows),
                "preReveal":  bool(sess.pre_mask_flat),
            },
            "out_dir": sdir,
        }
        with open(os.path.join(sdir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        summary.append(meta)

    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log", help="path to .log or .gz")
    ap.add_argument("--out-dir", default="parsed_new")
    ap.add_argument("--only-user", help="process only this userId")
    ap.add_argument("--only-id", help="process only this puzzle id")
    ap.add_argument("--ignore-id", help="skip this puzzle id entirely")
    ap.add_argument("--accept-nonlog", action="store_true", help="accept lines even if [Log:/sudoku] prefix isn't present")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.log):
        raise FileNotFoundError(args.log)

    parse(
        log_path=args.log,
        out_dir=args.out_dir,
        only_user=args.only_user,
        only_id=args.only_id,
        ignore_id=args.ignore_id,
        accept_nonlog=args.accept_nonlog,
        debug=args.debug
    )

if __name__ == "__main__":
    main()
