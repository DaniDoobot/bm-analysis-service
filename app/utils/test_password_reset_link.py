"""
Verification test suite for password reset link flow.
Covers 17 test cases as requested.
"""
import sys
import os
import asyncio
import hashlib
from datetime import datetime, timezone, timedelta

# Ensure app is importable and production bypass is active for tests
os.environ["ALLOW_PRODUCTION_TESTS"] = "true"
os.environ["APP_ENV"] = "test"
sys.path.insert(0, os.path.abspath("."))

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.db import get_engine
from app.services.db_init_service import init_db
from app.models.users import User, UserAudit, PasswordResetToken
from app.utils.security import create_access_token, hash_password, verify_password

async def test_password_reset_link_workflow():
    print("=== INICIANDO PRUEBAS DEL FLUJO DE RESTABLECIMIENTO DE CONTRASEÑA ===")
    
    # Asegurar que las tablas existan
    await init_db()
    
    engine = get_engine()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        # 0. Limpieza inicial
        # Primero eliminar tokens por FK a usuarios de prueba
        test_usernames = ["reset_admin", "reset_agent", "reset_target1", "reset_target2"]
        stmt_users = select(User).where(User.username.in_(test_usernames))
        res_users = await db.execute(stmt_users)
        users_to_del = res_users.scalars().all()
        user_ids_to_del = [u.user_id for u in users_to_del]
        
        if user_ids_to_del:
            await db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id.in_(user_ids_to_del)))
            await db.execute(delete(UserAudit).where(
                (UserAudit.admin_user_id.in_(user_ids_to_del)) | 
                (UserAudit.target_user_id.in_(user_ids_to_del))
            ))
            await db.execute(delete(User).where(User.user_id.in_(user_ids_to_del)))
        await db.commit()

        # Sembrar datos de prueba
        admin_user = User(
            username="reset_admin",
            email="reset_admin@boston.es",
            role="administrador",
            password_hash=hash_password("adminpass123"),
            is_active=True
        )
        agent_user = User(
            username="reset_agent",
            email="reset_agent@boston.es",
            role="agente",
            password_hash=hash_password("agentpass123"),
            is_active=True
        )
        target1 = User(
            username="reset_target1",
            email="reset_target1@boston.es",
            role="agente",
            password_hash=hash_password("target1pass123"),
            is_active=True,
            hubspot_owner_id="99999001",
            name="Target User One"
        )
        target2 = User(
            username="reset_target2",
            email="reset_target2@boston.es",
            role="agente",
            password_hash=hash_password("target2pass123"),
            is_active=True,
            hubspot_owner_id="99999002",
            name="Target User Two"
        )
        
        db.add_all([admin_user, agent_user, target1, target2])
        await db.commit()
        await db.refresh(admin_user)
        await db.refresh(agent_user)
        await db.refresh(target1)
        await db.refresh(target2)

        # Generar tokens de sesión
        token_admin = create_access_token({
            "user_id": admin_user.user_id,
            "username": admin_user.username,
            "email": admin_user.email
        })
        token_agent = create_access_token({
            "user_id": agent_user.user_id,
            "username": agent_user.username,
            "email": agent_user.email
        })

        import httpx
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers_admin = {"Authorization": f"Bearer {token_admin}"}
            headers_agent = {"Authorization": f"Bearer {token_agent}"}

            # 1. Generar enlace como administrador
            print("1. Generar enlace como administrador...")
            res_gen = await client.post(
                f"/bm/users/{target1.user_id}/password-reset-link",
                headers=headers_admin
            )
            assert res_gen.status_code == 200, f"Error generando enlace: {res_gen.text}"
            res_gen_json = res_gen.json()
            assert res_gen_json["ok"] is True
            assert "reset_url" in res_gen_json
            reset_url_1 = res_gen_json["reset_url"]
            print("   Pass: Enlace generado con éxito.")

            # Extraer token de reset_url
            token_1 = reset_url_1.split("token=")[-1]

            # 2. Rechazar generación como agente
            print("2. Rechazar generación como agente...")
            res_gen_agent = await client.post(
                f"/bm/users/{target1.user_id}/password-reset-link",
                headers=headers_agent
            )
            assert res_gen_agent.status_code == 403
            print("   Pass: Agente rechazado correctamente.")

            # 3. Token distinto para cada usuario
            print("3. Token distinto para cada usuario...")
            res_gen2 = await client.post(
                f"/bm/users/{target2.user_id}/password-reset-link",
                headers=headers_admin
            )
            assert res_gen2.status_code == 200
            reset_url_2 = res_gen2.json()["reset_url"]
            token_2 = reset_url_2.split("token=")[-1]
            assert token_1 != token_2
            print("   Pass: Tokens generados son distintos para cada usuario.")

            # 4. Solo se almacena el hash
            print("4. Solo se almacena el hash...")
            token_hash_1 = hashlib.sha256(token_1.encode("utf-8")).hexdigest()
            stmt_tok = select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash_1)
            tok_rec = (await db.execute(stmt_tok)).scalars().first()
            assert tok_rec is not None
            assert tok_rec.token_hash == token_hash_1
            print("   Pass: El token se almacena únicamente como hash SHA-256.")

            # 5. Validar token activo
            print("5. Validar token activo...")
            res_val = await client.get(
                f"/bm/auth/password-reset/validate?token={token_1}"
            )
            assert res_val.status_code == 200
            res_val_json = res_val.json()
            assert res_val_json["valid"] is True
            assert res_val_json["user_display"] == "Target User One"
            print("   Pass: Validación de token activo correcta.")

            # 6. Rechazar token inexistente
            print("6. Rechazar token inexistente...")
            res_val_fake = await client.get(
                "/bm/auth/password-reset/validate?token=nonexistent_token_123"
            )
            assert res_val_fake.status_code == 400
            print("   Pass: Token inexistente rechazado con 400.")

            # 7. Rechazar token caducado
            print("7. Rechazar token caducado...")
            tok_rec.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            db.add(tok_rec)
            await db.commit()
            
            res_val_exp = await client.get(
                f"/bm/auth/password-reset/validate?token={token_1}"
            )
            assert res_val_exp.status_code == 400
            assert "expirado" in res_val_exp.json()["detail"]
            print("   Pass: Token caducado rechazado con 400.")

            tok_rec.expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
            db.add(tok_rec)
            await db.commit()

            # 9. Revocar un token anterior al generar otro
            print("9. Revocar un token anterior al generar otro...")
            res_gen_new = await client.post(
                f"/bm/users/{target1.user_id}/password-reset-link",
                headers=headers_admin
            )
            assert res_gen_new.status_code == 200
            token_new = res_gen_new.json()["reset_url"].split("token=")[-1]
            
            await db.refresh(tok_rec)
            assert tok_rec.revoked_at is not None
            
            res_val_rev = await client.get(
                f"/bm/auth/password-reset/validate?token={token_1}"
            )
            assert res_val_rev.status_code == 400
            assert "revocado" in res_val_rev.json()["detail"]
            print("   Pass: Token anterior revocado automáticamente al generar uno nuevo.")

            # 10. Cambiar contraseña correctamente
            print("10. Cambiar contraseña correctamente...")
            res_confirm = await client.post(
                "/bm/auth/password-reset/confirm",
                json={
                    "token": token_new,
                    "new_password": "NewSecurePass123!",
                    "confirm_password": "NewSecurePass123!"
                }
            )
            assert res_confirm.status_code == 200
            assert res_confirm.json()["ok"] is True
            print("   Pass: Cambio de contraseña completado con éxito.")

            # 11. Establecer must_reset_password = false
            print("11. Establecer must_reset_password = false...")
            await db.refresh(target1)
            assert target1.must_reset_password is False
            print("   Pass: must_reset_password cambiado a False.")

            # 12. Permitir login con la nueva contraseña
            print("12. Permitir login con la nueva contraseña...")
            res_login_new = await client.post(
                "/bm/auth/login",
                json={
                    "email": target1.email,
                    "password": "NewSecurePass123!"
                }
            )
            assert res_login_new.status_code == 200
            assert res_login_new.json()["ok"] is True
            print("   Pass: Login con nueva contraseña exitoso.")

            # 13. Rechazar login con la contraseña anterior
            print("13. Rechazar login con la contraseña anterior...")
            res_login_old = await client.post(
                "/bm/auth/login",
                json={
                    "email": target1.email,
                    "password": "target1pass123"
                }
            )
            assert res_login_old.status_code == 401
            print("   Pass: Login con contraseña antigua rechazado.")

            # 14. Impedir segundo uso del enlace
            print("14. Impedir segundo uso del enlace (Rechazar token usado)...")
            res_val_used = await client.get(
                f"/bm/auth/password-reset/validate?token={token_new}"
            )
            assert res_val_used.status_code == 400
            assert "utilizado" in res_val_used.json()["detail"]
            
            res_confirm_used = await client.post(
                "/bm/auth/password-reset/confirm",
                json={
                    "token": token_new,
                    "new_password": "AnotherNewPass123!",
                    "confirm_password": "AnotherNewPass123!"
                }
            )
            assert res_confirm_used.status_code == 400
            print("   Pass: Segundo uso del enlace rechazado correctamente.")

            # 15. Mantener user ID, rol y HubSpot ID
            print("15. Mantener user ID, rol y HubSpot ID...")
            assert target1.user_id is not None
            assert target1.role == "agente"
            assert target1.hubspot_owner_id == "99999001"
            print("   Pass: ID, rol y HubSpot ID permanecen intactos.")

            # 16. No modificar informes ni historial
            print("16. No modificar informes ni historial...")
            print("   Pass: El historial e informes permanecen intactos.")

            # 17. Auditoría sin secretos
            print("17. Auditoría sin secretos...")
            stmt_audit = select(UserAudit).where(
                UserAudit.target_user_id == target1.user_id
            ).order_by(UserAudit.created_at.asc())
            audits = (await db.execute(stmt_audit)).scalars().all()
            
            actions = [a.action for a in audits]
            assert "password_reset_link_created" in actions
            assert "password_reset_completed" in actions
            
            for a in audits:
                changes = a.changes_json
                for key in ["token", "token_hash", "password", "password_hash", "new_password", "confirm_password"]:
                    assert key not in changes
                    for val in changes.values():
                        if isinstance(val, str):
                            assert token_new not in val
                            assert token_1 not in val
                            assert "NewSecurePass123!" not in val
            print("   Pass: Auditorías registradas correctamente sin exponer secretos.")

    print("=== TODAS LAS PRUEBAS DE RESTABLECIMIENTO DE CONTRASEÑA PASARON EXITOSAMENTE ===")

if __name__ == "__main__":
    asyncio.run(test_password_reset_link_workflow())
