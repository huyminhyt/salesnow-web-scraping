"""HTML -> plain text, label/value pairs (tables, <dl>, label-per-line layouts) and
the 沿革 timeline parser used to recover the founding month."""
import re
import unicodedata

from bs4 import BeautifulSoup

FIELD_LABELS = {
    "head_office_address": r"本社所在地|本社住所|本店所在地|本社|本店|所在地|住所|head\s*office|headquarters|address",
    "representative": r"代表者|代表取締役|代表|社長|representative|president|ceo",
    "capital": r"資本金|capital",
    "established": r"設立|創立|established|founded|incorporated",
}
_ANY_LABEL = re.compile("|".join(FIELD_LABELS.values()), re.I)


def normalize(text: str) -> str:
    """NFKC + collapse whitespace; used for matching, never for output values."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def parse_html(html: bytes | str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "template"]):
        tag.decompose()
    return soup


def label_value_pairs(soup: BeautifulSoup) -> list[tuple[str, str]]:
    pairs = []
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) >= 2:
            pairs.append((cells[0].get_text(" ", strip=True), " ".join(c.get_text(" ", strip=True) for c in cells[1:])))
    for dl in soup.find_all("dl"):
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                pairs.append((dt.get_text(" ", strip=True), dd.get_text(" ", strip=True)))
    return [(normalize(k), normalize(v)) for k, v in pairs if k.strip() and v.strip() and len(k) <= 40]


def text_pairs(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Fallback for profiles laid out with divs instead of tables: a short line that is
    exactly a known label, followed by its value on the next line."""
    lines = page_text(soup).splitlines()
    return [(lines[i], lines[i + 1]) for i in range(len(lines) - 1)
            if len(lines[i]) <= 8 and _ANY_LABEL.fullmatch(lines[i].strip())]


_ERAS = {"明治": 1867, "大正": 1911, "昭和": 1925, "平成": 1988, "令和": 2018}


def years_in(text: str) -> set[int]:
    """Western years mentioned in text, with wareki (昭和39年 etc.) converted."""
    years = {int(y) for y in re.findall(r"(?<!\d)(1[89]\d\d|20\d\d)(?!\d)", text)}
    for era, offset in _ERAS.items():
        years |= {offset + (1 if n == "元" else int(n)) for n in re.findall(rf"{era}\s*(\d+|元)\s*年", text)}
    return years


def founding_month(text: str, year: int) -> tuple[int, str] | None:
    """Scan a 沿革 timeline for the first 設立 entry in `year` and return (month, evidence).
    Timelines put 'YYYY年' / 'M月' on their own lines before the event, so we carry the
    most recent year/month forward line by line."""
    cur_year, month, ctx = None, None, []
    for line in text.splitlines():
        found = years_in(line)
        if found and re.fullmatch(r"[\d年月日\s.()/明治大正昭和平成令和元]+", line):
            cur_year, month, ctx = min(found), None, [line]
        m = re.search(r"(?<!\d)(\d{1,2})\s*月", line)
        if m and cur_year and len(line) <= 12:
            month, ctx = int(m.group(1)), ctx + [line] if line not in ctx else ctx
        if "設立" in line and cur_year == year and month:
            return month, " ".join(ctx + [line])
    return None


def page_text(soup: BeautifulSoup) -> str:
    lines = (normalize(line) for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def relevant_excerpt(soup: BeautifulSoup, max_chars: int = 4000) -> str:
    """What the LLM gets for one page: label/value pairs, then lines mentioning a target
    label plus the line after it. Keeps prompts small and free of nav/footer noise."""
    pairs = [f"{k}: {v}" for k, v in label_value_pairs(soup) if _ANY_LABEL.search(k)]
    lines = page_text(soup).splitlines()
    picked = []
    for i, line in enumerate(lines):
        if len(line) < 200 and (_ANY_LABEL.search(line) or re.fullmatch(r"\d{1,2}\s*月", line)):
            picked.extend(lines[max(i - 1, 0) : i + 2])
    covered = compact("".join(pairs)).replace(":", "")
    picked = [line for line in picked if compact(line) not in covered]  # already shown as a pair
    return "\n".join(dict.fromkeys(pairs + picked))[:max_chars]


def field_hits(soup: BeautifulSoup) -> int:
    """How many of the four target fields appear as a table/dl label on the page.
    Labels (not free text) avoid counting nav menus that merely say 会社概要."""
    keys = [k for k, _ in label_value_pairs(soup)]
    return sum(any(re.search(p, k, re.I) for k in keys) for p in FIELD_LABELS.values())
