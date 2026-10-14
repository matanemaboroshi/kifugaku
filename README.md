# 楽天ランキング急上昇ウォッチ

楽天市場のジャンル別ランキングを毎日自動取得・記録し、「急上昇」「初登場」「値下がり」「順位の推移」をまとめた静的サイトを自動生成・公開する。商品リンクは楽天アフィリエイトリンク。

- 費用：0円（GitHub Actions + GitHub Pages + 楽天ウェブサービス）
- 運用：毎朝 6:17（JST）に自動で取得 → 記録 → サイト更新
- ページ：トップ / ジャンル別ランキング（10ジャンル） / 商品ごとの順位推移ページ / sitemap.xml

## 初回セットアップ（約15分・一度だけ）

### 1. 楽天のIDを取得する
1. [楽天アフィリエイト](https://affiliate.rakuten.co.jp/) にログインし、アフィリエイトIDを確認する
2. [Rakuten Developers](https://webservice.rakuten.co.jp/) でアプリを新規登録する
   - アプリURL / WebサイトURL：`https://matanemaboroshi.github.io/moremoney/`
   - 登録後に表示される **アプリID（applicationId）** と **アクセスキー（accessKey）** を控える

### 2. GitHubの設定
1. リポジトリを **Public** にする（Settings → General → Danger Zone → Change visibility）
   - 無料プランでは非公開リポジトリから GitHub Pages を公開できないため。Secrets は公開されない
2. Settings → Pages → Build and deployment の Source を **GitHub Actions** にする
3. Settings → Secrets and variables → Actions → New repository secret で3つ登録する

   | Name | 値 |
   |---|---|
   | `RAKUTEN_APP_ID` | アプリID |
   | `RAKUTEN_ACCESS_KEY` | アクセスキー |
   | `RAKUTEN_AFFILIATE_ID` | アフィリエイトID |

4. このブランチを `main` にマージする（定期実行はデフォルトブランチでのみ動く）
5. Actions → daily-update → **Run workflow** で初回を実行する

公開URL：https://matanemaboroshi.github.io/moremoney/

### 3. （推奨）Google Search Console
[Search Console](https://search.google.com/search-console) にサイトを登録し、`sitemap.xml` を送信すると検索エンジンへの反映が早くなる。

## カスタマイズ

`config.json` を編集する。

- `genres`：取得するジャンル（楽天のジャンルID）
- `pages_per_genre`：1ページ30件。2なら60位まで
- `keep_days`：保存するスナップショットの日数
- `site_url`：独自ドメインに変える場合はここと楽天アプリの登録URLを両方変更する

## ローカルで動かす

```sh
RAKUTEN_APP_ID=... RAKUTEN_ACCESS_KEY=... RAKUTEN_AFFILIATE_ID=... python3 scripts/fetch_rankings.py
python3 scripts/build_site.py   # public/ に出力
```
