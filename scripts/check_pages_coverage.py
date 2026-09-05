# -*- coding: utf-8 -*-
"""来源可溯性静态扫描：整站/多页范围的检查结果必须带 pages。

进度⑰确立的验收标准（2026-09-06）：凡检查方法 scope/applicable_pages 声明含
"全站/内页/多页"，其每个非 SKIP 的 _result(...) 都必须挂 pages（结果实际涉及的
页面清单），否则报告来源会退化成"从文案反扒 URL"或"整站总结"兜底。

用法: python scripts/check_pages_coverage.py
输出: 违规清单；无输出（剩余=0）即通过。修改检查器后建议顺手跑一遍。
"""
import re
import glob
import sys

ROOT = __file__.rsplit("/", 2)[0]


def result_calls(src: str) -> list:
    """提取完整的 self._result(...) 调用文本（括号配平，不惧嵌套）。"""
    out, i = [], 0
    while True:
        i = src.find("self._result(", i)
        if i < 0:
            break
        j, depth = i + len("self._result("), 1
        while depth and j < len(src):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
            j += 1
        out.append(src[i:j])
        i = j
    return out


def main() -> int:
    issues = 0
    for f in sorted(glob.glob(f"{ROOT}/site_audit/audit/*.py")):
        src = open(f).read()
        for m in re.split(r"\n(?=    def )", src):
            dm = re.match(r"    def (\w+)", m)
            if not dm or "_item(" not in m:
                continue
            sm = re.search(r'scope="([^"]*)"', m)
            am = re.search(r'applicable_pages="([^"]*)"', m)
            scope = (sm.group(1) if sm else "") + (am.group(1) if am else "")
            if not any(k in scope for k in ("全站", "内页", "多页")):
                continue
            iid = re.search(r'"([a-z]+-\d+)"', m)
            iid = iid.group(1) if iid else "??"
            for r in result_calls(m):
                if "pages=" not in r and "Status.SKIP" not in r:
                    issues += 1
                    print(f"{f}: {iid} {dm.group(1)} 非 SKIP 结果缺 pages -> {r[:80]}...")
    print(f"剩余未挂 pages 的非 SKIP 结果: {issues}")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
