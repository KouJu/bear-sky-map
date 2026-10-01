"""Join sighting dates from the Miyagi Excel onto the already-geocoded GeoJSON.

The GeoJSON was generated without dates; re-geocoding takes ~15 min, so instead
we join on the record number. The Excel covers fiscal year 令和8年度 (April 2026
through March 2027) and only gives month/day, so months 4-12 are 2026 and
months 1-3 are 2027.
"""
import json
from pathlib import Path

import openpyxl

RAW_PATH = Path(__file__).parent.parent / "data" / "raw" / "miyagi_kuma.xlsx"
GEOJSON_PATH = Path(__file__).parent.parent / "data" / "processed" / "miyagi_kuma.geojson"

SHEET_NAME = "公表用シート（報告順）"
DATA_START_ROW = 5
FISCAL_YEAR_START = 2026  # 令和8年度


def main() -> None:
    wb = openpyxl.load_workbook(RAW_PATH, data_only=True)
    ws = wb[SHEET_NAME]

    date_by_number: dict[str, str] = {}
    for row in ws.iter_rows(min_row=DATA_START_ROW, values_only=True):
        number, month, day = row[0], row[1], row[2]
        if number is None or month is None or day is None:
            continue
        try:
            m = int(str(month).strip())
            d = int(str(day).strip())
        except ValueError:
            continue
        year = FISCAL_YEAR_START if m >= 4 else FISCAL_YEAR_START + 1
        date_by_number[str(number)] = f"{year}/{m}/{d}"

    geojson = json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))
    added = 0
    missing = 0
    for feature in geojson["features"]:
        number = str(feature["properties"].get("number"))
        date = date_by_number.get(number)
        if date:
            feature["properties"]["date"] = date
            added += 1
        else:
            missing += 1

    GEOJSON_PATH.write_text(json.dumps(geojson, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"added dates to {added} features ({missing} without a date)")


if __name__ == "__main__":
    main()
