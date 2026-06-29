import sys
import os
import asyncio
from datetime import datetime, timezone

# Add workspace directory to path
sys.path.append(".")

from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.db import get_engine, enforce_destructive_safety
from app.models.users import User, UserAudit
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingAgentReport,
)
from app.utils.security import create_access_token, hash_password, verify_password

async def test_admin_user_edition_workflow():
    enforce_destructive_safety(is_test=True)
    engine = get_engine()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        print("=== INICIANDO PRUEBAS DE EDICIÓN ADMINISTRATIVA DE USUARIOS ===")
        
        # Cleanup
        await db.execute(delete(UserAudit))
        await db.execute(delete(User).where(User.username.in_([
            "test_admin_editor", "test_user_edited", "test_agent_user", "test_conflict_user"
        ])))
        await db.execute(delete(User).where(User.email.in_([
            "test_admin_editor@boston.es", "test_user_edited@boston.es", "test_agent_user@boston.es", "test_conflict_user@boston.es",
            "new_email_user@boston.es", "conflict_email@boston.es"
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

        # Seed Admin editor
        admin_user = User(
            username="test_admin_editor",
            email="test_admin_editor@boston.es",
            role="administrador",
            hubspot_owner_id=None,
            password_hash=hash_password("adminpass123"),
            is_active=True
        )
        # Seed test user to be edited
        test_user = User(
            username="test_user_edited",
            email="test_user_edited@boston.es",
            name="Nombre Original",
            role="usuario",
            hubspot_owner_id=None,
            password_hash=hash_password("userpass123"),
            is_active=True
        )
        # Seed another user for email conflict testing
        conflict_user = User(
            username="test_conflict_user",
            email="conflict_email@boston.es",
            role="usuario",
            hubspot_owner_id=None,
            password_hash=hash_password("conflictpass123"),
            is_active=True
        )
        db.add_all([admin_user, test_user, conflict_user])
        await db.commit()
        await db.refresh(admin_user)
        await db.refresh(test_user)
        await db.refresh(conflict_user)

        # Generate tokens
        token_admin = create_access_token({"user_id": admin_user.user_id, "username": admin_user.username, "email": admin_user.email})
        token_user = create_access_token({"user_id": test_user.user_id, "username": test_user.username, "email": test_user.email})

        import httpx
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            try:
                headers_admin = {"Authorization": f"Bearer {token_admin}"}
                headers_user = {"Authorization": f"Bearer {token_user}"}

                # 1. GET /bm/users/{user_id} - Verification detail before edit
                res_get = await client.get(f"/bm/users/{test_user.user_id}", headers=headers_admin)
                assert res_get.status_code == 200, f"Error getting user: {res_get.text}"
                data_get = res_get.json()
                assert data_get["email"] == "test_user_edited@boston.es"
                assert data_get["name"] == "Nombre Original"
                assert "password_masked" in data_get
                assert "password_hash" not in data_get
                print("[+] Caso 1: GET /bm/users/{user_id} devuelve datos sin secretos exitoso.")

                # 2. PATCH /bm/users/{user_id} - Cambiar correo y verificar que conserva ID e HubSpot ID
                payload_email = {
                    "email": "new_email_user@boston.es"
                }
                res_patch = await client.patch(f"/bm/users/{test_user.user_id}", json=payload_email, headers=headers_admin)
                assert res_patch.status_code == 200, f"Error updating email: {res_patch.text}"
                data_patch = res_patch.json()
                assert data_patch["user_id"] == test_user.user_id, "El ID debe ser el mismo"
                assert data_patch["email"] == "new_email_user@boston.es"
                print("[+] Caso 2: Cambiar correo manteniendo user_id exitoso.")

                # 3. Validar sesión antigua rechazada tras cambio de correo
                res_me = await client.get("/bm/me", headers=headers_user)
                assert res_me.status_code == 401, f"Se esperaba 401 debido al cambio de email, se obtuvo: {res_me.status_code}"
                print("[+] Caso 3: Sesión antigua invalidada tras cambio de correo exitoso.")

                # 4. Iniciar sesión con el nuevo correo exitosamente
                payload_login = {
                    "email": "new_email_user@boston.es",
                    "password": "userpass123"
                }
                res_login = await client.post("/bm/auth/login", json=payload_login)
                assert res_login.status_code == 200, f"Error en login con nuevo correo: {res_login.text}"
                new_token = res_login.json()["access_token"]
                headers_new_user = {"Authorization": f"Bearer {new_token}"}
                
                # Probar que /bm/me funciona con el nuevo token
                res_me_new = await client.get("/bm/me", headers=headers_new_user)
                assert res_me_new.status_code == 200, "Error en /bm/me con nuevo token"
                print("[+] Caso 4: Login exitoso con el correo nuevo.")

                # 5. Rechazar correo duplicado (insensible a mayúsculas/minúsculas)
                payload_dup = {
                    "email": "CONFLICT_email@boston.es"  # conflict_email@boston.es existe en conflict_user
                }
                res_dup = await client.patch(f"/bm/users/{test_user.user_id}", json=payload_dup, headers=headers_admin)
                assert res_dup.status_code == 409, f"Se esperaba 409 Conflict, se obtuvo: {res_dup.status_code} - {res_dup.text}"
                assert "Ya existe otro usuario con este correo electrónico." in res_dup.json()["detail"]
                print("[+] Caso 5: Rechazo de correo duplicado exitoso.")

                # 6. Cambiar nombre y username de forma independiente
                payload_name_uname = {
                    "name": "Nuevo Nombre",
                    "username": "nuevo_username"
                }
                res_name = await client.patch(f"/bm/users/{test_user.user_id}", json=payload_name_uname, headers=headers_admin)
                assert res_name.status_code == 200, f"Error al cambiar nombre/username: {res_name.text}"
                data_name = res_name.json()
                assert data_name["name"] == "Nuevo Nombre"
                assert data_name["username"] == "nuevo_username"
                print("[+] Caso 6: Cambiar nombre y username de forma independiente exitoso.")

                # 7. Cambiar de rol administrador/usuario a agente con ID válido
                payload_to_agent = {
                    "role": "agente",
                    "hubspot_owner_id": "88888801"
                }
                res_to_agent = await client.patch(f"/bm/users/{test_user.user_id}", json=payload_to_agent, headers=headers_admin)
                assert res_to_agent.status_code == 200, f"Error al cambiar a agente: {res_to_agent.text}"
                assert res_to_agent.json()["role"] == "agente"
                assert res_to_agent.json()["hubspot_owner_id"] == "88888801"
                print("[+] Caso 7: Cambiar de rol a agente con ID de HubSpot válido exitoso.")

                # 8. Rechazar el cambio a agente sin ID de HubSpot
                payload_to_agent_fail = {
                    "role": "agente",
                    "hubspot_owner_id": None
                }
                res_to_agent_fail = await client.patch(f"/bm/users/{test_user.user_id}", json=payload_to_agent_fail, headers=headers_admin)
                assert res_to_agent_fail.status_code == 422, f"Se esperaba 422, se obtuvo: {res_to_agent_fail.status_code}"
                print("[+] Caso 8: Rechazar cambio a agente sin ID exitoso.")

                # 9. Cambiar de agente a otro rol (ej. administrador) liberando automáticamente el ID
                payload_to_admin = {
                    "role": "administrador"
                }
                res_to_admin = await client.patch(f"/bm/users/{test_user.user_id}", json=payload_to_admin, headers=headers_admin)
                assert res_to_admin.status_code == 200, f"Error al cambiar a admin: {res_to_admin.text}"
                data_to_admin = res_to_admin.json()
                assert data_to_admin["role"] == "administrador"
                assert data_to_admin["hubspot_owner_id"] is None, "El ID de HubSpot debe haberse liberado a null"
                print("[+] Caso 9: Cambiar de agente a administrador liberando el ID exitoso.")

                # 10. Desactivar un usuario (is_active=False)
                payload_deactivate = {
                    "is_active": False
                }
                res_deact = await client.patch(f"/bm/users/{test_user.user_id}", json=payload_deactivate, headers=headers_admin)
                assert res_deact.status_code == 200, f"Error al desactivar: {res_deact.text}"
                assert res_deact.json()["is_active"] is False
                print("[+] Caso 10: Desactivar usuario exitoso.")

                # 11. Impedir inicio de sesión del usuario desactivado
                res_login_deact = await client.post("/bm/auth/login", json={"email": "new_email_user@boston.es", "password": "userpass123"})
                assert res_login_deact.status_code == 403, f"Se esperaba 403 Forbidden para usuario inactivo, se obtuvo: {res_login_deact.status_code}"
                
                # Impedir peticiones con token nuevo
                res_me_deact = await client.get("/bm/me", headers=headers_new_user)
                assert res_me_deact.status_code == 401, f"Se esperaba 401 para token de usuario inactivo, se obtuvo: {res_me_deact.status_code}"
                print("[+] Caso 11: Impedir login y peticiones de usuario desactivado exitoso.")

                # 12. Reactivar el usuario
                payload_reactivate = {
                    "is_active": True
                }
                res_react = await client.patch(f"/bm/users/{test_user.user_id}", json=payload_reactivate, headers=headers_admin)
                assert res_react.status_code == 200, f"Error al reactivar: {res_react.text}"
                assert res_react.json()["is_active"] is True
                print("[+] Caso 12: Reactivar usuario exitoso.")

                # Cambiar de nuevo el rol a usuario no administrador
                res_to_user = await client.patch(f"/bm/users/{test_user.user_id}", json={"role": "usuario"}, headers=headers_admin)
                assert res_to_user.status_code == 200, f"Error al cambiar rol a usuario: {res_to_user.text}"

                # 13. Impedir que un usuario no administrador edite a otro
                payload_non_admin = {
                    "name": "Intento Malicioso"
                }
                res_malicious = await client.patch(f"/bm/users/{conflict_user.user_id}", json=payload_non_admin, headers=headers_new_user)
                assert res_malicious.status_code == 403, f"Se esperaba 403 para no-admin, se obtuvo: {res_malicious.status_code}"
                print("[+] Caso 13: Impedir que usuario no administrador edite a otro exitoso.")

                # 14. Impedir que se desactive al único administrador activo
                payload_deact_self = {
                    "is_active": False
                }
                res_deact_self = await client.patch(f"/bm/users/{admin_user.user_id}", json=payload_deact_self, headers=headers_admin)
                assert res_deact_self.status_code == 400, f"Se esperaba 400 por desactivación propia/último admin, se obtuvo: {res_deact_self.status_code}"
                print("[+] Caso 14: Impedir desactivar al único admin activo exitoso.")

                # 15. Restablecer contraseña con POST /bm/users/{user_id}/password-reset
                payload_reset = {
                    "temp_password": "newtempsecure123"
                }
                res_reset = await client.post(f"/bm/users/{test_user.user_id}/password-reset", json=payload_reset, headers=headers_admin)
                assert res_reset.status_code == 200, f"Error en reset password: {res_reset.text}"
                assert res_reset.json()["must_reset_password"] is True
                assert res_reset.json()["temp_password"] == "newtempsecure123"
                print("[+] Caso 15: Restablecer contraseña administrativa con temp_password exitoso.")

                # Probar que login con nueva contraseña temporal funciona pero devuelve requires_password_reset
                res_login_temp = await client.post("/bm/auth/login", json={"email": "new_email_user@boston.es", "password": "newtempsecure123"})
                assert res_login_temp.status_code == 200, f"Error al iniciar sesión con contraseña temporal: {res_login_temp.text}"
                assert res_login_temp.json()["requires_password_reset"] is True
                print("[+] Caso 15.b: Login con contraseña temporal redirige a cambio de contraseña exitoso.")

                # 16. Verificar registros de auditoría
                async with AsyncSession(engine) as audit_db:
                    stmt_audit = select(UserAudit).order_by(UserAudit.audit_id.asc())
                    res_audit = await audit_db.execute(stmt_audit)
                    audits = res_audit.scalars().all()
                    
                    assert len(audits) > 0, "Debe haber registros de auditoría creados"
                    print(f"[+] Caso 16: Se encontraron {len(audits)} registros de auditoría.")
                    for a in audits:
                        print(f"    - Auditoría: admin_id={a.admin_user_id}, target_id={a.target_user_id}, action='{a.action}', changes={a.changes_json}")
                        assert a.admin_user_id == admin_user.user_id
                        assert a.target_user_id == test_user.user_id
                        assert isinstance(a.changes_json, dict)

                print("=== ¡TODAS LAS PRUEBAS DE EDICIÓN ADMINISTRATIVA DE USUARIOS PASARON EXITOSAMENTE! ===")

            finally:
                # Cleanup
                print("Limpiando datos de prueba...")
                await db.execute(delete(UserAudit))
                await db.execute(delete(User).where(User.username.in_([
                    "test_admin_editor", "test_user_edited", "test_agent_user", "test_conflict_user", "nuevo_username"
                ])))
                await db.execute(delete(User).where(User.email.in_([
                    "test_admin_editor@boston.es", "test_user_edited@boston.es", "test_agent_user@boston.es", "test_conflict_user@boston.es",
                    "new_email_user@boston.es", "conflict_email@boston.es"
                ])))
                await db.execute(delete(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id.in_(["88888801", "88888802"])))
                await db.commit()
                print("Base de datos limpia.")

if __name__ == "__main__":
    asyncio.run(test_admin_user_edition_workflow())
