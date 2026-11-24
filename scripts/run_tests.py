"""Run tests.json against a deployed AdForge API and print a benchmark table.

    python scripts/run_tests.py --base https://<your-space>.hf.space
"""
import argparse
import json
import pathlib
import statistics
import time
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent


def post(base, payload):
    req = urllib.request.Request(base + "/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.loads(r.read())
    return body, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:7860")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    run = uuid.uuid4().hex[:6]  # fresh ids each run so memory starts clean
    cases = json.loads((ROOT / "tests.json").read_text())["cases"]
    rows, lat, tin, tout = [], [], 0, 0
    for c in cases:
        uid = f"{c['user_id']}_{run}"
        replies, tools_used, tok_total = [], [], 0
        for msg in c["turns"]:
            body, dt = post(base, {"user_id": uid, "thread_id": c["thread_id"], "message": msg})
            replies.append(body["reply"]); lat.append(dt)
            tools_used += [t["name"] for t in body["tool_calls"]]
            tok_total += body["tokens"]["in"] + body["tokens"]["out"]
            tin += body["tokens"]["in"]; tout += body["tokens"]["out"]
        fu_reply = ""
        if "followup" in c:
            for msg in c["followup"]["turns"]:
                body, dt = post(base, {"user_id": uid, "thread_id": c["followup"]["thread_id"], "message": msg})
                fu_reply = body["reply"]; lat.append(dt)
                tin += body["tokens"]["in"]; tout += body["tokens"]["out"]
        last = replies[-1]
        ok = all(t in tools_used for t in c.get("expect_tools", []))
        ok &= all(s.lower() in last.lower() for s in c.get("expect_reply_contains", []))
        ok &= not any(s.lower() in last.lower() for s in c.get("expect_reply_not_contains", []))
        ok &= all(s.lower() in fu_reply.lower() for s in c.get("expect_followup_contains", []))
        if c.get("expect_tokens_zero"):
            ok &= tok_total == 0
        if c["id"] == 7:
            ok &= not tools_used and any(w in last.lower() for w in ("advertis", "ad ", "ads", "marketing"))
        if c["id"] == 9:
            ok &= "launch_campaign" in tools_used and "confirm" in last.lower()
        rows.append((c["id"], c["name"], "PASS" if ok else "FAIL", ",".join(dict.fromkeys(tools_used)) or "-", tok_total))
        print(f"[{rows[-1][2]}] {c['id']:>2} {c['name']}\n      tools={rows[-1][3]} tokens={tok_total}\n      reply={last[:160]!r}")
    passed = sum(r[2] == "PASS" for r in rows)
    print("\n| # | Test | Result | Tools | Tokens |\n|---|---|---|---|---|")
    for r in rows:
        print(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} |")
    cost = tin * 0.10 / 1e6 + tout * 0.50 / 1e6
    print(f"\nPassed {passed}/{len(rows)} · requests {len(lat)} · median latency {statistics.median(lat):.2f}s · "
          f"p95 {sorted(lat)[int(len(lat)*0.95)-1]:.2f}s · tokens {tin} in / {tout} out · est cost ${cost:.4f}")


if __name__ == "__main__":
    main()
