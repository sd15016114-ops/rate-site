#!/usr/bin/env python3
"""每日抓取各銀行新臺幣存款牌告利率，輸出 data/rates.json。

用法：
    python scraper/fetch_rates.py            # 抓全部銀行
    python scraper/fetch_rates.py bot chb    # 只抓指定銀行（測試用）

設計原則：
- 不依賴各家網頁的 CSS 結構，而是讀出所有表格列，用「列的文字」辨識
  （例如「定期存款」「一年」「活期儲蓄存款」），銀行小幅改版時較不容易壞。
- 只取一般額度的利率，大額（含「萬」「以上」等字樣）一律略過。
- 任一家抓取失敗或數值異常時，保留前一次的資料並標記 stale，不會寫入壞資料。
"""
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

OUT = Path(__file__).resolve().parent.parent / "data" / "rates.json"
TPE = timezone(timedelta(hours=8))
HEADERS = {"User-Agent": "RateSiteBot/0.1 (daily deposit-rate check; contact: YOUR_EMAIL)"}
TENORS = (1, 3, 6, 9, 12, 24, 36)          # 要保留的存期（月）
REQUIRED_TIME = (1, 3, 6, 12)              # 定期存款至少要抓到這些存期才算成功
SKIP_WORDS = ("萬", "億", "以上", "大額", "證券", "証券", "薪資", "薪轉", "數位", "親子", "學生")

# ---------------------------------------------------------------- 銀行設定
BANKS = [
    {"id": "bot", "name": "臺灣銀行",
     "url": "https://rate.bot.com.tw/twd?Lang=zh-TW"},
    {"id": "land", "name": "土地銀行",
     "url": "https://rate.landbank.com.tw/zh-TW/TWDInfo?mid=23"},
    {"id": "tcb", "name": "合作金庫",
     "url": "https://www.tcb-bank.com.tw/personal-banking/deposit-exchange/deposit-rate/deposit-loans-rate/twd-deposit-rate",
     # 合庫的類別欄寫「儲蓄存款」，活儲寫在對象別「一般活儲」
     "categories": {"儲蓄存款": "savings"},
     "demand_savings_labels": ["一般活儲"]},
    {"id": "chb", "name": "彰化銀行",
     "url": "https://www.bankchb.com/frontend/G0210_020104_query.jsp"},
    {"id": "fcb", "name": "第一銀行",
     "url": "https://ebank.firstbank.com.tw/BATcpibWeb/html/FQ1001.html"},
]

# ---------------------------------------------------------------- 文字處理
CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
LEAD_NUM = re.compile(r"^(\d+|[一二三四五六七八九十]+)")
RATE = re.compile(r"^\d{1,2}\.\d{1,5}%?$")


def norm(text):
    """去掉所有空白與全形空白，方便比對「定 期 存 款」這類寫法。"""
    return re.sub(r"[\s\u3000\xa0]+", "", text or "")


def cn_to_int(s):
    if s.isdigit():
        return int(s)
    if "十" in s:
        a, _, b = s.partition("十")
        return (CN.get(a, 1) if a else 1) * 10 + (CN.get(b, 0) if b else 0)
    return CN.get(s)


def tenor_months(label):
    """把「一個月~未滿三個月」「3至4個月」「一年~二十三個月」「三年」轉成起始月數。"""
    m = LEAD_NUM.match(label)
    if not m:
        return None
    n = cn_to_int(m.group(1))
    if n is None:
        return None
    rest = label[m.end():]
    if rest.startswith("年"):
        return n * 12
    if "個月" in rest or rest.startswith("月"):
        return n
    return None


def extract_rows(html):
    """回傳頁面上所有「列」，每列是一串已正規化的儲存格文字。
    以 <tr> 為主；沒有表格的頁面改讀 <li>。"""
    soup = BeautifulSoup(html, "html.parser")
    rows = [[norm(c.get_text()) for c in tr.find_all(["th", "td"])] for tr in soup.find_all("tr")]
    rows = [r for r in rows if any(r)]
    if not rows:
        rows = [[norm(s) for s in li.stripped_strings] for li in soup.find_all("li")]
        rows = [r for r in rows if any(r)]
    return rows


# ---------------------------------------------------------------- 解析
def parse_rows(rows, cfg=None):
    cfg = cfg or {}
    categories = {"定期存款": "time", "定期儲蓄存款": "savings"}
    categories.update(cfg.get("categories", {}))
    ds_labels = ["活期儲蓄存款"] + cfg.get("demand_savings_labels", [])
    d_labels = ["活期存款"]

    fixed_first = True          # 預設欄位順序：固定、機動
    header_seen = False
    category = None
    out = {"demand": None, "demand_savings": None, "time": {}, "savings": {}}

    for cells in rows:
        # 1) 用表頭判斷固定／機動哪一欄在前
        if not header_seen:
            fi = next((i for i, c in enumerate(cells) if "固定" in c), None)
            mi = next((i for i, c in enumerate(cells) if "機動" in c), None)
            if fi is not None and mi is not None and fi != mi:
                fixed_first = fi < mi
                header_seen = True
                continue

        labels = [re.sub(r"利率$", "", c) for c in cells]
        nums = [float(c.rstrip("%")) for c in cells if RATE.match(c)]

        # 2) 類別（定期存款／定期儲蓄存款）；有 rowspan 的表格只在第一列出現，所以要記住
        for c in labels:
            if c in categories:
                category = categories[c]
                break

        # 3) 大額、薪轉、證券戶等一律略過
        if any(w in c for c in cells for w in SKIP_WORDS):
            continue

        tenor = next((t for t in (tenor_months(c) for c in labels) if t), None)

        # 4) 活期利率
        if tenor is None and nums:
            if out["demand_savings"] is None and any(c in ds_labels for c in labels):
                out["demand_savings"] = nums[0]
                continue
            if out["demand"] is None and any(c in d_labels for c in labels):
                out["demand"] = nums[0]
                continue

        # 5) 定存利率
        if tenor is not None and category and nums:
            if tenor in TENORS and str(tenor) not in out[category]:
                a, b = (nums[0], nums[1]) if len(nums) >= 2 else (nums[0], nums[0])
                fixed, floating = (a, b) if fixed_first else (b, a)
                out[category][str(tenor)] = {"fixed": fixed, "floating": floating}
            continue

        # 6) 第一格是不認得的項目名稱 → 離開目前類別，避免把別的表誤認成定存
        first = labels[0] if labels else ""
        if first and first not in categories and tenor is None:
            category = None

    return out


def validate(d):
    """資料不完整或數值不合理就丟出例外。"""
    if d["demand_savings"] is None:
        raise ValueError("找不到活期儲蓄存款利率")
    missing = [t for t in REQUIRED_TIME if str(t) not in d["time"]]
    if missing:
        raise ValueError("定期存款缺少存期（月）：%s" % missing)
    values = [d["demand_savings"]] + ([d["demand"]] if d["demand"] is not None else [])
    for group in (d["time"], d["savings"]):
        for r in group.values():
            values += [r["fixed"], r["floating"]]
    bad = [v for v in values if not 0 < v < 10]
    if bad:
        raise ValueError("利率數值不合理：%s" % bad)


def big_jump(new, old):
    """與前一次相比，任何一項變動超過 1 個百分點就視為可疑。"""
    def flat(d):
        items = {"ds": d.get("demand_savings")}
        for g in ("time", "savings"):
            for k, r in (d.get(g) or {}).items():
                items[g + k] = r["fixed"]
        return items
    a, b = flat(new), flat(old)
    return [k for k in a if a[k] is not None and b.get(k) is not None and abs(a[k] - b[k]) > 1.0]


# ---------------------------------------------------------------- 主流程
def fetch(url):
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.content        # 交給 BeautifulSoup 自行判斷編碼
        except Exception as e:      # noqa: BLE001
            last = e
            time.sleep(5 * (attempt + 1))
    raise last


def main(only=None):
    now = datetime.now(TPE)
    previous = {}
    if OUT.exists():
        previous = {b["id"]: b for b in json.loads(OUT.read_text(encoding="utf-8")).get("banks", [])}

    results, failed = [], []
    for cfg in BANKS:
        if only and cfg["id"] not in only:
            if cfg["id"] in previous:
                results.append(previous[cfg["id"]])
            continue
        entry = {"id": cfg["id"], "name": cfg["name"], "source": cfg["url"]}
        try:
            data = parse_rows(extract_rows(fetch(cfg["url"])), cfg)
            validate(data)
            old = previous.get(cfg["id"])
            jumps = big_jump(data, old) if old else []
            if jumps:
                raise ValueError("與前次相差超過 1 個百分點，請人工確認：%s" % jumps)
            entry.update(data, status="ok", fetched=now.isoformat(timespec="seconds"))
            print("OK    %s  活儲 %.3f  一年定存(固定) %.3f" % (
                cfg["name"], data["demand_savings"], data["time"]["12"]["fixed"]))
        except Exception as e:      # noqa: BLE001
            failed.append(cfg["name"])
            print("FAIL  %s  %s" % (cfg["name"], e))
            if cfg["id"] in previous:       # 保留舊資料，標記未更新
                entry = dict(previous[cfg["id"]], status="stale", error=str(e))
            else:
                entry.update(status="failed", error=str(e))
        results.append(entry)
        time.sleep(2)               # 對銀行網站客氣一點

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"updated": now.isoformat(timespec="seconds"), "banks": results},
                              ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("已寫入 %s（%d 家，失敗 %d 家）" % (OUT, len(results), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(set(sys.argv[1:]) or None))
