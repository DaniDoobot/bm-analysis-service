import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_trainer_chat_persistence.db"

from sqlalchemy import BigInteger, delete, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
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
from app.models.trainer_chat import TrainerChat, TrainerChatMessage
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.services.trainer_chat_persistence_service import TrainerChatPersistenceService


class TestTrainerChatPersistence(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with self.session_maker() as db:
            await db.execute(delete(TrainerChatMessage))
            await db.execute(delete(TrainerChat))
            await db.execute(delete(User))
            await db.execute(delete(Service))
            await db.execute(delete(Company))

            c1 = Company(company_id=1, company_name="Empresa 1", company_key="empresa-1", is_demo=False)
            c2 = Company(company_id=2, company_name="Empresa 2", company_key="empresa-2", is_demo=False)
            s1 = Service(service_id=1, company_id=1, service_name="Atención", service_key="atencion")
            db.add_all([c1, c2, s1])
            await db.flush()

            # User 1: Agent in Company 1
            self.user_agent_1 = User(
                user_id=10,
                username="agent1",
                email="agent1@empresa1.com",
                password_hash="hash",
                role="AGENT",
                hubspot_owner_id="agent_01",
                company_id=1,
            )
            # User 2: Agent in Company 1
            self.user_agent_2 = User(
                user_id=20,
                username="agent2",
                email="agent2@empresa1.com",
                password_hash="hash",
                role="AGENT",
                hubspot_owner_id="agent_02",
                company_id=1,
            )
            # User 3: Supervisor in Company 1
            self.user_sup = User(
                user_id=30,
                username="supervisor",
                email="sup@empresa1.com",
                password_hash="hash",
                role="COMPANY_ADMIN",
                hubspot_owner_id="sup_01",
                company_id=1,
            )
            # User 4: Agent in Company 2
            self.user_c2 = User(
                user_id=40,
                username="agent_c2",
                email="agent@empresa2.com",
                password_hash="hash",
                role="AGENT",
                hubspot_owner_id="agent_c2",
                company_id=2,
            )
            db.add_all([self.user_agent_1, self.user_agent_2, self.user_sup, self.user_c2])
            await db.commit()

        self.ctx_agent_1 = TenantContext(
            user_id=10,
            user_email="agent1@empresa1.com",
            raw_role="AGENT",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01"],
        )
        self.ctx_agent_2 = TenantContext(
            user_id=20,
            user_email="agent2@empresa1.com",
            raw_role="AGENT",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_02"],
        )
        self.ctx_sup = TenantContext(
            user_id=30,
            user_email="sup@empresa1.com",
            raw_role="COMPANY_ADMIN",
            normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01", "agent_02"],
        )
        self.ctx_c2 = TenantContext(
            user_id=40,
            user_email="agent@empresa2.com",
            raw_role="AGENT",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=2,
            allowed_company_ids=[2],
            allowed_agent_ids=["agent_c2"],
        )

    def _set_auth(self, user: User, ctx: TenantContext):
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: ctx

    async def asyncTearDown(self):
        app.dependency_overrides.clear()

    # ── 1. Create chat: default title and ownership ───────────────────────────
    async def test_01_create_chat_default_title_and_ownership(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post("/bm/trainer/chats", json={})
            self.assertEqual(res.status_code, 201)
            data = res.json()
            self.assertEqual(data["title"], "Nueva conversación")
            self.assertEqual(data["target_agent_id"], "agent_01")
            self.assertFalse(data["is_archived"])
            self.assertEqual(data["messages"], [])
            self.assertIn("chat_id", data)

    # ── 2. Create chat: custom title ──────────────────────────────────────────
    async def test_02_create_chat_custom_title(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post("/bm/trainer/chats", json={"title": "Manejo de objeciones de precio"})
            self.assertEqual(res.status_code, 201)
            data = res.json()
            self.assertEqual(data["title"], "Manejo de objeciones de precio")
            self.assertEqual(data["target_agent_id"], "agent_01")

    # ── 3. Create chat: agent own identity explicitly passed ──────────────────
    async def test_03_create_chat_agent_own_identity(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post("/bm/trainer/chats", json={"target_agent_id": "agent_01"})
            self.assertEqual(res.status_code, 201)
            data = res.json()
            self.assertEqual(data["target_agent_id"], "agent_01")

    # ── 4. Create chat: agent cannot specify another agent ────────────────────
    async def test_04_create_chat_agent_forbidden_other_agent(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post("/bm/trainer/chats", json={"target_agent_id": "agent_02"})
            self.assertEqual(res.status_code, 403)
            self.assertIn("permisos", res.json()["detail"].lower())

    # ── 5. Create chat: supervisor with allowed agent ─────────────────────────
    async def test_05_create_chat_supervisor_allowed_agent(self):
        self._set_auth(self.user_sup, self.ctx_sup)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post("/bm/trainer/chats", json={"target_agent_id": "agent_02", "title": "Revisión Agent 2"})
            self.assertEqual(res.status_code, 201)
            data = res.json()
            self.assertEqual(data["target_agent_id"], "agent_02")
            self.assertEqual(data["title"], "Revisión Agent 2")

    # ── 6. Create chat: supervisor forbidden for unauthorized agent ───────────
    async def test_06_create_chat_supervisor_forbidden_unauthorized_agent(self):
        self._set_auth(self.user_sup, self.ctx_sup)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post("/bm/trainer/chats", json={"target_agent_id": "agent_99"})
            self.assertEqual(res.status_code, 403)

    # ── 7. List chats: user isolation within same company ─────────────────────
    async def test_07_list_chats_user_isolation(self):
        # Create 2 chats as Agent 1
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/bm/trainer/chats", json={"title": "Chat 1 Agent 1"})
            await client.post("/bm/trainer/chats", json={"title": "Chat 2 Agent 1"})

        # Create 1 chat as Agent 2
        self._set_auth(self.user_agent_2, self.ctx_agent_2)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/bm/trainer/chats", json={"title": "Chat 1 Agent 2"})

        # Agent 1 lists chats: should see exactly 2 chats
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/bm/trainer/chats")
            self.assertEqual(res.status_code, 200)
            items = res.json()
            self.assertEqual(len(items), 2)
            titles = [c["title"] for c in items]
            self.assertIn("Chat 1 Agent 1", titles)
            self.assertIn("Chat 2 Agent 1", titles)
            self.assertNotIn("Chat 1 Agent 2", titles)

    # ── 8. List chats: company isolation ──────────────────────────────────────
    async def test_08_list_chats_company_isolation(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/bm/trainer/chats", json={"title": "Company 1 Chat"})

        # Company 2 Agent lists chats: should see 0 chats
        self._set_auth(self.user_c2, self.ctx_c2)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/bm/trainer/chats")
            self.assertEqual(res.status_code, 200)
            items = res.json()
            self.assertEqual(len(items), 0)

    # ── 9. List chats: target_agent_id filter ─────────────────────────────────
    async def test_09_list_chats_target_agent_filter(self):
        self._set_auth(self.user_sup, self.ctx_sup)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/bm/trainer/chats", json={"target_agent_id": "agent_01", "title": "Sup chat agent 1"})
            await client.post("/bm/trainer/chats", json={"target_agent_id": "agent_02", "title": "Sup chat agent 2"})

            # Filter by agent_01
            res1 = await client.get("/bm/trainer/chats?target_agent_id=agent_01")
            self.assertEqual(res1.status_code, 200)
            items1 = res1.json()
            self.assertEqual(len(items1), 1)
            self.assertEqual(items1[0]["title"], "Sup chat agent 1")

    # ── 10. List chats: exclude archived by default ───────────────────────────
    async def test_10_list_chats_exclude_archived_by_default(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            c1 = (await client.post("/bm/trainer/chats", json={"title": "Active Chat"})).json()
            c2 = (await client.post("/bm/trainer/chats", json={"title": "To Archive"})).json()
            await client.patch(f"/bm/trainer/chats/{c2['chat_id']}", json={"is_archived": True})

            res = await client.get("/bm/trainer/chats")
            self.assertEqual(res.status_code, 200)
            items = res.json()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "Active Chat")

    # ── 11. List chats: include archived when requested ───────────────────────
    async def test_11_list_chats_include_archived(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            c1 = (await client.post("/bm/trainer/chats", json={"title": "Active Chat"})).json()
            c2 = (await client.post("/bm/trainer/chats", json={"title": "To Archive"})).json()
            await client.patch(f"/bm/trainer/chats/{c2['chat_id']}", json={"is_archived": True})

            res = await client.get("/bm/trainer/chats?include_archived=true")
            self.assertEqual(res.status_code, 200)
            items = res.json()
            self.assertEqual(len(items), 2)

    # ── 12. List chats: message_count calculation ─────────────────────────────
    async def test_12_list_chats_message_count_accuracy(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            chat = (await client.post("/bm/trainer/chats", json={"title": "Chat with msgs"})).json()
            chat_id = chat["chat_id"]

        # Insert 3 messages directly into DB
        async with self.session_maker() as db:
            m1 = TrainerChatMessage(chat_id=chat_id, role="user", content="Hola", input_type="text")
            m2 = TrainerChatMessage(chat_id=chat_id, role="assistant", content="Hola, ¿en qué puedo ayudarte?", input_type="text")
            m3 = TrainerChatMessage(chat_id=chat_id, role="user", content="Dame feedback", input_type="text")
            db.add_all([m1, m2, m3])
            await db.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/bm/trainer/chats")
            self.assertEqual(res.status_code, 200)
            items = res.json()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["message_count"], 3)

    # ── 13. Get chat detail: messages in chronological order ───────────────────
    async def test_13_get_chat_detail_with_messages_chronological(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            chat = (await client.post("/bm/trainer/chats", json={"title": "Chat detail"})).json()
            chat_id = chat["chat_id"]

        async with self.session_maker() as db:
            m1 = TrainerChatMessage(chat_id=chat_id, role="user", content="M1", input_type="text")
            m2 = TrainerChatMessage(chat_id=chat_id, role="assistant", content="M2", input_type="text")
            db.add_all([m1, m2])
            await db.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get(f"/bm/trainer/chats/{chat_id}")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["chat_id"], chat_id)
            self.assertEqual(len(data["messages"]), 2)
            self.assertEqual(data["messages"][0]["content"], "M1")
            self.assertEqual(data["messages"][0]["role"], "user")
            self.assertEqual(data["messages"][1]["content"], "M2")
            self.assertEqual(data["messages"][1]["role"], "assistant")

    # ── 14. Get chat: foreign user in same company returns 404 ────────────────
    async def test_14_get_chat_foreign_user_same_company_returns_404(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            chat = (await client.post("/bm/trainer/chats", json={"title": "Agent 1 Private"})).json()
            chat_id = chat["chat_id"]

        # Agent 2 attempts to get Agent 1's chat
        self._set_auth(self.user_agent_2, self.ctx_agent_2)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get(f"/bm/trainer/chats/{chat_id}")
            self.assertEqual(res.status_code, 404)
            self.assertIn("no encontrada", res.json()["detail"].lower())

    # ── 15. Get chat: foreign company returns 404 ─────────────────────────────
    async def test_15_get_chat_foreign_company_returns_404(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            chat = (await client.post("/bm/trainer/chats", json={"title": "Company 1 Only"})).json()
            chat_id = chat["chat_id"]

        # Company 2 Agent attempts to get Company 1 chat
        self._set_auth(self.user_c2, self.ctx_c2)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get(f"/bm/trainer/chats/{chat_id}")
            self.assertEqual(res.status_code, 404)

    # ── 16. Update chat: patch title and is_archived ──────────────────────────
    async def test_16_update_chat_title_and_archive(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            chat = (await client.post("/bm/trainer/chats", json={"title": "Old Title"})).json()
            chat_id = chat["chat_id"]

            res = await client.patch(f"/bm/trainer/chats/{chat_id}", json={"title": "New Title", "is_archived": True})
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["title"], "New Title")
            self.assertTrue(data["is_archived"])

    # ── 17. Update chat: foreign user returns 404 ─────────────────────────────
    async def test_17_update_chat_foreign_user_returns_404(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            chat = (await client.post("/bm/trainer/chats", json={"title": "Agent 1"})).json()
            chat_id = chat["chat_id"]

        self._set_auth(self.user_agent_2, self.ctx_agent_2)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.patch(f"/bm/trainer/chats/{chat_id}", json={"title": "Hacked Title"})
            self.assertEqual(res.status_code, 404)

    # ── 18. Delete chat: soft delete ──────────────────────────────────────────
    async def test_18_delete_chat_soft_delete(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            chat = (await client.post("/bm/trainer/chats", json={"title": "To Delete"})).json()
            chat_id = chat["chat_id"]

            res = await client.delete(f"/bm/trainer/chats/{chat_id}")
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["ok"])

        # Check DB directly: row still exists, is_active is False
        async with self.session_maker() as db:
            db_chat = await db.get(TrainerChat, chat_id)
            self.assertIsNotNone(db_chat)
            self.assertFalse(db_chat.is_active)

    # ── 19. Delete chat: subsequent access returns 404 and excluded from list ──
    async def test_19_delete_chat_subsequent_access_returns_404(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            chat = (await client.post("/bm/trainer/chats", json={"title": "Deleted"})).json()
            chat_id = chat["chat_id"]
            await client.delete(f"/bm/trainer/chats/{chat_id}")

            # Subsequent GET returns 404
            res_get = await client.get(f"/bm/trainer/chats/{chat_id}")
            self.assertEqual(res_get.status_code, 404)

            # Subsequent list excludes it
            res_list = await client.get("/bm/trainer/chats")
            self.assertEqual(res_list.status_code, 200)
            self.assertEqual(len(res_list.json()), 0)

    # ── 20. Delete chat: foreign user returns 404 ─────────────────────────────
    async def test_20_delete_chat_foreign_user_returns_404(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            chat = (await client.post("/bm/trainer/chats", json={"title": "Agent 1"})).json()
            chat_id = chat["chat_id"]

        self._set_auth(self.user_agent_2, self.ctx_agent_2)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.delete(f"/bm/trainer/chats/{chat_id}")
            self.assertEqual(res.status_code, 404)

    # ── 21. Process chat: persists user and assistant messages, updates chat ─
    async def test_21_chat_process_with_chat_id_persists_user_and_assistant(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            chat = (await client.post("/bm/trainer/chats", json={"title": "Learning Session"})).json()
            chat_id = chat["chat_id"]

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "Hola, he analizado tu caso y lo hiciste bien."

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post(
                    "/bm/trainer/chat",
                    json={"chat_id": chat_id, "message": "¿Cómo puedo mejorar el sondeo?"}
                )
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertEqual(data["chat_id"], chat_id)
                self.assertIsNotNone(data["message_id"])
                self.assertIn("analizado tu caso", data["response"])

        # Check DB messages
        async with self.session_maker() as db:
            stmt = select(TrainerChatMessage).where(TrainerChatMessage.chat_id == chat_id).order_by(TrainerChatMessage.message_id.asc())
            res = await db.execute(stmt)
            msgs = list(res.scalars().all())
            self.assertEqual(len(msgs), 2)
            self.assertEqual(msgs[0].role, "user")
            self.assertEqual(msgs[0].content, "¿Cómo puedo mejorar el sondeo?")
            self.assertEqual(msgs[0].input_type, "text")
            self.assertEqual(msgs[1].role, "assistant")
            self.assertEqual(msgs[1].content, "Hola, he analizado tu caso y lo hiciste bien.")

    # ── 22. Process chat: sliding window of 8 messages sent to LLM ────────────
    async def test_22_chat_process_sliding_window_8_messages(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            chat = (await client.post("/bm/trainer/chats", json={"title": "Long Thread"})).json()
            chat_id = chat["chat_id"]

        # Insert 10 existing messages (5 turns: U1..A5)
        async with self.session_maker() as db:
            for i in range(1, 6):
                db.add(TrainerChatMessage(chat_id=chat_id, role="user", content=f"Pregunta {i}", input_type="text"))
                db.add(TrainerChatMessage(chat_id=chat_id, role="assistant", content=f"Respuesta {i}", input_type="text"))
            await db.commit()

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "Respuesta turno 6"

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post(
                    "/bm/trainer/chat",
                    json={"chat_id": chat_id, "message": "Pregunta 6"}
                )
                self.assertEqual(res.status_code, 200)

            # Check messages passed to complete_text
            call_args = mock_complete.call_args[1]
            sent_messages = call_args["messages"]
            # Structure: [system, history (max 8), user]
            # Total history messages = 8 (Pregunta 2 .. Respuesta 5)
            # U1 and A1 should be excluded by sliding window
            history_msgs = sent_messages[1:-1]
            self.assertEqual(len(history_msgs), 8)
            self.assertEqual(history_msgs[0]["content"], "Pregunta 2")
            self.assertEqual(history_msgs[-1]["content"], "Respuesta 5")
            self.assertEqual(sent_messages[-1]["content"], "Pregunta 6")

        # In DB, all 12 messages must exist
        async with self.session_maker() as db:
            stmt = select(TrainerChatMessage).where(TrainerChatMessage.chat_id == chat_id)
            res = await db.execute(stmt)
            total_msgs = len(list(res.scalars().all()))
            self.assertEqual(total_msgs, 12)

    # ── 23. Process chat: audio input persists input_type='audio' ──────────────
    async def test_23_chat_process_audio_input_persists_audio_type(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            chat = (await client.post("/bm/trainer/chats", json={"title": "Voice chat"})).json()
            chat_id = chat["chat_id"]

        with patch("app.services.openai_service.transcribe_audio", new_callable=AsyncMock) as mock_transcribe, \
             patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_transcribe.return_value = {"text": "Pregunta grabada por micro"}
            mock_complete.return_value = "Respuesta a la pregunta grabada"

            audio_payload = b"\xff\xfb\x90\x44" + b"\x00" * 50

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post(
                    "/bm/trainer/chat",
                    data={"chat_id": str(chat_id)},
                    files={"audio_file": ("test.mp3", audio_payload, "audio/mpeg")},
                )
                self.assertEqual(res.status_code, 200)

        async with self.session_maker() as db:
            stmt = select(TrainerChatMessage).where(TrainerChatMessage.chat_id == chat_id).order_by(TrainerChatMessage.message_id.asc())
            res = await db.execute(stmt)
            msgs = list(res.scalars().all())
            self.assertEqual(len(msgs), 2)
            self.assertEqual(msgs[0].role, "user")
            self.assertEqual(msgs[0].content, "Pregunta grabada por micro")
            self.assertEqual(msgs[0].input_type, "audio")
            self.assertEqual(msgs[1].role, "assistant")
            self.assertEqual(msgs[1].input_type, "text")

    # ── 24. Process chat: LLM failure keeps user message, no assistant msg ────
    async def test_24_chat_process_llm_failure_keeps_user_msg_without_assistant(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            chat = (await client.post("/bm/trainer/chats", json={"title": "Crash test"})).json()
            chat_id = chat["chat_id"]

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.side_effect = RuntimeError("OpenAI connection timed out")

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post(
                    "/bm/trainer/chat",
                    json={"chat_id": chat_id, "message": "Esta pregunta fallará en el modelo"}
                )
                self.assertEqual(res.status_code, 502)

        # Check DB: user message was persisted, assistant message was NOT
        async with self.session_maker() as db:
            stmt = select(TrainerChatMessage).where(TrainerChatMessage.chat_id == chat_id)
            res = await db.execute(stmt)
            msgs = list(res.scalars().all())
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0].role, "user")
            self.assertEqual(msgs[0].content, "Esta pregunta fallará en el modelo")

    # ── 25. Process chat: target_agent mismatch returns 400 ───────────────────
    async def test_25_chat_process_target_agent_mismatch_returns_400(self):
        self._set_auth(self.user_sup, self.ctx_sup)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            chat = (await client.post("/bm/trainer/chats", json={"target_agent_id": "agent_01", "title": "Chat Agent 1"})).json()
            chat_id = chat["chat_id"]

            # Try to send a message to Chat 1 while passing agent_id="agent_02"
            res = await client.post(
                "/bm/trainer/chat",
                json={"chat_id": chat_id, "agent_id": "agent_02", "message": "¿Hola?"}
            )
            self.assertEqual(res.status_code, 400)
            self.assertIn("no coincide", res.json()["detail"].lower())

    # ── 26. Process chat: stateless backward compatibility (chat_id omitted) ──
    async def test_26_chat_process_stateless_backward_compatibility(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "Respuesta sin persistencia"

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post(
                    "/bm/trainer/chat",
                    json={
                        "message": "Consulta stateless",
                        "conversation_history": [
                            {"role": "user", "content": "Prev user"},
                            {"role": "assistant", "content": "Prev assistant"}
                        ]
                    }
                )
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertIsNone(data["chat_id"])
                self.assertIsNone(data["message_id"])

        # No messages should have been created in DB
        async with self.session_maker() as db:
            stmt = select(TrainerChatMessage)
            res = await db.execute(stmt)
            self.assertEqual(len(list(res.scalars().all())), 0)

    # ── 27. Process chat: early returns persisted when chat_id is present ─────
    async def test_27_chat_process_early_returns_persisted(self):
        self._set_auth(self.user_agent_1, self.ctx_agent_1)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            chat = (await client.post("/bm/trainer/chats", json={"title": "Early Return Thread"})).json()
            chat_id = chat["chat_id"]

            # Request specific non-existent call (Flow B early return)
            res = await client.post(
                "/bm/trainer/chat",
                json={"chat_id": chat_id, "message": "Detalle de la llamada 99999"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["chat_id"], chat_id)
            self.assertIsNotNone(data["message_id"])
            self.assertIn("No he localizado la llamada", data["response"])

        # Verify DB contains both user question and the tutor's not-found answer
        async with self.session_maker() as db:
            stmt = select(TrainerChatMessage).where(TrainerChatMessage.chat_id == chat_id).order_by(TrainerChatMessage.message_id.asc())
            res = await db.execute(stmt)
            msgs = list(res.scalars().all())
            self.assertEqual(len(msgs), 2)
            self.assertEqual(msgs[0].role, "user")
            self.assertEqual(msgs[1].role, "assistant")
            self.assertIn("No he localizado la llamada", msgs[1].content)
