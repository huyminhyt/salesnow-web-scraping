"""HTML -> plain text, plus label/value pairs from the tables and <dl> lists that
Japanese 会社概要 pages almost always use."""
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


def page_text(soup: BeautifulSoup) -> str:
    main = soup.find("main") or soup.body or soup
    lines = (normalize(line) for line in main.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def relevant_excerpt(soup: BeautifulSoup, max_chars: int = 5000) -> str:
    """Keep only what the LLM needs: label/value pairs, then text lines that mention
    a target label (with one line of context). Keeps prompts small on long pages."""
    pairs = [f"{k}: {v}" for k, v in label_value_pairs(soup) if _ANY_LABEL.search(k)]
    lines = page_text(soup).splitlines()
    picked = []
    for i, line in enumerate(lines):
        if _ANY_LABEL.search(line) and len(line) < 300:
            picked.extend(lines[i : i + 2])
    seen, out = set(), []
    for line in pairs + picked:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return "\n".join(out)[:max_chars]


def field_hits(soup: BeautifulSoup) -> int:
    """How many of the four target fields appear as a table/dl label on the page.
    Labels (not free text) avoid counting nav menus that merely say 会社概要."""
    keys = [k for k, _ in label_value_pairs(soup)]
    return sum(any(re.search(p, k, re.I) for k in keys) for p in FIELD_LABELS.values())
