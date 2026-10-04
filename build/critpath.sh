#!/bin/sh
# usage: build/critpath.sh MHZ  -> fmax per clock + the user-clock critical path summary
cd "$(dirname "$0")/.." && rm -rf build/pnrwork && mkdir -p build/pnrwork && cp design.py build/pnrwork/
docker run --rm -v "$PWD/build/pnrwork:/w" ghcr.io/manhattanreasoning/mrg-sandbox:latest sh -c "cd /w && python -c \"
import mrg_build; mrg_build.build(mode='pnr', design='/w/design.py', work='/w/out', sys_clk_mhz=$1)\" >/dev/null 2>&1"
python3 - <<'PY'
import json, re
r = json.load(open("build/pnrwork/out/soc/gateware/report.json"))
print({k.split("$")[-1]: round(v["achieved"], 1) for k, v in r["fmax"].items()})
print("util:", {k: r["utilization"][k]["used"] for k in ("TRELLIS_COMB", "TRELLIS_FF", "DP16KD")})
for cp in r.get("critical_paths", []):
    if "clkout1" in cp["from"] and "clkout1" in cp["to"]:
        path = cp["path"]
        cells = [s["from"]["cell"] for s in path if s["type"] in ("clk-to-q", "logic")] + [path[-1]["to"]["cell"]]
        mods = []
        for c in cells:
            mm = re.sub(r"^user_design\.", "", c).split(".")[0]
            if not mods or mods[-1] != mm: mods.append(mm)
        print(f"crit {sum(s['delay'] for s in path):.2f} ns | {cells[0][:70]} -> {cells[-1][:70]}")
        print("  through:", " > ".join(mods))
PY
