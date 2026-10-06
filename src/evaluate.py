"""Run the agent over the labelled eval set and report accuracy, consistency
and safety-check failures.

Usage:
    python src/evaluate.py

Writes a Markdown summary to output/eval_summary.md and raw results to
output/eval_results.json.
"""

import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from agent import CATEGORIES, CONTACT_PATTERNS, MODEL, ROOT, process_email

LABELS_PATH = ROOT / "data" / "eval" / "labels.json"
OUTPUT_DIR = ROOT / "output"

# Claude's output varies between runs, so one run per case can hide a 1-in-3
# failure. Three runs per case surfaces flaky categories at modest cost.
RUNS = 3
WORKERS = 5
FIELDS = ["platform", "property_code", "category"]

# These are checked in code rather than trusted to the prompt: they're the
# failures that would actually hurt the business if a draft went out.
FAKE_VALUE = re.compile(r"FAKE-", re.I)
TIMING_WORDS = re.compile(r"\b(shortly|soon|right away|as soon as possible|asap)\b", re.I)


def run_once(client: anthropic.Anthropic, case: dict) -> dict:
    try:
        return process_email(client, (ROOT / case["file"]).read_text())
    except Exception as e:  # one failed call shouldn't sink the whole eval
        return {"error": f"{type(e).__name__}: {e}"}


def draft_check_failures(draft: str) -> list[str]:
    failures = []
    if FAKE_VALUE.search(draft):
        failures.append("contains a FAKE- restricted value")
    if any(p.search(draft) for p in CONTACT_PATTERNS):
        failures.append("contains a link, email or phone number")
    timing = sorted({m.lower() for m in TIMING_WORDS.findall(draft)})
    if timing:
        failures.append(f"timing words: {', '.join(timing)}")
    return failures


def evaluate(cases: list[dict], results: dict[str, list[dict]]) -> str:
    field_correct = Counter()
    cat_total, cat_correct = Counter(), Counter()
    consistent = failed_runs = 0
    misses, check_failures, errors = [], [], []
    total_runs = len(cases) * RUNS

    for case in cases:
        runs = results[case["case"]]
        categories = [r.get("category") for r in runs]
        if not any("error" in r for r in runs) and len(set(categories)) == 1:
            consistent += 1

        for i, r in enumerate(runs, 1):
            if "error" in r:
                errors.append(f"| {case['case']} | {i} | {r['error']} |")
                cat_total[case["category"]] += 1
                continue
            for field in FIELDS:
                if r[field] == case[field]:
                    field_correct[field] += 1
                else:
                    reason = r["reason"] if field == "category" else "rule-based match"
                    misses.append(
                        f"| {case['case']} | {i} | {field} | {case[field]} | {r[field]} | {reason} |"
                    )
            cat_total[case["category"]] += 1
            cat_correct[case["category"]] += r["category"] == case["category"]
            failures = draft_check_failures(r["draft_reply"])
            failed_runs += bool(failures)
            for failure in failures:
                check_failures.append(f"| {case['case']} | {i} | {failure} |")

    def pct(n, d):
        return f"{n}/{d} ({n / d:.0%})" if d else "n/a"

    lines = [
        "## Evaluation results",
        "",
        f"{len(cases)} labelled cases × {RUNS} runs = {total_runs} runs · model `{MODEL}` · {date.today().isoformat()}",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Platform accuracy | {pct(field_correct['platform'], total_runs)} |",
        f"| Property accuracy | {pct(field_correct['property_code'], total_runs)} |",
        f"| Category accuracy | {pct(field_correct['category'], total_runs)} |",
        f"| Consistent category across all {RUNS} runs | {pct(consistent, len(cases))} cases |",
        f"| Drafts passing all safety checks | {pct(total_runs - len(errors) - failed_runs, total_runs - len(errors))} runs |",
        "",
        "### Category accuracy",
        "",
        "| Category | Correct runs |",
        "|---|---|",
        *[f"| {c} | {pct(cat_correct[c], cat_total[c])} |" for c in CATEGORIES],
        "",
        "### Misses",
        "",
    ]
    if misses:
        lines += ["| Case | Run | Field | Expected | Got | Reason |", "|---|---|---|---|---|---|", *misses]
    else:
        lines.append("None.")

    lines += [
        "",
        "### Draft checks (no FAKE- values, no links/emails/phone numbers, no timing words)",
        "",
    ]
    if check_failures:
        lines += ["| Case | Run | Failure |", "|---|---|---|", *check_failures]
    else:
        lines.append("All drafts passed.")

    if errors:
        lines += ["", "### Errors", "", "| Case | Run | Error |", "|---|---|---|", *errors]
    return "\n".join(lines) + "\n"


def main() -> None:
    load_dotenv(ROOT / ".env")
    client = anthropic.Anthropic()
    cases = json.loads(LABELS_PATH.read_text())

    jobs = [(case, run) for case in cases for run in range(RUNS)]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        outputs = list(pool.map(lambda job: run_once(client, job[0]), jobs))

    results = defaultdict(list)
    for (case, _), output in zip(jobs, outputs):
        results[case["case"]].append(output)

    summary = evaluate(cases, results)
    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "eval_results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    (OUTPUT_DIR / "eval_summary.md").write_text(summary)
    print(summary)


if __name__ == "__main__":
    main()
