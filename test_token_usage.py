# token 用量统计与费用估算自检：运行 python3 test_token_usage.py
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from site_audit.core.ai_engine import estimate_cost_cny

tu = {"prompt_tokens": 97391, "completion_tokens": 28946, "total_tokens": 126337,
      "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 97391,
      "calls": 14, "search_calls": 20, "failed_attempts": 3}
# 好莱客真实用量：输入97391×0.8 + 输出28946×2.7（元/百万）≈ ¥0.156
assert abs(estimate_cost_cny(tu) - 0.1561) < 0.0005, estimate_cost_cny(tu)
# 搜索单价 0.012 元/次时计入 20 次 = +0.24
assert abs(estimate_cost_cny(tu, search_price_per_call=0.012) - 0.3961) < 0.0005

from main import _merge_usage
r = {"token_usage": dict(tu)}
class Fake: token_usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                           "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 10,
                           "calls": 1, "search_calls": 1, "failed_attempts": 2}
_merge_usage(r, Fake())
assert r["token_usage"]["search_calls"] == 21 and r["token_usage"]["failed_attempts"] == 5
assert r["token_usage"]["total_tokens"] == 126352
# 旧报告无新键也不炸
_merge_usage({"token_usage": {"prompt_tokens": 1}}, Fake())
print("token usage accounting OK")
