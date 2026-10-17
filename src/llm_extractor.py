"""Generative-AI extraction step: Gemini with a JSON schema, fed only the relevant
excerpts of the crawled pages. Every prompt/response is logged under prompts/."""
import json
import os
import re
import time
from pathlib import Path

from extractor import FIELDS

LOG_DIR = Path(__file__).resolve().parent.parent / "prompts" / "pipeline_runs"

SYSTEM_PROMPT = """あなたは日本企業の公式サイトから企業情報を抽出するアシスタントです。
与えられたページ抜粋（URL付き）だけを根拠に、次の4項目を抽出してください。

- head_office_address: 本社（本店）所在地。郵便番号・電話番号・地図やアクセス案内の文言は除き、住所のみ（ビル名・階は含める）。
- representative: 代表者の氏名のみ（役職は含めない）。代表取締役が複数いる場合は社長を優先し、他の代表者は notes に記載。
- capital: 資本金。ページの表記どおり（例: 30,685百万円）。「(2026年3月31日現在)」等の注記は除く。
- established: 設立年月を「YYYY年M月」形式で（和暦は西暦に換算）。会社概要に年しか無い場合は沿革の該当年の「設立」の記載から月を取る。月の根拠が無ければ「YYYY年」とし、推測で補わない。

ルール:
- 抜粋に根拠が無い項目は value / source_url / evidence をすべて空文字にする。一般知識による補完は禁止。
- evidence には根拠となる抜粋の行を原文のまま（改変・要約せず）コピーする。複数行にまたがる場合は改行で区切る。
- source_url は evidence を含むページの URL をそのまま記載する。
- 「設立」と「創業」「創立」が両方ある場合は「設立」を採用する。"""

_FIELD = {
    "type": "object",
    "properties": {"value": {"type": "string"}, "source_url": {"type": "string"}, "evidence": {"type": "string"}},
    "required": ["value", "source_url", "evidence"],
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {**{f: _FIELD for f in FIELDS}, "notes": {"type": "string"}},
    "required": FIELDS + ["notes"],
}


class GeminiExtractor:
    def __init__(self, api_key: str, model: str | None = None):
        from google import genai  # lazy: the rule-based path runs without the SDK

        self.client = genai.Client(api_key=api_key)
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    def extract(self, company: str, excerpts: list[tuple[str, str]]) -> dict:
        """excerpts: [(url, text)]. Raises ValueError on unusable output so the caller
        can fall back to rule-based values and record why."""
        from google.genai import errors, types

        prompt = f"企業名: {company}\n\n" + "\n\n".join(f"### URL: {u}\n{t}" for u, t in excerpts)
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0,
            response_mime_type="application/json",
            response_json_schema=OUTPUT_SCHEMA,
        )
        for attempt in range(3):
            try:
                response = self.client.models.generate_content(model=self.model, contents=prompt, config=config)
                break
            except errors.APIError as exc:
                # 429 (free-tier rate limit) and 5xx are transient; anything else is final.
                if attempt == 2 or not (exc.code == 429 or exc.code >= 500):
                    self._log(company, prompt, f"ERROR {exc.code}: {exc}")
                    raise
                time.sleep(20 * (attempt + 1))
        raw = response.text or ""
        self._log(company, prompt, raw, response.usage_metadata)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON from model: {exc}") from exc
        if not all(isinstance(data.get(f), dict) for f in FIELDS):
            raise ValueError("model JSON is missing required fields")
        return data

    def _log(self, company: str, prompt: str, raw: str, usage=None) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        tokens = f"prompt_tokens={usage.prompt_token_count} output_tokens={usage.candidates_token_count}" if usage else "-"
        name = re.sub(r"[^\w]+", "_", company)
        (LOG_DIR / f"{name}.txt").write_text(
            f"model: {self.model}\nusage: {tokens}\n\n===== SYSTEM =====\n{SYSTEM_PROMPT}\n\n"
            f"===== USER =====\n{prompt}\n\n===== RESPONSE =====\n{raw}\n",
            encoding="utf-8",
        )
