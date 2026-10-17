import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from content import founding_month, label_value_pairs, page_text, parse_html, text_pairs, years_in  # noqa: E402
from crawler import base_domain, in_scope, score_link  # noqa: E402
from extractor import (  # noqa: E402
    clean_address, clean_capital, clean_representative, extract, normalize_established, verify,
)

PROFILE = """<html><body><nav><a href="/company/">会社概要</a></nav>
<table>
<tr><th>本社所在地</th><td>〒108-0074 東京都港区高輪3丁目22番9号 タマホーム本社ビル TEL 03-6408-1200</td></tr>
<tr><th>代表者</th><td>代表取締役会長 玉木 康裕<br>代表取締役社長 玉木 伸弥</td></tr>
<tr><th>資本金</th><td>43億1,014万円 (2026年5月31日 現在)</td></tr>
<tr><th>設立</th><td>昭和39年(1964年)</td></tr>
</table></body></html>"""

HISTORY = """<html><body><ul>
<li>昭和38年</li><li>10月</li><li>関連会社を設立</li>
<li>昭和39年</li><li>7月</li><li>長野市においてデラップス商事株式会社を設立</li>
</ul></body></html>"""


def test_label_value_pairs_from_table_and_dl():
    soup = parse_html(PROFILE.replace("</table>", "</table><dl><dt>設立年月</dt><dd>1983年7月</dd></dl>"))
    pairs = dict(label_value_pairs(soup))
    assert pairs["資本金"] == "43億1,014万円 (2026年5月31日 現在)"
    assert pairs["設立年月"] == "1983年7月"


def test_text_pairs_for_div_layouts():
    soup = parse_html("<div><p>資本金</p><p>70億円</p><p>設立</p><p>2010年10月1日</p></div>")
    assert ("資本金", "70億円") in text_pairs(soup)


def test_clean_address_drops_postal_phone_and_prefix_label():
    assert clean_address("新潟本社 〒949-6695 新潟県南魚沼市余川89番地 TEL:025-778-0111(代表)") == ("新潟県南魚沼市余川89番地", "949-6695")
    assert clean_address("東京都港区赤坂三丁目3番5号 本社地図")[0] == "東京都港区赤坂三丁目3番5号"


def test_clean_representative_prefers_president():
    assert clean_representative("代表取締役会長 玉木 康裕 代表取締役社長 玉木 伸弥") == "玉木 伸弥"
    assert clean_representative("代表取締役社長執行役員 最高執行責任者 安田 大助") == "安田 大助"
    assert clean_representative("代表取締役社長 CEO 菅原 英宗") == "菅原 英宗"


def test_clean_capital_strips_notes():
    assert clean_capital("19億3百万円(2025年12月31日現在)") == "19億3百万円"
    assert clean_capital("30,685百万円") == "30,685百万円"


def test_normalize_established():
    assert normalize_established("1937年9月3日") == "1937年9月"
    assert normalize_established("昭和18年(1943年)") == "1943年"
    assert normalize_established("昭和39年") == "1964年"


def test_years_in_converts_wareki():
    assert years_in("昭和39年 平成元年") == {1964, 1989}


def test_founding_month_picks_matching_year_only():
    text = page_text(parse_html(HISTORY))
    month, evidence = founding_month(text, 1964)
    assert month == 7 and "デラップス商事" in evidence
    assert founding_month(text, 1970) is None


def test_extract_end_to_end_with_history_page():
    pages = [("https://ex.jp/profile", parse_html(PROFILE)), ("https://ex.jp/history", parse_html(HISTORY))]
    texts = {u: page_text(s) for u, s in pages}
    fields, postal = extract(pages, texts)
    assert postal == "108-0074"
    assert fields["head_office_address"].value == "東京都港区高輪3丁目22番9号 タマホーム本社ビル"
    assert fields["representative"].value == "玉木 伸弥"
    assert fields["capital"].value == "43億1,014万円"
    assert fields["established"].value == "1964年7月"
    assert fields["established"].source_url == "https://ex.jp/history"
    assert all(f.verified for f in fields.values())


def test_verify_rejects_evidence_not_on_page():
    assert not verify("capital", "50億円", "資本金 50億円", "資本金 70億円")
    assert not verify("established", "1964年8月", "昭和39年 7月 設立", "昭和39年 7月 設立")


def test_scope_allows_subdomains_only_of_same_site():
    domain = base_domain("https://www.sakataseed.co.jp/")
    assert in_scope("https://corporate.sakataseed.co.jp/company/", domain)
    assert not in_scope("https://www.yamada-holdings.jp/company/", domain)


def test_link_scoring_prefers_profile_over_ir():
    assert score_link("会社概要", "https://x.jp/company/profile/") > score_link("IR情報", "https://x.jp/ir/")
    assert score_link("ニュース", "https://x.jp/news/") <= 0


# --- LLM step (no network: a fake Gemini client stands in for the API) ---

import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from content import relevant_excerpt  # noqa: E402
from extractor import FieldResult, merge  # noqa: E402
from llm_extractor import GeminiExtractor  # noqa: E402


def _fake_llm(text, tmp_path, monkeypatch):
    import llm_extractor

    monkeypatch.setattr(llm_extractor, "LOG_DIR", tmp_path)
    llm = GeminiExtractor.__new__(GeminiExtractor)
    llm.model = "fake"
    usage = SimpleNamespace(prompt_token_count=1, candidates_token_count=1)
    llm.client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **_: SimpleNamespace(text=text, usage_metadata=usage)))
    return llm


def _llm_json(**overrides):
    base = {f: {"value": "", "source_url": "", "evidence": ""} for f in ("head_office_address", "representative", "capital", "established")}
    base.update(overrides)
    return {**base, "notes": ""}


def test_llm_output_is_parsed_and_logged(tmp_path, monkeypatch):
    payload = _llm_json(capital={"value": "55億円", "source_url": "u", "evidence": "資本金: 55億円"})
    llm = _fake_llm(json.dumps(payload, ensure_ascii=False), tmp_path, monkeypatch)
    assert llm.extract("テスト", [("u", "資本金: 55億円")])["capital"]["value"] == "55億円"
    log = (tmp_path / "テスト.txt").read_text(encoding="utf-8")
    assert "===== USER =====" in log and "資本金: 55億円" in log


@pytest.mark.parametrize("bad", ["not json", json.dumps({"notes": ""})])
def test_invalid_llm_output_raises(bad, tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        _fake_llm(bad, tmp_path, monkeypatch).extract("テスト", [("u", "x")])


def test_merge_rejects_unsupported_llm_value_in_favour_of_verified_rule():
    page = {"u": "資本金\n70億円"}
    rules = {f: FieldResult() for f in ("head_office_address", "representative", "capital", "established")}
    rules["capital"] = FieldResult("70億円", "u", "資本金 70億円", verified=True)
    hallucinated = _llm_json(capital={"value": "80億円", "source_url": "u", "evidence": "資本金: 80億円"})
    merged = merge(hallucinated, rules, page)
    assert merged["capital"].value == "70億円" and merged["capital"].method == "rule_based"
    assert merged["capital"].llm_value == "80億円"


def test_verify_accepts_nonadjacent_quoted_lines_but_not_for_established():
    page = "本社\n東京都港区1-1\n電話\n03\n設立\n1964年\n沿革\n7月\n設立"
    assert verify("head_office_address", "東京都港区1-1", "本社: 東京都港区1-1\n東京都港区1-1", page)
    assert not verify("established", "1964年7月", "1964年\n7月", page)


def test_excerpt_keeps_labels_and_drops_navigation():
    soup = parse_html("<nav><a>ニュース</a><a>採用</a></nav>" + PROFILE.split("<body>")[1])
    excerpt = relevant_excerpt(soup)
    assert "資本金: 43億1,014万円" in excerpt and "ニュース" not in excerpt
