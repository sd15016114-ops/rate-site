# 利率試算站：牌告利率抓取程式

每天自動抓取銀行官網的新臺幣存款牌告利率，存成 `data/rates.json`。
目前收錄 5 家：臺灣銀行、土地銀行、合作金庫、彰化銀行、第一銀行。

## 先在自己的電腦測試

需要 Python 3.9 以上。

    pip install -r scraper/requirements.txt
    python tests/test_parse.py        # 離線測試解析邏輯
    python scraper/fetch_rates.py     # 實際連線抓 5 家
    python scraper/fetch_rates.py bot # 只抓一家

成功的銀行會顯示 `OK`，失敗的顯示 `FAIL` 和原因。結果在 `data/rates.json`。

執行前請把 `scraper/fetch_rates.py` 裡 `HEADERS` 的 `YOUR_EMAIL` 換成你的聯絡信箱，
讓銀行網站管理者知道這支程式是誰的。

## 放上 GitHub 每天自動執行

1. 建立一個 GitHub 程式庫，把整個資料夾上傳（`.github` 資料夾也要）。
2. 到程式庫的 Actions 分頁，選「每日更新利率」，按 Run workflow 手動跑一次確認。
3. 之後每天台灣時間 09:30 左右自動執行，利率有變動才會產生新的提交。
4. 有銀行抓取失敗時，該次執行會顯示紅色，GitHub 會寄信通知你。

## rates.json 格式

    {
      "updated": "2026-10-05T09:30:00+08:00",
      "banks": [{
        "id": "bot", "name": "臺灣銀行", "source": "來源網址",
        "status": "ok",                  // ok 正常、stale 沿用舊資料、failed 從未成功
        "fetched": "抓取時間",
        "demand": 0.705,                 // 活期存款
        "demand_savings": 0.825,         // 活期儲蓄存款
        "time":    {"1": {"fixed": 1.225, "floating": 1.225}, "3": ..., "12": ...},  // 定期存款
        "savings": {"12": {"fixed": 1.725, "floating": 1.715}, "24": ..., "36": ...} // 定期儲蓄存款
      }]
    }

存期以月為單位，保留 1、3、6、9、12、24、36 個月；只收一般額度，不含大額存款。

## 保護機制

- 抓不到資料、缺少必要存期、或利率不在 0 到 10% 之間：不寫入，沿用前一次資料並標記 `stale`。
- 與前一次相差超過 1 個百分點：視為可疑，同樣沿用舊資料，等你確認。
- 每家之間停 2 秒，每天只連線一次。

## 新增銀行

在 `fetch_rates.py` 的 `BANKS` 加一筆 `id`、`name`、`url`。多數靜態表格頁面不需要額外設定；
如果該銀行的類別名稱特殊（例如合作金庫用「儲蓄存款」「一般活儲」），用 `categories`
和 `demand_savings_labels` 補上對應即可。
