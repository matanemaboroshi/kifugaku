# ふるさと納税 買い時チェッカー

楽天ふるさと納税の人気返礼品について、寄付額とレビュー数を毎日自動で記録し、次の情報をまとめた静的サイトを自動で作って公開する。返礼品へのリンクは楽天アフィリエイトのリンクになる。

- **寄付額の値上げ・値下げ**：毎日の変化と、直近30日の変更履歴ページ
- **記録上の最安値**：今の寄付額が、記録を始めてから一番安いかどうか
- **定番判定**：直近30日のうち80%以上、人気上位に入り続けている返礼品
- **勢い**：直近のレビュー増加数（申し込みが集まっている目安）
- **コスパ**：返礼品名から重さを読み取って出す、1kgあたりの寄付額（肉・魚介・米・果物・野菜）

費用は0円（GitHub Actions、GitHub Pages、楽天ウェブサービスを使う）。毎朝6時17分（日本時間）に、取得・記録・サイト更新まで自動で行う。

## 仕組み

1. `scripts/fetch_items.py`：楽天の商品検索API（レビュー数順）で、カテゴリごとに返礼品を取得する。ショップコードが「f＋6桁の数字」の店（自治体）の商品だけを残し、`data/daily/<日付>.json` と `data/catalog.json` に保存する
2. `scripts/build_site.py`：保存したデータから `public/` に静的サイトを作る
3. `.github/workflows/daily.yml`：毎日の実行、データのコミット、GitHub Pagesへの公開

## 最初の設定（一度だけ、約15分）

### 1. 楽天のIDを取る
1. [楽天アフィリエイト](https://affiliate.rakuten.co.jp/) にログインして、アフィリエイトIDを確認する
2. [Rakuten Developers](https://webservice.rakuten.co.jp/) でアプリを新しく登録する
   - アプリのURL（WebサイトURL）には `https://matanemaboroshi.github.io/moremoney/` を入れる
   - 登録後に表示される **アプリID（applicationId）** と **アクセスキー（accessKey）** を控えておく

### 2. GitHubの設定
1. リポジトリを **Public（公開）** にする（Settings → General → Danger Zone → Change visibility）
   - 無料プランでは、非公開のリポジトリからGitHub Pagesを公開できないため。Secretsに登録したIDは公開されない
2. Settings → Pages → Build and deployment の Source を **GitHub Actions** にする
3. Settings → Secrets and variables → Actions → New repository secret で、次の3つを登録する

   | Name | 値 |
   |---|---|
   | `RAKUTEN_APP_ID` | アプリID |
   | `RAKUTEN_ACCESS_KEY` | アクセスキー |
   | `RAKUTEN_AFFILIATE_ID` | アフィリエイトID |

4. このブランチを `main` にマージする（毎日の自動実行は、デフォルトブランチでしか動かない）
5. Actions → daily-update → **Run workflow** で、1回目を手動で実行する

公開URL：https://matanemaboroshi.github.io/moremoney/

勢いと値上げ・値下げは、記録が2日分以上たまると表示される。最安値は7日分、定番は14日分たまると表示される。

### 3. （おすすめ）Google Search Console
[Search Console](https://search.google.com/search-console) にサイトを登録して `sitemap.xml` を送信すると、検索結果に早く載る。

## カスタマイズ

`config.json` を編集する。

- `categories`：カテゴリごとの検索キーワード。`per_kg` を true にすると、1kgあたりの寄付額を計算する
- `pages_per_category`：1ページ30件。4なら最大120件を取得する（自治体以外の店を除くので、実際はもっと少なくなる）
- `search_endpoint`：楽天APIのURL。仕様が変わったときはここを変える
- `site_url`：独自ドメインに変えるときは、ここと楽天アプリに登録したURLの両方を変える

## 手元のパソコンで動かす

```sh
RAKUTEN_APP_ID=... RAKUTEN_ACCESS_KEY=... RAKUTEN_AFFILIATE_ID=... python3 scripts/fetch_items.py
python3 scripts/build_site.py   # public/ に出力される
```
