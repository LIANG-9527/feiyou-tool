"""测试 WebFetcher：Playwright搜索 + 抓取全文 + 集成profile_pipeline。"""
import asyncio
import sys
import logging
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("test")

async def test_1_playwright_search():
    """测试1：Playwright百度搜索，能否拿到URL"""
    print("\n" + "="*60)
    print("测试1：Playwright百度搜索")
    print("="*60)
    from web_fetcher import WebFetcher
    fetcher = WebFetcher(enable_playwright=True, enable_requests=False)
    try:
        urls = await fetcher.search("深圳极联信息技术股份有限公司 统一社会信用代码", max_results=5)
        print(f"搜到 {len(urls)} 个URL:")
        for i, u in enumerate(urls):
            print(f"  {i+1}. {u[:100]}")
        if not urls:
            print("⚠️  未搜到URL，Playwright搜索可能失败")
        return urls
    finally:
        await fetcher.close()

async def test_2_fetch_pages(urls):
    """测试2：抓取搜到的URL全文"""
    print("\n" + "="*60)
    print("测试2：抓取网页全文")
    print("="*60)
    from web_fetcher import WebFetcher
    fetcher = WebFetcher(enable_playwright=True, enable_requests=True)
    try:
        if not urls:
            print("跳过：无URL可抓")
            return
        # 只抓前2个，避免太慢
        test_urls = urls[:2]
        results = await fetcher.fetch_many(test_urls)
        for i, (url, r) in enumerate(zip(test_urls, results)):
            if r:
                print(f"\n--- 结果{i+1} ---")
                print(f"  URL: {r.final_url[:80]}")
                print(f"  状态码: {r.status_code}")
                print(f"  抓取方式: {r.fetch_method}")
                print(f"  标题: {r.title[:60]}")
                print(f"  正文字数: {len(r.text)}")
                print(f"  正文预览:\n{r.text[:300]}")
            else:
                print(f"\n--- 结果{i+1} ---")
                print(f"  URL: {url[:80]}")
                print(f"  ❌ 抓取失败")
    finally:
        await fetcher.close()

async def test_3_search_and_fetch():
    """测试3：search_and_fetch一条龙"""
    print("\n" + "="*60)
    print("测试3：search_and_fetch一条龙")
    print("="*60)
    from web_fetcher import WebFetcher
    fetcher = WebFetcher(enable_playwright=True, enable_requests=True)
    try:
        results = await fetcher.search_and_fetch(
            "深圳极联信息技术股份有限公司 法定代表人 成立时间",
            max_results=3, min_text_length=100)
        print(f"成功抓取 {len(results)} 个页面:")
        for i, r in enumerate(results):
            print(f"\n--- 页面{i+1} ---")
            print(f"  标题: {r.title[:60]}")
            print(f"  字数: {len(r.text)}")
            print(f"  方式: {r.fetch_method}")
            print(f"  预览: {r.text[:200]}")
        return results
    finally:
        await fetcher.close()

async def test_4_profile_integration():
    """测试4：集成到profile_pipeline，对比有无全文抓取的效果"""
    print("\n" + "="*60)
    print("测试4：集成profile_pipeline（企业画像）")
    print("="*60)

    with open("config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    llm_cfg = cfg["llm"]

    from llm_client import LLMClient
    llm = LLMClient(
        base_url=llm_cfg["base_url"],
        api_key=llm_cfg["api_key"],
        model=llm_cfg["model"],
        temperature=0.3,
        max_tokens=4096,
        timeout=120,
        max_retries=2,
        enable_search=True,
        enable_thinking=False,
        search_engine=["baidu"],
    )

    from profile_pipeline import _search_one

    # 测试单条检索：有全文抓取
    print("\n--- 模式A：启用WebFetcher全文抓取 ---")
    from web_fetcher import WebFetcher
    fetcher = WebFetcher(enable_playwright=True, enable_requests=True)
    try:
        result_a = await _search_one(llm, "深圳极联信息技术股份有限公司", "公司全称 工商信息", fetcher=fetcher)
        print(f"  answer: {result_a.get('answer', '')[:150]}")
        print(f"  quote: {result_a.get('quote', '')[:100]}")
        print(f"  confidence: {result_a.get('confidence', '')}")
        print(f"  has_fulltext: {result_a.get('has_fulltext', False)}")
    finally:
        await fetcher.close()

    # 测试单条检索：无全文抓取（纯百炼搜索）
    print("\n--- 模式B：纯百炼搜索（无全文抓取） ---")
    result_b = await _search_one(llm, "深圳极联信息技术股份有限公司", "公司全称 工商信息", fetcher=None)
    print(f"  answer: {result_b.get('answer', '')[:150]}")
    print(f"  quote: {result_b.get('quote', '')[:100]}")
    print(f"  confidence: {result_b.get('confidence', '')}")
    print(f"  has_fulltext: {result_b.get('has_fulltext', False)}")

async def main():
    # 按顺序测试
    urls = await test_1_playwright_search()
    await test_2_fetch_pages(urls)
    await test_3_search_and_fetch()
    # 测试4比较慢，默认跳过，需要时手动开启
    if "--full" in sys.argv:
        await test_4_profile_integration()
    else:
        print("\n💡 加 --full 参数运行完整集成测试（含LLM调用，较慢）")

if __name__ == "__main__":
    asyncio.run(main())
