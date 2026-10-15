import json
import os

scratch_dir = r'C:\Users\danim\.gemini\antigravity\brain\68429021-dae2-4e0b-a825-f90f68723f8b\scratch'
res_path = os.path.join(scratch_dir, 'ab_test_phase3_results.json')

with open(res_path, 'r', encoding='utf-8') as f:
    data = json.load(f)

results = data['results']

print("=== PHASE 3 DETAILED ANALYSIS ===")
print(f"Total executions: {len(results)}\n")

EVALUATIVE_KEYS = [
    'empatia', 'simpatia', 'claridad', 'procedimiento', 'saludo_inicio',
    'n3_preguntas', 'despedida_con_refuerzo', 'gestion_objeciones',
    'uso_nombre_paciente', 'uso_preguntas', 'explicaciones_medicas',
    'claridad_explicacion_economica', 'siguiente_paso'
]

calls = {}
for r in results:
    cid = r['call_id']
    if cid not in calls:
        calls[cid] = {'label': r['label'], 'A': None, 'B2': None}
    calls[cid][r['version']] = r

for cid, cdata in calls.items():
    print("=" * 80)
    print(f"CALL: {cdata['label']} (Call ID: {cid})")
    r_A = cdata['A']
    r_B2 = cdata['B2']
    
    j_A = r_A['raw_json']
    j_B2 = r_B2['raw_json']
    
    print(f"\n--- METRICS A vs B2 ---")
    print(f"Prod Score:   A={r_A['prod_score']} | B2={r_B2['prod_score']} | Delta={round(r_B2['prod_score'] - r_A['prod_score'], 2)}")
    print(f"Cost USD:     A=${r_A['cost_usd']:.6f} | B2=${r_B2['cost_usd']:.6f}")
    print(f"Latency:      A={r_A['latency_ms']}ms | B2={r_B2['latency_ms']}ms")
    u_A = r_A['usage']
    u_B2 = r_B2['usage']
    print(f"Input Text:   A={u_A['input_text_tokens']} | B2={u_B2['input_text_tokens']}")
    print(f"Cached Tok:   A={u_A['cached_tokens']} ({(u_A['cached_tokens']/u_A['input_text_tokens']*100):.1f}%) | B2={u_B2['cached_tokens']} ({(u_B2['cached_tokens']/u_B2['input_text_tokens']*100):.1f}%)")
    print(f"Thoughts:     A={u_A['thoughts_tokens']} | B2={u_B2['thoughts_tokens']}")
    print(f"Candidates:   A={u_A['candidates_tokens']} | B2={u_B2['candidates_tokens']}")
    print(f"Total Tokens: A={u_A['total_tokens']} | B2={u_B2['total_tokens']}")

    print(f"\n--- CANCELLATION CRITICAL SAFEGUARDS ---")
    print(f"n3_preguntas:")
    print(f"  A  value: {j_A.get('n3_preguntas')} | Feed: {j_A.get('n3_preguntas_feed')}")
    print(f"  B2 value: {j_B2.get('n3_preguntas')} | Feed: {j_B2.get('n3_preguntas_feed')}")
    print(f"claridad_explicacion_economica:")
    print(f"  A  value: {j_A.get('claridad_explicacion_economica')} | Feed: {j_A.get('claridad_explicacion_economica_feed')}")
    print(f"  B2 value: {j_B2.get('claridad_explicacion_economica')} | Feed: {j_B2.get('claridad_explicacion_economica_feed')}")

    print(f"\n--- 13 EVALUATIVE CRITERIA COMPARISON ---")
    print(f"{'CRITERION':32} | {'A VALUE':10} | {'B2 VALUE':10} | {'DELTA':8} | {'STATUS'}")
    print("-" * 75)
    for k in EVALUATIVE_KEYS:
        val_A = j_A.get(k)
        val_B2 = j_B2.get(k)
        if val_A is not None and val_B2 is not None:
            try:
                diff = round(float(val_B2) - float(val_A), 2)
                diff_str = f"{diff:+0.2f}"
            except Exception:
                diff_str = "N/A"
            status = "both_evaluable" if val_A == val_B2 else "diff_evaluable"
        elif val_A is None and val_B2 is None:
            diff_str = "0.00"
            status = "both_null"
        elif val_A is None and val_B2 is not None:
            diff_str = f"+{val_B2}"
            status = "A_null_B2_eval"
        else:
            diff_str = f"-{val_A}"
            status = "A_eval_B2_null"
        print(f"{k:32} | {str(val_A):10} | {str(val_B2):10} | {diff_str:8} | {status}")

    print(f"\n--- CLASSIFICATION & CRITICAL FIELDS ---")
    for field in ['tipo_llamada', 'motivo_no_cita', 'patologia_detectada', 'cierre_cita', 'alarma_legal', 'alarma_calidad']:
        print(f"  {field:22}: A = {j_A.get(field)} | B2 = {j_B2.get(field)}")
