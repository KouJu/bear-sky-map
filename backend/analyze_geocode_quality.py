"""Analyze the quality (not just success/failure) of the Miyagi geocoding sample."""
import json
from pathlib import Path

REPORT_PATH = Path(__file__).parent.parent / "data" / "processed" / "miyagi_geocode_report.json"


def main() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    log = report["log"]

    exact = []          # matched_title covers the full query text
    truncated = []       # matched fell back to a coarser (shorter) address than queried
    duplicated_muni = [] # district text itself repeats the municipality name

    for entry in log:
        query = entry["query"]
        matched = entry["matched_title"] or ""
        # crude duplication check: municipality name appears twice in query
        # (query = "宮城県" + municipality + district)
        prefix = "宮城県"
        rest = query[len(prefix):]
        for muni_len in range(2, 8):
            muni_guess = rest[:muni_len]
            if muni_guess and rest.count(muni_guess) >= 2 and len(muni_guess) >= 3:
                duplicated_muni.append(entry)
                break

        if matched == query:
            exact.append(entry)
        elif query.startswith(matched):
            truncated.append(entry)
        else:
            exact.append(entry)  # different but not a simple truncation; treat separately below

    truncation_chars = [len(e["query"]) - len(e["matched_title"] or "") for e in truncated]
    avg_truncation = sum(truncation_chars) / len(truncation_chars) if truncation_chars else 0

    print(f"sample size: {len(log)}")
    print(f"exact/full text match: {len(exact) - len(duplicated_muni)} (rough)")
    print(f"truncated to a coarser address (lost 字/番地 detail): {len(truncated)} "
          f"({len(truncated) / len(log):.1%}), avg {avg_truncation:.1f} chars dropped")
    print(f"municipality name duplicated in source 'district' field: {len(duplicated_muni)} "
          f"({len(duplicated_muni) / len(log):.1%})")
    print()
    print("Sample of truncated (coarser-than-requested) matches:")
    for e in truncated[:8]:
        print(f"  #{e['number']}: {e['query']}  ->  {e['matched_title']}")
    print()
    print("Sample of duplicated-municipality source rows:")
    for e in duplicated_muni[:8]:
        print(f"  #{e['number']}: {e['query']}  ->  {e['matched_title']}")


if __name__ == "__main__":
    main()
