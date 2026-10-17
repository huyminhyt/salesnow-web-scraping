"""Task B: observe (not bypass) the bot protection on Indeed's job search page."""
import re

import requests

URL = "https://vn.indeed.com/jobs?q=remote"
USER_AGENTS = {
    "python-requests default": requests.utils.default_user_agent(),
    "desktop Chrome UA": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
}
MARKERS = ["challenge-platform", "cf-chl", "turnstile", "captcha", "mosaic-provider-jobcards"]


def main() -> None:
    for label, ua in USER_AGENTS.items():
        resp = requests.get(URL, headers={"User-Agent": ua, "Accept-Language": "vi,en;q=0.8"}, timeout=20)
        title = re.search(r"<title>(.*?)</title>", resp.text, re.S)
        print(f"[{label}] status={resp.status_code} server={resp.headers.get('Server')} "
              f"cf-mitigated={resp.headers.get('Cf-Mitigated')} title={title.group(1).strip() if title else None!r} "
              f"markers={[m for m in MARKERS if m in resp.text]}")


if __name__ == "__main__":
    main()
