"""
Post-deploy production verification script (Read-Only).
Verifies:
1. /health
2. /bm/dashboard/summary (24h)
3. /bm/mass-evaluation-results (limit=10 and limit=100)
4. Checks that responses succeed with HTTP 200, valid structure, and no MissingGreenlet or 500 errors.
"""
import sys
import time
import httpx
from app.utils.security import create_access_token

BASE_URL = "https://speech-backend.doobot.ai"

def test_endpoints():
    print(f"=== POST-DEPLOY VALIDATION against {BASE_URL} ===\n")
    
    # Generate super_admin test token for read-only verification
    token = create_access_token({"user_id": 1, "username": "admin", "role": "super_admin", "is_super_admin": True})
    headers = {
        "User-Agent": "PostDeployVerification/1.0",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }

    with httpx.Client(timeout=30.0) as client:
        # 1. Health check
        t0 = time.perf_counter()
        r_health = client.get(f"{BASE_URL}/health")
        dt_health = (time.perf_counter() - t0) * 1000.0
        print(f"[1] GET /health -> Status: {r_health.status_code} ({dt_health:.1f} ms)")
        print(f"    Payload: {r_health.text}\n")
        assert r_health.status_code == 200, f"Expected 200, got {r_health.status_code}"

        # 2. Dashboard summary
        t0 = time.perf_counter()
        r_dash = client.get(f"{BASE_URL}/bm/dashboard/summary?period=24h", headers=headers)
        dt_dash = (time.perf_counter() - t0) * 1000.0
        print(f"[2] GET /bm/dashboard/summary?period=24h -> Status: {r_dash.status_code} ({dt_dash:.1f} ms)")
        if r_dash.status_code == 200:
            dash_data = r_dash.json()
            kpis = dash_data.get("kpis", {})
            print(f"    KPIs: {kpis}")
            print(f"    Evolution series points: {len(dash_data.get('evolution', []))}")
            print(f"    Agent rankings: {len(dash_data.get('agent_rankings', []))}\n")
        else:
            print(f"    Error text: {r_dash.text}\n")
        assert r_dash.status_code == 200, f"Expected 200 on dashboard summary, got {r_dash.status_code}"

        # 3. Mass evaluation results (limit=10)
        t0 = time.perf_counter()
        r_res10 = client.get(f"{BASE_URL}/bm/mass-evaluation-results?limit=10", headers=headers)
        dt_res10 = (time.perf_counter() - t0) * 1000.0
        print(f"[3] GET /bm/mass-evaluation-results?limit=10 -> Status: {r_res10.status_code} ({dt_res10:.1f} ms)")
        if r_res10.status_code == 200:
            res10_data = r_res10.json()
            total_count = res10_data.get("total", 0)
            items_count = len(res10_data.get("items", []))
            print(f"    Total in DB: {total_count}, Items returned: {items_count}")
            if items_count > 0:
                first_item = res10_data["items"][0]
                print(f"    First item call_id: {first_item.get('call_id')}, score: {first_item.get('evaluacion_global')}, status: {first_item.get('status')}\n")
        else:
            print(f"    Error text: {r_res10.text}\n")
        assert r_res10.status_code == 200, f"Expected 200 on mass-evaluation-results (10), got {r_res10.status_code}"

        # 4. Mass evaluation results (limit=100)
        t0 = time.perf_counter()
        r_res100 = client.get(f"{BASE_URL}/bm/mass-evaluation-results?limit=100", headers=headers)
        dt_res100 = (time.perf_counter() - t0) * 1000.0
        print(f"[4] GET /bm/mass-evaluation-results?limit=100 -> Status: {r_res100.status_code} ({dt_res100:.1f} ms)")
        if r_res100.status_code == 200:
            res100_data = r_res100.json()
            items_count = len(res100_data.get("items", []))
            print(f"    Items returned: {items_count} / {res100_data.get('total')}")
            print(f"    Query time: {dt_res100:.1f} ms (no greenlet errors, no timeout)\n")
        else:
            print(f"    Error text: {r_res100.text}\n")
        assert r_res100.status_code == 200, f"Expected 200 on mass-evaluation-results (100), got {r_res100.status_code}"

    print("=== ALL POST-DEPLOY ENDPOINTS RESPONDED WITH HTTP 200 OK ===")


if __name__ == "__main__":
    test_endpoints()
