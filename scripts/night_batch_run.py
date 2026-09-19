"""夜间批量补数据驱动：市场调研6家 + 问句挖掘5家 + 官网诊断16站。

经本地 API 提交（与服务同口径），分析任务由任务管理器排队（并发2）；
诊断受"进行中≤10"限制，驱动按空位逐个补提交。状态每分钟记一次 var/log/batch_run.log。
"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8777"
LOG = "var/log/batch_run.log"

RESEARCH = ["山西宜发同诚农业发展有限公司", "江西启帆教育科技有限公司", "浙江锦联国际物流有限公司",
            "熵函数(深圳)科技有限公司", "深圳依时货拉拉科技有限公司", "广州好莱客创意家居股份有限公司"]
MINING = ["赛轮集团股份有限公司", "好莱客", "和创企赢", "深圳市极联软件股份有限公司", "浙江锦联国际物流有限公司"]
AUDITS = ["http://www.yongsheng360.com/", "http://www.sxyftc.cn/col.jsp?id=104", "https://www.qixiangyun.com/",
          "http://www.tyccs.com/", "https://www.lishan-bio.com", "https://www.tonsynr.cn", "https://qifanjy.cn",
          "https://www.jolyvia.com/", "https://site.youfantech.cn/", "www.jlkfapp.com", "www.monkeysoftware.cn",
          "https://www.iaast.cn", "https://www.daxintax.com/", "https://www.aeh-metrology.com",
          "https://shanxi.iqiaowai.com/", "https://www.hcqycs.com/"]


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def get(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=30).read())


def ensure_verified(brand):
    """服务端要求名称先经工商核验登记（2026-09-19 反伪造定版）：核查登记后再提交任务。"""
    import urllib.parse
    r = get("/api/brand/check?name=" + urllib.parse.quote(brand))
    if not r.get("exists"):
        log(f"跳过 {brand}: 工商库查无此企业")
        return False
    return True


def main():
    task_ids = {}
    for b in RESEARCH:
        if not ensure_verified(b):
            continue
        r = post("/api/tasks", {"brand": b, "method": "customer_market_research"})
        task_ids[r["task_id"]] = ("调研", b)
        log(f"提交调研 {b} → {r['task_id']}")
        time.sleep(1)
    for b in MINING:
        if not ensure_verified(b):
            continue
        r = post("/api/tasks", {"brand": b, "method": "geo_query_mining"})
        task_ids[r["task_id"]] = ("挖掘", b)
        log(f"提交挖掘 {b} → {r['task_id']}")
        time.sleep(1)

    audit_done = {}
    pending = list(AUDITS)
    t0 = time.time()
    while True:
        # 诊断按空位补提交（进行中≤10）
        try:
            active = get("/api/audit/jobs").get("active", 0)
        except Exception:
            active = 10
        while pending and active < 8:
            u = pending.pop(0)
            try:
                r = post("/api/audit/start", {"url": u})
                log(f"提交诊断 {u} → {r.get('job_id')}")
                active += 1
            except Exception as e:
                audit_done[u] = f"提交失败 {e}"
                log(f"提交诊断失败 {u}: {e}")
            time.sleep(1)
        # 收集诊断结果
        try:
            for j in get("/api/audit/jobs").get("jobs", []):
                if j["url"] in audit_done:
                    continue
                if j["status"] == "done":
                    audit_done[j["url"]] = "完成"
                    log(f"诊断完成 {j['url']}")
                elif j["status"] == "error":
                    audit_done[j["url"]] = f"失败:{j.get('error','')}"
                    log(f"诊断失败 {j['url']}: {j.get('error')}")
        except Exception:
            pass
        # 分析任务状态
        try:
            tasks = {t["task_id"]: t for t in get("/api/tasks?limit=50").get("tasks", [])}
            live = [tid for tid in task_ids if tasks.get(tid, {}).get("status") in ("running", "pending")]
        except Exception:
            live = list(task_ids)
        if not live and not pending and len(audit_done) == len(AUDITS):
            break
        if time.time() - t0 > 3 * 3600:
            log("超 3 小时强制收尾")
            break
        time.sleep(60)

    for tid, (kind, b) in task_ids.items():
        try:
            st = get(f"/api/tasks/{tid}").get("status", "?")
        except Exception:
            st = "?"
        log(f"终态 {kind} {b}: {st}")
    for u in AUDITS:
        log(f"终态 诊断 {u}: {audit_done.get(u, '未知')}")
    log("ALL DONE")


if __name__ == "__main__":
    main()
