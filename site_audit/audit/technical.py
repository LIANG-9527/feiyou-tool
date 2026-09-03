"""技术可抓取性检查（11项）"""
import re
from typing import List
from .checker import BaseChecker
from site_audit.core.models import CheckResult, Status, Priority, Category
from site_audit.core.fetcher import PageData


class TechnicalChecker(BaseChecker):
    category = Category.TECHNICAL

    def run(self) -> List[CheckResult]:
        results = []
        results.append(self._check_https())
        results.append(self._check_robots_txt())
        results.append(self._check_sitemap())
        results.append(self._check_llms_txt())
        results.append(self._check_well_known())
        results.append(self._check_indexability())
        results.append(self._check_canonical())
        results.append(self._check_status_codes())
        results.append(self._check_content_accessibility())
        results.append(self._check_page_size_speed())
        results.append(self._check_mobile_friendly())
        results.append(self._check_url_slug())
        results.append(self._check_cms_fingerprint())
        results.append(self._check_admin_exposure())
        return results

    # 1. HTTPS
    def _check_https(self) -> CheckResult:
        item = self._item(
            "tech-01", "HTTPS 安全连接", Priority.P0,
            "站点是否使用 HTTPS 加密连接",
            "检查最终 URL 是否为 https 开头",
            "全站 HTTPS，证书有效未过期",
            manual_check="浏览器打开网站，看地址栏是否有小锁图标；或访问 https:// 开头的网址看是否正常跳转",
            purpose="HTTPS 是 AI 爬虫和搜索引擎的基础信任信号，未加密的站点会被降低抓取优先级和排名",
            excellent="全站 HTTPS，证书有效期 > 3 个月，支持 TLS 1.2+，HSTS 已开启",
            normal="已启用 HTTPS，证书有效，但可能未开启 HSTS 或存在混合内容",
            failed="未启用 HTTPS，或证书已过期/不受信任",
        )
        if self.page.final_url.startswith("https://"):
            return self._result(item, Status.PASS, "已启用 HTTPS")
        return self._result(item, Status.FAIL, "未使用 HTTPS，AI 爬虫可能降低信任度")

    # 2. robots.txt
    def _check_robots_txt(self) -> CheckResult:
        item = self._item(
            "tech-02", "robots.txt", Priority.P0,
            "robots.txt 是否存在，是否封禁 AI 爬虫，是否引用 sitemap",
            "访问 /robots.txt，检查 Disallow 规则和 Sitemap 引用",
            "允许主流 AI 爬虫，且引用了 sitemap 地址",
            manual_check="浏览器访问 https://你的域名/robots.txt，查看是否有针对 GPTBot/ClaudeBot/Google-Extended/Bytespider 的 Disallow 规则，以及最后是否有 Sitemap: 行",
            purpose="robots.txt 决定 AI 爬虫能不能抓你的站。另外，在robots.txt中引用sitemap，能让搜索引擎和AI爬虫自动发现你的sitemap，提升收录效率",
            excellent="robots.txt 存在，明确允许所有主流 AI 爬虫，引用了 sitemap 地址，规则清晰无歧义",
            normal="robots.txt 存在，未封禁主流 AI 爬虫，但未引用 sitemap 地址",
            failed="robots.txt 不存在，或封禁了主流 AI 爬虫",
        )
        resp = self.fetcher.fetch_raw(f"{self.base_url}/robots.txt")
        if resp is None or resp.status_code != 200:
            return self._result(item, Status.WARN, "未找到 robots.txt（默认允许所有爬虫）")

        content = resp.text.lower()
        ai_bots = ["gptbot", "claudebot", "google-extended", "bytespider",
                   "applebot", "chatgpt-user", "perplexitybot", "cohere-ai"]
        blocked = []
        for bot in ai_bots:
            pattern = rf"user-agent:\s*{re.escape(bot)}.*?disallow:\s*/"
            if re.search(pattern, content, re.DOTALL | re.IGNORECASE):
                blocked.append(bot)

        has_sitemap_ref = "sitemap:" in content

        if blocked:
            return self._result(
                item, Status.FAIL,
                f"robots.txt 封禁了 {len(blocked)} 个 AI 爬虫",
                detail=f"被封禁的爬虫: {', '.join(blocked)}",
            )
        if has_sitemap_ref:
            return self._result(item, Status.PASS, "robots.txt 存在，未封禁 AI 爬虫，已引用 sitemap")
        return self._result(item, Status.WARN, "robots.txt 存在，未封禁 AI 爬虫，但未引用 sitemap 地址")

    # 3. sitemap.xml
    def _check_sitemap(self) -> CheckResult:
        item = self._item(
            "tech-03", "sitemap.xml 站点地图", Priority.P0,
            "是否存在 sitemap.xml，格式是否合法",
            "访问 /sitemap.xml，检查 XML 格式和 URL 数量",
            "存在且可访问，包含核心页面",
            manual_check="浏览器访问 https://你的域名/sitemap.xml，看是否返回 XML 格式的页面列表，包含产品/案例/文章等核心页面 URL",
            purpose="sitemap 告诉 AI 爬虫和搜索引擎你有哪些页面，加快收录速度，避免重要页面被漏掉",
            excellent="sitemap 存在且格式规范，包含所有核心页面，按类型分 sitemap 索引，最后修改时间准确，已提交站长平台",
            normal="sitemap 存在且可访问，包含主要页面，但可能未更新或未提交站长平台",
            failed="sitemap 不存在，或格式错误无法解析，或几乎不包含任何 URL",
        )
        resp = self.fetcher.fetch_raw(f"{self.base_url}/sitemap.xml")
        if resp is None or resp.status_code != 200:
            for path in ["/sitemap_index.xml", "/sitemap.php", "/sitemap"]:
                resp = self.fetcher.fetch_raw(f"{self.base_url}{path}")
                if resp and resp.status_code == 200:
                    break
            if resp is None or resp.status_code != 200:
                return self._result(item, Status.FAIL, "未找到 sitemap.xml")

        content = resp.text
        url_count = len(re.findall(r"<loc>", content))
        if url_count == 0:
            return self._result(item, Status.WARN, "sitemap 存在但未包含任何 URL")
        return self._result(item, Status.PASS, f"sitemap 存在，包含 {url_count} 个 URL")

    # 4. llms.txt
    def _check_llms_txt(self) -> CheckResult:
        item = self._item(
            "tech-04", "llms.txt 大模型说明文件", Priority.P1,
            "是否存在 llms.txt，为大模型提供站点说明",
            "访问 /llms.txt，检查内容有效性",
            "存在，包含站点简介和核心页面列表",
            manual_check="浏览器访问 https://你的域名/llms.txt，看是否有纯文本的站点介绍、核心页面链接列表和更新时间",
            purpose="llms.txt 是 GEO 的核心文件，专门给大模型看的'站点说明书'。有了它，AI 能快速理解你是做什么的、有哪些重要页面，大幅提升被引用概率",
            excellent="llms.txt 存在，包含清晰的站点简介、分类核心页面链接（10+）、服务说明、联系方式和最后更新时间",
            normal="llms.txt 存在，有站点简介和部分页面链接，但内容较简单或更新时间缺失",
            failed="llms.txt 不存在（GEO 关键文件缺失）",
        )
        resp = self.fetcher.fetch_raw(f"{self.base_url}/llms.txt")
        if resp is None or resp.status_code != 200:
            return self._result(item, Status.FAIL, "未找到 llms.txt（GEO 关键文件缺失）")

        content = resp.text.strip()
        if len(content) < 20:
            return self._result(item, Status.WARN, "llms.txt 存在但内容过短")
        return self._result(item, Status.PASS, f"llms.txt 存在（{len(content)} 字符）")

    # 5. .well-known 发现文件
    def _check_well_known(self) -> CheckResult:
        item = self._item(
            "tech-05", ".well-known AI 发现文件", Priority.P1,
            "是否存在 AI 可发现的 .well-known 文件",
            "检查 /.well-known/ai-plugin.json、/.well-known/llms-full.txt",
            "至少存在一个 AI 发现文件",
            manual_check="浏览器访问 https://你的域名/.well-known/ai-plugin.json，看是否返回 JSON 格式的插件描述文件",
            purpose=".well-known 目录是 AI 自动发现网站能力的标准入口。ai-plugin.json 让 ChatGPT 等 AI 助手能自动识别和调用你的网站信息",
            excellent="存在 ai-plugin.json，包含完整的 name/description/api/logo/contact 字段，且指向 llms.txt 或 OpenAPI 接口",
            normal="存在 ai-plugin.json 或 llms-full.txt 中的一个，但字段不完整",
            failed="未找到任何 .well-known AI 发现文件",
        )
        found = []
        for path in ["/.well-known/ai-plugin.json", "/.well-known/llms-full.txt",
                     "/.well-known/llms.txt"]:
            if self.fetcher.exists(f"{self.base_url}{path}"):
                found.append(path)
        if found:
            return self._result(item, Status.PASS, f"发现 {len(found)} 个 .well-known 文件",
                                detail=", ".join(found))
        return self._result(item, Status.WARN, "未找到 .well-known AI 发现文件")

    # 6. 页面可索引性
    def _check_indexability(self) -> CheckResult:
        item = self._item(
            "tech-06", "页面可索引性", Priority.P0,
            "首页是否被设置为 noindex/nofollow",
            "检查 meta robots 标签和 X-Robots-Tag 响应头",
            "核心页面无 noindex/nofollow",
            manual_check="右键查看网页源代码，搜索 noindex 或 robots；或用浏览器开发者工具看响应头里有没有 X-Robots-Tag: noindex",
            purpose="noindex 标签会直接告诉 AI 和搜索引擎'不要收录这个页面'。很多站点因为测试时加了 noindex 忘了去掉，导致整站不被收录",
            excellent="所有核心页面均无 noindex/nofollow， robots  meta 标签配置合理，重要页面有明确的索引指令",
            normal="首页可索引，但可能部分内页存在 noindex（如登录页、搜索结果页，这是合理的）",
            failed="首页或核心产品页被设置了 noindex/nofollow，将导致不被 AI 和搜索引擎收录",
        )
        meta_robots = self.page.meta("robots")
        header_robots = self.page.headers.get("X-Robots-Tag", "").lower()

        issues = []
        if meta_robots and "noindex" in meta_robots.lower():
            issues.append("meta robots 含 noindex")
        if meta_robots and "nofollow" in meta_robots.lower():
            issues.append("meta robots 含 nofollow")
        if "noindex" in header_robots:
            issues.append("X-Robots-Tag 含 noindex")

        if issues:
            return self._result(item, Status.FAIL, "页面被禁止索引",
                                detail="; ".join(issues))
        return self._result(item, Status.PASS, "页面允许索引和抓取")

    # 7. 网址规范化
    def _check_canonical(self) -> CheckResult:
        item = self._item(
            "tech-07", "网址规范化（canonical）", Priority.P1,
            "是否使用 canonical 标签指定首选 URL",
            "检查 link rel=canonical 标签",
            "有统一规范，使用 canonical 指向首选 URL",
            manual_check="右键查看网页源代码，搜索 rel=\"canonical\"，看是否有指向当前页面的规范 URL",
            purpose="canonical 告诉 AI 同一个内容的多个 URL（带不带 www、尾斜杠、参数）哪个是正版，避免权重分散和重复内容判定",
            excellent="每页都有 canonical 标签，指向当前页面的规范 URL，与实际 URL 一致，无跨域指向",
            normal="首页有 canonical 标签，但内页可能缺失或指向不够精确",
            failed="未设置 canonical，或 canonical 指向错误页面（如全部指向首页）",
        )
        canonical = self.page.soup.find("link", attrs={"rel": "canonical"})
        if canonical and canonical.get("href"):
            href = canonical["href"]
            if href == self.page.final_url or href == self.page.url:
                return self._result(item, Status.PASS, f"canonical 指向当前页: {href}")
            return self._result(item, Status.WARN, f"canonical 指向其他页面: {href}")
        return self._result(item, Status.WARN, "未设置 canonical 标签")

    # 8. 状态码
    def _check_status_codes(self) -> CheckResult:
        item = self._item(
            "tech-08", "跳转与状态码", Priority.P0,
            "首页返回的 HTTP 状态码是否正常",
            "检查响应状态码",
            "返回 200，无 4xx/5xx 错误",
            manual_check="用浏览器开发者工具 Network 面板看首页请求的状态码；或用在线 HTTP 状态码检测工具",
            purpose="4xx/5xx 状态码意味着 AI 爬虫打不开页面，直接无法收录。3xx 重定向链过长也会降低抓取效率",
            excellent="首页返回 200 OK，所有核心页面均正常，无重定向链，响应头完整",
            normal="首页返回 200，但可能存在少量重定向或个别内页 404",
            failed="首页返回 4xx/5xx 错误，AI 爬虫无法访问",
        )
        code = self.page.status_code
        if code == 200:
            return self._result(item, Status.PASS, "返回 200 OK")
        elif 300 <= code < 400:
            return self._result(item, Status.WARN, f"返回 {code} 重定向")
        elif 400 <= code < 500:
            return self._result(item, Status.FAIL, f"返回 {code} 客户端错误")
        else:
            return self._result(item, Status.FAIL, f"返回 {code} 服务器错误")

    # 9. 内容可访问性
    def _check_content_accessibility(self) -> CheckResult:
        item = self._item(
            "tech-09", "内容可访问性", Priority.P0,
            "核心内容是否在初始 HTML 中，不依赖 JS 渲染",
            "检查 noscript 标签、初始 HTML 文本量",
            "主要文本内容在初始 HTML 中可见",
            manual_check="右键查看网页源代码（不是审查元素），看正文内容是否在 HTML 里；或禁用 JS 后刷新页面看内容是否还在",
            purpose="很多 AI 爬虫不执行 JS，如果你的内容全靠 JS 动态渲染，AI 看到的就是空白页，完全无法理解和引用",
            excellent="核心内容全部在初始 HTML 中，禁用 JS 后页面内容完整，无 noscript 降级提示",
            normal="主要内容在 HTML 中，但部分非关键内容（如评论、推荐）依赖 JS 加载",
            failed="初始 HTML 文本量极少，核心内容依赖 JS 渲染，AI 爬虫无法获取有效内容",
        )
        text = self.page.get_text()
        if len(text) < 100:
            return self._result(
                item, Status.FAIL,
                "初始 HTML 文本量过少，内容可能依赖 JS 动态渲染",
                detail=f"初始文本仅 {len(text)} 字符",
            )
        noscript = self.page.soup.find("noscript")
        if noscript and len(text) < 500:
            return self._result(
                item, Status.WARN,
                "检测到 noscript 标签，部分内容可能需要 JS 才能显示",
            )
        return self._result(item, Status.PASS, f"初始 HTML 包含 {len(text)} 字符文本")

    # 10. 页面体积与速度
    def _check_page_size_speed(self) -> CheckResult:
        item = self._item(
            "tech-10", "页面体积与加载速度", Priority.P1,
            "页面大小和响应时间是否合理",
            "测量 HTML 大小和响应时间",
            "HTML < 500KB，首屏 < 3s",
            manual_check="用浏览器开发者工具 Network 面板看文档加载时间和大小；或用 PageSpeed Insights / 站长工具测速",
            purpose="页面过大或加载过慢会降低 AI 爬虫的抓取效率和用户体验，搜索引擎也会把速度作为排名因素",
            excellent="HTML < 200KB，首屏加载 < 1.5s，图片已压缩，启用了 Gzip/Brotli 压缩和 CDN",
            normal="HTML < 500KB，首屏 < 3s，基本达标但有优化空间",
            failed="HTML > 500KB 或首屏 > 3s，严重影响抓取和体验",
        )
        size_kb = self.page.content_size / 1024
        time_s = self.page.response_time

        issues = []
        if size_kb > 500:
            issues.append(f"页面体积 {size_kb:.0f}KB 超过 500KB")
        if time_s > 3:
            issues.append(f"响应时间 {time_s:.1f}s 超过 3s")

        if issues:
            return self._result(item, Status.WARN, "页面体积或速度有待优化",
                                detail="; ".join(issues))
        return self._result(item, Status.PASS, f"页面 {size_kb:.0f}KB，响应 {time_s:.2f}s")

    # 11. 移动端友好
    def _check_mobile_friendly(self) -> CheckResult:
        item = self._item(
            "tech-11", "移动端友好", Priority.P1,
            "是否有 viewport 设置，基础移动端适配",
            "检查 meta viewport 标签",
            "有 viewport 设置，无明显移动端问题",
            manual_check="右键查看源代码搜索 viewport；或用浏览器手机模式打开网站，看是否有横向滚动、字体过小、按钮重叠",
            purpose="超过 70% 的 AI 搜索和传统搜索来自移动端，没有移动端适配的站点会被 AI 和搜索引擎降权",
            excellent="有 viewport 设置，响应式布局，移动端无横向滚动，字体 ≥14px，点击区域 ≥44px，通过 Google 移动端友好测试",
            normal="有 viewport 设置，基本可用，但可能存在字体偏小或间距问题",
            failed="未设置 viewport meta 标签，或移动端布局严重错乱",
        )
        viewport = self.page.meta("viewport")
        if not viewport:
            return self._result(item, Status.FAIL, "未设置 viewport meta 标签")
        if "width=device-width" in viewport.lower():
            return self._result(item, Status.PASS, "已设置响应式 viewport")
        return self._result(item, Status.WARN, f"viewport 设置异常: {viewport}")

    # 12. URL Slug 可读性
    def _check_url_slug(self) -> CheckResult:
        item = self._item(
            "tech-12", "URL结构与可读性", Priority.P1,
            "URL 路径是否简短可读、含关键词、用连字符分隔",
            "解析各子页面URL路径，检查动态参数、纯数字路径、长度",
            "路径简短可读，含关键词，用连字符分隔，无动态参数",
            manual_check="查看各子页面浏览器地址栏的URL路径，是 /products/geo-optimization 这种可读形式，还是 /p?id=123 或 /12345.html 这种动态形式",
            purpose="URL Slug 是 AI 和搜索引擎理解页面主题的辅助信号。首页URL是域名无需检查，需检查各子页面路径质量",
            excellent="所有子页面路径简短（<3层目录），含描述性关键词，用连字符分隔，全小写，无动态参数",
            normal="大部分路径可读，但个别页面偏长或含少量动态参数",
            failed="多个页面URL含动态参数（?id=、?p=），或纯数字路径，或路径过深（>5层）",
        )
        import re
        from urllib.parse import urlparse, parse_qs

        # 检查所有子页面的URL
        pages_to_check = self.extra_pages if self.extra_pages else []
        if not pages_to_check:
            return self._result(item, Status.SKIP,
                                "无抽样内页，无法检查URL Slug",
                                detail="需爬取子页面后检查URL路径质量，首页URL为域名无需检查")

        all_issues = []
        good_count = 0
        for p in pages_to_check:
            parsed = urlparse(p.final_url)
            path = parsed.path
            query = parsed.query
            page_issues = []

            if query:
                params = parse_qs(query)
                meaningful = [k for k in params if k.lower() not in ["utm_source", "utm_medium", "utm_campaign", "gclid", "fbclid"]]
                if meaningful:
                    page_issues.append(f"动态参数({query[:30]})")

            segments = [s for s in path.split("/") if s]
            if not segments:
                good_count += 1
                continue

            numeric_segments = [s for s in segments if re.match(r'^\d+$', s)]
            if numeric_segments:
                page_issues.append(f"纯数字路径({', '.join(numeric_segments)})")

            if len(segments) > 4:
                page_issues.append(f"路径过深({len(segments)}层)")

            if len(path) > 80:
                page_issues.append(f"路径过长({len(path)}字符)")

            if "_" in path:
                page_issues.append("含下划线(建议用连字符)")

            if page_issues:
                all_issues.append(f"{path}: {'; '.join(page_issues)}")
            else:
                good_count += 1

        total = len(pages_to_check)
        if not all_issues:
            return self._result(item, Status.PASS,
                                f"所有{total}个子页面URL结构良好",
                                detail=f"已检查{total}个内页路径，均简短可读")
        if len(all_issues) <= total * 0.3:
            return self._result(item, Status.WARN,
                                f"URL结构有改进空间（{good_count}/{total}良好）",
                                detail="；".join(all_issues[:5]))
        return self._result(item, Status.FAIL,
                            f"URL结构可读性差（仅{good_count}/{total}良好）",
                            detail="；".join(all_issues[:5]))

    # 14. CMS指纹识别
    def _check_cms_fingerprint(self) -> CheckResult:
        item = self._item(
            "tech-14", "CMS指纹识别", Priority.P1,
            "识别目标网站使用的建站系统/CMS类型",
            "通过特征文件、meta标签、cookie、目录结构等识别CMS",
            "识别出CMS类型，评估技术栈是否适合接手整改",
            manual_check="查看页面源代码中的generator标签、特征文件路径(如/wp-login.php、/dede/)、cookie名称等",
            purpose="识别CMS类型可判断技术栈是否适合接手整改，是否能灵活修改URL/结构/内容，以及是否存在已知安全风险",
            excellent="识别为常见CMS（WordPress/帝国等），技术栈成熟，二次开发友好，可灵活整改",
            normal="识别为SaaS建站平台（凡科/建站ABC等），整改受平台限制，需评估是否能获取编辑权限",
            failed="无法识别CMS类型（可能为全定制开发），需进一步排查技术栈",
        )
        from urllib.parse import urljoin
        import httpx

        soup = self.page.soup
        html = self.page.html
        base_url = self.page.final_url

        detected = []
        confidence = "低"

        # --- 1. Meta generator 标签 ---
        gen_tag = soup.find("meta", attrs={"name": "generator"})
        if gen_tag:
            gen_content = gen_tag.get("content", "").lower()
            if "wordpress" in gen_content:
                detected.append({"cms": "WordPress", "evidence": f'meta generator: {gen_tag.get("content","")}'})
                confidence = "高"
            elif "dedecms" in gen_content or "织梦" in gen_content:
                detected.append({"cms": "DedeCMS(织梦)", "evidence": f'meta generator: {gen_tag.get("content","")}'})
                confidence = "高"
            elif "drupal" in gen_content:
                detected.append({"cms": "Drupal", "evidence": f'meta generator: {gen_tag.get("content","")}'})
                confidence = "高"
            elif "joomla" in gen_content:
                detected.append({"cms": "Joomla", "evidence": f'meta generator: {gen_tag.get("content","")}'})
                confidence = "高"
            elif "discuz" in gen_content:
                detected.append({"cms": "Discuz!", "evidence": f'meta generator: {gen_tag.get("content","")}'})
                confidence = "高"
            elif "empirecms" in gen_content or "帝国" in gen_content:
                detected.append({"cms": "EmpireCMS(帝国)", "evidence": f'meta generator: {gen_tag.get("content","")}'})
                confidence = "高"

        # --- 2. 特征路径探测（HEAD请求，不下载内容）---
        fingerprint_paths = {
            "WordPress": ["/wp-login.php", "/wp-admin/", "/wp-content/", "/wp-includes/"],
            "DedeCMS(织梦)": ["/dede/", "/plus/", "/templets/", "/data/"],
            "EmpireCMS(帝国)": ["/e/admin/", "/e/class/", "/e/install/"],
            "Discuz!": ["/uc_server/", "/forum.php", "/portal.php"],
            "Drupal": ["/user/login", "/sites/default/"],
            "Joomla": ["/administrator/", "/components/"],
            "PhpCms": ["/phpcms/", "/install/"],
        }

        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

        for cms_name, paths in fingerprint_paths.items():
            if any(d["cms"] == cms_name for d in detected):
                continue
            matched = False
            for path in paths[:2]:
                try:
                    check_url = urljoin(base_url, path)
                    with httpx.Client(timeout=8, follow_redirects=False, headers=headers) as client:
                        resp = client.head(check_url)
                        if resp.status_code in (200, 301, 302, 403):
                            detected.append({"cms": cms_name, "evidence": f"{path} → HTTP {resp.status_code}"})
                            matched = True
                            confidence = "高"
                            break
                except Exception:
                    pass
            if matched:
                break

        # --- 3. HTML特征模式 ---
        html_lower = html.lower()
        html_patterns = {
            "凡科(Faishe)": ["/_nvs/", "faishe", "fksite", "fanke"],
            "建站ABC": ["jzabc", "建站abc"],
            "Wix": ["wix.com", "static.wixstatic"],
            "Shopify": ["cdn.shopify.com", "shopify.theme"],
            "Squarespace": ["squarespace", "static1.squarespace"],
            "Weebly": ["weebly", "wsite-"],
            "WordPress.com": ["wp.com", "wordpress.com"],
            "Hugo": ["hugo-theme"],
            "Vue/React SPA": ["__next_data__", "data-reactroot", "id=\"app\"", "id=\"__nuxt\""],
        }
        for cms_name, patterns in html_patterns.items():
            if any(d["cms"] == cms_name for d in detected):
                continue
            for pat in patterns:
                if pat in html_lower:
                    detected.append({"cms": cms_name, "evidence": f"HTML特征: {pat}"})
                    confidence = "中"
                    break

        # --- 4. Cookie特征 ---
        cookies = self.page.status_code  # placeholder, actual cookies handled below
        try:
            with httpx.Client(timeout=8, follow_redirects=True, headers=headers) as client:
                resp = client.get(base_url)
                cookie_names = set(resp.cookies.keys())
                cookie_map = {
                    "WordPress": ["wordpress_logged_in", "wp-settings", "wp-postpass"],
                    "DedeCMS(织梦)": ["dede_user_id", "dede_csrf"],
                    "Discuz!": ["*_sid", "*_auth", "ucid"],
                    "EmpireCMS(帝国)": ["ecmscookie"],
                }
                for cms_name, cookie_pats in cookie_map.items():
                    if any(d["cms"] == cms_name for d in detected):
                        continue
                    for pat in cookie_pats:
                        if pat.startswith("*"):
                            suffix = pat[1:]
                            if any(c.endswith(suffix) for c in cookie_names):
                                detected.append({"cms": cms_name, "evidence": f"Cookie: {pat}"})
                                confidence = "中"
                                break
                        elif pat in cookie_names:
                            detected.append({"cms": cms_name, "evidence": f"Cookie: {pat}"})
                            confidence = "中"
                            break
        except Exception:
            pass

        # --- 综合判断 ---
        if detected:
            cms_list = [d["cms"] for d in detected]
            evidence_list = [f"{d['cms']}: {d['evidence']}" for d in detected]
            # 判断是否SaaS建站（整改受限）
            saas_platforms = ["凡科(Faishe)", "建站ABC", "Wix", "Shopify", "Squarespace", "Weebly", "WordPress.com"]
            is_saas = any(d["cms"] in saas_platforms for d in detected)

            if is_saas:
                status = Status.WARN
                msg = f"识别为SaaS建站平台: {', '.join(cms_list)}（整改可能受平台限制）"
            else:
                status = Status.PASS
                msg = f"识别为: {', '.join(cms_list)}（技术栈可接手整改）"

            suitability = self._cms_suitability(cms_list[0])
            return self._result(item, status, msg,
                                detail=f"置信度: {confidence}。证据: {'; '.join(evidence_list[:3])}。接手评估: {suitability}")
        else:
            # 无法识别，检查是否为静态站或全定制
            is_static = "/static/" in html_lower or "/assets/" in html_lower
            has_framework = any(fw in html_lower for fw in ["vue", "react", "angular", "next.js", "nuxt"])
            if has_framework:
                return self._result(item, Status.WARN, "疑似前端框架定制开发(Vue/React/Angular)",
                                    detail="无法确定后端CMS，可能为全定制开发，需进一步排查技术栈")
            return self._result(item, Status.WARN, "未能识别CMS类型（可能为全定制开发）",
                                detail="无meta generator标签、无特征路径、无HTML特征匹配。建议人工排查技术栈")

    def _cms_suitability(self, cms_name: str) -> str:
        """评估CMS接手整改的适合度"""
        suitability_map = {
            "WordPress": "成熟CMS，插件生态丰富，二次开发友好，适合接手整改",
            "DedeCMS(织梦)": "老牌CMS，源码开放，模板灵活，但安全漏洞多，接手后需重点加固",
            "EmpireCMS(帝国)": "开源CMS，扩展性强，适合接手整改",
            "Discuz!": "论坛型CMS，适合社区类站点，非论坛站不太适用",
            "Drupal": "企业级CMS，灵活度高但学习曲线陡",
            "Joomla": "中型CMS，扩展性好，适合接手整改",
        }
        saas_map = {
            "凡科(Faishe)": "SaaS平台，URL和结构受平台限制，需确认能否编辑HTML/上传文件",
            "建站ABC": "SaaS平台，整改受限，建议评估是否有源码导出功能",
            "Wix": "SaaS平台，封闭生态，整改严重受限",
            "Shopify": "电商SaaS，适合电商站，但非电商功能受限",
            "WordPress.com": "SaaS版WordPress，比自托管受限，需确认是否支持插件安装",
        }
        if cms_name in suitability_map:
            return suitability_map[cms_name]
        if cms_name in saas_map:
            return saas_map[cms_name]
        return "需进一步评估技术栈和可编辑性"

    # 15. 后台路径暴露
    def _check_admin_exposure(self) -> CheckResult:
        item = self._item(
            "tech-15", "后台路径暴露检测", Priority.P1,
            "检测常见后台路径是否可公开访问",
            "扫描常见CMS后台路径(/admin, /dede, /wp-admin等)，检查HTTP状态码",
            "后台路径不可公开访问，或需要认证才能访问",
            manual_check="在浏览器中输入域名+/admin、/dede、/wp-admin等路径，看是否能直接打开后台登录页",
            purpose="后台路径暴露存在被暴力破解或扫描攻击的风险。接手整改前需评估后台安全性",
            excellent="常见后台路径均返回404或不可访问，后台有IP限制或二次验证",
            normal="后台路径返回403禁止访问，或登录页有验证码/频率限制",
            failed="后台登录页可公开访问，无验证码/频率限制，存在被暴力破解风险",
        )
        from urllib.parse import urljoin
        import httpx

        base_url = self.page.final_url
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

        # 按CMS类型分组的后台路径
        admin_paths = [
            # WordPress
            ("WordPress后台", "/wp-admin/"),
            ("WordPress登录", "/wp-login.php"),
            # DedeCMS
            ("织梦后台", "/dede/"),
            # EmpireCMS
            ("帝国后台", "/e/admin/"),
            # 通用
            ("通用后台", "/admin/"),
            ("通用登录", "/login"),
            ("通用管理", "/manage/"),
            ("PHP后台", "/system/"),
            ("ASP后台", "/admin/login"),
            ("管理员", "/administrator/"),
            # 其他
            ("Drupal后台", "/user/login"),
            ("Joomla后台", "/administrator/"),
        ]

        exposed = []
        forbidden = []
        not_found = []

        for label, path in admin_paths:
            try:
                check_url = urljoin(base_url, path)
                with httpx.Client(timeout=6, follow_redirects=True, headers=headers) as client:
                    resp = client.get(check_url)
                    status_code = resp.status_code
                    content_len = len(resp.content)

                    if status_code == 200 and content_len > 200:
                        # 检查是否是登录页（含form/password字段）
                        text_lower = resp.text.lower()
                        is_login_page = any(kw in text_lower for kw in
                            ["password", "passwd", "登录", "密码", "login", "log in", "username", "用户名"])
                        if is_login_page:
                            has_captcha = any(kw in text_lower for kw in
                                ["captcha", "验证码", "geetest", "recaptcha", "vcode"])
                            if has_captcha:
                                exposed.append(f"{label}({path}): 可访问，有验证码")
                            else:
                                exposed.append(f"{label}({path}): 可访问，无验证码")
                        else:
                            # 200但非登录页，可能是首页跳转
                            not_found.append(f"{label}({path}): 跳转到非登录页")
                    elif status_code in (401, 403):
                        forbidden.append(f"{label}({path}): {status_code}禁止访问")
                    elif status_code == 404:
                        not_found.append(f"{label}({path}): 404不存在")
                    elif status_code in (301, 302):
                        not_found.append(f"{label}({path}): 重定向")
                    else:
                        not_found.append(f"{label}({path}): {status_code}")
            except Exception:
                not_found.append(f"{label}({path}): 请求失败")

        if exposed:
            no_captcha = [e for e in exposed if "无验证码" in e]
            if no_captcha:
                return self._result(item, Status.FAIL,
                                    f"发现{len(exposed)}个后台路径暴露（{len(no_captcha)}个无验证码）",
                                    detail="暴露: " + "; ".join(exposed[:5]))
            return self._result(item, Status.WARN,
                                f"发现{len(exposed)}个后台路径可访问（均有验证码）",
                                detail="暴露: " + "; ".join(exposed[:5]))
        elif forbidden:
            return self._result(item, Status.PASS, "后台路径存在但已禁止访问",
                                detail="禁止: " + "; ".join(forbidden[:3]))
        else:
            return self._result(item, Status.PASS, "未发现暴露的后台路径",
                                detail=f"扫描了{len(admin_paths)}个常见路径，均为404或重定向")
