import os
import sys
import unittest
from httpx import AsyncClient, ASGITransport

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///mass_job_derivation_test.db"

# Safety Confirmation Check
db_url = os.environ.get("DATABASE_URL", "")
if "91.98.230.119" in db_url or "n8n" in db_url.lower():
    raise RuntimeError("CRITICAL: Test execution was blocked because DATABASE_URL points to production!")

# Setup path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.teams import UserServiceAssociation
from app.models.prompts import Prompt, PromptVersion
from app.utils.security import create_access_token
from app.main import app

from sqlalchemy.ext.asyncio import AsyncSession


class TestMassEvaluationJobDerivation(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"

        if os.path.exists("mass_job_derivation_test.db"):
            try:
                os.remove("mass_job_derivation_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.session_factory = get_engine()
        async with AsyncSession(self.session_factory) as db:
            # 1. Companies
            self.c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            self.c2 = Company(company_id=2, company_name="GesDent", company_key="gesdent", is_active=True)
            db.add_all([self.c1, self.c2])
            await db.flush()

            # 2. Services
            self.s1 = Service(service_id=1, service_name="Front Desk Boston", service_key="front-boston", company_id=self.c1.company_id)
            self.s2 = Service(service_id=2, service_name="Experiencia GesDent", service_key="experiencia-gesdent", company_id=self.c2.company_id)
            self.s3 = Service(service_id=3, service_name="Asesores Boston", service_key="asesores-boston", company_id=self.c1.company_id)
            db.add_all([self.s1, self.s2, self.s3])
            await db.flush()

            # 3. Users
            self.u_super = User(user_id=1, username="super_admin", email="super@test.com", role="admin", password_hash="dummy")
            self.u_boston_admin = User(user_id=2, username="boston_admin", email="boston_admin@test.com", role="company_admin", company_id=self.c1.company_id, password_hash="dummy")
            self.u_gesdent_admin = User(user_id=3, username="gesdent_admin", email="gesdent_admin@test.com", role="company_admin", company_id=self.c2.company_id, password_hash="dummy")
            self.u_svc_manager = User(user_id=4, username="boston_svc_mgr", email="mgr@test.com", role="responsable_servicio", company_id=self.c1.company_id, password_hash="dummy")
            db.add_all([self.u_super, self.u_boston_admin, self.u_gesdent_admin, self.u_svc_manager])
            await db.flush()

            db.add(UserServiceAssociation(user_id=self.u_svc_manager.user_id, service_id=1))
            await db.flush()

            # 4. Prompts
            # Prompt 101: Boston / Front (service_id=1, company_id=1)
            self.p101 = Prompt(prompt_id=101, prompt_name="Front Boston Audio", prompt_type="audio", service_id=1, company_id=1, is_active=True)
            self.v101 = PromptVersion(id=101, prompt_id=101, prompt="Test prompt 101", version_label="v1", is_current=True)
            
            # Prompt 102: Boston / Asesores (service_id=3, company_id=1)
            self.p102 = Prompt(prompt_id=102, prompt_name="Asesores Boston Audio", prompt_type="audio", service_id=3, company_id=1, is_active=True)
            self.v102 = PromptVersion(id=102, prompt_id=102, prompt="Test prompt 102", version_label="v1", is_current=True)

            # Prompt 103: Gesdent / Experiencia (service_id=2, company_id=2)
            self.p103 = Prompt(prompt_id=103, prompt_name="GesDent Audio", prompt_type="audio", service_id=2, company_id=2, is_active=True)
            self.v103 = PromptVersion(id=103, prompt_id=103, prompt="Test prompt 103", version_label="v1", is_current=True)

            # Prompt 104: Legacy NULL company_id but service_id=1
            self.p104 = Prompt(prompt_id=104, prompt_name="Legacy Front Audio", prompt_type="audio", service_id=1, company_id=None, is_active=True)
            self.v104 = PromptVersion(id=104, prompt_id=104, prompt="Test prompt 104", version_label="v1", is_current=True)

            # Prompt 105: Legacy NULL company_id AND NULL service_id
            self.p105 = Prompt(prompt_id=105, prompt_name="Orphan Audio", prompt_type="audio", service_id=None, company_id=None, is_active=True)
            self.v105 = PromptVersion(id=105, prompt_id=105, prompt="Test prompt 105", version_label="v1", is_current=True)

            db.add_all([self.p101, self.v101, self.p102, self.v102, self.p103, self.v103, self.p104, self.v104, self.p105, self.v105])
            await db.commit()

        # Build Tokens
        self.t_super = create_access_token({"user_id": 1, "email": "super@test.com"})
        self.t_boston_admin = create_access_token({"user_id": 2, "email": "boston_admin@test.com"})
        self.t_gesdent_admin = create_access_token({"user_id": 3, "email": "gesdent_admin@test.com"})
        self.t_svc_manager = create_access_token({"user_id": 4, "email": "mgr@test.com"})

        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")

    async def asyncTearDown(self):
        if os.path.exists("mass_job_derivation_test.db"):
            try:
                os.remove("mass_job_derivation_test.db")
            except Exception:
                pass

    async def test_super_admin_create_job_derives_company_and_service(self):
        """super_admin creates job with prompt_id (Front) without sending company_id/service_id -> job receives company_id=1, service_id=1."""
        res = await self.client.post(
            "/bm/mass-evaluation-jobs",
            json={
                "job_name": "Super Admin Job Front",
                "prompt_id": 101,
            },
            headers={"Authorization": f"Bearer {self.t_super}"}
        )
        self.assertEqual(res.status_code, 201, res.text)
        data = res.json()
        self.assertEqual(data["company_id"], 1)
        self.assertEqual(data["service_id"], 1)

    async def test_super_admin_create_job_asesores(self):
        """super_admin creates job with prompt_id (Asesores) -> job receives service_id=3."""
        res = await self.client.post(
            "/bm/mass-evaluation-jobs",
            json={
                "job_name": "Super Admin Job Asesores",
                "prompt_id": 102,
            },
            headers={"Authorization": f"Bearer {self.t_super}"}
        )
        self.assertEqual(res.status_code, 201, res.text)
        data = res.json()
        self.assertEqual(data["company_id"], 1)
        self.assertEqual(data["service_id"], 3)

    async def test_payload_mismatch_returns_400(self):
        """payload with prompt_id (Front, service=1) but service_id=3 (Asesores) -> 400 Bad Request."""
        res = await self.client.post(
            "/bm/mass-evaluation-jobs",
            json={
                "job_name": "Mismatch Job",
                "prompt_id": 101,
                "service_id": 3,
            },
            headers={"Authorization": f"Bearer {self.t_super}"}
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("La empresa/servicio no coincide", res.json()["detail"])

    async def test_company_admin_boston_creates_job_success(self):
        """company_admin Boston creates job with Boston prompt -> 201."""
        res = await self.client.post(
            "/bm/mass-evaluation-jobs",
            json={
                "job_name": "Boston Admin Job",
                "prompt_id": 101,
            },
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 201, res.text)
        data = res.json()
        self.assertEqual(data["company_id"], 1)

    async def test_company_admin_boston_creates_job_other_company_403(self):
        """company_admin Boston tries to create job with GesDent prompt -> 403 Forbidden."""
        res = await self.client.post(
            "/bm/mass-evaluation-jobs",
            json={
                "job_name": "Cross-company Job",
                "prompt_id": 103,  # GesDent
            },
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("pertenece a otra empresa", res.json()["detail"])

    async def test_service_manager_allowed_service_success(self):
        """service_manager (allowed_service_ids=[1]) creates job for service 1 -> 201."""
        res = await self.client.post(
            "/bm/mass-evaluation-jobs",
            json={
                "job_name": "Svc Mgr Allowed Job",
                "prompt_id": 101,  # service_id=1
            },
            headers={"Authorization": f"Bearer {self.t_svc_manager}"}
        )
        self.assertEqual(res.status_code, 201, res.text)
        data = res.json()
        self.assertEqual(data["service_id"], 1)

    async def test_service_manager_disallowed_service_403(self):
        """service_manager (allowed_service_ids=[1]) tries to create job for service 3 (Asesores) -> 403."""
        res = await self.client.post(
            "/bm/mass-evaluation-jobs",
            json={
                "job_name": "Svc Mgr Disallowed Job",
                "prompt_id": 102,  # service_id=3
            },
            headers={"Authorization": f"Bearer {self.t_svc_manager}"}
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("servicio no asignado", res.json()["detail"])

    async def test_prompt_null_company_derives_from_service(self):
        """Prompt with company_id=NULL but service_id=1 derives company_id=1 from Service."""
        res = await self.client.post(
            "/bm/mass-evaluation-jobs",
            json={
                "job_name": "Legacy Prompt Job",
                "prompt_id": 104,
            },
            headers={"Authorization": f"Bearer {self.t_super}"}
        )
        self.assertEqual(res.status_code, 201, res.text)
        data = res.json()
        self.assertEqual(data["company_id"], 1)
        self.assertEqual(data["service_id"], 1)

    async def test_prompt_null_company_and_null_service_error_400(self):
        """Prompt with company_id=NULL and service_id=NULL -> 400 Bad Request."""
        res = await self.client.post(
            "/bm/mass-evaluation-jobs",
            json={
                "job_name": "Orphan Prompt Job",
                "prompt_id": 105,
            },
            headers={"Authorization": f"Bearer {self.t_super}"}
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("empresa asociada", res.json()["detail"])


if __name__ == "__main__":
    import asyncio
    asyncio.run(unittest.main())

