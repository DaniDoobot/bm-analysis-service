"""
Smoke test to ensure the entire application compiles and imports correctly.
This prevents runtime NameErrors, missing import errors, or other syntax/annotation errors on startup.
"""
import os
import sys

# Override DATABASE_URL to a safe local SQLite URL before imports to avoid safety checks
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///trainer_test.db"

# Add root directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

def test_import_main():
    print("Testing application imports and compilation...")
    try:
        from app.main import app
        print("[OK] FastAPI application (app.main) imported successfully.")
        
        # Verify app has some routers loaded to confirm it is not just an empty app
        routers_count = len(app.routes)
        print(f"[OK] Total registered routes: {routers_count}")
        assert routers_count > 0, "No routes registered on application."
        
        print("=== APP IMPORT SMOKE TEST PASSED ===")
    except Exception as e:
        print("=== APP IMPORT SMOKE TEST FAILED ===")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    test_import_main()
