"""Homepage -> crawl -> relevant excerpts -> Gemini extraction (+ rule-based cross-check)
-> evidence verification -> JSON/CSV."""
import argparse
import csv
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from content import field_hits, page_text, parse_html, relevant_excerpt
from crawler import HISTORY, Crawler
from extractor import FIELDS, FieldResult, as_dict, extract, merge
from llm_extractor import GeminiExtractor

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("salesnow")


def process(company: str, url: str, crawler: Crawler, llm: GeminiExtractor | None) -> dict:
    record = {"company_name": company, "url": url, "status": "error", "postal_code": "",
              "llm_model": llm.model if llm else None, "llm_status": "disabled" if llm is None else "not_run"}
    crawl = crawler.crawl(url)
    record["crawled_pages"] = crawl.visited
    record["errors"] = crawl.errors

    soups = [(p.url, parse_html(p.html)) for p in crawl.pages]
    page_texts = {u: page_text(s) for u, s in soups}
    ranked = sorted(soups, key=lambda us: field_hits(us[1]), reverse=True)
    rules, postal = extract(ranked, page_texts) if soups else ({f: FieldResult() for f in FIELDS}, "")

    llm_data = None
    # Only profile-like pages (and the 沿革 page, if crawled for the month) go to the model.
    chosen = [us for us in ranked if field_hits(us[1]) > 0][:3]
    chosen += [us for us in soups if HISTORY.search(us[0]) and us not in chosen][-1:]
    excerpts = [(u, e) for u, e in ((u, relevant_excerpt(s)) for u, s in chosen) if e]
    record["llm_input_pages"] = [u for u, _ in excerpts]
    if llm and excerpts:
        try:
            llm_data = llm.extract(company, excerpts)
            record["llm_status"] = "ok"
            record["llm_notes"] = llm_data.get("notes", "")
        except Exception as exc:  # API or output-validation failure: keep rule-based values, record why
            log.warning("%s: LLM extraction failed: %s", company, exc)
            record["llm_status"] = f"error: {type(exc).__name__}: {exc}"[:500]
    fields = merge(llm_data, rules, page_texts)

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
        columns += [f, f"{f}_source_url", f"{f}_method", f"{f}_verified"]
    columns += ["missing_fields", "unverified_fields", "llm_model", "llm_status", "errors", "extracted_at"]
    # utf-8-sig so the CSV opens correctly in Japanese Excel.
    with open(out_dir / "companies.csv", "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for r in records:
            row = {c: r[c] for c in ("company_name", "url", "status", "postal_code", "llm_model", "llm_status", "extracted_at")}
            for f in FIELDS:
                row.update({f: r[f]["value"], f"{f}_source_url": r[f]["source_url"], f"{f}_method": r[f]["method"], f"{f}_verified": r[f]["verified"]})
            row.update({k: "; ".join(r[k]) for k in ("missing_fields", "unverified_fields", "errors")})
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(ROOT / "src" / "companies.csv"))
    parser.add_argument("--out", default=str(ROOT / "result"))
    parser.add_argument("--only", help="process only this company name")
    parser.add_argument("--max-pages", type=int, default=12)
    parser.add_argument("--no-llm", action="store_true", help="rule-based extraction only")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    api_key = os.getenv("GEMINI_API_KEY")
    if not args.no_llm and not api_key:
        log.warning("GEMINI_API_KEY is not set: running rule-based extraction only")
    llm = GeminiExtractor(api_key) if api_key and not args.no_llm else None

    with open(args.input, encoding="utf-8") as fh:
        companies = [r for r in csv.DictReader(fh) if not args.only or r["company_name"] == args.only]

    crawler = Crawler(max_pages=args.max_pages, delay=args.delay)
    records = []
    for row in companies:
        log.info("=== %s %s", row["company_name"], row["url"])
        record = process(row["company_name"], row["url"], crawler, llm)
        record["extracted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        records.append(record)
        log.info("%s: status=%s llm=%s methods=%s unverified=%s", row["company_name"], record["status"], record["llm_status"],
                 [record[f]["method"] for f in FIELDS], record["unverified_fields"])

    write_outputs(records, Path(args.out))
    log.info("wrote %d records to %s", len(records), args.out)


if __name__ == "__main__":
    main()
