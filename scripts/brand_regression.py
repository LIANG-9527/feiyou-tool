#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
品牌名回归表跑表器——企业全称 → 主品牌名。
用法：
  python3 scripts/brand_regression.py            # 只跑 trusted=1（已七渠道核查、走缓存，秒回）
  FULL=1 python3 scripts/brand_regression.py      # 全量跑（含待核对行；未缓存的会触发真实联网核查，慢）
  需服务在 127.0.0.1:8777 运行。退出码非0=有用例错。
判定：actual primary 归一后 == expect_primary，或命中 expect_any 任一，即通过；
      同时 expect_primary 必须出现在返回 items 里（防"主牌对了但正解被丢出清单"）。
"""
import csv
import os
import re
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:8777")
CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brand_regression_cases.csv")
FULL = os.environ.get("FULL") == "1"


def norm(s):
    return re.sub(r"[\s（）()：:，,。·、]", "", str(s or ""))


def lookup(name):
    url = BASE + "/api/brand/lookup-brand?name=" + urllib.parse.quote(name)
    with urllib.request.urlopen(url, timeout=120) as r:
        import json
        return json.loads(r.read().decode("utf-8"))


def main():
    rows = list(csv.DictReader(open(CSV, encoding="utf-8")))
    run = [r for r in rows if FULL or r["trusted"] == "1"]
    print(f"跑 {len(run)}/{len(rows)} 条（trusted=1 {len([r for r in rows if r['trusted']=='1'])} 条"
          + (FULL and "，含待核对" or "，待核对行用 FULL=1 跑") + "）\n")
    passed = failed = 0
    fails = []
    for r in run:
        name = r["input"]
        exp = norm(r["expect_primary"])
        anyset = {norm(x) for x in (r["expect_any"] or "").split("|") if x.strip()} | {exp}
        try:
            d = lookup(name)
        except Exception as e:
            print(f"✗ {name}  请求失败: {e}")
            failed += 1
            fails.append((name, "(请求失败)", r["expect_primary"]))
            continue
        actual = norm(d.get("brand") or d.get("primary") or "")
        items = {norm(i.get("name")) for i in (d.get("brands") or d.get("items") or []) if i.get("name")}
        ok_primary = actual == exp or actual in anyset
        ok_in_items = exp in items
        ok = ok_primary and ok_in_items
        tag = "✓" if ok else "✗"
        print(f"{tag} {name}\n    期望主={r['expect_primary']!r}  实际主={d.get('brand') or d.get('primary')!r}  items={[i.get('name') for i in (d.get('brands') or d.get('items') or [])]}")
        if r.get("note"):
            print(f"    注: {r['note']}")
        if not ok:
            failed += 1
            fails.append((name, d.get("brand") or d.get("primary"), r["expect_primary"]))
        else:
            passed += 1
    print("\n" + "──" * 18)
    print(f"通过 {passed} / {len(run)}")
    if fails:
        print("未通过：")
        for n, act, exp in fails:
            print(f"  - {n}: 实际主={act!r} 期望≈{exp!r}")
        sys.exit(1)
    print("═══ 全部通过 ═══")
    sys.exit(0)


if __name__ == "__main__":
    main()
