"""
Verification test suite for user name and full_name alias and extra fields validation.
Includes 16 test cases covering creation, update, conflict, normalization, and auditing.
"""
import sys
import os
import asyncio
from datetime import datetime, timezone

# Ensure app is importable and production bypass is active for tests
os.environ["ALLOW_PRODUCTION_TESTS"] = "true"
os.environ["APP_ENV"] = "test"
sys.path.insert(0, os.path.abspath("."))

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.db import get_engine
from app.models.users import User, UserAudit
from app.models.personalized_training import TrainingAgentSetting
from app.utils.security import create_access_token, hash_password

async def test_user_name_alias_workflow():
    print("=== INICIANDO PRUEBAS DE ALIAS DE NOMBRE Y PREVENCIÓN DE CAMPOS DESCONOCIDOS ===")
    
    engine = get_engine()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        # Cleanup
        await db.execute(delete(UserAudit))
        await db.execute(delete(User).where(User.username.in_([
            "alias_admin", "test_name_user", "test_fullname_user", "test_conflict_user", "test_extra_user"
        ])))
        await db.execute(delete(User).where(User.email.in_([
            "alias_admin@boston.es", "test_name_user@boston.es", "test_fullname_user@boston.es",
            "test_conflict_user@boston.es", "test_extra_user@boston.es"
        ])))
        await db.execute(delete(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id.in_(["88888801", "88888802"])))
        await db.commit()

        # Seed HubSpot agent settings
        agent_setting_1 = TrainingAgentSetting(
            hubspot_owner_id="88888801",
            agent_name="Agente Pruebas 1",
            agent_initials="P1",
            is_enabled=True
        )
        agent_setting_2 = TrainingAgentSetting(
            hubspot_owner_id="88888802",
            agent_name="Agente Pruebas 2",
            agent_initials="P2",
            is_enabled=True
        )
        db.add_all([agent_setting_1, agent_setting_2])
        await db.commit()

        # Seed Admin user for API authentication
        admin_user = User(
            username="alias_admin",
            email="alias_admin@boston.es",
            role="administrador",
            password_hash=hash_password("adminpass123"),
            is_active=True
        )
        db.add(admin_user)
        await db.commit()
        await db.refresh(admin_user)

        # Generate Token
        token_admin = create_access_token({
            "user_id": admin_user.user_id,
            "username": admin_user.username,
            "email": admin_user.email
        })

        import httpx
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {token_admin}"}

            # --- CASO 1: Crear usuario enviando 'name' ---
            print("1. Crear usuario enviando 'name'...")
            res1 = await client.post("/bm/users", json={
                "email": "test_name_user@boston.es",
                "username": "test_name_user",
                "name": "   Luis Garcia   ",  # includes outer spaces for TAREA 13 validation
                "role": "agente",
                "password": "userpass123",
                "hubspot_owner_id": "88888801"
            }, headers=headers, params={"allow_unverified_hubspot_id": "true"})
            assert res1.status_code == 201, f"Failed to create user with 'name': {res1.text}"
            res1_json = res1.json()
            assert res1_json["ok"] is True
            # Confirm outer spaces normalized (TAREA 13)
            assert res1_json["user"]["name"] == "Luis Garcia"
            print("   Pass: Usuario creado con 'name' y espacios limpios.")
            
            # --- CASO 5 & 6: Confirmar persistencia en db y retorno en GET ---
            print("5 & 6. Confirmar persistencia de 'name' en DB y GET...")
            created_user_id = res1_json["user"]["id"]
            res_get = await client.get(f"/bm/users/{created_user_id}", headers=headers)
            assert res_get.status_code == 200
            assert res_get.json()["name"] == "Luis Garcia"
            print("   Pass: 'name' persistido y recuperado en GET.")

            # --- CASO 2: Crear usuario enviando 'full_name' ---
            print("2. Crear usuario enviando 'full_name'...")
            res2 = await client.post("/bm/users", json={
                "email": "test_fullname_user@boston.es",
                "username": "test_fullname_user",
                "full_name": "Luci Dos Santos Furtado",
                "role": "agente",
                "password": "userpass123",
                "hubspot_owner_id": "88888802"
            }, headers=headers, params={"allow_unverified_hubspot_id": "true"})
            assert res2.status_code == 201, f"Failed to create user with 'full_name': {res2.text}"
            res2_json = res2.json()
            assert res2_json["user"]["name"] == "Luci Dos Santos Furtado"
            print("   Pass: Usuario creado con 'full_name' mapeando a 'name'.")
            
            # Confirm GET returns name
            created_fullname_user_id = res2_json["user"]["id"]
            res_get2 = await client.get(f"/bm/users/{created_fullname_user_id}", headers=headers)
            assert res_get2.json()["name"] == "Luci Dos Santos Furtado"
            print("   Pass: Mapeo de 'full_name' a 'name' confirmado en GET.")

            # --- CASO 3 & 7: Editar usuario enviando 'name' y retornar valor actualizado ---
            print("3 & 7. Editar usuario enviando 'name' y comprobar retorno inmediato...")
            res_patch = await client.patch(f"/bm/users/{created_user_id}", json={
                "name": "Luis Gomez"
            }, headers=headers, params={"allow_unverified_hubspot_id": "true"})
            assert res_patch.status_code == 200
            assert res_patch.json()["name"] == "Luis Gomez"
            print("   Pass: Editado con 'name' y valor retornado inmediatamente.")

            # --- CASO 4: Editar usuario enviando 'full_name' ---
            print("4. Editar usuario enviando 'full_name'...")
            res_patch2 = await client.patch(f"/bm/users/{created_user_id}", json={
                "full_name": "Luci Dos Santos"
            }, headers=headers, params={"allow_unverified_hubspot_id": "true"})
            assert res_patch2.status_code == 200
            assert res_patch2.json()["name"] == "Luci Dos Santos"
            print("   Pass: Editado con 'full_name' mapeando correctamente.")

            # --- CASO 8, 9, 10: Confirmar que editar el nombre no cambia username, email, ni HubSpot ID ---
            print("8, 9, 10. Confirmar que no cambian otros campos al editar nombre...")
            user_data = res_patch2.json()
            assert user_data["username"] == "test_name_user"
            assert user_data["email"] == "test_name_user@boston.es"
            assert user_data["hubspot_owner_id"] == "88888801"
            print("   Pass: username, email y HubSpot ID permanecen intactos.")

            # --- CASO 11: Rechazar 'name' y 'full_name' con valores diferentes ---
            print("11. Rechazar 'name' y 'full_name' en conflicto...")
            res_conflict = await client.patch(f"/bm/users/{created_user_id}", json={
                "name": "Luis Gomez",
                "full_name": "Luci Dos Santos"
            }, headers=headers)
            assert res_conflict.status_code == 422
            assert "valores diferentes" in res_conflict.text
            print("   Pass: Conflicto de campos rechazado correctamente con 422.")

            # --- CASO 12: Aceptar ambos cuando sean iguales ---
            print("12. Aceptar ambos cuando sean iguales...")
            res_same = await client.patch(f"/bm/users/{created_user_id}", json={
                "name": "Luci Dos Santos",
                "full_name": "Luci Dos Santos"
            }, headers=headers)
            assert res_same.status_code == 200
            assert res_same.json()["name"] == "Luci Dos Santos"
            print("   Pass: Valores idénticos aceptados sin conflicto.")

            # --- CASO 13: Normalizar espacios exteriores ---
            print("13. Normalizar espacios exteriores al editar...")
            res_spaces = await client.patch(f"/bm/users/{created_user_id}", json={
                "full_name": "    Luci Dos Santos Furtado    "
            }, headers=headers)
            assert res_spaces.status_code == 200
            assert res_spaces.json()["name"] == "Luci Dos Santos Furtado"
            print("   Pass: Espacios exteriores recortados (strip).")

            # --- CASO 14: Comprobar el comportamiento con cadena vacía ---
            print("14. Comportamiento con cadena vacía (mapea a null)...")
            res_empty = await client.patch(f"/bm/users/{created_user_id}", json={
                "full_name": "     "
            }, headers=headers)
            assert res_empty.status_code == 200
            assert res_empty.json()["name"] is None
            print("   Pass: Cadena vacía normalizada a null (None).")

            # --- CASO 15: Rechazar un campo desconocido con 422 ---
            print("15. Rechazar campo desconocido con 422 (extra = forbid)...")
            res_unknown = await client.patch(f"/bm/users/{created_user_id}", json={
                "invalid_field": "some value"
            }, headers=headers)
            assert res_unknown.status_code == 422
            assert "extra fields not permitted" in res_unknown.text or "extra" in res_unknown.text
            print("   Pass: Campo desconocido rechazado con 422.")

            # --- CASO 16: Confirmar que se registra la auditoría ---
            print("16. Confirmar registro de auditoría...")
            # Perform a name change to check audit
            await client.patch(f"/bm/users/{created_user_id}", json={
                "full_name": "Audited Name"
            }, headers=headers)
            
            # Fetch audits
            stmt_audit = select(UserAudit).where(UserAudit.target_user_id == created_user_id).order_by(UserAudit.audit_id.desc())
            db_res = await db.execute(stmt_audit)
            audits = db_res.scalars().all()
            assert len(audits) > 0, "No audits found"
            latest_audit = audits[0]
            assert latest_audit.action == "update"
            assert "name" in latest_audit.changes_json
            assert latest_audit.changes_json["name"]["new"] == "Audited Name"
            print("   Pass: Auditoría registrada correctamente en bm_user_audits.")

            # Cleanup
            await db.execute(delete(UserAudit))
            await db.execute(delete(User).where(User.username.in_([
                "alias_admin", "test_name_user", "test_fullname_user", "test_conflict_user", "test_extra_user"
            ])))
            await db.execute(delete(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id.in_(["88888801", "88888802"])))
            await db.commit()

        print("\n=== TODAS LAS PRUEBAS DE ALIAS DE NOMBRE PASARON CORRECTAMENTE ===")

if __name__ == "__main__":
    asyncio.run(test_user_name_alias_workflow())
