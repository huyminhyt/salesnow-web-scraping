"""Rule-based field extraction (cross-check and fallback for the LLM), per-field cleaning,
evidence-based verification, and the merge of LLM and rule-based results."""
import re
from dataclasses import asdict, dataclass

from content import FIELD_LABELS, compact, founding_month, label_value_pairs, text_pairs, years_in

FIELDS = list(FIELD_LABELS)

# Ordered label preferences per field: the first pattern that matches any page wins,
# so 本社所在地 beats a generic 所在地 found elsewhere.
RULE_LABELS = {
    "head_office_address": [r"本社所在地|本社住所|本店所在地", r"^本社|^本店", r"^所在地$|^住所$"],
    "representative": [r"^代表者|代表者名", r"^代表$|代表取締役"],
    "capital": [r"^資本金"],
    "established": [r"^設立"],
}

POSTAL = re.compile(r"〒\s*(\d{3})\s*[-‐－ー]\s*(\d{4})")
ADDRESS_NOISE = re.compile(r"\s*(TEL|Tel|FAX|Fax|電話|MAP|Map|地図|本社地図|アクセス|Google|大きな地図|>|【).*$")
PREFECTURE = re.compile(r"(東京都|北海道|(?:京都|大阪)府|.{2,3}県)")
TITLE_WORDS = re.compile(r"代表取締役|代表執行役|取締役|執行役員|社長|会長|副|専務|常務|兼|CEO|COO|CFO|最高\S*?責任者|グループ|役員紹介|[()（）]")
CAPITAL = re.compile(r"[\d,，.．]+\s*(?:億|百万|千万|万)?[\d,，]*\s*(?:百万|千万|万)?円")


@dataclass
class FieldResult:
    value: str = ""
    source_url: str = ""
    evidence: str = ""
    verified: bool = False
    method: str = ""  # "llm" | "rule_based" | "" (not found)
    llm_value: str = ""
    rule_value: str = ""


def clean_address(raw: str) -> tuple[str, str]:
    """Returns (address, postal_code). Drops phone/map/access text that shares the cell."""
    postal = POSTAL.search(raw)
    text = POSTAL.sub(" ", raw)
    text = ADDRESS_NOISE.sub("", text).strip()
    pref = PREFECTURE.search(text)
    if pref:  # drop leading labels such as 新潟本社
        text = text[pref.start():]
    return text.strip(), (f"{postal.group(1)}-{postal.group(2)}" if postal else "")


def clean_representative(raw: str) -> str:
    """'代表取締役会長 玉木 康裕 代表取締役社長 玉木 伸弥' -> '玉木 伸弥': with several
    representative directors, the 社長 is the one SalesNow-style data lists."""
    people = [p for p in re.split(r"(?=代表取締役|代表執行役)", raw) if p.strip()]
    chosen = next((p for p in people if "社長" in p), people[0] if people else raw)
    names = [n.strip() for n in TITLE_WORDS.sub("|", chosen).split("|") if n.strip()]
    return names[0] if names else ""


def clean_capital(raw: str) -> str:
    m = CAPITAL.search(raw.replace(" ", ""))
    return m.group() if m else raw


def normalize_established(raw: str) -> str:
    """'1937年9月3日' -> '1937年9月'; '昭和39年(1964年)' -> '1964年' (month unknown)."""
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", raw) or re.search(r"(\d{4})[./](\d{1,2})", raw)
    if m:
        return f"{m.group(1)}年{int(m.group(2))}月"
    years = years_in(raw)
    return f"{min(years)}年" if years else ""


CLEANERS = {
    "head_office_address": lambda v: clean_address(v)[0],
    "representative": clean_representative,
    "capital": clean_capital,
    "established": normalize_established,
}

FORMATS = {
    "head_office_address": re.compile(r"^(東京都|北海道|京都府|大阪府|.{2,3}県).+\d"),
    "representative": re.compile(r"^[^\d\s]{1,8}(\s[^\d\s]{1,8})?$"),
    "capital": re.compile(r"\d.*円$"),
    "established": re.compile(r"^\d{4}年(\d{1,2}月)?$"),
}


def verify(field: str, value: str, evidence: str, page_text: str) -> bool:
    """The evidence must appear on the cited page, the value must be traceable to the
    evidence, and the value must have the expected shape."""
    # Each evidence line must be on the page; lines are checked separately because the model
    # may quote non-adjacent lines. Colons are dropped: excerpts render cells as "label: value".
    page = compact(page_text).replace(":", "")
    lines = [compact(line).replace(":", "") for line in evidence.splitlines() if line.strip()]
    if not value or not lines or any(line not in page for line in lines):
        return False
    # A timeline's "7月" occurs many times, so year/month/設立 must be quoted as one contiguous run.
    if field == "established" and "".join(lines) not in page:
        return False
    if not FORMATS[field].search(value):
        return False
    if field == "established":
        month = re.search(r"年(\d{1,2})月", value)
        ev = compact(evidence)
        return int(value[:4]) in years_in(ev) and (not month or f"{month.group(1)}月" in ev or re.search(rf"[./]0?{month.group(1)}(?!\d)", ev))
    if field == "capital":
        return re.sub(r"\D", "", value) in re.sub(r"\D", "", evidence)
    return compact(value) in compact(evidence)


def extract(pages: list[tuple[str, object]], page_texts: dict[str, str]) -> tuple[dict[str, FieldResult], str]:
    """pages: [(url, soup)] ranked best-first. Returns (fields, postal_code)."""
    pair_sources = [label_value_pairs, text_pairs]
    out = {f: FieldResult() for f in FIELDS}
    postal = ""
    for field, patterns in RULE_LABELS.items():
        hit = None
        for get_pairs in pair_sources:
            for pattern in patterns:
                hit = next(((u, k, v) for u, s in pages for k, v in get_pairs(s) if re.search(pattern, k) and CLEANERS[field](v)), None)
                if hit:
                    break
            if hit:
                break
        if not hit:
            continue
        url, key, raw = hit
        out[field] = FieldResult(CLEANERS[field](raw), url, f"{key} {raw}")
        if field == "head_office_address":
            postal = clean_address(raw)[1]

    est = out["established"]
    if re.fullmatch(r"\d{4}年", est.value):
        # Month missing on the profile page: look it up in the crawled 沿革 timeline.
        year = int(est.value[:4])
        for url, text in page_texts.items():
            found = founding_month(text, year)
            if found:
                out["established"] = FieldResult(f"{year}年{found[0]}月", url, found[1])
                break

    for field, r in out.items():
        r.verified = verify(field, r.value, r.evidence, page_texts.get(r.source_url, ""))
    return out, postal


def merge(llm_data: dict | None, rules: dict[str, FieldResult], page_texts: dict[str, str]) -> dict[str, FieldResult]:
    """The LLM value is used when it passes verification; otherwise the verified
    rule-based value is used, so an unsupported LLM answer never reaches the output."""
    merged = {}
    for f in FIELDS:
        rule = rules[f]
        llm = FieldResult()
        if llm_data:
            d = llm_data[f]
            llm = FieldResult(d.get("value", "").strip(), d.get("source_url", "").strip(), d.get("evidence", "").strip())
            llm.verified = verify(f, llm.value, llm.evidence, page_texts.get(llm.source_url, ""))
        if llm.verified or (llm.value and not rule.verified):
            chosen, method = llm, "llm"
        elif rule.value:
            chosen, method = rule, "rule_based"
        else:
            chosen, method = FieldResult(), ""
        merged[f] = FieldResult(chosen.value, chosen.source_url, chosen.evidence, chosen.verified, method, llm.value, rule.value)
    return merged


def as_dict(fields: dict[str, FieldResult]) -> dict:
    return {f: asdict(r) for f, r in fields.items()}
