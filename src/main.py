"""Homepage -> crawl -> rule-based extraction -> verification -> JSON/CSV."""
import argparse
import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from content import field_hits, page_text, parse_html
from crawler import Crawler
from extractor import FIELDS, FieldResult, as_dict, extract

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("salesnow")


def process(company: str, url: str, crawler: Crawler) -> dict:
    record = {"company_name": company, "url": url, "status": "error", "postal_code": ""}
    crawl = crawler.crawl(url)
    record["crawled_pages"] = crawl.visited
    record["errors"] = crawl.errors

    soups = [(p.url, parse_html(p.html)) for p in crawl.pages]
    page_texts = {u: page_text(s) for u, s in soups}
    ranked = sorted(soups, key=lambda us: field_hits(us[1]), reverse=True)
    fields, postal = extract(ranked, page_texts) if soups else ({f: FieldResult() for f in FIELDS}, "")

    record["postal_code"] = postal
    record.update(as_dict(fields))
    found = [f for f in FIELDS if fields[f].value]
    if soups:
        record["status"] = "ok" if len(found) == len(FIELDS) else ("partial" if found else "not_found")
    record["missing_fields"] = [f for f in FIELDS if not fields[f].value]
    record["unverified_fields"] = [f for f in found if not fields[f].verified]
    return record


def write_outputs(records: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(exist_ok=True)
    (out_dir / "companies.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    columns = ["company_name", "url", "status", "postal_code"]
    for f in FIELDS:
        columns += [f, f"{f}_source_url", f"{f}_verified"]
    columns += ["missing_fields", "unverified_fields", "errors", "extracted_at"]
    # utf-8-sig so the CSV opens correctly in Japanese Excel.
    with open(out_dir / "companies.csv", "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for r in records:
            row = {c: r[c] for c in ("company_name", "url", "status", "postal_code", "extracted_at")}
            for f in FIELDS:
                row.update({f: r[f]["value"], f"{f}_source_url": r[f]["source_url"], f"{f}_verified": r[f]["verified"]})
            row.update({k: "; ".join(r[k]) for k in ("missing_fields", "unverified_fields", "errors")})
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(ROOT / "src" / "companies.csv"))
    parser.add_argument("--out", default=str(ROOT / "result"))
    parser.add_argument("--only", help="process only this company name")
    parser.add_argument("--max-pages", type=int, default=12)
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(args.input, encoding="utf-8") as fh:
        companies = [r for r in csv.DictReader(fh) if not args.only or r["company_name"] == args.only]

    crawler = Crawler(max_pages=args.max_pages, delay=args.delay)
    records = []
    for row in companies:
        log.info("=== %s %s", row["company_name"], row["url"])
        record = process(row["company_name"], row["url"], crawler)
        record["extracted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        records.append(record)
        log.info("%s: status=%s unverified=%s", row["company_name"], record["status"], record["unverified_fields"])

    write_outputs(records, Path(args.out))
    log.info("wrote %d records to %s", len(records), args.out)


if __name__ == "__main__":
    main()
