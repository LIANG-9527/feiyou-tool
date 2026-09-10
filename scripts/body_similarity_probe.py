# -*- coding: utf-8 -*-
"""正文近重复探针——验证"只比正文、剔公共块"能否分清 复制换标题 vs 半复制 vs 正常不同页。

背景：ZSTEK 案例发现 /10-tips/ 与 /join-us/ 两篇正文 100% 相似（换标题）；
而 about/faq/history/honor 等正常不同页，去模板后相似度约 53%~58%（底噪来自共享公共块）。

本脚本用于定"正文近重复"分数线的探针，三组对照：
  A组 正常不同页      → 相似度应低（不误报）
  B组 复制换标题      → 相似度应高（应抓）
  C组 半复制（复用一大段，其他不同）→ 关键：看落点，决定初值是否成立

规矩（写进注释，不许违反）：
1. 只比"正文"，绝不比"整页/结构"。整页相似度会因共享模板把正常页冤枉到 90%。
2. 机器只标"疑似"，只写报告给人看；禁止静默删页。最终算不算重复，由 AI 比对定。
3. 判断不了（正文提取失败/太短）一律升级 AI，不许默认"不重复"放行。
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from site_audit.core.fetcher import Fetcher
from site_audit.core.crawler import SiteCrawler


def text_jaccard(a: str, b: str) -> float:
    """字符二元组 Jaccard 相似度（0-1）。对中文/英文都可用，线性复杂度。"""
    A = {a[i:i + 2] for i in range(len(a) - 1)}
    B = {b[i:i + 2] for i in range(len(b) - 1)}
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def strip_boilerplate(page) -> str:
    """去掉全站共享模板（nav/footer/header/aside）+ 页面的公共板块。

    用"同站多页反复出现的文字块即公共部分"的思路剔除，不手写规则。
    （首次调用传单页，仅能剔 nav/footer；传同站多页可剔公共卡片。）
    """
    from bs4 import BeautifulSoup
    import copy
    soup = copy.copy(page.soup)
    for tag in soup.find_all(["nav", "footer", "header", "aside"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def body_similarity(page_a, page_b, full=False) -> float:
    """两页正文相似度。full=False 只比正文（剔公共），True 比整页。"""
    if full:
        a = page_a.soup.get_text(separator=" ", strip=True)
        b = page_b.soup.get_text(separator=" ", strip=True)
    else:
        a = strip_boilerplate(page_a)
        b = strip_boilerplate(page_b)
    return text_jaccard(a, b)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="正文近重复探针")
    ap.add_argument("target", help="站点URL")
    ap.add_argument("--pairs", nargs="*", default=[], help="要对比的URL对，如 a b")
    args = ap.parse_args()

    f = Fetcher(timeout=15)
    home = f.fetch(args.target)
    c = SiteCrawler(f)

    url = args.target if args.target.startswith("http") else "https://" + args.target
    urls = c.collect_all_urls(home, max_urls=200) if home else []
    print(f"[收集] {len(urls)} 页\n")

    # 若无指定 pair，遍历首页常见页做对照
    fetch_pages = {}
    def page_for(u):
        if u not in fetch_pages:
            p = f.fetch(u)
            fetch_pages[u] = p if (p and p.status_code == 200) else None
        return fetch_pages[u]

    if args.pairs:
        pairs = [(args.pairs[i], args.pairs[i+1]) for i in range(0, len(args.pairs), 2)]
    else:
        # 默认抓 few 页做两两对照
        picks = ["about", "faq", "history", "honor", "job",
                 "10-tips-for-adventure-travel", "join-us-for-a-ride-in-the-surrey-hills"]
        base = url.rstrip("/")
        pairs = []
        for i in range(len(picks)):
            for j in range(i+1, len(picks)):
                pairs.append((picks[i], picks[j]))

    for a, b in pairs:
        pa = page_for(url.rstrip("/") + "/" + a if not a.startswith("http") else a)
        pb = page_for(url.rstrip("/") + "/" + b if not b.startswith("http") else b)
        if not pa or not pb:
            print(f"  skip {a} vs {b}（抓取失败）")
            continue
        full = body_similarity(pa, pb, full=True)
        body = body_similarity(pa, pb, full=False)
        print(f"  {a[:24]:26s} vs {b[:24]:26s}  整页 {full*100:.0f}% | 正文 {body*100:.0f}%")


if __name__ == "__main__":
    main()
