"""Bounded best-first crawler: starts at the homepage and follows internal links that
look like company-profile pages, until the four target fields are covered."""
import heapq
import logging
import re
import time
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import requests

from content import FIELD_LABELS, founding_month, label_value_pairs, page_text, parse_html, years_in

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; SalesNowScrapingTest/1.0; +https://github.com/)"
HISTORY = re.compile(r"沿革|歴史|あゆみ|history|enkaku|rekishi|ayumi", re.I)
MONTH = re.compile(r"\d{1,2}\s*月|\d{4}[./]\d{1,2}|(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", re.I)
SKIP_EXT =re.compile(r"\.(pdf|jpe?g|png|gif|svg|zip|xlsx?|docx?|pptx?|mp4|mp3|css|js)$", re.I)

# (pattern, weight) matched against anchor text + URL path.
LINK_SCORES = [
    (r"会社概要|企業概要|会社案内|会社データ|基本情報|company\s*(profile|overview|outline|data)|corporate\s*(profile|data)|gaiyou|gaiyo|outline|profile|/info", 10),
    (r"企業情報|会社情報|について|about|corporate|company", 4),
    (r"沿革|歴史|history|enkaku|rekishi", 2),
    (r"/ir/|\bIR\b|news|recruit|saiyo|採用|product|商品|csr|sustainab|contact|privacy|sitemap|faq|gallery|movie|recipe", -6),
]


@dataclass
class Page:
    url: str
    html: bytes


@dataclass
class CrawlResult:
    pages: list[Page] = field(default_factory=list)
    visited: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def base_domain(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def in_scope(url: str, domain: str) -> bool:
    # Subdomains are allowed: e.g. sakataseed.co.jp keeps its profile on corporate.sakataseed.co.jp.
    host = urlparse(url).hostname or ""
    return host == domain or host.endswith("." + domain)


def score_link(text: str, url: str) -> int:
    target = f"{text} {urlparse(url).path}"
    return sum(w for pattern, w in LINK_SCORES if re.search(pattern, target, re.I))


class Crawler:
    def __init__(self, max_pages: int = 12, delay: float = 1.0, timeout: float = 20.0):
        self.max_pages = max_pages
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"})
        self._robots: dict[str, robotparser.RobotFileParser] = {}

    def allowed(self, url: str) -> bool:
        origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
        if origin not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                resp = self.session.get(origin + "/robots.txt", timeout=self.timeout)
                rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            self._robots[origin] = rp
        return self._robots[origin].can_fetch(USER_AGENT, url)

    def fetch(self, url: str) -> requests.Response:
        for attempt in range(2):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code >= 500 and attempt == 0:
                    time.sleep(2)
                    continue
                resp.raise_for_status()
                return resp
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 1:
                    raise
                time.sleep(2)
        raise RuntimeError("unreachable")

    def crawl(self, start_url: str) -> CrawlResult:
        result = CrawlResult()
        domain = base_domain(start_url)
        queue = [(0, 0, start_url)]  # (-score, seq, url)
        seen = {start_url}
        covered: set[str] = set()
        founding_year = None  # set when the profile lacks the month -> 沿革 pages only
        history_urls: set[str] = set()
        seq = 0

        while queue and len(result.visited) < self.max_pages:
            _, _, url = heapq.heappop(queue)
            if founding_year and url not in history_urls:
                continue
            if not self.allowed(url):
                result.errors.append(f"robots.txt disallows {url}")
                continue
            try:
                resp = self.fetch(url)
            except requests.RequestException as exc:
                result.errors.append(f"{url}: {exc}")
                continue
            finally:
                time.sleep(self.delay)

            final_url = urldefrag(resp.url)[0]
            if final_url in result.visited:  # two links redirecting to the same page
                continue
            seen.add(final_url)
            result.visited.append(final_url)
            if "html" not in resp.headers.get("Content-Type", "html"):
                continue
            if len(result.visited) == 1:
                # Homepage may redirect (umios.com -> /jp/); keep scope on the final host's domain too.
                domain = base_domain(final_url) if not in_scope(final_url, domain) else domain

            soup = parse_html(resp.content)
            result.pages.append(Page(final_url, resp.content))
            pairs = label_value_pairs(soup)
            covered |= {f for f, p in FIELD_LABELS.items() if any(re.search(p, k, re.I) for k, _ in pairs)}
            if founding_year:
                if founding_month(page_text(soup), founding_year):
                    break
            elif len(covered) == len(FIELD_LABELS):
                # Profiles often give only the year (e.g. 昭和18年); the month is then on the 沿革 page.
                founded = " ".join(v for k, v in pairs if re.search(FIELD_LABELS["established"], k, re.I))
                if MONTH.search(founded) or not years_in(founded):
                    break
                founding_year = min(years_in(founded))

            for a in soup.find_all("a", href=True):
                link = urldefrag(urljoin(final_url, a["href"]))[0]
                if link in seen or not link.startswith("http") or SKIP_EXT.search(urlparse(link).path):
                    continue
                if not in_scope(link, domain):
                    continue
                score = score_link(a.get_text(" ", strip=True), link)
                if score > 0:
                    seen.add(link)
                    if HISTORY.search(f"{a.get_text()} {link}"):
                        history_urls.add(link)
                    seq += 1
                    heapq.heappush(queue, (-score, seq, link))

        log.info("%s: visited %d pages, fields covered: %s", start_url, len(result.visited), sorted(covered))
        return result
