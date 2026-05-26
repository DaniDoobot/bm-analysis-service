import sys
import os

# Add parent directory to path to allow imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.main import app

def test_automation_openapi_paths():
    """Verify that all required automation paths are correctly registered in FastAPI/OpenAPI schema."""
    print("=== STARTING OPENAPI ROUTES VERIFICATION ===")
    
    # Generate OpenAPI schema
    openapi = app.openapi()
    paths = openapi.get("paths", {})
    
    required_paths = [
        "/bm/mass-analysis/automations",
        "/bm/mass-analysis/automations/{automation_id}",
        "/bm/mass-analysis/automations/{automation_id}/run-now",
        "/bm/mass-analysis/automations/{automation_id}/runs"
    ]
    
    missing_paths = []
    for p in required_paths:
        if p not in paths:
            missing_paths.append(p)
            print(f"[-] MISSING PATH: {p}")
        else:
            print(f"[+] FOUND PATH: {p}")
            
    # Also verify the HTTP methods supported for those paths
    if not missing_paths:
        print("[+] All required automation paths are present!")
        
        # Check specific methods
        # GET & POST /bm/mass-analysis/automations
        assert "get" in paths["/bm/mass-analysis/automations"]
        assert "post" in paths["/bm/mass-analysis/automations"]
        
        # GET, PATCH & DELETE /bm/mass-analysis/automations/{automation_id}
        assert "get" in paths["/bm/mass-analysis/automations/{automation_id}"]
        assert "patch" in paths["/bm/mass-analysis/automations/{automation_id}"]
        assert "delete" in paths["/bm/mass-analysis/automations/{automation_id}"]
        
        # POST /bm/mass-analysis/automations/{automation_id}/run-now
        assert "post" in paths["/bm/mass-analysis/automations/{automation_id}/run-now"]
        
        # GET /bm/mass-analysis/automations/{automation_id}/runs
        assert "get" in paths["/bm/mass-analysis/automations/{automation_id}/runs"]
        
        print("[+] All associated HTTP methods (GET, POST, PATCH, DELETE, run-now) are correctly registered.")
    
    assert len(missing_paths) == 0, f"Missing required paths in OpenAPI: {missing_paths}"
    print("=== OPENAPI ROUTES VERIFICATION PASSED SUCCESSFULLY ===")

if __name__ == "__main__":
    test_automation_openapi_paths()
