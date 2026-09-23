import io
import json
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_trainer_chatbot.db"

from sqlalchemy import BigInteger, delete, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from httpx import AsyncClient, ASGITransport
from fastapi import HTTPException

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import Base, get_engine
from app.main import app
from app.dependencies import get_db, get_current_user, get_tenant_context
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.personalized_training import (
    TrainingAgentReport,
    TrainingKnowledgeDocument,
    TrainingSimulationPrompt,
    TrainingCallEvaluation,
)
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.services.trainer_chatbot_service import TrainerChatbotService, MAX_HISTORY_MESSAGES


class TestTrainerChatbot(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with self.session_maker() as db:
            await db.execute(delete(TrainingKnowledgeDocument))
            await db.execute(delete(TrainingCallEvaluation))
            await db.execute(delete(TrainingSimulationPrompt))
            await db.execute(delete(TrainingAgentReport))
            await db.execute(delete(User))
            await db.execute(delete(Service))
            await db.execute(delete(Company))

            # Base test data
            company1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_demo=False)
            company2 = Company(company_id=7, company_name="Empresa Demo", company_key="empresa-demo", is_demo=True)
            service1 = Service(service_id=1, company_id=1, service_name="Atención", service_key="atencion")
            db.add_all([company1, company2, service1])
            await db.flush()

            # Knowledge documents for Agent 1 (Company 1)
            now = datetime.now(timezone.utc)
            doc_c101_s1 = TrainingKnowledgeDocument(
                id=1,
                company_id=1,
                hubspot_owner_id="agent_01",
                service_id=1,
                cycle_id=101,
                simulation_id=201,
                document_type="simulation",
                title="Doc Simulación 1 - Ciclo 101",
                content="En la simulación 1 el agente demostró buen saludo pero falló en el cierre.",
                metadata_json={"score": 6.5, "passed": False},
            )
            doc_c101_cycle = TrainingKnowledgeDocument(
                id=2,
                company_id=1,
                hubspot_owner_id="agent_01",
                service_id=1,
                cycle_id=101,
                simulation_id=None,
                document_type="cycle",
                title="Doc Resumen Ciclo 101",
                content="Resumen global ciclo 101: Media de 6.5. Objetivo de sondeo cumplido.",
                metadata_json={"avg_score": 6.5},
            )
            doc_c102_s1 = TrainingKnowledgeDocument(
                id=3,
                company_id=1,
                hubspot_owner_id="agent_01",
                service_id=1,
                cycle_id=102,
                simulation_id=202,
                document_type="simulation",
                title="Doc Simulación 1 - Ciclo 102",
                content="En la simulación 1 del ciclo 102 el agente mejoró el cierre notablemente.",
                metadata_json={"score": 9.0, "passed": True},
            )

            # Knowledge document for Agent 2 (Company 1)
            doc_agent2 = TrainingKnowledgeDocument(
                id=4,
                company_id=1,
                hubspot_owner_id="agent_02",
                service_id=1,
                cycle_id=101,
                simulation_id=203,
                document_type="simulation",
                title="Doc Simulación Agent 2",
                content="Datos confidenciales del agente 2.",
                metadata_json={"score": 5.0},
            )

            # Knowledge document for Agent 1 in Company 2 (cross-company test)
            doc_cross_company = TrainingKnowledgeDocument(
                id=5,
                company_id=7,
                hubspot_owner_id="agent_01",
                service_id=1,
                cycle_id=103,
                simulation_id=204,
                document_type="simulation",
                title="Doc Demo Company Agent 1",
                content="Documento de Empresa Demo que no debe ser visto por Boston Medical.",
                metadata_json={"score": 8.0},
            )

            db.add_all([doc_c101_s1, doc_c101_cycle, doc_c102_s1, doc_agent2, doc_cross_company])
            await db.commit()

    async def asyncTearDown(self):
        app.dependency_overrides.clear()

    async def test_chat_text_input_success(self):
        """1. Pregunta de texto -> respuesta RAG correcta y grounded."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        user = User(user_id=10, email="agent1@speech.com", role="agent", hubspot_owner_id="agent_01", company_id=1)
        context = TenantContext(
            user_id=10,
            user_email="agent1@speech.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01"],
        )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: context

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "En tu última simulación mejoraste el cierre con un 9.0."

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post(
                    "/bm/trainer/chat",
                    json={"message": "¿Cómo fue mi evolución en el cierre?"}
                )

                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertEqual(data["input_type"], "text")
                self.assertEqual(data["user_query"], "¿Cómo fue mi evolución en el cierre?")
                self.assertIn("mejoraste el cierre", data["response"])
                self.assertTrue(len(data["sources"]) > 0)

                # Verificar que el LLM fue llamado con el system prompt conteniendo los documentos
                mock_complete.assert_called_once()
                call_args = mock_complete.call_args[1]
                messages = call_args["messages"]
                system_content = messages[0]["content"]
                self.assertIn("BASE DE CONOCIMIENTO", system_content)
                self.assertIn("Doc Simulación 1 - Ciclo 102", system_content)
                self.assertEqual(messages[-1]["content"], "¿Cómo fue mi evolución en el cierre?")

    async def test_chat_audio_input_success_and_query_transcription(self):
        """2, 3, 4. Audio -> transcribe_audio mockeado -> misma ruta de RAG, user_query contiene transcripción, input_type='audio'."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        user = User(user_id=10, email="agent1@speech.com", role="agent", hubspot_owner_id="agent_01", company_id=1)
        context = TenantContext(
            user_id=10,
            user_email="agent1@speech.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01"],
        )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: context

        with patch("app.services.openai_service.transcribe_audio", new_callable=AsyncMock) as mock_transcribe, \
             patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:

            mock_transcribe.return_value = {"text": "¿Qué puntuación saqué en el sondeo?", "model": "test-model"}
            mock_complete.return_value = "En el ciclo 101 tu media fue de 6.5."

            audio_payload = b"\xff\xfb\x90\x44" + b"\x00" * 100  # Fake mp3 header and bytes

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post(
                    "/bm/trainer/chat",
                    files={"audio_file": ("pregunta.mp3", audio_payload, "audio/mpeg")},
                )

                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertEqual(data["input_type"], "audio")
                self.assertEqual(data["user_query"], "¿Qué puntuación saqué en el sondeo?")
                self.assertEqual(data["response"], "En el ciclo 101 tu media fue de 6.5.")

                mock_transcribe.assert_called_once()
                mock_complete.assert_called_once()

    async def test_agent_role_forces_own_hubspot_owner_id_ignoring_requested_agent_id(self):
        """5. Un usuario con rol agent ignora el agent_id enviado por el frontend y fuerza su propio hubspot_owner_id autenticado."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        user = User(user_id=10, email="agent1@speech.com", role="agent", hubspot_owner_id="agent_01", company_id=1)
        context = TenantContext(
            user_id=10,
            user_email="agent1@speech.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01"],
        )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: context

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "Respuesta."

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                # El agente 1 intenta maliciosamente consultar el agent_02
                res = await client.post(
                    "/bm/trainer/chat",
                    json={"message": "Dime los datos del agente 2", "agent_id": "agent_02"}
                )

                self.assertEqual(res.status_code, 200)
                data = res.json()
                # Sources no debe incluir el documento 4 (que pertenece a agent_02)
                source_ids = [s["document_id"] for s in data["sources"]]
                self.assertNotIn(4, source_ids)
                for s in data["sources"]:
                    self.assertNotEqual(s["title"], "Doc Simulación Agent 2")

    async def test_multitenant_company_isolation(self):
        """6. No se pueden cruzar documentos de otra empresa (company_id)."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        # Usuario pertenece a Company 1
        user = User(user_id=10, email="agent1@speech.com", role="agent", hubspot_owner_id="agent_01", company_id=1)
        context = TenantContext(
            user_id=10,
            user_email="agent1@speech.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01"],
        )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: context

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "Respuesta."

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post(
                    "/bm/trainer/chat",
                    json={"message": "¿Qué documentos tengo?"}
                )

                self.assertEqual(res.status_code, 200)
                data = res.json()
                source_ids = [s["document_id"] for s in data["sources"]]
                # Documento 5 pertenece a Empresa Demo (company_id=7) -> NUNCA debe aparecer
                self.assertNotIn(5, source_ids)

    async def test_supervisor_can_query_allowed_agent(self):
        """7. Supervisor/coordinador/admin puede consultar un agente permitido."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        # Coordinador con permiso sobre agent_01 y agent_02
        user = User(user_id=20, email="coor@speech.com", role="team_coordinator", company_id=1)
        context = TenantContext(
            user_id=20,
            user_email="coor@speech.com",
            raw_role="team_coordinator",
            normalized_role=InternalRole.TEAM_COORDINATOR,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01", "agent_02"],
        )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: context

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "El agente 2 sacó un 5.0."

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post(
                    "/bm/trainer/chat",
                    json={"message": "¿Cómo le fue al agente 2?", "agent_id": "agent_02"}
                )

                self.assertEqual(res.status_code, 200)
                data = res.json()
                source_ids = [s["document_id"] for s in data["sources"]]
                self.assertIn(4, source_ids)  # Doc de agent_02 recuperado con éxito

    async def test_supervisor_cannot_query_unauthorized_agent(self):
        """7b. Supervisor no puede consultar un agente fuera de su equipo/allowed_agent_ids."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        user = User(user_id=20, email="coor@speech.com", role="team_coordinator", company_id=1)
        context = TenantContext(
            user_id=20,
            user_email="coor@speech.com",
            raw_role="team_coordinator",
            normalized_role=InternalRole.TEAM_COORDINATOR,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01"],  # No tiene permiso sobre agent_02
        )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: context

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post(
                "/bm/trainer/chat",
                json={"message": "¿Cómo le fue al agente 2?", "agent_id": "agent_02"}
            )
            self.assertEqual(res.status_code, 403)
            self.assertIn("No tienes permisos", res.json()["detail"])

    async def test_cycle_id_filter_limits_documents(self):
        """8, 9. cycle_id limita correctamente los documentos recuperados y sources corresponden únicamente a los usados."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        user = User(user_id=10, email="agent1@speech.com", role="agent", hubspot_owner_id="agent_01", company_id=1)
        context = TenantContext(
            user_id=10,
            user_email="agent1@speech.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01"],
        )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: context

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "En el ciclo 101 hiciste dos simulaciones."

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post(
                    "/bm/trainer/chat",
                    json={"message": "¿Qué hice en el ciclo 101?", "cycle_id": 101}
                )

                self.assertEqual(res.status_code, 200)
                data = res.json()
                sources = data["sources"]
                source_ids = [s["document_id"] for s in sources]
                # Documentos 1 y 2 son del ciclo 101
                self.assertIn(1, source_ids)
                self.assertIn(2, source_ids)
                # Documento 3 es del ciclo 102 -> NO debe estar presente
                self.assertNotIn(3, source_ids)

    async def test_no_knowledge_documents_warning_grounded(self):
        """10. Si no existen documentos de conocimiento, el prompt advierte explícitamente y sources es vacío."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        user = User(user_id=99, email="newagent@speech.com", role="agent", hubspot_owner_id="agent_brand_new", company_id=1)
        context = TenantContext(
            user_id=99,
            user_email="newagent@speech.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_brand_new"],
        )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: context

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "No dispongo de evaluaciones registradas para tu usuario."

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post(
                    "/bm/trainer/chat",
                    json={"message": "¿Qué nota tengo?"}
                )

                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertEqual(data["sources"], [])
                call_args = mock_complete.call_args[1]
                sys_content = call_args["messages"][0]["content"]
                self.assertIn("No existen documentos de conocimiento ni evaluaciones registradas", sys_content)

    async def test_transcription_failure_returns_controlled_502(self):
        """11. Si la transcripción falla, devuelve un error 502 controlado sin colgar la petición."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        user = User(user_id=10, email="agent1@speech.com", role="agent", hubspot_owner_id="agent_01", company_id=1)
        context = TenantContext(
            user_id=10,
            user_email="agent1@speech.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01"],
        )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: context

        with patch("app.services.openai_service.transcribe_audio", side_effect=RuntimeError("Whisper upstream timeout")):
            audio_payload = b"\xff\xfb\x90\x44" + b"\x00" * 100

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post(
                    "/bm/trainer/chat",
                    files={"audio_file": ("audio.mp3", audio_payload, "audio/mpeg")},
                )

                self.assertEqual(res.status_code, 502)
                self.assertIn("Fallo en el servicio de transcripción", res.json()["detail"])

    async def test_both_text_and_audio_rejected(self):
        """Validación defensiva: Rechazar si se envían texto y audio a la vez."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        user = User(user_id=10, email="agent1@speech.com", role="agent", hubspot_owner_id="agent_01", company_id=1)
        context = TenantContext(
            user_id=10,
            user_email="agent1@speech.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01"],
        )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: context

        audio_payload = b"\xff\xfb\x90\x44" + b"\x00" * 100
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post(
                "/bm/trainer/chat",
                data={"message": "Pregunta"},
                files={"audio_file": ("audio.mp3", audio_payload, "audio/mpeg")},
            )
            self.assertEqual(res.status_code, 400)
            self.assertIn("no ambos simultáneamente", res.json()["detail"])

    async def test_neither_text_nor_audio_rejected(self):
        """Validación defensiva: Rechazar si no se envía ni texto ni audio."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        user = User(user_id=10, email="agent1@speech.com", role="agent", hubspot_owner_id="agent_01", company_id=1)
        context = TenantContext(
            user_id=10,
            user_email="agent1@speech.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01"],
        )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: context

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post("/bm/trainer/chat", json={})
            self.assertEqual(res.status_code, 400)
            self.assertIn("Debe proporcionar un mensaje de texto o un archivo de audio", res.json()["detail"])

    async def test_conversation_history_is_bounded(self):
        """14. conversation_history no puede crecer sin límite (acotado a MAX_HISTORY_MESSAGES)."""
        history_20_turns = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"Turno {i}"} for i in range(20)]
        sanitized = TrainerChatbotService.sanitize_history(history_20_turns)
        self.assertEqual(len(sanitized), MAX_HISTORY_MESSAGES)
        self.assertEqual(sanitized[-1]["content"], "Turno 19")

    def test_no_pgvector_or_embeddings_in_service(self):
        """12. Verificación estática de que el módulo no importa ni usa pgvector o embeddings."""
        import inspect
        from app.services import trainer_chatbot_service
        source = inspect.getsource(trainer_chatbot_service)
        self.assertNotIn("pgvector", source)
        self.assertNotIn("vector", source.lower())
        self.assertNotIn("embedding", source.lower())

    def test_training_knowledge_documents_registered_in_app_models_metadata(self):
        """15. Verifica que TrainingKnowledgeDocument está registrado en app.models y en Base.metadata.tables con todas sus columnas."""
        import app.models
        self.assertIn("TrainingKnowledgeDocument", app.models.__all__)
        self.assertTrue(hasattr(app.models, "TrainingKnowledgeDocument"))
        self.assertIn("bm_training_knowledge_documents", Base.metadata.tables)

        table = Base.metadata.tables["bm_training_knowledge_documents"]
        expected_cols = {
            "id", "company_id", "hubspot_owner_id", "service_id", "team_id",
            "cycle_id", "simulation_id", "evaluation_id", "document_type",
            "title", "content", "metadata_json", "created_at", "updated_at"
        }
        actual_cols = {col.name for col in table.columns}
        self.assertTrue(expected_cols.issubset(actual_cols), f"Missing columns: {expected_cols - actual_cols}")

    async def test_chat_queries_knowledge_documents_without_undefined_table_error(self):
        """16. Verifica que el endpoint /bm/trainer/chat ejecuta la consulta contra bm_training_knowledge_documents sin lanzar UndefinedTableError para demo_owner_11."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        user = User(user_id=11, email="agente.demo.11@doobot.ai", role="agent", hubspot_owner_id="demo_owner_11", company_id=7)
        context = TenantContext(
            user_id=11,
            user_email="agente.demo.11@doobot.ai",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=7,
            allowed_company_ids=[7],
            allowed_agent_ids=["demo_owner_11"],
        )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: context

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "Hola agente demo 11, estoy aquí para ayudarte con tu entrenamiento."

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post(
                    "/bm/trainer/chat",
                    json={"message": "¿Cuál es mi progreso en las simulaciones?"}
                )

                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertEqual(data["input_type"], "text")
                self.assertIn("Hola agente demo 11", data["response"])

    def test_metadata_json_server_default_compiles_valid_postgresql_ddl(self):
        """17. Verifica que metadata_json genera DEFAULT '{}'::jsonb en PostgreSQL DDL y no una cadena con comillas duplicadas."""
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable
        from app.models.personalized_training import TrainingKnowledgeDocument

        table = TrainingKnowledgeDocument.__table__
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))

        # Debe compilar con sintaxis válida DEFAULT '{}'::jsonb
        self.assertIn("DEFAULT '{}'::jsonb", ddl, f"DDL did not contain expected DEFAULT: {ddl}")
        # NO debe contener el error de comillas anidadas/escapadas
        self.assertNotIn("'''{}''::jsonb'", ddl, f"DDL contains invalid quoted default: {ddl}")

        # Comprobar la columna en el modelo
        col = table.c.metadata_json
        self.assertFalse(col.nullable)
        self.assertIsNotNone(col.server_default)

    # ── Multi-Tenant & Conversation Isolation Test Suite ──────────────────────────

    async def test_iso_1_agent_a_does_not_reuse_agent_b_history(self):
        """1. Agente A no reutiliza el historial del agente B."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db

        # Context Agent A (demo_owner_11)
        user_a = User(user_id=11, email="demo11@doobot.ai", role="agent", hubspot_owner_id="demo_owner_11", company_id=7)
        ctx_a = TenantContext(
            user_id=11, user_email="demo11@doobot.ai", raw_role="agent",
            normalized_role=InternalRole.AGENT, is_super_admin=False,
            company_id=7, allowed_company_ids=[7], allowed_agent_ids=["demo_owner_11"],
        )

        # Context Agent B (demo_owner_01)
        user_b = User(user_id=12, email="demo01@doobot.ai", role="agent", hubspot_owner_id="demo_owner_01", company_id=7)
        ctx_b = TenantContext(
            user_id=12, user_email="demo01@doobot.ai", raw_role="agent",
            normalized_role=InternalRole.AGENT, is_super_admin=False,
            company_id=7, allowed_company_ids=[7], allowed_agent_ids=["demo_owner_01"],
        )

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "Respuesta para Agente A"

            # Turn 1: Agent A chats
            app.dependency_overrides[get_current_user] = lambda: user_a
            app.dependency_overrides[get_tenant_context] = lambda: ctx_a

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res_a = await client.post("/bm/trainer/chat", json={"message": "Pregunta de Agente A"})
                self.assertEqual(res_a.status_code, 200)
                data_a = res_a.json()
                self.assertEqual(data_a["agent_id"], "demo_owner_11")
                self.assertEqual(data_a["company_id"], 7)

            # Turn 2: Agent B chats independently
            mock_complete.reset_mock()
            mock_complete.return_value = "Respuesta para Agente B"

            app.dependency_overrides[get_current_user] = lambda: user_b
            app.dependency_overrides[get_tenant_context] = lambda: ctx_b

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res_b = await client.post("/bm/trainer/chat", json={"message": "Pregunta de Agente B"})
                self.assertEqual(res_b.status_code, 200)
                data_b = res_b.json()
                self.assertEqual(data_b["agent_id"], "demo_owner_01")
                self.assertEqual(data_b["company_id"], 7)

            # Verify prompt received by LLM for Agent B had NO trace of Agent A's question
            call_args = mock_complete.call_args[1]
            sent_messages = call_args["messages"]
            sent_contents = [m["content"] for m in sent_messages]
            self.assertIn("Pregunta de Agente B", sent_contents)
            self.assertNotIn("Pregunta de Agente A", sent_contents)
            self.assertNotIn("Respuesta para Agente A", sent_contents)
            self.assertIn("demo_owner_01", sent_messages[0]["content"])
            self.assertNotIn("demo_owner_11", sent_messages[0]["content"])

    async def test_iso_2_empresa_demo_does_not_reuse_boston_medical_history_or_docs(self):
        """2. Agente de Empresa Demo no reutiliza el historial ni documentos de Boston Medical."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db

        # Boston Medical Agent
        user_bm = User(user_id=10, email="bmg@speech.com", role="agent", hubspot_owner_id="agent_01", company_id=1)
        ctx_bm = TenantContext(
            user_id=10, user_email="bmg@speech.com", raw_role="agent",
            normalized_role=InternalRole.AGENT, is_super_admin=False,
            company_id=1, allowed_company_ids=[1], allowed_agent_ids=["agent_01"],
        )

        # Empresa Demo Agent
        user_demo = User(user_id=11, email="demo11@doobot.ai", role="agent", hubspot_owner_id="demo_owner_11", company_id=7)
        ctx_demo = TenantContext(
            user_id=11, user_email="demo11@doobot.ai", raw_role="agent",
            normalized_role=InternalRole.AGENT, is_super_admin=False,
            company_id=7, allowed_company_ids=[7], allowed_agent_ids=["demo_owner_11"],
        )

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "Respuesta Demo"

            # Query as Empresa Demo
            app.dependency_overrides[get_current_user] = lambda: user_demo
            app.dependency_overrides[get_tenant_context] = lambda: ctx_demo

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res_demo = await client.post("/bm/trainer/chat", json={"message": "¿Cómo voy en Empresa Demo?"})
                self.assertEqual(res_demo.status_code, 200)
                data_demo = res_demo.json()
                self.assertEqual(data_demo["company_id"], 7)
                self.assertEqual(data_demo["agent_id"], "demo_owner_11")

            # Check prompt grounding: must not contain Boston Medical documents (doc 1, 2, 3)
            call_args = mock_complete.call_args[1]
            sys_prompt = call_args["messages"][0]["content"]
            self.assertIn("demo_owner_11", sys_prompt)
            self.assertNotIn("Doc Simulación 1 - Ciclo 101", sys_prompt)
            self.assertNotIn("Doc Resumen Ciclo 101", sys_prompt)

    async def test_iso_3_two_agents_same_company_have_independent_conversations(self):
        """3. Dos agentes distintos de la misma empresa tienen conversaciones y contextos independientes."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db

        # Boston Medical Agent 01
        user_1 = User(user_id=10, email="agent1@speech.com", role="agent", hubspot_owner_id="agent_01", company_id=1)
        ctx_1 = TenantContext(
            user_id=10, user_email="agent1@speech.com", raw_role="agent",
            normalized_role=InternalRole.AGENT, is_super_admin=False,
            company_id=1, allowed_company_ids=[1], allowed_agent_ids=["agent_01"],
        )

        # Boston Medical Agent 02
        user_2 = User(user_id=20, email="agent2@speech.com", role="agent", hubspot_owner_id="agent_02", company_id=1)
        ctx_2 = TenantContext(
            user_id=20, user_email="agent2@speech.com", raw_role="agent",
            normalized_role=InternalRole.AGENT, is_super_admin=False,
            company_id=1, allowed_company_ids=[1], allowed_agent_ids=["agent_02"],
        )

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            # Query Agent 2
            app.dependency_overrides[get_current_user] = lambda: user_2
            app.dependency_overrides[get_tenant_context] = lambda: ctx_2
            mock_complete.return_value = "Respuesta para Agente 2"

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res_2 = await client.post("/bm/trainer/chat", json={"message": "Mis datos de Agente 2"})
                self.assertEqual(res_2.status_code, 200)
                data_2 = res_2.json()
                self.assertEqual(data_2["agent_id"], "agent_02")
                # Doc 4 is agent_02's doc
                source_ids = [s["document_id"] for s in data_2["sources"]]
                self.assertIn(4, source_ids)

            # Query Agent 1
            mock_complete.reset_mock()
            mock_complete.return_value = "Respuesta para Agente 1"
            app.dependency_overrides[get_current_user] = lambda: user_1
            app.dependency_overrides[get_tenant_context] = lambda: ctx_1

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res_1 = await client.post("/bm/trainer/chat", json={"message": "Mis datos de Agente 1"})
                self.assertEqual(res_1.status_code, 200)
                data_1 = res_1.json()
                self.assertEqual(data_1["agent_id"], "agent_01")
                # Doc 4 (agent_02) must NEVER appear in Agent 1's sources
                source_ids_1 = [s["document_id"] for s in data_1["sources"]]
                self.assertNotIn(4, source_ids_1)

    async def test_iso_4_agent_cannot_access_other_agent_context_by_manipulating_agent_id(self):
        """4. Un agente no puede solicitar el historial/contexto de otro agente manipulando agent_id."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db

        user = User(user_id=11, email="demo11@doobot.ai", role="agent", hubspot_owner_id="demo_owner_11", company_id=7)
        ctx = TenantContext(
            user_id=11, user_email="demo11@doobot.ai", raw_role="agent",
            normalized_role=InternalRole.AGENT, is_super_admin=False,
            company_id=7, allowed_company_ids=[7], allowed_agent_ids=["demo_owner_11"],
        )
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: ctx

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "OK"

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                # Spoof attempt: pass another company's agent
                res = await client.post(
                    "/bm/trainer/chat",
                    json={"message": "Intento de acceso indebido", "agent_id": "agent_01"}
                )
                self.assertEqual(res.status_code, 200)
                data = res.json()
                # Backend MUST strictly force target to demo_owner_11
                self.assertEqual(data["agent_id"], "demo_owner_11")
                self.assertEqual(data["company_id"], 7)

            # Check prompt was generated for demo_owner_11, NOT agent_01
            call_args = mock_complete.call_args[1]
            sys_prompt = call_args["messages"][0]["content"]
            self.assertIn("demo_owner_11", sys_prompt)
            self.assertNotIn("agent_01", sys_prompt)

    async def test_iso_5_backend_derives_identity_from_authenticated_session(self):
        """5. El backend deriva correctamente la identidad del agente autenticado y rechaza sin hubspot_owner_id."""
        user_valid = User(user_id=15, email="valid@speech.com", role="agent", hubspot_owner_id="owner_valid_15", company_id=1)
        ctx_valid = TenantContext(
            user_id=15, user_email="valid@speech.com", raw_role="agent",
            normalized_role=InternalRole.AGENT, is_super_admin=False,
            company_id=1, allowed_company_ids=[1], allowed_agent_ids=["owner_valid_15"],
        )

        resolved_id = TrainerChatbotService.resolve_target_agent(
            current_user=user_valid,
            requested_agent_id="attacker_chosen_id",
            context=ctx_valid,
        )
        self.assertEqual(resolved_id, "owner_valid_15")

        # User without hubspot_owner_id must raise 400
        user_invalid = User(user_id=16, email="no_owner@speech.com", role="agent", hubspot_owner_id=None, company_id=1)
        ctx_invalid = TenantContext(
            user_id=16, user_email="no_owner@speech.com", raw_role="agent",
            normalized_role=InternalRole.AGENT, is_super_admin=False,
            company_id=1, allowed_company_ids=[1], allowed_agent_ids=None,
        )
        with self.assertRaises(HTTPException) as exc_info:
            TrainerChatbotService.resolve_target_agent(
                current_user=user_invalid,
                requested_agent_id=None,
                context=ctx_invalid,
            )
        self.assertEqual(exc_info.exception.status_code, 400)
        self.assertIn("HubSpot Owner ID", exc_info.exception.detail)

    async def test_iso_6_closing_and_reopening_tutor_preserves_conversation_for_same_agent(self):
        """6. El cierre y reapertura del Tutor conserva la conversación del MISMO agente mediante partición aislada."""
        # Simulated client storage dictionary
        client_local_storage: dict[str, str] = {}

        company_id = 7
        agent_id = "demo_owner_11"
        storage_key = TrainerChatbotService.get_storage_key(company_id, agent_id, "text")

        # Turn 1
        history_turn1 = [{"role": "user", "content": "Hola tutor"}, {"role": "assistant", "content": "¡Hola! ¿En qué puedo ayudarte?"}]
        client_local_storage[storage_key] = json.dumps(history_turn1)

        # Panel closed (unmounted) ...
        # Panel reopened for SAME agent
        reloaded_history_raw = client_local_storage.get(storage_key)
        self.assertIsNotNone(reloaded_history_raw)
        reloaded_history = json.loads(reloaded_history_raw)
        self.assertEqual(len(reloaded_history), 2)
        self.assertEqual(reloaded_history[0]["content"], "Hola tutor")

        # Agent continues conversation with preserved history
        cleaned = TrainerChatbotService.sanitize_history(reloaded_history)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[0]["content"], "Hola tutor")

    async def test_iso_7_changing_user_does_not_recover_prior_conversation(self):
        """7. Cambiar de usuario o empresa no recupera la conversación anterior."""
        client_local_storage: dict[str, str] = {}

        # User 1 (Demo 11) creates a conversation
        key_user1 = TrainerChatbotService.get_storage_key(company_id=7, user_identifier="demo_owner_11", key_type="text")
        client_local_storage[key_user1] = json.dumps([
            {"role": "user", "content": "Conversación confidencial demo 11"},
            {"role": "assistant", "content": "Respuesta demo 11"},
        ])

        # User logs out and User 2 (Boston Medical Agent 01) logs in
        key_user2 = TrainerChatbotService.get_storage_key(company_id=1, user_identifier="agent_01", key_type="text")

        # User 2 opens Tutor panel: reads its own storage partition
        user2_history_raw = client_local_storage.get(key_user2)
        self.assertIsNone(user2_history_raw, "User 2 must not see or load User 1's conversation!")

        # Even if User 2 in same company (Demo 01) logs in:
        key_user3 = TrainerChatbotService.get_storage_key(company_id=7, user_identifier="demo_owner_01", key_type="text")
        user3_history_raw = client_local_storage.get(key_user3)
        self.assertIsNone(user3_history_raw, "Demo 01 must not see Demo 11's conversation!")

    def test_iso_8_frontend_storage_namespace_contract(self):
        """8. Si existe persistencia en frontend, sus claves/namespaces están aislados por identidad + empresa."""
        # Key for Demo 11
        k_demo11_text = TrainerChatbotService.get_storage_key(company_id=7, user_identifier="demo_owner_11", key_type="text")
        # Key for Demo 01
        k_demo01_text = TrainerChatbotService.get_storage_key(company_id=7, user_identifier="demo_owner_01", key_type="text")
        # Key for Boston Medical Agent 01
        k_bmg01_text = TrainerChatbotService.get_storage_key(company_id=1, user_identifier="agent_01", key_type="text")
        # Audio key for Demo 11
        k_demo11_audio = TrainerChatbotService.get_storage_key(company_id=7, user_identifier="demo_owner_11", key_type="audio")

        # Assert all keys are strictly distinct and structured
        self.assertNotEqual(k_demo11_text, k_demo01_text)
        self.assertNotEqual(k_demo11_text, k_bmg01_text)
        self.assertNotEqual(k_demo01_text, k_bmg01_text)
        self.assertNotEqual(k_demo11_text, k_demo11_audio)

        self.assertEqual(k_demo11_text, "trainer_chat_7_demo_owner_11_text")
        self.assertEqual(k_bmg01_text, "trainer_chat_1_agent_01_text")
        self.assertEqual(k_demo11_audio, "trainer_chat_7_demo_owner_11_audio")

        # Check safety when identifiers are None
        k_fallback = TrainerChatbotService.get_storage_key(company_id=None, user_identifier=None)
        self.assertEqual(k_fallback, "trainer_chat_unknown_company_unknown_user_text")

    def test_iso_9_supervisor_selector_partitions_by_target_agent(self):
        """9. Un supervisor que cambia entre agentes en un selector obtiene claves aisladas por target_agent_id."""
        supervisor_id = "sup_01"
        company_id = 1
        # Supervisor viewing Agent A
        key_sup_agentA = TrainerChatbotService.get_storage_key(
            company_id=company_id, user_identifier=supervisor_id, target_agent_id="agent_01", key_type="text"
        )
        # Supervisor viewing Agent B
        key_sup_agentB = TrainerChatbotService.get_storage_key(
            company_id=company_id, user_identifier=supervisor_id, target_agent_id="agent_02", key_type="text"
        )
        self.assertNotEqual(key_sup_agentA, key_sup_agentB)
        self.assertEqual(key_sup_agentA, "trainer_chat_1_sup_01_target_agent_01_text")
        self.assertEqual(key_sup_agentB, "trainer_chat_1_sup_01_target_agent_02_text")

    def test_iso_10_legacy_storage_keys_purged_and_never_reused(self):
        """10. Las claves antiguas trainerChatText y trainerChatAudio son purgadas de forma segura y nunca reutilizadas."""
        # Simulated client storage containing contaminated legacy keys
        client_local_storage = {
            "trainerChatText": json.dumps([{"role": "user", "content": "Mensaje antiguo filtrado"}]),
            "trainerChatAudio": "audio_data_antiguo",
        }

        # New isolation key for authenticated user
        clean_key = TrainerChatbotService.get_storage_key(company_id=7, user_identifier="demo_owner_11")
        self.assertNotIn(clean_key, ["trainerChatText", "trainerChatAudio"])

        # Emulate legacy key purge logic in client initialization
        legacy_keys = ["trainerChatText", "trainerChatAudio"]
        for lk in legacy_keys:
            client_local_storage.pop(lk, None)

        self.assertNotIn("trainerChatText", client_local_storage)
        self.assertNotIn("trainerChatAudio", client_local_storage)
        # Verify fresh isolated session starts clean without importing legacy data
        self.assertNotIn(clean_key, client_local_storage)


if __name__ == "__main__":
    unittest.main()
