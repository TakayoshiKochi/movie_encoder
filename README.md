# TV 録画 TS → MP4 自動エンコード

Windows / PowerShell 向け。Python の標準ライブラリーでキューを処理し、FFmpeg の **hevc_nvenc** または **libx265** で H.265/HEVC、音声は AAC に変換します。H.264 用の libx264 プロファイルも選べます。既定は NVENC CQ 24 です。x264.exe / x265.exe の別途導入は不要です。TMPGEnc のフィルターや品質値と完全に一致するものではなく、短い実写・アニメ・動きの多い場面を比較して設定を決めるための土台です。

現在の TMPGEnc 設定は「適応補間でインターレース解除、MP4 / HEVC、YUV 4:2:0・8bit/ch、VBR（固定品質）50%、フレームレートは入力に合わせる、音声48kHz / 128～192kbps」。これに合わせたサンプル比較後、既定として NVENC / HEVC / yuv420p（YUV 4:2:0・8bit/ch）/ CQ 24 / p6 / hq を採用しました。入力フレームレートを倍化しない bwdif、AAC 48kHz / 192kbps（2ch以下）とし、サラウンドは別途384kbpsにしています。

**品質50%から CRF への換算式はありません。CRF 22 は TMPGEnc と同等画質を保証する値ではありませんが、サンプル比較後の採用値です。** TMPGEnc の「適応補間」と bwdif も同一アルゴリズムとは扱いません。実写・アニメ混在のため x265 の tune は指定せず、24p 化は自動実行しません。

## 用意するもの

- Python 3.10 以降（以下の `py -3` が動作すること。Python 実行ファイルのフルパスでも可）
- `ffmpeg.exe` と `ffprobe.exe`。既定プロファイルでは対応するNVIDIA GPU・ドライバーとhevc_nvencが必要です。x265プロファイルではlibx265、H.264ではlibx264が必要です。共通してAACとbwdifも使えるビルドを使用します（FFmpeg 5.1以降を想定、実機検証は9.0.2）。
- FFmpeg で映像・音声をデコードできる TS。暗号化解除の機能はありません。

[FFmpeg 公式ダウンロード案内](https://ffmpeg.org/download.html) に Windows ビルドのリンクがあります。2 本の exe が入った bin フォルダーを PATH に加えるか、毎回 `--ffmpeg` / `--ffprobe` でフルパスを渡します。スクリプトが依存ソフトを自動インストールすることはありません。

```powershell
py -3 --version
ffmpeg -version
ffprobe -version
ffmpeg -hide_banner -h encoder=libx265
ffmpeg -hide_banner -h encoder=hevc_nvenc
```

プロファイルと品質の指定を省略すると、Python・定期実行用PowerShellとも `hevc-nvenc` / CQ 24 を使います。

```powershell
py -3 .\encode.py encode 'D:\Recordings\番組.ts' --output-dir 'D:\Encoded-NVENC' --verify-full
```

従来のコマンドで `--profile hevc-balanced` やPowerShellの `-Profile hevc-balanced` を明示していれば、引き続きx265になります。NVENCの既定値へ移行する場合はその指定と `--crf 22` を外します。CQは必要な場合だけ `--cq` で上書きできます。既存出力と設定が異なる場合は上書きしないため、移行時は別の出力先を使ってください。

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

## x265 と NVENC を比較する

対応する NVIDIA GPU とドライバー、FFmpegの `hevc_nvenc` が必要です。`ffmpeg -h encoder=hevc_nvenc` はビルドの対応確認で、GPUでの実行可否まで保証するものではありません。非対応環境では変換が失敗してログを残し、x265へ自動切替はしません。

`hevc-nvenc` は、x265用の `hevc-balanced` と同じインターレース解除・解像度・SAR・入力fps・YUV 4:2:0 8bit・音声設定で、映像エンコーダーをNVENCにします。入力のデコードとbwdifはCPUのままにし、GPUデコード・別のGPUフィルターによる見た目の差を持ち込みません。

```powershell
py -3 .\encode.py encode 'D:\Recordings\番組.ts' --profile hevc-balanced --crf 22 --start 300 --duration 60 --output-dir '.\samples\compare-x265' --verify-full
py -3 .\encode.py encode 'D:\Recordings\番組.ts' --profile hevc-nvenc --cq 22 --start 300 --duration 60 --output-dir '.\samples\compare-nvenc22' --verify-full
py -3 .\encode.py encode 'D:\Recordings\番組.ts' --profile hevc-nvenc --cq 26 --start 300 --duration 60 --output-dir '.\samples\compare-nvenc26' --verify-full
py -3 .\encode.py encode 'D:\Recordings\番組.ts' --profile hevc-nvenc --cq 30 --start 300 --duration 60 --output-dir '.\samples\compare-nvenc30' --verify-full
```

必要に応じて全コマンドへ同じ `--program` と、前述のFFmpeg/ffprobeフルパスを追加してください。破損箇所を含める場合は同じ `--allow-input-errors` を付けます。

**CRF 22 と CQ 22 は同等画質を意味しません。** 上のCQ値は比較を始めるための候補です。見た目を揃えて容量・時間を比べるか、CQを調整して容量を近づけたうえで画質を比べます。CQが小さいほど品質を高める方向ですが、TMPGEncの品質%との換算はありません。CQ=0は自動設定になるため、このスクリプトでは指定を拒否します。NVENCに `--crf`、x265/x264に `--cq` を指定した場合もエラーにします。

NVENCの初期値は `p6 / hq / VBR-CQ`、目標平均ビットレートを指定しない `-b:v 0`、先読み32フレーム、spatial/temporal AQ有効、Bフレーム3枚、`multipass=fullres` です。これは画質と圧縮効率を考慮した比較用設定であり、TMPGEncのNVENC設定を再現するものではありません。NVENCのmultipassは内部のフレーム解析方式で、動画全体を2回処理するx265の2-pass ABRとは異なります。古いGPUやドライバーでこれらの機能が使えない場合はログを確認し、専用プロファイルを複製して `nvenc` の設定を調整してください。

出力 `.mp4.json` の `encode_seconds` は、FFmpegによる入力デコード・シーク・フィルター・映像/音声エンコード・MP4出力までの時間で、後段の出力検査を含みません。`elapsed_seconds` は後段の検査を含む合計です。容量はMP4のファイルサイズで比較できます。先頭までの読み飛ばしや短い素材の起動コストも時間に含まれるため、エンコーダー単体の速度とは区別し、長めの同じ区間で比較してください。

## 設定ファイル

`profiles.json` を編集します。独自のプロファイルを複製して名前を付け、`--profile 名前` で選べます。別ファイルは `--config パス` で指定できます。

| プロファイル | 品質 / preset | インターレース処理 | 用途の出発点 |
|---|---|---|---|
| `hevc-balanced` | CRF 22 / medium | bwdif、1 入力フレーム → 1 フレーム | HEVC、x265比較・CPUで処理したい場合 |
| `hevc-bob` | 22 / medium | bwdif、1 フィールド → 1 フレーム | HEVC、59.94p等で動きを優先する比較用 |
| `hevc-progressive` | 22 / medium | 解除しない | HEVC、プログレッシブと確認できた素材 |
| `hevc-nvenc`（初期値） | CQ 24 / p6 | bwdif、1 入力フレーム → 1 フレーム | HEVC、NVENC採用設定 |
| `tv-balanced` | 20 / slow | bwdif、1 フィールド → 1 フレーム | H.264、動きを優先 |
| `tv-small` | 23 / slow | bwdif、1 入力フレーム → 1 フレーム | H.264、容量優先 |
| `progressive` | 20 / slow | 解除しない | H.264、プログレッシブ素材 |

これらは推奨値を決めるための暫定値です。CRF は目標容量ではなく品質基準で、番組ごとに容量が変わります。固定容量を狙う 2-pass ABR は未実装です。

| キー | 変更の目安 |
|---|---|
| `codec` | `libx265` / `hevc_nvenc` = HEVC、`libx264` = H.264。省略時は旧設定互換のため libx264 |
| `pix_fmt` | libx265は `yuv420p`（8bit）または `yuv420p10le`（10bit）。省略時8bit。NVENC/H.264はこのスクリプトでは8bitのみ |
| `crf` | 小さいほど高画質・大容量の傾向。HEVCはまず 20 / 22 / 24 を比較。`--crf` で一時上書き可能。x264とx265の同じCRF値は同等画質を意味しない |
| `preset` | 速度と圧縮効率の調整。HEVCはmediumから開始し、余裕があればslowを比較。CRFを固定しても容量・画質は完全には一致しない |
| `cq` | NVENC専用。0より大きく51以下。`--cq`で上書き。NVENCプロファイルにはcrfを入れない |
| `nvenc` | NVENC専用の設定オブジェクト。`lookahead`（0～32）、`spatial_aq` / `temporal_aq`（真偽値）、`multipass`（disabled/qres/fullres）、`bframes`（0～4）。presetはp1～p7 |
| `tune` | x265の混在素材は `null`。必要なら `animation` / `grain` を比較。`film` はlibx264のみで使用可。NVENCはhq/ll/ullから選ぶ |
| `deinterlace` | `bob` / `frame` / `off` |
| `parity` | `auto` が基本。フィールド順を確認できた場合に `tff` / `bff` |
| `deint_frames` | 初期値 `interlaced` はインターレースとマークされたフレームのみ。マークが誤っている素材は調査後に `all` |
| `audio` | `all` はサービス内の全音声、`first` は最初の音声のみ |
| `audio_sample_rate` | `48000`。省略時も48kHz。出力検査でも確認 |
| `audio_bitrate` | 2ch 以下の音声 1 トラックあたり。初期値 `192k` |
| `surround_bitrate` | 3ch 以上の音声 1 トラックあたり。初期値 `384k` |
| `x264_params` | 初期値は空。例 `aq-mode=3:deblock=-1,-1`。高度な設定が必要な場合のみ |
| `x265_params` | 初期値は空。libx265専用の詳細設定。NVENCではx264_paramsとx265_paramsを両方空にする |

映像は選択したコーデックとピクセル形式で、`--resize` を省略した場合は元の解像度とピクセルアスペクト比を維持します。HEVC の MP4 は hvc1 タグで出力します。再生機器でのHEVC・10bit対応は実機で確認してください。リサイズ未指定時は1440×1080 / SAR 4:3 の映像を1920×1080へ拡大する処理も加えていません。AAC は48kHzに揃え、入力のチャンネル数を基本的に維持し、ステレオへ強制変換しません。言語・タイトルのタグが取得できれば引き継ぎます。

**29.97 フレーム/秒のインターレース素材は、bob で約 59.94p、frame で約 29.97p になります。** 初期設定は interlaced フラグを使うため、混在素材では可変フレームレートになることがあります。固定 30fps / 60fps への丸めはしません。

アニメや映画の 24p テレシネに対して、bwdif は元の 24p を復元する処理ではありません。単に 23.976fps に設定すると、コマ落ちや残像の原因になります。24p 化、フィールドマッチ、混在周期の処理は素材を確認して別途設計します。

音声のステレオ／5.1ch が番組途中で切り替わる TS、二重音声 AAC（dual mono）、ARIB 字幕・データ放送、CM カットは個別対応が必要です。このスクリプトでは二重音声の言語分離や字幕変換・CM 自動検出は行いません。字幕・データ・チャプター・番組メタデータは MP4 に入れません。二重音声は短い試聴で確認してください。

## 縦横比を保ってリサイズする

```powershell
py -3 .\encode.py encode 'D:\Recordings\番組.ts' --resize 1280x720 --output-dir 'D:\Encoded-720p' --verify-full
```

`--resize 幅x高さ` は、黒帯を含めた完成サイズを指定します。デインターレース → 表示アスペクト比を保って枠内へ縮小・拡大（Lanczos）→ 正方形ピクセル（SAR 1:1）に揃える → 余白を黒で埋める、の順で処理します。映像を切り取らず、入力SARも考慮します。プログレッシブ用プロファイルではデインターレースを省いてから同じリサイズを行います。

| 入力映像 | `--resize 1280x720` の結果 |
|---|---|
| 1440×1080・SAR 4:3（表示16:9） | 映像1280×720、黒帯なし |
| 4:3映像 | 映像960×720、左右に各160pxの黒帯 |
| 1920×960・SAR 1:1（表示2:1） | 映像1280×640、上下に各40pxの黒帯 |
| 480×720・SAR 1:1の縦長映像 | 映像480×720、左右に各400pxの黒帯 |

出力は指定サイズそのものになり、幅・高さは2以上の偶数で指定します。YUV 4:2:0用に映像領域と余白位置を偶数へ丸めるため、比率や左右・上下の余白に最大数ピクセル相当の丸め差が出る場合があります。入力映像に既に焼き込まれている黒帯は自動除去せず、映像の一部として扱います。

省略時は従来どおり元サイズ・SARを維持します。指定サイズは `.mp4.json` の `request.resize` に記録し、完成時と再開時に出力サイズとSAR 1:1も検査します。サイズを変更して再変換する場合は、既存MP4を上書きしないよう別の出力フォルダーを指定してください。

`--jobs 2` や `--allow-input-errors` とも併用できます。定期実行用では `-Resize 1280x720` を使います。

```powershell
.\Run-Queue.ps1 -InputDirectory 'D:\Recordings\Completed' -OutputDirectory 'D:\Encoded-720p' -Jobs 2 -Resize 1280x720 -VerifyFull
```

## 本番の一括処理

まずコマンドを確認します。dry-run でも ffprobe で入力を読みますが、出力フォルダー・ログ・MP4 は作りません。

```powershell
py -3 .\encode.py encode 'D:\Recordings\Completed' --output-dir 'D:\Encoded' --recursive --dry-run
py -3 .\encode.py encode 'D:\Recordings\Completed' --output-dir 'D:\Encoded' --recursive --verify-full
```

- 対象は `.ts` / `.m2ts` / `.mts`（大文字小文字は不問）。既定は1本ずつ、`--jobs 2` で最大2本ずつ処理します。
- 各ファイルの処理開始時に `INPUT [3/10] パス` の形式で、開始番号と最初に列挙した総ファイル数を表示します。並列実行でも番号は開始順です。総数にはスキップ・待機・失敗になるファイルも含み、この番号はエンコード完了件数ではありません。
- `BATCH START` にバッチ開始時刻、`INPUT` に各ファイルの開始時刻を表示します。各ファイルの処理終了時は `PROGRESS [完了件数/総数]` と経過時間・全体の終了予定時刻（`ETA`）を更新し、最後に実際の `BATCH END` 時刻を表示します。時刻にはPCのローカルタイムゾーンとUTCオフセットを含めます。
- 対応する端末では表示を自動で色分けします。バッチ開始・終了は紫、入力は青、進捗はシアン、成功は緑、警告・待機は黄、失敗は赤、スキップとFFmpegコマンド行は控えめなグレーです。Windowsでは端末のANSI対応を確認して有効にし、非対応なら無色に戻します。
- 標準出力・標準エラーをそれぞれ判定し、ファイルへのリダイレクトやパイプでは色の制御文字を付けません。`inspect` のJSON、最後の件数JSON、各ファイルの `.log` / `.mp4.json` は色付けしません。端末でも無色にしたい場合は、PowerShellで `$env:NO_COLOR = '1'` を設定します（解除は `Remove-Item Env:NO_COLOR`）。`TERM=dumb` でも無色になります。
- 終了予定は「開始からの経過時間 × 総数 ÷ 処理済み件数」で外挿します。処理済み0件では `ETA=calculating` です。並列処理の実際の処理ペースと出力検査時間も含まれます。処理済み件数にはスキップ・待機・失敗も含むため、それらが多い場合やファイルの長さが異なる場合、予想は大きく変わり得ます。dry-runの予想は入力確認処理の終了予定であり、本番エンコードの所要時間ではありません。
- 出力名は `番組.ts.mp4`。サブフォルダー構造を保持し、拡張子違いの入力の衝突を避けます。
- ディレクトリー処理では、更新から 300 秒未満の入力を `PENDING` として後回しにします。`--min-age` で変更できます。これは録画完了の保証ではないため、**録画完了専用フォルダーへの移動を推奨**します。単一ファイル指定では待機判定を行いません。
- 同じ出力ルートへの別バッチの同時実行は OS ロックで拒否します。1つのバッチ内の `--jobs 2` は同じ出力先を共有できます。`.encode.lock` ファイルは残りますが、処理終了時にロックは自動解放されます。
- 完成済み MP4 は、元ファイルのパス・サイズ・更新時刻と設定が記録に一致し、再検査が通る場合だけ `SKIP` します。内容のハッシュ照合は行いません。設定を変える場合は別の出力先を指定します。旧H.264版から更新した場合も、記録の設定項目が増えたため別の出力先を使ってください。
- 1 本が失敗しても残りを続行し、1 件でも失敗すると終了コード 1、全件成功／スキップ／待機なら 0 を返します。中断は 130 です。
- 各エンコードの出力先に固有名の `.log` と、完成時に `.mp4.json`（入力情報・設定・コマンド・処理時間・出力情報）を保存します。

一時 MP4 に変換 → FFmpeg の終了コード確認 → ffprobe で映像・音声・再生時間を検査 → 必要なら全体をデコード検査 → 入力の更新有無を検査 → 完成名に変更、という順序です。失敗時は今回の一時 MP4 を削除し、ログと TS を残します。強制終了・停電で残った `.partial.mp4` は完成品として扱いません。

`--verify-full` は生成した MP4 の全映像・音声をデコードして検査するため、追加の処理時間が必要です。未指定時は構造と時間の検査のみです。再生時間は取得できた入力映像長（取得不可なら単一サービスのファイル長）と比較し、3 秒または 1% を超える差を拒否します。元 TS の時間情報が不正な場合は、ログを調べて個別対応してください。これらの検査は音ズレや主観画質の保証ではありません。

既定では、変換時に FFmpeg の `-xerror` を使い、エラーを検出したときは処理を失敗させます。`--verify-full` の有無に関係なく入力TSのデコードエラーで停止します。ログには `STAGE: encode input`、`STAGE: probe and validate output`、`STAGE: verify-full (decode output MP4)` を記録し、失敗時も段階を表示します。

### 処理中の自動スリープを防ぐ（Windows）

```powershell
py -3 .\encode.py encode 'D:\Recordings\Completed' --recursive --output-dir 'D:\Encoded' --jobs 2 --verify-full --keep-awake
.\Run-Queue.ps1 -InputDirectory 'D:\Recordings\Completed' -OutputDirectory 'D:\Encoded' -Jobs 2 -VerifyFull -KeepAwake
```

`--keep-awake`（PowerShellでは `-KeepAwake`）を付けると、バッチの入力解析・エンコード・出力検査中はWindowsの自動スリープを抑止します。並列処理にも対応し、完了・エラー・Ctrl+Cでの中断時に元の状態へ戻します。電源プランの設定変更や追加パッケージ、管理者権限は不要です。画面の消灯は許可します。指定を省略した場合は従来どおりです。

[WindowsのSetThreadExecutionState API](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate) を使用します。手動でのスリープや、ノートPCの蓋を閉じる操作によるスリープは防ぎません。抑止に失敗した場合はエラーとして処理開始前に停止します。`inspect`・`--dry-run`・入力0件の場合は抑止しません。Windows以外で実処理に指定するとエラーになります。

### 2並列で処理する

```powershell
py -3 .\encode.py encode 'D:\Recordings\Completed' --output-dir 'D:\Encoded' --recursive --jobs 2 --verify-full
```

`--jobs` は同時処理するファイル数の上限です。1以上の整数を指定でき、省略時は1です。2本を開始し、1本の処理が完了すると次のファイルを開始します。単一ファイルの指定では、その動画を分割せず1本だけ処理します。

並列数は入力解析・変換・出力検査を含めたファイル単位の上限です。あるファイルを `--verify-full` で検査している間、もう一方では変換を進められます。各ファイルのログ・一時MP4・完成記録は個別に保存し、最後に成功・スキップ・待機・失敗の件数を集計します。1本が失敗しても残りを処理し、失敗があれば終了コード1を返します。

Ctrl+Cでは新しいファイルの開始を止め、このバッチのFFmpeg/ffprobe子プロセスを終了させ、今回処理中だった一時MP4を削除してから出力先のロックを解放します。既に完成したMP4と元TSは保持します。タスクの強制終了や停電では一時ファイルが残る場合があります。再実行で完成済みファイルはスキップでき、`--jobs 2` から `--jobs 1` への変更もエンコード設定の変更とは扱いません。

既定のNVENC CQ24に加え、x265/x264プロファイルでも使用できます。NVENCエンジンへの割り当てはドライバーが行います。CPUの入力デコード・bwdif・音声変換・出力検査やディスクI/Oも同時に増えるため、並列数に比例して速度が上がるとは限りません。

### TS の破損を補完して続行したい場合

ログに `mpeg2video` の `ac-tex damaged` / `corrupt decoded frame` が出ている場合、入力の MPEG-2 映像の破損が疑われます。受信ドロップやファイル破損などが考えられますが、ログだけで原因までは断定できません。HEVCに変換済みの出力MP4の検査であれば、デコーダーは `mpeg2video` にはなりません。

破損箇所を含む出力を許容して変換を試す場合だけ `--allow-input-errors` を指定します。

```powershell
py -3 .\encode.py encode 'D:\Recordings\番組.ts' --output-dir 'D:\Encoded-Recovered' --allow-input-errors --verify-full
```

このオプションは**入力を変換するプロセスの `-xerror` のみ外し、FFmpegの通常のエラー補完・続行を許可**します。破損パケットを強制的に捨てる `discardcorrupt` や、追加の `ignore_err`、自動音声時刻補正は指定しません。FFmpeg が致命的エラーで終了した場合、出力の再生時間・構造検査に失敗した場合、入力が変化した場合は、引き続き完成品にしません。

**`--verify-full` はこの場合も `-xerror` で厳格に出力MP4を検査します。** ただし、入力の欠損がブロックノイズやフリーズとして正常なHEVC映像に変換されれば、デコード検査は通り得ます。補完は失われた映像を復元するものではありません。ログの破損箇所の前後で画質・音ズレを確認してください。

オプションの使用は警告表示と `.mp4.json` の `request.allow_input_errors` に記録します。厳格モードで作った出力と区別するため、設定が違う既存出力は上書き・スキップしません。定期実行用スクリプトでは `-AllowInputErrors` が対応します。

## 定期実行

`Run-Queue.ps1` は一度キューを処理して終了します。Windows タスクスケジューラで 10 分おきなどに起動すれば、待機ファイル・失敗ファイルは次回再処理し、成功済みはスキップします。最初は手動で動作確認してください。

```powershell
.\Run-Queue.ps1 -InputDirectory 'D:\Recordings\Completed' -OutputDirectory 'D:\Encoded' -DryRun
.\Run-Queue.ps1 -InputDirectory 'D:\Recordings\Completed' -OutputDirectory 'D:\Encoded' -VerifyFull
```

2並列にする場合は `-Jobs 2` を付けます。省略時は1本ずつです。

```powershell
.\Run-Queue.ps1 -InputDirectory 'D:\Recordings\Completed' -OutputDirectory 'D:\Encoded' -Jobs 2 -VerifyFull
```

タスクスケジューラでも同じ引数を使えます。2並列は1つのバッチが管理するため、スケジューラの「新しいインスタンスを開始しない」は引き続き設定してください。

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

テストでは主に外部プロセスをモックし、サービス選択、音声マッピング、上書き防止、入力更新・変換失敗・検査失敗時の扱い、再開、サンプル時間、一括処理を検証します。並列数の上限、並列時の失敗後の継続、割り込み時の新規ジョブ抑止も確認し、子プロセスの終了管理は実際の短いPythonプロセスでも検査します。実際の FFmpeg と放送 TS での画質・同期検証は別途必要です。

実際の FFmpeg を使う動作確認もできます。6秒の人工インターレース MPEG-2 TS（720×480、約29.97fps、SAR 8:9、音声2トラック）を生成し、HEVCのframe/bob、H.264のbobを変換します。全体デコード、フレームレート、ピクセル形式、アスペクト比、音声・言語タグ、完成品の再実行時スキップを検査します。さらに人工素材のコピーの映像ペイロード3パケットを破損させ、厳格モードの停止と、`--allow-input-errors --verify-full` での変換・出力検査の成功を確認します。生成物と `report.json` は `samples/smoke-*` に保存します。

```powershell
py -3 .\tests\smoke_ffmpeg.py --ffmpeg 'C:\ProgramData\chocolatey\bin\ffmpeg.exe' --ffprobe 'C:\ProgramData\chocolatey\bin\ffprobe.exe'
```

対応GPUがある場合は、このコマンドに `--nvenc` を追加するとNVENCの変換・全体デコード・入力fps/形式/音声/SAR・再開も検査します。`--nvenc` でNVENCが利用できない場合は検査失敗になり、スキップはしません。レポートには各MP4のサイズとエンコード段階の処理時間も記録します。6秒の人工素材は動作確認用であり、実番組の画質・容量・速度を代表するベンチマークではありません。

同じ動作確認では、3本の人工TSを `--jobs 2` で同じ出力フォルダーへ変換し、処理時間の重なり・出力検査・`--jobs 1`での再開・不正な入力を含む場合の終了コードも検証します。2026-10-17、RTX 5080 / FFmpeg 9.0.2 / NVENC CQ24で、この並列変換を含む動作確認が成功しました。

2026-10-16、GeForce RTX 5080 / NVIDIA Driver 617.42 / FFmpeg 9.0.2 で `--nvenc` を含む動作確認が成功しました。NVENCでもHEVC・YUV 4:2:0 8bit・約29.97p・SAR保持・AAC 48kHzの2音声・全体デコード・再開を確認しています。

2026-10-16、Chocolatey の FFmpeg / ffprobe 9.0.2（gyan.dev essentials build）で上記の動作確認を実行し、全3プロファイルで成功しました。HEVC初期設定は約29.97p、bobは約59.94p、全出力でYUV 4:2:0・8bitとAAC 48kHz・2音声を確認しています。実際の放送TSのドロップ・方式混在・途中の音声構成変更や主観画質・同期は、この人工素材での検査には含みません。

リサイズの実変換と黒帯の画素検査は次のコマンドで実行できます。`--nvenc` なしならx265を使います。

```powershell
py -3 .\tests\smoke_resize.py --nvenc --ffmpeg 'C:\ProgramData\chocolatey\bin\ffmpeg.exe' --ffprobe 'C:\ProgramData\chocolatey\bin\ffprobe.exe'
```

2026-10-17、RTX 5080 / FFmpeg 9.0.2 / NVENC CQ24で、4:3・非正方形ピクセルの16:9・2:1・縦長の4種類を1280×720へ2並列で変換し、映像領域の寸法・黒帯の画素値・SAR 1:1・全体デコード・再開が成功しました。生成物とレポートは `samples/resize-*` に保存します。

## 公式仕様

- [FFmpeg の入力・出力とストリーム選択](https://ffmpeg.org/ffmpeg.html)
- [libx264 の設定と x264-params](https://ffmpeg.org/ffmpeg-codecs.html#libx264_002c-libx264rgb)
- [libx265 の設定と x265-params](https://ffmpeg.org/ffmpeg-codecs.html#libx265)
- [NVIDIA のFFmpeg NVENC設定](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.1/ffmpeg-with-nvidia-gpu/)
- [x265 のCRF・品質指定](https://x265.readthedocs.io/en/master/cli.html#quality-rate-control-and-rate-distortion-options)
- [bwdif の mode / parity / deint](https://ffmpeg.org/ffmpeg-filters.html#bwdif)
- [ffprobe の JSON 出力](https://ffmpeg.org/ffprobe.html)
