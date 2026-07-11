# xtcli — xTranslator ヘッドレス CLI ラッパー

[xTranslator](https://www.nexusmods.com/skyrimspecialedition/mods/134)（Skyrim / Fallout / Starfield MOD 翻訳ツール）を CLI から自動実行するためのラッパーです。エージェント（LLM）による翻訳ワークフローを想定しています。

esp / esm / esl / pex / MCM 翻訳テキストのすべてを **xTranslator 本体のコード**で読み書きするため、ファイル形式の互換性は GUI 操作と完全に同一です。

## 仕組み

xTranslator v1.4.16+ に搭載されている **BatchProcessor**（スクリプトによる一括処理機能）を利用します。ただし同機能は GUI からしか起動できず、原文抽出コマンドも持たないため、xtcli は次の 2 つの仕掛けで補います。

1. **偽 OpenAI API サーバー** — xTranslator の翻訳 API 設定（`OpenAI_Url`）を `http://127.0.0.1:<port>` に向け、バッチの `apitranslation` コマンドで xTranslator 自身に全対象文字列を送信させます。
   - `extract`: サーバーは受信した原文を記録し、そのまま返す（抽出）
   - `apply`: サーバーは JSON の翻訳表から訳文を返す（適用）→ `finalize` で出力
2. **最小限の UI 自動化**（pywinauto + Windows メッセージ）— バッチスクリプトを `~processor.tmp` として配置しておき、「Wizards → BatchProcessor を開く」（メニュー項目へ `WM_COMMAND` を送信）「Run Processor をクリック」の 2 操作だけを行います。実マウス/キーボード入力を使わないため、フォアグラウンドウィンドウが無い環境でも動作します。完了はセンチネル文字列の受信で検知します。

## 必要なもの

- **xTranslator v1.4.16 以降**（推奨: 最新版）— [Nexus](https://www.nexusmods.com/skyrimspecialedition/mods/134) から手動ダウンロード（要ログイン）
- Python 3.11+ / `pip install pywinauto`
- Windows（xTranslator が動く環境）

## セットアップ

```console
pip install -e .
xtcli setup --exe "C:\Tools\xTranslator\xTranslator.exe" --game skyrimse
xtcli doctor
```

`setup` は以下を行います（元ファイルは `*.xtcli-bak` として一度だけバックアップ。`xtcli setup --restore` で復元）:

- `UserPrefs/commonApiPrefs.ini` — OpenAI API をローカルサーバーへ向ける
- `Misc/ApiTranslator.txt` — 1 リクエスト 1 文字列化・スロットリング解除
- `UserPrefs/commonPrefs.ini` — UI 言語を English に固定（自動化の安定用）、finalize 確認ダイアログを自動 Yes に

> フレッシュインストールの場合は一度 GUI を手動起動してください（初回のゲームパス設定ダイアログは自動化できません）。
> xTranslator は終了時に prefs を書き戻すため設定がずれることがありますが、xtcli は毎回実行前に自動で再適用します。

## 使い方（エージェント翻訳ワークフロー）

```console
# 1. 原文抽出（辞書があれば --sst で先に適用し、未翻訳分だけ抽出）
xtcli extract MyMod.esp --from english --to japanese -o strings.json

# 2. strings.json の "trans" をエージェント（または人間）が埋める

# 3. 適用と finalize
xtcli apply MyMod.esp -t strings.json -o out\ --missing-out missing.json
```

- `strings.json`: `{"strings": [{"orig": "...", "trans": ""}]}` 形式。翻訳時は `<a_A>`（エイリアス）、`999`（数値）、`<Alias=...>`、`<L_F>` 等のトークンを必ず原文どおり残すこと。
- `apply` は入力ファイルをステージングにコピーしてから処理するため、元ファイルは変更されません。出力は `-o` のディレクトリに集められます。
- 複数ファイル / SST 辞書の併用可: `xtcli extract a.esp b.esp --sst dic.sst`
- 生の BatchProcessor スクリプトの実行: `xtcli run script.txt`

## 動作確認済み（xTranslator 1.6.3b / SkyrimSE）

- **非ローカライズ esp**（FULL 等の埋め込み文字列）: バイナリ往復で文字列サブレコードのみ変化、他は完全一致
- **ローカライズ プラグイン**（.STRINGS/.DLSTRINGS/.ILSTRINGS）: 訳文が正しく書き込まれ、`<a_A>` 正規化されたエイリアスが実ゲーム変数（例 `<dur>`）へ自動復元される
- **MCM 翻訳テキスト**: UTF-16 往復で訳文一致
- **PEX**: ロード経路の動作確認（翻訳対象文字列を持つサンプルでの往復は未検証）

## 出力先について

`apply` の出力は `-o` で指定したディレクトリに集約されます。xTranslator はファイル種別ごとに出力先を変える（esp は同フォルダ、ローカライズは `Strings\`、pex は `Scripts\`、MCM は `Interface\Translations\`）ため、xtcli がそれらの `xtcli_out` 一時フォルダを回収・削除して `-o` にまとめます。

## 制限・注意

- 実行中 xTranslator のウィンドウが表示されます（数秒〜、対象ファイルのサイズ次第）。
- `apitranslation` は「未翻訳またはpartial」の文字列だけを送るため、SST 適用済み・翻訳済みの文字列は抽出されません（これがハイブリッド運用の仕組みです）。
- `extract` の原文に現れる `<a_A>` はエイリアス、`999` は数値のプレースホルダ（xTranslator が正規化したもの）。翻訳時はそのまま残せば、finalize 時に実際のゲーム変数へ復元されます。
- PEX の finalize はロードしたファイル自体を上書きするため、xtcli はステージングにコピーして処理し結果を回収します。
- 長文（3500 文字超の複数行文字列）は生の改行で送受信されます。仕組み上の対応済みです。
- 完了はセンチネル文字列の受信で検知します。万一検知できない場合は `idle_timeout` 秒（既定 120、`xtcli.toml`）の無通信でフォールバック終了します。大量ファイルで途中終了する場合は延長してください。

## 開発

```console
python -m pytest tests/
```

ソース調査メモ: プロトコルの根拠はすべて xTranslator のソース（MPL-2.0, https://github.com/MGuffin/xTranslator ）で確認済み。主な参照箇所は `TESVT_main.pas` の `runCommands`（バッチコマンド）、`TESVT_TranslatorApi.pas` の `TOpenAITranslator.Translate`（API ワイヤ形式）、`TESVT_commandProcessor.pas`（`~processor.tmp` の自動ロード）。
