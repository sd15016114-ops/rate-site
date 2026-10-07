#!/usr/bin/env python3
"""每日抓取各銀行新臺幣存款牌告利率，輸出 data/rates.json。

用法：
    python scraper/fetch_rates.py            # 抓全部銀行
    python scraper/fetch_rates.py bot chb    # 只抓指定銀行（測試用）

設計原則：
- 利率由網頁程式動態填入的銀行（設定 render），改用無頭瀏覽器開啟後再讀。
- 不依賴各家網頁的 CSS 結構，而是讀出所有表格列，用「列的文字」辨識
  （例如「定期存款」「一年」「活期儲蓄存款」），銀行小幅改版時較不容易壞。
- 只取一般額度的利率，大額（含「萬」「以上」等字樣）一律略過。
- 任一家抓取失敗或數值異常時，保留前一次的資料並標記 stale，不會寫入壞資料。
"""
import json
import re
import unicodedata
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
REQUIRED_TIME = (1, 3, 6)                  # 定期存款至少要抓到這些存期；另外一年期要有定存或定儲其中之一
SKIP_WORDS = ("證券", "証券", "薪資", "薪轉", "數位", "親子", "學生",   # 特殊帳戶，不是一般牌告
              "公教", "公益", "優惠", "專案", "優存", "新戶", "外資", "同業")
SPECIAL_PREFIX = re.compile(r"^\((?!一般\))[^()]*\)")                 # 「(優存)1個月定存」這類開頭的特殊方案
NOT_LARGE = ("未達", "以下", "以內", "未滿", "起息", "限額")                              # 「未達三百萬」是一般額度

# ---------------------------------------------------------------- 銀行設定
# 銀行清單放在 scraper/banks.json，新增銀行只要在那裡加一筆 id、name、url。
# 可選欄位：
#   render   true 表示一律用瀏覽器開
#   pages    利率分散在好幾頁時列出每一頁：[{"url": ..., "category": "time"}]，
#            category 用在整頁只有一張表、表上又沒寫類別的情況（time 定存／savings 定儲）
#   select   要先選下拉選單才會顯示的頁面：{"selector": "select#type", "values": ["1", "3"]}
#   categories、demand_savings_labels、demand_labels   該銀行特有的名稱
BANKS = json.loads((Path(__file__).resolve().parent / "banks.json").read_text(encoding="utf-8"))

# ---------------------------------------------------------------- 文字處理
CN = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
LEAD_NUM = re.compile(r"^(\d+|[一二兩三四五六七八九十]+)")
RATE = re.compile(r"^\d{1,2}\.\d{1,5}%?$")


def norm(text):
    """去掉所有空白與全形空白，方便比對「定 期 存 款」這類寫法。"""
    text = unicodedata.normalize("NFKC", text or "")     # 全形數字、符號轉成半形
    return re.sub(r"[\s\u3000\xa0]+", "", text)


def cn_to_int(s):
    if s.isdigit():
        return int(s)
    if "十" in s:
        a, _, b = s.partition("十")
        return (CN.get(a, 1) if a else 1) * 10 + (CN.get(b, 0) if b else 0)
    return CN.get(s)


def is_large(text):
    """是不是大額存款的列或類別（含「萬」「億」但不是「未達／以下」這類一般額度）。"""
    if "大額" in text:
        return True
    return ("萬" in text or "億" in text) and not any(w in text for w in NOT_LARGE)


def clean_label(c):
    """把儲存格整理成純中文標籤：去掉括號內容、英文對照、「利率」字尾與「新臺幣」字首。"""
    prev = None
    while prev != c:
        prev, c = c, re.sub(r"\([^()]*\)", "", c)
    c = re.sub(r"[A-Za-z]+", "", c).strip("~-/:.,、")
    c = re.sub(r"利率$", "", c)
    return re.sub(r"^(新臺幣|新台幣|臺幣|台幣)", "", c)


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


LOOSE_RATE = re.compile(r"(?<![\d.])\d{1,2}\.\d{2,5}(?![\d.])")


def make_soup(html):
    """用 lxml 解析：有些銀行的舊式頁面沒有寫 </tr> 結尾標籤，lxml 會自動補上。"""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:               # noqa: BLE001  沒裝 lxml 時退回內建解析器
        return BeautifulSoup(html, "html.parser")


HEADING_NOISE = ("資料", "生效", "查詢時間", "單位", "年息", "年利率", "下載", "列印")
HAS_CJK = re.compile(r"[\u4e00-\u9fff]")


SAME = "〃"      # 表格緊接在另一張表後面、中間沒有標題：沿用上一張表的類別


def table_heading(table):
    """找出表格上方最近的一段標題文字（例如「定存」「300萬元以下定期存款利率」）。"""
    cap = table.find("caption")
    if cap is not None and norm(cap.get_text()):
        t = norm(cap.get_text())
        return t if len(t) <= 30 else ""
    for text in table.find_all_previous(string=True, limit=40):
        t = norm(text)
        if not t or not HAS_CJK.search(t) or any(w in t for w in HEADING_NOISE):
            continue
        if len(t) > 30:
            return ""
        # 最近的文字其實是前一張表的儲存格內容，而且看不出類別 → 視為同一組表格
        if text.find_parent(["td", "th"]) is not None and heading_category(t) is None:
            return SAME
        return t
    return ""


def heading_category(h):
    """有些銀行每種存款各一張表，類別寫在表格上方的標題，而不是表格裡。"""
    if is_large(h) or any(w in h for w in ("優利", "可轉讓", "存單", "郵政", "專案", "外幣", "放款",
                                             "分期", "零存", "存本", "試算", "同業", "機構")):
        return None
    h = h.replace("性", "")                    # 「定期性存款利率」視同「定期存款利率」
    if "定期儲蓄" in h or "定儲" in h:
        return "savings"
    if "定期存款" in h or "定存" in h:
        return "time"
    return None


def split_cells(text):
    """清單式版面常把「一個月 1.2250 1.2250」寫在同一段文字裡：把數字拆成獨立的格子，
    其餘文字合併成標籤（「一 個 月」這種中間有空白的寫法也能還原）。"""
    out, buf = [], []
    for tok in text.split():
        if RATE.match(tok) or tok in ("-", "--"):
            if buf:
                out.append(norm("".join(buf)))
                buf = []
            out.append(tok)
        else:
            buf.append(tok)
    if buf:
        out.append(norm("".join(buf)))
    return out


def extract_rows(html):
    """回傳頁面上所有「列」，每列是一串已正規化的儲存格文字。
    表格的 <tr> 和清單的 <li> 都算一列（有些銀行用清單排版利率表）。
    每張表格或每個清單開始前會多一列 ["§", 標題]，讓解析時知道換了一區。"""
    soup = make_soup(html)
    rows, seen = [], set()
    for el in soup.find_all(["tr", "li"]):
        if el.name == "tr":
            box = el.find_parent("table")
        else:
            if el.find("li") or el.find_parent("tr"):       # 只取最內層、且不在表格裡的清單項目
                continue
            box = el.find_parent(["ul", "ol"])
        if box is not None and id(box) not in seen:
            seen.add(id(box))
            rows.append(["§", table_heading(box)])
        if el.name == "tr":
            # 取屬於這一列的儲存格：包含被 <font>、<span> 等標籤包住的，但不含巢狀表格裡的
            cells = [norm(c.get_text()) for c in el.find_all(["th", "td"]) if c.find_parent("tr") is el]
            # 有些頁面把利率數字寫在儲存格外面；儲存格裡找不到利率時，改從整列文字找
            if cells and not any(RATE.match(c) for c in cells) and not el.find("tr"):
                cells += LOOSE_RATE.findall(el.get_text(" "))
        else:
            cells = [c for text in el.stripped_strings for c in split_cells(text)]
        rows.append(cells)
    return [r for r in rows if any(r)]


def debug_rows(html, rows):
    """抓取失敗時，把頁面的結構印到紀錄裡：每一區（表格或清單）的標題和前幾列。"""
    blocks, cur = [], None
    for r in rows:
        if r[0] == "§":
            cur = {"title": r[1] if len(r) > 1 else "", "rows": []}
            blocks.append(cur)
        elif cur is not None:
            cur["rows"].append(r)
    rated = [b for b in blocks if any(RATE.match(c) for r in b["rows"] for c in r)]
    print("      共 %d 區，其中 %d 區含有利率數字" % (len(blocks), len(rated)))
    for b in rated[:16]:
        print("      【%s】共 %d 列" % (b["title"][:40] or "（沒有標題）", len(b["rows"])))
        for r in b["rows"][:5]:
            print("        | " + " | ".join(c[:22] for c in r[:6]))
    soup = make_soup(html)
    # 活儲那一列的原始內容：數字沒出現時，可以看出它是怎麼被填進去的
    for el in soup.find_all(["tr", "li"]):
        t = norm(el.get_text())
        if (t.startswith("活期儲蓄") or t.startswith("新臺幣活儲") or t.startswith("活儲")) and not el.find(["tr", "li"]):
            print("      活儲列原始內容：" + re.sub(r"\s+", " ", str(el))[:700])
            break
    if not rated:                   # 沒有可用的表格：印出頁面上相關的文字，看版面長什麼樣子
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        lines = [norm(x) for x in soup.get_text("\n").split("\n")]
        lines = [x for x in lines if x]
        print("      頁面共 %d 行文字，開頭：%s" % (len(lines), " / ".join(lines[:8])[:200]))
        idx = [i for i, x in enumerate(lines) if any(k in x for k in ("活期", "活儲", "定期", "定存"))]
        shown = set()
        for i in idx[:12]:
            for j in range(i, min(i + 6, len(lines))):
                if j not in shown:
                    shown.add(j)
                    print("      %4d: %s" % (j, lines[j][:60]))


# ---------------------------------------------------------------- 解析
SHORT_CATEGORIES = {"定存": "time", "定儲": "savings"}


def split_category(lab, categories):
    """處理類別和存期寫在同一格的標籤，例如「定期存款一個月」「1年定存」。
    回傳 (類別, 月數)，不是這種寫法就回傳 (None, None)。"""
    names = dict(SHORT_CATEGORIES, **categories)
    for name in sorted(names, key=len, reverse=True):
        if lab.startswith(name) and len(lab) > len(name):
            t = tenor_months(lab[len(name):])
            if t:
                return names[name], t
        if lab.endswith(name) and len(lab) > len(name):
            t = tenor_months(lab[:-len(name)])
            if t:
                return names[name], t
    return None, None


def parse_rows(rows, cfg=None):
    cfg = cfg or {}
    categories = {"定期存款": "time", "定期儲蓄存款": "savings"}
    categories.update(cfg.get("categories", {}))
    ds_labels = ["活期儲蓄存款", "活期儲蓄", "活儲存款", "活儲", "活儲息"] + cfg.get("demand_savings_labels", [])
    d_labels = ["活期存款", "活存", "活存息"] + cfg.get("demand_labels", [])

    fixed_first = True          # 預設欄位順序：固定、機動
    kind = None                 # 整張表只有固定或只有機動利率時（寫在表格標題或表頭上）
    category = None
    last_tenor = None           # 固定、機動分成上下兩列時，第二列沒有存期，沿用上一列的
    out = {"demand": None, "demand_savings": None, "time": {}, "savings": {}}

    for cells in rows:
        if not cells:
            continue
        # 0) 新的一張表：用表格上方的標題決定類別
        if cells[0] == "§":
            h = cells[1] if len(cells) > 1 else ""
            if h != SAME:
                category = heading_category(h)
            kind = "fixed" if "固定" in h and "機動" not in h else "floating" if "機動" in h and "固定" not in h else None
            last_tenor = None
            continue

        labels = [clean_label(c) for c in cells]
        nums = [float(c.rstrip("%")) for c in cells if RATE.match(c)]

        # 1) 表頭列：判斷固定／機動哪一欄在前（每張表各自判斷）；只有其中一種時整張表都算那一種
        if not nums and any("固定" in c or "機動" in c for c in cells):
            fi = next((i for i, c in enumerate(cells) if "固定" in c), None)
            mi = next((i for i, c in enumerate(cells) if "機動" in c), None)
            if fi is not None and mi is not None and fi != mi:
                fixed_first = fi < mi
                kind = None
            elif fi is not None and mi is None:
                kind = "fixed"
            elif mi is not None and fi is None:
                kind = "floating"
            continue

        # 2) 類別（定期存款／定期儲蓄存款）；有 rowspan 的表格只在第一列出現，所以要記住
        for raw, lab in zip(cells, labels):
            if lab in categories:
                category = None if is_large(raw) else categories[lab]
                last_tenor = None
                break

        # 3) 大額、薪轉、證券戶、特殊方案等一律略過
        if any(is_large(c) for c in cells):
            if any(is_large(c) and re.search("定期|定存|定儲", c) for c in cells):
                category = None             # 「大額定期存款」是另一個類別，後面幾列都不要
            continue
        if any(w in lab for lab in labels for w in SKIP_WORDS) or any(SPECIAL_PREFIX.match(c) for c in cells):
            continue

        tenor = None
        for lab in labels:                  # 類別和存期寫在同一格（「定期存款一個月」「1年定存」）
            cat, t = split_category(lab, categories)
            if cat:
                category, tenor = cat, t
                break
        if tenor is None:
            tenor = next((t for t in (tenor_months(c) for c in labels) if t), None)

        # 固定、機動寫在各自的列上（儲存格內容就是「固定」或「機動」）
        row_kind = "fixed" if "固定" in cells else "floating" if "機動" in cells else None
        if tenor is not None:
            last_tenor = tenor
        elif row_kind and category and nums:
            tenor = last_tenor

        # 4) 活期利率
        if tenor is None and nums:
            if out["demand_savings"] is None and any(c in ds_labels for c in labels + cells):
                out["demand_savings"] = nums[0]
                continue
            if out["demand"] is None and any(c in d_labels for c in labels + cells):
                out["demand"] = nums[0]
                continue

        # 5) 定存利率
        if tenor is not None and category and nums:
            key = str(tenor)
            if tenor in TENORS:
                k = row_kind or kind
                if k:                                     # 固定、機動分開寫：各填各的
                    out[category].setdefault(key, {}).setdefault(k, nums[0])
                elif key not in out[category]:
                    a, b = (nums[0], nums[1]) if len(nums) >= 2 else (nums[0], nums[0])
                    fixed, floating = (a, b) if fixed_first else (b, a)
                    out[category][key] = {"fixed": fixed, "floating": floating}
            continue

        # 6) 第一格是不認得的項目名稱 → 離開目前類別，避免把別的表誤認成定存
        first = labels[0] if labels else ""
        if nums and first and first not in categories and tenor is None:
            category = None

    # 只抓到固定或機動其中一種時，另一種用同一個數字補上
    for group in (out["time"], out["savings"]):
        for r in group.values():
            r.setdefault("fixed", r.get("floating"))
            r.setdefault("floating", r.get("fixed"))
    return out


def one_year(d):
    """一年期利率：個人戶優先看定期儲蓄存款，沒有才看定期存款。"""
    return (d["savings"].get("12") or d["time"].get("12") or {}).get("fixed")


def validate(d):
    """資料不完整或數值不合理就丟出例外。"""
    if d["demand_savings"] is None:
        raise ValueError("找不到活期儲蓄存款利率")
    missing = [t for t in REQUIRED_TIME if str(t) not in d["time"]]
    if missing:
        raise ValueError("定期存款缺少存期（月）：%s" % missing)
    if one_year(d) is None:
        raise ValueError("找不到一年期的定存或定儲利率")
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


# 等到「活期儲蓄存款」那一列出現利率數字，才算頁面載入完成
WAIT_JS = r"""() => [...document.querySelectorAll('tr, li')].some(el => {
    const t = el.textContent.replace(/\s/g, '');
    return /活期儲蓄|活儲/.test(t) && /\d\.\d{2,}/.test(t);
})"""


# 不特別等活儲那一列，只要頁面上出現任何利率數字就算載入完成（給只有定存的分頁用）
WAIT_ANY_JS = r"""() => [...document.querySelectorAll('tr, li')].some(el => /\d\.\d{2,}/.test(el.textContent))"""


def fetch_rendered(url, select=None, wait_any=False):
    """用無頭瀏覽器開啟頁面，等網頁上的程式把利率填好後再取回內容，回傳一串 HTML。
    select 有設定時，會依序選下拉選單的每個選項，各取回一份。
    只有需要的銀行會用到，所以沒安裝 playwright 也不影響其他銀行。"""
    from playwright.sync_api import sync_playwright
    wait_js = WAIT_ANY_JS if (wait_any or select) else WAIT_JS
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(locale="zh-TW")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            def settle():
                try:                # 有些頁面分好幾次載入資料，先等網路連線都安靜下來
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:   # noqa: BLE001
                    pass
                try:
                    page.wait_for_function(wait_js, timeout=20000)
                except Exception:   # noqa: BLE001  等不到就照樣取回，交給後面的檢查與診斷
                    print("      等候 20 秒仍未出現利率數字")

            settle()
            if not select:
                return [page.content()]
            pages = []
            for value in select["values"]:
                page.select_option(select["selector"], value)
                page.wait_for_timeout(4000)
                settle()
                pages.append(page.content())
            return pages
        finally:
            browser.close()


FORCED_HEADING = {"time": "定期存款", "savings": "定期儲蓄存款"}


def page_rows(html, page_cfg):
    rows = extract_rows(html)
    forced = FORCED_HEADING.get(page_cfg.get("category"))
    if forced:                      # 這一頁整頁都是同一種存款，表格標題一律換成該類別
        rows = [["§", forced] if r[0] == "§" else r for r in rows]
    return rows


def read_bank(cfg):
    """先用一般方式讀；讀不到完整資料就改用瀏覽器再試一次。回傳 (資料, 模式說明)。"""
    pages = cfg.get("pages") or [{"url": cfg["url"]}]
    select = cfg.get("select")
    if not cfg.get("render") and not select:
        try:
            rows = []
            for pg in pages:
                rows += page_rows(fetch(pg["url"]), pg)
            data = parse_rows(rows, cfg)
            validate(data)
            return data, ""
        except Exception as e:      # noqa: BLE001
            print("      %s 一般方式讀不到（%s），改用瀏覽器" % (cfg["name"], e))
    rows, htmls = [], []
    for pg in pages:
        for html in fetch_rendered(pg["url"], select, wait_any=bool(pg.get("category"))):
            htmls.append(html)
            rows += page_rows(html, pg)
    data = parse_rows(rows, cfg)
    try:
        validate(data)
    except ValueError:
        debug_rows(htmls[-1] if htmls else "", rows)
        raise
    return data, "  （瀏覽器模式）"


def main(only=None):
    now = datetime.now(TPE)
    print("抓取程式版本 13，共 %d 家銀行" % len(BANKS))
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
            data, mode = read_bank(cfg)
            old = previous.get(cfg["id"])
            jumps = big_jump(data, old) if old else []
            if jumps:
                raise ValueError("與前次相差超過 1 個百分點，請人工確認：%s" % jumps)
            entry.update(data, status="ok", fetched=now.isoformat(timespec="seconds"))
            print("OK    %s  活儲 %.3f  一年期(固定) %.3f%s" % (
                cfg["name"], data["demand_savings"], one_year(data), mode))
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
