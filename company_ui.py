"""企业资料页：公司档案库（SQLite company 表）的列表 + 详情视图。

路由本体在 main.py（/company、/api/company/list、/api/company/detail），
本模块只管渲染。详情页按内容模块 div 区块展示，字段全中文标签，未知字段兜底直显。
删除交互与官网诊断报告列表同款：×按钮两段式确认 + 管理密码弹窗（同款样式）。
"""

DETAIL_JS = """
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function val(v){
  if(v==null||v==='')return '';
  if(Array.isArray(v))return v.join('；');
  if(typeof v==='object')return JSON.stringify(v);
  return String(v);
}
function raw(s){return {__raw:String(s==null?'':s)};}
function block(title,rows){
  const items=rows.filter(r=>val(r[1])!=='');
  if(!items.length)return '';
  return '<div class="blk"><div class="blk-t">'+title+'</div>'+
    items.map(r=>'<div class="kv"><span class="k">'+r[0]+'</span><span class="v">'+
      (r[1]&&r[1].__raw!=null?r[1].__raw:esc(Array.isArray(r[1])?r[1].join('、'):val(r[1])))+'</span></div>').join('')+
    '</div>';
}
const LAB={full_name:'公司全称',credit_code:'统一社会信用代码',legal_person:'法定代表人',founder:'创始人',
 founded:'成立时间',address:'注册地址/总部',industry:'所属行业',industry_position:'行业地位',
 listed:'上市状态',employees:'员工规模',revenue:'年营收',qualifications:'资质认证',patents:'专利/软著',
 status:'经营状态',reg_capital:'注册资本',company_type:'公司类型',business_scope:'经营范围',products:'主要产品',
 brands:'品牌名',aliases:'别名',nature:'企业性质',domains:'官网域名',
 license_number:'营业执照号',org_code:'组织机构代码',regit_address:'注册地址(原文)',province:'省份',
 city:'城市',district:'区县',industry_code:'行业代码',staff_number:'从业人数',social_security_num:'参保人数',
 social_security_from:'参保人数年份',contributed_capital:'实缴资本',annual_turnover:'年营业额',
 report_tax_from:'纳税申报年份',report_tax_total:'纳税总额',approve_date:'核准日期',revoke_date:'吊销日期',
 business_period:'营业期限',issuing_authority:'登记机关',address_last_change:'地址最近变更',
 company_profile:'企业简介',history_names:'曾用名',api_id:'工商库ID'};
let currentDetailId='';
async function loadDetail(id){
  currentDetailId=id;
  const r=await fetch('/api/company/detail?id='+encodeURIComponent(id));
  const d=await r.json();
  if(d.error){document.getElementById('main').innerHTML='<div class="empty">'+d.error+'</div>';return;}
  const info=d.info||{};
  const used=new Set(Object.keys(LAB));
  const extra=Object.entries(info).filter(([k])=>!used.has(k));
  // 企业性质：读档案存储结论（derive_nature 单一口径，SPEC-company-nature-unify），前端不再复刻规则
  const _nature=val(info.nature);
  let html='<div class="crumb"><a href="/company">企业资料</a> ／ '+esc(d.name)+
    '<span class="crumb-ts">首次创建 '+esc(d.created)+' · 最后更新 '+esc(d.updated)+'</span></div>';
  const _arr=v=>Array.isArray(v)?esc(v.join('、')):esc(val(v));
  // 页签一：企业档案（字段顺序对齐市场调研企业画像卡：性质tag首行→成立时间→地址→行业地位→营收→员工→资质→专利→法人→创始人；其余工商字段随后）
  const _chip=t=>('<span style="display:inline-block;padding:3px 10px;margin:2px 6px 2px 0;background:#eef3ff;border:1px solid #c9d8ff;color:#2f54eb;border-radius:999px;font-size:12px;font-weight:600;">'+esc(t)+'</span>');
  let archive=block('工商信息',[['企业性质',_nature?raw(_chip(_nature)):'待补'],['成立时间',info.founded],
    ['总部/地址',info.address],['行业地位',info.industry_position],['年营收',info.revenue],
    ['员工人数',info.employees],['资质',info.qualifications],['专利',info.patents],
    ['法定代表人',info.legal_person],['创始人/实控人',info.founder],
    ['公司全称',info.full_name],['统一社会信用代码',info.credit_code],
    ['经营状态',info.status],['注册资本',info.reg_capital],['公司类型',info.company_type],
    ['上市状态',info.listed],['所属行业',info.industry],['主要产品',info.products],['经营范围',info.business_scope]]);
  if(val(info.brands)!==''||val(info.aliases)!==''){
    archive+='<div class="blk"><div class="blk-t">品牌与别名</div>';
    if(val(info.brands)!=='')archive+='<div class="kv"><span class="k">品牌名</span><span class="v">'+_arr(info.brands)+'</span></div>';
    if(val(info.domains)!=='')archive+='<div class="kv"><span class="k">官网域名</span><span class="v">'+_arr(info.domains)+'</span></div>';
    archive+='<div class="kv"><span class="k">别名</span><span class="v">'+_arr(info.aliases)+
      ' <button id="aliasUpd" title="联网搜索该品牌的常见别名（简称/英文名/曾用名），增量并入" style="margin-left:8px;border:1px solid #c9d8ff;background:#f5f8ff;color:#2f54eb;border-radius:6px;padding:2px 10px;font-size:12px;cursor:pointer;font-family:inherit">更新</button></span></div></div>';
  }
  archive+=block('工商登记明细',[['曾用名',info.history_names],['企业简介',info.company_profile],
    ['执照号码',info.license_number],
    ['组织机构代码',info.org_code],['注册地址(登记原文)',info.regit_address],
    ['省/市/区',[info.province,info.city,info.district].filter(Boolean).join(' ')],
    ['行业代码',info.industry_code],['从业人数',info.staff_number],
    ['参保人数',info.social_security_num?(info.social_security_num+(info.social_security_from?' ('+info.social_security_from+')':'')):''],
    ['实缴资本',info.contributed_capital],['年营业额',info.annual_turnover],
    ['纳税总额(万元)',info.report_tax_total],['核准日期',info.approve_date],['营业期限',info.business_period],
    ['登记机关',info.issuing_authority],['地址最近变更',info.address_last_change]]);
  archive+=block('其他',[...extra]);
  // 页签二：问句挖掘沉淀（产品/客群含B、C端/旅程/话题库/意图簇/验证）
  const m=d.mining;
  let mining;
  if(m){
    const brands=[...new Set([...(m.personas||[]),...(m.topics||[]),...(m.clusters||[])].map(x=>x&&x.brand).filter(Boolean))];
    const segTypeTag=t=>t&&t!=='未标注'?('<span style="display:inline-block;margin-left:8px;padding:1px 8px;border-radius:999px;background:#eef3ff;color:#2f54eb;font-size:11px;font-weight:600">'+esc(t)+'</span>'):'';
    mining='<div style="color:#6e7991;font-size:12px;margin-bottom:12px">最后入库 '+esc(d.mining_updated||'')+' · 问句 '+(m.query_count||0)+' 条'+
      (brands.length>1?('　品牌过滤 <select id="mBrand" style="padding:2px 8px;border-radius:6px;border:1px solid #c9d8ff;font-size:12px" onchange="renderMining()"><option value="">全部</option>'+brands.map(b=>'<option>'+esc(b)+'</option>').join('')+'</select>'):'')+'</div>'+
      '<div id="mBody"></div>';
    window._m=m;
    window.renderMining=()=>{
      const q=(document.getElementById('mBrand')||{value:''}).value;
      const fl=arr=>(arr||[]).filter(x=>!q||!x||x.brand==null||x.brand===q);
      const _stxt=s=>typeof s==='string'?s:(s.text||s.query||s.stage_name||'');
      const journeyRows=(m.journey&&m.journey.length)
        ? fl(m.journey).map(j=>['阶段'+(j.stage_index||'')+'　'+(j.stage_name||''), j.customer_question||j.question||j.definition||''])
        : fl(m.journey_stages).map((s,i)=>['阶段'+(i+1), _stxt(s)]);
      document.getElementById('mBody').innerHTML=
      block('主打产品',fl(m.products).map(p=>[p.name||p.product_name||'', raw(esc(p.positioning||'')+(p.evidence?('<br><span style="color:#6e7991">主推依据：'+esc(p.evidence)+'</span>'):''))]))+
      block('目标消费者',fl(m.personas).map(p=>[esc(p.name||'')+segTypeTag(p.audience_type), (p.definition||'')+(p.sales_strategy?('<br>策略：'+p.sales_strategy):'')]))+
      block('决策旅程',journeyRows)+
      block('话题库与问句',fl(m.topics).slice(0,120).map(t=>['阶段'+t.stage_index+' · '+t.stage_name, (t.queries||[]).map(x=>x.query).join('　')]))+
      block('意图簇',fl(m.clusters).map(c=>[c.cluster_name,(c.queries||[]).length+' 条问句']))+
      block('反演验证',fl(m.verifications).map(v=>[v.cluster_name||'',(v.conclusion||'')+(v.coverage_check?('　'+v.coverage_check):'')]));
    };
    setTimeout(()=>renderMining(),0);
  } else mining='<div class="empty">该客户暂无问句挖掘数据（在「GEO搜索问句挖掘」页跑一次分析后自动入库）</div>';
  // 页签三：官网检测结论（维度分/指纹/后台/计数/核心问题，报告链接为附加）
  let audits='';
  (d.audits||[]).forEach((a,ai)=>{
    audits+='<div class="blk"><div class="blk-t" style="display:flex;align-items:center">'+esc(a.domain)+'　'+esc(a.date)+'　<span style="font-size:20px;font-weight:700;color:'+(a.score>=75?'#16a34a':a.score>=60?'#d97706':'#dc2626')+'">'+esc(a.score)+'</span>　'+esc(a.grade)+
      (a.report_file?('　<a style="font-size:12px;font-weight:400;color:#3567ff" href="/audit-result/'+encodeURIComponent(String(a.report_file).replace(/\\.json$/,".html"))+'" target="_blank">完整报告</a>'):'')+
      '　<button class="btn-del bc-del" data-sid="'+esc(a.sid)+'" title="删除该检测记录">&times;</button></div>';
    if(a.categories&&a.categories.length)audits+='<div class="dims">'+a.categories.map(c=>
      '<div class="dim"><span class="dn">'+esc(c.name)+'</span><span class="db"><i style="width:'+Math.max(2,c.score)+'%"></i></span><span class="dv">'+c.score+'</span></div>').join('')+'</div>';
    if(a.core_counts)audits+='<div style="font-size:13px;color:#3d4664;margin:8px 0">必检项：合格 '+a.core_counts.pass+' · 不足 '+a.core_counts.warn+' · 不合格 <b style="color:#dc2626">'+a.core_counts.fail+'</b>（共 '+a.core_counts.total+'）'+
      (a.bonus_counts?('　｜　非必检：合格 '+a.bonus_counts.pass+' · 不合格 '+a.bonus_counts.fail):'')+'</div>';
    if(a.fingerprint)audits+='<div style="font-size:13px;color:#3d4664">官网指纹：'+esc(a.fingerprint.message)+'</div>';
    if(a.backend)audits+='<div style="font-size:13px;color:#3d4664">管理后台：'+esc(a.backend.message)+'</div>';
    if(a.issues&&a.issues.length)audits+='<div style="margin-top:8px"><b style="font-size:13px">核心问题（不合格 '+a.issues.length+' 项）</b>'+
      a.issues.map(x=>'<div class="iss">· '+esc(x.name)+'：'+esc(x.message)+'</div>').join('')+'</div>';
    audits+='</div>';
  });
  if(!audits)audits='<div class="empty">该客户暂无官网检测记录（在「网站 SEO/GEO 诊断」页检测并绑定企业后自动关联）</div>';
  html+='<div class="tabs"><button class="tab on" onclick="tab(this,0)">企业档案</button>'+
    '<button class="tab" onclick="tab(this,1)">问句挖掘'+(m?'':'（无）')+'</button>'+
    '<button class="tab" onclick="tab(this,2)">官网检测'+((d.audits&&d.audits.length)?('（'+d.audits.length+'）'):'（无）')+'</button></div>'+
    '<div class="tabpane">'+(archive||'<div class="empty">该客户暂无档案字段</div>')+'</div>'+
    '<div class="tabpane" style="display:none">'+mining+'</div>'+
    '<div class="tabpane" style="display:none">'+audits+'</div>';
  document.getElementById('main').innerHTML=html;
  document.querySelectorAll('.bc-del').forEach(function(b){
    b.style.cssText='margin-left:auto;border:none;background:transparent;color:#a0a9bd;font-size:16px;cursor:pointer;width:24px;height:24px;border-radius:6px';
    b.onclick=function(){
      if(!confirm('删除这条检测记录？（仅删库内结论行，报告文件不受影响）')) return;
      fetch('/api/audit/snapshots/'+encodeURIComponent(b.dataset.sid), {method:'DELETE'})
        .then(function(r){return r.json();}).then(function(d){ if(d.ok) loadDetail(currentDetailId); else alert(d.error||'删除失败'); });
    };
  });
  const _ab=document.getElementById('aliasUpd');
  if(_ab)_ab.onclick=()=>updateAliases(id);
}
function tab(btn,i){
  btn.parentNode.querySelectorAll('.tab').forEach((b,j)=>b.className='tab'+(j===i?' on':''));
  let el=btn.parentNode, ps=[];
  while(el=el.nextElementSibling){ if(el.classList&&el.classList.contains('tabpane')) ps.push(el); else break; }
  ps.forEach((p,j)=>p.style.display=(j===i?'':'none'));
}
// 别名增量更新：LLM 联网搜品牌常见别名，去重并入档案
async function updateAliases(id){
  const btn=document.getElementById('aliasUpd'); if(!btn)return;
  btn.disabled=true; const old=btn.textContent; btn.textContent='搜索中…';
  try{
    const r=await fetch('/api/company/alias-update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});
    const d=await r.json();
    if(d.ok){alert(d.added.length?('新增别名：'+d.added.join('、')):'未发现新别名'); loadDetail(id);}
    else alert(d.error||'更新失败');
  }catch(e){alert('更新失败：网络异常');}
  btn.disabled=false; btn.textContent=old;
}
// 管理密码弹窗：样式与报告页「更新企业信息」密码框同款
function askPwd(cb){
  const m=document.createElement('div');
  m.className='pwd-mask';
  m.innerHTML='<div class="pwd-box"><div class="pwd-t">删除客户档案，请输入管理密码：</div><input type="password" class="pwd-i"><div class="pwd-btns"><button class="pwd-cancel">取消</button><button class="pwd-ok">确定</button></div></div>';
  document.body.appendChild(m);
  const i=m.querySelector('input'); i.focus();
  const done=ok=>{const v=i.value.trim(); m.remove(); cb(ok&&v?v:null);};
  m.querySelector('.pwd-ok').onclick=()=>done(true);
  m.querySelector('.pwd-cancel').onclick=()=>done(false);
  i.onkeydown=e=>{if(e.key==='Enter')done(true);if(e.key==='Escape')done(false);};
}
// 与官网诊断报告列表同款删除交互：× → 确认（3秒超时回退）→ 密码 → 删除
function handleDel(btn){
  if(!btn.classList.contains('confirming')){
    btn.classList.add('confirming');
    btn.textContent='确认';
    setTimeout(function(){
      if(btn.classList.contains('confirming')){
        btn.classList.remove('confirming');
        btn.innerHTML='&times;';
      }
    },3000);
    return;
  }
  askPwd(function(pwd){
    if(!pwd)return;
    fetch('/api/company/'+encodeURIComponent(btn.dataset.id),{method:'DELETE',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({pwd:pwd})})
    .then(function(r){return r.json();})
    .then(function(d){
      if(d.ok){loadList();}
      else{alert(d.detail||d.error||'删除失败');}
    });
  });
}
let _allCompanies=[];
async function loadList(){
  document.getElementById('main').innerHTML=
    '<input id="coSearch" class="rp-search" type="text" placeholder="搜索客户名称…" style="margin-bottom:12px" oninput="renderList()">'+
    '<div id="coCount"></div><div id="coTable"></div>';
  const r=await fetch('/api/company/list'); const d=await r.json();
  _allCompanies=d.companies||[];
  renderList();
}
function renderList(){
  const q=(document.getElementById('coSearch').value||'').trim().toLowerCase();
  const list=_allCompanies.filter(c=>!q||(c.name+' '+c.id).toLowerCase().indexOf(q)>=0);
  document.getElementById('coCount').innerHTML=q?('<span style="color:#6e7991;font-size:13px">'+list.length+' / '+_allCompanies.length+' 家</span>'):'';
  const t=document.getElementById('coTable');
  if(!list.length){t.innerHTML='<div class="empty">'+(q?'没有匹配的客户':'企业资料库为空')+'</div>';return;}
  t.innerHTML='<table><thead><tr><th>客户名称</th><th>首次创建时间</th><th>最后更新时间</th><th>首次信息来源</th><th>最后更新触点</th><th></th></tr></thead><tbody>'+
    list.map(c=>'<tr onclick="location=\\'/company?id='+encodeURIComponent(c.id)+'\\'">'+
      '<td class="nm">'+esc(c.name)+'</td><td>'+esc(c.created)+'</td><td>'+esc(c.updated)+'</td>'+
      '<td>'+esc(c.origin_first||'-')+'</td><td>'+esc(c.origin_last||'-')+'</td>'+
      '<td class="td-op"><button class="btn-del" data-id="'+esc(c.id)+'" title="删除">&times;</button></td></tr>').join('')+
    '</tbody></table>';
  document.querySelectorAll('.btn-del').forEach(b=>b.addEventListener('click',function(e){e.stopPropagation();handleDel(this);}));
}
const q=new URLSearchParams(location.search).get('id');
if(q){loadDetail(q);}else{loadList();}
"""

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>企业资料</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f4f5ff;color:#091026;line-height:1.6}
.topnav{display:flex;gap:24px;justify-content:flex-end;align-items:center;padding:24px;max-width:1200px;margin:0 auto;position:relative;box-sizing:border-box}
.topnav a{font-size:16px;font-weight:400;text-decoration:none;color:#6e7991;transition:color .2s}
.topnav a:hover{color:#091026}
.topnav a.active{color:#091026;font-weight:600}
.nav-dropdown{position:relative}
.nav-dropdown-toggle{display:flex;align-items:center;gap:6px;font-size:16px;font-weight:400;color:#6e7991;cursor:pointer;transition:color .2s}
.nav-dropdown:hover .nav-dropdown-toggle{color:#091026}
.nav-arrow{width:1rem;height:1rem;transition:transform .2s}
.nav-dropdown:hover .nav-arrow{transform:rotate(180deg)}
.nav-dropdown-menu{position:absolute;top:100%;right:0;margin-top:12px;padding:8px;background:#fff;border:1px solid #eff1f6;border-radius:1rem;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a;display:none;z-index:50}
.nav-dropdown:hover .nav-dropdown-menu{display:block}
.nav-dropdown-menu a{display:block;padding:8px 12px;border-radius:.5rem;font-size:16px;color:#6e7991;text-decoration:none;white-space:nowrap;transition:all .15s}
.nav-dropdown-menu a:hover{background:#f4f5ff;color:#091026}
.nav-dropdown::after{content:"";position:absolute;left:0;right:0;top:100%;height:12px}
.page{max-width:1000px;margin:0 auto;padding:8px 24px 48px}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:1rem;overflow:hidden;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a}
th{background:#eff1f6;text-align:left;padding:12px 20px;font-size:13px;color:#6e7991;font-weight:600}
td{padding:14px 20px;border-top:1px solid #eff1f6;font-size:14px;color:#3d4664}
tr{cursor:pointer;transition:background .15s}
tr:hover{background:#f4f5ff}
td.nm{font-weight:600;color:#091026}
.empty{text-align:center;color:#a0a9bd;padding:48px 0}
.crumb{font-size:14px;color:#091026;font-weight:600;margin-bottom:16px}
.crumb a{color:#3567ff;text-decoration:none;font-weight:400}
.crumb-ts{font-weight:400;color:#6e7991;font-size:12px;margin-left:12px}
.blk{background:#fff;border-radius:1rem;padding:20px 24px;margin-bottom:16px;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a}
.tabs{display:flex;gap:4px;margin-bottom:14px;background:#fff;border-radius:12px;padding:4px;width:fit-content;box-shadow:0 .0625rem .0625rem #0b14371a,0 .25rem .25rem #99beff1a}
.tab{border:none;background:transparent;border-radius:9px;padding:7px 18px;font-size:14px;color:#6e7991;cursor:pointer;font-family:inherit}
.tab.on{background:#091026;color:#fff;font-weight:600}
.dims{display:flex;flex-direction:column;gap:4px;margin:6px 0}
.dim{display:flex;align-items:center;gap:10px;font-size:12px;color:#6e7991}
.dim .dn{flex:0 0 120px}
.dim .db{flex:1;height:6px;background:#eff1f6;border-radius:3px;overflow:hidden}
.dim .db i{display:block;height:100%;background:linear-gradient(90deg,#3567ff,#6b8cff);border-radius:3px}
.dim .dv{flex:0 0 44px;text-align:right;color:#091026;font-weight:600}
.iss{font-size:12.5px;color:#3d4664;padding:3px 0}
.rp-search{padding:8px 14px;border:1.5px solid #eff1f6;border-radius:10px;font-size:13px;outline:none;width:260px;box-sizing:border-box;font-family:inherit}
.rp-search:focus{border-color:#3567ff}
.blk-t{font-size:15px;font-weight:600;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #eff1f6}
.kv{display:flex;gap:16px;padding:6px 0;font-size:14px}
.kv .k{flex:0 0 180px;color:#6e7991}
.kv .v{flex:1;color:#091026;word-break:break-all}
/* 删除按钮与两段式确认：样式复制自官网诊断报告列表 */
.td-op{text-align:right;padding:0 10px}
.btn-del{width:28px;height:28px;border:none;border-radius:8px;background:transparent;color:#a0a9bd;font-size:16px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;transition:all .15s}
.btn-del:hover{background:#fef2f2;color:#dc2626}
.btn-del.confirming{background:#dc2626;color:#fff;font-size:11px;font-weight:600}
/* 密码弹窗：样式复制自报告页「更新企业信息」密码框 */
.pwd-mask{position:fixed;inset:0;background:rgba(9,16,38,.45);z-index:10001;display:flex;align-items:center;justify-content:center}
.pwd-box{background:#fff;border-radius:12px;padding:20px 22px;width:300px;box-shadow:0 10px 40px rgba(0,0,0,.25)}
.pwd-t{font-size:14px;font-weight:600;color:#1a1d29;margin-bottom:12px}
.pwd-i{width:100%;box-sizing:border-box;padding:8px 10px;border:1.5px solid #c9d2e0;border-radius:8px;font-size:13px;margin-bottom:12px}
.pwd-btns{display:flex;gap:10px;justify-content:flex-end}
.pwd-btns button{padding:6px 16px;border-radius:8px;border:none;font-size:13px;cursor:pointer}
.pwd-ok{background:#3567ff;color:#fff}
.pwd-cancel{background:#eef1f7;color:#556}
</style>
</head>
<body>
<nav class="topnav">
  <a href="/research" class="brand-logo" style="position:absolute;left:24px;top:50%;transform:translateY(-50%)"><img src="/brand/logo.png" alt="飞柚 GEO 品牌标识" style="height:32px"></a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>分析</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/mining">GEO搜索问句挖掘</a>
      <a href="/research">客户市场调研</a>
    </div>
  </div>
  <a href="/audit">官网诊断</a>
  <a href="/chat">对话</a>
  <div class="nav-dropdown">
    <div class="nav-dropdown-toggle"><span>配置管理</span>
      <svg class="nav-arrow" viewBox="0 0 20 20" fill="none"><path d="M15.5 7.03835L10 12.9614L4.5 7.03835" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <div class="nav-dropdown-menu">
      <a href="/skills">Skill管理</a>
      <a href="/company" class="active" id="crumbHome">企业资料</a>
      <a href="/oplog">操作日志</a>
      <a href="/backfill">JSON补录</a>
      <a href="/changelog">__ver__</a>
    </div>
  </div>
</nav>
<div class="page"><div id="main"><div class="empty">加载中…</div></div></div>
<script>__DETAIL_JS__</script>
</body>
</html>"""


def company_page_html() -> str:
    return PAGE.replace("__DETAIL_JS__", DETAIL_JS)
