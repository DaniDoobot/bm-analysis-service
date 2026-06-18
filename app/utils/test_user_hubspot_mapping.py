import sys
import os
import asyncio
from datetime import datetime, timezone

# Add workspace directory to path
sys.path.append(".")

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.db import get_engine
from app.models.users import User
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingAgentReport,
)
from app.utils.security import create_access_token, hash_password

async def test_user_hubspot_mapping_workflow():
    engine = get_engine()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        print("=== INICIANDO PRUEBAS DE VALIDACIÓN DE HUBSPOT OWNER ID ===")
        
        # 1. Limpieza inicial
        await db.execute(delete(User).where(User.username.in_(["test_admin_mapping", "test_agent_valid", "test_agent_invalid", "test_agent_dup", "test_user_to_agent", "new_admin_user", "agent_invalid", "test_agent_self"])))
        await db.execute(delete(User).where(User.email.in_(["new_admin@boston.es", "agent_valid@boston.es", "agent_invalid@boston.es", "test_user_to_agent@boston.es"])))
        await db.execute(delete(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id.in_(["99999901", "99999902"])))
        await db.execute(delete(TrainingAgentReport).where(TrainingAgentReport.hubspot_owner_id.in_(["99999901", "99999902"])))
        await db.commit()
        
        # 2. Registrar algunos agentes conocidos de prueba en el inventario
        setting_1 = TrainingAgentSetting(
            hubspot_owner_id="99999901",
            agent_name="Agente Conocido A",
            agent_initials="AA",
            is_enabled=True
        )
        setting_2 = TrainingAgentSetting(
            hubspot_owner_id="99999902",
            agent_name="Agente Conocido B",
            agent_initials="AB",
            is_enabled=True
        )
        db.add_all([setting_1, setting_2])
        await db.commit()
        
        # Crear usuario admin real para el token
        admin_user = User(
            username="test_admin_mapping",
            email="test_admin_mapping@boston.es",
            role="administrador",
            hubspot_owner_id=None,
            password_hash=hash_password("adminpass123"),
            is_active=True
        )
        # Crear usuario agente para probar la auto-edición
        agent_user = User(
            username="test_agent_self",
            email="test_agent_self@boston.es",
            role="agente",
            hubspot_owner_id="99999901",
            password_hash=hash_password("agentpass123"),
            is_active=True
        )
        db.add_all([admin_user, agent_user])
        await db.commit()
        await db.refresh(admin_user)
        await db.refresh(agent_user)
        
        token_admin = create_access_token({"user_id": admin_user.user_id, "username": admin_user.username})
        token_agent = create_access_token({"user_id": agent_user.user_id, "username": agent_user.username})
        
        import httpx
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            try:
                headers_admin = {"Authorization": f"Bearer {token_admin}"}
                headers_agent = {"Authorization": f"Bearer {token_agent}"}
                
                # Caso 1: Crear administrador sin HubSpot ID -> Debe tener éxito (201)
                payload_admin = {
                    "email": "new_admin@boston.es",
                    "username": "new_admin_user",
                    "password": "securepass123",
                    "role": "administrador",
                    "is_active": True,
                    "must_reset_password": False
                }
                res = await client.post("/bm/users", json=payload_admin, headers=headers_admin)
                assert res.status_code == 201, f"No se pudo crear admin: {res.text}"
                print("[+] Caso 1: Crear admin sin HubSpot ID exitoso.")
                
                # Caso 2: Crear agente con ID válido -> Debe tener éxito (201)
                payload_agent_valid = {
                    "email": "agent_valid@boston.es",
                    "name": "Agente Válido",
                    "password": "securepass123",
                    "role": "agente",
                    "is_active": True,
                    "hubspot_owner_id": "99999902",
                    "must_reset_password": False
                }
                res = await client.post("/bm/users", json=payload_agent_valid, headers=headers_admin)
                assert res.status_code == 201, f"No se pudo crear agente: {res.text}"
                data_agent_valid = res.json()["user"]
                # Debe haber mapeado name a username
                assert data_agent_valid["username"] == "Agente Válido"
                print("[+] Caso 2: Crear agente con ID válido exitoso (y mapeo de name a username).")
                
                # Caso 3: Rechazar agente sin ID -> Debe fallar con 422
                payload_agent_no_id = {
                    "email": "agent_no_id@boston.es",
                    "username": "agent_no_id",
                    "password": "securepass123",
                    "role": "agent",
                    "is_active": True,
                    "must_reset_password": False
                }
                res = await client.post("/bm/users", json=payload_agent_no_id, headers=headers_admin)
                assert res.status_code == 422, f"Se esperaba 422, obtenido: {res.status_code} - {res.text}"
                assert "El ID de HubSpot es obligatorio" in res.json()["detail"]
                print("[+] Caso 3: Rechazar agente sin ID exitoso.")
                
                # Caso 4: Rechazar ID inexistente -> Debe fallar con 400
                payload_agent_invalid = {
                    "email": "agent_invalid@boston.es",
                    "username": "agent_invalid",
                    "password": "securepass123",
                    "role": "agente",
                    "is_active": True,
                    "hubspot_owner_id": "99999999", # Inexistente
                    "must_reset_password": False
                }
                res = await client.post("/bm/users", json=payload_agent_invalid, headers=headers_admin)
                assert res.status_code == 400, f"Se esperaba 400, obtenido: {res.status_code} - {res.text}"
                assert "No existe ningún agente conocido" in res.json()["detail"]
                print("[+] Caso 4: Rechazar agente con ID inexistente exitoso.")
                
                # Caso 5: Crear agente con ID inexistente usando bypass allow_unverified_hubspot_id=true -> Debe tener éxito (201)
                res = await client.post("/bm/users?allow_unverified_hubspot_id=true", json=payload_agent_invalid, headers=headers_admin)
                assert res.status_code == 201, f"Se esperaba 201 con bypass, obtenido: {res.status_code} - {res.text}"
                print("[+] Caso 5: Crear agente con ID inexistente usando bypass exitoso.")
                
                # Caso 6: Rechazar ID duplicado -> Debe fallar con 409
                payload_agent_dup = {
                    "email": "agent_dup@boston.es",
                    "username": "agent_dup",
                    "password": "securepass123",
                    "role": "agente",
                    "is_active": True,
                    "hubspot_owner_id": "99999902", # Ya asignado al Caso 2
                    "must_reset_password": False
                }
                res = await client.post("/bm/users", json=payload_agent_dup, headers=headers_admin)
                assert res.status_code == 409, f"Se esperaba 409, obtenido: {res.status_code} - {res.text}"
                assert "Este agente de HubSpot ya está asignado" in res.json()["detail"]
                print("[+] Caso 6: Rechazar ID duplicado exitoso.")
                
                # Caso 7: Editar un usuario existente para asignarle un ID -> Debe tener éxito
                payload_user = {
                    "email": "test_user_to_agent@boston.es",
                    "username": "test_user_to_agent",
                    "password": "securepass123",
                    "role": "usuario",
                    "is_active": True,
                    "must_reset_password": False
                }
                res_create = await client.post("/bm/users", json=payload_user, headers=headers_admin)
                assert res_create.status_code == 201
                user_id_to_edit = res_create.json()["user"]["user_id"]
                
                payload_update_id = {
                    "hubspot_owner_id": "99999901"
                }
                res_update_dup = await client.patch(f"/bm/users/{user_id_to_edit}", json=payload_update_id, headers=headers_admin)
                assert res_update_dup.status_code == 409, f"Se esperaba 409 por duplicado, obtenido: {res_update_dup.status_code} - {res_update_dup.text}"
                print("[+] Caso 7a: Editar usuario con ID de HubSpot duplicado arroja 409 Conflict exitosamente.")

                # Cambiemos test_agent_self a rol usuario y liberamos 99999901
                payload_free = {
                    "role": "usuario",
                    "hubspot_owner_id": None
                }
                res_free = await client.patch(f"/bm/users/{agent_user.user_id}", json=payload_free, headers=headers_admin)
                assert res_free.status_code == 200, f"Error liberando: {res_free.text}"
                
                res_update_ok = await client.patch(f"/bm/users/{user_id_to_edit}", json=payload_update_id, headers=headers_admin)
                assert res_update_ok.status_code == 200, f"Error editando: {res_update_ok.text}"
                print("[+] Caso 7b: Editar usuario para asignarle ID de HubSpot libre exitoso.")
                
                # Caso 8: Cambiar un usuario a rol agente sin ID -> Debe fallar con 422
                res_clear = await client.patch(f"/bm/users/{user_id_to_edit}", json={"hubspot_owner_id": None}, headers=headers_admin)
                assert res_clear.status_code == 200
                
                payload_to_agent = {
                    "role": "agente"
                }
                res_change_fail = await client.patch(f"/bm/users/{user_id_to_edit}", json=payload_to_agent, headers=headers_admin)
                assert res_change_fail.status_code == 422, f"Se esperaba 422, obtenido: {res_change_fail.status_code} - {res_change_fail.text}"
                assert "El ID de HubSpot es obligatorio" in res_change_fail.json()["detail"]
                print("[+] Caso 8: Cambiar a rol agente sin ID de HubSpot arroja 422 exitosamente.")
                
                # Caso 9: Listar agentes disponibles -> Debe retornar la lista de agentes ordenados
                res_agents = await client.get("/bm/admin/hubspot-agents?available_only=true", headers=headers_admin)
                assert res_agents.status_code == 200, f"Error listando: {res_agents.text}"
                agents_list = res_agents.json()
                available_ids = [a["hubspot_owner_id"] for a in agents_list]
                assert "99999901" in available_ids
                assert "99999902" not in available_ids # Ya asignado a agent_valid
                
                # Comprobar ordenación por nombre:
                names = [a["agent_name"] for a in agents_list if a["agent_name"]]
                assert names == sorted(names, key=lambda x: x.lower()), "La lista de agentes no está correctamente ordenada por nombre!"
                print("[+] Caso 9: Listado de agentes disponibles y ordenación correctos.")
                
                # Caso 10: Proteger endpoints de administrador contra no-administradores
                res_unauth = await client.get("/bm/admin/hubspot-agents", headers=headers_agent)
                assert res_unauth.status_code == 403, f"Se esperaba 403, obtenido: {res_unauth.status_code}"
                print("[+] Caso 10: Acceso bloqueado para no-admins en endpoint administrativo exitoso.")
                
                # Caso 11: Impedir auto-edición del HubSpot Owner ID por un agente
                # Cambiemos test_agent_self de nuevo a agente con un ID
                res_reagent = await client.patch(f"/bm/users/{agent_user.user_id}", json={"role": "agente", "hubspot_owner_id": "99999901"}, headers=headers_admin)
                assert res_reagent.status_code == 200, f"Error restaurando agente: {res_reagent.text}"

                payload_self = {
                    "current_password": "agentpass123",
                    "hubspot_owner_id": "99999902"
                }
                res_self_edit = await client.patch("/bm/me", json=payload_self, headers=headers_agent)
                assert res_self_edit.status_code == 400, f"Se esperaba 400, obtenido: {res_self_edit.status_code} - {res_self_edit.text}"
                assert "No está permitido modificar tu propio HubSpot Owner ID" in res_self_edit.json()["detail"]
                print("[+] Caso 11: Impedir que el agente altere su propio ID en /bm/me exitoso.")
                
                print("=== ¡TODAS LAS PRUEBAS DE MAPPING DE HUBSPOT PASARON EXITOSAMENTE! ===")
                
            finally:
                # Limpieza final de datos de prueba
                print("Limpiando base de datos...")
                await db.execute(delete(User).where(User.username.in_(["test_admin_mapping", "test_agent_valid", "test_agent_invalid", "test_agent_dup", "test_user_to_agent", "test_agent_self", "new_admin_user", "agent_invalid"])))
                await db.execute(delete(User).where(User.email.in_(["new_admin@boston.es", "agent_valid@boston.es", "agent_invalid@boston.es", "test_user_to_agent@boston.es"])))
                await db.execute(delete(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id.in_(["99999901", "99999902"])))
                await db.execute(delete(TrainingAgentReport).where(TrainingAgentReport.hubspot_owner_id.in_(["99999901", "99999902"])))
                await db.commit()
                print("Base de datos limpia.")

if __name__ == "__main__":
    asyncio.run(test_user_hubspot_mapping_workflow())
