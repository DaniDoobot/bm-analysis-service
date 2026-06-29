import os
import subprocess
from fastapi import APIRouter

router = APIRouter(tags=["Health"])


def get_version() -> str:
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    version_file = os.path.join(base_dir, "app", "version.txt")

    # Primary: read from git (always reflects the actually-deployed code)
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        ).decode("utf-8").strip()
        if commit:
            return commit
    except Exception:
        pass

    # Fallback: read from app/version.txt (written at build/deploy time)
    try:
        with open(version_file, "r") as f:
            val = f.read().strip()
            if val:
                return val
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

