import json
import urllib.request
import sys
sys.path.insert(0, r'c:\Users\danim\Proyectos\bm-analysis-service')
from app.utils.security import create_access_token
from app.utils.scores import calculate_score_from_items, EVALUATIVE_CRITERIA_KEYS
from app.services.analysis_results_mapper import map_criterion_value
from app.utils.evaluability import determine_evaluability

token = create_access_token({'user_id': 1})
headers = {'Authorization': f'Bearer {token}'}

def get(path):
    url = f'https://speech-backend.doobot.ai{path}'
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

det_sample = get('/bm/mass-evaluations/results/84587')
criteria_template = []
for it in det_sample['items_json']:
    criteria_template.append({
        'criterion_id': it['criterion_id'],
        'criterion_key': it['criterion_key'],
        'output_key': it.get('output_key') or it['criterion_key'],
        'criterion_type': it.get('type') or 'text',
        'name': it.get('name')
    })
print(f'Loaded {len(criteria_template)} criteria from production template')

with open(r'C:\Users\danim\.gemini\antigravity\brain\68429021-dae2-4e0b-a825-f90f68723f8b\scratch\ab_test_results.json', encoding='utf-8') as f:
    ab_data = json.load(f)

calls = ['527566359779', '527116146936', '527241182407', '527193877743', '527412783337']

for cid in calls:
    ra = next(r for r in ab_data['results'] if str(r['call_id']) == cid and r['version'] == 'A')
    rb = next(r for r in ab_data['results'] if str(r['call_id']) == cid and r['version'] == 'B')
    
    for r, ver in [(ra, 'A'), (rb, 'B')]:
        rj = r['raw_json']
        items = []
        for crit in criteria_template:
            out_k = crit['output_key']
            raw_val = rj.get(out_k)
            feed_val = rj.get(f'{out_k}_feed')
            c_type = crit['criterion_type']
            typed = map_criterion_value(raw_val, c_type)
            
            is_na = raw_val is None
            items.append({
                'criterion_id': crit['criterion_id'],
                'criterion_key': crit['criterion_key'],
                'output_key': out_k,
                'name': crit['name'],
                'type': c_type,
                'value': raw_val,
                'feed': feed_val,
                'numeric_value': typed['value_number'],
                'boolean_value': typed['value_boolean'],
                'not_applicable': is_na,
                'is_applicable': not is_na
            })
            
        is_ev, reason = determine_evaluability(
            typology_key=rj.get('tipo_llamada'),
            result_json=rj,
            items=items,
            call_duration_seconds=r['dur'],
            status='completed'
        )
        prod_score = calculate_score_from_items(items) if is_ev else None
        r['computed_is_eval'] = is_ev
        r['computed_reason'] = reason
        r['computed_prod_score'] = prod_score
        r['computed_items'] = items

print('='*90)
print('RECALCULATION COMPLETE WITH 100% PRODUCTION LOGIC (app.utils.scores)')
print('='*90)

for cid in calls:
    ra = next(r for r in ab_data['results'] if str(r['call_id']) == cid and r['version'] == 'A')
    rb = next(r for r in ab_data['results'] if str(r['call_id']) == cid and r['version'] == 'B')
    label = ra['label']
    dur = ra['dur']
    hist = ra['hist_score']
    sa = ra['computed_prod_score']
    sb = rb['computed_prod_score']
    delta = round(sb - sa, 2) if sa is not None and sb is not None else 'N/A'
    ev_a = ra['computed_is_eval']
    ev_b = rb['computed_is_eval']
    print(f'{label:12} ({dur:3}s) [Call {cid}] | Hist: {hist:4} | Prod A: {str(sa):5} | Prod B: {str(sb):5} | Delta (B-A): {str(delta):5} | Eval: A={ev_a}/B={ev_b}')
