# SalesNow Webスクレイピング課題

上場企業10社の公式サイトのトップページからリンクをたどってクロールし、**生成AI（Google Gemini）** で
**本社住所・代表者氏名・資本金・設立年月** を抽出して JSON / CSV に出力するプログラムです。

```
requests + BeautifulSoup によるクロール
 → 関連部分の日本語テキストを抽出（抜粋）
 → Gemini API（JSON Schema による構造化出力）で抽出
 → Python 側で検証（根拠テキストが出典ページに実在するか・値の形式）
 → JSON / CSV に出力
```

Gemini にはクロール済みページの抜粋だけを渡します（モデル自身にWeb検索はさせません）。
ルールベースの抽出器も並行して動かしており、AIの回答の照合と、AIが使えない場合のフォールバックに使います。

## ディレクトリ構成

```
├── prompts/
│   ├── pipeline_runs/       # 本プログラムが Gemini に送ったプロンプトと実際の回答（1社1ファイル、実行のたびに上書き）
│   └── claude_code_session.txt  # 開発で使用した生成AI（Claude Code）との対話履歴
├── result/
│   ├── companies.json       # 抽出結果（根拠URL・根拠テキスト・抽出方法・検証結果つき）
│   └── companies.csv        # 同内容のフラット版（UTF-8 BOM、Excelで文字化けしない）
├── src/
│   ├── companies.csv        # 入力：企業名とURL（10社）
│   ├── main.py              # エントリポイント（クロール→抜粋→AI抽出→検証→出力）
│   ├── crawler.py           # 同一ドメイン内のベストファースト・クローラ
│   ├── content.py           # HTML→テキスト、ラベル/値ペア抽出、AI用の抜粋作成、沿革の解析
│   ├── llm_extractor.py     # Gemini による構造化抽出とプロンプト/回答のログ保存
│   ├── extractor.py         # ルールベース抽出（照合・フォールバック用）、検証、AI結果とのマージ
│   └── taskb_probe.py       # 課題B：Indeed のBot対策の観測用スクリプト
├── tests/test_extraction.py # 単体テスト（ネットワーク不要。Gemini はフェイクで代替）
├── .env.example             # APIキーの設定例（.env は Git 管理外）
├── requirements.txt
├── README.md
└── report.md                # 技術選定の比較、課題Bの考察
```

## 環境構築

Python 3.10 以上が必要です（Python 3.12 で動作を確認しました）。

```powershell
git clone https://github.com/huyminhyt/salesnow-web-scraping.git
cd salesnow-web-scraping
py -3.12 -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### APIキーの設定

1. https://aistudio.google.com/apikey で Gemini API キーを発行します（無料枠で10社分を処理できます）。
2. `.env.example` をコピーして `.env` を作成し、キーを設定します。`.env` は `.gitignore` 済みです。

```
GEMINI_API_KEY=発行したキー
GEMINI_MODEL=gemini-2.5-flash
```

キーが未設定の場合は警告を出し、ルールベース抽出のみで動作します（出力の `llm_status` に記録されます）。

## 実行方法

```powershell
# 10社すべてを処理して result/ に出力（約2分）
python src/main.py

# 1社だけ処理する
python src/main.py --only ホクト

# オプション
#   --input      入力CSV（既定: src/companies.csv。列: company_name,url）
#   --out        出力ディレクトリ（既定: result）
#   --max-pages  1社あたりの最大取得ページ数（既定: 12）
#   --delay      リクエスト間隔（秒、既定: 1.0）
#   --no-llm     生成AIを使わず、ルールベースのみで実行
```

Windows のコンソールで日本語ログが文字化けする場合は、`python -X utf8 src/main.py` で実行してください。
入力CSVに行を追加すれば、他の企業にもそのまま適用できます。

```powershell
python -m pytest -q tests        # 単体テスト
python src/taskb_probe.py        # 課題B（任意）の観測
```

## 処理の流れ

1. **クロール（`crawler.py`）**
   - トップページから開始し、リンクのアンカーテキストとURLをスコアリングして、スコアの高いリンクから順に辿ります（会社概要・企業概要・company/profile・outline・gaiyou 等は加点、IR・ニュース・採用・商品等は減点）。
   - 同一サイトのドメインとそのサブドメインのみを対象にします（例：`corporate.sakataseed.co.jp` は許可、外部の親会社サイトは除外）。
   - `robots.txt` を遵守し、リクエスト間隔は1秒、タイムアウトは20秒、5xx・接続エラーは1回リトライします。リダイレクト先も含めてURLを重複排除します。
   - 4項目のラベルが揃った時点で停止します。ただし設立が「昭和18年(1943年)」のように月を含まない場合は、沿革ページ（沿革・歴史・history・rekishi）に限定して追加でクロールします。
2. **抜粋（`content.relevant_excerpt`）**
   - 4項目に関係するページ（最大3ページ）と沿革ページについて、表・dl のラベル/値ペアと、対象ラベルを含む行とその前後の行だけを取り出します。ナビゲーションやフッターは送りません。
   - 1社あたりの入力は約450〜2,200トークンです。
3. **生成AIによる抽出（`llm_extractor.py`）**
   - Gemini（`gemini-2.5-flash`、temperature 0）に、日本語のシステムプロンプトと JSON Schema（`response_json_schema`）を渡します。
   - 各項目について `value`・`source_url`・`evidence`（原文のままの根拠行）を返させます。根拠が無ければ空文字とし、推測による補完を禁止しています。
   - 429/5xx はリトライします。不正なJSONや必須項目の欠落は例外として扱い、`llm_status` に記録します。
   - 送ったプロンプトと実際の回答は `prompts/pipeline_runs/` に保存します。
4. **検証とマージ（`extractor.py`）**
   - 根拠の各行が出典ページに実在するか、値が根拠から導出できるか、値の形式が正しいかを確認します（都道府県から始まる住所、`円` で終わる金額、`YYYY年M月` 等）。設立年月は、沿革の年・月・「設立」の行が連続して引用されていることも確認します。
   - 検証を通ったAIの値を採用します。通らなかった場合は、検証済みのルールベースの値を採用します（`method` に記録）。
   - 根拠の無いAIの回答が、検証済みとして出力されることはありません。
5. **出力**：`result/companies.json` と `result/companies.csv` に出力します。

## 出力スキーマ

`companies.json` の1要素：

| キー | 内容 |
|---|---|
| `company_name`, `url` | 入力値 |
| `status` | `ok`（4項目取得）/ `partial` / `not_found` / `error`（ページ取得不可） |
| `postal_code` | 本社の郵便番号（記載がある場合） |
| `head_office_address` / `representative` / `capital` / `established` | `{value, source_url, evidence, verified, method, llm_value, rule_value}`。`method` は `llm` / `rule_based` |
| `llm_model`, `llm_status`, `llm_notes` | 使用モデル、AI抽出の結果（`ok` / `error: …` / `disabled`）、AIの補足（例：他の代表者） |
| `crawled_pages`, `llm_input_pages` | 取得したURL / AIに渡したURL |
| `missing_fields` / `unverified_fields` | 未取得の項目 / 検証に通らなかった項目 |
| `errors`, `extracted_at` | HTTPエラー等、取得日時（UTC） |

CSVは同じ情報を1社1行に展開したものです（`<項目>`, `<項目>_source_url`, `<項目>_method`, `<項目>_verified` など）。

## 実行結果（2026-10-17、gemini-2.5-flash）

10社すべてで `status=ok` となり、**40項目すべてが生成AIの抽出値で、すべて検証に通りました**。ルールベースの値とも40/40で一致しています。

| 企業 | 本社住所 | 代表者 | 資本金 | 設立年月 |
|---|---|---|---|---|
| ニッスイ | 東京都港区西新橋1-3-1 西新橋スクエア | 田中輝 | 30,685百万円 | 1943年3月 ※沿革 |
| Ｕｍｉｏｓ | 東京都港区高輪二丁目21番2号 THE LINKPILLAR 1 SOUTH | 安田 大助 | 200億円 | 1943年3月 |
| ユキグニファクトリー | 新潟県南魚沼市余川89番地 | 湯澤 尚史 | 100百万円 | 1983年7月 |
| サカタのタネ | 神奈川県横浜市都筑区仲町台二丁目7番1号 | 加々美 勉 | 135億円 | 1942年12月 |
| ホクト | 長野県長野市南堀138-1 | 水野雅義 | 55億円 | 1964年7月 ※沿革 |
| ショーボンドホールディングス | 東京都中央区日本橋箱崎町7番8号 | 岸本 達也 | 50億円 | 2008年1月 |
| ミライト・ワン | 東京都港区虎ノ門2-2-3 | 菅原 英宗 | 70億円 | 2010年10月 |
| タマホーム | 東京都港区高輪3丁目22番9号 タマホーム本社ビル | 玉木 伸弥 | 43億1,014万円 | 1998年6月 |
| 極洋 | 東京都港区赤坂三丁目3番5号 | 井上 誠 | 75億2700万円 | 1937年9月 |
| 日本アクア | 東京都港区港南2-16-2 太陽生命品川ビル20F | 中村 文隆 | 19億3百万円 | 2004年11月 |

※沿革：会社概要ページには年のみの記載だったため、沿革ページの記載から月を取得しました。

注意点：
- 代表取締役が複数いる企業（タマホーム、サカタのタネ）は社長を採用しています。他の代表者は `llm_notes` に記録しています。
- ニッスイの設立月は、沿革の「1943年3月 日本海洋漁業統制株式会社設立」（現ニッスイの前身となる法人の設立）から取得しています。
