"""Authoring eval: does a model, following the sport skills through the
workout-author agent, turn a coaching brief into a session whose rendering
matches the brief?

The cases live in evals/authoring/cases.json. Each case is a brief (what the
coach asks for), the checks its rendering must satisfy (regexes over the
preview step tree, the workout type, the estimated duration), and a golden
session that satisfies them — the golden proves the checks are achievable
under the contract and keeps the cases in step with it (tests/
test_authoring_eval.py renders every golden in CI).

Modes
-----
--check   Render every golden with coach.workout_builder.build_workout_report
          and apply its checks. No model, no cost. This is what CI runs.
--run     Author each brief with `claude -p --agent workout-author` against a
          hermetic coach server (synthetic athlete in a temp data dir, dummy
          Garmin credentials, only preview/list tools allowed), render the
          session the agent returns with the same renderer, apply the checks,
          and write evals/authoring/results/<timestamp>-<model>.json plus a
          summary table. Owner-run: every case costs real money.

Examples
--------
  .venv/Scripts/python.exe scripts/authoring_eval.py --check
  .venv/Scripts/python.exe scripts/authoring_eval.py --run --model sonnet --budget-usd 6
  .venv/Scripts/python.exe scripts/authoring_eval.py --run --cases gym_circuit,run_walk_ladder
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # run from a checkout: scripts/ is not a package
    sys.path.insert(0, str(ROOT))
EVAL_DIR = ROOT / "evals" / "authoring"
CASES_FILE = EVAL_DIR / "cases.json"
RESULTS_DIR = EVAL_DIR / "results"
SEED_FILES = ("athlete.json", "training_config.json", "exercises.json")

# Tools the authoring agent may call in print mode. Everything else is denied
# (the agent definition already disallows the save/push/snapshot tools).
ALLOWED_TOOLS = (
    "mcp__coach-mcp__preview_workout",
    "mcp__coach-mcp__list_exercises",
    "mcp__coach-mcp__research_exercise",
    "Skill", "Read", "Glob", "Grep",
)

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "session": {"type": ["object", "null"], "additionalProperties": True,
                    "description": "The final session dict, or null when the brief was incomplete"},
        "missing_brief": {"type": ["array", "null"], "items": {"type": "string"},
                          "description": "What the brief did not carry, when you could not author"},
        "notes": {"type": "string", "description": "Assumptions and residual warnings, one line each"},
    },
    "required": ["session", "missing_brief", "notes"],
    "additionalProperties": False,
}

PROMPT_TEMPLATE = """\
Author ONE plan session from the brief below, following your procedure (read the
sport skill, load the contract with preview_workout(), compose, preview, fix
until the step tree matches the brief).

End your reply with ONE fenced ```json block and nothing after it, of the shape
{{"session": <the final session dict exactly as it would sit under a plan day's
`planned`, or null>, "missing_brief": <list of what the brief lacks, or null>,
"notes": "<assumptions and residual warnings, one line each>"}}. When the brief
is incomplete per your contract, `session` is null and `missing_brief` names
the gaps.

{brief}
"""


def extract_answer(payload: dict) -> dict:
    """The agent's answer as {session, missing_brief, notes}.

    Prefers the CLI's structured_output; falls back to the last fenced JSON
    block (or bare JSON object) in the reply text, which is how an agent run
    with --agent tends to answer."""
    structured = payload.get("structured_output") or {}
    if isinstance(structured, dict) and ("session" in structured or "missing_brief" in structured):
        return structured
    text = str(payload.get("result") or "")
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if not candidates:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            candidates = [text[start:end + 1]]
    for raw in reversed(candidates):
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if "session" in obj or "missing_brief" in obj:
            return obj
        if "type" in obj:  # the agent pasted the session itself
            return {"session": obj, "missing_brief": None, "notes": None}
    if "missing_brief" in text:
        return {"session": None, "missing_brief": ["(see the reply text)"], "notes": None}
    return {}


# ---------------------------------------------------------------------------
# Cases and checks
# ---------------------------------------------------------------------------

def load_cases(path: Path = CASES_FILE) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data["cases"]


def _patterns(spec) -> list[tuple[str, int]]:
    """Normalise a steps_match entry list: strings, or {pattern, min} objects."""
    out = []
    for item in spec or []:
        if isinstance(item, str):
            out.append((item, 1))
        else:
            out.append((item["pattern"], int(item.get("min", 1))))
    return out


def apply_checks(report: dict, checks: dict) -> list[str]:
    """Return the list of failed checks (empty means the rendering matches)."""
    failures: list[str] = []
    steps = report.get("steps") or []
    joined = "\n".join(steps)

    if "ok" in checks and bool(report.get("ok")) != bool(checks["ok"]):
        failures.append(f"ok={report.get('ok')} expected {checks['ok']}; errors={report.get('errors')}")
    if "workout_type" in checks and report.get("workout_type") != checks["workout_type"]:
        failures.append(f"workout_type={report.get('workout_type')!r} expected {checks['workout_type']!r}")
    if "estimated_mins" in checks:
        lo, hi = checks["estimated_mins"]
        est = report.get("estimated_mins")
        if est is None or not (lo <= est <= hi):
            failures.append(f"estimated_mins={est} expected {lo}-{hi}")
    if "steps_count" in checks and len(steps) != checks["steps_count"]:
        failures.append(f"{len(steps)} step lines, expected {checks['steps_count']}")
    for pattern, minimum in _patterns(checks.get("steps_match")):
        hits = sum(1 for line in steps if re.search(pattern, line, re.IGNORECASE))
        if hits < minimum:
            failures.append(f"missing step /{pattern}/ (found {hits}, need {minimum})")
    for pattern in checks.get("steps_absent") or []:
        if re.search(pattern, joined, re.IGNORECASE):
            failures.append(f"forbidden step /{pattern}/ present")
    if "max_warnings" in checks and len(report.get("warnings") or []) > checks["max_warnings"]:
        failures.append(f"{len(report['warnings'])} warnings, allowed {checks['max_warnings']}: "
                        f"{[w.get('message') for w in report['warnings']]}")
    for pattern in checks.get("warnings_absent") or []:
        for warning in report.get("warnings") or []:
            if re.search(pattern, str(warning.get("message", "")), re.IGNORECASE):
                failures.append(f"warning /{pattern}/ present: {warning.get('message')}")
    return failures


def render(session: dict) -> dict:
    """Render a session the way push_plan_to_garmin and preview_workout do.

    Imported lazily so --run can point COACH_DATA_DIR at the eval data dir
    before coach.config resolves it."""
    import coach.workout_builder as workout_builder
    return workout_builder.build_workout_report(session, "preview")


def evaluate_case(case: dict, session: dict | None, missing_brief: list | None) -> dict:
    """Score one authored answer against the case's expectations."""
    expect_missing = bool(case.get("expect_missing_brief"))
    if expect_missing:
        ok = session is None and bool(missing_brief)
        return {"pass": ok, "failures": [] if ok else
                ["expected missing_brief, got a session" if session is not None
                 else "expected a non-empty missing_brief list"],
                "report": None}
    if session is None:
        return {"pass": False,
                "failures": [f"no session returned (missing_brief={missing_brief})"],
                "report": None}
    report = render(session)
    failures = apply_checks(report, case["checks"])
    public = {k: v for k, v in report.items() if k != "workout"}
    return {"pass": not failures, "failures": failures, "report": public}


# ---------------------------------------------------------------------------
# --check: goldens
# ---------------------------------------------------------------------------

def check_goldens(cases: list[dict]) -> int:
    bad = 0
    for case in cases:
        if case.get("expect_missing_brief"):
            print(f"  {case['id']:<28} (missing_brief case; nothing to render)")
            continue
        result = evaluate_case(case, case["golden"], None)
        mark = "ok  " if result["pass"] else "FAIL"
        print(f"  {case['id']:<28} {mark} est={result['report']['estimated_mins']}")
        if not result["pass"]:
            bad += 1
            for f in result["failures"]:
                print(f"      - {f}")
            for line in result["report"]["steps"]:
                print(f"        {line}")
    print(f"\n{len(cases) - bad} of {len(cases)} goldens satisfy their checks")
    return bad


# ---------------------------------------------------------------------------
# --run: the model authors each brief
# ---------------------------------------------------------------------------

def seed_data_dir(data_dir: Path) -> dict:
    """A synthetic athlete, a minimal config and the committed slice of the
    Garmin exercise catalogue. When the owner's full catalogue is present
    (data/exercises.json, 1,493 Garmin keys) its exercises and categories
    replace the slice so the model can look keys up as it would in
    production — never the personal parts of that file."""
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in SEED_FILES:
        shutil.copyfile(EVAL_DIR / name, data_dir / name)
    (data_dir / "tokens").mkdir(exist_ok=True)
    info = {"catalogue": "eval slice"}
    full_catalogue = ROOT / "data" / "exercises.json"
    if full_catalogue.exists():
        with open(full_catalogue, encoding="utf-8") as fh:
            full = json.load(fh)
        slim = {k: full[k] for k in ("exercises", "categories", "metadata") if k in full}
        with open(data_dir / "exercises.json", "w", encoding="utf-8") as fh:
            json.dump(slim, fh)
        info["catalogue"] = "owner's full catalogue"
    with open(data_dir / "exercises.json", encoding="utf-8") as fh:
        info["catalogue_entries"] = len(json.load(fh).get("exercises") or {})
    return info


def write_mcp_config(data_dir: Path) -> Path:
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)
    cfg = {"mcpServers": {"coach-mcp": {
        "command": str(python),
        "args": [str(ROOT / "server.py")],
        "env": {
            "COACH_DATA_DIR": str(data_dir),
            "COACH_TOKEN_DIR": str(data_dir / "tokens"),
            "GARMIN_EMAIL": "eval@example.invalid",
            "GARMIN_PASSWORD": "not-a-password",
        },
    }}}
    path = data_dir / "mcp.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    return path


def author_with_claude(case: dict, model: str, mcp_config: Path, per_case_usd: float,
                       max_turns: int, timeout: int) -> dict:
    """Run the workout-author agent in print mode on one brief."""
    claude = shutil.which("claude") or "claude"
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}  # nested runs refuse otherwise
    cmd = [
        claude, "-p", PROMPT_TEMPLATE.format(brief=case["brief"]),
        "--agent", "workout-author",
        "--model", model,
        "--output-format", "json",
        "--json-schema", json.dumps(OUTPUT_SCHEMA),
        "--no-session-persistence",
        "--max-turns", str(max_turns),
        "--max-budget-usd", f"{per_case_usd:.2f}",
        "--mcp-config", str(mcp_config),
        "--strict-mcp-config",
        "--allowedTools", *ALLOWED_TOOLS,
    ]
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"timeout after {timeout}s", "seconds": timeout}
    seconds = round(time.monotonic() - started, 1)
    if proc.returncode != 0 and not proc.stdout.strip():
        return {"error": f"claude exited {proc.returncode}: {proc.stderr[-800:]}", "seconds": seconds}
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": f"unparseable output: {proc.stdout[-800:]}", "seconds": seconds}
    structured = extract_answer(payload)
    return {
        "session": structured.get("session"),
        "missing_brief": structured.get("missing_brief"),
        "notes": structured.get("notes"),
        "cost_usd": payload.get("total_cost_usd"),
        "turns": payload.get("num_turns"),
        "is_error": payload.get("is_error"),
        "stop_reason": payload.get("stop_reason"),
        "subtype": payload.get("subtype"),
        "terminal_reason": payload.get("terminal_reason"),
        "denials": payload.get("permission_denials"),
        "raw_result": str(payload.get("result") or "")[:3000],  # what the agent said when no session came back
        "seconds": seconds,
    }


def run_eval(cases: list[dict], model: str, parallel: int, budget_usd: float,
             per_case_usd: float, max_turns: int, timeout: int, keep_data: bool) -> dict:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="coach-eval-"))
    os.environ["COACH_DATA_DIR"] = str(data_dir)  # the harness renders against the same athlete
    os.environ["COACH_TOKEN_DIR"] = str(data_dir / "tokens")
    seed_info = seed_data_dir(data_dir)
    mcp_config = write_mcp_config(data_dir)
    print(f"eval data dir: {data_dir} (catalogue: {seed_info})")

    results: list[dict] = []
    spent = 0.0
    stopped = False

    def one(case):
        return case, author_with_claude(case, model, mcp_config, per_case_usd, max_turns, timeout)

    pending = list(cases)
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        while pending and not stopped:
            batch, pending = pending[:parallel], pending[parallel:]
            for case, answer in pool.map(one, batch):
                entry = {"id": case["id"], "sport": case["sport"], **answer}
                if "error" in answer:
                    entry.update({"pass": False, "failures": [answer["error"]]})
                else:
                    scored = evaluate_case(case, answer.get("session"), answer.get("missing_brief"))
                    entry.update(scored)
                    spent += float(answer.get("cost_usd") or 0.0)
                results.append(entry)
                mark = "pass" if entry["pass"] else "FAIL"
                print(f"  {case['id']:<28} {mark}  ${float(entry.get('cost_usd') or 0):.2f}  "
                      f"{entry.get('turns')} turns  {entry.get('seconds')}s")
                for f in entry.get("failures") or []:
                    print(f"      - {f}")
            if spent > budget_usd and pending:
                print(f"budget {budget_usd:.2f} USD exceeded ({spent:.2f}); {len(pending)} case(s) not run")
                stopped = True

    summary = summarise(results, cases, model, spent, seed_info)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"{stamp}-{model}.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "results": results}, fh, indent=2, default=str)
    print_summary(summary)
    print(f"\nresults: {out}")
    if not keep_data:
        shutil.rmtree(data_dir, ignore_errors=True)
    return summary


def summarise(results: list[dict], cases: list[dict], model: str, spent: float, seed_info: dict) -> dict:
    by_sport: dict[str, dict] = {}
    for r in results:
        bucket = by_sport.setdefault(r["sport"], {"run": 0, "passed": 0})
        bucket["run"] += 1
        bucket["passed"] += int(bool(r.get("pass")))
    run = len(results)
    passed = sum(int(bool(r.get("pass"))) for r in results)
    return {
        "model": model, "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cases_total": len(cases), "cases_run": run, "passed": passed,
        "pass_rate": round(passed / run, 3) if run else None,
        "cost_usd": round(spent, 2), "by_sport": by_sport, "seed": seed_info,
        "failed_ids": [r["id"] for r in results if not r.get("pass")],
    }


def print_summary(summary: dict) -> None:
    print("\n== Authoring eval ==")
    print(f"model {summary['model']}  passed {summary['passed']}/{summary['cases_run']} "
          f"(of {summary['cases_total']} cases)  cost ${summary['cost_usd']:.2f}")
    for sport, bucket in sorted(summary["by_sport"].items()):
        print(f"  {sport:<10} {bucket['passed']}/{bucket['run']}")
    if summary["failed_ids"]:
        print("  failed: " + ", ".join(summary["failed_ids"]))


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="render the goldens and apply their checks (no model)")
    mode.add_argument("--run", action="store_true", help="author every brief with claude -p (costs money)")
    parser.add_argument("--model", default="sonnet", help="model for --run (default: sonnet)")
    parser.add_argument("--cases", default="", help="comma-separated case ids (default: all)")
    parser.add_argument("--parallel", type=int, default=3)
    parser.add_argument("--budget-usd", type=float, default=6.0, help="stop starting cases past this spend")
    parser.add_argument("--per-case-usd", type=float, default=0.60, help="--max-budget-usd per case")
    parser.add_argument("--max-turns", type=int, default=16)
    parser.add_argument("--timeout", type=int, default=600, help="seconds per case")
    parser.add_argument("--keep-data", action="store_true", help="keep the temp data dir after --run")
    args = parser.parse_args(argv)

    cases = load_cases()
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",") if c.strip()}
        unknown = wanted - {c["id"] for c in cases}
        if unknown:
            parser.error(f"unknown case id(s): {sorted(unknown)}")
        cases = [c for c in cases if c["id"] in wanted]

    if args.check:
        return 1 if check_goldens(cases) else 0
    summary = run_eval(cases, args.model, args.parallel, args.budget_usd, args.per_case_usd,
                       args.max_turns, args.timeout, args.keep_data)
    return 0 if summary["cases_run"] and summary["passed"] == summary["cases_run"] else 1


if __name__ == "__main__":
    sys.exit(main())
