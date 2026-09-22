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


if __name__ == "__main__":
    unittest.main()
