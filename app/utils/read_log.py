import sys
import traceback

try:
    sys.stdout.reconfigure(encoding='utf-8')
    with open("verify_output.log", "r", encoding="utf-16") as f:
        content = f.read()
    lines = content.splitlines()
    print("\n".join(lines[:150]))
    if len(lines) > 150:
        print("...")
        # And let's print the lines containing "[ERROR executing statement]"
        for i, line in enumerate(lines):
            if "[ERROR executing statement]" in line:
                print(f"\n--- Found error at line {i} ---")
                print("\n".join(lines[i:i+40]))
except Exception as e:
    traceback.print_exc()
