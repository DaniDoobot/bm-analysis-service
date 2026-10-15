import re
import difflib

with open(r'C:\Users\danim\.gemini\antigravity\brain\68429021-dae2-4e0b-a825-f90f68723f8b\scratch\prompt_A.txt', 'r', encoding='utf-8') as f:
    prompt_A = f.read()

with open(r'C:\Users\danim\.gemini\antigravity\brain\68429021-dae2-4e0b-a825-f90f68723f8b\scratch\prompt_B.txt', 'r', encoding='utf-8') as f:
    prompt_B = f.read()

with open(r'C:\Users\danim\.gemini\antigravity\brain\68429021-dae2-4e0b-a825-f90f68723f8b\scratch\prompt_B2.txt', 'r', encoding='utf-8') as f:
    prompt_B2 = f.read()

def audit_prompt(name, text):
    crit = re.findall(r'- \[ID: \d+\].*', text)
    out_keys = re.findall(r'output_key: (\w+)', text)
    feed_keys = re.findall(r'feed_key: (\w+)', text)
    return {
        'name': name,
        'chars': len(text),
        'words': len(text.split()),
        'crit_count': len(crit),
        'out_keys': len(out_keys),
        'feed_keys': len(feed_keys),
        'total_keys': len(out_keys) + len(feed_keys),
        'out_set': set(out_keys),
        'feed_set': set(feed_keys)
    }

res_A = audit_prompt('Prompt A', prompt_A)
res_B = audit_prompt('Prompt B', prompt_B)
res_B2 = audit_prompt('Prompt B2', prompt_B2)

print("=== OFFLINE PROMPT VERIFICATION ===")
for r in [res_A, res_B, res_B2]:
    print(f"{r['name']}:")
    print(f"  Chars: {r['chars']:,} | Words: {r['words']:,}")
    print(f"  Criteria count: {r['crit_count']}")
    print(f"  Output keys: {r['out_keys']} | Feed keys: {r['feed_keys']} | Total keys: {r['total_keys']}")

# Check key equality
print("\nKey sets comparison:")
print("A out_keys == B2 out_keys:", res_A['out_set'] == res_B2['out_set'])
print("A feed_keys == B2 feed_keys:", res_A['feed_set'] == res_B2['feed_set'])
print("B out_keys == B2 out_keys:", res_B['out_set'] == res_B2['out_set'])
print("B feed_keys == B2 feed_keys:", res_B['feed_set'] == res_B2['feed_set'])

# Unified diff between B and B2
diff = list(difflib.unified_diff(prompt_B.splitlines(keepends=True), prompt_B2.splitlines(keepends=True)))
print("\nExact Diff B vs B2:")
for line in diff:
    print(line, end='')
