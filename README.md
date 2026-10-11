# xtcli — xTranslator ヘッドレス CLI ラッパー

[xTranslator](https://www.nexusmods.com/skyrimspecialedition/mods/134)（Skyrim / Fallout / Starfield MOD 翻訳ツール）を CLI から自動実行するためのラッパーです。エージェント（LLM）による翻訳ワークフローを想定しています。

> **非公式ツールです。** xTranslator 作者（McGuffin）とは無関係で、xTranslator 本体は同梱していません。

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

## 呼び出し方法

このリポジトリのルート（`pyproject.toml` がある場所）から、Python モジュールとして実行します:

```console
python -m xtcli <サブコマンド> ...
```

`pip install -e .` を実行し Python の `Scripts` ディレクトリが PATH にあれば `xtcli <サブコマンド>` という短縮形も使えますが、**確実なのは `python -m xtcli` です。以降の例もこちらで統一します**。サブコマンドは `setup` / `extract` / `apply` / `run` / `doctor`。各コマンドの全オプションは `python -m xtcli <コマンド> --help` で確認できます。

**終了コード**: 成功 = `0`、バッチが完了しなかった（センチネル未受信でタイムアウト、または出力ファイルが生成されなかった）= `2`。エージェントはこれで成否を判定できます。

## セットアップ

```console
pip install -e .
python -m xtcli setup --exe "C:\Tools\xTranslator\xTranslator.exe" --game skyrimse
python -m xtcli doctor
```

`setup` は以下を行います（元ファイルは `*.xtcli-bak` として一度だけバックアップ。`python -m xtcli setup --restore` で復元）:

- `UserPrefs/commonApiPrefs.ini` — OpenAI API をローカルサーバーへ向ける
- `Misc/ApiTranslator.txt` — 1 リクエスト 1 文字列化・スロットリング解除
- `UserPrefs/commonPrefs.ini` — UI 言語を English に固定（自動化の安定用）、finalize 確認ダイアログを自動 Yes に

> フレッシュインストールの場合は一度 GUI を手動起動してください（初回のゲームパス設定ダイアログは自動化できません）。
> xTranslator は終了時に prefs を書き戻すため設定がずれることがありますが、xtcli は毎回実行前に自動で再適用します。

## 使い方（エージェント翻訳ワークフロー）

```console
# 1. 原文抽出（辞書があれば --sst で先に適用し、未翻訳分だけ抽出）
python -m xtcli extract MyMod.esp --from english --to japanese -o strings.json

# 2. strings.json の各 "trans" をエージェント（または人間）が埋める（下記スキーマ参照）

# 3. 適用と finalize（out\ に翻訳済みファイルが出力される）
python -m xtcli apply MyMod.esp -t strings.json -o out\ --missing-out missing.json
```

### strings.json のスキーマ（エージェント向け）

`extract` は次の JSON を出力します。**エージェントが編集してよいのは各要素の `trans` フィールドだけ**で、他のキー（`orig` を含む）は一切変更しないでください。`instructions` に翻訳時の注意が入っているので従ってください。`apply` は `orig` をキーに訳文を突き合わせるため、`orig` を書き換えると一致しません。

```json
{
  "tool": "xtcli 0.1.0",
  "source_lang": "english",
  "dest_lang": "japanese",
  "files": ["...\\MyMod.esp"],
  "instructions": "Fill in every empty 'trans' with the translation of 'orig'. Keep placeholder tokens exactly as they are ...",
  "strings": [
    { "orig": "Iron Sword", "trans": "", "kind": "name" },
    { "orig": "Emits light for <a_A> seconds.", "trans": "" },
    { "orig": "Hello, my love. Back from some adventure I bet.", "trans": "",
      "kind": "npc_line", "speaker": "female", "edid": "WenchHelloTopic" }
  ]
}
```

翻訳のルール:
- 空の `trans` をすべて埋める。`orig` はそのまま残す。
- `<a_A>`（エイリアス）、`999`（数値）、`<Alias=...>`、`%s` 系の書式、`<L_F>`（改行）、その他 `<...>` タグは**原文どおり保持**する（finalize 時に実際のゲーム変数へ復元される）。
- `orig` の改行の数を変えない。
- `kind` / `speaker` / `edid` は読み取り専用の文脈情報（下記参照）。**編集も削除もしない**こと。口調（話者の性別・立場に合う言い回し）の選択に使う。

### 台詞メタデータ（kind / speaker / edid）

`extract` はプラグイン（esp/esm/esl）を直接パースし（`xtcli/espmeta.py`）、各文字列に文脈フィールドを付与します。xTranslator の API ワイヤには生テキストしか流れないため、この情報はプラグインのレコードから独自に取得しています。

- `kind` — 文字列の種別: `npc_line`（**NPC が喋る**台詞 = INFO:NAM1）/ `player_line`（プレイヤーが喋る台詞・会話選択肢 = DIAL:FULL, INFO:RNAM）/ `book` / `name` / `system` / `other`
- `speaker` — `npc_line` のみ。話者の性別: `female` / `male` / `mixed`（男女どちらも話し得る）/ `unknown`（判定材料なし）
  - 判定材料: CTDA 条件 `GetIsSex`・`GetIsVoiceType`（VTYP の Female フラグ、FLST は展開）・`GetIsID`、INFO の `ANAM`（話者 NPC の ACBS Female フラグ）。エイリアス・派閥ベースの条件しか無い台詞は `unknown` になるため、MOD 側の知識（そのMODの話者が誰か）で補完すること。
- `edid` — 出所レコードの EditorID（トピック名など。文脈のヒント）

バニラマスター（Skyrim.esm 等）の音声タイプ解決には、xTranslator の prefs にあるゲーム Data フォルダを参照し、初回スキャン結果を `.espmeta-cache/` にキャッシュします（Data フォルダが見つからない場合は同梱の `xtcli/data/vanilla_voicetypes.json` にフォールバック）。ローカライズ済みプラグインと pex 由来の文字列にはメタデータは付きません。フィールドは追加のみの後方互換で、`apply` は `orig`/`trans` 以外を参照しません。

### その他

- `apply` は元ファイルを変更しません（プラグイン/MCM は同フォルダで処理し出力のみ `xtcli_out` 一時フォルダ経由で `-o` へ回収、pex はコピーして処理）。
- `--missing-out missing.json` を付けると、辞書でも埋まらず訳文も無かった文字列が同じスキーマで書き出されます。これを次の翻訳対象にできます。
- 複数ファイル / SST 辞書の併用可: `python -m xtcli extract a.esp b.esp --sst dic.sst`
- 言語（`--from` / `--to`）を省略すると `xtcli.toml` の既定（`source_lang` / `dest_lang`）が使われます。
- 生の BatchProcessor スクリプトの実行: `python -m xtcli run script.txt`

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
- 完了はセンチネル文字列の受信で検知します。万一検知できない場合は `idle_timeout` 秒（既定 300、`xtcli.toml`）の無通信でフォールバック終了します。大量ファイルで途中終了する場合は延長してください。

## 開発

```console
python -m pytest tests/
```

ソース調査メモ: プロトコルの根拠はすべて xTranslator のソース（MPL-2.0, https://github.com/MGuffin/xTranslator ）で確認済み。主な参照箇所は `TESVT_main.pas` の `runCommands`（バッチコマンド）、`TESVT_TranslatorApi.pas` の `TOpenAITranslator.Translate`（API ワイヤ形式）、`TESVT_commandProcessor.pas`（`~processor.tmp` の自動ロード）。

## ライセンス

[MIT License](LICENSE)。xTranslator 本体（MPL-2.0）のコードは含みません。
