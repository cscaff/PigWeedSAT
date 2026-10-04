"""Token usage of Claude Code sessions in this project, from the local transcripts.

    python tools/token_log.py            # newest session -> build/token_usage.csv + summary
    python tools/token_log.py --all      # every session of this project

Claude Code writes each session to ~/.claude/projects/<project>/<session>.jsonl;
every model response carries a `usage` record.  Streamed chunks repeat the
same record, so responses are de-duplicated by message id.  Counts are what
the API reported; they are not a bill (pricing depends on your plan).
"""

import csv
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJ = os.path.expanduser("~/.claude/projects/" + ROOT.replace("/", "-").replace(" ", "-"))
# The project was renamed from SAT-Accel-Lattice; earlier sessions live under the old key.
PROJ_DIRS = [PROJ, PROJ.replace("PigWeedSAT", "SAT-Accel-Lattice")]
FIELDS = ["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"]


def session_rows(path):
    seen, rows = set(), []
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = d.get("message") or {}
            u = msg.get("usage")
            mid = msg.get("id")
            if not u or not mid or mid in seen:
                continue
            seen.add(mid)
            rows.append({"timestamp": d.get("timestamp", ""), "model": msg.get("model", ""),
                         "message_id": mid, **{k: u.get(k, 0) or 0 for k in FIELDS}})
    return rows


def main(argv):
    files = sorted({f for d in PROJ_DIRS for f in glob.glob(os.path.join(d, "*.jsonl"))},
                   key=os.path.getmtime)
    if not files:
        sys.exit(f"no transcripts under {PROJ}")
    if "--all" not in argv:
        files = files[-1:]
    os.makedirs(os.path.join(ROOT, "build"), exist_ok=True)
    out = os.path.join(ROOT, "build", "token_usage.csv")
    total = {k: 0 for k in FIELDS}
    n = 0
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["session", "timestamp", "model", "message_id", *FIELDS])
        w.writeheader()
        for path in files:
            sid = os.path.basename(path)[:-6]
            for r in session_rows(path):
                w.writerow({"session": sid, **r})
                n += 1
                for k in FIELDS:
                    total[k] += r[k]
    grand = sum(total.values())
    print(f"{len(files)} session(s), {n} model responses -> {out}")
    for k in FIELDS:
        print(f"  {k:30s} {total[k]:>14,}")
    print(f"  {'total':30s} {grand:>14,}")


if __name__ == "__main__":
    main(sys.argv[1:])
