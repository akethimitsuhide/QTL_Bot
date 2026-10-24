# web/vendor/ — 同梱している外部ライブラリ

ダッシュボードは外部の CDN に依存せず、次のライブラリの **版を固定して** この場所に同梱しています
（CDN の改ざん・停止・追跡の影響を受けず、LAN 内だけでも動かすため）。
配信は `core/web_static.py` の許可リスト（`ASSETS`）にあるファイルだけで、LICENSE や本ファイルは配信されません。

| ファイル | ライブラリ | 版 | ライセンス | 入手元 | SHA-256 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `chart.umd.min.js` | Chart.js | 4.5.1 | MIT（`chart.js.LICENSE.md`） | npm `chart.js@4.5.1` の `dist/chart.umd.js` | `ecc3cd1eeb8c34d2178e3f59fd63ec5a3d84358c11730af0b9958dc886d7652a` |
| `leaflet.js` | Leaflet | 1.9.4 | BSD-2-Clause（`leaflet.LICENSE`） | npm `leaflet@1.9.4` の `dist/leaflet.js` | `db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a` |
| `leaflet.css` | Leaflet | 1.9.4 | 同上 | npm `leaflet@1.9.4` の `dist/leaflet.css` | `a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6` |

- ファイルの内容は npm パッケージ内のものと同一です（改変なし）。確認: `sha256sum web/vendor/*`
- Chart.js は公開から十分に時間が経った安定版を選んでいます（公開直後の版は避ける方針）。
- Leaflet の画像（`images/`）は、レイヤー切り替えコントロールやマーカー画像に使うものですが、このダッシュボードは
  どちらも使わない（円マーカーのみ）ため同梱していません。

## 更新するとき
1. 新しい版の npm パッケージを取得する（例: `npm pack chart.js@<版>`）。公開から 2 週間以上経った版を選ぶ。
2. `dist/` の該当ファイルを置き換え、LICENSE も更新する。
3. 上の表の版・SHA-256 を更新する。
4. `python3 -m pytest tests/test_web_static.py` と、ブラウザで `/dashboard`・`/quake_map` の表示を確認する。
