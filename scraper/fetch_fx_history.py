"""累積臺灣銀行每個營業日的牌告匯率（全部幣別），一年存一個檔 data/fx_history_YYYY.json。

每次執行會把 2025/1/1 到昨天之間還沒有的日期補上：第一次會補齊全部歷史，之後每天只抓缺的那幾天。
"""
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from fetch_rates import TPE, fetch

DATA = Path(__file__).resolve().parent.parent / "data"
START = date(2025, 1, 1)
URL = "https://rate.bot.com.tw/xrt/flcsv/0/%s"
RECHECK_DAYS = 5            # 最近幾天查不到資料時先不認定為假日，之後再查一次


def parse_day(raw):
    """CSV 一列一個幣別 → {幣別: [現金買入, 即期買入, 現金賣出, 即期賣出]}，0 代表沒有提供。"""
    text = raw.decode("utf-8-sig", "replace") if isinstance(raw, bytes) else raw
    out = {}
    for line in text.strip().splitlines()[1:]:
        c = [x.strip() for x in line.split(",")]
        if len(c) < 14 or not (len(c[0]) == 3 and c[0].isalpha()):
            continue
        try:
            vals = [float(c[i]) for i in (2, 3, 12, 13)]
        except ValueError:
            continue
        if any(vals):
            out[c[0].upper()] = vals
    return out


def load(year):
    p = DATA / ("fx_history_%d.json" % year)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"days": {}, "closed": []}


def save(year, d, now):
    d["updated"] = now.isoformat(timespec="seconds")
    d["days"] = dict(sorted(d["days"].items()))
    d["closed"] = sorted(set(d["closed"]))
    (DATA / ("fx_history_%d.json" % year)).write_text(
        json.dumps(d, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def main():
    now = datetime.now(TPE)
    today = now.date()
    files, added, errors = {}, 0, 0
    day = START
    while day < today:
        key = day.isoformat()
        f = files.setdefault(day.year, load(day.year))
        if day.weekday() < 5 and key not in f["days"] and key not in f["closed"]:
            try:
                data = parse_day(fetch(URL % key))
            except Exception as e:      # noqa: BLE001
                errors += 1
                print("FAIL  %s  %s" % (key, e))
                if errors >= 5:
                    print("連續失敗太多次，先停止，下次執行再補")
                    break
                day += timedelta(days=1)
                continue
            if "USD" in data:
                f["days"][key] = data
                added += 1
            elif (today - day).days > RECHECK_DAYS:
                f["closed"].append(key)     # 平日但沒有牌告：國定假日或颱風假
            if added and added % 50 == 0:
                print("已補 %d 天（到 %s）" % (added, key))
            time.sleep(0.6)
        day += timedelta(days=1)
    for year, f in files.items():
        if f["days"] or f["closed"]:
            save(year, f, now)
    total = sum(len(f["days"]) for f in files.values())
    print("歷史匯率：新增 %d 天，目前共 %d 個營業日，失敗 %d 次" % (added, total, errors))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
