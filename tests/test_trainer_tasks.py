import os
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_trainer_tasks.db"

from sqlalchemy import BigInteger, delete, select, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from pydantic import ValidationError
from fastapi import HTTPException

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import Base, get_engine
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, AgentTeamAssociation
from app.models.users import User
from app.models.trainer import (
    TrainerSimulation,
    TrainerSession,
    TrainerEvaluation,
    TrainerEvaluationConfig,
    TrainerTask,
    TrainerTaskItem,
    TrainerTaskAssignee,
    TrainerTaskAttemptAllocation,
)
from app.schemas.trainer_tasks import (
    TaskCreate,
    TaskItemCreate,
    TaskUpdate,
)
from app.services.trainer_task_service import TrainerTaskService
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.main import app


class TestTrainerTasks(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            await db.execute(delete(TrainerTaskAttemptAllocation))
            await db.execute(delete(TrainerTaskAssignee))
            await db.execute(delete(TrainerTaskItem))
            await db.execute(delete(TrainerTask))
            await db.execute(delete(TrainerEvaluation))
            await db.execute(delete(TrainerSession))
            await db.execute(delete(TrainerSimulation))
            await db.execute(delete(TrainerEvaluationConfig))
            await db.execute(delete(AgentTeamAssociation))
            await db.execute(delete(Team))
            await db.execute(delete(User))
            await db.execute(delete(Service))
            await db.execute(delete(Company))

            # Base seeds
            self.c1 = Company(company_id=1, company_name="Company 1", company_key="comp1", is_active=True)
            self.c2 = Company(company_id=2, company_name="Company 2", company_key="comp2", is_active=True)
            db.add_all([self.c1, self.c2])

            self.s1 = Service(service_id=10, company_id=1, service_name="Customer Service C1", service_key="cs_c1", is_active=True)
            self.s2 = Service(service_id=20, company_id=1, service_name="Sales C1", service_key="sales_c1", is_active=True)
            self.s3 = Service(service_id=30, company_id=2, service_name="Support C2", service_key="support_c2", is_active=True)
            db.add_all([self.s1, self.s2, self.s3])

            self.team1 = Team(team_id=100, company_id=1, service_id=10, team_name="Alpha Team", is_active=True)
            self.team2 = Team(team_id=200, company_id=1, service_id=10, team_name="Beta Team", is_active=True)
            db.add_all([self.team1, self.team2])

            # Users
            self.u_admin = User(
                user_id=1, company_id=1, primary_service_id=10, username="c1_admin",
                email="admin@c1.com", role="company_admin", password_hash="x", is_active=True
            )
            self.u_tl = User(
                user_id=2, company_id=1, primary_service_id=10, primary_team_id=100, username="c1_tl",
                email="tl@c1.com", role="coordinador_equipo", password_hash="x", is_active=True
            )
            self.u_agent1 = User(
                user_id=3, company_id=1, primary_service_id=10, primary_team_id=100,
                hubspot_owner_id="agent_01", username="agent1", email="agent1@c1.com",
                role="agent", password_hash="x", is_active=True
            )
            self.u_agent2 = User(
                user_id=4, company_id=1, primary_service_id=10, primary_team_id=200,
                hubspot_owner_id="agent_02", username="agent2", email="agent2@c1.com",
                role="agent", password_hash="x", is_active=True
            )
            self.u_agent3 = User(
                user_id=5, company_id=1, primary_service_id=20,
                hubspot_owner_id="agent_03", username="agent3", email="agent3@c1.com",
                role="agent", password_hash="x", is_active=True
            )
            self.u_agent_c2 = User(
                user_id=6, company_id=2, primary_service_id=30,
                hubspot_owner_id="agent_c2", username="agentc2", email="agentc2@c2.com",
                role="agent", password_hash="x", is_active=True
            )
            db.add_all([self.u_admin, self.u_tl, self.u_agent1, self.u_agent2, self.u_agent3, self.u_agent_c2])

            self.sim1 = TrainerSimulation(
                simulation_id=101, company_id=1, service_id=10, name="Sim 1 - CS",
                code="SIM-CS-01", roleplay_prompt="Roleplay 1", status="published"
            )
            self.sim2 = TrainerSimulation(
                simulation_id=102, company_id=1, service_id=10, name="Sim 2 - CS",
                code="SIM-CS-02", roleplay_prompt="Roleplay 2", status="published"
            )
            self.sim_s2 = TrainerSimulation(
                simulation_id=201, company_id=1, service_id=20, name="Sim Sales",
                code="SIM-SALES-01", roleplay_prompt="Roleplay Sales", status="published"
            )
            self.sim_c2 = TrainerSimulation(
                simulation_id=301, company_id=2, service_id=30, name="Sim C2",
                code="SIM-C2-01", roleplay_prompt="Roleplay C2", status="published"
            )
            db.add_all([self.sim1, self.sim2, self.sim_s2, self.sim_c2])

            await db.commit()

        # Contexts
        self.ctx_super = TenantContext(
            user_id=999, raw_role="super_admin", normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True, allowed_company_ids=[1, 2], allowed_service_ids=None
        )
        self.ctx_c1_admin = TenantContext(
            user_id=1, raw_role="company_admin", normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False, allowed_company_ids=[1], allowed_service_ids=None
        )
        self.ctx_sm_s1 = TenantContext(
            user_id=10, raw_role="service_manager", normalized_role=InternalRole.SERVICE_MANAGER,
            is_super_admin=False, allowed_company_ids=[1], allowed_service_ids=[10]
        )
        self.ctx_tl_t1 = TenantContext(
            user_id=2, raw_role="team_coordinator", normalized_role=InternalRole.TEAM_COORDINATOR,
            is_super_admin=False, allowed_company_ids=[1], allowed_service_ids=[10],
            allowed_team_ids=[100], allowed_agent_ids=["agent_01"]
        )
        self.ctx_c2_admin = TenantContext(
            user_id=99, raw_role="company_admin", normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False, allowed_company_ids=[2], allowed_service_ids=None
        )

    # ── 1. Task Creation Tests ──────────────────────────────────────────────────

    async def test_01_create_task_single_training(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            due = datetime.now(timezone.utc) + timedelta(days=7)
            payload = TaskCreate(
                title="Tarea Simple", description="Desc",
                due_at=due, items=[TaskItemCreate(simulation_id=101, required_attempts=2)],
                agent_ids=["agent_01"]
            )
            res = await TrainerTaskService.create_task(db, payload, self.ctx_c1_admin, user_id=1)
            self.assertEqual(res["title"], "Tarea Simple")
            self.assertEqual(res["status"], "active")
            self.assertEqual(len(res["items"]), 1)
            self.assertEqual(res["items"][0]["required_attempts"], 2)
            self.assertEqual(len(res["assignees"]), 1)
            self.assertEqual(res["assignees"][0]["hubspot_owner_id"], "agent_01")
            self.assertEqual(res["assignees"][0]["status"], "pending")

    async def test_02_create_task_multiple_trainings(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            payload = TaskCreate(
                title="Tarea Multiple",
                items=[
                    TaskItemCreate(simulation_id=101, required_attempts=1),
                    TaskItemCreate(simulation_id=102, required_attempts=3),
                ],
                agent_ids=["agent_01", "agent_02"]
            )
            res = await TrainerTaskService.create_task(db, payload, self.ctx_c1_admin, user_id=1)
            self.assertEqual(len(res["items"]), 2)
            self.assertEqual(len(res["assignees"]), 2)
            self.assertEqual(res["total_assignees"], 2)

    async def test_03_create_task_cross_service_simulation_rejection(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # 101 is service 10, 201 is service 20 -> should reject
            payload = TaskCreate(
                title="Invalid Cross Service Sim",
                items=[
                    TaskItemCreate(simulation_id=101, required_attempts=1),
                    TaskItemCreate(simulation_id=201, required_attempts=1),
                ],
                agent_ids=["agent_01"]
            )
            with self.assertRaises(HTTPException) as cm:
                await TrainerTaskService.create_task(db, payload, self.ctx_c1_admin, user_id=1)
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("al mismo servicio", cm.exception.detail)

    async def test_04_create_task_cross_service_agent_rejection(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            payload = TaskCreate(
                title="Invalid Cross Service Agent",
                items=[TaskItemCreate(simulation_id=101, required_attempts=1)],
                agent_ids=["agent_03"]  # agent_03 is service 20, simulation 101 is service 10
            )
            with self.assertRaises(HTTPException) as cm:
                await TrainerTaskService.create_task(db, payload, self.ctx_c1_admin, user_id=1)
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("no es elegible", cm.exception.detail)

    async def test_05_create_task_due_at_default_null(self):
        payload = TaskCreate(
            title="Task Sin Fecha",
            items=[TaskItemCreate(simulation_id=101, required_attempts=1)],
            agent_ids=["agent_01"]
        )
        self.assertIsNone(payload.due_at)
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            res = await TrainerTaskService.create_task(db, payload, self.ctx_c1_admin, user_id=1)
            self.assertIsNone(res["due_at"])

    async def test_06_create_task_required_attempts_zero_rejected(self):
        with self.assertRaises(ValidationError):
            TaskItemCreate(simulation_id=101, required_attempts=0)

    async def test_07_create_task_required_attempts_negative_rejected(self):
        with self.assertRaises(ValidationError):
            TaskItemCreate(simulation_id=101, required_attempts=-2)

    async def test_08_create_task_empty_items_rejected(self):
        with self.assertRaises(ValidationError):
            TaskCreate(
                title="Empty items",
                items=[], agent_ids=["agent_01"]
            )

    async def test_09_create_task_empty_assignees_rejected(self):
        with self.assertRaises(ValidationError):
            TaskCreate(
                title="Empty assignees",
                items=[TaskItemCreate(simulation_id=101, required_attempts=1)],
                agent_ids=[]
            )

    async def test_10_create_task_duplicate_simulations_in_items_rejected(self):
        with self.assertRaises(ValidationError):
            TaskCreate(
                title="Duplicate Sims",
                items=[
                    TaskItemCreate(simulation_id=101, required_attempts=1),
                    TaskItemCreate(simulation_id=101, required_attempts=2),
                ],
                agent_ids=["agent_01"]
            )

    async def test_11_create_task_duplicate_agents_in_assignees_rejected(self):
        with self.assertRaises(ValidationError):
            TaskCreate(
                title="Duplicate Agents",
                items=[TaskItemCreate(simulation_id=101, required_attempts=1)],
                agent_ids=["agent_01", "agent_01"]
            )

    async def test_12_create_task_tenant_scoping_company_id(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # Company 1 admin trying to create task with Company 2 simulation (301)
            payload = TaskCreate(
                title="Malicious C2 Task",
                items=[TaskItemCreate(simulation_id=301, required_attempts=1)],
                agent_ids=["agent_c2"]
            )
            with self.assertRaises(HTTPException) as cm:
                await TrainerTaskService.create_task(db, payload, self.ctx_c1_admin, user_id=1)
            self.assertEqual(cm.exception.status_code, 403)

    # ── 2. Permission Tests ─────────────────────────────────────────────────────

    async def test_13_create_task_super_admin_permissions(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            payload = TaskCreate(
                title="Super Admin C2 Task",
                items=[TaskItemCreate(simulation_id=301, required_attempts=1)],
                agent_ids=["agent_c2"]
            )
            res = await TrainerTaskService.create_task(db, payload, self.ctx_super, user_id=999)
            self.assertEqual(res["company_id"], 2)
            self.assertEqual(res["service_id"], 30)

    async def test_14_create_task_company_admin_permissions(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            payload = TaskCreate(
                title="C1 Admin Task",
                items=[TaskItemCreate(simulation_id=101, required_attempts=1)],
                agent_ids=["agent_01"]
            )
            res = await TrainerTaskService.create_task(db, payload, self.ctx_c1_admin, user_id=1)
            self.assertEqual(res["company_id"], 1)

    async def test_15_create_task_service_manager_permissions(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            payload = TaskCreate(
                title="SM Allowed S1 Task",
                items=[TaskItemCreate(simulation_id=101, required_attempts=1)],
                agent_ids=["agent_01"]
            )
            res = await TrainerTaskService.create_task(db, payload, self.ctx_sm_s1, user_id=10)
            self.assertEqual(res["service_id"], 10)

    async def test_16_create_task_service_manager_forbidden_other_service(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # SM allowed service [10] tries to create task with sim 201 (service 20)
            payload = TaskCreate(
                title="SM Forbidden S2 Task",
                items=[TaskItemCreate(simulation_id=201, required_attempts=1)],
                agent_ids=["agent_03"]
            )
            with self.assertRaises(HTTPException) as cm:
                await TrainerTaskService.create_task(db, payload, self.ctx_sm_s1, user_id=10)
            self.assertEqual(cm.exception.status_code, 403)

    async def test_17_create_task_team_leader_permissions(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # TL team 100 assigning agent_01 (in team 100) -> OK
            payload = TaskCreate(
                title="TL Team 100 Task",
                items=[TaskItemCreate(simulation_id=101, required_attempts=1)],
                agent_ids=["agent_01"]
            )
            res = await TrainerTaskService.create_task(db, payload, self.ctx_tl_t1, user_id=2)
            self.assertEqual(res["assignees"][0]["hubspot_owner_id"], "agent_01")

    async def test_18_create_task_team_leader_forbidden_agent_outside_team(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # TL team 100 trying to assign agent_02 (in team 200) -> 400
            payload = TaskCreate(
                title="TL Forbidden Team Task",
                items=[TaskItemCreate(simulation_id=101, required_attempts=1)],
                agent_ids=["agent_02"]
            )
            with self.assertRaises(HTTPException) as cm:
                await TrainerTaskService.create_task(db, payload, self.ctx_tl_t1, user_id=2)
            self.assertEqual(cm.exception.status_code, 400)
            self.assertIn("no es elegible", cm.exception.detail)

    # ── 3. List & Filter Tests ──────────────────────────────────────────────────

    async def test_19_list_tasks_multi_tenant_isolation(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # Create task for C1 and task for C2
            t1 = TrainerTask(company_id=1, service_id=10, title="Task C1", status="active")
            t2 = TrainerTask(company_id=2, service_id=30, title="Task C2", status="active")
            db.add_all([t1, t2])
            await db.commit()

            tasks_c1, total_c1 = await TrainerTaskService.list_tasks(db, self.ctx_c1_admin)
            tasks_c2, total_c2 = await TrainerTaskService.list_tasks(db, self.ctx_c2_admin)

            self.assertEqual(total_c1, 1)
            self.assertEqual(tasks_c1[0]["title"], "Task C1")
            self.assertEqual(total_c2, 1)
            self.assertEqual(tasks_c2[0]["title"], "Task C2")

    async def test_20_list_tasks_aggregated_progress(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t = TrainerTask(task_id=1, company_id=1, service_id=10, title="Progress Task", status="active")
            db.add(t)
            await db.flush()
            item = TrainerTaskItem(task_id=1, simulation_id=101, required_attempts=2, order_index=0)
            db.add(item)
            await db.flush()
            a1 = TrainerTaskAssignee(task_id=1, company_id=1, hubspot_owner_id="agent_01", status="in_progress")
            a2 = TrainerTaskAssignee(task_id=1, company_id=1, hubspot_owner_id="agent_02", status="completed")
            db.add_all([a1, a2])
            await db.flush()
            # 1 attempt for a1, 2 attempts for a2
            alloc1 = TrainerTaskAttemptAllocation(task_assignee_id=a1.task_assignee_id, task_item_id=item.task_item_id, session_id=1001)
            alloc2 = TrainerTaskAttemptAllocation(task_assignee_id=a2.task_assignee_id, task_item_id=item.task_item_id, session_id=1002)
            alloc3 = TrainerTaskAttemptAllocation(task_assignee_id=a2.task_assignee_id, task_item_id=item.task_item_id, session_id=1003)
            db.add_all([alloc1, alloc2, alloc3])
            await db.commit()

            tasks, total = await TrainerTaskService.list_tasks(db, self.ctx_c1_admin)
            self.assertEqual(total, 1)
            res = tasks[0]
            self.assertEqual(res["total_assignees"], 2)
            self.assertEqual(res["completed_assignees"], 1)
            self.assertEqual(res["progress_completed_attempts"], 3)
            self.assertEqual(res["progress_required_attempts"], 4)
            self.assertEqual(res["progress_percentage"], 75.0)

    async def test_21_list_tasks_filter_by_service_id(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t1 = TrainerTask(company_id=1, service_id=10, title="T S10", status="active")
            t2 = TrainerTask(company_id=1, service_id=20, title="T S20", status="active")
            db.add_all([t1, t2])
            await db.commit()

            tasks, total = await TrainerTaskService.list_tasks(db, self.ctx_c1_admin, service_id=10)
            self.assertEqual(total, 1)
            self.assertEqual(tasks[0]["title"], "T S10")

    async def test_22_list_tasks_filter_by_status(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t1 = TrainerTask(company_id=1, service_id=10, title="T Active", status="active")
            t2 = TrainerTask(company_id=1, service_id=10, title="T Cancelled", status="cancelled")
            db.add_all([t1, t2])
            await db.commit()

            tasks, total = await TrainerTaskService.list_tasks(db, self.ctx_c1_admin, status="cancelled")
            self.assertEqual(total, 1)
            self.assertEqual(tasks[0]["title"], "T Cancelled")

    # ── 4. Task Detail Tests ────────────────────────────────────────────────────

    async def test_23_get_task_detail_success(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t = TrainerTask(task_id=10, company_id=1, service_id=10, title="Detail Task", status="active")
            db.add(t)
            await db.flush()
            item = TrainerTaskItem(task_id=10, simulation_id=101, required_attempts=1, order_index=0)
            a = TrainerTaskAssignee(task_id=10, company_id=1, hubspot_owner_id="agent_01", status="pending")
            db.add_all([item, a])
            await db.commit()

            detail = await TrainerTaskService.get_task_detail(db, 10, self.ctx_c1_admin)
            self.assertEqual(detail["title"], "Detail Task")
            self.assertEqual(len(detail["items"]), 1)
            self.assertEqual(len(detail["assignees"]), 1)
            self.assertEqual(detail["assignees"][0]["hubspot_owner_id"], "agent_01")

    async def test_24_get_task_detail_cross_company_forbidden(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t = TrainerTask(task_id=20, company_id=2, service_id=30, title="C2 Secret Task", status="active")
            db.add(t)
            await db.commit()

            with self.assertRaises(HTTPException) as cm:
                await TrainerTaskService.get_task_detail(db, 20, self.ctx_c1_admin)
            self.assertEqual(cm.exception.status_code, 403)

    # ── 5. Patch & Cancel Tests ─────────────────────────────────────────────────

    async def test_25_patch_task_title_and_description(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t = TrainerTask(task_id=30, company_id=1, service_id=10, title="Old Title", description="Old Desc", status="active")
            db.add(t)
            await db.commit()

            patch_payload = TaskUpdate(title="New Title", description="New Desc")
            updated = await TrainerTaskService.patch_task(db, 30, patch_payload, self.ctx_c1_admin)
            self.assertEqual(updated["title"], "New Title")
            self.assertEqual(updated["description"], "New Desc")

    async def test_26_patch_task_due_at(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t = TrainerTask(task_id=31, company_id=1, service_id=10, title="Task Due", status="active")
            db.add(t)
            await db.commit()

            new_due = datetime.now(timezone.utc) + timedelta(days=5)
            updated = await TrainerTaskService.patch_task(db, 31, TaskUpdate(due_at=new_due), self.ctx_c1_admin)
            self.assertIsNotNone(updated["due_at"])

            # Reset due_at to None
            updated2 = await TrainerTaskService.patch_task(db, 31, TaskUpdate(due_at=None), self.ctx_c1_admin)
            self.assertIsNone(updated2["due_at"])

    async def test_27_patch_task_empty_title_rejected(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t = TrainerTask(task_id=32, company_id=1, service_id=10, title="Valid Title", status="active")
            db.add(t)
            await db.commit()

            with self.assertRaises(HTTPException) as cm:
                await TrainerTaskService.patch_task(db, 32, TaskUpdate(title="   "), self.ctx_c1_admin)
            self.assertEqual(cm.exception.status_code, 400)

    async def test_28_patch_task_cannot_modify_items_or_assignees(self):
        # Verify TaskUpdate model only has title, description, due_at
        fields = set(TaskUpdate.model_fields.keys())
        self.assertEqual(fields, {"title", "description", "due_at"})

    async def test_29_patch_task_cross_company_forbidden(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t = TrainerTask(task_id=33, company_id=2, service_id=30, title="C2 Task", status="active")
            db.add(t)
            await db.commit()

            with self.assertRaises(HTTPException) as cm:
                await TrainerTaskService.patch_task(db, 33, TaskUpdate(title="Hacked"), self.ctx_c1_admin)
            self.assertEqual(cm.exception.status_code, 403)

    async def test_30_cancel_task_success(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t = TrainerTask(task_id=40, company_id=1, service_id=10, title="To Cancel", status="active")
            db.add(t)
            await db.flush()
            a1 = TrainerTaskAssignee(task_id=40, company_id=1, hubspot_owner_id="agent_01", status="pending")
            a2 = TrainerTaskAssignee(task_id=40, company_id=1, hubspot_owner_id="agent_02", status="in_progress")
            db.add_all([a1, a2])
            await db.commit()

            cancelled = await TrainerTaskService.cancel_task(db, 40, self.ctx_c1_admin)
            self.assertEqual(cancelled["status"], "cancelled")
            for a in cancelled["assignees"]:
                self.assertEqual(a["status"], "cancelled")

    async def test_31_cancel_task_preserves_completed_assignees(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t = TrainerTask(task_id=41, company_id=1, service_id=10, title="To Cancel Partial", status="active")
            db.add(t)
            await db.flush()
            a1 = TrainerTaskAssignee(task_id=41, company_id=1, hubspot_owner_id="agent_01", status="completed")
            a2 = TrainerTaskAssignee(task_id=41, company_id=1, hubspot_owner_id="agent_02", status="in_progress")
            db.add_all([a1, a2])
            await db.commit()

            cancelled = await TrainerTaskService.cancel_task(db, 41, self.ctx_c1_admin)
            self.assertEqual(cancelled["status"], "cancelled")
            assignee_map = {a["hubspot_owner_id"]: a["status"] for a in cancelled["assignees"]}
            self.assertEqual(assignee_map["agent_01"], "completed")
            self.assertEqual(assignee_map["agent_02"], "cancelled")

    async def test_32_cancel_task_preserves_allocations(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t = TrainerTask(task_id=42, company_id=1, service_id=10, title="To Cancel Alloc", status="active")
            db.add(t)
            await db.flush()
            item = TrainerTaskItem(task_id=42, simulation_id=101, required_attempts=1, order_index=0)
            a = TrainerTaskAssignee(task_id=42, company_id=1, hubspot_owner_id="agent_01", status="in_progress")
            db.add_all([item, a])
            await db.flush()
            alloc = TrainerTaskAttemptAllocation(task_assignee_id=a.task_assignee_id, task_item_id=item.task_item_id, session_id=9999)
            db.add(alloc)
            await db.commit()

            await TrainerTaskService.cancel_task(db, 42, self.ctx_c1_admin)

            stmt = select(TrainerTaskAttemptAllocation).where(TrainerTaskAttemptAllocation.session_id == 9999)
            res = await db.execute(stmt)
            persisted = res.scalars().first()
            self.assertIsNotNone(persisted)

    async def test_33_cancel_task_cross_company_forbidden(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t = TrainerTask(task_id=43, company_id=2, service_id=30, title="C2 Task", status="active")
            db.add(t)
            await db.commit()

            with self.assertRaises(HTTPException) as cm:
                await TrainerTaskService.cancel_task(db, 43, self.ctx_c1_admin)
            self.assertEqual(cm.exception.status_code, 403)

    async def test_34_no_delete_endpoint(self):
        methods_on_task_path = []
        for route in app.routes:
            if hasattr(route, "path") and route.path == "/bm/trainer/tasks/{task_id}":
                methods_on_task_path.extend(route.methods)
        self.assertNotIn("DELETE", methods_on_task_path)

    # ── 6. Progress & Allocation Tests ──────────────────────────────────────────

    async def test_35_allocation_starts_zero_of_x(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t = TrainerTask(task_id=50, company_id=1, service_id=10, title="Zero Progress", status="active")
            db.add(t)
            await db.flush()
            item = TrainerTaskItem(task_id=50, simulation_id=101, required_attempts=3, order_index=0)
            a = TrainerTaskAssignee(task_id=50, company_id=1, hubspot_owner_id="agent_01", status="pending")
            db.add_all([item, a])
            await db.commit()

            detail = await TrainerTaskService.get_task_detail(db, 50, self.ctx_c1_admin)
            self.assertEqual(detail["assignees"][0]["items_progress"][0]["completed_attempts"], 0)
            self.assertEqual(detail["assignees"][0]["items_progress"][0]["required_attempts"], 3)
            self.assertEqual(detail["assignees"][0]["status"], "pending")

    async def test_36_past_simulations_ignored(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # Session ended in the past
            past_time = datetime.now(timezone.utc) - timedelta(hours=2)
            sess = TrainerSession(
                session_id=2001, simulation_id=101, agent_id="agent_01", agent_code="A01",
                company_id=1, service_id=10, call_id="c_past", status="completed",
                evaluation_status="evaluated", started_at=past_time - timedelta(minutes=5), ended_at=past_time
            )
            db.add(sess)
            await db.flush()
            eval_rec = TrainerEvaluation(
                session_id=sess.session_id, prompt_snapshot="p", result_json={"score": 8.0}, score=Decimal("8.0")
            )
            db.add(eval_rec)

            # Task assigned AFTER the session ended
            assign_time = datetime.now(timezone.utc) - timedelta(hours=1)
            t = TrainerTask(task_id=51, company_id=1, service_id=10, title="After Past Session", status="active")
            db.add(t)
            await db.flush()
            item = TrainerTaskItem(task_id=51, simulation_id=101, required_attempts=1, order_index=0)
            a = TrainerTaskAssignee(
                task_id=51, company_id=1, hubspot_owner_id="agent_01",
                assigned_at=assign_time, status="pending"
            )
            db.add_all([item, a])
            await db.commit()

            # Attempt allocation
            alloc = await TrainerTaskService.allocate_completed_attempt(db, session_id=2001)
            self.assertIsNone(alloc)

            # Assignee remains 0 attempts
            detail = await TrainerTaskService.get_task_detail(db, 51, self.ctx_c1_admin)
            self.assertEqual(detail["assignees"][0]["items_progress"][0]["completed_attempts"], 0)

    async def test_37_allocation_single_attempt_increments(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            assign_time = datetime.now(timezone.utc) - timedelta(minutes=10)
            t = TrainerTask(task_id=52, company_id=1, service_id=10, title="Increment Test", status="active")
            db.add(t)
            await db.flush()
            item = TrainerTaskItem(task_id=52, simulation_id=101, required_attempts=2, order_index=0)
            a = TrainerTaskAssignee(
                task_id=52, company_id=1, hubspot_owner_id="agent_01",
                assigned_at=assign_time, status="pending"
            )
            db.add_all([item, a])
            await db.flush()

            sess = TrainerSession(
                session_id=2002, simulation_id=101, agent_id="agent_01", agent_code="A01",
                company_id=1, service_id=10, call_id="c_2002", status="completed",
                evaluation_status="evaluated", started_at=datetime.now(timezone.utc) - timedelta(minutes=2),
                ended_at=datetime.now(timezone.utc)
            )
            db.add(sess)
            await db.flush()
            eval_rec = TrainerEvaluation(
                session_id=sess.session_id, prompt_snapshot="p", result_json={"score": 8.5}, score=Decimal("8.5")
            )
            db.add(eval_rec)
            await db.commit()

            alloc = await TrainerTaskService.allocate_completed_attempt(db, session_id=2002)
            self.assertIsNotNone(alloc)
            self.assertEqual(alloc.session_id, 2002)

            detail = await TrainerTaskService.get_task_detail(db, 52, self.ctx_c1_admin)
            self.assertEqual(detail["assignees"][0]["status"], "in_progress")
            self.assertEqual(detail["assignees"][0]["items_progress"][0]["completed_attempts"], 1)

    async def test_38_allocation_idempotent_duplicate_session(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            assign_time = datetime.now(timezone.utc) - timedelta(minutes=10)
            t = TrainerTask(task_id=53, company_id=1, service_id=10, title="Idempotent Test", status="active")
            db.add(t)
            await db.flush()
            item = TrainerTaskItem(task_id=53, simulation_id=101, required_attempts=2, order_index=0)
            a = TrainerTaskAssignee(task_id=53, company_id=1, hubspot_owner_id="agent_01", assigned_at=assign_time, status="pending")
            db.add_all([item, a])
            await db.flush()

            sess = TrainerSession(
                session_id=2003, simulation_id=101, agent_id="agent_01", agent_code="A01",
                company_id=1, service_id=10, call_id="c_2003", status="completed",
                evaluation_status="evaluated", ended_at=datetime.now(timezone.utc)
            )
            db.add(sess)
            await db.flush()
            db.add(TrainerEvaluation(session_id=sess.session_id, prompt_snapshot="p", result_json={}, score=Decimal("7.0")))
            await db.commit()

            # First allocation
            alloc1 = await TrainerTaskService.allocate_completed_attempt(db, session_id=2003)
            # Second allocation (should be no-op / idempotent)
            alloc2 = await TrainerTaskService.allocate_completed_attempt(db, session_id=2003)

            self.assertEqual(alloc1.allocation_id, alloc2.allocation_id)
            stmt = select(func.count(TrainerTaskAttemptAllocation.allocation_id)).where(TrainerTaskAttemptAllocation.session_id == 2003)
            rc = await db.execute(stmt)
            self.assertEqual(rc.scalar(), 1)

    async def test_39_allocation_capped_at_required_attempts(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            assign_time = datetime.now(timezone.utc) - timedelta(minutes=10)
            t = TrainerTask(task_id=54, company_id=1, service_id=10, title="Cap Test", status="active")
            db.add(t)
            await db.flush()
            item = TrainerTaskItem(task_id=54, simulation_id=101, required_attempts=1, order_index=0)
            a = TrainerTaskAssignee(task_id=54, company_id=1, hubspot_owner_id="agent_01", assigned_at=assign_time, status="pending")
            db.add_all([item, a])
            await db.flush()

            for sid in [2004, 2005]:
                sess = TrainerSession(
                    session_id=sid, simulation_id=101, agent_id="agent_01", agent_code="A01",
                    company_id=1, service_id=10, call_id=f"c_{sid}", status="completed",
                    evaluation_status="evaluated", ended_at=datetime.now(timezone.utc)
                )
                db.add(sess)
                await db.flush()
                db.add(TrainerEvaluation(session_id=sid, prompt_snapshot="p", result_json={}, score=Decimal("8.0")))
            await db.commit()

            # First session should allocate
            a1 = await TrainerTaskService.allocate_completed_attempt(db, session_id=2004)
            self.assertIsNotNone(a1)

            # Second session should NOT allocate because required_attempts=1 is already fulfilled
            a2 = await TrainerTaskService.allocate_completed_attempt(db, session_id=2005)
            self.assertIsNone(a2)

    async def test_40_allocation_multiple_trainings_independent_progress(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            assign_time = datetime.now(timezone.utc) - timedelta(minutes=10)
            t = TrainerTask(task_id=55, company_id=1, service_id=10, title="Multi Item Progress", status="active")
            db.add(t)
            await db.flush()
            it1 = TrainerTaskItem(task_id=55, simulation_id=101, required_attempts=1, order_index=0)
            it2 = TrainerTaskItem(task_id=55, simulation_id=102, required_attempts=1, order_index=1)
            a = TrainerTaskAssignee(task_id=55, company_id=1, hubspot_owner_id="agent_01", assigned_at=assign_time, status="pending")
            db.add_all([it1, it2, a])
            await db.flush()

            sess = TrainerSession(
                session_id=2006, simulation_id=101, agent_id="agent_01", agent_code="A01",
                company_id=1, service_id=10, call_id="c_2006", status="completed",
                evaluation_status="evaluated", ended_at=datetime.now(timezone.utc)
            )
            db.add(sess)
            await db.flush()
            db.add(TrainerEvaluation(session_id=2006, prompt_snapshot="p", result_json={}, score=Decimal("8.0")))
            await db.commit()

            await TrainerTaskService.allocate_completed_attempt(db, session_id=2006)

            detail = await TrainerTaskService.get_task_detail(db, 55, self.ctx_c1_admin)
            prog = detail["assignees"][0]["items_progress"]
            self.assertEqual(prog[0]["completed_attempts"], 1)
            self.assertEqual(prog[1]["completed_attempts"], 0)
            self.assertEqual(detail["assignees"][0]["status"], "in_progress")

    async def test_41_allocation_multi_agent_independent_progress(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            assign_time = datetime.now(timezone.utc) - timedelta(minutes=10)
            t = TrainerTask(task_id=56, company_id=1, service_id=10, title="Multi Agent Progress", status="active")
            db.add(t)
            await db.flush()
            it = TrainerTaskItem(task_id=56, simulation_id=101, required_attempts=1, order_index=0)
            a1 = TrainerTaskAssignee(task_id=56, company_id=1, hubspot_owner_id="agent_01", assigned_at=assign_time, status="pending")
            a2 = TrainerTaskAssignee(task_id=56, company_id=1, hubspot_owner_id="agent_02", assigned_at=assign_time, status="pending")
            db.add_all([it, a1, a2])
            await db.flush()

            sess1 = TrainerSession(
                session_id=2007, simulation_id=101, agent_id="agent_01", agent_code="A01",
                company_id=1, service_id=10, call_id="c_2007", status="completed",
                evaluation_status="evaluated", ended_at=datetime.now(timezone.utc)
            )
            db.add(sess1)
            await db.flush()
            db.add(TrainerEvaluation(session_id=2007, prompt_snapshot="p", result_json={}, score=Decimal("8.0")))
            await db.commit()

            await TrainerTaskService.allocate_completed_attempt(db, session_id=2007)

            detail = await TrainerTaskService.get_task_detail(db, 56, self.ctx_c1_admin)
            assignee_map = {a["hubspot_owner_id"]: a for a in detail["assignees"]}
            self.assertEqual(assignee_map["agent_01"]["status"], "completed")
            self.assertEqual(assignee_map["agent_02"]["status"], "pending")

    async def test_42_allocation_completes_assignee_when_all_items_met(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            assign_time = datetime.now(timezone.utc) - timedelta(minutes=10)
            t = TrainerTask(task_id=57, company_id=1, service_id=10, title="Assignee Complete Test", status="active")
            db.add(t)
            await db.flush()
            it1 = TrainerTaskItem(task_id=57, simulation_id=101, required_attempts=1, order_index=0)
            it2 = TrainerTaskItem(task_id=57, simulation_id=102, required_attempts=1, order_index=1)
            a = TrainerTaskAssignee(task_id=57, company_id=1, hubspot_owner_id="agent_01", assigned_at=assign_time, status="pending")
            db.add_all([it1, it2, a])
            await db.flush()

            for sid, sim_id in [(2008, 101), (2009, 102)]:
                sess = TrainerSession(
                    session_id=sid, simulation_id=sim_id, agent_id="agent_01", agent_code="A01",
                    company_id=1, service_id=10, call_id=f"c_{sid}", status="completed",
                    evaluation_status="evaluated", ended_at=datetime.now(timezone.utc)
                )
                db.add(sess)
                await db.flush()
                db.add(TrainerEvaluation(session_id=sid, prompt_snapshot="p", result_json={}, score=Decimal("9.0")))
            await db.commit()

            await TrainerTaskService.allocate_completed_attempt(db, session_id=2008)
            await TrainerTaskService.allocate_completed_attempt(db, session_id=2009)

            detail = await TrainerTaskService.get_task_detail(db, 57, self.ctx_c1_admin)
            self.assertEqual(detail["assignees"][0]["status"], "completed")
            self.assertIsNotNone(detail["assignees"][0]["completed_at"])

    async def test_43_allocation_completes_task_when_all_assignees_done(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            assign_time = datetime.now(timezone.utc) - timedelta(minutes=10)
            t = TrainerTask(task_id=58, company_id=1, service_id=10, title="Task Complete Test", status="active")
            db.add(t)
            await db.flush()
            it = TrainerTaskItem(task_id=58, simulation_id=101, required_attempts=1, order_index=0)
            a1 = TrainerTaskAssignee(task_id=58, company_id=1, hubspot_owner_id="agent_01", assigned_at=assign_time, status="pending")
            a2 = TrainerTaskAssignee(task_id=58, company_id=1, hubspot_owner_id="agent_02", assigned_at=assign_time, status="pending")
            db.add_all([it, a1, a2])
            await db.flush()

            for sid, ag_id in [(2010, "agent_01"), (2011, "agent_02")]:
                sess = TrainerSession(
                    session_id=sid, simulation_id=101, agent_id=ag_id, agent_code=ag_id,
                    company_id=1, service_id=10, call_id=f"c_{sid}", status="completed",
                    evaluation_status="evaluated", ended_at=datetime.now(timezone.utc)
                )
                db.add(sess)
                await db.flush()
                db.add(TrainerEvaluation(session_id=sid, prompt_snapshot="p", result_json={}, score=Decimal("9.0")))
            await db.commit()

            await TrainerTaskService.allocate_completed_attempt(db, session_id=2010)
            t_mid = await TrainerTaskService.get_task_detail(db, 58, self.ctx_c1_admin)
            self.assertEqual(t_mid["status"], "active")

            await TrainerTaskService.allocate_completed_attempt(db, session_id=2011)
            t_fin = await TrainerTaskService.get_task_detail(db, 58, self.ctx_c1_admin)
            self.assertEqual(t_fin["status"], "completed")

    async def test_43b_allocation_completes_task_with_mixed_completed_and_completed_late(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            due_date = datetime.now(timezone.utc) - timedelta(hours=1)
            assign_time = due_date - timedelta(days=1)
            t = TrainerTask(task_id=59, company_id=1, service_id=10, title="Mixed Completion Task", status="active", due_at=due_date)
            db.add(t)
            await db.flush()
            it = TrainerTaskItem(task_id=59, simulation_id=101, required_attempts=1, order_index=0)
            a1 = TrainerTaskAssignee(task_id=59, company_id=1, hubspot_owner_id="agent_01", assigned_at=assign_time, status="pending")
            a2 = TrainerTaskAssignee(task_id=59, company_id=1, hubspot_owner_id="agent_02", assigned_at=assign_time, status="pending")
            db.add_all([it, a1, a2])
            await db.flush()

            # Agent 1 completed before due_date (on time)
            time_on_time = due_date - timedelta(hours=2)
            s1 = TrainerSession(
                session_id=2026, simulation_id=101, agent_id="agent_01", agent_code="agent_01",
                company_id=1, service_id=10, call_id="c_2026", status="completed",
                evaluation_status="evaluated", started_at=time_on_time - timedelta(minutes=2),
                ended_at=time_on_time
            )
            db.add(s1)
            await db.flush()
            db.add(TrainerEvaluation(session_id=2026, prompt_snapshot="p", result_json={}, score=Decimal("9.0")))

            # Agent 2 completed after due_date (late)
            time_late = datetime.now(timezone.utc)
            s2 = TrainerSession(
                session_id=2027, simulation_id=101, agent_id="agent_02", agent_code="agent_02",
                company_id=1, service_id=10, call_id="c_2027", status="completed",
                evaluation_status="evaluated", started_at=time_late - timedelta(minutes=2),
                ended_at=time_late
            )
            db.add(s2)
            await db.flush()
            db.add(TrainerEvaluation(session_id=2027, prompt_snapshot="p", result_json={}, score=Decimal("8.5")))
            await db.commit()

            # Allocate agent 1
            await TrainerTaskService.allocate_completed_attempt(db, session_id=2026)
            # Allocate agent 2
            await TrainerTaskService.allocate_completed_attempt(db, session_id=2027)

            # Verify physical statuses in DB
            stmt_a1 = select(TrainerTaskAssignee).where(TrainerTaskAssignee.task_assignee_id == a1.task_assignee_id)
            res_a1 = await db.execute(stmt_a1)
            p_a1 = res_a1.scalar_one()
            self.assertEqual(p_a1.status, "completed")
            self.assertEqual(p_a1.compute_effective_status(), "completed")

            stmt_a2 = select(TrainerTaskAssignee).where(TrainerTaskAssignee.task_assignee_id == a2.task_assignee_id)
            res_a2 = await db.execute(stmt_a2)
            p_a2 = res_a2.scalar_one()
            self.assertEqual(p_a2.status, "completed_late")
            self.assertEqual(p_a2.compute_effective_status(), "completed_late")

            # Global task MUST be completed
            stmt_t = select(TrainerTask).where(TrainerTask.task_id == 59)
            res_t = await db.execute(stmt_t)
            p_t = res_t.scalar_one()
            self.assertEqual(p_t.status, "completed")
            self.assertEqual(p_t.compute_effective_status(), "completed")

    # ── 7. FIFO Resolution Tests ────────────────────────────────────────────────

    async def test_44_fifo_same_training_two_tasks(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            time_a = datetime.now(timezone.utc) - timedelta(hours=2)
            time_b = datetime.now(timezone.utc) - timedelta(hours=1)

            # Task A (older)
            ta = TrainerTask(task_id=61, company_id=1, service_id=10, title="Task A", status="active")
            db.add(ta)
            await db.flush()
            ita = TrainerTaskItem(task_id=61, simulation_id=101, required_attempts=1, order_index=0)
            aa = TrainerTaskAssignee(task_id=61, company_id=1, hubspot_owner_id="agent_01", assigned_at=time_a, status="pending")
            db.add_all([ita, aa])

            # Task B (newer)
            tb = TrainerTask(task_id=62, company_id=1, service_id=10, title="Task B", status="active")
            db.add(tb)
            await db.flush()
            itb = TrainerTaskItem(task_id=62, simulation_id=101, required_attempts=1, order_index=0)
            ab = TrainerTaskAssignee(task_id=62, company_id=1, hubspot_owner_id="agent_01", assigned_at=time_b, status="pending")
            db.add_all([itb, ab])
            await db.flush()

            s1 = TrainerSession(
                session_id=2012, simulation_id=101, agent_id="agent_01", agent_code="A01",
                company_id=1, service_id=10, call_id="c_2012", status="completed",
                evaluation_status="evaluated", ended_at=datetime.now(timezone.utc)
            )
            db.add(s1)
            await db.flush()
            db.add(TrainerEvaluation(session_id=2012, prompt_snapshot="p", result_json={}, score=Decimal("8.0")))

            s2 = TrainerSession(
                session_id=2013, simulation_id=101, agent_id="agent_01", agent_code="A01",
                company_id=1, service_id=10, call_id="c_2013", status="completed",
                evaluation_status="evaluated", ended_at=datetime.now(timezone.utc)
            )
            db.add(s2)
            await db.flush()
            db.add(TrainerEvaluation(session_id=2013, prompt_snapshot="p", result_json={}, score=Decimal("8.5")))
            await db.commit()

            alloc1 = await TrainerTaskService.allocate_completed_attempt(db, session_id=2012)
            self.assertEqual(alloc1.task_assignee_id, aa.task_assignee_id)

            alloc2 = await TrainerTaskService.allocate_completed_attempt(db, session_id=2013)
            self.assertEqual(alloc2.task_assignee_id, ab.task_assignee_id)

    async def test_45_fifo_tie_breaking_by_assignee_and_item_id(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            same_time = datetime.now(timezone.utc) - timedelta(hours=1)
            ta = TrainerTask(task_id=63, company_id=1, service_id=10, title="Tie A", status="active")
            tb = TrainerTask(task_id=64, company_id=1, service_id=10, title="Tie B", status="active")
            db.add_all([ta, tb])
            await db.flush()

            ita = TrainerTaskItem(task_item_id=501, task_id=63, simulation_id=101, required_attempts=1, order_index=0)
            itb = TrainerTaskItem(task_item_id=502, task_id=64, simulation_id=101, required_attempts=1, order_index=0)
            aa = TrainerTaskAssignee(task_assignee_id=601, task_id=63, company_id=1, hubspot_owner_id="agent_01", assigned_at=same_time, status="pending")
            ab = TrainerTaskAssignee(task_assignee_id=602, task_id=64, company_id=1, hubspot_owner_id="agent_01", assigned_at=same_time, status="pending")
            db.add_all([ita, itb, aa, ab])
            await db.flush()

            s = TrainerSession(
                session_id=2014, simulation_id=101, agent_id="agent_01", agent_code="A01",
                company_id=1, service_id=10, call_id="c_2014", status="completed",
                evaluation_status="evaluated", ended_at=datetime.now(timezone.utc)
            )
            db.add(s)
            await db.flush()
            db.add(TrainerEvaluation(session_id=2014, prompt_snapshot="p", result_json={}, score=Decimal("8.0")))
            await db.commit()

            alloc = await TrainerTaskService.allocate_completed_attempt(db, session_id=2014)
            self.assertEqual(alloc.task_assignee_id, 601)

    async def test_46_fifo_cancelled_task_skipped(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            time_a = datetime.now(timezone.utc) - timedelta(hours=2)
            time_b = datetime.now(timezone.utc) - timedelta(hours=1)

            ta = TrainerTask(task_id=65, company_id=1, service_id=10, title="Cancelled Task", status="cancelled")
            db.add(ta)
            await db.flush()
            ita = TrainerTaskItem(task_id=65, simulation_id=101, required_attempts=1, order_index=0)
            aa = TrainerTaskAssignee(task_id=65, company_id=1, hubspot_owner_id="agent_01", assigned_at=time_a, status="cancelled")
            db.add_all([ita, aa])

            tb = TrainerTask(task_id=66, company_id=1, service_id=10, title="Active Task", status="active")
            db.add(tb)
            await db.flush()
            itb = TrainerTaskItem(task_id=66, simulation_id=101, required_attempts=1, order_index=0)
            ab = TrainerTaskAssignee(task_id=66, company_id=1, hubspot_owner_id="agent_01", assigned_at=time_b, status="pending")
            db.add_all([itb, ab])
            await db.flush()

            s = TrainerSession(
                session_id=2015, simulation_id=101, agent_id="agent_01", agent_code="A01",
                company_id=1, service_id=10, call_id="c_2015", status="completed",
                evaluation_status="evaluated", ended_at=datetime.now(timezone.utc)
            )
            db.add(s)
            await db.flush()
            db.add(TrainerEvaluation(session_id=2015, prompt_snapshot="p", result_json={}, score=Decimal("8.0")))
            await db.commit()

            alloc = await TrainerTaskService.allocate_completed_attempt(db, session_id=2015)
            self.assertEqual(alloc.task_assignee_id, ab.task_assignee_id)

    async def test_47_fifo_overdue_task_still_receives_attempts(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            past_due = datetime.now(timezone.utc) - timedelta(days=1)
            t = TrainerTask(task_id=67, company_id=1, service_id=10, title="Overdue Task", status="active", due_at=past_due)
            db.add(t)
            await db.flush()
            it = TrainerTaskItem(task_id=67, simulation_id=101, required_attempts=1, order_index=0)
            a = TrainerTaskAssignee(task_id=67, company_id=1, hubspot_owner_id="agent_01", assigned_at=past_due - timedelta(days=1), status="pending")
            db.add_all([it, a])
            await db.flush()

            s = TrainerSession(
                session_id=2016, simulation_id=101, agent_id="agent_01", agent_code="A01",
                company_id=1, service_id=10, call_id="c_2016", status="completed",
                evaluation_status="evaluated", ended_at=datetime.now(timezone.utc)
            )
            db.add(s)
            await db.flush()
            db.add(TrainerEvaluation(session_id=2016, prompt_snapshot="p", result_json={}, score=Decimal("8.0")))
            await db.commit()

            alloc = await TrainerTaskService.allocate_completed_attempt(db, session_id=2016)
            self.assertIsNotNone(alloc)
            self.assertEqual(alloc.task_assignee_id, a.task_assignee_id)

    # ── 8. Effective Status Tests ───────────────────────────────────────────────

    async def test_48_effective_status_dynamic_overdue(self):
        past_due = datetime.now(timezone.utc) - timedelta(days=2)
        t = TrainerTask(status="active", due_at=past_due)
        self.assertEqual(t.compute_effective_status(), "overdue")

        future_due = datetime.now(timezone.utc) + timedelta(days=2)
        t_fut = TrainerTask(status="active", due_at=future_due)
        self.assertEqual(t_fut.compute_effective_status(), "active")

        t_none = TrainerTask(status="active", due_at=None)
        self.assertEqual(t_none.compute_effective_status(), "active")

    async def test_49_effective_status_completed_late(self):
        past_due = datetime.now(timezone.utc) - timedelta(days=2)
        a_late = TrainerTaskAssignee(
            status="completed",
            completed_at=datetime.now(timezone.utc) - timedelta(days=1)
        )
        t = TrainerTask(status="completed", due_at=past_due)
        a_late.task = t
        self.assertEqual(a_late.compute_effective_status(), "completed_late")

        a_on_time = TrainerTaskAssignee(
            status="completed",
            completed_at=past_due - timedelta(days=1)
        )
        a_on_time.task = t
        self.assertEqual(a_on_time.compute_effective_status(), "completed")

    async def test_49b_persisted_completed_late_in_db(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            past_due = datetime.now(timezone.utc) - timedelta(days=1)
            t = TrainerTask(task_id=68, company_id=1, service_id=10, title="Overdue Task Late Completion", status="active", due_at=past_due)
            db.add(t)
            await db.flush()
            it = TrainerTaskItem(task_id=68, simulation_id=101, required_attempts=1, order_index=0)
            a = TrainerTaskAssignee(task_id=68, company_id=1, hubspot_owner_id="agent_01", assigned_at=past_due - timedelta(days=1), status="pending")
            db.add_all([it, a])
            await db.flush()

            now_sess = datetime.now(timezone.utc)
            s = TrainerSession(
                session_id=2025, simulation_id=101, agent_id="agent_01", agent_code="A01",
                company_id=1, service_id=10, call_id="c_2025", status="completed",
                evaluation_status="evaluated", started_at=now_sess - timedelta(minutes=2),
                ended_at=now_sess
            )
            db.add(s)
            await db.flush()
            db.add(TrainerEvaluation(session_id=2025, prompt_snapshot="p", result_json={}, score=Decimal("8.5")))
            await db.commit()

            alloc = await TrainerTaskService.allocate_completed_attempt(db, session_id=2025)
            self.assertIsNotNone(alloc)

            # Query physically from DB
            stmt_a = select(TrainerTaskAssignee).where(TrainerTaskAssignee.task_assignee_id == a.task_assignee_id)
            res_a = await db.execute(stmt_a)
            persisted_a = res_a.scalar_one()

            # Check PHYSICAL persisted status in DB column
            self.assertEqual(persisted_a.status, "completed_late")
            self.assertEqual(persisted_a.compute_effective_status(), "completed_late")
            self.assertIsNotNone(persisted_a.completed_at)

            # Check task status in DB column
            stmt_t = select(TrainerTask).where(TrainerTask.task_id == 68)
            res_t = await db.execute(stmt_t)
            persisted_t = res_t.scalar_one()
            self.assertEqual(persisted_t.status, "completed")
            self.assertEqual(persisted_t.compute_effective_status(), "completed")

    # ── 9. Reassignment & History Tests ─────────────────────────────────────────

    async def test_50_reassignment_starts_new_history(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t1 = TrainerTask(task_id=71, company_id=1, service_id=10, title="Cycle 1 Task", status="completed")
            db.add(t1)
            await db.flush()
            it1 = TrainerTaskItem(task_id=71, simulation_id=101, required_attempts=1, order_index=0)
            a1 = TrainerTaskAssignee(task_id=71, company_id=1, hubspot_owner_id="agent_01", status="completed")
            db.add_all([it1, a1])
            await db.flush()
            alloc1 = TrainerTaskAttemptAllocation(task_assignee_id=a1.task_assignee_id, task_item_id=it1.task_item_id, session_id=2017)
            db.add(alloc1)

            t2 = TrainerTask(task_id=72, company_id=1, service_id=10, title="Cycle 2 Task", status="active")
            db.add(t2)
            await db.flush()
            it2 = TrainerTaskItem(task_id=72, simulation_id=101, required_attempts=2, order_index=0)
            a2 = TrainerTaskAssignee(task_id=72, company_id=1, hubspot_owner_id="agent_01", status="pending")
            db.add_all([it2, a2])
            await db.commit()

            detail2 = await TrainerTaskService.get_task_detail(db, 72, self.ctx_c1_admin)
            self.assertEqual(detail2["assignees"][0]["items_progress"][0]["completed_attempts"], 0)
            self.assertEqual(detail2["assignees"][0]["status"], "pending")

            detail1 = await TrainerTaskService.get_task_detail(db, 71, self.ctx_c1_admin)
            self.assertEqual(detail1["assignees"][0]["items_progress"][0]["completed_attempts"], 1)

    # ── 10. Agent Endpoints Tests ───────────────────────────────────────────────

    async def test_51_agent_my_tasks_endpoint(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            past_due = datetime.now(timezone.utc) - timedelta(days=1)
            t = TrainerTask(task_id=81, company_id=1, service_id=10, title="Overdue Agent Task", status="active", due_at=past_due)
            db.add(t)
            await db.flush()
            it = TrainerTaskItem(task_id=81, simulation_id=101, required_attempts=2, order_index=0)
            a = TrainerTaskAssignee(task_id=81, company_id=1, hubspot_owner_id="agent_01", status="pending")
            db.add_all([it, a])
            await db.commit()

            tasks = await TrainerTaskService.get_agent_tasks(db, hubspot_owner_id="agent_01", company_id=1)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["title"], "Overdue Agent Task")
            self.assertEqual(tasks[0]["effective_status"], "overdue")
            self.assertEqual(tasks[0]["total_required_attempts"], 2)
            self.assertEqual(tasks[0]["total_completed_attempts"], 0)

    async def test_52_agent_my_tasks_isolation(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t = TrainerTask(task_id=82, company_id=1, service_id=10, title="Agent 2 Only Task", status="active")
            db.add(t)
            await db.flush()
            it = TrainerTaskItem(task_id=82, simulation_id=101, required_attempts=1, order_index=0)
            a = TrainerTaskAssignee(task_id=82, company_id=1, hubspot_owner_id="agent_02", status="pending")
            db.add_all([it, a])
            await db.commit()

            tasks_ag1 = await TrainerTaskService.get_agent_tasks(db, hubspot_owner_id="agent_01", company_id=1)
            self.assertEqual(len(tasks_ag1), 0)

            tasks_ag2 = await TrainerTaskService.get_agent_tasks(db, hubspot_owner_id="agent_02", company_id=1)
            self.assertEqual(len(tasks_ag2), 1)

    async def test_53_agent_my_results_endpoint_with_task_attribution(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t = TrainerTask(task_id=83, company_id=1, service_id=10, title="Task Attributed", status="active")
            db.add(t)
            await db.flush()
            it = TrainerTaskItem(task_id=83, simulation_id=101, required_attempts=1, order_index=0)
            a = TrainerTaskAssignee(task_id=83, company_id=1, hubspot_owner_id="agent_01", status="completed")
            db.add_all([it, a])
            await db.flush()

            sess = TrainerSession(
                session_id=2018, simulation_id=101, agent_id="agent_01", agent_code="A01",
                company_id=1, service_id=10, call_id="c_2018", status="completed",
                evaluation_status="evaluated", ended_at=datetime.now(timezone.utc)
            )
            db.add(sess)
            await db.flush()
            db.add(TrainerEvaluation(session_id=2018, prompt_snapshot="p", result_json={}, score=Decimal("9.2"), summary="Bien"))
            alloc = TrainerTaskAttemptAllocation(task_assignee_id=a.task_assignee_id, task_item_id=it.task_item_id, session_id=2018)
            db.add(alloc)
            await db.commit()

            results, total = await TrainerTaskService.get_agent_results(db, hubspot_owner_id="agent_01", company_id=1)
            self.assertEqual(total, 1)
            r = results[0]
            self.assertTrue(r["counted_for_task"])
            self.assertEqual(r["task_id"], 83)
            self.assertEqual(r["task_title"], "Task Attributed")
            self.assertEqual(r["score"], 9.2)

    async def test_54_agent_my_results_endpoint_without_task(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            sess = TrainerSession(
                session_id=2019, simulation_id=101, agent_id="agent_01", agent_code="A01",
                company_id=1, service_id=10, call_id="c_2019", status="completed",
                evaluation_status="evaluated", ended_at=datetime.now(timezone.utc)
            )
            db.add(sess)
            await db.flush()
            db.add(TrainerEvaluation(session_id=2019, prompt_snapshot="p", result_json={}, score=Decimal("7.5")))
            await db.commit()

            results, total = await TrainerTaskService.get_agent_results(db, hubspot_owner_id="agent_01", company_id=1)
            self.assertEqual(total, 1)
            r = results[0]
            self.assertFalse(r["counted_for_task"])
            self.assertIsNone(r["task_id"])
            self.assertIsNone(r["task_title"])

    # ── 11. Eligible Agents Tests ───────────────────────────────────────────────

    async def test_55_eligible_agents_scoping(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            agents_c1 = await TrainerTaskService.get_eligible_agents(db, service_id=10, context=self.ctx_c1_admin)
            owner_ids = [ag["hubspot_owner_id"] for ag in agents_c1]
            self.assertIn("agent_01", owner_ids)
            self.assertIn("agent_02", owner_ids)
            self.assertNotIn("agent_03", owner_ids)
            self.assertNotIn("agent_c2", owner_ids)

            agents_tl = await TrainerTaskService.get_eligible_agents(db, service_id=10, context=self.ctx_tl_t1)
            tl_ids = [ag["hubspot_owner_id"] for ag in agents_tl]
            self.assertEqual(tl_ids, ["agent_01"])

    # ── 12. Regression & Robustness Tests ───────────────────────────────────────

    async def test_56_regression_unrelated_trainer_session_safe(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            sess = TrainerSession(
                session_id=2020, simulation_id=102, agent_id="agent_01", agent_code="A01",
                company_id=1, service_id=10, call_id="c_2020", status="completed",
                evaluation_status="evaluated", ended_at=datetime.now(timezone.utc)
            )
            db.add(sess)
            await db.flush()
            db.add(TrainerEvaluation(session_id=2020, prompt_snapshot="p", result_json={}, score=Decimal("8.0")))
            await db.commit()

            alloc = await TrainerTaskService.allocate_completed_attempt(db, session_id=2020)
            self.assertIsNone(alloc)

    async def test_57_regression_incomplete_session_not_allocated(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t = TrainerTask(task_id=90, company_id=1, service_id=10, title="Incomplete Test", status="active")
            db.add(t)
            await db.flush()
            it = TrainerTaskItem(task_id=90, simulation_id=101, required_attempts=1, order_index=0)
            a = TrainerTaskAssignee(task_id=90, company_id=1, hubspot_owner_id="agent_01", status="pending")
            db.add_all([it, a])
            await db.flush()

            sess = TrainerSession(
                session_id=2021, simulation_id=101, agent_id="agent_01", agent_code="A01",
                company_id=1, service_id=10, call_id="c_2021", status="started",
                evaluation_status="started"
            )
            db.add(sess)
            await db.commit()

            alloc = await TrainerTaskService.allocate_completed_attempt(db, session_id=2021)
            self.assertIsNone(alloc)

    async def test_58_created_by_user_id_nullable(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            t = TrainerTask(
                task_id=91, company_id=1, service_id=10, title="Null Creator Task",
                created_by_user_id=None, status="active"
            )
            db.add(t)
            await db.commit()

            detail = await TrainerTaskService.get_task_detail(db, 91, self.ctx_c1_admin)
            self.assertIsNone(detail["created_by_user_id"])
            self.assertIsNone(detail["created_by_name"])


if __name__ == "__main__":
    unittest.main()
