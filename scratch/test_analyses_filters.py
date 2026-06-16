import sys
import os
import asyncio
from datetime import datetime, timezone
from decimal import Decimal

# Add current directory to path
sys.path.insert(0, os.path.abspath("."))

# Mock environment to avoid SQLite engine creation crash during app.db import
os.environ["DATABASE_URL"] = "postgresql://emerald_borer:rxuxzrccfky5dhkotrpnv3dh@91.98.230.119:5432/n8n"

# Custom compilation rule for SQLite to handle PostgreSQL JSONB dialect in tests
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

import httpx
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.db import Base
from app.main import app
from app.dependencies import get_db, get_current_user
from app.models.users import User
from app.models.analyses import Analysis, CallAnalysisCurrent

# SQLite Memory engine specifically for our tests
engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autocommit=False, autoflush=False
)

# Dependency overrides
async def override_get_db():
    async with AsyncSessionLocal() as session:
        yield session

def mock_get_current_user():
    return User(
        user_id=1,
        username="admin",
        email="admin@doobot.ai",
        role="admin",
        hubspot_owner_id=None,
        password_hash="fakehash"
    )

app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_current_user] = mock_get_current_user

async def seed_data():
    async with engine.begin() as conn:
        # Create all tables in sqlite memory
        await conn.run_sync(Base.metadata.create_all)
        
    async with AsyncSessionLocal() as db:
        # Seed 4 analyses (history)
        a1 = Analysis(
            analysis_id=1,
            call_id="call_1",
            analysis_type="audio",
            evaluacion_global=Decimal("5.00"),
            agente_telefonico="LD",
            status="completed",
            run_ts=datetime.now(timezone.utc),
            fecha_eval=datetime.now(timezone.utc),
        )
        a2 = Analysis(
            analysis_id=2,
            call_id="call_2",
            analysis_type="audio",
            evaluacion_global=Decimal("7.50"),
            agente_telefonico="LD",
            status="completed",
            run_ts=datetime.now(timezone.utc),
            fecha_eval=datetime.now(timezone.utc),
        )
        a3 = Analysis(
            analysis_id=3,
            call_id="call_3",
            analysis_type="audio",
            evaluacion_global=Decimal("9.00"),
            agente_telefonico="LD",
            status="completed",
            run_ts=datetime.now(timezone.utc),
            fecha_eval=datetime.now(timezone.utc),
        )
        a4 = Analysis(
            analysis_id=4,
            call_id="call_4",
            analysis_type="audio",
            evaluacion_global=None,
            agente_telefonico="LD",
            status="completed",
            run_ts=datetime.now(timezone.utc),
            fecha_eval=datetime.now(timezone.utc),
        )
        db.add_all([a1, a2, a3, a4])
        await db.flush()

        # Seed 4 current analyses
        c1 = CallAnalysisCurrent(
            call_id="call_1",
            analysis_type="audio",
            latest_analysis_id=1,
            evaluacion_global=Decimal("5.00"),
            agente_telefonico="LD",
            status="completed",
            fecha_eval=datetime.now(timezone.utc),
        )
        c2 = CallAnalysisCurrent(
            call_id="call_2",
            analysis_type="audio",
            latest_analysis_id=2,
            evaluacion_global=Decimal("7.50"),
            agente_telefonico="LD",
            status="completed",
            fecha_eval=datetime.now(timezone.utc),
        )
        c3 = CallAnalysisCurrent(
            call_id="call_3",
            analysis_type="audio",
            latest_analysis_id=3,
            evaluacion_global=Decimal("9.00"),
            agente_telefonico="LD",
            status="completed",
            fecha_eval=datetime.now(timezone.utc),
        )
        c4 = CallAnalysisCurrent(
            call_id="call_4",
            analysis_type="audio",
            latest_analysis_id=4,
            evaluacion_global=None,
            agente_telefonico="LD",
            status="completed",
            fecha_eval=datetime.now(timezone.utc),
        )
        db.add_all([c1, c2, c3, c4])
        
        await db.commit()

async def run_integration_tests():
    print("=== SEEDING ANALYSES IN-MEMORY DATABASE ===")
    await seed_data()
    
    print("\n=== RUNNING FILTER AND GLOBAL SCORE INTEGRATION TEST ===")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        
        # Test 1: No filters, returns all 4 analyses (including the one with None score)
        print("Test 1: GET /bm/analyses (no filters)")
        r = await client.get("/bm/analyses")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}"
        data = r.json()
        assert len(data) == 4
        # Verify that global_score and evaluacion_global are present and correct
        scores = {item["call_id"]: item["global_score"] for item in data}
        assert scores["call_1"] == 5.0
        assert scores["call_2"] == 7.5
        assert scores["call_3"] == 9.0
        assert scores["call_4"] is None
        print("[OK] No filters returns all items and correctly populates global_score.")

        # Test 2: Filter by global_score_min = 7.0
        print("Test 2: GET /bm/analyses?global_score_min=7.0")
        r = await client.get("/bm/analyses?global_score_min=7.0")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 2, f"Expected 2 items, got {len(data)}"
        call_ids = [item["call_id"] for item in data]
        assert "call_2" in call_ids
        assert "call_3" in call_ids
        assert "call_1" not in call_ids
        assert "call_4" not in call_ids
        print("[OK] global_score_min filter works.")

        # Test 3: Filter by global_score_max = 8.0
        print("Test 3: GET /bm/analyses?global_score_max=8.0")
        r = await client.get("/bm/analyses?global_score_max=8.0")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 2
        call_ids = [item["call_id"] for item in data]
        assert "call_1" in call_ids
        assert "call_2" in call_ids
        assert "call_3" not in call_ids
        assert "call_4" not in call_ids
        print("[OK] global_score_max filter works.")

        # Test 4: Range filter (global_score_min = 7.0, global_score_max = 8.0)
        print("Test 4: GET /bm/analyses?global_score_min=7.0&global_score_max=8.0")
        r = await client.get("/bm/analyses?global_score_min=7.0&global_score_max=8.0")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["call_id"] == "call_2"
        assert data[0]["global_score"] == 7.5
        print("[OK] Range filters work.")

        # Test 5: Invalid range min > max (global_score_min = 8.0, global_score_max = 7.0)
        print("Test 5: GET /bm/analyses?global_score_min=8.0&global_score_max=7.0")
        r = await client.get("/bm/analyses?global_score_min=8.0&global_score_max=7.0")
        assert r.status_code == 422
        data = r.json()
        assert "global_score_min cannot be greater than global_score_max" in data["detail"]
        print("[OK] min > max validation throws 422.")

        # Test 6: Out-of-bounds parameters
        print("Test 6: GET /bm/analyses?global_score_min=-1.0")
        r = await client.get("/bm/analyses?global_score_min=-1.0")
        assert r.status_code == 422
        print("[OK] negative score min validation throws 422.")
        
        print("Test 6b: GET /bm/analyses?global_score_max=10.5")
        r = await client.get("/bm/analyses?global_score_max=10.5")
        assert r.status_code == 422
        print("[OK] score max > 10 validation throws 422.")

        # Test 7: Individual analysis detail response
        print("Test 7: GET /bm/analysis-detail?analysis_id=2")
        r = await client.get("/bm/analysis-detail?analysis_id=2")
        assert r.status_code == 200
        data = r.json()
        print("DEBUG DETAIL DATA:")
        import pprint
        pprint.pprint(data)
        assert "analysis" in data
        assert "summary" in data
        assert data["analysis"]["global_score"] == 7.5
        assert data["summary"]["global_score"] == 7.5
        assert float(data["analysis"]["evaluacion_global"]) == 7.5
        assert float(data["summary"]["evaluacion_global"]) == 7.5
        print("[OK] Individual analysis detail contains global_score in analysis and summary.")

        # Test 8: Current analyses endpoint with filters
        print("Test 8: GET /bm/analyses/current?global_score_min=6.0")
        r = await client.get("/bm/analyses/current?global_score_min=6.0")
        assert r.status_code == 200
        data = r.json()
        call_ids = [item["call_id"] for item in data]
        assert "call_2" in call_ids
        assert "call_3" in call_ids
        assert "call_1" not in call_ids
        assert "call_4" not in call_ids
        print("[OK] /analyses/current filtering works.")

        # Test 9: History analyses endpoint with filters
        print("Test 9: GET /bm/analyses/history?global_score_min=6.0")
        r = await client.get("/bm/analyses/history?global_score_min=6.0")
        assert r.status_code == 200
        data = r.json()
        call_ids = [item["call_id"] for item in data]
        assert "call_2" in call_ids
        assert "call_3" in call_ids
        assert "call_1" not in call_ids
        assert "call_4" not in call_ids
        print("[OK] /analyses/history filtering works.")

    print("\n=== ALL INTEGRATION TESTS COMPLETED SUCCESSFULLY ===")

if __name__ == "__main__":
    asyncio.run(run_integration_tests())
