"""技术可抓取性检查（11项）"""
import re
from typing import List
from urllib.parse import urlparse, urljoin
from .checker import BaseChecker
from site_audit.core.models import CheckResult, Status, Priority, Category
from site_audit.core.fetcher import PageData
from site_audit.core.crawler import SiteCrawler


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
        results.append(self._check_head_get())
        results.append(self._check_open_graph())
        results.append(self._check_session_cache())
        results.append(self._check_security_headers())
        results.append(self._check_host_canonicalization())
        results.append(self._check_cms_fingerprint())
        results.append(self._check_admin_exposure())
        results.append(self._check_search_index())
        results.append(self._check_pagination())
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
            manual_check="浏览器打开 /robots.txt，按 RFC 9309 语义检查（支持通配符）：① A 组检索型爬虫任一被封→不合格。A组=搜索引擎（Baiduspider/360Spider/搜狗/神马/头条/Googlebot/Bingbot/Applebot）+ AI检索（Bytespider/OAI-SearchBot/PerplexityBot/KimiBot/ChatGPT-User/Perplexity-User/Claude-User）；② B 组训练型（GPTBot/ClaudeBot/Google-Extended/Cohere-AI）封禁属商业选择，不判级；③ 文末有 Sitemap: 行→sitemap 引用子项合格（sitemap 文件本身的质量由「站点地图」检查项单独查）",
            purpose="robots.txt 决定 AI 爬虫能不能抓你的站。另外，在robots.txt中引用sitemap，能让搜索引擎和AI爬虫自动发现你的sitemap，提升收录效率",
            excellent="robots.txt 存在，明确允许所有主流 AI 爬虫，引用了 sitemap 地址，规则清晰无歧义",
            normal="robots.txt 存在，未封禁主流 AI 爬虫，但未引用 sitemap 地址",
            failed="robots.txt 不存在，或封禁了主流 AI 爬虫",
        )
        resp = self.fetcher.fetch_raw(f"{self.base_url}/robots.txt")
        if resp is None or resp.status_code != 200:
            return self._result(item, Status.WARN, "未找到 robots.txt（默认允许所有爬虫）")

        # 2026-09-10 V6：按 RFC 9309 语义解析（Allow/Disallow、* 通配、$ 锚定、最长匹配），
        # 名单配置化（config/ai_crawlers.yaml，季度复核）。A组检索型（搜索引擎+AI检索）
        # 任一被封→不合格；B组训练型封禁=商业选择，不判级不出报告。
        groups = self._load_crawler_groups()
        rules = self._parse_robots_rules(resp.text)
        group_a = groups.get("search_engine", []) + groups.get("ai_retrieval", [])
        blocked_a = [b for b in group_a if self._bot_blocked(rules, b)]
        scope_note = "UA 可伪造，本结果为初判；robots.txt 只约束声明遵守的爬虫"
        if blocked_a:
            return self._result(
                item, Status.FAIL,
                f"robots.txt 封禁了 {len(blocked_a)} 个检索型爬虫（封禁=阻断收录或AI引用资格）",
                detail=f"被封禁: {', '.join(blocked_a)}；{scope_note}",
            )
        has_sitemap_ref = "sitemap:" in resp.text.lower()
        if has_sitemap_ref:
            return self._result(item, Status.PASS, "robots.txt 存在，检索型爬虫未被封禁，已引用 sitemap",
                                detail=scope_note)
        return self._result(item, Status.WARN, "robots.txt 存在，未封禁检索型爬虫，但未引用 sitemap 地址",
                            detail=scope_note)

    @staticmethod
    def _load_crawler_groups() -> dict:
        """爬虫分组名单（配置文件化，季度复核只改 yaml）。"""
        import os
        import yaml
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "config", "ai_crawlers.yaml")
        try:
            with open(path, encoding="utf-8") as f:
                return yaml.safe_load(f).get("groups", {})
        except Exception:
            return {}

    @staticmethod
    def _parse_robots_rules(text: str) -> dict:
        """解析 robots.txt → {ua_token小写: {"allow": [...], "disallow": [...]}}。"""
        rules = {}
        uas = []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key == "user-agent":
                uas.append(val.lower())
            elif key in ("allow", "disallow") and uas:
                for ua in uas:
                    bucket = rules.setdefault(ua, {"allow": [], "disallow": []})
                    if val:
                        bucket[key].append(val)
        return rules

    @staticmethod
    def _robots_rule_matches(pattern: str, path: str) -> bool:
        if not pattern:
            return False
        regex = "^" + re.escape(pattern).replace(r"\*", ".*")
        if pattern.endswith("$"):
            regex = regex[:-2] + "$"  # 去掉转义尾巴的 \$
        return re.match(regex, path) is not None

    @classmethod
    def _bot_blocked(cls, rules: dict, bot: str) -> bool:
        """bot 抓取根路径 / 是否被 Disallow：该 bot 所属组内 Allow/Disallow 最长匹配，同长 Allow 优先。"""
        allow, disallow = [], []
        bot_l = bot.lower()
        for ua, r in rules.items():
            if ua in bot_l or bot_l in ua:
                allow += r.get("allow", [])
                disallow += r.get("disallow", [])
        best = ("", -1)
        for p in allow:
            if cls._robots_rule_matches(p, "/") and len(p) > best[1]:
                best = ("allow", len(p))
        for p in disallow:
            if cls._robots_rule_matches(p, "/") and len(p) >= best[1]:
                best = ("disallow", len(p))
        return best[0] == "disallow"

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
        # 站点地图地址：优先用 robots.txt 里声明的，其次常见路径（2026-09-05 升级为内容质量检查）
        sm_declared = None
        rbt = self.fetcher.fetch_raw(f"{self.base_url}/robots.txt")
        if rbt is not None and rbt.status_code == 200:
            msm = re.search(r"(?im)^\s*Sitemap:\s*(\S+)", rbt.text)
            if msm:
                sm_declared = msm.group(1).strip()
        candidates = ([sm_declared] if sm_declared else []) + [
            f"{self.base_url}/sitemap.xml", f"{self.base_url}/sitemap_index.xml",
            f"{self.base_url}/sitemap.php", f"{self.base_url}/sitemap"]
        resp = None
        for u in candidates:
            if not u:
                continue
            rr = self.fetcher.fetch_raw(u)
            if rr is not None and rr.status_code == 200:
                resp = rr
                break
        if resp is None:
            return self._result(item, Status.FAIL, "未找到 sitemap.xml")

        content = resp.text

        def _parse(xml):
            out = []
            for block in re.findall(r"<url>(.*?)</url>", xml, re.S | re.I):
                loc = re.search(r"<loc>(.*?)</loc>", block, re.S | re.I)
                lm = re.search(r"<lastmod>(.*?)</lastmod>", block, re.S | re.I)
                cf = re.search(r"<changefreq>(.*?)</changefreq>", block, re.S | re.I)
                if loc:
                    out.append((loc.group(1).strip(),
                                (lm.group(1).strip()[:10] if lm else ""),
                                (cf.group(1).strip().lower() if cf else "")))
            return out

        entries = _parse(content)
        sub_maps = re.findall(r"<sitemap>.*?<loc>(.*?)</loc>", content, re.S | re.I)
        if sub_maps and not entries:  # 一层 sitemap-index
            for su in sub_maps[:5]:
                sr = self.fetcher.fetch_raw(su)
                if sr is not None and sr.status_code == 200:
                    entries += _parse(sr.text)
        if not entries:  # 兜底：至少提取 loc
            raw_locs = re.findall(r"<loc>(.*?)</loc>", content, re.S | re.I)
            if not raw_locs:
                return self._result(item, Status.WARN, "sitemap 存在但未包含任何 URL")
            entries = [(x.strip(), "", "") for x in raw_locs]

        url_count = len(entries)
        locs = [e[0] for e in entries]
        cur_host = urlparse(self.page.final_url).netloc.lower()

        def host_of(u):
            return urlparse(u).netloc.lower()

        def path_of(u):
            return (urlparse(u).path.lower().rstrip("/") or "/")

        hard, soft = [], []

        # 1) 主机是否统一到当前首选域名
        bad_host = [u for u in locs if host_of(u) and host_of(u) != cur_host]
        if bad_host and len(bad_host) / url_count > 0.5:
            hard.append(f"{len(bad_host)}/{url_count} 个网址用的是「{host_of(bad_host[0])}」，"
                        f"和当前首选域名「{cur_host}」不一致，会把爬虫引到另一套主机、分散权重")
        # 2) 是否大量带 index.php 入口段
        messy = [u for u in locs if "/index.php" in u.lower()]
        if messy and len(messy) / url_count > 0.5:
            hard.append(f"{len(messy)}/{url_count} 个网址带 index.php 中间路径（伪静态短链没有用上）")
        # 3) 站内可达的详情页是否被漏收（按路径比对）
        sm_paths = {path_of(u) for u in locs}
        missing = [path_of(p.final_url) for p in self.all_pages
                   if (SiteCrawler._is_detail(p.final_url)
                       or SiteCrawler.classify(p.final_url, "") == "faq")
                   and path_of(p.final_url) not in sm_paths]
        if missing:
            hard.append(f"{len(missing)} 个站内可达的重要页面（详情页/FAQ页）没进站点地图（如 {missing[0]}），"
                        "这类高价值页只能靠内链被偶然发现，FAQ 页为高发遗漏区")
        # 3.5) CMS 默认演示/示例内容仍在线（C4/K3：口径="从 sitemap 移除，并删除或 noindex 二选一"）
        demo_paths = [path_of(u) for u in locs
                      if re.search(r"/(hello-world|sample-page|hello-world-2|test-page|uncategorized)/?$",
                                   path_of(u))]
        if demo_paths:
            soft.append(f"站点地图仍含 CMS 默认演示内容（如 {demo_paths[0]}），"
                        "应先从 sitemap 移除，再将页面删除或设为 noindex（二选一）——演示文章会稀释专业度并可能被 AI 检索到")
        # 3.6) C4：被 noindex 的页面不得留在 sitemap
        noindexed_in_map = []
        for p in self.all_pages:
            if path_of(p.final_url) not in sm_paths:
                continue
            meta_robots = (p.soup.find("meta", attrs={"name": "robots"}) or {}).get("content", "") if p.soup else ""
            x_robots = (p.headers or {}).get("X-Robots-Tag", "")
            if "noindex" in (meta_robots + " " + x_robots).lower():
                noindexed_in_map.append(path_of(p.final_url))
        if noindexed_in_map:
            hard.append(f"{len(noindexed_in_map)} 个被 noindex 的页面仍留在 sitemap 中（如 {noindexed_in_map[0]}），"
                        "noindex 与 sitemap 互相矛盾，应从 sitemap 移除")
        # F2：lastmod 等于检测当天 = 疑似抓取时间冒充更新时间
        import datetime as _dt
        today = _dt.date.today().isoformat()
        fake_lm = [e[0] for e in entries if e[1] == today]
        if len(fake_lm) >= 3:
            soft.append(f"{len(fake_lm)} 个网址的 lastmod 是检测当天（{today}），疑似用抓取时间冒充更新时间（会被搜索引擎忽略）")

        # 4) lastmod 异常集中（轻提示）
        lms = [e[1] for e in entries if e[1]]
        if len(lms) > 10:
            from collections import Counter
            top_day, top_n = Counter(lms).most_common(1)[0]
            if top_n / len(lms) > 0.8:
                soft.append(f"更新时间有 {top_n}/{len(lms)} 集中在同一天（{top_day}），不像是各页真实更新时间")
        # 5) changefreq 滥用（引擎基本忽略该字段，仅建议、不重罚）
        aggressive = [e for e in entries if e[2] in ("always", "hourly")]
        if aggressive and len(aggressive) / url_count > 0.5:
            soft.append(f"{len(aggressive)}/{url_count} 标注 always/hourly，更新频率标注过激（搜索引擎基本忽略此字段，建议 weekly/monthly）")

        detail = (["需修正：" + "；".join(hard)] if hard else []) + \
                 (["可优化：" + "；".join(soft)] if soft else []) + \
                 [f"地图共 {url_count} 个网址，基准域名 {cur_host}"]
        if hard:
            return self._result(item, Status.WARN,
                                f"站点地图可用但有 {len(hard)} 类实质问题（共 {url_count} 个网址）",
                                detail="；".join(detail))
        if soft:
            return self._result(item, Status.PASS,
                                f"站点地图可用（{url_count} 个网址、主机统一、核心页已收录），另有细节可优化",
                                detail="；".join(detail))
        return self._result(item, Status.PASS,
                            f"站点地图规范，共 {url_count} 个网址，主机统一且核心页已收录")

    # 4. llms.txt
    def _check_llms_txt(self) -> CheckResult:
        item = self._item(
            "tech-04", "llms.txt 大模型说明文件（前瞻可选）", Priority.P2,
            "是否存在 llms.txt 为大模型提供站点说明（非必须，属前瞻性布局）",
            "访问 /llms.txt，检查内容有效性",
            "存在则更好，缺失不扣分（目前无主流搜索引擎/AI 引擎强制要求）",
            manual_check="浏览器访问 https://你的域名/llms.txt，看是否有纯文本的站点介绍、核心页面链接列表和更新时间",
            purpose="llms.txt 是面向大模型的'站点说明书'提案，目前尚无主流搜索引擎或 AI 引擎明确背书、也不影响收录与排名，因此只作前瞻建议、不计分；配置得当可能方便部分 AI 抓取器快速理解站点",
            excellent="llms.txt 存在，包含清晰的站点简介、分类核心页面链接、服务说明、联系方式和更新时间",
            normal="llms.txt 存在但内容较简单或更新时间缺失",
            failed="未配置 llms.txt（非必须，不影响评分）",
            tier="bonus",
        )
        resp = self.fetcher.fetch_raw(f"{self.base_url}/llms.txt")
        if resp is None or resp.status_code != 200:
            return self._result(item, Status.WARN, "未配置 llms.txt（前瞻可选项，不计分）")

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
            tier="bonus",
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
        # 2026-09-10 V6：canonical 五项机检（任一命中→不合格）
        # ①href空/解析失败/模板占位符 ②指向站外(注册域≠本站) ③目标404/410/5xx
        # ④目标3xx且落点≠声明值 ⑤目标页含noindex。注册域用内置公共后缀清单（eTLD+1）。
        failures = []
        fetch_cache = {}
        for p in self.all_pages:
            if not p.soup:
                continue
            c = p.soup.find("link", attrs={"rel": "canonical"})
            if not c:
                continue
            href = (c.get("href") or "").strip()
            path_p = urlparse(p.final_url).path or "/"
            if not href or re.search(r"\{\{|\{%|%s", href):
                failures.append(f"{path_p}：canonical 为空/解析失败/含模板占位符")
                continue
            target = urljoin(p.final_url, href)
            if self._registered_domain(urlparse(target).netloc) != self._registered_domain(urlparse(p.final_url).netloc):
                failures.append(f"{path_p}：canonical 指向站外（{urlparse(target).netloc}）")
                continue
            if target not in fetch_cache:
                fetch_cache[target] = self.fetcher.fetch(target) if self.fetcher else None
            fp = fetch_cache[target]
            if fp is None:
                continue  # 目标请求失败不武断判级
            if fp.status_code in (404, 410) or fp.status_code >= 500:
                failures.append(f"{path_p}：canonical 目标返回 {fp.status_code}（{target}）")
                continue
            if fp.final_url.rstrip("/") != target.rstrip("/"):
                failures.append(f"{path_p}：canonical 声明 {target}，实际落在 {fp.final_url}")
                continue
            meta_r = ""
            if fp.soup:
                tag = fp.soup.find("meta", attrs={"name": "robots"})
                meta_r = (tag or {}).get("content", "") if tag else ""
            x_r = (fp.headers or {}).get("X-Robots-Tag", "")
            if "noindex" in (meta_r + " " + x_r).lower():
                failures.append(f"{path_p}：canonical 目标页含 noindex（{target}）")
        if failures:
            return self._result(item, Status.FAIL,
                                f"{len(failures)} 个页面的 canonical 存在严重错误（指向站外/空值/死链/被noindex），等同告诉搜索引擎'别收录本页'",
                                detail="；".join(failures[:6]),
                                pages=[p.final_url for p in self.all_pages])
        # C2：全站自指校验——canonical 指向其他页面的页面逐个列出
        mismatch = []
        for p in self.all_pages:
            c = p.soup.find("link", attrs={"rel": "canonical"}) if p.soup else None
            if c and c.get("href"):
                target = urljoin(p.final_url, c["href"])
                if target.rstrip("/") != p.final_url.rstrip("/"):
                    mismatch.append(f"{urlparse(p.final_url).path or '/'} → {target}")
        if mismatch:
            return self._result(item, Status.WARN,
                                f"{len(mismatch)} 个页面 canonical 指向其他页面（需确认是否有意归一）",
                                detail="；".join(mismatch[:6]),
                                pages=[p.final_url for p in self.all_pages
                                       if (urlparse(p.final_url).path or "/") in
                                       {m.split(' → ')[0] for m in mismatch}])
        canonical = self.local_soup.find("link", attrs={"rel": "canonical"})
        if canonical and canonical.get("href"):
            href = canonical["href"]
            if href == self.page.final_url or href == self.page.url:
                return self._result(item, Status.PASS, f"canonical 指向当前页: {href}",
                                    pages=[p.final_url for p in self.all_pages])
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
        text = self.get_text()
        note = "本次未检测 JS 依赖（渲染对比能力下期上线）"
        if len(text) < 100:
            return self._result(
                item, Status.FAIL,
                "初始 HTML 纯文本 <100 字符，爬虫视同空壳，不收录不引用",
                detail=f"初始文本仅 {len(text)} 字符。{note}",
            )
        if len(text) < 500:
            extra = "检测到 noscript 降级标签；" if self.local_soup.find("noscript") else ""
            return self._result(
                item, Status.WARN,
                f"服务端正文偏少（{len(text)} 字符）",
                detail=f"{extra}{note}",
            )
        return self._result(item, Status.PASS, f"初始 HTML 包含 {len(text)} 字符文本",
                            detail=note)

    # 10. 页面体积与速度
    def _check_page_size_speed(self) -> CheckResult:
        item = self._item(
            "tech-10", "页面响应速度与体积", Priority.P1,
            "服务器返回 HTML 文档的快慢与文档大小（首页+抽样内页一起看），以及是否开启传输压缩",
            "统计首页和每个抽样内页的文档响应时间、HTML 体积的中位数与最慢页，并检查响应头 Content-Encoding",
            "文档响应中位数 <0.8s、HTML 中位数 <300KB 为优；>2s 或 >800KB 为差；需开启 gzip/br 压缩",
            manual_check="本项是'服务器文档响应'（HTML 下载）耗时；真实用户首屏 LCP/INP/CLS 需用 PageSpeed Insights、WebPageTest 或浏览器 Network/Performance 面板复测",
            purpose="文档响应慢、体积大会拖慢 AI 爬虫与搜索引擎抓取（慢页面抓取配额更低），用户也更易跳出；开压缩能显著减少传输字节",
            excellent="首页与多数内页文档响应 <0.8s、HTML<300KB，已开启 gzip/br 压缩",
            normal="文档响应 0.8-2s 或 HTML 300-800KB，或个别内页偏慢，基本可用但有优化空间",
            failed="文档响应中位数 >2s 或 HTML 中位数 >800KB，明显拖累抓取与体验",
            scope="全站", applicable_pages="首页+内页",
            suggestion="开启 gzip/Brotli 压缩与 CDN、给静态资源长缓存、压缩图片与精简内联脚本样式",
        )
        from statistics import median
        pages = [self.page] + (self.extra_pages or [])
        rows = []
        for p in pages:
            label = "首页" if p is self.page else self._path_of(p.final_url)
            rows.append((label, float(p.response_time or 0.0), p.content_size / 1024))
        times = [r[1] for r in rows]
        kbs = [r[2] for r in rows]
        med_t, med_kb = median(times), median(kbs)
        slowest = sorted(rows, key=lambda r: r[1], reverse=True)[:2]
        enc = (self.page.headers or {}).get("Content-Encoding", "") or ""
        compressed = bool(re.search(r"gzip|br|deflate", enc, re.I))

        issues, soft = [], []
        if med_t > 2:
            issues.append(f"文档响应中位数 {med_t:.2f}s 超过 2s")
        elif med_t > 0.8:
            soft.append(f"文档响应中位数 {med_t:.2f}s，偏慢（理想<0.8s）")
        if med_kb > 800:
            issues.append(f"HTML 体积中位数 {med_kb:.0f}KB 超过 800KB")
        elif med_kb > 300:
            soft.append(f"HTML 体积中位数 {med_kb:.0f}KB，偏大（理想<300KB）")
        if not compressed:
            soft.append("未检测到 gzip/br 传输压缩（响应头无 Content-Encoding）")

        slow_note = "；最慢页：" + "、".join(f"{l} {t:.2f}s" for l, t, _ in slowest) if len(slowest) >= 1 else ""
        comp_note = f"传输压缩：{enc}" if compressed else "传输压缩：未开启"
        scope_note = f"指标固定为 HTML 文档下载耗时（TTFB+下载），不是首屏渲染速度（LCP），共测 {len(rows)} 页（首页+内页），受检测点网络影响，供参考"

        head = rows[0]
        summary = f"首页 {head[1]:.2f}s/{head[2]:.0f}KB，{len(rows)} 页中位 {med_t:.2f}s/{med_kb:.0f}KB"
        # 检测过的全部页面挂进 pages，报告来源才能显示与说明一致的全量页面清单
        measured = [p.final_url for p in pages]
        if issues:
            return self._result(item, Status.FAIL, summary + "，速度/体积不达标",
                                detail="；".join(issues + soft) + slow_note + "；" + comp_note + "；" + scope_note,
                                pages=measured)
        if soft:
            return self._result(item, Status.WARN, summary + "，可进一步优化",
                                detail="；".join(soft) + slow_note + "；" + comp_note + "；" + scope_note,
                                pages=measured)
        return self._result(item, Status.PASS, summary + "，已开压缩",
                            detail=comp_note + slow_note + "；" + scope_note,
                            pages=measured)

    @staticmethod
    def _path_of(url: str) -> str:
        p = urlparse(url)
        path = p.path or "/"
        return path if len(path) <= 32 else "…" + path[-31:]

    _PUBLIC_SUFFIXES = None  # 类级缓存

    @classmethod
    def _registered_domain(cls, host: str) -> str:
        """注册域（eTLD+1）：内置精简公共后缀清单（config/public_suffixes.yaml）。
        未命中已知后缀时整个 host 视为注册域（宁漏勿错）。"""
        host = (host or "").lower().rstrip(".")
        if not host:
            return ""
        if cls._PUBLIC_SUFFIXES is None:
            import os
            import yaml
            path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                "config", "public_suffixes.yaml")
            try:
                with open(path, encoding="utf-8") as f:
                    cls._PUBLIC_SUFFIXES = set(yaml.safe_load(f).get("suffixes", []))
            except Exception:
                cls._PUBLIC_SUFFIXES = set()
        labels = host.split(".")
        for n in range(len(labels), 0, -1):
            if ".".join(labels[-n:]) in cls._PUBLIC_SUFFIXES:
                return ".".join(labels[-(n + 1):]) if len(labels) > n else host
        return host

    # 16. HEAD 与 GET 状态一致
    def _check_head_get(self) -> CheckResult:
        item = self._item(
            "tech-16", "HEAD/GET 状态码一致", Priority.P2,
            "同一个页面用 HEAD 与 GET 两种方式请求，返回的状态码是否一致",
            "对首页和部分内页先发 HEAD 再对照已抓到的 GET 状态码",
            "HEAD 与 GET 返回相同的成功状态码（如都 200）",
            purpose="部分爬虫、CDN 预检、健康监控会先发 HEAD 探活；若 HEAD 报 404/5xx 而 GET 正常，可能被误判为页面不存在，白白丢掉抓取",
            manual_check="命令行执行 curl -I 页面地址 看状态码，与浏览器直接打开(GET)是否一致",
            excellent="HEAD 与 GET 状态码一致",
            normal="个别静态资源 HEAD 行为不同（不影响正文页可不处理）",
            failed="正文页 HEAD 报 404/5xx 但 GET 正常",
            tier="bonus",  # 2026-09-10 V6定版：很多服务器不支持HEAD（405/501），误报多，转只提醒
            scope="首页+内页", applicable_pages="首页+内页",
            suggestion="让服务端路由对 HEAD 与 GET 走同一控制器、返回相同状态码，勿把 HEAD 误判成静态文件缺失",
        )
        if self.fetcher is None:
            return self._result(item, Status.SKIP, "无抓取器，无法比对 HEAD")
        # 按最终地址去重（重定向合并后的重复页不重复计数）
        _seen = set()
        targets = []
        for _p in [self.page] + (self.extra_pages or [])[:3]:
            k = _p.final_url.rstrip("/")
            if k in _seen:
                continue
            _seen.add(k)
            targets.append(_p)
        mismatch = []
        mismatch_pages = []
        checked = 0
        for p in targets:
            head_code = self.fetcher.head_status(p.final_url)
            get_code = p.status_code
            checked += 1
            if head_code is None:
                continue
            if head_code >= 400 and (get_code or 0) < 400:
                mismatch.append(f"{self._path_of(p.final_url)}：GET {get_code} / HEAD {head_code}")
                mismatch_pages.append(p.final_url)
        if not mismatch:
            return self._result(item, Status.PASS, f"{checked} 个抽查页 HEAD 与 GET 状态一致",
                                pages=[p.final_url for p in targets])
        return self._result(item, Status.WARN,
                            f"{len(mismatch)} 个页面 HEAD 报错但 GET 正常",
                            detail="；".join(mismatch), pages=mismatch_pages)

    # 17. Open Graph 分享卡片
    def _check_open_graph(self) -> CheckResult:
        item = self._item(
            "tech-17", "分享卡片标签(Open Graph)", Priority.P2,
            "是否配置 og:title/og:description/og:image/og:url/og:type 等分享与AI识别标签",
            "检查 <meta property='og:*'>，并抽 1 个内页确认公共头是否也带",
            "核心 og 标签齐全，分享到微信/社交平台时标题、描述、封面图可控",
            purpose="没有 OG 标签，链接被转发到微信/微博/IM 时标题和配图不可控、点击率受损；部分 AI 抓取器也会读取 og 信息辅助理解",
            manual_check="用微信发送自己官网链接看卡片是否带标题与图；或查看源代码 og: 标签，用分享调试工具预览",
            excellent="og:title/description/image/url/type/site_name 齐全，文章页 og:type=article",
            normal="缺 1-2 个非核心 og 标签",
            failed="完全没有 og 标签，分享卡片不可控",
            scope="全站", applicable_pages="全站",
            suggestion="在公共页头补 og:title/description/url/image/type/site_name，文章页 og:type 用 article",
            tier="bonus",  # 2026-09-10 V6定版：只影响社交分享卡片，转只提醒
        )
        core = ["og:title", "og:description", "og:image", "og:url", "og:type"]

        def og_props(soup):
            return {m.get("property", "").strip() for m in soup.find_all("meta")
                    if (m.get("property") or "").startswith("og:")}

        home_og = og_props(self.page.soup)
        missing = [k for k in core if k not in home_og]
        inner_note = ""
        if self.extra_pages and home_og:
            ip = self.extra_pages[0]
            if not og_props(ip.soup):
                inner_note = f"；内页 {self._path_of(ip.final_url)} 未带 og 标签（公共头可能只在首页加了）"
        if not home_og:
            return self._result(item, Status.WARN, "全站未发现 Open Graph 分享标签",
                                pages=[p.final_url for p in self.all_pages])
        if missing:
            return self._result(item, Status.WARN,
                                f"分享标签缺核心项：{'、'.join(missing)}" + inner_note,
                                pages=[p.final_url for p in self.all_pages])
        extra = "og:site_name" if "og:site_name" in home_og else ""
        return self._result(item, Status.PASS,
                            "核心分享标签齐全" + ("，含站点名" if extra else "") + inner_note,
                            pages=[p.final_url for p in self.all_pages])

    # 18. 匿名页会话与缓存
    def _check_session_cache(self) -> CheckResult:
        item = self._item(
            "tech-18", "页面缓存与会话开销", Priority.P1,
            "匿名访问的前台只读页是否无意义地开启了会话 Cookie、并禁止了缓存",
            "检查首页响应头 Set-Cookie 是否含会话标识，Cache-Control 是否 private/no-cache",
            "静态首页不向匿名访客下发会话 Cookie，且允许公共短缓存",
            purpose="每个匿名访客都开一次会话、又把页面设为 no-cache，会让 CDN/浏览器无法缓存，服务器重复渲染，首字节变慢，爬虫抓取成本也更高",
            manual_check="查看响应头是否每次都 Set-Cookie: PHPSESSID/JSESSIONID 等，且 Cache-Control 是否为 private/no-cache",
            excellent="匿名只读页不下发会话 Cookie，Cache-Control 允许 public 短缓存",
            normal="有会话或缓存其一配置不优，但影响有限",
            failed="匿名页既强制开会话又禁止缓存，明显拖慢响应",
            scope="首页", applicable_pages="首页",
            suggestion="前台只读页关闭 session、允许 public max-age 短缓存，表单/登录走独立接口",
        )
        h = {k.lower(): v for k, v in (self.page.headers or {}).items()}
        set_cookie = h.get("set-cookie", "")
        cache = h.get("cache-control", "")
        opens_session = bool(re.search(r"phpsessid|jsessionid|asp\.net_sessionid|sessid|session", set_cookie, re.I))
        no_cache = bool(re.search(r"no-cache|no-store|private", cache, re.I)) and not re.search(r"public", cache, re.I)
        if opens_session and no_cache:
            return self._result(item, Status.WARN, "匿名首页既下发会话 Cookie 又禁止缓存，拖慢响应",
                                detail=f"Set-Cookie: {set_cookie[:60]}；Cache-Control: {cache or '（无）'}")
        if opens_session:
            return self._result(item, Status.WARN, "匿名首页下发了会话 Cookie",
                                detail=f"Set-Cookie: {set_cookie[:60]}（只读页通常无需为每位访客开会话）")
        if no_cache:
            return self._result(item, Status.WARN, "首页禁止了缓存（Cache-Control 不可公开缓存）",
                                detail=f"Cache-Control: {cache or 'private/no-cache'}（可对只读页设置 public 短缓存）")
        return self._result(item, Status.PASS, "匿名首页未强制开会话，缓存策略未发现明显问题",
                            detail=f"Cache-Control: {cache or '未显式设置（服务器默认）'}")

    # 19. 安全响应头
    def _check_security_headers(self) -> CheckResult:
        item = self._item(
            "tech-19", "安全响应头", Priority.P2,
            "是否配置 HSTS、MIME 嗅探拦截、点击劫持防护、来源策略等基础安全响应头",
            "检查 Strict-Transport-Security、X-Content-Type-Options、X-Frame-Options/frame-ancestors、Referrer-Policy",
            "HTTPS 站点至少有 HSTS，并补齐其余基础安全头",
            purpose="安全头对排名权重影响很轻，属于站点健康与防护项：缺 HSTS 会留下降级访问风险，缺点击劫持/MIME 防护存在被嵌套与误解析隐患",
            manual_check="查看响应头是否包含上述字段；可在安全检测平台复测",
            excellent="HSTS、nosniff、frame-ancestors/X-Frame-Options、Referrer-Policy 均已配置",
            normal="配置了部分安全头，仍有缺失",
            failed="几乎没有基础安全响应头",
            scope="首页", applicable_pages="首页",
            suggestion="在 Web 服务器补 HSTS、X-Content-Type-Options: nosniff、X-Frame-Options 或 CSP frame-ancestors、Referrer-Policy",
            tier="bonus",  # 安全头对 SEO/GEO 排名影响很轻，按"影响小非必要不计分"仅作建议
        )
        h = {k.lower(): v for k, v in (self.page.headers or {}).items()}
        csp = h.get("content-security-policy", "")
        checks = [
            ("HSTS 强制 HTTPS", bool(h.get("strict-transport-security"))),
            ("MIME 嗅探拦截(X-Content-Type-Options)", "nosniff" in h.get("x-content-type-options", "").lower()),
            ("点击劫持防护(X-Frame-Options/CSP)", bool(h.get("x-frame-options")) or "frame-ancestors" in csp),
            ("来源策略(Referrer-Policy)", bool(h.get("referrer-policy"))),
        ]
        missing = [name for name, ok in checks if not ok]
        if not missing:
            return self._result(item, Status.PASS, "基础安全响应头齐全")
        is_https = (self.page.final_url or "").startswith("https://")
        head_lack_hsts = is_https and "HSTS 强制 HTTPS" in missing
        if len(missing) >= 3:
            return self._result(item, Status.WARN, f"缺 {len(missing)}/4 项基础安全响应头",
                                detail=("；".join(missing)) + ("；HTTPS 站点建议优先补 HSTS" if head_lack_hsts else "")
                                + "；本项价值主要在等保合规而非排名")
        return self._result(item, Status.WARN, f"安全响应头缺：{'、'.join(missing)}；本项价值主要在等保合规而非排名")

    # 20. 多入口统一跳转到首选域（www/非www、http/https 收口）
    def _check_host_canonicalization(self) -> CheckResult:
        item = self._item(
            "tech-20", "多入口统一跳转(首选域)", Priority.P0,
            "http/https、带www/不带www 这几个入口是否都永久跳转到同一个官方网址，而不是各自打开重复内容",
            "对四个入口逐个不跟随地探测跳转链，判断终点是否都落到本次实际访问的首选网址，以及是否用 301 永久跳转",
            "所有非首选入口均 301/308 到唯一 https 首选域，无入口直接返回 200 重复页",
            purpose="同一个站若 www 和不带 www、http 和 https 都能直接打开，搜索引擎会当成多份近似重复站、权重分散、收录不稳；统一 301 收口才能把信号集中到一个官方网址",
            manual_check="命令行 curl -I 分别访问 http://域名、http://www.域名、https://域名，看是否都 301 到 https://www.域名（或你选定的唯一首选）",
            excellent="各入口都用 301/308 一跳或两跳收敛到唯一首选域",
            normal="基本收口，但用了 302 临时跳转或个别入口链路不规范",
            failed="存在入口直接 200 打开与首选域相同的内容（重复入口未收口）",
            scope="整站", applicable_pages="整站",
            suggestion="在 Web 服务器把非首选主机名与 http 全部 301 到唯一 https 首选域，一条链接力、避免多次跳转，并配自引用 canonical",
        )
        if self.fetcher is None:
            return self._result(item, Status.SKIP, "无抓取器，无法探测多入口")
        pref = urlparse(self.page.final_url)
        pref_scheme, pref_host = pref.scheme, pref.netloc.lower()
        host = pref.netloc.lower()
        bare = host[4:] if host.startswith("www.") else host
        www = host if host.startswith("www.") else ("www." + host if host else host)
        hosts = [h for h in dict.fromkeys([bare, www]) if h]
        entries = [f"{s}://{h}/" for h in hosts for s in ("http", "https")]
        entries = [u for u in entries if u != f"{pref_scheme}://{pref_host}/"]

        def follow(start, max_hop=3):
            cur, hops = start, []
            for _ in range(max_hop):
                code, loc = self.fetcher.probe(cur)
                if code is None:
                    return cur, hops, "unreachable"
                hops.append((code, loc))
                if code in (301, 302, 303, 307, 308) and loc:
                    nxt = urljoin(cur, loc)
                    if nxt == cur:
                        return cur, hops, "loop"
                    cur = nxt
                    continue
                if 200 <= code < 300:
                    return cur, hops, "ok200"
                return cur, hops, f"err{code}"
            return cur, hops, "toolong"

        dup_open, temp_only, abnormal, unreachable = [], [], [], []
        for u in entries:
            final, hops, state = follow(u)
            fp = urlparse(final)
            seq = " → ".join([u] + [f"{c}:{loc or '·'}" for c, loc in hops])
            lands = fp.scheme == pref_scheme and fp.netloc.lower() == pref_host
            if state == "ok200" and not lands:
                dup_open.append(seq)                                    # 非首选地址直接 200 摊开重复页
            elif state == "ok200" and lands:
                if any(c in (302, 303, 307) for c, _ in hops):
                    temp_only.append(seq)                               # 到是到了，但用临时跳转
            elif state == "unreachable":
                unreachable.append(u)
            else:
                abnormal.append(seq + f"（{state}）")

        if dup_open:
            return self._result(item, Status.WARN,
                                f"{len(dup_open)} 个入口直接打开了重复内容、未统一跳转到首选域",
                                detail="未收口：" + "；".join(dup_open)
                                       + f"；应统一 301 到 {pref_scheme}://{pref_host}/")
        if temp_only:
            return self._result(item, Status.WARN, "入口已收敛但用的是 302 临时跳转，建议改 301 永久跳转",
                                detail="；".join(temp_only))
        if abnormal:
            return self._result(item, Status.WARN, "部分入口跳转链路异常",
                                detail="；".join(abnormal))
        note = f"http/https、www/非www 均收敛到 {pref_scheme}://{pref_host}/"
        if unreachable:
            note += "；个别入口不可达：" + "、".join(unreachable)
        return self._result(item, Status.PASS, "各入口已统一 301 收口到首选域", detail=note)

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
        bd_note = "另：移动适配与落地页弹窗体验引擎测不了，请到百度资源平台自查（百度有独立的移动适配要求与落地页体验算法）"
        if not viewport:
            return self._result(item, Status.FAIL, "未设置 viewport meta 标签", detail=bd_note)
        if "width=device-width" in viewport.lower():
            return self._result(item, Status.PASS, "已设置响应式 viewport", detail=bd_note)
        return self._result(item, Status.WARN, f"viewport 设置异常: {viewport}", detail=bd_note)

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

            # 日期型数字段（如 /2026/09/）是正常 CMS 结构，不计问题
            def _is_date_segment(idx, seg):
                if not re.match(r'^\d+$', seg or ''):
                    return False
                if len(seg) == 4 and 1900 <= int(seg) <= 2100:
                    return True
                return len(seg) in (1, 2) and idx > 0 and re.match(r'^(19|20)\d{2}$', segments[idx - 1] or '') is not None

            numeric_segments = [seg for i, seg in enumerate(segments)
                                if re.match(r'^\d+$', seg) and not _is_date_segment(i, seg)]
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
        # 2026-09-10 V6：来源挂实测页清单，悬浮框可展开逐条路径，避免"没真检测"的观感
        checked_urls = [p.final_url for p in pages_to_check]
        checked_paths = "、".join(urlparse(p.final_url).path or "/" for p in pages_to_check)
        if not all_issues:
            return self._result(item, Status.PASS,
                                f"所有{total}个子页面URL结构良好",
                                detail=f"已逐页检查以下 {total} 个内页路径，均简短可读：{checked_paths}",
                                pages=checked_urls)
        if len(all_issues) <= total * 0.3:
            return self._result(item, Status.WARN,
                                f"URL结构有改进空间（{good_count}/{total}良好）",
                                detail="；".join(all_issues[:5]),
                                pages=checked_urls)
        return self._result(item, Status.FAIL,
                            f"URL结构可读性差（仅{good_count}/{total}良好）",
                            detail="；".join(all_issues[:5]),
                            pages=checked_urls)

    # 14. CMS指纹识别
    def _load_cms_config(self) -> dict:
        """从 YAML 加载国内 CMS 指纹补充库"""
        import os
        import yaml
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                   "config", "cms_fingerprints.yaml")
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _load_wappalyzer_techs(self) -> dict:
        """加载 Wappalyzer 技术特征库"""
        import os
        import json
        tech_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                 "config", "wappalyzer", "technologies.json")
        with open(tech_path, "r", encoding="utf-8") as f:
            return json.load(f).get("technologies", {})

    def _match_wappalyzer(self, html: str, response_headers: dict,
                         cookie_names: set, meta_generator: str) -> list:
        """用 Wappalyzer 特征库匹配技术栈"""
        import re
        techs = self._load_wappalyzer_techs()
        detected = []

        for name, rules in techs.items():
            cats = rules.get("cats", [])
            # 只关注 CMS(1), Web Frameworks(6), E-commerce(6), JS Frameworks(12),
            # Static Site Generators(57), CDN(31), Web Servers(22), Programming Languages(27)
            target_cats = {1, 6, 12, 57, 31, 22, 27}
            if not any(c in target_cats for c in cats):
                continue

            evidence = None
            confidence = "低"

            # 1) Meta 标签匹配
            if not evidence and meta_generator and "meta" in rules:
                for meta_key, pattern in rules["meta"].items():
                    if meta_key.lower() == "generator":
                        if isinstance(pattern, str):
                            if re.search(pattern, meta_generator, re.IGNORECASE):
                                evidence = f"meta generator: {meta_generator}"
                                confidence = "高"
                        elif isinstance(pattern, list):
                            for p in pattern:
                                if re.search(p, meta_generator, re.IGNORECASE):
                                    evidence = f"meta generator: {meta_generator}"
                                    confidence = "高"
                                    break

            # 2) HTML 匹配
            if not evidence and "html" in rules:
                html_rules = rules["html"]
                if isinstance(html_rules, str):
                    html_rules = [html_rules]
                elif not isinstance(html_rules, list):
                    html_rules = []
                for pattern in html_rules:
                    try:
                        if re.search(pattern, html, re.IGNORECASE):
                            evidence = f"HTML特征: {pattern[:60]}"
                            confidence = "中"
                            break
                    except re.error:
                        pass

            # 3) Headers 匹配
            if not evidence and "headers" in rules:
                for header_name, pattern in rules["headers"].items():
                    header_val = ""
                    for k, v in response_headers.items():
                        if k.lower() == header_name.lower():
                            header_val = v
                            break
                    if header_val:
                        if isinstance(pattern, str):
                            try:
                                if re.search(pattern, header_val, re.IGNORECASE):
                                    evidence = f"Header {header_name}: {pattern[:60]}"
                                    confidence = "高"
                                    break
                            except re.error:
                                pass

            # 4) Cookie 匹配（精确匹配 cookie 名称，不做正则搜索）
            if not evidence and "cookies" in rules:
                for cookie_name, pattern in rules["cookies"].items():
                    if cookie_name in cookie_names:
                        evidence = f"Cookie: {cookie_name}"
                        confidence = "中"
                        break
                    if isinstance(pattern, str) and pattern:
                        for actual_cookie in cookie_names:
                            try:
                                if re.fullmatch(pattern, actual_cookie, re.IGNORECASE):
                                    evidence = f"Cookie: {cookie_name}"
                                    confidence = "中"
                                    break
                            except re.error:
                                pass
                        if evidence:
                            break

            # 5) Script src 匹配
            if not evidence and "scripts" in rules:
                script_rules = rules["scripts"]
                if isinstance(script_rules, str):
                    script_rules = [script_rules]
                elif isinstance(script_rules, dict):
                    script_rules = list(script_rules.values())
                elif not isinstance(script_rules, list):
                    script_rules = []
                for pattern in script_rules:
                    if isinstance(pattern, str):
                        try:
                            if re.search(pattern, html, re.IGNORECASE):
                                evidence = f"Script: {pattern[:60]}"
                                confidence = "中"
                                break
                        except re.error:
                            pass

            if evidence:
                cat_name = self._wappalyzer_cat_name(cats)
                detected.append({
                    "cms": name,
                    "type": cat_name,
                    "evidence": evidence,
                    "confidence": confidence,
                    "cats": cats,
                    "suitability": "",
                })

        return detected

    def _match_yaml_platforms(self, html: str, response_headers: dict,
                              cookie_names: set, meta_generator: str,
                              base_url: str) -> list:
        """v2 YAML: 权重累计 + 多维度 + excludes/implies + fingerprint_paths"""
        import httpx
        from urllib.parse import urljoin

        config = self._load_cms_config()
        platforms = config.get("platforms", [])
        global_config = config.get("global", {})
        conf_levels = global_config.get("confidence_levels", {})
        high_threshold = conf_levels.get("high", 70)
        medium_threshold = conf_levels.get("medium", 40)

        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        candidates = []

        # Phase 1: 非HTTP维度匹配（meta_generator / html_patterns / header_patterns / cookie_names）
        for platform in platforms:
            min_confidence = platform.get("min_confidence", 40)
            weight_total = 0
            evidence_parts = []
            matched_dims = 0

            # meta_generator
            for rule in (platform.get("meta_generator") or []):
                pattern = rule.get("pattern", "")
                weight = rule.get("weight", 20)
                flags_str = rule.get("flags", "i")
                flags = re.IGNORECASE if "i" in flags_str else 0
                if meta_generator and pattern:
                    try:
                        if re.search(pattern, meta_generator, flags):
                            weight_total += weight
                            matched_dims += 1
                            evidence_parts.append(f"meta generator: {meta_generator}")
                            break
                    except re.error:
                        pass

            # html_patterns
            for rule in (platform.get("html_patterns") or []):
                pattern = rule.get("pattern", "")
                weight = rule.get("weight", 20)
                flags_str = rule.get("flags", "i")
                flags = re.IGNORECASE if "i" in flags_str else 0
                if pattern:
                    try:
                        if re.search(pattern, html, flags):
                            weight_total += weight
                            matched_dims += 1
                            evidence_parts.append(f"HTML特征: {pattern[:80]}")
                            break
                    except re.error:
                        pass

            # header_patterns
            for rule in (platform.get("header_patterns") or []):
                header_name = rule.get("header", "")
                pattern = rule.get("pattern", "")
                weight = rule.get("weight", 20)
                flags_str = rule.get("flags", "i")
                flags = re.IGNORECASE if "i" in flags_str else 0
                if header_name and pattern:
                    header_val = ""
                    for k, v in response_headers.items():
                        if k.lower() == header_name.lower():
                            header_val = v
                            break
                    if header_val:
                        try:
                            if re.search(pattern, header_val, flags):
                                weight_total += weight
                                matched_dims += 1
                                evidence_parts.append(f"Header {header_name}")
                                break
                        except re.error:
                            pass

            # cookie_names
            for rule in (platform.get("cookie_names") or []):
                cookie_pattern = rule.get("name", "")
                weight = rule.get("weight", 20)
                if not cookie_pattern:
                    continue
                for actual_cookie in cookie_names:
                    if cookie_pattern.endswith("*"):
                        if actual_cookie.startswith(cookie_pattern[:-1]):
                            weight_total += weight
                            matched_dims += 1
                            evidence_parts.append(f"Cookie: {cookie_pattern}")
                            break
                    elif cookie_pattern.startswith("*"):
                        if actual_cookie.endswith(cookie_pattern[1:]):
                            weight_total += weight
                            matched_dims += 1
                            evidence_parts.append(f"Cookie: {cookie_pattern}")
                            break
                    elif cookie_pattern == actual_cookie:
                        weight_total += weight
                        matched_dims += 1
                        evidence_parts.append(f"Cookie: {cookie_pattern}")
                        break

            candidates.append([platform, weight_total, evidence_parts, matched_dims])

        # Phase 2: fingerprint_paths — 仅对有权重但未达阈值的平台做 GET 探测
        needs_paths = [c for c in candidates
                       if 0 < c[1] < c[0].get("min_confidence", 40)]
        needs_paths.sort(key=lambda x: x[1], reverse=True)

        for cand in needs_paths:
            platform = cand[0]
            min_confidence = platform.get("min_confidence", 40)
            for rule in (platform.get("fingerprint_paths") or []):
                if cand[1] >= min_confidence:
                    break
                path = rule.get("path", "")
                expect_status = rule.get("expect_status", [200, 302, 403])
                expect_content = rule.get("expect_content", "")
                weight = rule.get("weight", 20)
                if not path:
                    continue
                try:
                    check_url = urljoin(base_url, path)
                    with httpx.Client(timeout=8, follow_redirects=False, headers=headers) as client:
                        resp = client.get(check_url)
                    if resp.status_code in expect_status:
                        content_ok = True
                        if expect_content:
                            try:
                                if not re.search(expect_content, resp.text[:5000], re.IGNORECASE):
                                    content_ok = False
                            except re.error:
                                pass
                        if content_ok:
                            cand[1] += weight
                            cand[3] += 1
                            cand[2].append(f"{path} → HTTP {resp.status_code}")
                except Exception:
                    pass

        # Phase 3: 收集达标平台
        detected = []
        for platform, weight_total, evidence_parts, matched_dims in candidates:
            min_confidence = platform.get("min_confidence", 40)
            if weight_total < min_confidence:
                continue

            name = platform.get("name", "")
            ptype = platform.get("type", "cms")
            suitability = platform.get("suitability", "需进一步评估技术栈和可编辑性")

            # version_extract
            version = None
            for rule in (platform.get("version_extract") or []):
                source = rule.get("source", "")
                regex = rule.get("regex", "")
                flags_str = rule.get("flags", "i")
                flags = re.IGNORECASE if "i" in flags_str else 0
                if not regex:
                    continue
                try:
                    if source == "meta_generator" and meta_generator:
                        m = re.search(regex, meta_generator, flags)
                        if m:
                            version = m.group(1)
                            break
                    elif source == "html":
                        m = re.search(regex, html, flags)
                        if m:
                            version = m.group(1)
                            break
                except (re.error, IndexError):
                    pass

            if weight_total >= high_threshold:
                confidence = "高"
            elif weight_total >= medium_threshold:
                confidence = "中"
            else:
                confidence = "低"

            display_name = f"{name} {version}" if version else name

            detected.append({
                "cms": display_name,
                "original_name": name,
                "type": ptype,
                "evidence": "；".join(evidence_parts[:3]) if evidence_parts else "权重累计达标",
                "confidence": confidence,
                "weight": weight_total,
                "matched_dims": matched_dims,
                "version": version,
                "suitability": suitability,
                "excludes": platform.get("excludes", []),
                "implies": platform.get("implies", []),
            })

        # Apply excludes
        to_remove = set()
        for d in detected:
            to_remove.update(d.get("excludes", []))
        if to_remove:
            detected = [d for d in detected if d["original_name"] not in to_remove]

        # Apply implies
        implied = set()
        for d in detected:
            implied.update(d.get("implies", []))
        if implied:
            existing = {d["original_name"] for d in detected}
            for platform in platforms:
                pname = platform.get("name", "")
                if pname in implied and pname not in existing:
                    detected.append({
                        "cms": pname,
                        "original_name": pname,
                        "type": platform.get("type", "cms"),
                        "evidence": "由其他技术推断",
                        "confidence": "低",
                        "weight": 0,
                        "matched_dims": 0,
                        "version": None,
                        "suitability": platform.get("suitability", ""),
                        "excludes": [],
                        "implies": [],
                    })

        return detected

    def _wappalyzer_cat_name(self, cats: list) -> str:
        """Wappalyzer 分类 ID → 类型名"""
        cat_map = {
            1: "cms",
            6: "ecommerce",
            12: "framework",
            57: "cms",
            31: "saas",
        }
        for cat_id in cats:
            if cat_id in cat_map:
                return cat_map[cat_id]
        return "cms"

    def _check_cms_fingerprint(self) -> CheckResult:
        item = self._item(
            "tech-14", "CMS指纹识别", Priority.P1,
            "识别目标网站使用的建站系统/CMS类型",
            "通过Wappalyzer特征库(1270+技术) + 国内平台补充库识别CMS",
            "识别出CMS类型，评估技术栈是否适合AI GEO优化运营",
            manual_check="查看页面源代码中的generator标签、特征文件路径(如/wp-login.php、/dede/)、cookie名称等",
            purpose="识别CMS类型可判断技术栈是否适合AI GEO优化运营，是否能灵活修改URL/结构/内容，以及是否存在已知安全风险",
            excellent="识别为常见CMS（WordPress/帝国等），技术栈成熟，二次开发友好，可灵活整改",
            normal="识别为SaaS建站平台（凡科/建站ABC等），整改受平台限制，需评估是否能获取编辑权限",
            failed="无法识别CMS类型（可能为全定制开发），需进一步排查技术栈",
        )
        import httpx

        soup = self.local_soup
        html = self.page.html
        base_url = self.page.final_url
        html_lower = html.lower()

        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

        # --- 获取响应头和 Cookie ---
        response_headers = {}
        cookie_names = set()
        try:
            with httpx.Client(timeout=10, follow_redirects=True, headers=headers) as client:
                resp = client.get(base_url)
                response_headers = dict(resp.headers)
                cookie_names = set(resp.cookies.keys())
        except Exception:
            pass

        # --- Meta generator ---
        gen_tag = soup.find("meta", attrs={"name": "generator"})
        gen_content = gen_tag.get("content", "") if gen_tag else ""

        detected = []

        # === Layer 1: YAML 特征库（权重累计，多维度）===
        yaml_results = self._match_yaml_platforms(
            html, response_headers, cookie_names, gen_content, base_url)
        detected.extend(yaml_results)

        yaml_names = {d["original_name"] for d in detected if "original_name" in d}

        # === Layer 2: Wappalyzer 特征库（补充，跳过 YAML 已识别的）===
        wappalyzer_results = self._match_wappalyzer(html, response_headers, cookie_names, gen_content)
        for wr in wappalyzer_results:
            if wr["cms"] not in yaml_names:
                detected.append(wr)

        # === Layer 3: Wappalyzer URL 规则（仅当无匹配时）===
        if not detected:
            techs = self._load_wappalyzer_techs()
            for name, rules in techs.items():
                if 1 not in rules.get("cats", []):
                    continue
                url_patterns = rules.get("url", "")
                if isinstance(url_patterns, str) and url_patterns:
                    try:
                        if re.search(url_patterns, base_url, re.IGNORECASE):
                            detected.append({"cms": name, "type": "cms",
                                             "evidence": f"URL匹配: {url_patterns[:60]}",
                                             "confidence": "中", "suitability": ""})
                            break
                    except re.error:
                        pass

        # --- 综合判断 ---
        if detected:
            # 优先 YAML 结果（有 weight 或 suitability）
            yaml_hits = [d for d in detected if d.get("weight", 0) > 0 or d.get("suitability")]
            best = yaml_hits[0] if yaml_hits else detected[0]
            cms_name = best["cms"]
            ptype = best.get("type", "cms")
            confidence = best.get("confidence", "低")
            suitability = best.get("suitability", "需进一步评估技术栈和可编辑性")
            evidence = best.get("evidence", "")

            all_names = [d["cms"] for d in detected]
            tech_stack = ", ".join(all_names[:5])

            if ptype == "saas":
                status = Status.WARN
                msg = f"识别为SaaS建站平台: {cms_name}（整改可能受平台限制）"
            elif ptype == "framework":
                status = Status.WARN
                msg = f"识别为前端框架: {cms_name}（需后端配合整改）"
            elif ptype == "ecommerce":
                status = Status.PASS
                msg = f"识别为: {cms_name}（电商系统）"
            else:
                status = Status.PASS
                msg = f"识别为: {cms_name}（技术栈可接手整改）"

            detail = f"置信度: {confidence}。证据: {evidence}。接手评估: {suitability}"
            if len(all_names) > 1:
                detail += f"。技术栈: {tech_stack}"

            return self._result(item, status, msg, detail=detail)
        else:
            has_framework = any(fw in html_lower for fw in ["vue", "react", "angular", "next.js", "nuxt", "__next", "__nuxt"])
            if has_framework:
                return self._result(item, Status.WARN, "疑似前端框架定制开发(Vue/React/Angular)",
                                    detail="无法确定后端CMS，可能为全定制开发，需进一步排查技术栈")
            return self._result(item, Status.WARN, "未能识别CMS类型（可能为全定制开发）",
                                detail="Wappalyzer(1270+技术)和国内补充库均未匹配。建议人工排查技术栈")

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

        # 从 YAML 配置加载后台路径
        config = self._load_cms_config()
        admin_paths = [(p["label"], p["path"]) for p in config.get("admin_paths", [])]

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
                                detail=f"扫描了{len(admin_paths)}个常见路径，均为404或重定向；主动探测可能被贵方 WAF/访问日志记录，属正常审计行为")


    # 24. 站内搜索页可索引性（A1 ④：noindex=合规不报；可索引=WARN 建议加 noindex；搜索页不进通用打分）
    def _check_search_index(self) -> CheckResult:
        item = self._item(
            "tech-21", "站内搜索页可索引性", Priority.P2,
            "站内搜索结果页是否被 noindex（搜索页被收录会产生大量低质重复页）",
            "爬虫识别搜索结果页（/search、?s=、?q=、?keyword= 等特征），抽查其 noindex 状态",
            "搜索结果页已 noindex 或站点无站内搜索",
            manual_check="在站内搜索任意词，看结果页网址；右键源代码搜索 noindex，确认搜索页是否禁止收录",
            purpose="搜索结果页内容随查询词无限变化，被收录会产生海量低质重复页，稀释整站质量信号",
            excellent="搜索页已 noindex 或无站内搜索",
            normal="未检测到站内搜索",
            failed="搜索结果页可被索引",
            tier="bonus",
        )
        pages = getattr(self, "search_pages", None) or []
        if not pages:
            # 2026-09-10 V6：未检查也要给检测证据（查了多少页、有无搜索表单）
            n_urls = len(getattr(self, "all_site_urls", []) or []) or len(self.all_pages)
            has_form = bool(self.local_soup.find("form")) or bool(
                self.local_soup.find("input", attrs={"type": "search"}))
            form_note = "首页存在表单但未见搜索结果页" if has_form else "首页亦无搜索表单"
            return self._result(item, Status.SKIP,
                                f"站点未配置站内搜索功能，本项不适用"
                                f"（已核查 {n_urls} 个页面，无搜索结果页特征，{form_note}）",
                                skip_kind="na")
        indexable = [p.final_url for p in pages if not self._search_noindexed(p)]
        if not indexable:
            return self._result(item, Status.SKIP,
                                f"{len(pages)} 个搜索结果页均已 noindex，合规",
                                detail="；".join((urlparse(p.final_url).path or "/") for p in pages),
                                skip_kind="na",
                                pages=[p.final_url for p in pages])
        return self._result(item, Status.WARN,
                            f"{len(indexable)} 个搜索结果页可被索引，建议加 noindex",
                            detail="；".join(indexable[:5]),
                            pages=indexable)

    @staticmethod
    def _search_noindexed(p) -> bool:
        meta_robots = ""
        if p.soup:
            tag = p.soup.find("meta", attrs={"name": "robots"})
            meta_robots = (tag or {}).get("content", "") if tag else ""
        x_robots = (p.headers or {}).get("X-Robots-Tag", "")
        return "noindex" in (meta_robots + " " + x_robots).lower()

    # 25. 分页可抓取性（B6 降级版：只查分页链接是否为静态 href，不引入无头浏览器）
    def _check_pagination(self) -> CheckResult:
        item = self._item(
            "tech-22", "分页可抓取性", Priority.P2,
            "列表分页链接是否为静态 href（源代码可直接看到），而非 JS 渲染后才出现",
            "在抽样页面中查找分页特征（page/2、?page=2、?p=2 等）静态链接；发现分页 UI 但无静态链接时标注需人工复核",
            "分页链接为静态 href，爬虫可沿链接到达后续页",
            manual_check="打开列表页翻到第 2 页，复制网址；再右键源代码搜索该网址，能搜到即为静态链接",
            purpose="分页只有 JS 能点出来时，爬虫无法到达第 2 页之后的商品/文章，大量内容对 AI 和搜索引擎不可见",
            excellent="分页为静态 href 链接",
            normal="未检测到分页（单页站点不适用）",
            failed="存在分页 UI 但链接依赖 JS 渲染",
            tier="bonus",
        )
        pag_static, js_ui = [], []
        for p in self.all_pages:
            has_static = False
            for a in p.soup.find_all("a", href=True):
                if re.search(r"(?:[?&]page=\d+|/page/\d+|[?&]p=\d+)", a["href"]):
                    has_static = True
                    break
            if has_static:
                pag_static.append(p.final_url)
                continue
            # 2026-09-10 V6：轮播组件的圆点指示器（swiper-pagination 等）不是列表翻页，排除
            has_ui = False
            for el in p.soup.find_all(class_=re.compile(r"pagination|pager|page-numbers|page-nav", re.I)):
                blob = (str(el.get("class") or []) + " " + str(el.get("id") or "")).lower()
                if re.search(r"swiper|carousel|slick|slider|slide|banner", blob):
                    continue
                has_ui = True
                break
            if has_ui:
                js_ui.append(p.final_url)
        if js_ui:
            return self._result(item, Status.WARN,
                                f"{len(js_ui)} 个页面存在分页 UI 但未发现静态分页链接（JS 渲染分页，需人工复核）",
                                detail="；".join((urlparse(u).path or "/") for u in js_ui[:5]),
                                pages=js_ui)
        if pag_static:
            return self._result(item, Status.PASS,
                                f"{len(pag_static)} 个页面存在静态分页链接，可正常抓取",
                                pages=pag_static)
        return self._result(item, Status.SKIP, "抽样页面未检测到分页", skip_kind="na")

