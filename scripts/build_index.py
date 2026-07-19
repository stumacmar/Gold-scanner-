#!/usr/bin/env python3
"""Build data/index.json listing every per-carat scan file for the dashboard.

Run after the scanner has produced data/<key>.json files (one per carat search).
The dashboard reads index.json first to populate its carat selector, then loads
the chosen scan file.
"""
import glob
import json
import os

DATA_DIR = "data"
INDEX_PATH = os.path.join(DATA_DIR, "index.json")


def main():
    searches = []
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "*.json"))):
        if os.path.basename(path) == "index.json":
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            meta = payload.get("meta", {})
        except (json.JSONDecodeError, OSError):
            continue
        key = os.path.splitext(os.path.basename(path))[0]
        label = ("Silver" if meta.get("metal") == "silver"
                 else f"{meta.get('default_carat', key)}ct")
        searches.append({
            "key": key,
            "label": label,
            "file": os.path.basename(path),
            "query": meta.get("query", ""),
            "generated_at": meta.get("generated_at"),
            "value_count": meta.get("value_count", 0),
            "total_analysed": meta.get("total_analysed", 0),
        })

    # Order by carat ascending where possible; Silver goes last.
    def sort_key(s):
        try:
            return int(str(s["label"]).replace("ct", ""))
        except ValueError:
            return 999   # "Silver" and anything non-numeric
    searches.sort(key=sort_key)

    latest = max((s["generated_at"] for s in searches if s["generated_at"]),
                 default=None)
    with open(INDEX_PATH, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": latest, "searches": searches}, fh,
                  indent=2, ensure_ascii=False)
    print(f"Wrote {INDEX_PATH} with {len(searches)} search(es): "
          + ", ".join(s["label"] for s in searches))


if __name__ == "__main__":
    main()
