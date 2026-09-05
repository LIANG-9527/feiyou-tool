import fs from 'fs';

const origPath = new URL('../output/audit/feiyougeo.cn_20260904_100007.html', import.meta.url).pathname;
const outPath = new URL('../geo-audit-report-brand/pages/index.html', import.meta.url).pathname;

const orig = fs.readFileSync(origPath, 'utf8');

// Extract body content from the container div
const bodyMatch = orig.match(/<body[^>]*>([\s\S]*)<\/body>/i);
if (!bodyMatch) throw new Error('No body found');
let body = bodyMatch[1];

// Extract content inside <div class="container">
const containerMatch = body.match(/<div class="container"[^>]*>([\s\S]*)<\/div>\s*<\/body>/i) || body.match(/<div class="container"[^>]*>([\s\S]*)<\/div>\s*$/i);
let content = containerMatch ? containerMatch[1] : body;

// Helper to strip HTML tags
function stripTags(html) {
  return html.replace(/<[^>]+>/g, '').replace(/&quot;/g, '"').replace(/&#x27;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&').trim();
}

// Extract sections by HTML comments
function extractSections(html) {
  const sections = [];
  const firstComment = html.search(/<!--\s/);
  if (firstComment > 0) {
    sections.push({ type: 'brand-header', html: html.slice(0, firstComment) });
  }
  const commentRegex = /<!--\s*([^>]*?)\s*-->([\s\S]*?)(?=<!--\s|<\/div>\s*<\/details>|<\/div>\s*$)/g;
  let m;
  while ((m = commentRegex.exec(html)) !== null) {
    sections.push({ type: m[1].trim(), html: m[2] });
  }
  return sections;
}

const sections = extractSections(content);

// Build the new main content
let main = '';

// Brand header
const brandHeader = sections.find(s => s.type === 'brand-header');
let reportDate = '2026-09-04 10:00:07';
if (brandHeader) {
  const dateMatch = brandHeader.html.match(/(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})/);
  if (dateMatch) reportDate = dateMatch[1];
}

main += `
<div class="max-w-[1280px] mx-auto px-4 sm:px-6 lg:px-8 py-6">
  <!-- Brand Header -->
  <header class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-6">
    <div class="flex items-center gap-3">
      <div class="w-10 h-10 rounded-[var(--jl-radius-large)] bg-gradient-to-br from-[var(--jl-blue-600)] to-[var(--jl-blue-800)] flex items-center justify-center text-white font-extrabold text-base shadow-sm">
        极
      </div>
      <div>
        <div class="text-[15px] font-bold text-[var(--jl-neutral-900)]">极联股份 · GEO 诊断中心</div>
        <div class="text-xs text-[var(--jl-neutral-500)]">让 AI 搜索优先推荐你的品牌</div>
      </div>
    </div>
    <div class="text-left sm:text-right">
      <div class="text-xs text-[var(--jl-neutral-500)]">检测时间</div>
      <div class="text-sm font-semibold text-[var(--jl-neutral-700)]">${reportDate}</div>
    </div>
  </header>
`;

// Hero section
main += `
  <!-- Hero -->
  <section class="relative overflow-hidden rounded-2xl bg-gradient-to-br from-[var(--jl-blue-800)] to-[var(--jl-blue-600)] p-6 sm:p-8 lg:p-10 text-white mb-6 shadow-md">
    <div class="relative z-10">
      <div class="inline-flex items-center gap-2 text-sm text-white/80 mb-3">
        <i data-lucide="scan-search" class="w-4 h-4"></i>
        <span>GEO 官网诊断报告</span>
      </div>
      <h1 id="report-title" class="text-2xl sm:text-3xl font-bold text-white mb-2">feiyougeo.cn</h1>
      <p id="report-url" class="text-sm text-white/70">检测页面: https://feiyougeo.cn/</p>
    </div>
    <div class="absolute top-0 right-0 w-64 h-64 bg-white/5 rounded-full -translate-y-1/2 translate-x-1/3 blur-2xl"></div>
  </section>
`;

// Executive summary
main += `
  <!-- Executive Summary -->
  <section id="executive-summary" class="bg-[var(--jl-card)] rounded-2xl p-6 sm:p-8 mb-5 shadow-sm border border-[var(--jl-border)]">
    <div class="flex items-center gap-2 text-sm font-semibold text-[var(--jl-primary)] mb-3">
      <i data-lucide="sparkles" class="w-4 h-4"></i>
      <span>30 秒看懂这份报告</span>
    </div>
    <p class="text-lg sm:text-xl font-bold text-[var(--jl-neutral-900)] leading-relaxed">
      您的网站能被百度搜到，但 AI 搜索（豆包/DeepSeek/元宝）几乎不会引用您——本次体检 46 项，2 项亮红灯，其中最要命的 3 个在下面。
    </p>
  </section>
`;

// Loss estimate card
main += `
  <!-- Loss Estimate -->
  <section class="rounded-2xl p-5 sm:p-6 mb-5 text-white bg-gradient-to-r from-[var(--state-error)] to-[#f87171] shadow-sm">
    <div class="flex items-start gap-3">
      <i data-lucide="trending-down" class="w-6 h-6 mt-0.5 flex-shrink-0"></i>
      <div>
        <p class="text-base sm:text-lg font-bold leading-relaxed mb-1">
          按当前状态估算，您每月约少 <span class="inline-block min-w-[80px] border-b-2 border-dashed border-white/80">&nbsp;</span> 次有效客户咨询
        </p>
        <p class="text-xs sm:text-sm text-white/85">
          此处数字将按您所在行业的搜索量数据填入（口径：行业月搜索量 × AI 引用缺失比例 × 咨询转化率），填数前本报告不估算。
        </p>
      </div>
    </div>
  </section>
`;

// Problem cards
const problemCards = [];
const problemRegex = /<div[^>]*border-left:6px solid #ef4444;[^>]*>([\s\S]*?)<\/div>\s*(?=<div[^>]*border-left:6px solid #ef4444;|<\/div>\s*<!-- 技术明细)/g;
let pm;
while ((pm = problemRegex.exec(content)) !== null) {
  problemCards.push(pm[1]);
}

main += `
  <!-- Problem Cards -->
  <section class="space-y-4 mb-6">
`;

problemCards.forEach((cardHtml, idx) => {
  const titleMatch = cardHtml.match(/<div[^>]*font-size:17px[^>]*>([\s\S]*?)<\/div>/);
  const title = titleMatch ? stripTags(titleMatch[1]) : `问题 ${idx + 1}`;

  // Extract evidence/impact/fix blocks
  const evidenceMatch = cardHtml.match(/证据（您网站上的真实情况）<\/div>([\s\S]*?)<\/div>\s*<\/div>/);
  const impactMatch = cardHtml.match(/对您生意的影响<\/div>([\s\S]*?)<\/div>\s*<\/div>/);
  const fixMatch = cardHtml.match(/修改示例（改前 → 改后）<\/div>([\s\S]*?)<\/div>\s*<\/div>/);

  const evidence = evidenceMatch ? stripTags(evidenceMatch[1]).replace(/出自：.*/, '').trim() : '';
  const evidenceSource = evidenceMatch ? (evidenceMatch[1].match(/出自：([^<]+)/) || [])[1] : '';
  const impact = impactMatch ? stripTags(impactMatch[1]) : '';
  const fix = fixMatch ? stripTags(fixMatch[1]) : '';

  main += `
    <article class="relative bg-[var(--jl-card)] rounded-2xl p-5 sm:p-6 shadow-sm border border-[var(--jl-border)] overflow-hidden">
      <div class="absolute left-0 top-0 bottom-0 w-1.5 bg-[var(--state-error)]"></div>
      <h2 class="text-lg font-bold text-[var(--jl-neutral-900)] mb-4 pl-3">${title}</h2>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4 pl-3">
        <div class="rounded-xl p-4 bg-[var(--state-error-bg)] border border-[var(--state-error)]/10">
          <div class="flex items-center gap-2 text-xs font-bold text-[var(--state-error-text)] mb-2">
            <i data-lucide="file-search" class="w-3.5 h-3.5"></i>
            <span>证据（您网站上的真实情况）</span>
          </div>
          <p class="text-sm text-[var(--state-error-text)] leading-relaxed">${evidence}</p>
          ${evidenceSource ? `<p class="text-xs text-[var(--state-error-text)]/70 mt-2 break-all">出自：${evidenceSource}</p>` : ''}
        </div>
        <div class="rounded-xl p-4 bg-[var(--state-warning-bg)] border border-[var(--state-warning)]/10">
          <div class="flex items-center gap-2 text-xs font-bold text-[var(--state-warning-text)] mb-2">
            <i data-lucide="alert-triangle" class="w-3.5 h-3.5"></i>
            <span>对您生意的影响</span>
          </div>
          <p class="text-sm text-[var(--state-warning-text)] leading-relaxed">${impact}</p>
        </div>
      </div>
      <div class="rounded-xl p-4 bg-[var(--state-success-bg)] border border-[var(--state-success)]/10 pl-3 ml-0">
        <div class="flex items-center gap-2 text-xs font-bold text-[var(--state-success-text)] mb-2">
          <i data-lucide="check-circle-2" class="w-3.5 h-3.5"></i>
          <span>修改示例（改前 → 改后）</span>
        </div>
        <p class="text-sm text-[var(--state-success-text)] leading-relaxed">${fix}</p>
      </div>
    </article>
  `;
});

main += `
  </section>
`;

// Technical details (collapsible)
main += `
  <!-- Technical Details -->
  <details class="group mb-8">
    <summary class="cursor-pointer list-none bg-[var(--jl-card)] rounded-2xl p-5 sm:p-6 shadow-sm border border-[var(--jl-border)] flex items-center justify-between gap-4">
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-[var(--jl-radius-large)] bg-[var(--jl-blue-50)] text-[var(--jl-primary)] flex items-center justify-center">
          <i data-lucide="settings-2" class="w-5 h-5"></i>
        </div>
        <div>
          <h2 class="text-base sm:text-lg font-bold text-[var(--jl-neutral-900)]">技术明细</h2>
          <p class="text-xs text-[var(--jl-muted-foreground)]">46 项全量打分表与检测证据，点击展开</p>
        </div>
      </div>
      <i data-lucide="chevron-down" class="w-5 h-5 text-[var(--jl-neutral-500)] transition-transform group-open:rotate-180"></i>
    </summary>
    <div class="pt-5 space-y-6">
`;

// Total score overview
main += `
      <!-- Total Score -->
      <section id="total-score" class="grid grid-cols-1 lg:grid-cols-3 gap-5">
        <div class="lg:col-span-1 bg-[var(--jl-card)] rounded-2xl p-6 text-center shadow-sm border border-[var(--jl-border)]">
          <div class="text-5xl font-extrabold text-[var(--state-warning)] leading-none">72.1</div>
          <div class="text-sm text-[var(--jl-neutral-500)] mt-2">总分 (0-100)</div>
          <div class="mt-4 inline-flex items-center gap-1.5 px-4 py-1.5 rounded-full text-lg font-bold text-white bg-[var(--state-warning)]">
            C 级
          </div>
        </div>
        <div class="lg:col-span-2 bg-[var(--jl-card)] rounded-2xl p-6 shadow-sm border border-[var(--jl-border)]">
          <h3 class="text-sm font-semibold text-[var(--jl-neutral-700)] mb-4">检测概览</h3>
          <div class="grid grid-cols-4 gap-3 text-center">
            <div class="rounded-xl p-3 bg-[var(--jl-muted)]">
              <div class="text-2xl font-bold text-[var(--jl-neutral-700)]">46</div>
              <div class="text-xs text-[var(--jl-neutral-500)]">总检查项</div>
            </div>
            <div class="rounded-xl p-3 bg-[var(--state-success-bg)] border border-[var(--state-success)]/10">
              <div class="text-2xl font-bold text-[var(--state-success)]">23</div>
              <div class="text-xs text-[var(--state-success-text)]">通过</div>
            </div>
            <div class="rounded-xl p-3 bg-[var(--state-warning-bg)] border border-[var(--state-warning)]/10">
              <div class="text-2xl font-bold text-[var(--state-warning)]">21</div>
              <div class="text-xs text-[var(--state-warning-text)]">警告</div>
            </div>
            <div class="rounded-xl p-3 bg-[var(--state-error-bg)] border border-[var(--state-error)]/10">
              <div class="text-2xl font-bold text-[var(--state-error)]">2</div>
              <div class="text-xs text-[var(--state-error-text)]">失败</div>
            </div>
          </div>
        </div>
      </section>
`;

// Dimension scores
const dimScores = [
  { name: '技术可抓取性', score: 73.1, pass: 8, warn: 3, fail: 2 },
  { name: 'AI友好度专项', score: 72.7, pass: 5, warn: 6, fail: 0 },
  { name: '内容质量与结构化', score: 68.8, pass: 3, warn: 5, fail: 0 },
  { name: '权威与信任信号', score: 71.4, pass: 3, warn: 4, fail: 0 },
  { name: '商业转化与决策内容', score: 78.6, pass: 4, warn: 3, fail: 0 }
];

main += `
      <!-- Dimension Scores -->
      <section class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4">
`;
dimScores.forEach(dim => {
  const barColor = dim.score >= 75 ? 'var(--state-success)' : (dim.score >= 60 ? 'var(--state-warning)' : 'var(--state-error)');
  main += `
        <div class="bg-[var(--jl-card)] rounded-2xl p-5 shadow-sm border border-[var(--jl-border)]">
          <div class="flex justify-between items-center mb-3">
            <span class="text-sm font-semibold text-[var(--jl-neutral-700)]">${dim.name}</span>
            <span class="text-xl font-bold text-[var(--jl-neutral-900)]">${dim.score}</span>
          </div>
          <div class="h-2 rounded-full bg-[var(--jl-neutral-200)] overflow-hidden">
            <div class="h-full rounded-full" style="width: ${dim.score}%; background-color: ${barColor};"></div>
          </div>
          <div class="mt-3 text-xs text-[var(--jl-neutral-500)]">通过 ${dim.pass} · 警告 ${dim.warn} · 失败 ${dim.fail}</div>
        </div>
  `;
});
main += `
      </section>
`;

// Scoring logic
main += `
      <!-- Scoring Logic -->
      <section class="bg-[var(--jl-card)] rounded-2xl p-6 shadow-sm border border-[var(--jl-border)]">
        <h3 class="text-base font-bold text-[var(--jl-neutral-900)] mb-5 flex items-center gap-2">
          <span class="w-1 h-5 rounded-full bg-[var(--jl-primary)]"></span>
          打分逻辑说明
        </h3>
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <div>
            <h4 class="text-sm font-semibold text-[var(--jl-neutral-700)] mb-3">一、维度权重与得分</h4>
            <div class="overflow-x-auto rounded-xl border border-[var(--jl-border)]">
              <table class="w-full text-sm">
                <thead class="bg-[var(--jl-muted)] text-[var(--jl-neutral-500)]">
                  <tr>
                    <th class="px-4 py-2.5 text-left font-medium">维度</th>
                    <th class="px-4 py-2.5 text-center font-medium">权重</th>
                    <th class="px-4 py-2.5 text-center font-medium">检查项</th>
                    <th class="px-4 py-2.5 text-center font-medium">得分</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-[var(--jl-border)]">
                  <tr><td class="px-4 py-2.5 text-[var(--jl-neutral-700)]">技术可抓取性</td><td class="px-4 py-2.5 text-center font-semibold text-[var(--jl-neutral-900)]">30%</td><td class="px-4 py-2.5 text-center text-[var(--jl-neutral-500)]">13 项</td><td class="px-4 py-2.5 text-center font-bold text-[var(--state-warning)]">73.1</td></tr>
                  <tr><td class="px-4 py-2.5 text-[var(--jl-neutral-700)]">AI友好度专项</td><td class="px-4 py-2.5 text-center font-semibold text-[var(--jl-neutral-900)]">30%</td><td class="px-4 py-2.5 text-center text-[var(--jl-neutral-500)]">11 项</td><td class="px-4 py-2.5 text-center font-bold text-[var(--state-warning)]">72.7</td></tr>
                  <tr><td class="px-4 py-2.5 text-[var(--jl-neutral-700)]">内容质量与结构化</td><td class="px-4 py-2.5 text-center font-semibold text-[var(--jl-neutral-900)]">20%</td><td class="px-4 py-2.5 text-center text-[var(--jl-neutral-500)]">8 项</td><td class="px-4 py-2.5 text-center font-bold text-[var(--state-warning)]">68.8</td></tr>
                  <tr><td class="px-4 py-2.5 text-[var(--jl-neutral-700)]">权威与信任信号</td><td class="px-4 py-2.5 text-center font-semibold text-[var(--jl-neutral-900)]">15%</td><td class="px-4 py-2.5 text-center text-[var(--jl-neutral-500)]">7 项</td><td class="px-4 py-2.5 text-center font-bold text-[var(--state-warning)]">71.4</td></tr>
                  <tr><td class="px-4 py-2.5 text-[var(--jl-neutral-700)]">商业转化与决策内容</td><td class="px-4 py-2.5 text-center font-semibold text-[var(--jl-neutral-900)]">5%</td><td class="px-4 py-2.5 text-center text-[var(--jl-neutral-500)]">7 项</td><td class="px-4 py-2.5 text-center font-bold text-[var(--jl-blue-500)]">78.6</td></tr>
                </tbody>
              </table>
            </div>
            <p class="text-xs text-[var(--jl-neutral-400)] mt-2">总分 = Σ(各维度得分 × 维度权重)</p>
          </div>
          <div>
            <h4 class="text-sm font-semibold text-[var(--jl-neutral-700)] mb-3">二、单项计分规则</h4>
            <div class="grid grid-cols-4 gap-2 mb-5">
              <div class="rounded-xl p-3 text-center bg-[var(--state-success-bg)] border border-[var(--state-success)]/10">
                <div class="text-lg font-bold text-[var(--state-success)]">1分</div>
                <div class="text-xs text-[var(--state-success-text)]">通过</div>
              </div>
              <div class="rounded-xl p-3 text-center bg-[var(--state-warning-bg)] border border-[var(--state-warning)]/10">
                <div class="text-lg font-bold text-[var(--state-warning)]">0.5分</div>
                <div class="text-xs text-[var(--state-warning-text)]">警告</div>
              </div>
              <div class="rounded-xl p-3 text-center bg-[var(--state-error-bg)] border border-[var(--state-error)]/10">
                <div class="text-lg font-bold text-[var(--state-error)]">0分</div>
                <div class="text-xs text-[var(--state-error-text)]">失败</div>
              </div>
              <div class="rounded-xl p-3 text-center bg-[var(--jl-muted)] border border-[var(--jl-border)]">
                <div class="text-lg font-bold text-[var(--jl-neutral-500)]">--</div>
                <div class="text-xs text-[var(--jl-neutral-600)]">未检查</div>
              </div>
            </div>
            <h4 class="text-sm font-semibold text-[var(--jl-neutral-700)] mb-3">三、等级划分标准</h4>
            <div class="grid grid-cols-5 gap-2">
              <div class="rounded-lg p-2 text-center bg-[var(--state-success-bg)] border border-[var(--state-success)]/10">
                <div class="text-base font-bold text-[var(--state-success)]">A</div>
                <div class="text-[10px] text-[var(--state-success-text)]">90-100</div>
              </div>
              <div class="rounded-lg p-2 text-center bg-[var(--jl-blue-50)] border border-[var(--jl-blue-200)]">
                <div class="text-base font-bold text-[var(--jl-blue-500)]">B</div>
                <div class="text-[10px] text-[var(--jl-blue-700)]">75-89</div>
              </div>
              <div class="rounded-lg p-2 text-center bg-[var(--state-warning-bg)] border border-[var(--state-warning)]/10">
                <div class="text-base font-bold text-[var(--state-warning)]">C</div>
                <div class="text-[10px] text-[var(--state-warning-text)]">60-74</div>
              </div>
              <div class="rounded-lg p-2 text-center bg-orange-50 border border-orange-200">
                <div class="text-base font-bold text-orange-500">D</div>
                <div class="text-[10px] text-orange-700">40-59</div>
              </div>
              <div class="rounded-lg p-2 text-center bg-[var(--state-error-bg)] border border-[var(--state-error)]/10">
                <div class="text-base font-bold text-[var(--state-error)]">F</div>
                <div class="text-[10px] text-[var(--state-error-text)]">0-39</div>
              </div>
            </div>
          </div>
        </div>
      </section>
`;

// Helper to extract table rows from original tables
function extractRows(tableHtml) {
  const rows = [];
  const rowRegex = /<tr[^>]*>([\s\S]*?)<\/tr>/g;
  let rm;
  while ((rm = rowRegex.exec(tableHtml)) !== null) {
    const cells = [];
    const cellRegex = /<t(?:h|d)[^>]*>([\s\S]*?)<\/t(?:h|d)>/g;
    let cm;
    while ((cm = cellRegex.exec(rm[1])) !== null) {
      cells.push(cm[1]);
    }
    rows.push(cells);
  }
  return rows;
}

function renderStatusBadge(text) {
  const t = text.trim();
  if (t === '通过') return `<span class="inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold bg-[var(--state-success-bg)] text-[var(--state-success-text)] border border-[var(--state-success)]/10"><i data-lucide="check" class="w-3 h-3 mr-1"></i>通过</span>`;
  if (t === '警告') return `<span class="inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold bg-[var(--state-warning-bg)] text-[var(--state-warning-text)] border border-[var(--state-warning)]/10"><i data-lucide="alert-circle" class="w-3 h-3 mr-1"></i>警告</span>`;
  if (t === '失败') return `<span class="inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold bg-[var(--state-error-bg)] text-[var(--state-error-text)] border border-[var(--state-error)]/10"><i data-lucide="x" class="w-3 h-3 mr-1"></i>失败</span>`;
  return `<span class="inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold bg-[var(--jl-muted)] text-[var(--jl-neutral-600)] border border-[var(--jl-border)]">${t}</span>`;
}

function cleanCell(html) {
  return html
    .replace(/style="[^"]*"/g, '')
    .replace(/class="[^"]*"/g, '')
    .replace(/<div>\s*<\/div>/g, '')
    .replace(/<span>\s*<\/span>/g, '')
    .trim();
}

function transformCell(html) {
  let h = cleanCell(html);
  // Replace status spans
  h = h.replace(/<span[^>]*>\s*(通过|警告|失败)\s*<\/span>/g, (m, status) => renderStatusBadge(status));
  // Replace priority badges P0/P1/P2
  h = h.replace(/<span[^>]*background:#6366f1[^>]*>\s*(P[0-9])\s*<\/span>/g, '<span class="inline-flex items-center px-1.5 py-0.5 rounded-md text-[10px] font-semibold bg-[var(--jl-primary)] text-white">$1</span>');
  // Replace scope badges
  h = h.replace(/<span[^>]*background:#f5f3ff[^>]*color:#8b5cf6[^>]*>([^<]+)<\/span>/g, '<span class="inline-flex items-center px-1.5 py-0.5 rounded-md text-[10px] font-semibold bg-[var(--jl-blue-50)] text-[var(--jl-blue-700)]">$1</span>');
  h = h.replace(/<span[^>]*background:#ecfdf5[^>]*color:#10b981[^>]*>([^<]+)<\/span>/g, '<span class="inline-flex items-center px-1.5 py-0.5 rounded-md text-[10px] font-semibold bg-[var(--state-success-bg)] text-[var(--state-success-text)]">$1</span>');
  h = h.replace(/<span[^>]*background:#fffbeb[^>]*color:#f59e0b[^>]*>([^<]+)<\/span>/g, '<span class="inline-flex items-center px-1.5 py-0.5 rounded-md text-[10px] font-semibold bg-[var(--state-warning-bg)] text-[var(--state-warning-text)]">$1</span>');
  h = h.replace(/<span[^>]*background:#f0f9ff[^>]*color:#0ea5e9[^>]*>([^<]+)<\/span>/g, '<span class="inline-flex items-center px-1.5 py-0.5 rounded-md text-[10px] font-semibold bg-[var(--state-info-bg)] text-[var(--state-info-text)]">$1</span>');
  // Replace page tags
  h = h.replace(/<span[^>]*font-size:10px[^>]*color:#9ca3af[^>]*>([^<]+)<\/span>/g, '<span class="text-[10px] text-[var(--jl-neutral-400)]">$1</span>');
  // Remove empty divs
  h = h.replace(/<div>\s*<\/div>/g, '');
  return h;
}

function renderDetailTable(tableHtml, sectionTitle) {
  const rows = extractRows(tableHtml);
  if (rows.length < 2) return '';
  const header = rows[0];
  const dataRows = rows.slice(1);

  let out = `
      <section>
        <h3 class="text-base font-bold text-[var(--jl-neutral-900)] mb-4 flex items-center gap-2">
          <span class="w-1 h-5 rounded-full bg-[var(--jl-primary)]"></span>
          ${sectionTitle}
        </h3>
        <div class="overflow-x-auto rounded-2xl border border-[var(--jl-border)] bg-[var(--jl-card)] shadow-sm">
          <table class="w-full min-w-[1100px] text-sm">
            <thead class="bg-[var(--jl-muted)] text-[var(--jl-neutral-500)]">
              <tr>
`;
  header.forEach((cell, i) => {
    const align = i === 1 ? 'text-center' : 'text-left';
    out += `                <th class="${align} px-4 py-3 font-medium whitespace-nowrap">${cleanCell(cell)}</th>\n`;
  });
  out += `              </tr>
            </thead>
            <tbody class="divide-y divide-[var(--jl-border)]">
`;
  dataRows.forEach(row => {
    out += `              <tr class="align-top">\n`;
    row.forEach((cell, i) => {
      const align = i === 1 ? 'text-center' : 'text-left';
      out += `                <td class="${align} px-4 py-3 text-[var(--jl-neutral-700)]">${transformCell(cell)}</td>\n`;
    });
    out += `              </tr>\n`;
  });
  out += `            </tbody>
          </table>
        </div>
      </section>
`;
  return out;
}

// CMS diagnosis
const cmsMatch = content.match(/CMS诊断与接手评估[\s\S]*?<\/table>/);
if (cmsMatch) {
  main += renderDetailTable(cmsMatch[0], 'CMS诊断与接手评估');
}

// Sampled pages
const sampledMatch = content.match(/多页面抽样覆盖[\s\S]*?<\/table>/);
if (sampledMatch) {
  const rows = extractRows(sampledMatch[0]);
  main += `
      <!-- Sampled Pages -->
      <section>
        <h3 class="text-base font-bold text-[var(--jl-neutral-900)] mb-2 flex items-center gap-2">
          <span class="w-1 h-5 rounded-full bg-[var(--jl-blue-400)]"></span>
          多页面抽样覆盖（共 12 个内页）
        </h3>
        <p class="text-xs text-[var(--jl-neutral-500)] mb-4">除首页外，按 产品/方案 &gt; 案例 &gt; 资讯 &gt; 关于 的优先级抽样抓取内页；断链检测、Title/Description唯一性、关键词蚕食基于这些页面跨页分析</p>
        <div class="overflow-x-auto rounded-2xl border border-[var(--jl-border)] bg-[var(--jl-card)] shadow-sm">
          <table class="w-full min-w-[700px] text-sm">
            <thead class="bg-[var(--jl-muted)] text-[var(--jl-neutral-500)]">
              <tr>
                <th class="text-left px-4 py-3 font-medium w-12">#</th>
                <th class="text-left px-4 py-3 font-medium">页面标题</th>
                <th class="text-left px-4 py-3 font-medium">URL</th>
                <th class="text-center px-4 py-3 font-medium">状态</th>
                <th class="text-center px-4 py-3 font-medium">大小</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[var(--jl-border)]">
`;
  rows.slice(1).forEach(row => {
    main += `              <tr class="align-top">\n`;
    main += `                <td class="text-left px-4 py-2.5 text-[var(--jl-neutral-500)] whitespace-nowrap">${cleanCell(row[0])}</td>\n`;
    main += `                <td class="text-left px-4 py-2.5 text-[var(--jl-neutral-900)] max-w-xs break-words">${cleanCell(row[1])}</td>\n`;
    main += `                <td class="text-left px-4 py-2.5 text-[var(--jl-primary)] max-w-sm break-all text-xs">${cleanCell(row[2])}</td>\n`;
    main += `                <td class="text-center px-4 py-2.5 font-semibold text-[var(--state-success)]">${cleanCell(row[3])}</td>\n`;
    main += `                <td class="text-center px-4 py-2.5 text-[var(--jl-neutral-500)] whitespace-nowrap">${cleanCell(row[4])}</td>\n`;
    main += `              </tr>\n`;
  });
  main += `            </tbody>
          </table>
        </div>
      </section>
`;
}

// Todo list
const todoMatch = content.match(/待优化清单[\s\S]*?<\/table>/);
if (todoMatch) {
  main += renderDetailTable(todoMatch[0], '待优化清单');
}

// Detailed results sections
const detailMatch = content.match(/详细诊断结果[\s\S]*?<\/details>/);
if (detailMatch) {
  // Extract each subsection
  const subSectionRegex = /<h3[^>]*>([^<]+)<\/h3>[\s\S]*?<table[^>]*>([\s\S]*?)<\/table>/g;
  let sm;
  while ((sm = subSectionRegex.exec(detailMatch[0])) !== null) {
    main += renderDetailTable(sm[2], sm[1].trim());
  }
}

main += `
    </div>
  </details>

  <!-- Footer -->
  <footer class="mt-8 pt-6 border-t border-[var(--jl-border)] text-center text-xs text-[var(--jl-neutral-400)]">
    <p>GEO 官网诊断工具 · 本报告基于自动化检测生成，仅供参考</p>
  </footer>
</div>
`;

// Read the skeleton
let skeleton = fs.readFileSync(outPath, 'utf8');

// Replace the empty <main></main> with our content
skeleton = skeleton.replace(/<main>\s*<\/main>/, `<main>\n${main}\n    </main>`);

fs.writeFileSync(outPath, skeleton, 'utf8');
console.log('Written branded report to', outPath);
