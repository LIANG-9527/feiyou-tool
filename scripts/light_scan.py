# -*- coding: utf-8 -*-
"""轻量全站扫描（light_scan）——全站元数据并发抓取，几分钟出结果。

用途：快速摸清全站概况（状态码/标题/字数/H1/图片/JSON-LD/响应时间），
用于发现死链、404、标题重复、模板副本、图片缺alt、H1异常等浅层问题。
不进入重度的 AI 语义检查（那部分仍走体检主流程的深度抽样）。

对比价值：第三方工具几分钟能出全站结果，本工具用同样的轻量思路，且加了参数归一化
（避免 ?zstek_product_family=x 这类筛选参数爆炸），单站 39 页实测 4.3 秒。

用法: python scripts/light_scan.py <域名|URL> [--max-urls 500] [--workers 12]
输出: 每页一行元数据 + 汇总（各状态码数量、疑似重复标题、模板副本等）
"""
import sys
import json
import time
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from site_audit.core.fetcher import Fetcher
from site_audit.core.crawler import SiteCrawler


def main():
    ap = argparse.ArgumentParser(description="轻量全站扫描（元数据并发抓取）")
    ap.add_argument("target", help="域名或URL，如 https://jianyongl6.sg-host.com/")
    ap.add_argument("--max-urls", type=int, default=500, help="URL 清单上限")
    ap.add_argument("--workers", type=int, default=12, help="并发线程数")
    ap.add_argument("--json", dest="json_out", default="", help="输出JSON文件路径（可选）")
    args = ap.parse_args()

    url = args.target if args.target.startswith("http") else "https://" + args.target
    f = Fetcher(timeout=15)
    home = f.fetch(url)
    if home is None or home.status_code >= 400:
        print(f"[错误] 无法访问首页 {url}：状态 {getattr(home, 'status_code', '无响应')}")
        return 1

    c = SiteCrawler(f, max_workers=8)
    t0 = time.time()
    urls = c.collect_all_urls(home, max_urls=args.max_urls)
    collect_s = time.time() - t0
    print(f"[收集] 全站 URL 清单 {len(urls)} 个（{collect_s:.1f}s），参数归一化已应用")

    t1 = time.time()
    meta = c.fetch_metadata_concurrent(urls, max_workers=args.workers)
    scan_s = time.time() - t1
    print(f"[抓取] 元数据 {len(meta)} 页（{scan_s:.1f}s，平均 {(scan_s/len(meta))*1000:.0f}ms/页）")
    print(f"[总计] {collect_s + scan_s:.1f}s\n")

    # 汇总分析
    status_counts = {}
    dup_titles = {}
    cloned = []        # 与首页字数相同的页（疑似模板副本）
    h1_multi = []      # H1 异常
    img_missing = []   # 图片缺 alt
    for m in meta:
        sc = m["statusCode"]
        status_counts[sc] = status_counts.get(sc, 0) + 1
        title = m["title"]
        if title:
            dup_titles.setdefault(title, []).append(m["url"])
        if m["wordCount"] == meta[0]["wordCount"] and len(m["url"]) > 20:
            cloned.append(m["url"])
        if m["h1Count"] != 1:
            h1_multi.append((m["url"], m["h1Count"]))
        if m["imagesTotal"] > 0 and m["imagesMissingAlt"] == m["imagesTotal"]:
            img_missing.append((m["url"], m["imagesTotal"]))

    print("== 状态码分布 ==")
    for sc, n in sorted(status_counts.items()):
        print(f"  {sc}: {n} 页")
    print("\n== 疑似重复标题（多页同标题）==")
    for t, us in list(dup_titles.items()):
        if len(us) >= 2:
            print(f"  「{t[:30]}」×{len(us)}：{', '.join(u[:50] for u in us[:3])}")
    print("\n== 与首页字数相同的页（疑似模板副本）==")
    for u in cloned[:10]:
        print(f"  {u[:70]}")
    print("\n== H1 数量异常（非1个）==")
    for u, n in h1_multi[:10]:
        print(f"  {u[:60]}（H1={n}）")
    print("\n== 图片全缺 alt ==")
    for u, n in img_missing[:10]:
        print(f"  {u[:60]}（{n}张全缺）")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n[JSON] 已保存 {args.json_out}（{len(meta)} 页）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
