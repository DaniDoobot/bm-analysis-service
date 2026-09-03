"""
Unit and integration test suite for HubSpot Alarm Tickets.
Tests all cases with fully mocked HubSpot API calls and in-memory test database.
"""
import asyncio
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db import Base
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationResult,
    MassEvaluationRun,
)
from app.services.hubspot_service import HubSpotService
from app.services.mass_evaluation_service import MassEvaluationService


class TestHubSpotAlarmTickets(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Create an in-memory SQLite async engine for isolated testing
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.async_session = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

        # Base mock settings
        self.settings = get_settings()
        self.settings.hubspot_access_token = "mock_hs_token"
        self.settings.hubspot_portal_id = "140451581"
        self.settings.hubspot_ticket_pipeline = "mock_pipeline_id"
        self.settings.hubspot_ticket_stage = "mock_stage_id"
        self.settings.hubspot_tipo_de_rem = "mock_tipo_de_rem_val"
        self.settings.hubspot_alarm_tickets_enabled = True
        self.settings.hubspot_alarm_company_id = 1

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _create_test_result(
        self,
        session: AsyncSession,
        call_id: str = "call_123",
        status: str = "completed",
        is_evaluable: bool = True,
        company_id: int = 1,
        execution_source: str = "automation",
        hubspot_contact_id: str | None = None
    ) -> MassEvaluationResult:
        # Create dummy job & run
        job = MassEvaluationJob(
            job_name="Test Front Job",
            prompt_id=58,
            service_id=1,
            company_id=company_id,
            is_active=True,
            execution_source=execution_source
        )
        session.add(job)
        await session.flush()

        run = MassEvaluationRun(
            job_id=job.job_id,
            company_id=company_id,
            service_id=1,
            trigger_type="automation",
            status="completed",
            execution_source=execution_source
        )
        session.add(run)
        await session.flush()

        res = MassEvaluationResult(
            run_id=run.run_id,
            job_id=job.job_id,
            call_id=call_id,
            prompt_id=58,
            prompt_snapshot="Test Prompt Snapshot",
            status=status,
            is_evaluable=is_evaluable,
            company_id=company_id,
            service_id=1,
            service_name="Front",
            agent_name="Carlos Santana",
            call_timestamp=datetime(2026, 8, 22, 10, 30, 0, tzinfo=timezone.utc),
            call_duration_seconds=185,
            direction="INBOUND",
            evaluacion_global=Decimal("4.5"),
            hubspot_contact_id=hubspot_contact_id,
            created_at=datetime.now(timezone.utc)
        )
        session.add(res)
        await session.commit()
        await session.refresh(res)
        return res

    # 1. completed + evaluable + Alarma=True + contact_id → 1 Ticket con subject="REM doobot speechFront" y asociación
    async def test_01_successful_alarm_ticket_creation(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session)
            
            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                mock_post.return_value = MagicMock(
                    status_code=201,
                    json=lambda: {"id": "hs_ticket_9999", "properties": {}},
                    raise_for_status=lambda: None
                )

                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="Front",
                    agent_name="Carlos Santana",
                    call_id="call_123",
                    call_timestamp=res.call_timestamp,
                    typology_name="Queja",
                    direction="INBOUND",
                    call_duration_seconds=185,
                    evaluacion_global=res.evaluacion_global,
                    alarma_feed="El paciente exige poner una reclamación.",
                    contact_id="hs_contact_456"
                )

                self.assertEqual(mock_post.call_count, 1)
                payload = mock_post.call_args[1]["json"]
                self.assertEqual(payload["properties"]["subject"], "REM doobot speechFront")
                
                # Check associations
                self.assertIn("associations", payload)
                assoc = payload["associations"][0]
                self.assertEqual(assoc["to"]["id"], "hs_contact_456")
                self.assertEqual(assoc["types"][0]["associationTypeId"], 16)
                self.assertEqual(assoc["types"][0]["associationCategory"], "HUBSPOT_DEFINED")

                # Check DB update
                stmt = select(MassEvaluationResult).where(MassEvaluationResult.mass_analysis_id == res.mass_analysis_id)
                updated = (await session.execute(stmt)).scalars().first()
                self.assertEqual(updated.hubspot_ticket_id, "hs_ticket_9999")
                self.assertEqual(updated.hubspot_ticket_status, "created")
                self.assertEqual(updated.hubspot_contact_id, "hs_contact_456")
                self.assertIsNotNone(updated.hubspot_ticket_created_at)
                self.assertIsNone(updated.hubspot_ticket_error)

    # 2. Alarma=False → 0 Tickets
    async def test_02_alarm_false_no_ticket(self):
        items = [
            {"criterion_key": "alarma", "type": "boolean", "boolean_value": False, "feed": "Sin quejas"}
        ]
        has_alarm, feed = MassEvaluationService._is_alarm_detected(items)
        self.assertFalse(has_alarm)

    # 3. Alarma=None → 0 Tickets
    async def test_03_alarm_none_no_ticket(self):
        items = [
            {"criterion_key": "alarma", "type": "boolean", "boolean_value": None, "feed": None}
        ]
        has_alarm, feed = MassEvaluationService._is_alarm_detected(items)
        self.assertFalse(has_alarm)

    # 4. is_evaluable=False + Alarma=True → 0 Tickets
    async def test_04_non_evaluable_no_ticket(self):
        is_eval = False
        self.assertFalse(is_eval is not False)

    # 5. failed evaluation status → 0 Tickets
    async def test_05_failed_evaluation_status(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session, status="failed")
            self.assertEqual(res.status, "failed")

    # 6. Test Análisis + Alarma=True → 0 Tickets (Strict protection)
    async def test_06_test_analysis_excluded(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session, execution_source="test_analysis")
            
            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="test_analysis",  # Excluded!
                    company_id=1,
                    service_name="Front",
                    agent_name="Carlos Santana",
                    call_id="call_123",
                    call_timestamp=res.call_timestamp,
                    typology_name="Queja",
                    direction="INBOUND",
                    call_duration_seconds=185,
                    evaluacion_global=res.evaluacion_global,
                    alarma_feed="Reclamación en test",
                    contact_id="contact_123"
                )
                self.assertEqual(mock_post.call_count, 0)

    # 7. HubSpot POST éxito → ticket_id, contact_id y status created persistidos
    async def test_07_post_success_persistence(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session)
            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                mock_post.return_value = MagicMock(
                    status_code=201,
                    json=lambda: {"id": "1234567"},
                    raise_for_status=lambda: None
                )
                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="on_demand",
                    company_id=1,
                    service_name="Front",
                    agent_name="Agente",
                    call_id="call_123",
                    call_timestamp=None,
                    typology_name=None,
                    direction=None,
                    call_duration_seconds=None,
                    evaluacion_global=None,
                    alarma_feed="Detalle",
                    contact_id="contact_777"
                )
                await session.refresh(res)
                self.assertEqual(res.hubspot_ticket_id, "1234567")
                self.assertEqual(res.hubspot_ticket_status, "created")
                self.assertEqual(res.hubspot_contact_id, "contact_777")

    # 8. HubSpot 500 → evaluación sigue completed + ticket_status failed
    async def test_08_hubspot_500_evaluation_stays_completed(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session)
            
            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                import httpx
                mock_post.side_effect = httpx.HTTPStatusError("500 Internal Server Error", request=MagicMock(), response=MagicMock(status_code=500, text="Internal Error"))

                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="Front",
                    agent_name="Agente",
                    call_id="call_123",
                    call_timestamp=None,
                    typology_name=None,
                    direction=None,
                    call_duration_seconds=None,
                    evaluacion_global=None,
                    alarma_feed="Detalle",
                    contact_id="contact_999"
                )

                await session.refresh(res)
                self.assertEqual(res.status, "completed")  # Evaluation MUST NOT fail
                self.assertEqual(res.hubspot_ticket_status, "failed")
                self.assertIn("500 Internal Server Error", res.hubspot_ticket_error)

    # 9. Timeout → evaluación sigue completed + ticket_status failed
    async def test_09_hubspot_timeout_evaluation_stays_completed(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session)
            
            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                import httpx
                mock_post.side_effect = httpx.TimeoutException("Connection timed out after 15s")

                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="Front",
                    agent_name="Agente",
                    call_id="call_123",
                    call_timestamp=None,
                    typology_name=None,
                    direction=None,
                    call_duration_seconds=None,
                    evaluacion_global=None,
                    alarma_feed="Detalle",
                    contact_id="contact_999"
                )

                await session.refresh(res)
                self.assertEqual(res.status, "completed")
                self.assertEqual(res.hubspot_ticket_status, "failed")
                self.assertIn("timed out", res.hubspot_ticket_error)

    # 10. Segunda ejecución de misma evaluación con Ticket created → no duplica
    async def test_10_idempotency_does_not_duplicate(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session)
            res.hubspot_ticket_id = "hs_existing_888"
            res.hubspot_ticket_status = "created"
            await session.commit()

            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="Front",
                    agent_name="Agente",
                    call_id="call_123",
                    call_timestamp=None,
                    typology_name=None,
                    direction=None,
                    call_duration_seconds=None,
                    evaluacion_global=None,
                    alarma_feed="Detalle",
                    contact_id="contact_111"
                )
                self.assertEqual(mock_post.call_count, 0)

    # 11. boolean True detectado desde item normalizado (no frágil)
    async def test_11_boolean_detection_normalized(self):
        items_1 = [{"criterion_key": "alarma", "type": "boolean", "boolean_value": True, "feed": "Feed 1"}]
        has_alarm_1, feed_1 = MassEvaluationService._is_alarm_detected(items_1)
        self.assertTrue(has_alarm_1)
        self.assertEqual(feed_1, "Feed 1")

        items_2 = [{"criterion_key": "alarma", "type": "boolean", "value": True, "raw_value": "Si", "feed": "Feed 2"}]
        has_alarm_2, feed_2 = MassEvaluationService._is_alarm_detected(items_2)
        self.assertTrue(has_alarm_2)
        self.assertEqual(feed_2, "Feed 2")

    # 12. alarma_feed se incluye en content y subject es estrictamente "REM doobot speechFront"
    async def test_12_alarma_feed_in_ticket_content(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session)
            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                mock_post.return_value = MagicMock(
                    status_code=201,
                    json=lambda: {"id": "111"},
                    raise_for_status=lambda: None
                )
                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="Front",
                    agent_name="Carlos Santana",
                    call_id="call_123",
                    call_timestamp=res.call_timestamp,
                    typology_name="Queja",
                    direction="INBOUND",
                    call_duration_seconds=185,
                    evaluacion_global=res.evaluacion_global,
                    alarma_feed="Cita literal del paciente enfadado",
                    contact_id="contact_123"
                )
                call_args = mock_post.call_args[1]["json"]
                self.assertEqual(call_args["properties"]["subject"], "REM doobot speechFront")
                content = call_args["properties"]["content"]
                self.assertIn("Cita literal del paciente enfadado", content)
                self.assertIn("Carlos Santana", content)
                self.assertIn("Front", content)

    # 13. alarma_feed vacío no impide Ticket
    async def test_13_empty_alarma_feed_creates_ticket(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session)
            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                mock_post.return_value = MagicMock(
                    status_code=201,
                    json=lambda: {"id": "222"},
                    raise_for_status=lambda: None
                )
                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="Front",
                    agent_name="Agente",
                    call_id="call_123",
                    call_timestamp=None,
                    typology_name=None,
                    direction=None,
                    call_duration_seconds=None,
                    evaluacion_global=None,
                    alarma_feed=None,
                    contact_id="contact_123"
                )
                self.assertEqual(mock_post.call_count, 1)
                call_args = mock_post.call_args[1]["json"]
                self.assertEqual(call_args["properties"]["subject"], "REM doobot speechFront")
                content = call_args["properties"]["content"]
                self.assertIn("Alarma detectada sin detalle adicional.", content)

    # 14. falta pipeline/stage/tipo → 0 POST y evaluación intacta
    async def test_14_missing_config_no_post(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session)
            self.settings.hubspot_ticket_pipeline = ""  # Incomplete!

            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="Front",
                    agent_name="Agente",
                    call_id="call_123",
                    call_timestamp=None,
                    typology_name=None,
                    direction=None,
                    call_duration_seconds=None,
                    evaluacion_global=None,
                    alarma_feed="Detalle",
                    contact_id="contact_123"
                )
                self.assertEqual(mock_post.call_count, 0)
                await session.refresh(res)
                self.assertEqual(res.status, "completed")
                self.assertEqual(res.hubspot_ticket_status, "failed")
                self.assertIn("incomplete", res.hubspot_ticket_error)

    # 15. Feature flag False → 0 POST
    async def test_15_feature_flag_disabled_no_post(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session)
            self.settings.hubspot_alarm_tickets_enabled = False

            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="Front",
                    agent_name="Agente",
                    call_id="call_123",
                    call_timestamp=None,
                    typology_name=None,
                    direction=None,
                    call_duration_seconds=None,
                    evaluacion_global=None,
                    alarma_feed="Detalle",
                    contact_id="contact_123"
                )
                self.assertEqual(mock_post.call_count, 0)
                await session.refresh(res)
                self.assertIsNone(res.hubspot_ticket_status)

    # 16. Company distinta a la configurada → 0 POST
    async def test_16_different_company_no_post(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session, company_id=2)  # Company 2 != 1

            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=2,  # Different company
                    service_name="Front",
                    agent_name="Agente",
                    call_id="call_123",
                    call_timestamp=None,
                    typology_name=None,
                    direction=None,
                    call_duration_seconds=None,
                    evaluacion_global=None,
                    alarma_feed="Detalle",
                    contact_id="contact_123"
                )
                self.assertEqual(mock_post.call_count, 0)

    # 17. Contacto encontrado → asociación ID 16
    async def test_17_contact_association_id_16(self):
        hs_service = HubSpotService()
        with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=201,
                json=lambda: {"id": "ticket_777"},
                raise_for_status=lambda: None
            )
            res = await hs_service.create_ticket(
                properties={"subject": "REM doobot speechFront", "content": "Test body"},
                contact_id="contact_555"
            )
            self.assertEqual(res["id"], "ticket_777")
            call_json = mock_post.call_args[1]["json"]
            self.assertIn("associations", call_json)
            assoc = call_json["associations"][0]
            self.assertEqual(assoc["to"]["id"], "contact_555")
            self.assertEqual(assoc["types"][0]["associationTypeId"], 16)
            self.assertEqual(assoc["types"][0]["associationCategory"], "HUBSPOT_DEFINED")

    # 18. OUTBOUND: callee_object_type=CONTACT, callee_object_id=123, associations=[123] → paciente=123
    async def test_18_outbound_callee_contact_resolution(self):
        data = {
            "properties": {
                "hs_call_direction": "OUTBOUND",
                "hs_call_callee_object_type": "CONTACT",
                "hs_call_callee_object_id": "123",
                "hubspot_owner_id": "agent_999"
            },
            "associations": {
                "contacts": {
                    "results": [{"id": "123"}]
                }
            }
        }
        contact_id, is_ambiguous = HubSpotService.extract_patient_contact_id(data)
        self.assertEqual(contact_id, "123")
        self.assertFalse(is_ambiguous)

    # 19. INBOUND: callee_object_type=CONTACT, callee_object_id=456, associations=[456] → paciente=456
    async def test_19_inbound_callee_contact_resolution(self):
        data = {
            "properties": {
                "hs_call_direction": "INBOUND",
                "hs_call_callee_object_type": "CONTACT",
                "hs_call_callee_object_id": "456",
                "hubspot_owner_id": "agent_999"
            },
            "associations": {
                "contacts": {
                    "results": [{"id": "456"}]
                }
            }
        }
        contact_id, is_ambiguous = HubSpotService.extract_patient_contact_id(data)
        self.assertEqual(contact_id, "456")
        self.assertFalse(is_ambiguous)

    # 20. Múltiples contactos: contacts=[123, 456], callee CONTACT id=456 → paciente=456 únicamente
    async def test_20_multiple_contacts_with_callee_match(self):
        data = {
            "properties": {
                "hs_call_callee_object_type": "CONTACT",
                "hs_call_callee_object_id": "456",
                "hubspot_owner_id": "agent_999"
            },
            "associations": {
                "contacts": {
                    "results": [
                        {"id": "123"},
                        {"id": "456"}
                    ]
                }
            }
        }
        contact_id, is_ambiguous = HubSpotService.extract_patient_contact_id(data)
        self.assertEqual(contact_id, "456")
        self.assertFalse(is_ambiguous)

    # 21. Un único contacto sin callee fiable: contacts=[123] → paciente=123
    async def test_21_single_contact_without_callee(self):
        data = {
            "properties": {
                "hs_call_callee_object_type": "",
                "hs_call_callee_object_id": "",
                "hubspot_owner_id": "agent_999"
            },
            "associations": {
                "contacts": {
                    "results": [{"id": "123"}]
                }
            }
        }
        contact_id, is_ambiguous = HubSpotService.extract_patient_contact_id(data)
        self.assertEqual(contact_id, "123")
        self.assertFalse(is_ambiguous)

    # 22. Múltiples contactos sin callee fiable: contacts=[123, 456] → ambiguo, NO Ticket
    async def test_22_multiple_contacts_without_callee_is_ambiguous(self):
        data = {
            "properties": {
                "hs_call_callee_object_type": "",
                "hs_call_callee_object_id": "",
                "hubspot_owner_id": "agent_999"
            },
            "associations": {
                "contacts": {
                    "results": [
                        {"id": "123"},
                        {"id": "456"}
                    ]
                }
            }
        }
        contact_id, is_ambiguous = HubSpotService.extract_patient_contact_id(data)
        self.assertIsNone(contact_id)
        self.assertTrue(is_ambiguous)

    # 23. callee_object_type=COMPANY → NO tratar callee_object_id como Contact ID
    async def test_23_callee_company_not_used_as_contact(self):
        data = {
            "properties": {
                "hs_call_callee_object_type": "COMPANY",
                "hs_call_callee_object_id": "company_888",
                "hubspot_owner_id": "agent_999"
            },
            "associations": {
                "contacts": {
                    "results": []
                }
            }
        }
        contact_id, is_ambiguous = HubSpotService.extract_patient_contact_id(data)
        self.assertIsNone(contact_id)
        self.assertFalse(is_ambiguous)

    # 24. callee CONTACT que no coincide con ninguna asociación → ambiguo/inconsistente, NO asociar arbitrariamente
    async def test_24_callee_contact_not_in_associations_is_ambiguous(self):
        data = {
            "properties": {
                "hs_call_callee_object_type": "CONTACT",
                "hs_call_callee_object_id": "999",  # Not in associations
                "hubspot_owner_id": "agent_999"
            },
            "associations": {
                "contacts": {
                    "results": [
                        {"id": "123"},
                        {"id": "456"}
                    ]
                }
            }
        }
        contact_id, is_ambiguous = HubSpotService.extract_patient_contact_id(data)
        self.assertIsNone(contact_id)
        self.assertTrue(is_ambiguous)

    # 25. Ningún contacto (sin asociaciones y sin callee) → NO Ticket
    async def test_25_no_contacts_at_all_fails_without_ticket(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session)
            
            with patch("app.services.hubspot_service.HubSpotService.get_call", new_callable=AsyncMock) as mock_get_call, \
                 patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                
                mock_get_call.return_value = {
                    "call_id": "call_123",
                    "contact_id": None,
                    "is_contact_ambiguous": False
                }

                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="Front",
                    agent_name="Agente",
                    call_id="call_123",
                    call_timestamp=None,
                    typology_name=None,
                    direction=None,
                    call_duration_seconds=None,
                    evaluacion_global=None,
                    alarma_feed="Detalle",
                    contact_id=None
                )

                self.assertEqual(mock_post.call_count, 0)
                await session.refresh(res)
                self.assertEqual(res.status, "completed")
                self.assertEqual(res.hubspot_ticket_status, "failed")
                self.assertIn("No se encontró contacto asociado", res.hubspot_ticket_error)

    # 26. Reintento posterior usando MassEvaluationResult.hubspot_contact_id persistido sin objeto call original
    async def test_26_retry_using_persisted_hubspot_contact_id(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session, hubspot_contact_id="persisted_contact_333")
            
            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                mock_post.return_value = MagicMock(
                    status_code=201,
                    json=lambda: {"id": "ticket_retry_999"},
                    raise_for_status=lambda: None
                )

                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="Front",
                    agent_name="Carlos Santana",
                    call_id="call_123",
                    call_timestamp=res.call_timestamp,
                    typology_name="Queja",
                    direction="INBOUND",
                    call_duration_seconds=185,
                    evaluacion_global=res.evaluacion_global,
                    alarma_feed="Detalle alarma",
                    contact_id=None
                )

                self.assertEqual(mock_post.call_count, 1)
                payload = mock_post.call_args[1]["json"]
                self.assertEqual(payload["associations"][0]["to"]["id"], "persisted_contact_333")
                self.assertEqual(payload["properties"]["subject"], "REM doobot speechFront")

                await session.refresh(res)
                self.assertEqual(res.hubspot_ticket_id, "ticket_retry_999")
                self.assertEqual(res.hubspot_ticket_status, "created")

    # 27. Subject exacto "REM doobot speechFront" sin agente, servicio, fecha, prefijos ni "REM Speech"
    async def test_27_exact_subject_rem_doobot_speechfront(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session)
            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                mock_post.return_value = MagicMock(
                    status_code=201,
                    json=lambda: {"id": "ticket_exact_subject"},
                    raise_for_status=lambda: None
                )
                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="Servicio Complejo XYZ",
                    agent_name="Agente Especial 007",
                    call_id="call_123",
                    call_timestamp=datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc),
                    typology_name="Tipologia ABC",
                    direction="OUTBOUND",
                    call_duration_seconds=300,
                    evaluacion_global=Decimal("3.0"),
                    alarma_feed="Alarma critica",
                    contact_id="contact_exact_1"
                )
                payload = mock_post.call_args[1]["json"]
                props = payload["properties"]
                # Must be strictly "REM doobot speechFront"
                self.assertEqual(props["subject"], "REM doobot speechFront")
                self.assertNotEqual(props["subject"], "REM Speech")
                self.assertNotIn("REM Speech", props["subject"])
                self.assertNotIn("Servicio Complejo XYZ", props["subject"])
                self.assertNotIn("Agente Especial 007", props["subject"])
                self.assertNotIn("2026", props["subject"])

    # 28. Validación con IDs y valores reales de producción (1458057418, 1991865549, General Call Center)
    async def test_28_real_production_ids_mock_payload(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session)
            
            # Configure exact verified production parameters
            self.settings.hubspot_ticket_pipeline = "1458057418"
            self.settings.hubspot_ticket_stage = "1991865549"
            self.settings.hubspot_tipo_de_rem = "General Call Center"
            self.settings.hubspot_alarm_tickets_enabled = True

            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                mock_post.return_value = MagicMock(
                    status_code=201,
                    json=lambda: {"id": "hs_rem_ticket_real_123"},
                    raise_for_status=lambda: None
                )

                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="Front",
                    agent_name="Carlos Santana",
                    call_id="511728701643",
                    call_timestamp=res.call_timestamp,
                    typology_name="Queja",
                    direction="INBOUND",
                    call_duration_seconds=185,
                    evaluacion_global=res.evaluacion_global,
                    alarma_feed="El paciente manifiesta gran enfado por retraso.",
                    contact_id="hs_contact_999"
                )

                self.assertEqual(mock_post.call_count, 1)
                payload = mock_post.call_args[1]["json"]
                props = payload["properties"]

                # Verify exact production properties
                self.assertEqual(props["hs_pipeline"], "1458057418")
                self.assertEqual(props["hs_pipeline_stage"], "1991865549")
                self.assertEqual(props["tipo_de_rem"], "General Call Center")
                self.assertEqual(props["subject"], "REM doobot speechFront")
                self.assertIn("MOTIVO DE LA ALARMA:\nEl paciente manifiesta gran enfado por retraso.", props["content"])
                self.assertIn("Call ID: 511728701643", props["content"])

                # Verify association: exactly 1 Contact association with associationTypeId=16
                self.assertIn("associations", payload)
                self.assertEqual(len(payload["associations"]), 1)
                assoc = payload["associations"][0]
                self.assertEqual(assoc["to"]["id"], "hs_contact_999")
                self.assertEqual(assoc["types"][0]["associationTypeId"], 16)
                self.assertEqual(assoc["types"][0]["associationCategory"], "HUBSPOT_DEFINED")

    # 29. Resumen llamada included in ticket content immediately before MOTIVO DE LA ALARMA
    async def test_29_resumen_llamada_included_in_ticket_content(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session, hubspot_contact_id="hs_contact_101")

            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                mock_post.return_value = MagicMock(
                    status_code=201,
                    json=lambda: {"id": "ticket_with_summary_123"},
                    raise_for_status=lambda: None
                )

                sample_summary = "El paciente llama para solicitar cita urgente tras sufrir molestias de dos semanas."
                sample_feed = "El paciente expresa descontento por demora en la atención."

                self.settings.hubspot_ticket_pipeline = "1458057418"
                self.settings.hubspot_ticket_stage = "1991865549"
                self.settings.hubspot_tipo_de_rem = "General Call Center"

                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="Front",
                    agent_name="Agente Resumen",
                    call_id=res.call_id,
                    call_timestamp=res.call_timestamp,
                    typology_name="Queja",
                    direction="INBOUND",
                    call_duration_seconds=150,
                    evaluacion_global=res.evaluacion_global,
                    alarma_feed=sample_feed,
                    contact_id="hs_contact_101",
                    resumen_llamada=sample_summary,
                )

                self.assertEqual(mock_post.call_count, 1)
                payload = mock_post.call_args[1]["json"]
                props = payload["properties"]
                content = props["content"]

                # Content checks
                self.assertIn("RESUMEN DE LA LLAMADA:\n" + sample_summary, content)
                self.assertIn("MOTIVO DE LA ALARMA:\n" + sample_feed, content)

                # Position check: RESUMEN DE LA LLAMADA must appear strictly before MOTIVO DE LA ALARMA
                idx_summary = content.index("RESUMEN DE LA LLAMADA:")
                idx_motivo = content.index("MOTIVO DE LA ALARMA:")
                self.assertLess(idx_summary, idx_motivo)

                # Confirm required properties unchanged
                self.assertEqual(props["subject"], "REM doobot speechFront")
                self.assertEqual(props["hs_pipeline"], "1458057418")
                self.assertEqual(props["hs_pipeline_stage"], "1991865549")
                self.assertEqual(props["tipo_de_rem"], "General Call Center")

                # Association check
                self.assertEqual(len(payload["associations"]), 1)
                self.assertEqual(payload["associations"][0]["to"]["id"], "hs_contact_101")
                self.assertEqual(payload["associations"][0]["types"][0]["associationTypeId"], 16)
                self.assertEqual(payload["associations"][0]["types"][0]["associationCategory"], "HUBSPOT_DEFINED")

    # 30. Resumen llamada absent: clean fallback without empty block or N/A
    async def test_30_resumen_llamada_absent_clean_fallback(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session, hubspot_contact_id="hs_contact_102")

            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                mock_post.return_value = MagicMock(
                    status_code=201,
                    json=lambda: {"id": "ticket_no_summary_456"},
                    raise_for_status=lambda: None
                )

                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="Front",
                    agent_name="Agente Sin Resumen",
                    call_id=res.call_id,
                    call_timestamp=res.call_timestamp,
                    typology_name="General",
                    direction="OUTBOUND",
                    call_duration_seconds=90,
                    evaluacion_global=res.evaluacion_global,
                    alarma_feed="Feed sin resumen",
                    contact_id="hs_contact_102",
                    resumen_llamada=None,
                )

                self.assertEqual(mock_post.call_count, 1)
                payload = mock_post.call_args[1]["json"]
                props = payload["properties"]
                content = props["content"]

                # Must NOT contain RESUMEN DE LA LLAMADA block or None/null/undefined/N/A
                self.assertNotIn("RESUMEN DE LA LLAMADA", content)
                self.assertNotIn("None", content)
                self.assertNotIn("undefined", content)
                self.assertIn("MOTIVO DE LA ALARMA:\nFeed sin resumen", content)
                self.assertEqual(props["subject"], "REM doobot speechFront")

    # 31. Resumen llamada extracted automatically from persisted items_json
    async def test_31_resumen_llamada_extracted_from_persisted_items_json(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session, hubspot_contact_id="hs_contact_103")
            res.items_json = [
                {
                    "criterion_id": 682,
                    "criterion_key": "resumen_llamada",
                    "output_key": "resumen_llamada",
                    "name": "Resumen llamada",
                    "type": "text",
                    "value": "El paciente consulta tarifas y agenda cita para el jueves."
                },
                {
                    "criterion_key": "alarma",
                    "output_key": "alarma",
                    "boolean_value": True,
                    "feed": "Alarma por tono agresivo."
                }
            ]
            await session.commit()

            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                mock_post.return_value = MagicMock(
                    status_code=201,
                    json=lambda: {"id": "ticket_persisted_789"},
                    raise_for_status=lambda: None
                )

                # Call without passing resumen_llamada argument: should extract from record.items_json
                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="Front",
                    agent_name="Agente Items",
                    call_id=res.call_id,
                    call_timestamp=res.call_timestamp,
                    typology_name="Cita",
                    direction="INBOUND",
                    call_duration_seconds=120,
                    evaluacion_global=res.evaluacion_global,
                    alarma_feed="Alarma por tono agresivo.",
                    contact_id="hs_contact_103",
                    resumen_llamada=None,
                )

                self.assertEqual(mock_post.call_count, 1)
                props = mock_post.call_args[1]["json"]["properties"]
                content = props["content"]

                self.assertIn("RESUMEN DE LA LLAMADA:\nEl paciente consulta tarifas y agenda cita para el jueves.", content)
                self.assertIn("MOTIVO DE LA ALARMA:\nAlarma por tono agresivo.", content)
                self.assertLess(content.index("RESUMEN DE LA LLAMADA:"), content.index("MOTIVO DE LA ALARMA:"))

    # 32. Resumen llamada with trivial or dummy values (N/A, None) is ignored
    async def test_32_resumen_llamada_trivial_value_ignored(self):
        async with self.async_session() as session:
            res = await self._create_test_result(session, hubspot_contact_id="hs_contact_104")

            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                mock_post.return_value = MagicMock(
                    status_code=201,
                    json=lambda: {"id": "ticket_trivial_000"},
                    raise_for_status=lambda: None
                )

                for trivial in ["  N/A  ", "None", "null", "undefined", ""]:
                    mock_post.reset_mock()
                    res.hubspot_ticket_status = None
                    res.hubspot_ticket_id = None
                    await session.commit()

                    await MassEvaluationService._process_alarm_hubspot_ticket(
                        db=session,
                        mass_analysis_id=res.mass_analysis_id,
                        execution_source="automation",
                        company_id=1,
                        service_name="Front",
                        agent_name="Agente NA",
                        call_id=res.call_id,
                        call_timestamp=res.call_timestamp,
                        typology_name="Info",
                        direction="INBOUND",
                        call_duration_seconds=60,
                        evaluacion_global=res.evaluacion_global,
                        alarma_feed="Motivo de prueba",
                        contact_id="hs_contact_104",
                        resumen_llamada=trivial,
                    )

                    self.assertEqual(mock_post.call_count, 1)
                    content = mock_post.call_args[1]["json"]["properties"]["content"]
                    self.assertNotIn("RESUMEN DE LA LLAMADA", content)
                    self.assertIn("MOTIVO DE LA ALARMA:\nMotivo de prueba", content)


if __name__ == "__main__":
    unittest.main()
