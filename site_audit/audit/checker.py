"""检查器基类"""
from abc import ABC, abstractmethod
from typing import List
from site_audit.core.models import CheckResult, CheckItem, Status, Priority, Category
from site_audit.core import priority_config
from site_audit.core.fetcher import Fetcher, PageData


# 每个检查项"没有会产生什么影响"的说明
IMPACT_MAP = {
    # 技术可抓取性
    "tech-01": "没有HTTPS，AI爬虫会降低站点信任度，搜索排名靠后，用户浏览器也会提示'不安全'，直接影响转化",
    "tech-02": "robots.txt封禁AI爬虫，等于主动把自己排除在AI搜索之外，用户问AI时完全不会提到你的品牌",
    "tech-03": "没有sitemap，AI爬虫只能靠链接发现页面，重要页面可能长期不被收录，新内容收录极慢",
    "tech-04": "没有llms.txt，AI无法快速理解你的站点结构和核心页面，被引用概率大幅降低，这是GEO最核心的文件",
    "tech-05": "没有.well-known发现文件，ChatGPT等AI助手无法自动识别你的站点能力，错失AI插件式曝光机会",
    "tech-06": "页面被noindex，等于告诉AI和搜索引擎'不要收录我'，核心页面会直接从搜索结果中消失",
    "tech-07": "没有canonical，同一内容多个URL会分散权重，AI可能选到非首选页面，甚至判定为重复内容降权",
    "tech-08": "返回4xx/5xx，AI爬虫根本打不开页面，无法抓取任何内容，该页面完全不会被收录",
    "tech-09": "内容全靠JS渲染，AI爬虫看到的是空白页，完全无法理解和引用你的内容，等于做了个隐形网站",
    "tech-10": "页面过大或加载过慢，AI爬虫可能超时放弃抓取，用户也会因等待流失，搜索引擎也会降权",
    "tech-11": "没有移动端适配，70%以上的移动搜索流量会流失，AI和搜索引擎都会优先推荐移动端友好的站点",
    "tech-12": "URL含动态参数或纯数字，AI和用户都无法从URL判断页面主题，分享时显示不专业，搜索引擎也会降低可读性评分",
    "tech-13": "站内存在死链，AI爬虫抓取会中断、降低整站抓取效率，用户点到404页面直接流失，搜索引擎也会降低站点健康度评分",
    "tech-14": "无法识别CMS类型，难以判断技术栈是否适合接手整改，也无法针对性给出优化方案",
    "tech-15": "后台路径可公开访问，存在被暴力破解或扫描攻击的风险，安全隐患大",
    "tech-16": "正文页 HEAD 报错而 GET 正常，部分爬虫、CDN 预检、监控会先用 HEAD 探活，可能误判页面不存在而放弃抓取",
    "tech-17": "没有分享标签，链接转发到微信等平台时标题、描述、封面图不可控，点击率受损，部分 AI 抓取器也读不到摘要信息",
    "tech-18": "匿名只读页强行开会话又禁缓存，CDN/浏览器无法缓存、服务器重复渲染，首字节变慢，抓取成本上升",
    "tech-19": "缺基础安全响应头，存在被嵌套点击劫持、MIME 误解析、降级访问等隐患，属站点健康度扣分项",
    "tech-20": "www/非www、http/https 多入口都能直接打开，搜索引擎会当成多份重复站、权重分散、收录不稳，信号集中不到官方网址",
    # AI友好度
    "ai-01": "没有Title或过短，AI无法判断页面主题，搜索结果显示无意义标题，点击率和引用率双降",
    "ai-02": "没有Description，AI只能从页面随机摘取内容做摘要，可能抓到大段导航或无关文字，严重影响展示效果",
    "ai-03": "标题层级混乱，AI无法理解内容结构，难以定位关键信息，摘取答案时容易遗漏核心内容",
    "ai-04": "没有结构化数据，AI无法识别你的产品/文章/评价类型，出不了富摘要，被精准引用的概率降低",
    "ai-05": "内容全是大段文字，AI难以直接摘取成答案，用户问具体问题时，AI更愿意引用有列表/表格/FAQ的页面",
    "ai-06": "没有面包屑，AI无法判断页面在站点中的层级位置，搜索结果也显示不了面包屑路径，点击率降低",
    "ai-07": "站内链接极少，AI爬虫发现不了你的其他页面，整站收录率低，品牌信息不完整",
    "ai-08": "没验证站长平台，无法主动提交sitemap和查看收录异常，问题发现滞后，收录速度慢",
    "ai-09": "正文语义混乱、概念不定义、指代不明，大模型难以准确提取事实，摘取时容易断章取义或放弃引用",
    "ai-10": "标题分块只是形式、块内内容东拉西扯，AI定位到小标题却提取不到连贯答案，无法整块引用",
    "ai-11": "多个页面共用相同Title/Description，AI和搜索引擎无法区分页面主题，导致权重分散、该收录的内页不被收录",
    # 内容质量
    "content-01": "内容过薄，AI提取不到有效信息，不会选为答案来源，用户也觉得不专业，跳出率高",
    "content-02": "没有更新时间，AI无法判断内容是否过时；内容若是全网通用套话/AI水文、无原创价值，AI会优先引用有独到观点的竞品",
    "content-03": "没有FAQ，用户问具体问题时AI没有现成答案可摘，FAQ是GEO转化率最高的内容形式",
    "content-04": "图片没有alt，AI看不懂图片内容，图片搜索流量全丢，无障碍访问也不合格",
    "content-05": "锚文本全是'点击这里'，AI无法判断被链接页面的主题，内页关键词权重传递失效",
    "content-06": "多语言无hreflang，不同语言版本互相竞争排名，海外用户可能看到错误语言的页面",
    "content-07": "内容类型与用户搜索意图错位（想找方案却看到公司介绍），AI判定答非所问不予引用，用户也快速跳出",
    "content-08": "多个页面争夺同一关键词，AI分不清该引用哪页，可能都不引用，整站该词排名互相拖后腿",
    # 权威信任
    "auth-01": "没有备案和联系方式，AI判定为低可信度站点，不会优先推荐，用户也不敢联系，律所/金融等行业尤其致命",
    "auth-02": "没有客户评价和案例，AI缺少社会证明信号，用户决策时没有参考，转化率低",
    "auth-03": "没有社交平台账号，AI判断品牌存在感弱，品牌实体认知度低，推荐优先级下降",
    "auth-04": "没有权威外链，AI判断站点缺乏第三方背书，权威性不足，排名和推荐都靠后",
    "auth-05": "没有百科/知识面板，AI不认为你是知名品牌实体，品牌词搜索时不会出知识卡片",
    "auth-06": "没有跨平台分发，AI训练数据中你的品牌出现频次低，覆盖面窄，被提及概率小",
    "auth-07": "内容无作者署名、无第一手经验、无来源标注，AI无法判断专业可信度，会优先引用有明确专家背书的竞品内容",
    # SEO基础五项（seo-01~05）无独立检查逻辑，属死数据，已于 2026-09-03 清除（阿栈）。
    # 并入映射表（存档备查，勿恢复为独立检查项）：
    #   seo-01 标题质量        → ai-01（Title质量）+ ai-11（Title/Description唯一性）
    #   seo-02 描述行动号召    → ai-02（Description质量，修复建议已含CTA）
    #   seo-03 图片SEO        → content-04（图片alt）
    #   seo-04 富结果Schema    → ai-04（结构化数据）+ ai-06（BreadcrumbList）
    #   seo-05 robots引sitemap → tech-02（robots.txt）+ tech-03（sitemap）
    # B端增强
    "b2b-01": "没有独立产品/方案页，AI无法匹配用户的具体产品搜索需求，B端精准流量全丢",
    "b2b-02": "没有客户案例，B端客户决策时缺少信任依据，AI回答'XX行业谁做过'时也不会提到你",
    "b2b-03": "没有行业方案页，用户搜'XX行业解决方案'时匹配不到，错失高意向行业流量",
    "b2b-04": "没有对比/选型内容，用户在竞品对比阶段找不到你，被竞品内容截胡流量",
    "b2b-05": "技术文档不友好，开发者和技术决策者搜技术问题时找不到你，技术驱动的B端业务流失",
    "b2b-06": "没有问答型内容，用户搜'多少钱''怎么部署''安全吗'等高意向问题时匹配不到，转化关键节点缺失",
    "b2b-07": "内容讲得好却没有下一步入口，或一上来就硬推销，用户和AI都找不到从了解到联系的路径，高意向流量白白流失",
    "b2b-08": "全站找不到有效联系方式，高意向访客无法触达，获客链路直接断开，AI也给不出可信的联系路径",
    "b2b-09": "缺少可点的行动按钮（咨询/预约/购买），访客产生兴趣后没有明确下一步，意向在页面上流失",
    "b2b-10": "价格、怎么买、流程、售后、覆盖范围这些下决心前的信息缺失，客户在犹豫中流失，AI也无法替你说清",
    # 内页多页聚合扫描（2026-09-05 整改新增）
    "mp-01": "内页缺独立标题/描述或套用首页，搜索和AI分不清每页主题，该收录的内页不被收录、不被引用",
    "mp-02": "一个页面出现多个H1，AI抓不到页面主题，正文结构混乱会被降权并减少引用",
    "mp-03": "文章/产品/案例详情页没有结构化标记，AI认不出它是什么类型的内容，难以精准引用",
    "mp-04": "内页正文过薄给不出有效信息，AI不会把它选为答案来源，也撑不起要竞争的关键词",
    "mp-05": "内页图片没有alt，AI看不懂图片内容，图片搜索流量丢失，无障碍也不合格",
    "mp-06": "缺关于/产品服务/FAQ/隐私等必备页，AI答不出'你是谁、卖什么、客户常问什么、是否正规'；缺FAQ直接降低被引用概率",
    "mp-07": "关于等内页只是首页内容的复制副本、没有独立信息，属于重复内容，搜索不重复收录、AI也得不到新的可引用事实",
    "auth-08": "成立年、规模、电话、官网等同一事实在名片/页脚/正文里出现两种说法，AI做实体对齐时无法判断哪个可信，会降低采信甚至弃用，是GEO硬伤",
}

# 每个检查项的检测范围：全站 vs 首页 vs 内容页
# 内容页 = 从抽样内页中挑一篇文章/案例/产品页单页执行（_pick_content_page）
# 2026-09-03 阿栈逐项核对代码后修正 11 项（原默认「首页」与实际取页行为脱节）
SCOPE_MAP = {
    # 全站级：影响整个站点
    "tech-01": "全站",
    "tech-02": "全站",
    "tech-03": "全站",
    "tech-04": "全站",
    "tech-05": "全站",
    "auth-05": "全站",
    "auth-06": "全站",
    "content-08": "全站",  # 关键词蚕食：需跨多个页面对比
    "content-02": "全站",  # 原创性与时效性：全站检查所有内容页
    "tech-12": "全站",    # URL结构与可读性：检查全部抽样内页的URL（technical.py 取 extra_pages）
    "tech-13": "全站",    # 断链检测：跨页面
    "ai-11": "全站",      # Title/Description唯一性：跨页面
    "b2b-05": "全站",     # 技术文档：规则层扫描全部抽样页（ai_engine.check_tech_docs 取 all_pages）
    "b2b-06": "全站",     # 问答型内容：规则层扫描全部抽样页（check_qa_content 取 all_pages）
    # 内容页级：从抽样内页挑单个内容页执行（_pick_content_page）
    "ai-06": "内容页",    # 面包屑导航：挑抽样内容页检查（ai_friendly.py:215）
    "ai-09": "内容页",    # NLP语义友好度：挑抽样内容页检查
    "ai-10": "内容页",    # 内容分块：挑抽样内容页检查
    "content-07": "内容页",  # 搜索意图匹配：挑抽样内容页检查
    "auth-07": "内容页",  # E-E-A-T：挑抽样内容页检查
    # 其余默认首页级
}

# 每个检查项的适用页面类型：首页 / 内页 / 内容页 / 全站
# 内页/内容页专属项在只检测首页时标记为"需内页检测"，不直接扣分
APPLICABLE_PAGES_MAP = {
    "tech-02": "全站",    # robots.txt 是站级配置文件，非首页内容
    "tech-03": "全站",    # sitemap.xml 站级配置
    "tech-04": "全站",    # llms.txt 站级配置
    "tech-05": "全站",    # .well-known 站级配置
    "tech-12": "内页",    # URL检查对象是子页面路径，首页URL为域名无需检查
    "tech-13": "全站",    # 断链检测覆盖全部抽样页（cross_page.py 取 all_pages）
    "ai-06": "内容页",    # 面包屑在抽样内容页检查
    "ai-09": "内容页",
    "ai-10": "内容页",
    "ai-11": "全站",      # 跨页面对比全部抽样页的 Title/Description
    "content-02": "全站",  # 内容原创性与时效性：全站检查所有内容页
    "content-07": "内容页",
    "content-08": "全站",  # 关键词蚕食跨全部抽样页
    "auth-07": "内容页",
    "b2b-05": "全站",
    "b2b-06": "全站",
    # 其余默认"首页"（首页可检测）
}

# 每个检查项的修复建议
SUGGESTION_MAP = {
    # 技术可抓取性
    "tech-01": "配置SSL证书（可使用免费的Let's Encrypt），确保全站HTTPS，开启HSTS",
    "tech-02": "检查robots.txt，删除针对GPTBot/ClaudeBot/Google-Extended/Bytespider等AI爬虫的Disallow规则",
    "tech-03": "生成sitemap.xml并提交到搜索引擎站长平台，确保包含所有重要页面",
    "tech-04": "在网站根目录创建llms.txt，列出站点简介、核心页面URL、服务内容、联系方式",
    "tech-05": "在/.well-known/目录下创建ai-plugin.json或类似发现文件，声明站点能力",
    "tech-06": "移除noindex标签，确保核心页面可被索引；仅对隐私政策等不需要收录的页面使用noindex",
    "tech-07": "在每页head中添加canonical标签，指向该页面的首选URL",
    "tech-08": "修复4xx/5xx错误页面，设置301重定向或恢复页面内容",
    "tech-09": "关键内容采用服务端渲染（SSR）或静态化，确保AI爬虫不执行JS也能看到核心内容",
    "tech-10": "压缩图片、启用Gzip/Brotli、精简JS/CSS，目标页面体积<500KB、加载时间<2s",
    "tech-11": "采用响应式设计，确保移动端正常显示，通过Google移动端友好测试",
    "tech-12": "URL路径使用描述性关键词，用连字符分隔，全小写，去掉动态参数，控制在3层目录以内",
    "tech-13": "用Screaming Frog/Xenu等工具全站爬链，修复或301跳转所有404/5xx死链，定期巡检",
    "tech-14": "通过特征文件和meta标签识别CMS类型，评估是否适合接手整改",
    "tech-15": "限制后台路径访问IP/添加验证/改默认路径，确保后台不暴露在公网",
    "tech-16": "让服务端对 HEAD 与 GET 走同一控制器、返回相同状态码，勿把 HEAD 误判为静态文件缺失",
    "tech-17": "在公共页头补齐 og:title/description/url/image/type/site_name，文章页 og:type 用 article",
    "tech-18": "前台只读页关闭会话、允许 public 短缓存，表单/登录走独立接口",
    "tech-19": "在 Web 服务器补 HSTS、X-Content-Type-Options: nosniff、X-Frame-Options 或 CSP frame-ancestors、Referrer-Policy",
    "tech-20": "把非首选主机名与 http 全部 301 永久跳转到唯一 https 首选域，一条链接力、避免多次跳转，并在页面写自引用 canonical",
    # AI友好度
    "ai-01": "Title控制在30-60字符，格式：核心关键词 + 品牌/地域，避免堆砌",
    "ai-02": "Description控制在70-160字符，包含核心关键词和行动号召，准确概括页面内容",
    "ai-03": "每页仅1个H1（页面主题），H2按大板块分，H3按子点分，层级不跳级",
    "ai-04": "添加对应Schema.org结构化数据：Organization/WebSite/Product/Article/FAQPage等",
    "ai-05": "关键内容用列表（ul/ol）、表格（table）、FAQ结构呈现，便于AI直接摘取答案",
    "ai-06": "在内页添加面包屑导航（首页 > 分类 > 详情），并标记BreadcrumbList Schema",
    "ai-07": "增加站内相关推荐、产品分类导航、文章链接，确保每个页面至少有3个内部链接指向",
    "ai-08": "验证百度/Google/Bing站长平台，提交sitemap，监控收录和抓取异常",
    "ai-09": "专业概念首次出现先给定义，用具体名称替代模糊指代，一句话讲清一个点，事实写全主语+结论+依据",
    "ai-10": "每个小标题只讲一个要点，按是什么→为什么→怎么做组织板块顺序，过长板块继续拆分到可独立引用",
    "ai-11": "为每个页面撰写独立的Title和Description，包含该页独有的核心关键词，避免模板化复用",
    # 内容质量
    "content-01": "核心页面正文不少于500字，产品/服务页详细介绍功能、优势、应用场景",
    "content-02": "在文章/产品页标注发布时间和最后更新时间，使用ISO 8601格式",
    "content-03": "整理客户高频问题，创建FAQ板块，每个问题用h3，答案紧跟其后，并加FAQPage Schema",
    "content-04": "重要图片（产品图、分类图、logo、案例图）添加描述性alt；装饰性小图标用alt=\"\"",
    "content-05": "将'点击这里''了解更多'改为描述性锚文本，如'查看GEO优化服务''阅读客户案例'",
    "content-06": "多语言站点添加hreflang标签，指定每个语言版本的URL，避免互相竞争",
    "content-07": "先明确每页核心关键词对应的用户意图（学知识/比方案/直接买），再让内容类型与之一一对应",
    "content-08": "为每个页面分配差异化的核心关键词，主题相近的页面用canonical或内链锚文本区分主次，建立关键词矩阵",
    # 权威信任
    "auth-01": "页脚添加ICP备案号、公司地址、电话/邮箱、隐私政策链接，律所/金融等行业需加执业许可证",
    "auth-02": "添加客户评价、客户Logo墙、成功案例、数据成果（服务XX家客户、满意度XX%）",
    "auth-03": "开通微信公众号、知乎、抖音等官方账号，在官网添加链接，保持内容同步更新",
    "auth-04": "通过行业媒体投稿、合作伙伴友链、公益活动等方式获取权威网站外链",
    "auth-05": "创建百度百科/维基百科条目，确保品牌信息一致，争取AI搜索知识面板展示",
    "auth-06": "将官网内容同步分发到知乎、微信公众号、头条等平台，扩大品牌在AI训练数据中的覆盖",
    "auth-07": "为内容标注作者姓名与专业资质，补充第一手经验/实测/服务案例，关键数据标注来源，建立编辑审核机制",
    # SEO基础五项修复建议已随死数据一并清除（2026-09-03 阿栈），并入映射见 IMPACT_MAP 上方注释
    # B端增强
    "b2b-01": "为每个产品/服务创建独立详情页，包含功能介绍、优势、价格区间、应用场景、FAQ",
    "b2b-02": "创建客户案例页，包含客户背景、挑战、解决方案、成果数据，配客户Logo和评价",
    "b2b-03": "按目标行业创建解决方案页，如'制造业GEO方案''电商GEO方案'，覆盖行业关键词",
    "b2b-04": "创建竞品对比页、选型指南、价格对比表，截获用户决策阶段的搜索流量",
    "b2b-05": "技术文档采用清晰目录结构、代码示例、API说明，添加llms.txt引用，确保AI可抓取",
    "b2b-06": "针对'多少钱''怎么部署''安全吗''和XX比怎么样'等高意向问题创建问答内容",
    "b2b-07": "按决策阶段设计分层CTA（科普页给资料下载、产品页给预约演示、高意向给咨询），提供多种低门槛联系入口",
    "b2b-08": "在页脚固定电话/微信/二维码，关键页面放咨询表单或在线客服，确保链接真实可点、至少两种通道",
    "b2b-09": "首屏、产品/服务、案例页各放一个明确按钮（免费咨询/预约到店/立即购买等），链接指向真实表单或联系方式",
    "b2b-10": "补齐价格或获取报价方式、购买/合作流程、服务流程、售后承诺、服务范围/配送/门店等信息",
    # 内页多页聚合扫描（2026-09-05 整改新增）
    "mp-01": "为每个栏目/详情页填写独立标题(15-80字)与描述(50-200字)，不要复用首页 SEO 字段",
    "mp-02": "每页只保留1个H1，正文小节标题统一改为 H2/H3",
    "mp-03": "文章加 Article、产品加 Product、案例加 Article/Service 结构化标记",
    "mp-04": "充实内页正文，建议≥500字，补足参数、场景、细节，避免只有标题和图片",
    "mp-05": "为内页所有重要图片补非空、描述性的 alt",
    "mp-06": "补齐关于、产品/服务、FAQ、隐私政策等必备页，优先建立可被AI摘取的FAQ问答页",
    "mp-07": "把关于/介绍页做成含公司主体、沿革、团队、资质的独立实体页，导航指向独立URL而非首页锚点",
    "auth-08": "先定一份唯一的官方事实表（主体全称、成立年、规模、对外电话、官网标准写法），再同步改正文、页脚与隐形名片",
}


class BaseChecker(ABC):
    """检查器基类"""

    category: Category = Category.TECHNICAL

    def __init__(self, fetcher: Fetcher, page: PageData, extra_pages=None,
                 ai_engine=None, all_site_urls=None, sitemap_article_urls=None):
        self.fetcher = fetcher
        self.page = page
        # 多页面抽样：除首页外抓取到的内页 PageData 列表（跨页面检查用）
        self.extra_pages = extra_pages or []
        self.all_pages = [page] + self.extra_pages
        # 全站 URL 清单（轻量层 collect_all_urls 产出，含整站可达页面，
        # 供"必备页面完备性"等需要全站清单的检查用；未传入时为 None 则回退 all_pages）
        self.all_site_urls = all_site_urls or []
        # 站点地图「文章清单」子地图声明的文章页（站长认定 + 引擎解析，
        # 供内容页挑选器识别真文章，天然排除栏目列表页；未传入时为空集）
        self.sitemap_article_urls = set(sitemap_article_urls or ())
        self.base_url = self._get_base_url()
        # AI语义引擎（可选，用于AI层检查）
        self.ai_engine = ai_engine
        # 独立soup副本：从page.html重新解析，不受AI引擎decompose影响
        self._local_soup = None

    @property
    def local_soup(self):
        """独立解析的soup副本，规则检查专用，不受AI引擎操作影响"""
        if self._local_soup is None:
            from bs4 import BeautifulSoup
            self._local_soup = BeautifulSoup(self.page.html, "lxml")
        return self._local_soup

    def get_text(self) -> str:
        """从独立副本获取页面纯文本"""
        return self.local_soup.get_text(separator=" ", strip=True)

    def _get_base_url(self) -> str:
        from urllib.parse import urlparse
        parsed = urlparse(self.page.final_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _result(self, item: CheckItem, status: Status, message: str,
                detail: str = None, score: float = None,
                skip_kind: str = "na", pages: list = None) -> CheckResult:
        """构造检查结果"""
        if score is None:
            score = {Status.PASS: 1.0, Status.WARN: 0.5, Status.FAIL: 0.0, Status.SKIP: 0.0}[status]
        return CheckResult(
            item_id=item.id,
            item_name=item.name,
            category=item.category,
            priority=item.priority,
            status=status,
            message=message,
            detail=detail,
            score=score,
            manual_check=item.manual_check,
            purpose=item.purpose,
            impact=item.impact,
            scope=item.scope,
            applicable_pages=item.applicable_pages,
            suggestion=item.suggestion,
            excellent=item.excellent,
            normal=item.normal,
            failed=item.failed,
            tier=item.tier,
            description=item.description,
            check_method=item.check_method,
            standard=item.standard,
            skip_kind=skip_kind,
            pages=pages,
        )

    def _item(self, id: str, name: str, priority: Priority,
              description: str, check_method: str, standard: str,
              manual_check: str = "", purpose: str = "", impact: str = "",
              scope: str = "", applicable_pages: str = "", suggestion: str = "",
              excellent: str = "", normal: str = "", failed: str = "",
              tier: str = "core") -> CheckItem:
        # 如果没传 impact，从全局字典查找
        if not impact:
            impact = IMPACT_MAP.get(id, "")
        # 如果没传 scope，从全局字典查找，默认首页
        if not scope:
            scope = SCOPE_MAP.get(id, "首页")
        # 如果没传 applicable_pages，从全局字典查找，默认首页
        if not applicable_pages:
            applicable_pages = APPLICABLE_PAGES_MAP.get(id, "首页")
        # 如果没传 suggestion，从全局字典查找
        if not suggestion:
            suggestion = SUGGESTION_MAP.get(id, "")
        # 管理页"优先级配置"的自定义覆盖：生效优先级改变计分权重与报告排序
        priority = Priority(priority_config.effective_priority(id, priority))
        return CheckItem(
            id=id, name=name, category=self.category, priority=priority,
            description=description, check_method=check_method, standard=standard,
            manual_check=manual_check, purpose=purpose, impact=impact,
            scope=scope, applicable_pages=applicable_pages, suggestion=suggestion,
            excellent=excellent, normal=normal, failed=failed,
            tier=tier,
        )

    def _skip_if_homepage(self, item: CheckItem) -> CheckResult:
        """如果检查项只适用于内页且当前检测的是首页，返回 SKIP"""
        if item.applicable_pages == "内页":
            return self._result(
                item, Status.SKIP,
                f"{item.name}为内页专属检查，首页不适用，需检测产品页/文章页等内页",
            )
        return None

    def _pick_content_page(self):
        """从抽样内页中挑选最适合做内容检查的页面（文章/案例/产品页）。

        排除与首页正文高度雷同的克隆页（ZSTEK教训：/adventure-news/ 实为首页模板副本，
        曾被当文章页做署名检查，得出对克隆页无意义的结论）；排除后无候选则返回 None，
        由调用方如实跳过。
        2026-09-10 客户反馈补充：署名/语义等要查的是文章详情页，不是栏目列表页
        （ZSTEK再次教训：/adventure-news/ 是新闻列表页却被当文章查署名）。
        站点地图「文章清单」子地图声明的文章页是最高优先候选——那是站长认定、
        引擎可验证的真文章；栏目列表页在这类主题下与文章页结构难辨（实测
        article/time 标签、链接密度均不可分），不再用结构启发式硬猜。
        整站抓不到声明文章/合格候选时返回 None，各内容页检查项如实标「未检查·需人工复核」。
        """
        if not self.extra_pages:
            return None
        import re
        home_text = self.page.get_text()

        def _is_home_clone(p) -> bool:
            a = {home_text[i:i + 2] for i in range(len(home_text) - 1)}
            p_text = p.get_text()
            b = {p_text[i:i + 2] for i in range(len(p_text) - 1)}
            if not a or not b:
                return False
            return len(a & b) / len(a | b) > 0.85

        candidates = [p for p in self.extra_pages
                      if p.word_count() > 100 and not _is_home_clone(p)]
        if not candidates:
            return None
        # 站点地图声明的文章页最高优先（去尾斜杠比对）
        if self.sitemap_article_urls:
            declared = {u.rstrip("/") for u in self.sitemap_article_urls}
            in_sitemap = [p for p in candidates
                          if p.final_url.rstrip("/") in declared]
            if in_sitemap:
                return max(in_sitemap, key=lambda p: p.word_count())
        priority_patterns = [
            r"article|blog|news|post|文章|新闻|资讯",
            r"case|customer|story|案例|客户",
            r"product|service|solution|产品|服务|方案",
        ]
        for pattern in priority_patterns:
            for p in candidates:
                if re.search(pattern, p.final_url, re.I):
                    return p
        return max(candidates, key=lambda p: p.word_count(), default=None)

    @staticmethod
    def _is_list_page(page) -> bool:
        """判断页面是否为栏目列表页/着陆页：链接密集且无文章结构特征。

        实测校准（ZSTEK 2026-09-10）：WordPress 主题给列表页也套 article/time 标签、
        链接文字占比普遍低于 0.2，结构启发式不可分；本方法不再用于挑选器硬过滤，
        保留供诊断探针使用。
        """
        soup = page.soup
        if soup is None:
            return False
        if soup.find("article") is not None:
            return False
        if soup.find("time", attrs={"datetime": True}) is not None:
            return False
        if soup.find("meta", attrs={"property": "article:published_time"}) is not None:
            return False
        total_text = page.get_text()
        if not total_text:
            return True
        link_text_len = 0
        for a in soup.find_all("a"):
            link_text_len += len(a.get_text(strip=True))
        return link_text_len / len(total_text) >= 0.5

    @staticmethod
    def _pg(page) -> list:
        """把选中的 PageData 转成可溯源的路径列表（供结果 pages 使用，报告侧再补全成完整链接）。"""
        from urllib.parse import urlparse
        if page is None:
            return []
        return [urlparse(page.final_url).path or "/"]

    @abstractmethod
    def run(self) -> List[CheckResult]:
        """执行所有检查，返回结果列表"""
        pass
