# TV 録画 TS → MP4 自動エンコード

Windows / PowerShell 向け。Python の標準ライブラリーでキューを処理し、FFmpeg の **libx265** で H.265/HEVC、音声は AAC に変換します。H.264 用の libx264 プロファイルも選べます。x264.exe / x265.exe の別途導入は不要です。TMPGEnc のフィルターや品質値と完全に一致するものではなく、短い実写・アニメ・動きの多い場面を比較して設定を決めるための土台です。

現在の TMPGEnc 設定は「適応補間でインターレース解除、MP4 / HEVC、YUV 4:2:0・8bit/ch、VBR（固定品質）50%、フレームレートは入力に合わせる、音声48kHz / 128～192kbps」。これに合わせ、初期値は HEVC / yuv420p（YUV 4:2:0・8bit/ch）/ CRF 22 / medium、入力フレームレートを倍化しない bwdif、AAC 48kHz / 192kbps（2ch以下）としています。サラウンドは別途384kbpsにしています。

**品質50%から CRF への換算式はありません。CRF 22 は同等画質を保証する値ではなく、比較用の暫定値です。** TMPGEnc の「適応補間」と bwdif も同一アルゴリズムとは扱いません。実写・アニメ混在のため tune は指定せず、24p 化は自動実行しません。

## 用意するもの

- Python 3.10 以降（以下の `py -3` が動作すること。Python 実行ファイルのフルパスでも可）
- `ffmpeg.exe` と `ffprobe.exe`。FFmpeg 5.1 以降を想定し、libx265（H.264を使う場合はlibx264）、AAC、bwdif が使えるビルドが必要です。
- FFmpeg で映像・音声をデコードできる TS。暗号化解除の機能はありません。

[FFmpeg 公式ダウンロード案内](https://ffmpeg.org/download.html) に Windows ビルドのリンクがあります。2 本の exe が入った bin フォルダーを PATH に加えるか、毎回 `--ffmpeg` / `--ffprobe` でフルパスを渡します。スクリプトが依存ソフトを自動インストールすることはありません。

```powershell
py -3 --version
ffmpeg -version
ffprobe -version
ffmpeg -hide_banner -h encoder=libx265
```

Chocolatey でインストールした場合、インストール前から開いているアプリには PATH の変更が反映されないことがあります。PowerShell を開き直すか、次のようにフルパスを指定します（Chocolatey の標準配置の場合）。

```powershell
py -3 .\encode.py inspect 'D:\Recordings\番組.ts' --ffprobe 'C:\ProgramData\chocolatey\bin\ffprobe.exe'
py -3 .\encode.py encode 'D:\Recordings\番組.ts' --start 300 --duration 60 --output-dir '.\samples\first' --ffmpeg 'C:\ProgramData\chocolatey\bin\ffmpeg.exe' --ffprobe 'C:\ProgramData\chocolatey\bin\ffprobe.exe' --verify-full
```

## ワークフロー

1. 録画完了後、TS を入力フォルダーへ移動する。録画ソフトの「録画後コマンド」から呼ぶ方法もあります。
2. 初回は `inspect` でサービス、映像、音声を確認する。
3. 同じ箇所を短く変換し、CRF とインターレース処理を比較する。
4. 設定を決めたら一括処理する。
5. MP4 の再生時間・先頭・中盤・末尾・音ズレ・音声切替を確認する。
6. 必要なら Windows タスクスケジューラから定期実行する。

**元の TS は常に保持します。** MP4 の確認後の削除・保管は手動で行ってください。

## まず 1 本を調べる

以下はこのフォルダーをカレントディレクトリーにした PowerShell の例です。`D:\Recordings` などは自分のパスに置き換えます。

```powershell
py -3 .\encode.py inspect 'D:\Recordings\番組.ts'
```

JSON の `programs[].program_id`（サービス ID）、`tags.service_name`、`streams[].index`、`codec_type`、`width` / `height`、`sample_aspect_ratio`、`field_order`、`avg_frame_rate`、音声の `channels` / `tags.language` を確認します。フレームレートや field_order は手がかりであり、番組全体の映像方式を保証するものではありません。

映像を含むサービスが複数ある TS は、誤選択を避けるため処理を止めます。目的のサービスを `--program 101` のように明示してください。映像と音声は同じサービスから選択し、映像が複数ある場合も停止します。

## 60 秒のサンプルで決める

```powershell
py -3 .\encode.py encode 'D:\Recordings\番組.ts' --profile hevc-balanced --start 300 --duration 60 --crf 20 --output-dir '.\samples\hevc-crf20'
py -3 .\encode.py encode 'D:\Recordings\番組.ts' --profile hevc-balanced --start 300 --duration 60 --crf 22 --output-dir '.\samples\hevc-crf22'
py -3 .\encode.py encode 'D:\Recordings\番組.ts' --profile hevc-balanced --start 300 --duration 60 --crf 24 --output-dir '.\samples\hevc-crf24'
```

必要ならそれぞれ `--program 101` を付けます。`--start` / `--duration` の単位は秒です。比較サンプルは別の出力先に保存し、本番の一括処理にはこの 2 つのオプションを付けません。シークは正確さを優先して入力後に指定しているため、開始位置までの読み飛ばしに時間がかかります。

確認する場面は、暗部、細かい模様、字幕テロップ、パン、スポーツなどの速い動きです。TMPGEnc の出力とも同じ場面で比較してください。

## 設定ファイル

`profiles.json` を編集します。独自のプロファイルを複製して名前を付け、`--profile 名前` で選べます。別ファイルは `--config パス` で指定できます。

| プロファイル | CRF / preset | インターレース処理 | 用途の出発点 |
|---|---|---|---|
| `hevc-balanced`（初期値） | 22 / medium | bwdif、1 入力フレーム → 1 フレーム | HEVC、現在の入力fps維持に合わせる |
| `hevc-bob` | 22 / medium | bwdif、1 フィールド → 1 フレーム | HEVC、59.94p等で動きを優先する比較用 |
| `hevc-progressive` | 22 / medium | 解除しない | HEVC、プログレッシブと確認できた素材 |
| `tv-balanced` | 20 / slow | bwdif、1 フィールド → 1 フレーム | H.264、動きを優先 |
| `tv-small` | 23 / slow | bwdif、1 入力フレーム → 1 フレーム | H.264、容量優先 |
| `progressive` | 20 / slow | 解除しない | H.264、プログレッシブ素材 |

これらは推奨値を決めるための暫定値です。CRF は目標容量ではなく品質基準で、番組ごとに容量が変わります。固定容量を狙う 2-pass ABR は未実装です。

| キー | 変更の目安 |
|---|---|
| `codec` | `libx265` = HEVC、`libx264` = H.264。省略時は旧設定互換のため libx264 |
| `pix_fmt` | HEVCは `yuv420p`（8bit）または `yuv420p10le`（10bit）。省略時8bit。H.264はこのスクリプトでは8bitのみ |
| `crf` | 小さいほど高画質・大容量の傾向。HEVCはまず 20 / 22 / 24 を比較。`--crf` で一時上書き可能。x264とx265の同じCRF値は同等画質を意味しない |
| `preset` | 速度と圧縮効率の調整。HEVCはmediumから開始し、余裕があればslowを比較。CRFを固定しても容量・画質は完全には一致しない |
| `tune` | 混在素材は `null`。必要なら `animation` / `grain` を比較。`film` はlibx264のみで使用可 |
| `deinterlace` | `bob` / `frame` / `off` |
| `parity` | `auto` が基本。フィールド順を確認できた場合に `tff` / `bff` |
| `deint_frames` | 初期値 `interlaced` はインターレースとマークされたフレームのみ。マークが誤っている素材は調査後に `all` |
| `audio` | `all` はサービス内の全音声、`first` は最初の音声のみ |
| `audio_sample_rate` | `48000`。省略時も48kHz。出力検査でも確認 |
| `audio_bitrate` | 2ch 以下の音声 1 トラックあたり。初期値 `192k` |
| `surround_bitrate` | 3ch 以上の音声 1 トラックあたり。初期値 `384k` |
| `x264_params` | 初期値は空。例 `aq-mode=3:deblock=-1,-1`。高度な設定が必要な場合のみ |
| `x265_params` | 初期値は空。HEVC用の詳細設定。libx264では空にする。libx265ではx264_paramsを空にする |

映像は選択したコーデックとピクセル形式で、元の解像度とピクセルアスペクト比を維持します。HEVC の MP4 は hvc1 タグで出力します。再生機器でのHEVC・10bit対応は実機で確認してください。1440×1080 / SAR 4:3 の映像を 1920×1080 に拡大する処理は加えていません。AAC は48kHzに揃え、入力のチャンネル数を基本的に維持し、ステレオへ強制変換しません。言語・タイトルのタグが取得できれば引き継ぎます。

**29.97 フレーム/秒のインターレース素材は、bob で約 59.94p、frame で約 29.97p になります。** 初期設定は interlaced フラグを使うため、混在素材では可変フレームレートになることがあります。固定 30fps / 60fps への丸めはしません。

アニメや映画の 24p テレシネに対して、bwdif は元の 24p を復元する処理ではありません。単に 23.976fps に設定すると、コマ落ちや残像の原因になります。24p 化、フィールドマッチ、混在周期の処理は素材を確認して別途設計します。

音声のステレオ／5.1ch が番組途中で切り替わる TS、二重音声 AAC（dual mono）、ARIB 字幕・データ放送、CM カットは個別対応が必要です。このスクリプトでは二重音声の言語分離や字幕変換・CM 自動検出は行いません。字幕・データ・チャプター・番組メタデータは MP4 に入れません。二重音声は短い試聴で確認してください。

## 本番の一括処理

まずコマンドを確認します。dry-run でも ffprobe で入力を読みますが、出力フォルダー・ログ・MP4 は作りません。

```powershell
py -3 .\encode.py encode 'D:\Recordings\Completed' --output-dir 'D:\Encoded' --recursive --dry-run
py -3 .\encode.py encode 'D:\Recordings\Completed' --output-dir 'D:\Encoded' --recursive --verify-full
```

- 対象は `.ts` / `.m2ts` / `.mts`（大文字小文字は不問）。1 本ずつ処理します。
- 出力名は `番組.ts.mp4`。サブフォルダー構造を保持し、拡張子違いの入力の衝突を避けます。
- ディレクトリー処理では、更新から 300 秒未満の入力を `PENDING` として後回しにします。`--min-age` で変更できます。これは録画完了の保証ではないため、**録画完了専用フォルダーへの移動を推奨**します。単一ファイル指定では待機判定を行いません。
- 同じ出力ルートへの同時実行は OS ロックで拒否します。`.encode.lock` ファイルは残りますが、プロセス終了時にロックは自動解放されます。
- 完成済み MP4 は、元ファイルのパス・サイズ・更新時刻と設定が記録に一致し、再検査が通る場合だけ `SKIP` します。内容のハッシュ照合は行いません。設定を変える場合は別の出力先を指定します。旧H.264版から更新した場合も、記録の設定項目が増えたため別の出力先を使ってください。
- 1 本が失敗しても残りを続行し、1 件でも失敗すると終了コード 1、全件成功／スキップ／待機なら 0 を返します。中断は 130 です。
- 各エンコードの出力先に固有名の `.log` と、完成時に `.mp4.json`（入力情報・設定・コマンド・処理時間・出力情報）を保存します。

一時 MP4 に変換 → FFmpeg の終了コード確認 → ffprobe で映像・音声・再生時間を検査 → 必要なら全体をデコード検査 → 入力の更新有無を検査 → 完成名に変更、という順序です。失敗時は今回の一時 MP4 を削除し、ログと TS を残します。強制終了・停電で残った `.partial.mp4` は完成品として扱いません。

`--verify-full` は生成した MP4 の全映像・音声をデコードして検査するため、追加の処理時間が必要です。未指定時は構造と時間の検査のみです。再生時間は取得できた入力映像長（取得不可なら単一サービスのファイル長）と比較し、3 秒または 1% を超える差を拒否します。元 TS の時間情報が不正な場合は、ログを調べて個別対応してください。これらの検査は音ズレや主観画質の保証ではありません。

FFmpeg の `-xerror` を使い、エラーを検出したときは処理を失敗させます。TS のドロップを無条件に無視するオプションや、自動的な音声時刻補正は加えていません。

## 定期実行

`Run-Queue.ps1` は一度キューを処理して終了します。Windows タスクスケジューラで 10 分おきなどに起動すれば、待機ファイル・失敗ファイルは次回再処理し、成功済みはスキップします。最初は手動で動作確認してください。

```powershell
.\Run-Queue.ps1 -InputDirectory 'D:\Recordings\Completed' -OutputDirectory 'D:\Encoded' -DryRun
.\Run-Queue.ps1 -InputDirectory 'D:\Recordings\Completed' -OutputDirectory 'D:\Encoded' -VerifyFull
```

スケジューラの設定例（実際の Python / FFmpeg の配置に置き換える）：

- プログラム：`powershell.exe`
- 引数：`-NoProfile -File "C:\home\src\movie_encoder\Run-Queue.ps1" -InputDirectory "D:\Recordings\Completed" -OutputDirectory "D:\Encoded" -Python "C:\Python313\python.exe" -Ffmpeg "C:\Tools\ffmpeg\bin\ffmpeg.exe" -Ffprobe "C:\Tools\ffmpeg\bin\ffprobe.exe" -VerifyFull`
- 開始フォルダー：`C:\home\src\movie_encoder`
- トリガー：10 分間隔など。設定の「タスクが既に実行中の場合」は「新しいインスタンスを開始しない」。

入力フォルダーには録画完了ファイルだけを置きます。サービス ID が異なる録画が混在する場合は録画側で目的サービスを抽出するか、サービス別にキューを分け、`-ProgramId` と別の出力フォルダーを設定します。実行ポリシーで `.ps1` が制限される環境では、その環境の方針に従うか、Python に `encode.py encode ...` を直接渡してください。タスクの登録はこのリポジトリーでは自動実行しません。

## 検証

```powershell
py -3 -m unittest discover -s tests -v
```

テストでは外部プロセスをモックし、サービス選択、音声マッピング、上書き防止、入力更新・変換失敗・検査失敗時の扱い、再開、サンプル時間、一括処理を検証します。実際の FFmpeg と放送 TS での画質・同期検証は別途必要です。

実際の FFmpeg を使う動作確認もできます。6秒の人工インターレース MPEG-2 TS（720×480、約29.97fps、SAR 8:9、音声2トラック）を生成し、HEVCのframe/bob、H.264のbobを変換します。全体デコード、フレームレート、ピクセル形式、アスペクト比、音声・言語タグ、完成品の再実行時スキップを検査します。生成物と `report.json` は `samples/smoke-*` に保存します。

```powershell
py -3 .\tests\smoke_ffmpeg.py --ffmpeg 'C:\ProgramData\chocolatey\bin\ffmpeg.exe' --ffprobe 'C:\ProgramData\chocolatey\bin\ffprobe.exe'
```

2026-10-16、Chocolatey の FFmpeg / ffprobe 9.0.2（gyan.dev essentials build）で上記の動作確認を実行し、全3プロファイルで成功しました。HEVC初期設定は約29.97p、bobは約59.94p、全出力でYUV 4:2:0・8bitとAAC 48kHz・2音声を確認しています。実際の放送TSのドロップ・方式混在・途中の音声構成変更や主観画質・同期は、この人工素材での検査には含みません。

## 公式仕様

- [FFmpeg の入力・出力とストリーム選択](https://ffmpeg.org/ffmpeg.html)
- [libx264 の設定と x264-params](https://ffmpeg.org/ffmpeg-codecs.html#libx264_002c-libx264rgb)
- [libx265 の設定と x265-params](https://ffmpeg.org/ffmpeg-codecs.html#libx265)
- [x265 のCRF・品質指定](https://x265.readthedocs.io/en/master/cli.html#quality-rate-control-and-rate-distortion-options)
- [bwdif の mode / parity / deint](https://ffmpeg.org/ffmpeg-filters.html#bwdif)
- [ffprobe の JSON 出力](https://ffmpeg.org/ffprobe.html)
