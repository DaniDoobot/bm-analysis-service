import os
import subprocess
from fastapi import APIRouter

router = APIRouter(tags=["Health"])


def get_version() -> str:
    # Try reading app/version.txt (standard deployment)
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    version_file = os.path.join(base_dir, "app", "version.txt")
    if os.path.exists(version_file):
        try:
            with open(version_file, "r") as f:
                val = f.read().strip()
                if val:
                    return val
        except Exception:
            pass

    # Fallback to local git
    try:
        commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL)
        return commit.decode("utf-8").strip()
    except Exception:
        pass

    return "unknown"


@router.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "service": "bm-analysis-service",
        "commit": get_version()
    }

