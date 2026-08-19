# location_history_movie

Google マップのタイムライン（ロケーション履歴）エクスポート JSON から、
移動の様子をスタイリッシュなムービー (mp4) に変換するツール。

[google-timeline-visualizer](https://github.com/mahlernim/google-timeline-visualizer)
にインスパイアされた、追従カメラ + 発光トレイル + ダークマップのアニメーションを生成します。

- CartoDB ダーク（またはライト）ベースマップ上を、現在地に追従するカメラが移動
- 移動範囲に応じて自動でズームイン / ズームアウト
- 残光のように減衰するトレイル（シアン → バイオレットのグラデーション）
- 日付・時刻・曜日の HUD と期間プログレスバー
- 停滞中（自宅・滞在中など）は自動で追加早送り
- 横長 (1920x1080) と縦長 (1080x1920) の 2 種類を一度に出力可能

## 必要なもの

- Python 3.10+
- ffmpeg（`brew install ffmpeg` など）
- ネットワーク接続（地図タイルの取得。`tile_cache/` にキャッシュされます）

```bash
python3 -m venv .venv
.venv/bin/pip install pillow requests
```

## データの用意

スマホの Google マップ → プロフィールアイコン → 設定 →
「位置情報とプライバシー」→「タイムラインデータをエクスポート」で
得られる `location-history.json`（`activity` / `visit` / `timelinePath`
セグメントを含む新形式）をこのディレクトリに置きます。

## 使い方

```bash
# 全期間を約90秒のムービーに（横長・縦長の両方を出力）
.venv/bin/python timeline_movie.py --duration 90 -o mymovie

# 期間を指定（JST・日付のみなら end はその日いっぱいまで含む）
.venv/bin/python timeline_movie.py --start 2025-07-01 --end 2025-07-31 --duration 60

# 早送り倍率を直接指定：実時間1800秒（30分）を動画の1秒に
.venv/bin/python timeline_movie.py --start 2025-08-01 --end 2025-08-10 --speedup 1800

# 時刻まで指定して1日だけ・縦長のみ
.venv/bin/python timeline_movie.py --start 2025-07-05T06:00 --end 2025-07-05T23:00 \
    --speedup 300 --orientation portrait
```

出力は `<basename>_landscape.mp4` / `<basename>_portrait.mp4`。

## 主なオプション

| オプション | 意味 | デフォルト |
|---|---|---|
| `--start` / `--end` | 期間（JST, `YYYY-MM-DD` または `YYYY-MM-DDTHH:MM`） | データ全期間 |
| `--speedup` | 移動中の早送り倍率（実秒 / 動画秒）。例 `3600` = 1時間/秒 | `--duration` から自動計算 |
| `--duration` | 目標の動画長（秒）。`--speedup` 指定時は無視 | 75 |
| `--idle-speedup` | 停滞中にさらに掛かる早送り係数。`1` で等速 | 10 |
| `--trail-hours` | トレイルの残存時間（実時間の時間数） | 期間長から自動 |
| `--orientation` | `landscape` / `portrait` / `both` | `both` |
| `--width` / `--height` | 任意解像度（1本のみ出力） | — |
| `--fps` | フレームレート | 30 |
| `--style` | `dark` / `light` | `dark` |
| `--zoom-min` / `--zoom-max` | カメラズームの範囲 | 3 / 16 |
| `--fade` | 冒頭・末尾のフェード秒数（0 で無効） | 0.8 |
| `--tile-cache` | タイルキャッシュのディレクトリ | `tile_cache` |
| `--view-hours` | カメラの注視ウィンドウ（直近N時間の動きにズームをフィット） | min(6, trail) |
| `--home-speedup` | 自宅圏内でさらに掛かる早送り係数（>1 で旅行フォーカス） | 1（無効） |
| `--home-radius-km` | 自宅圏の半径 | 80 |
| `--home` | 自宅座標 `lat,lon`（省略時は滞在時間から自動検出） | 自動 |
| `--workers` | フレーム描画の並列プロセス数（0=自動: コア数−2） | 0 |
| `--encoder` | `auto` / `x264` / `nvenc` / `videotoolbox` | `auto` |
| `--ffmpeg` | ffmpeg バイナリのパス | `ffmpeg` |

## 旅行フォーカスモード

`--home-speedup 8` のように指定すると、滞在時間が最長の場所を「自宅」と自動検出し、
その `--home-radius-km` 圏内にいる時間をさらに 8 倍速で流します。
日常の移動（通勤・都内の移動など）は一瞬で過ぎ、旅行部分が動画の大半になります。
`--view-hours` はカメラのズームを「直近N時間の動き」にフィットさせるので、
旅先に着いた後は現地での動き回りにズームインしていきます
（トレイル自体は `--trail-hours` の長さで残ります）。

## GPU / マルチコア

- フレーム描画はデフォルトでマルチプロセス並列（`--workers 0` = コア数−2）
- エンコードは `--encoder auto` で GPU エンコーダ（NVIDIA NVENC / Apple
  VideoToolbox）を自動検出し、なければ libx264 (CPU) にフォールバック
- NVENC には NVENC 対応の ffmpeg ビルドが必要です（例:
  [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases) の
  linux64-gpl。johnvansickle の静的ビルドは NVENC 非対応）

## 早送りの考え方

動画の再生速度は 2 段階です。

- **移動中**: `--speedup` 倍（例 `1800` なら実時間30分 → 動画1秒）
- **停滞中**（速度 0.7km/h 未満が15分以上続く区間）: `--speedup × --idle-speedup` 倍

そのため夜間や滞在中が長くても間延びせず、移動シーンが動画の大半を占めます。
`--duration` を指定すると、この 2 段階構成を踏まえて全体が指定秒数に収まるよう
`--speedup` が自動計算されます。

## 地図について

タイルは [CARTO basemaps](https://carto.com/basemaps/)
(© OpenStreetMap contributors, © CARTO) を使用しています。
個人利用の範囲でご利用ください。
