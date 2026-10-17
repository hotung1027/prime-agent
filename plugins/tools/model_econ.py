
"""Per-task economics for factory role models: cost, time, and cost-over-time.

Method
- Tokens per task come from the benchmark that measures the role's aspect (Artificial Analysis
  per-benchmark `outputTokensPerTask`, e.g. Terminal-Bench 4.0, SciCode, CritPt).
- Cost per task = tokens_in * p_in + tokens_out * p_out, using OpenRouter's live prices.
- Time per task   = tokens_out / speed, where `speed` is chosen by --speed-source:
    openrouter : average of the per-provider `throughput_last_30m` from
                 /api/v1/models/<id>/endpoints. Currently NULL for every provider we checked, so this
                 falls back automatically and says so.
    aa         : Artificial Analysis median output speed (tokens/s), measured by AA.
    measured   : our own observed tokens/s for that model, parsed from the local trace timeline.
- Cost over time is then cost_per_task / time_per_task ($/s), i.e. how fast a task burns money.

Usage: python3 model_econ.py [--rate 30] [--speed-source openrouter|aa|measured] [role1 role2 ...]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent if "__file__" in globals() else Path.home() / ".prime" / "agent" / "pipelines"
AA_CACHE = Path("/tmp/aa.html")
OR_LIST = Path("/tmp/or2.json")
TRACE_DIR = Path.home() / ".prime" / "agent" / "traces" / "local"

# role -> (benchmark slug, what it measures)
ROLE_BENCH = {
    "researcher": "critpt", "planner": "humanitys-last-exam", "implementer": "terminalbench-4-0",
    "reviewer": "scicode", "verifier": "terminalbench-4-0", "documentator": "artificial-analysis-long-context-reasoning",
    "visual": "mmmu-pro", "security": "gpqa", "profiling": "scicode", "infrastructure": "terminalbench-4-0",
    "cicd": "terminalbench-4-0",
}
ROLE_CHAIN = {  # what engine.json currently uses, so the tool can price the live config
    "researcher": ["xiaomi/mimo-v2.6-pro"], "planner": ["xiaomi/mimo-v2.6-pro"],
    "implementer": ["xiaomi/mimo-v2.6-pro"], "reviewer": ["z-ai/glm-5.3-flash"],
    "verifier": ["xiaomi/mimo-v2.6-pro"], "visual": ["deepseek/deepseek-v4.1-flash"],
    "documentator": ["xiaomi/mimo-v2.6-pro"], "cicd": ["z-ai/glm-5.3-flash"],
    "security": ["z-ai/glm-5.3-flash"], "profiling": ["z-ai/glm-5.3-flash"],
}


def norm(s):
    """Normalise a model name for matching: lowercase alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def best_aa_name(or_name, table):
    """AA short names ('MiMo-V2.6-Pro') vs OpenRouter names ('Xiaomi: MiMo-V2.6-Pro')."""
    want = norm(or_name)
    exact = [k for k in table if norm(k) == want]
    if exact:
        return exact[0]
    suffix = [k for k in table if want.endswith(norm(k)) or norm(k).endswith(want)]
    if suffix:
        return max(suffix, key=lambda k: len(norm(k)))
    return None


def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "prime-agent-econ"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def rsc_payload(html):
    """Decode a Next.js RSC payload: the benchmark JSON is inside escaped JS strings."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*(".*?")\]\)', html)
    if not chunks:
        return html
    try:
        return "".join(json.loads(c) for c in chunks)
    except Exception:
        return html


def aa_data():
    """model name -> {"<bench>": {"score","out_tokens","cost"}} and name -> speed (tok/s)."""
    html = AA_CACHE.read_text(errors="ignore") if AA_CACHE.exists() else _get("https://artificialanalysis.ai/models")
    html = rsc_payload(html)
    per_bench, speed = {}, {}
    row = re.compile(r'\{"slug":"([a-z0-9\-]+)","score":([0-9.]+|null),'
                     r'"outputTokensPerTask":([0-9.]+),"costPerTask":([0-9.]+|null),'
                     r'"timePerTask":([0-9.]+|null)\}')
    for m in row.finditer(html):
        names = re.findall(r'"name":"([^"]{2,60})"', html[max(0, m.start()-20000):m.start()])
        if not names:
            continue
        per_bench.setdefault(names[-1], {})[m.group(1)] = {
            "score": float(m.group(2)) if m.group(2) != "null" else None,
            "out_tokens": float(m.group(3)),
        }
    for m in re.finditer(r'"medianOutputSpeed":([0-9.]+)', html):
        names = re.findall(r'"name":"([^"]{2,60})"', html[max(0, m.start()-30000):m.start()])
        if names:
            speed.setdefault(names[-1], float(m.group(1)))
    return per_bench, speed


def or_prices():
    data = json.loads(OR_LIST.read_text()) if OR_LIST.exists() else json.loads(_get("https://openrouter.ai/api/v1/models"))
    out = {}
    for m in data["data"]:
        p = m.get("pricing") or {}
        try:
            out[m["id"]] = {"in": float(p.get("prompt") or 0) * 1e6,
                            "out": float(p.get("completion") or 0) * 1e6,
                            "name": m.get("name")}
        except (TypeError, ValueError):
            continue
    return out


def or_throughput(model_id):
    """Average of the providers' throughput_last_30m (OpenRouter's own number), or None."""
    try:
        eps = json.loads(_get(f"https://openrouter.ai/api/v1/models/{model_id}/endpoints"))["data"]["endpoints"]
    except Exception:
        return None, 0, 0
    vals = [e.get("throughput_last_30m") for e in eps if e.get("throughput_last_30m")]
    return (statistics.mean(vals) if vals else None), len(vals), len(eps)


def measured_speed():
    """tokens/s observed in our own runs, from role spans that recorded duration_ms."""
    out = {}
    if not TRACE_DIR.exists():
        return out
    for f in TRACE_DIR.glob("events-*.jsonl"):
        for line in f.open():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") != "span-end" or not rec["name"].startswith("role:"):
                continue
            a = rec.get("attributes") or {}
            spd = a.get("tokens_per_second")
            if spd and a.get("model"):
                out.setdefault(a["model"].split("/")[-1].lower(), []).append(float(spd))
    return {k: statistics.mean(v) for k, v in out.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=30.0, help="$ per hour of waiting time")
    ap.add_argument("--speed-source", default="openrouter", choices=["openrouter", "aa", "measured"])
    ap.add_argument("roles", nargs="*")
    ap.add_argument("--rank", default=None, help="rank the whole pool for this role")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--available", action="store_true",
                    help="only providers that answer on this account (openai/google/anthropic are 403)")
    args = ap.parse_args()
    per_bench, aa_speed = aa_data()
    prices = or_prices()
    if args.rank:
        bench = ROLE_BENCH.get(args.rank, args.rank)
        pool = []
        for name, benches in per_bench.items():
            row = benches.get(bench)
            if not row or not row.get("out_tokens"):
                continue
            spd = aa_speed.get(name)
            orname = [k for k in prices if norm(k).endswith(norm(name)) or norm(name).endswith(norm(k))]
            if not spd or not orname:
                continue
            if args.available:
                prov = orname[0].split("/")[0].lstrip("~")
                if prov in ("openai", "google", "anthropic", "amazon", "qwen", "moonshotai", "meta-llama",
                            "x-ai", "aion-labs") or not prov:
                    continue
            p = prices[orname[0]]
            cost = row["out_tokens"] / 1e6 * p["out"]
            tsec = row["out_tokens"] / spd
            eff = cost + tsec * args.rate / 3600
            pool.append((name, orname[0], row["score"] * 100, row["out_tokens"], p["out"], cost, spd,
                         tsec / 60, eff, (row["score"] * 100) / eff))
        print(f"pool for {args.rank} on {bench}, time at ${args.rate:.0f}/h - ranked by score/(cost+time)\n")
        print(f"{'model':26} {'score':>6} {'tok/task':>9} {'$/Mtok':>7} {'$cost':>7} {'tok/s':>6} {'min':>6} {'$+time':>7} {'score/$':>8}")
        for r in sorted(pool, key=lambda x: -x[9])[:args.top]:
            print(f"{r[0][:26]:26} {r[2]:6.1f} {r[3]:9,.0f} {r[4]:7.2f} {r[5]:7.3f} {r[6]:6.1f} {r[7]:6.1f} {r[8]:7.2f} {r[9]:8.2f}")
        return
    roles = args.roles or list(ROLE_BENCH)
    print(f"speed source: {args.speed_source} | waiting time valued at ${args.rate:.0f}/h\n")
    print(f"{'role':13} {'model':26} {'score':>5} {'tok/task':>9} {'$/Mtok out':>10} "
          f"{'$ / task':>8} {'tok/s':>6} {'time min':>8} {'$+time':>7}")
    for role in roles:
        bench = ROLE_BENCH[role]
        for slug in ROLE_CHAIN.get(role, []):
            name = prices.get(slug, {}).get("name", slug)
            aa_name = best_aa_name(name, per_bench) or name
            row = (per_bench.get(aa_name) or {}).get(bench)
            if not row:
                print(f"{role:13} {slug[:26]:26}   (no {bench} row with tokens)"); continue
            tok = row["out_tokens"]
            p_out = prices.get(slug, {}).get("out", 0)
            cost = tok / 1e6 * p_out
            spd = None
            if args.speed_source == "openrouter":
                spd, n_have, n_all = or_throughput(slug)
                if spd is None:
                    print(f"{role:13} {slug[:26]:26}   openrouter throughput null ({n_have}/{n_all} providers) -> falling back to aa")
            if spd is None:
                spd = aa_speed.get(aa_name)
            if spd is None:
                spd = measured_speed().get(slug.split("/")[-1].lower())
            if not spd:
                print(f"{role:13} {slug[:26]:26}   (no speed source)"); continue
            t = tok / spd
            eff = cost + t * args.rate / 3600
            print(f"{role:13} {slug[:26]:26} {row['score']*100:5.1f} {tok:9,.0f} {p_out:10.2f} "
                  f"{cost:8.3f} {spd:6.1f} {t/60:8.1f} {eff:7.2f}")


if __name__ == "__main__":
    main()
