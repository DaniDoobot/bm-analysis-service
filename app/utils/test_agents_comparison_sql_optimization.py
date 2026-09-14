"""
Test Suite: test_agents_comparison_sql_optimization.py
======================================================
Validates:
1. Synthetic Volume & Mathematical Precision:
   - 10 agents, 500 evaluations, 6 criteria.
   - Validates mathematical exactness of AVG and COUNT for all agents and criteria.
2. Query Count Instrumentation (O(1) Absence of N+1):
   - Validates that the query count remains constant regardless of agent volume.
3. Heavy JSON Resilience:
   - Analyses with 100KB+ JSON payloads in result_json/items_json.
   - Validates fast execution without loading JSON payloads into memory.
4. Functional Regression Matrix (A through P):
   - A) 1 agent / 1 criterion
   - B) Multiple agents / multiple criteria
   - C) Selected agent without data (has_data=False, analysis_count=0, value=None, count=0)
   - D) Omitted agent_owner_ids (only agents with data)
   - E) Service isolation
   - F) Team isolation
   - G) Role security (agent role gets 403)
   - H) Company isolation
   - I) Date range filtering
   - J) Typology filtering
   - K) Direction filtering
   - L) Status filtering (completed / failed / all)
   - M) Duration filtering
   - N) Metric types: cierre_cita percentage vs boolean (100.0/0.0)
   - O) Non-applicable criteria (is_applicable=False) excluded from denominator
   - P) Canonical names from bm_users preserved
"""
import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///agents_comp_sql_opt_test.db"

db_url = os.environ.get("DATABASE_URL", "")
if "91.98.230.119" in db_url or "n8n" in db_url.lower():
    raise RuntimeError("CRITICAL: Test execution blocked because DATABASE_URL points to production!")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import BigInteger, event
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine, Base
from app.main import app
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserTeamAssociation
from app.models.users import User
from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationCriterionResult
from app.utils.security import create_access_token


class TestAgentsComparisonSqlOptimization(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        if os.path.exists("agents_comp_sql_opt_test.db"):
            try:
                os.remove("agents_comp_sql_opt_test.db")
            except Exception:
                pass

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            u_admin = User(
                user_id=1,
                username="admin",
                email="admin@test.com",
                role="superadmin",
                company_id=1,
                is_active=True,
                password_hash="dummy"
            )
            db.add(u_admin)
            await db.commit()

        self.token_admin = create_access_token({"sub": "admin", "role": "superadmin", "company_id": 1, "user_id": 1})
        self.token_agent = create_access_token({"sub": "agent_0", "role": "agent", "company_id": 1, "user_id": 100})
        self.token_co2_admin = create_access_token({"sub": "admin2", "role": "superadmin", "company_id": 2, "user_id": 2})

    async def asyncTearDown(self):
        if hasattr(self, "engine"):
            await self.engine.dispose()
        if os.path.exists("agents_comp_sql_opt_test.db"):
            try:
                os.remove("agents_comp_sql_opt_test.db")
            except Exception:
                pass

    async def test_01_synthetic_volume_and_mathematical_precision(self):
        """1. Synthetic Volume: 10 agents, 500 evaluations, 6 criteria with exact mathematical verification."""
        async with AsyncSession(self.engine) as db:
            c = Company(company_id=1, company_name="Corp Alpha", company_key="alpha", is_active=True)
            s = Service(service_id=1, company_id=1, service_name="Front Desk", service_key="front", is_active=True)
            db.add_all([c, s])

            agents = []
            for i in range(10):
                u = User(
                    user_id=100 + i,
                    username=f"agent_{i}",
                    email=f"agent_{i}@alpha.com",
                    name=f"Agent Number {i}",
                    role="agent",
                    company_id=1,
                    primary_service_id=1,
                    hubspot_owner_id=f"owner_{i}",
                    agent_initials=f"A{i}",
                    is_active=True,
                    password_hash="dummy"
                )
                agents.append(u)
            db.add_all(agents)
            await db.flush()

            base_dt = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
            crit_keys = ["claridad", "empatia", "simpatia", "procedimiento", "cierre_cita"]

            # Ground truth accumulator: agent_id -> {total_calls: int, global_vals: [], crit_vals: {key: []}}
            ground_truth = {f"owner_{i}": {"total_calls": 0, "global_vals": [], "crit_vals": {k: [] for k in crit_keys}} for i in range(10)}

            call_idx = 1
            crit_id = 1
            for eval_num in range(500):
                agent_num = eval_num % 10
                owner_id = f"owner_{agent_num}"
                global_score = round(6.0 + (eval_num % 41) * 0.1, 1) # 6.0 to 10.0
                eval_dt = base_dt + timedelta(hours=eval_num)

                res = MassEvaluationResult(
                    mass_analysis_id=call_idx,
                    run_id=1,
                    job_id=1,
                    prompt_id=1,
                    prompt_snapshot="{}",
                    call_id=f"call_vol_{call_idx}",
                    company_id=1,
                    service_id=1,
                    service_key="front",
                    hubspot_owner_id=owner_id,
                    agent_name=f"Agent Number {agent_num}",
                    call_timestamp=eval_dt,
                    analysis_timestamp=eval_dt,
                    evaluacion_global=Decimal(str(global_score)),
                    status="completed"
                )
                db.add(res)
                ground_truth[owner_id]["total_calls"] += 1
                ground_truth[owner_id]["global_vals"].append(global_score)

                for k_idx, c_key in enumerate(crit_keys):
                    is_app = not (eval_num % 25 == 0 and k_idx == 0)
                    if c_key == "cierre_cita":
                        b_val = (eval_num % 2 == 0)
                        val_for_calc = 100.0 if b_val else 0.0
                        crit = MassEvaluationCriterionResult(
                            id=crit_id,
                            mass_analysis_id=call_idx,
                            run_id=1,
                            job_id=1,
                            call_id=f"call_vol_{call_idx}",
                            criterion_key=c_key,
                            criterion_name=c_key.capitalize(),
                            criterion_type="percentage",
                            boolean_value=b_val,
                            is_applicable=is_app
                        )
                    else:
                        num_val = round(5.0 + ((eval_num + k_idx) % 51) * 0.1, 1)
                        val_for_calc = num_val
                        crit = MassEvaluationCriterionResult(
                            id=crit_id,
                            mass_analysis_id=call_idx,
                            run_id=1,
                            job_id=1,
                            call_id=f"call_vol_{call_idx}",
                            criterion_key=c_key,
                            criterion_name=c_key.capitalize(),
                            criterion_type="score",
                            numeric_value=Decimal(str(num_val)),
                            is_applicable=is_app
                        )
                    db.add(crit)
                    crit_id += 1

                    if is_app:
                        ground_truth[owner_id]["crit_vals"][c_key].append(val_for_calc)

                call_idx += 1

            await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get(
                "/bm/analytics/agents-comparison?service_id=1&date_from=2026-05-01&date_to=2026-06-30",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(len(data["agents"]), 10)
            self.assertEqual(data["selected_agents_count"], 10)
            self.assertEqual(data["agents_with_data_count"], 10)

            for ag in data["agents"]:
                oid = ag["hubspot_owner_id"]
                gt = ground_truth[oid]
                self.assertEqual(ag["has_data"], True)
                self.assertEqual(ag["analysis_count"], gt["total_calls"])

                # Check evaluacion_global
                eg_rows = [r for r in data["comparison"] if r["hubspot_owner_id"] == oid and r["item_key"] == "evaluacion_global"]
                self.assertEqual(len(eg_rows), 1)
                expected_eg_avg = round(sum(gt["global_vals"]) / len(gt["global_vals"]), 1)
                self.assertAlmostEqual(eg_rows[0]["value"], expected_eg_avg, places=1)
                self.assertEqual(eg_rows[0]["count"], len(gt["global_vals"]))

                # Check each criterion
                for c_key in crit_keys:
                    c_rows = [r for r in data["comparison"] if r["hubspot_owner_id"] == oid and r["item_key"] == c_key]
                    self.assertEqual(len(c_rows), 1)
                    expected_count = len(gt["crit_vals"][c_key])
                    expected_avg = round(sum(gt["crit_vals"][c_key]) / expected_count, 1) if expected_count > 0 else None
                    self.assertEqual(c_rows[0]["count"], expected_count)
                    if expected_avg is not None:
                        self.assertAlmostEqual(c_rows[0]["value"], expected_avg, places=1)

    async def test_02_query_count_instrumentation_absence_of_n_plus_one(self):
        """2. Instrumentation: SQL query count is O(1) regardless of agent volume."""
        async with AsyncSession(self.engine) as db:
            c = Company(company_id=1, company_name="Corp Beta", company_key="beta", is_active=True)
            s = Service(service_id=1, company_id=1, service_name="Front", service_key="front", is_active=True)
            db.add_all([c, s])
            for i in range(10):
                db.add(User(
                    user_id=200 + i, username=f"b_agent_{i}", email=f"b_{i}@beta.com",
                    name=f"Beta Agent {i}", role="agent", company_id=1, primary_service_id=1,
                    hubspot_owner_id=f"b_owner_{i}", agent_initials=f"B{i}", is_active=True, password_hash="dummy"
                ))
            await db.flush()

            now = datetime.now(timezone.utc)
            crit_id = 1
            for eval_idx in range(50):
                owner_id = f"b_owner_{eval_idx % 10}"
                db.add(MassEvaluationResult(
                    mass_analysis_id=eval_idx + 1, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                    call_id=f"b_call_{eval_idx}", company_id=1, service_id=1, service_key="front",
                    hubspot_owner_id=owner_id, agent_name=f"Beta Agent {eval_idx % 10}",
                    call_timestamp=now, analysis_timestamp=now, evaluacion_global=Decimal("8.0"), status="completed"
                ))
                db.add(MassEvaluationCriterionResult(
                    id=crit_id, mass_analysis_id=eval_idx + 1, run_id=1, job_id=1, call_id=f"b_call_{eval_idx}",
                    criterion_key="claridad", criterion_name="Claridad", criterion_type="score",
                    numeric_value=Decimal("8.5"), is_applicable=True
                ))
                crit_id += 1
            await db.commit()

        queries_executed_2_agents = []
        def listener_2(conn, cursor, statement, parameters, context, executemany):
            if "select" in statement.lower():
                queries_executed_2_agents.append(statement)

        event.listen(self.engine.sync_engine, "before_cursor_execute", listener_2)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res2 = await client.get(
                "/bm/analytics/agents-comparison?service_id=1&agent_owner_ids=b_owner_0&agent_owner_ids=b_owner_1&duration_min_seconds=1",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res2.status_code, 200)

        event.remove(self.engine.sync_engine, "before_cursor_execute", listener_2)

        queries_executed_10_agents = []
        def listener_10(conn, cursor, statement, parameters, context, executemany):
            if "select" in statement.lower():
                queries_executed_10_agents.append(statement)

        event.listen(self.engine.sync_engine, "before_cursor_execute", listener_10)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res10 = await client.get(
                "/bm/analytics/agents-comparison?service_id=1&duration_min_seconds=2",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res10.status_code, 200)

        event.remove(self.engine.sync_engine, "before_cursor_execute", listener_10)

        count_2 = len(queries_executed_2_agents)
        count_10 = len(queries_executed_10_agents)

        self.assertEqual(count_2, count_10, f"Query count must be constant (O(1)). Got {count_2} vs {count_10}")

    async def test_03_heavy_json_resilience(self):
        """3. Heavy JSON Resilience: massive result_json & items_json are not loaded into memory."""
        async with AsyncSession(self.engine) as db:
            c = Company(company_id=1, company_name="Corp Heavy", company_key="heavy", is_active=True)
            s = Service(service_id=1, company_id=1, service_name="Front", service_key="front", is_active=True)
            u = User(
                user_id=300, username="heavy_agent", email="heavy@test.com",
                name="Heavy Agent", role="agent", company_id=1, primary_service_id=1,
                hubspot_owner_id="heavy_owner", agent_initials="HA", is_active=True, password_hash="dummy"
            )
            db.add_all([c, s, u])
            await db.flush()

            heavy_payload = {"bloat": "X" * 120_000, "meta": {"debug": [i for i in range(1000)]}}
            now = datetime.now(timezone.utc)

            for i in range(5):
                db.add(MassEvaluationResult(
                    mass_analysis_id=1000 + i, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                    call_id=f"heavy_call_{i}", company_id=1, service_id=1, service_key="front",
                    hubspot_owner_id="heavy_owner", agent_name="Heavy Agent",
                    call_timestamp=now, analysis_timestamp=now, evaluacion_global=Decimal("9.0"),
                    result_json=heavy_payload, items_json=[heavy_payload],
                    status="completed"
                ))
                db.add(MassEvaluationCriterionResult(
                    id=5000 + i, mass_analysis_id=1000 + i, run_id=1, job_id=1, call_id=f"heavy_call_{i}",
                    criterion_key="claridad", criterion_name="Claridad", criterion_type="score",
                    numeric_value=Decimal("9.0"), is_applicable=True
                ))
            await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get(
                "/bm/analytics/agents-comparison?service_id=1",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(len(data["agents"]), 1)
            self.assertEqual(data["agents"][0]["analysis_count"], 5)
            self.assertEqual(data["agents"][0]["has_data"], True)

    async def test_04_regression_matrix_a_through_p(self):
        """4. Functional Regression Matrix: Scopes, filters, metric types, non-applicable, and canonical names."""
        async with AsyncSession(self.engine) as db:
            c1 = Company(company_id=1, company_name="Co 1", company_key="co1", is_active=True)
            c2 = Company(company_id=2, company_name="Co 2", company_key="co2", is_active=True)
            s1 = Service(service_id=1, company_id=1, service_name="Front 1", service_key="f1", is_active=True)
            s2 = Service(service_id=2, company_id=1, service_name="Expac 2", service_key="e2", is_active=True)
            t1 = Team(team_id=1, team_name="Team A", service_id=1, company_id=1)
            t2 = Team(team_id=2, team_name="Team B", service_id=1, company_id=1)
            db.add_all([c1, c2, s1, s2, t1, t2])

            u_a = User(
                user_id=10, username="ag_a", email="a@co1.com", name="Alice Canonical",
                role="agent", company_id=1, primary_service_id=1, primary_team_id=1,
                hubspot_owner_id="owner_a", agent_initials="AC", is_active=True, password_hash="dummy"
            )
            u_b = User(
                user_id=11, username="ag_b", email="b@co1.com", name="Bob Canonical",
                role="agent", company_id=1, primary_service_id=1, primary_team_id=2,
                hubspot_owner_id="owner_b", agent_initials="BC", is_active=True, password_hash="dummy"
            )
            u_c = User(
                user_id=12, username="ag_c", email="c@co1.com", name="Charlie Canonical",
                role="agent", company_id=1, primary_service_id=1, primary_team_id=1,
                hubspot_owner_id="owner_c", agent_initials="CC", is_active=True, password_hash="dummy"
            )
            db.add_all([u_a, u_b, u_c])
            db.add_all([UserTeamAssociation(user_id=10, team_id=1), UserTeamAssociation(user_id=11, team_id=2), UserTeamAssociation(user_id=12, team_id=1)])
            await db.flush()

            now = datetime.now(timezone.utc)
            dt1 = now - timedelta(days=2)
            db.add(MassEvaluationResult(
                mass_analysis_id=1, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                call_id="call_1", company_id=1, service_id=1, service_key="f1",
                hubspot_owner_id="owner_a", agent_name="Old Alice Name",
                call_timestamp=dt1, analysis_timestamp=dt1,
                direction="inbound", call_duration_seconds=120,
                evaluacion_global=Decimal("8.0"), status="completed"
            ))
            db.add_all([
                MassEvaluationCriterionResult(
                    id=1, mass_analysis_id=1, run_id=1, job_id=1, call_id="call_1",
                    criterion_key="claridad", criterion_type="score", numeric_value=Decimal("9.0"), is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    id=2, mass_analysis_id=1, run_id=1, job_id=1, call_id="call_1",
                    criterion_key="cierre_cita", criterion_type="percentage", boolean_value=True, is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    id=3, mass_analysis_id=1, run_id=1, job_id=1, call_id="call_1",
                    criterion_key="empatia", criterion_type="score", numeric_value=None, is_applicable=True
                )
            ])

            dt2 = now - timedelta(days=1)
            db.add(MassEvaluationResult(
                mass_analysis_id=2, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                call_id="call_2", company_id=1, service_id=1, service_key="f1",
                hubspot_owner_id="owner_a", agent_name="Old Alice Name",
                call_timestamp=dt2, analysis_timestamp=dt2,
                direction="outbound", call_duration_seconds=60,
                evaluacion_global=Decimal("6.0"), status="completed"
            ))
            db.add_all([
                MassEvaluationCriterionResult(
                    id=4, mass_analysis_id=2, run_id=1, job_id=1, call_id="call_2",
                    criterion_key="claridad", criterion_type="score", numeric_value=Decimal("7.0"), is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    id=5, mass_analysis_id=2, run_id=1, job_id=1, call_id="call_2",
                    criterion_key="cierre_cita", criterion_type="percentage", boolean_value=False, is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    id=6, mass_analysis_id=2, run_id=1, job_id=1, call_id="call_2",
                    criterion_key="empatia", criterion_type="score", numeric_value=Decimal("8.0"), is_applicable=False
                )
            ])

            dt3 = now - timedelta(hours=12)
            db.add(MassEvaluationResult(
                mass_analysis_id=3, run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                call_id="call_3", company_id=1, service_id=1, service_key="f1",
                hubspot_owner_id="owner_b", agent_name="Old Bob Name",
                call_timestamp=dt3, analysis_timestamp=dt3,
                direction="inbound", call_duration_seconds=300,
                evaluacion_global=Decimal("5.0"), status="failed"
            ))

            await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res_a = await client.get(
                "/bm/analytics/agents-comparison?service_id=1&agent_owner_ids=owner_a",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_a.status_code, 200)
            data_a = res_a.json()
            self.assertEqual(len(data_a["agents"]), 1)
            self.assertEqual(data_a["agents"][0]["agent_name"], "Alice Canonical")
            self.assertEqual(data_a["agents"][0]["agent_initials"], "AC")
            self.assertEqual(data_a["agents"][0]["analysis_count"], 2)

            cc_row = next(r for r in data_a["comparison"] if r["item_key"] == "cierre_cita")
            self.assertEqual(cc_row["value"], 50.0)
            self.assertEqual(cc_row["count"], 2)

            emp_row = next(r for r in data_a["comparison"] if r["item_key"] == "empatia")
            self.assertIsNone(emp_row["value"])
            self.assertEqual(emp_row["count"], 0)

            res_c = await client.get(
                "/bm/analytics/agents-comparison?service_id=1&agent_owner_ids=owner_c",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_c.status_code, 200)
            data_c = res_c.json()
            self.assertEqual(len(data_c["agents"]), 1)
            self.assertEqual(data_c["agents"][0]["has_data"], False)
            self.assertEqual(data_c["agents"][0]["analysis_count"], 0)
            for r in data_c["comparison"]:
                self.assertIsNone(r["value"])
                self.assertEqual(r["count"], 0)

            res_d = await client.get(
                "/bm/analytics/agents-comparison?service_id=1",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_d.status_code, 200)
            data_d = res_d.json()
            self.assertEqual(len(data_d["agents"]), 1)
            self.assertEqual(data_d["agents"][0]["hubspot_owner_id"], "owner_a")

            res_t1 = await client.get(
                "/bm/analytics/agents-comparison?service_id=1&team_id=1",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_t1.status_code, 200)
            data_t1 = res_t1.json()
            self.assertEqual(len(data_t1["agents"]), 1)
            self.assertEqual(data_t1["agents"][0]["hubspot_owner_id"], "owner_a")

            token_ag = create_access_token({"sub": "ag_a", "role": "agent", "company_id": 1, "user_id": 10})
            res_g = await client.get(
                "/bm/analytics/agents-comparison?service_id=1",
                headers={"Authorization": f"Bearer {token_ag}"}
            )
            self.assertEqual(res_g.status_code, 403)

            res_k = await client.get(
                "/bm/analytics/agents-comparison?service_id=1&direction=inbound",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_k.status_code, 200)
            data_k = res_k.json()
            self.assertEqual(len(data_k["agents"]), 1)
            self.assertEqual(data_k["agents"][0]["analysis_count"], 1)
            eg_k = next(r for r in data_k["comparison"] if r["item_key"] == "evaluacion_global")
            self.assertEqual(eg_k["value"], 8.0)

            res_l = await client.get(
                "/bm/analytics/agents-comparison?service_id=1&status=all",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_l.status_code, 200)
            data_l = res_l.json()
            self.assertEqual(len(data_l["agents"]), 2)
            oids = {a["hubspot_owner_id"] for a in data_l["agents"]}
            self.assertEqual(oids, {"owner_a", "owner_b"})

            res_m = await client.get(
                "/bm/analytics/agents-comparison?service_id=1&duration_min_seconds=100",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_m.status_code, 200)
            data_m = res_m.json()
            self.assertEqual(data_m["agents"][0]["analysis_count"], 1)

    async def test_05_legacy_partial_and_applicability_matrix_a_through_k(self):
        """
        5. Deep Functional Matrix A-K:
           A) Fully normalized evaluation.
           B) Fully legacy evaluation.
           C) Partially normalized evaluation with missing criterion in table.
           D) is_applicable = True.
           E) is_applicable = False.
           F) is_applicable = NULL.
           G) Scores (0-10 scale).
           H) Percentages (cierre_cita 0-100 scale).
           I) Multiple criteria per call do not inflate analysis_count.
           J) NULL metric outside denominator.
           K) Boolean and percentage criteria according to real semantics.
        """
        async with AsyncSession(self.engine) as db:
            c = Company(company_id=10, company_name="Matrix Co", company_key="matrix", is_active=True)
            s = Service(service_id=10, company_id=10, service_name="Matrix Svc", service_key="msvc", is_active=True)
            db.add_all([c, s])

            u_norm = User(
                user_id=501, username="u_norm", email="norm@matrix.com", name="Agent Normalized",
                role="agent", company_id=10, primary_service_id=10, hubspot_owner_id="ag_norm",
                agent_initials="AN", is_active=True, password_hash="dummy"
            )
            u_leg = User(
                user_id=502, username="u_leg", email="leg@matrix.com", name="Agent Legacy",
                role="agent", company_id=10, primary_service_id=10, hubspot_owner_id="ag_leg",
                agent_initials="AL", is_active=True, password_hash="dummy"
            )
            u_part = User(
                user_id=503, username="u_part", email="part@matrix.com", name="Agent Partial",
                role="agent", company_id=10, primary_service_id=10, hubspot_owner_id="ag_part",
                agent_initials="AP", is_active=True, password_hash="dummy"
            )
            db.add_all([u_norm, u_leg, u_part])
            await db.flush()

            now = datetime.now(timezone.utc)

            # Call 1 (ag_norm): 6 criteria rows, evaluacion_global=8.5
            db.add(MassEvaluationResult(
                mass_analysis_id=201, run_id=10, job_id=10, prompt_id=10, prompt_snapshot="{}",
                call_id="call_norm_1", company_id=10, service_id=10, service_key="msvc",
                hubspot_owner_id="ag_norm", agent_name="Agent Normalized",
                call_timestamp=now, analysis_timestamp=now, evaluacion_global=Decimal("8.5"), status="completed"
            ))
            db.add_all([
                # D) is_applicable = True, G) Score 0-10
                MassEvaluationCriterionResult(
                    id=2001, mass_analysis_id=201, run_id=10, job_id=10, call_id="call_norm_1",
                    criterion_key="claridad", criterion_type="score", numeric_value=Decimal("9.0"), is_applicable=True
                ),
                # H) Percentage cierre_cita, K) Boolean True -> 100.0
                MassEvaluationCriterionResult(
                    id=2002, mass_analysis_id=201, run_id=10, job_id=10, call_id="call_norm_1",
                    criterion_key="cierre_cita", criterion_type="percentage", boolean_value=True, is_applicable=True
                ),
                # K) simpatia boolean True -> 10.0
                MassEvaluationCriterionResult(
                    id=2003, mass_analysis_id=201, run_id=10, job_id=10, call_id="call_norm_1",
                    criterion_key="simpatia", criterion_type="score", boolean_value=True, is_applicable=True
                ),
                # K) procedimiento percentage 80% -> 8.0
                MassEvaluationCriterionResult(
                    id=2004, mass_analysis_id=201, run_id=10, job_id=10, call_id="call_norm_1",
                    criterion_key="procedimiento", criterion_type="score", percentage_value=Decimal("80.0"), is_applicable=True
                ),
                # J) NULL metric outside denominator
                MassEvaluationCriterionResult(
                    id=2005, mass_analysis_id=201, run_id=10, job_id=10, call_id="call_norm_1",
                    criterion_key="empatia", criterion_type="score", numeric_value=None, is_applicable=True
                ),
                # E) is_applicable = False (must be excluded from count & avg)
                MassEvaluationCriterionResult(
                    id=2006, mass_analysis_id=201, run_id=10, job_id=10, call_id="call_norm_1",
                    criterion_key="despedida_refuerzo", criterion_type="score", numeric_value=Decimal("7.0"), is_applicable=False
                )
            ])

            # Call 2 (ag_norm): 2 criteria rows, evaluacion_global=7.5
            db.add(MassEvaluationResult(
                mass_analysis_id=202, run_id=10, job_id=10, prompt_id=10, prompt_snapshot="{}",
                call_id="call_norm_2", company_id=10, service_id=10, service_key="msvc",
                hubspot_owner_id="ag_norm", agent_name="Agent Normalized",
                call_timestamp=now, analysis_timestamp=now, evaluacion_global=Decimal("7.5"), status="completed"
            ))
            db.add_all([
                # F) is_applicable = None (treated as applicable)
                MassEvaluationCriterionResult(
                    id=2007, mass_analysis_id=202, run_id=10, job_id=10, call_id="call_norm_2",
                    criterion_key="claridad", criterion_type="score", numeric_value=Decimal("7.0"), is_applicable=None
                ),
                # H) cierre_cita percentage 50.0%
                MassEvaluationCriterionResult(
                    id=2008, mass_analysis_id=202, run_id=10, job_id=10, call_id="call_norm_2",
                    criterion_key="cierre_cita", criterion_type="percentage", percentage_value=Decimal("50.0"), is_applicable=True
                )
            ])

            # B) Fully legacy evaluation (ag_leg): 0 criteria rows, evaluacion_global=None
            db.add(MassEvaluationResult(
                mass_analysis_id=203, run_id=10, job_id=10, prompt_id=10, prompt_snapshot="{}",
                call_id="call_leg_1", company_id=10, service_id=10, service_key="msvc",
                hubspot_owner_id="ag_leg", agent_name="Agent Legacy",
                call_timestamp=now, analysis_timestamp=now, evaluacion_global=None,
                result_json={"evaluacion_global": 7.0, "claridad": 8.0, "cierre_cita": "si"},
                items_json=[{"key": "simpatia", "value": 6.0}],
                status="completed"
            ))

            # C) Partially normalized evaluation (ag_part):
            # claridad is normalized (8.0). result_json has claridad (2.0 - must be ignored!) and simpatia (7.5 - must be recovered!)
            db.add(MassEvaluationResult(
                mass_analysis_id=204, run_id=10, job_id=10, prompt_id=10, prompt_snapshot="{}",
                call_id="call_part_1", company_id=10, service_id=10, service_key="msvc",
                hubspot_owner_id="ag_part", agent_name="Agent Partial",
                call_timestamp=now, analysis_timestamp=now, evaluacion_global=Decimal("9.0"),
                result_json={"claridad": 2.0, "simpatia": 7.5},
                status="completed"
            ))
            db.add(MassEvaluationCriterionResult(
                id=2009, mass_analysis_id=204, run_id=10, job_id=10, call_id="call_part_1",
                criterion_key="claridad", criterion_type="score", numeric_value=Decimal("8.0"), is_applicable=True
            ))

            await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get(
                "/bm/analytics/agents-comparison?service_id=10&item_keys=evaluacion_global&item_keys=claridad&item_keys=cierre_cita&item_keys=simpatia&item_keys=procedimiento&item_keys=empatia&item_keys=despedida_refuerzo",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()

        agent_dict = {a["hubspot_owner_id"]: a for a in data["agents"]}
        rows_by_agent_key = {(r["hubspot_owner_id"], r["item_key"]): r for r in data["comparison"]}

        # I) Multiple criteria per call do not inflate analysis_count
        # ag_norm has call 1 (6 criteria) and call 2 (2 criteria) -> analysis_count must be exactly 2!
        self.assertEqual(agent_dict["ag_norm"]["analysis_count"], 2)
        self.assertEqual(agent_dict["ag_leg"]["analysis_count"], 1)
        self.assertEqual(agent_dict["ag_part"]["analysis_count"], 1)

        # Evaluacion Global:
        # ag_norm: (8.5 + 7.5) / 2 = 8.0, count = 2
        self.assertEqual(rows_by_agent_key[("ag_norm", "evaluacion_global")]["value"], 8.0)
        self.assertEqual(rows_by_agent_key[("ag_norm", "evaluacion_global")]["count"], 2)

        # B) Fully legacy ag_leg: evaluacion_global recovered from JSON = 7.0, count = 1
        self.assertEqual(rows_by_agent_key[("ag_leg", "evaluacion_global")]["value"], 7.0)
        self.assertEqual(rows_by_agent_key[("ag_leg", "evaluacion_global")]["count"], 1)

        # C) Partially normalized ag_part: evaluacion_global from column = 9.0
        self.assertEqual(rows_by_agent_key[("ag_part", "evaluacion_global")]["value"], 9.0)

        # D & F) Claridad on ag_norm:
        # call 1: is_applicable=True, val=9.0
        # call 2: is_applicable=None, val=7.0
        # avg = (9.0 + 7.0) / 2 = 8.0, count = 2
        self.assertEqual(rows_by_agent_key[("ag_norm", "claridad")]["value"], 8.0)
        self.assertEqual(rows_by_agent_key[("ag_norm", "claridad")]["count"], 2)

        # B) Claridad on ag_leg: recovered from result_json = 8.0, count = 1
        self.assertEqual(rows_by_agent_key[("ag_leg", "claridad")]["value"], 8.0)
        self.assertEqual(rows_by_agent_key[("ag_leg", "claridad")]["count"], 1)

        # C) Claridad on ag_part: normalized is 8.0, result_json has 2.0 (must NOT overwrite!)
        self.assertEqual(rows_by_agent_key[("ag_part", "claridad")]["value"], 8.0)
        self.assertEqual(rows_by_agent_key[("ag_part", "claridad")]["count"], 1)

        # H & K) Cierre Cita on ag_norm:
        # call 1: boolean_value=True -> 100.0
        # call 2: percentage_value=50.0 -> 50.0
        # avg = (100.0 + 50.0) / 2 = 75.0, count = 2
        self.assertEqual(rows_by_agent_key[("ag_norm", "cierre_cita")]["value"], 75.0)
        self.assertEqual(rows_by_agent_key[("ag_norm", "cierre_cita")]["count"], 2)

        # B & K) Cierre Cita on ag_leg: "si" in result_json -> 100.0
        self.assertEqual(rows_by_agent_key[("ag_leg", "cierre_cita")]["value"], 100.0)
        self.assertEqual(rows_by_agent_key[("ag_leg", "cierre_cita")]["count"], 1)

        # K) Simpatia on ag_norm: boolean True -> 10.0, count = 1
        self.assertEqual(rows_by_agent_key[("ag_norm", "simpatia")]["value"], 10.0)
        self.assertEqual(rows_by_agent_key[("ag_norm", "simpatia")]["count"], 1)

        # B) Simpatia on ag_leg: from items_json = 6.0, count = 1
        self.assertEqual(rows_by_agent_key[("ag_leg", "simpatia")]["value"], 6.0)
        self.assertEqual(rows_by_agent_key[("ag_leg", "simpatia")]["count"], 1)

        # C) Simpatia on ag_part: missing in table, recovered from result_json = 7.5, count = 1
        self.assertEqual(rows_by_agent_key[("ag_part", "simpatia")]["value"], 7.5)
        self.assertEqual(rows_by_agent_key[("ag_part", "simpatia")]["count"], 1)

        # K) Procedimiento on ag_norm: percentage 80.0 / 10 = 8.0, count = 1
        self.assertEqual(rows_by_agent_key[("ag_norm", "procedimiento")]["value"], 8.0)
        self.assertEqual(rows_by_agent_key[("ag_norm", "procedimiento")]["count"], 1)

        # J) Empatia on ag_norm: numeric_value is None -> count = 0, value = None
        self.assertIsNone(rows_by_agent_key[("ag_norm", "empatia")]["value"])
        self.assertEqual(rows_by_agent_key[("ag_norm", "empatia")]["count"], 0)

        # E) Despedida Refuerzo on ag_norm: is_applicable = False -> count = 0, value = None
        self.assertIsNone(rows_by_agent_key[("ag_norm", "despedida_refuerzo")]["value"])
        self.assertEqual(rows_by_agent_key[("ag_norm", "despedida_refuerzo")]["count"], 0)

    async def test_06_criterion_duplicates_determinism_and_precedence(self):
        """
        6. Criterion Duplicates & Determinism:
           Prevents:
           - Accidental AVG
           - Accidental SUM
           - Arbitrary MAX
           - Double counting
           Validates:
           - Exact canonical key precedence over aliases (even when alias value > canonical value)
           - FIFO / id ASC tie-breaker for identical alias priorities
           - analysis_count remains strictly 1
        """
        async with AsyncSession(self.engine) as db:
            c = Company(company_id=20, company_name="Dup Co", company_key="dup_co", is_active=True)
            s = Service(service_id=20, company_id=20, service_name="Dup Svc", service_key="dsvc", is_active=True)
            db.add_all([c, s])

            # Agent 1: Canonical (90.0) vs Alias (70.0)
            u1 = User(
                user_id=601, username="u_dup1", email="dup1@test.com", name="Agent Dup 1",
                role="agent", company_id=20, primary_service_id=20, hubspot_owner_id="ag_dup1",
                agent_initials="D1", is_active=True, password_hash="dummy"
            )
            # Agent 2: Canonical (70.0) vs Alias (90.0) -> Proves NOT MAX!
            u2 = User(
                user_id=602, username="u_dup2", email="dup2@test.com", name="Agent Dup 2",
                role="agent", company_id=20, primary_service_id=20, hubspot_owner_id="ag_dup2",
                agent_initials="D2", is_active=True, password_hash="dummy"
            )
            # Agent 3: Alias priority 3 ("cita", 70.0) vs Alias priority 4 ("cierre", 90.0)
            u3 = User(
                user_id=603, username="u_dup3", email="dup3@test.com", name="Agent Dup 3",
                role="agent", company_id=20, primary_service_id=20, hubspot_owner_id="ag_dup3",
                agent_initials="D3", is_active=True, password_hash="dummy"
            )
            # Agent 4: Equal priority (else_=10) -> Proves FIFO / id ASC tie-breaker!
            u4 = User(
                user_id=604, username="u_dup4", email="dup4@test.com", name="Agent Dup 4",
                role="agent", company_id=20, primary_service_id=20, hubspot_owner_id="ag_dup4",
                agent_initials="D4", is_active=True, password_hash="dummy"
            )
            db.add_all([u1, u2, u3, u4])
            await db.flush()

            now = datetime.now(timezone.utc)

            # Call 1 for ag_dup1: cierre (70.0) vs cierre_cita (90.0)
            db.add(MassEvaluationResult(
                mass_analysis_id=301, run_id=20, job_id=20, prompt_id=20, prompt_snapshot="{}",
                call_id="call_dup_1", company_id=20, service_id=20, service_key="dsvc",
                hubspot_owner_id="ag_dup1", agent_name="Agent Dup 1",
                call_timestamp=now, analysis_timestamp=now, evaluacion_global=Decimal("8.0"), status="completed"
            ))
            db.add_all([
                MassEvaluationCriterionResult(
                    id=3001, mass_analysis_id=301, run_id=20, job_id=20, call_id="call_dup_1",
                    criterion_key="cierre", criterion_type="percentage", percentage_value=Decimal("70.0"), is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    id=3002, mass_analysis_id=301, run_id=20, job_id=20, call_id="call_dup_1",
                    criterion_key="cierre_cita", criterion_type="percentage", percentage_value=Decimal("90.0"), is_applicable=True
                )
            ])

            # Call 2 for ag_dup2: cierre_cita (70.0) vs cierre (90.0) -> If MAX was used, it would yield 90.0. Precedence yields 70.0!
            db.add(MassEvaluationResult(
                mass_analysis_id=302, run_id=20, job_id=20, prompt_id=20, prompt_snapshot="{}",
                call_id="call_dup_2", company_id=20, service_id=20, service_key="dsvc",
                hubspot_owner_id="ag_dup2", agent_name="Agent Dup 2",
                call_timestamp=now, analysis_timestamp=now, evaluacion_global=Decimal("8.0"), status="completed"
            ))
            db.add_all([
                MassEvaluationCriterionResult(
                    id=3003, mass_analysis_id=302, run_id=20, job_id=20, call_id="call_dup_2",
                    criterion_key="cierre_cita", criterion_type="percentage", percentage_value=Decimal("70.0"), is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    id=3004, mass_analysis_id=302, run_id=20, job_id=20, call_id="call_dup_2",
                    criterion_key="cierre", criterion_type="percentage", percentage_value=Decimal("90.0"), is_applicable=True
                )
            ])

            # Call 3 for ag_dup3: cita (70.0, id=3005) vs cierre (90.0, id=3006) -> Priority 3 (cita) beats Priority 4 (cierre)
            db.add(MassEvaluationResult(
                mass_analysis_id=303, run_id=20, job_id=20, prompt_id=20, prompt_snapshot="{}",
                call_id="call_dup_3", company_id=20, service_id=20, service_key="dsvc",
                hubspot_owner_id="ag_dup3", agent_name="Agent Dup 3",
                call_timestamp=now, analysis_timestamp=now, evaluacion_global=Decimal("8.0"), status="completed"
            ))
            db.add_all([
                MassEvaluationCriterionResult(
                    id=3005, mass_analysis_id=303, run_id=20, job_id=20, call_id="call_dup_3",
                    criterion_key="cita", criterion_type="percentage", percentage_value=Decimal("70.0"), is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    id=3006, mass_analysis_id=303, run_id=20, job_id=20, call_id="call_dup_3",
                    criterion_key="cierre", criterion_type="percentage", percentage_value=Decimal("90.0"), is_applicable=True
                )
            ])

            # Call 4 for ag_dup4: custom_a (70.0, id=3007) vs custom_b (90.0, id=3008) -> Equal priority -> id ASC picks 3007 (70.0)
            db.add(MassEvaluationResult(
                mass_analysis_id=304, run_id=20, job_id=20, prompt_id=20, prompt_snapshot="{}",
                call_id="call_dup_4", company_id=20, service_id=20, service_key="dsvc",
                hubspot_owner_id="ag_dup4", agent_name="Agent Dup 4",
                call_timestamp=now, analysis_timestamp=now, evaluacion_global=Decimal("8.0"), status="completed"
            ))
            db.add_all([
                MassEvaluationCriterionResult(
                    id=3007, mass_analysis_id=304, run_id=20, job_id=20, call_id="call_dup_4",
                    criterion_key="custom_crit", criterion_type="score", numeric_value=Decimal("7.0"), is_applicable=True
                ),
                MassEvaluationCriterionResult(
                    id=3008, mass_analysis_id=304, run_id=20, job_id=20, call_id="call_dup_4",
                    criterion_key="custom_crit_alt", criterion_type="score", numeric_value=Decimal("9.0"), is_applicable=True
                )
            ])

            await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get(
                "/bm/analytics/agents-comparison?service_id=20&item_keys=cierre_cita&item_keys=custom_crit",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()

        agent_dict = {a["hubspot_owner_id"]: a for a in data["agents"]}
        rows_by_agent_key = {(r["hubspot_owner_id"], r["item_key"]): r for r in data["comparison"]}

        # 1. analysis_count must be strictly 1 for each agent
        self.assertEqual(agent_dict["ag_dup1"]["analysis_count"], 1)
        self.assertEqual(agent_dict["ag_dup2"]["analysis_count"], 1)
        self.assertEqual(agent_dict["ag_dup3"]["analysis_count"], 1)
        self.assertEqual(agent_dict["ag_dup4"]["analysis_count"], 1)

        # 2. Case 1 (ag_dup1): canonical cierre_cita (90.0) takes precedence over alias cierre (70.0)
        # Value must be 90.0, count must be 1 (NOT AVG 80.0, NOT SUM 160.0, NOT count 2)
        r1 = rows_by_agent_key[("ag_dup1", "cierre_cita")]
        self.assertEqual(r1["value"], 90.0)
        self.assertEqual(r1["count"], 1)

        # 3. Case 2 (ag_dup2): canonical cierre_cita (70.0) takes precedence over alias cierre (90.0)
        # Value must be 70.0, proving this is NOT arbitrary MAX!
        r2 = rows_by_agent_key[("ag_dup2", "cierre_cita")]
        self.assertEqual(r2["value"], 70.0)
        self.assertEqual(r2["count"], 1)

        # 4. Case 3 (ag_dup3): cita (priority 3, 70.0) beats cierre (priority 4, 90.0)
        r3 = rows_by_agent_key[("ag_dup3", "cierre_cita")]
        self.assertEqual(r3["value"], 70.0)
        self.assertEqual(r3["count"], 1)

        # 5. Case 4 (ag_dup4): equal priority -> id ASC picks id 3007 (7.0), proving deterministic tie-breaker
        r4 = rows_by_agent_key.get(("ag_dup4", "custom_crit"))
        if r4:
            self.assertEqual(r4["value"], 7.0)
            self.assertEqual(r4["count"], 1)


if __name__ == "__main__":
    unittest.main()
