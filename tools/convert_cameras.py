#!/usr/bin/env python3
"""
把「政府資料開放平臺」(data.gov.tw) 下載的測速照相 CSV
合併、清理後轉成 cameras.json。

主要來源：警政署「測速執法設置點」https://data.gov.tw/dataset/7320（全國，CSV）
也可以一起放各縣市自己公布的 CSV 或 JSON（例如路口科技執法設置地點），重複的點會自動去掉。
取締項目沒有超速／測速的（闖紅燈、違規轉彎等）標成「科技執法」。

用法：
    python3 tools/convert_cameras.py 下載的資料/*.csv -o cameras.json

各縣市欄位名稱不同，這支程式會自動辨識常見的欄位名稱。
座標不是 WGS84 經緯度（例如 TWD97 公尺座標）的資料列會被略過並列出。
"""
import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

FIELDS = {
    "lat": ["座標緯度", "緯度", "Latitude", "latitude", "lat", "Y坐標", "Y"],
    "lon": ["座標經度", "經度", "Longitude", "longitude", "lon", "lng", "X坐標", "X"],
    "limit": ["速限", "限速", "速限(公里)", "速限(km/h)", "limit"],
    "direction": ["拍攝方向", "測照方向", "取締方向", "方向", "direct"],
    "address": ["設置地點(路口或路段)", "設置地點", "設置地址", "地點", "設置位置", "Address"],
    "city": ["縣市", "設置縣市", "所在市鄉鎮", "行政區", "CityName"],
    "kind": ["取締項目", "型式", "類別"],
}

DIRS = {"北": 0.0, "東北": 45.0, "東": 90.0, "東南": 135.0,
        "南": 180.0, "西南": 225.0, "西": 270.0, "西北": 315.0}
# 方位字後面接這些字才算方向，避免「往南崁」「往西勢湖路」這類地名被當成方向
_END = r"(?=$|[向方上下行車線道(（)）\s,，、])"
_D = r"(東北|東南|西南|西北|[東西南北])"


def parse_heading(text):
    """把「南向北」「往北」「北上」「西南向東北」等文字轉成車流行進方向（度）。
    雙向、兩個方向都有、或無法判斷時回傳 None（兩個方向都算）。"""
    if not text:
        return None
    s = str(text).strip()
    # 「東西向」「南北雙向」「北南」「多向」都是雙向
    if re.search(r"雙向|雙方向|多向|東西|西東|南北|北南", s):
        return None
    found = set()
    for m in re.finditer(_D + r"\s*(?:向|往|至|到|→|->)\s*" + _D + _END, s):
        found.add(DIRS[m.group(2)])
    if not found:
        for m in re.finditer(r"(?:往|向)\s*" + _D + _END, s):
            found.add(DIRS[m.group(1)])
    if not found:
        if "北上" in s:
            found.add(0.0)
        if "南下" in s:
            found.add(180.0)
    if not found:
        for m in re.finditer(_D + r"\s*(?:向|行|上|下)", s):
            found.add(DIRS[m.group(1)])
    # 「南向50北向60」這類兩個方向都測的，當成雙向
    return found.pop() if len(found) == 1 else None


def parse_kind(item, *texts):
    """統一成三種：區間測速（常只寫在方向或地址欄）、超速、科技執法（闖紅燈、違規轉彎等路口違規）。"""
    if any("區間" in (t or "") for t in (item, *texts)):
        return "區間測速"
    if item and not re.search(r"超速|測速|速率|速度", item):
        return "科技執法"
    return "超速"


def parse_limit(text):
    if not text:
        return None
    m = re.search(r"\d{2,3}", str(text))
    if not m:
        return None
    v = int(m.group())
    return v if 10 <= v <= 130 else None


def pick(row, key):
    for name in FIELDS[key]:
        if name in row and str(row[name]).strip():
            return str(row[name]).strip()
    return None


def read_rows(path):
    if str(path).lower().endswith(".json"):
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        rows = data.get("data", data) if isinstance(data, dict) else data
        return [{str(k).strip(): ("" if v is None else str(v)) for k, v in r.items()} for r in rows]
    for enc in ("utf-8-sig", "cp950", "big5"):
        try:
            with open(path, encoding=enc, newline="") as f:
                rows = list(csv.DictReader(f))
            # 去掉欄位名稱前後空白
            return [{(k or "").strip(): v for k, v in r.items()} for r in rows]
        except UnicodeDecodeError:
            continue
    raise ValueError(f"無法辨識編碼：{path}")


def convert(paths):
    cameras, skipped, seen = [], [], set()
    for path in paths:
        prev_address, prev_item = "", None
        for i, row in enumerate(read_rows(path), start=2):
            # 警政署 CSV 第一列英文欄位、第二列中文欄位，跳過中文欄位那列
            if pick(row, "lat") in FIELDS["lat"]:
                continue
            # 有些縣市把方向黏在座標後面，例如「120.329487 (南向北)」
            raw_lat, raw_lon = pick(row, "lat") or "", pick(row, "lon") or ""
            nums = [re.match(r"\s*(-?\d+(?:\.\d+)?)", t) for t in (raw_lat, raw_lon)]
            lat, lon = (float(m.group(1)) if m else float("nan") for m in nums)
            # 台灣本島與離島範圍（含金門、馬祖）
            if not (21.5 <= lat <= 26.5 and 118.0 <= lon <= 122.5):
                skipped.append(f"{Path(path).name} 第 {i} 列：座標無效或非經緯度")
                continue
            direction = pick(row, "direction") or re.sub(r"[\d.\s]", "", raw_lat + raw_lon) or None
            heading = parse_heading(direction)
            # 同一個路口的第二支相機常只填座標，沿用上一列的地點與取締項目
            address = pick(row, "address") or (prev_address if not pick(row, "kind") else "")
            item = pick(row, "kind") or (prev_item if address == prev_address else None)
            prev_address, prev_item = address, item
            key = (round(lat, 5), round(lon, 5), heading)
            if key in seen:
                continue
            seen.add(key)
            cid = hashlib.sha1(f"{key}".encode()).hexdigest()[:10]
            cameras.append({
                "id": cid,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "limit": parse_limit(pick(row, "limit")),
                "heading": heading,
                "address": address or "",
                "city": pick(row, "city"),
                "kind": parse_kind(item, direction, address),
            })
    return cameras, skipped


def write_meta(out, body, count):
    """寫 cameras.meta.json：使用端可先抓這個小檔比對版本，有新資料才下載整份。
    版本用內容雜湊，資料沒變就不會變，免費用戶不會白看廣告。"""
    path = out.with_name("cameras.meta.json")
    version = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    updated = old.get("updated") if old.get("version") == version else date.today().isoformat()
    meta = {"version": version, "updated": updated, "count": count}
    path.write_text(json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return meta


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="+", help="data.gov.tw 下載的 CSV / JSON 檔")
    ap.add_argument("-o", "--output", default="cameras.json")
    args = ap.parse_args()
    # Windows 終端機預設 cp950，印 emoji 會出錯
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    cameras, skipped = convert(args.csv)
    # 一筆一行：檔案小，資料更新時 git diff 也看得懂
    lines = ",\n".join(json.dumps(c, ensure_ascii=False) for c in cameras)
    body = f"[\n{lines}\n]\n"
    out = Path(args.output)
    out.write_text(body, encoding="utf-8", newline="\n")
    meta = write_meta(out, body, len(cameras))
    print(f"✅ 輸出 {len(cameras)} 個測速點 → {args.output}（版本 {meta['version']}，{meta['updated']}）")
    no_limit = sum(1 for c in cameras if c["limit"] is None)
    no_dir = sum(1 for c in cameras if c["heading"] is None)
    kinds = {k: sum(1 for c in cameras if c["kind"] == k) for k in ("超速", "區間測速", "科技執法")}
    print(f"   無速限：{no_limit}　雙向/無方向：{no_dir}　" + "　".join(f"{k}：{v}" for k, v in kinds.items()))
    if skipped:
        print(f"⚠️ 略過 {len(skipped)} 列：", file=sys.stderr)
        for s in skipped[:20]:
            print("   " + s, file=sys.stderr)


if __name__ == "__main__":
    main()
