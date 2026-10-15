import json
import hashlib

with open(r'C:\Users\danim\.gemini\antigravity\brain\68429021-dae2-4e0b-a825-f90f68723f8b\scratch\ab_test_results.json', encoding='utf-8') as f:
    p1 = json.load(f)

with open(r'C:\Users\danim\.gemini\antigravity\brain\68429021-dae2-4e0b-a825-f90f68723f8b\scratch\ab_test_phase2_results.json', encoding='utf-8') as f:
    p2 = json.load(f)

print("AUDIT OF ALL 16 EXECUTIONS (PHASE 1 + PHASE 2)")
print("=" * 110)
print(f"{'#':2} | {'FASE':6} | {'ORDEN':5} | {'CALL ID':12} | {'VER':3} | {'IN_TEXT':8} | {'CACHED':8} | {'% CACHED':8} | {'THOUGHTS':8} | {'LATENCY':8} | {'PROMPT_HASH':10}")
print("=" * 110)

exec_num = 0

# Phase 1: 5 calls x 2 versions = 10 executions
for r in p1['results']:
    exec_num += 1
    u = r['usage']
    in_text = u['input_text_tokens']
    cached = u['cached_tokens']
    pct = (cached / in_text) * 100 if in_text else 0
    th = u['thoughts_tokens']
    lat = r['latency_ms']
    ver = r['version']
    cid = r['call_id']
    # Hash of version
    h = "A_v248" if ver == "A" else "B_opt"
    print(f"{exec_num:2} | Fase 1 | {exec_num:5} | {cid:12} | {ver:3} | {in_text:8} | {cached:8} | {pct:7.2f}% | {th:8} | {lat:6}ms | {h:10}")

# Phase 2: 3 calls x 2 versions = 6 executions
for r in p2['results']:
    exec_num += 1
    u = r['usage']
    in_text = u['input_text_tokens']
    cached = u['cached_tokens']
    pct = (cached / in_text) * 100 if in_text else 0
    th = u['thoughts_tokens']
    lat = r['latency_ms']
    ver = r['version']
    cid = r['call_id']
    h = "A_v248" if ver == "A" else "B_opt"
    print(f"{exec_num:2} | Fase 2 | {exec_num-10:5} | {cid:12} | {ver:3} | {in_text:8} | {cached:8} | {pct:7.2f}% | {th:8} | {lat:6}ms | {h:10}")
