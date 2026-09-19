#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UI 冒烟（浏览器交互层）——2026-09-19 起因：下午一半的坑长在弹窗点击时序/异步竞态上，
graphify 与 pyright 都照不到嵌在 main.py 字符串里的前端逻辑。本脚本用真实浏览器把
名称核查弹窗的三条流程各点一遍，专门钉死"识别中抢点"这个时序用例。

设计红线：
  1) 走真实链路：打开 /mining 真实填表、点按钮，弹窗逻辑跑的是页面上那份 JS。
  2) 全程绝不真建分析任务：每个弹窗验证完一律点"取消/重新输入"，并统计整轮
     POST /api/tasks 次数必须为 0（真建任务会烧 LLM/工商调用，冒烟不该花钱）。
  3) 竞态用例用路由"挂起" /api/brand/lookup-brand 的响应，把"识别中"窗口撑开成
     确定状态：选完 radio 立刻断言提交按钮禁用、无任务 POST，再放行让它解锁。
     用 async API：路由 handler await 事件时让出事件循环，测试协程才能在窗口里跑断言
     （sync API 会在 handler 里阻塞主循环，断言根本没机会执行——踩过的坑）。

用法：
  python3 scripts/ui_smoke.py            # 无头跑（CI/默认）
  HEADED=1 python3 scripts/ui_smoke.py    # 有头调试
  依赖服务已在 127.0.0.1:8777 运行（./scripts/restart.sh 起服）。退出码非 0 = 有流程没过。
"""
import asyncio
import os
import re
import sys

from playwright.async_api import async_playwright, expect

BASE = "http://127.0.0.1:8777"
BRAND_NOT_FOUND = "冠不存在的公司名xyz99887"   # 工商库必查无 → 未找到弹窗
BRAND_UNIQUE = "广州好莱客"                    # 唯一匹配 → 品牌确认弹窗
BRAND_MULTI = "好莱客"                         # 多候选 → 选择企业+异步识别弹窗

results = []


def check(name, cond, detail=""):
    ok = bool(cond)
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (("  | " + detail) if detail else ""))


def new_task_tracker(page, task_posts):
    page.on("request", lambda r: task_posts.append(r)
            if r.method == "POST" and r.url.rstrip("/").endswith("/api/tasks") else None)


async def open_form(page):
    await page.goto(BASE + "/mining")
    await page.locator('[data-field="brand"]').wait_for(state="visible", timeout=10000)


async def fill_and_start(page, brand):
    await page.fill('[data-field="brand"]', brand)
    await page.click("#startBtn")


# ── 用例 1：工商查无 → 未找到弹窗，点"重新输入"关闭，不建任务 ──
async def case_not_found(page, task_posts):
    await open_form(page)
    before = len(task_posts)
    await fill_and_start(page, BRAND_NOT_FOUND)
    await expect(page.locator("text=未找到该企业")).to_be_visible(timeout=8000)
    check("未找到弹窗出现", True)
    await page.locator("#bcancel").click()  # 好的，重新输入
    await expect(page.locator("text=未找到该企业")).to_be_hidden(timeout=3000)
    check("未找到弹窗可关闭", True)
    check("未找到流程未建任务", len(task_posts) == before, f"task_posts +{len(task_posts)-before}")


# ── 用例 2：唯一匹配 → 品牌确认弹窗，品牌框存在，取消不建任务 ──
async def case_unique(page, task_posts):
    await open_form(page)
    before = len(task_posts)
    await fill_and_start(page, BRAND_UNIQUE)
    await expect(page.locator("text=已识别唯一匹配企业")).to_be_visible(timeout=8000)
    check("唯一匹配确认弹窗出现", True)
    check("品牌名输入框已渲染", await page.locator("#bbrand").is_visible())
    await page.locator("#bcancel").click()  # 取消
    await expect(page.locator("text=已识别唯一匹配企业")).to_be_hidden(timeout=3000)
    check("唯一匹配弹窗可取消关闭", True)
    check("唯一匹配流程未建任务", len(task_posts) == before, f"task_posts +{len(task_posts)-before}")


# ── 用例 3：多候选 + 识别中抢点竞态（核心）──────────────────
# 挂起 lookup-brand 响应，把"品牌识别中…"窗口撑开，证明识别中提交按钮禁用、点不动、
# 不建任务；放行后按钮解锁、品牌预填；最后取消，不建任务。
async def case_multi_race(page, task_posts):
    release = asyncio.Event()
    hit = {"n": 0}

    async def hold_lookup(route):
        # 先挂起请求（不打后端），等主断言完成再放行。await 事件会让出事件循环，
        # 测试协程得以在"识别中"窗口里跑断言。
        hit["n"] += 1
        await release.wait()
        resp = await route.fetch()  # 放行后才真打后端，走真实链路
        await route.fulfill(response=resp)

    await page.route(re.compile(r"/api/brand/lookup-brand"), hold_lookup)

    await open_form(page)
    before = len(task_posts)
    await fill_and_start(page, BRAND_MULTI)
    await expect(page.locator("text=可能不是唯一匹配")).to_be_visible(timeout=8000)
    check("多候选弹窗出现", True)

    async def btn_state():
        return await page.evaluate(
            "() => {const e=document.getElementById('bok');"
            "return e ? {d:e.disabled, t:e.textContent} : null}"
        )

    # 选第一家企业 → 触发异步识别（此刻 lookup-brand 被挂起）
    await page.locator('input[name="bcand"]').first.check()

    # ── 竞态核心断言：识别进行中，提交按钮必须禁用、文案、不建任务 ──
    st = None
    for _ in range(60):  # 至多 3s 内等到"禁用中"态
        st = await btn_state()
        if st and st["d"]:
            break
        await asyncio.sleep(0.05)
    check("识别中提交按钮禁用", bool(st and st["d"]), f"state={st}")
    check("识别中按钮文案为'品牌识别中…'", bool(st and "识别中" in st["t"]), st and st["t"])
    check("识别中抢点未建任务", len(task_posts) == before, f"task_posts +{len(task_posts)-before}")
    check("识别请求已被路由拦截(用例前提)", hit["n"] >= 1, f"hit={hit['n']}")

    # 仍在识别中（未放行）：禁用态必须持续，不许中途自己解锁
    await asyncio.sleep(0.2)
    still = await btn_state()
    check("识别中禁用态持续不松", bool(still and still["d"]), f"state={still}")

    # 放行识别响应 → 按钮应解锁、品牌预填
    release.set()
    unlocked = False
    for _ in range(120):  # 至多 6s 等到解锁
        st = await btn_state()
        if st and (not st["d"]) and "用该企业开始" in st["t"]:
            unlocked = True
            break
        await asyncio.sleep(0.05)
    check("识别完成后按钮解锁", unlocked, f"state={st}")

    await page.locator("#bcancel").click()  # 取消，绝不点"用该企业开始"
    await expect(page.locator("text=可能不是唯一匹配")).to_be_hidden(timeout=3000)
    check("多候选弹窗可取消关闭", True)
    check("多候选流程全程未建任务", len(task_posts) == before, f"task_posts +{len(task_posts)-before}")


async def run():
    import urllib.request
    try:
        urllib.request.urlopen(BASE + "/health", timeout=3).read()
    except Exception as e:
        print(f"FAIL  服务未就绪：{BASE}/health 不通（先 ./scripts/restart.sh）：{e}")
        return 2

    task_posts = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=os.environ.get("HEADED") != "1")
        # 用例1
        ctx = await browser.new_context()
        page = await ctx.new_page()
        new_task_tracker(page, task_posts)
        await case_not_found(page, task_posts)
        await page.close(); await ctx.close()
        # 用例2
        ctx = await browser.new_context()
        page = await ctx.new_page()
        new_task_tracker(page, task_posts)
        await case_unique(page, task_posts)
        await page.close(); await ctx.close()
        # 用例3
        ctx = await browser.new_context()
        page = await ctx.new_page()
        new_task_tracker(page, task_posts)
        await case_multi_race(page, task_posts)
        await page.close(); await ctx.close()
        await browser.close()

    print("──" * 20)
    total = len(results)
    passed = sum(1 for r in results if r)
    print(f"UI 冒烟结果：{passed}/{total} 通过；整轮 POST /api/tasks = {len(task_posts)}（应为 0）")
    if passed == total and len(task_posts) == 0:
        print("═══ UI 冒烟通过（三弹窗流程 + 识别中抢点用例）═══")
        return 0
    print("═══ UI 冒烟未通过 ═══")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
