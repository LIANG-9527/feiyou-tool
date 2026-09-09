# 独立需求单：F3 站点收录比（sitemap URL 数 vs GSC 实际收录数）

> 来源：SEO 检测工具自查清单 F3。经业务拍板，**本次不并入体检工具**，列为独立需求单。

## 需求描述
将 sitemap 声明的 URL 数与 Google Search Console（GSC）实际收录数对比，比例异常（如收录数远低于声明数）时提示收录障碍。

## 前置条件
- 站长授权 GSC 数据访问（API 或定期导出）；
- 站点已验证 GSC 所有权。

## 依赖的外部系统
- Google Search Console URL Inspection API / Search Analytics API。

## 建议实现
- 体检报告增加可选字段"GSC 收录比"，需站长提供授权后展示；
- 未授权时报告显示"未接入 GSC，收录比待站长提供数据"。

## 优先级建议
P3（增强项）。核心体检流程不依赖该数据。
