"""每日抓取各銀行外幣（美元、日圓）存款牌告利率，寫入 data/fx_rates.json。

用法： python scraper/fetch_fx.py            # 抓全部
       python scraper/fetch_fx.py bot esun   # 只抓指定銀行
"""
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from fetch_rates import TPE, fetch, fetch_rendered, make_soup

OUT = Path(__file__).resolve().parent.parent / "data" / "fx_rates.json"
BANKS = json.loads((Path(__file__).resolve().parent / "fx_banks.json").read_text(encoding="utf-8"))

CURRENCIES = {"USD": re.compile(r"美元|美金|USD", re.I), "JPY": re.compile(r"日圓|日幣|日元|JPY", re.I)}
REQUIRED = {"USD": (1, 3, 6, 12)}          # 美元一定要抓到活存和這些存期；日圓有就收
TENORS = (1, 3, 6, 9, 12)
CN = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
NUM = r"(\d+|[一二兩三四五六七八九十]+)"
VALUE = re.compile(r"^\d{1,2}(\.\d{1,5})?%?$")
EMPTY = re.compile(r"^(|-+|—+|N/?A)$", re.I)


def text_of(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def to_int(s):
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + CN.get(s[1:], 0)
    if s.endswith("十"):
        return CN.get(s[0], 0) * 10
    return CN.get(s, 0)


def column_key(label):
    """欄位標題 → 'demand'、存期月數，或 None（不收的欄位，例如一週、優利活期）。"""
    t = label.replace(" ", "")
    if re.search(r"~|～|以上|大額", t):
        return None
    if re.search(r"優|綜合|週|周|星期|天|日", t):
        return "skip"                       # 認得但不收的欄位，位置仍要算進去
    m = re.search(NUM + r"(個月|月)", t)
    if m:
        return to_int(m.group(1))
    m = re.search(NUM + r"年", t)
    if m:
        return to_int(m.group(1)) * 12
    if re.search(r"活期|活存", t):
        return "demand"
    return None


def currency_of(label):
    """這段文字只講一種我們要的幣別時回傳代號。"""
    if len(label) > 30 or re.search(r"大額|優利|放款|貸", label):
        return None
    hits = [c for c, pat in CURRENCIES.items() if pat.search(label)]
    return hits[0] if len(hits) == 1 else None


def value_of(cell):
    c = cell.replace(" ", "")
    if EMPTY.match(c):
        return None
    if VALUE.match(c):
        return float(c.rstrip("%"))
    return "x"                              # 不是利率也不是空白


def heading_currency(table):
    """表格前面最近的一小段文字若是幣別名稱（例如卡片標題「美元USD」），整張表就是那個幣別。"""
    seen = 0
    for s in table.find_all_previous(string=True):
        t = re.sub(r"\s+", " ", s).strip()
        if not t:
            continue
        if s.find_parent("table") is not None or s.find_parent(["option", "select", "script", "style"]):
            return None
        cur = currency_of(t)
        if cur or any(p.search(t) for p in CURRENCIES.values()):
            return cur
        if re.search(r"[一-鿿]{2,}(USD|EUR|CNY|HKD|GBP|AUD)|\b[A-Z]{3}\b", t) and len(t) < 30:
            return None                     # 是別的幣別的標題
        seen += 1
        if seen >= 6:
            return None
    return None


def put(out, cur, key, val):
    """同一幣別同一欄位，先出現的為準（後面常是機動利率、大額或同業拆款）。"""
    if val is None or val == "x" or key is None:
        return
    d = out.setdefault(cur, {"demand": None, "time": {}})
    if key == "demand":
        if d["demand"] is None:
            d["demand"] = val
    elif key in TENORS:
        d["time"].setdefault(str(key), val)


def parse_fx(html):
    soup = make_soup(html)
    out = {}
    for table in soup.find_all("table"):
        if table.find("table"):
            continue
        rows = [[text_of(c) for c in tr.find_all(["th", "td"], recursive=False)] for tr in table.find_all("tr")]
        rows = [r for r in rows if r]
        # 版型一：一列一個幣別，欄位是活期與各存期
        keys = []
        for r in rows:
            if any(currency_of(c) for c in r[:1]):
                break
            ks = [column_key(c) for c in r]
            if sum(isinstance(k, int) for k in ks) >= 3:
                idx = [i for i, k in enumerate(ks) if k is not None]
                keys = ks[idx[0]: idx[-1] + 1]
        matched = False
        if keys:
            for r in rows:
                cur = currency_of(r[0])
                if not cur:
                    continue
                vals = []
                for c in r[1:]:
                    v = value_of(c)
                    if v == "x":
                        break
                    vals.append(v)
                while len(vals) > len(keys) + 1 and vals[-1] is None:
                    vals.pop()
                if len(vals) == len(keys) + 1 and "demand" not in keys:
                    put(out, cur, "demand", vals[0])
                    vals = vals[1:]
                if len(vals) != len(keys):
                    continue
                matched = True
                complete = bool(cur in out and out[cur]["time"])
                for k, v in zip(keys, vals):
                    if not (complete and k != "demand"):    # 已有一整組定存利率就不混用第二張表
                        put(out, cur, k, v)
            if matched:
                continue
        # 版型二：一張表一個幣別（標題在表格前面），一列一個存期
        cur = heading_currency(table)
        if cur and not (cur in out and out[cur]["time"]):
            for r in rows:
                if len(r) >= 2:
                    v = value_of(r[-1])
                    if isinstance(v, float):
                        put(out, cur, column_key(r[0]), v)
    return out


def validate(data):
    for cur, months in REQUIRED.items():
        d = data.get(cur)
        if not d:
            raise ValueError("找不到 %s 的利率" % cur)
        if d["demand"] is None:
            raise ValueError("%s 缺活存利率" % cur)
        miss = [m for m in months if str(m) not in d["time"]]
        if miss:
            raise ValueError("%s 缺存期 %s 個月" % (cur, miss))
    for cur, d in data.items():
        for v in [d["demand"]] + list(d["time"].values()):
            if v is not None and not 0 <= v <= 15:
                raise ValueError("%s 利率不合理：%s" % (cur, v))


def big_jump(new, old):
    out = []
    for cur, d in new.items():
        o = (old or {}).get(cur) or {}
        for k, v in [("demand", d["demand"])] + list(d["time"].items()):
            ov = o.get("demand") if k == "demand" else (o.get("time") or {}).get(k)
            if v is not None and ov is not None and abs(v - ov) > 1.5:
                out.append("%s %s: %s→%s" % (cur, k, ov, v))
    return out


def read_bank(cfg):
    if not cfg.get("render"):
        try:
            data = parse_fx(fetch(cfg["url"]))
            validate(data)
            return data, ""
        except Exception as e:      # noqa: BLE001
            print("      %s 一般方式讀不到（%s），改用瀏覽器" % (cfg["name"], e))
    data = parse_fx(fetch_rendered(cfg["url"], wait_any=True)[0])
    validate(data)
    return data, "  （瀏覽器模式）"


def main(only=None):
    now = datetime.now(TPE)
    print("外幣抓取程式版本 1，共 %d 家銀行" % len(BANKS))
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
        old = previous.get(cfg["id"])
        try:
            data, mode = read_bank(cfg)
            jumps = big_jump(data, old.get("rates")) if old else []
            if jumps:
                raise ValueError("與前次相差過大，請人工確認：%s" % jumps)
            entry.update(rates=data, status="ok", fetched=now.isoformat(timespec="seconds"))
            jpy = data.get("JPY", {}).get("time", {}).get("12")
            print("OK    %s  美元活存 %s  美元一年 %s  日圓一年 %s%s" % (
                cfg["name"], data["USD"]["demand"], data["USD"]["time"]["12"], jpy, mode))
        except Exception as e:      # noqa: BLE001
            failed.append(cfg["name"])
            print("FAIL  %s  %s" % (cfg["name"], e))
            if old and old.get("rates"):
                entry = dict(old, status="stale", error=str(e))
            else:
                entry.update(status="failed", error=str(e))
        results.append(entry)
        time.sleep(2)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"updated": now.isoformat(timespec="seconds"), "banks": results},
                              ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("已寫入 %s（%d 家，失敗 %d 家）" % (OUT, len(results), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(set(sys.argv[1:]) or None))
