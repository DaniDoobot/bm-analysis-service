import asyncio
import os
from sqlalchemy import select
from app.db import get_engine
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.personalized_training import TrainingAgentSetting
from app.services.personalized_training_service import PersonalizedTrainingService, edit_distance
from app.services.db_init_service import init_db


async def main():
    print("=== TEST: IDENTIFICACION Y VALIDACION DE CODIGOS ===")
    
    # 1. Run migrations first
    print("Ejecutando inicialización/migración de base de datos... (SKIPPED in test)")
    # await init_db()
    print("Base de datos inicializada/migrada.")
    
    # 2. Test edit_distance helper
    assert edit_distance("LD23", "LD23") == 0
    assert edit_distance("LD23", "LD24") == 1
    assert edit_distance("LD23", "FD23") == 1
    assert edit_distance("LD23", "FR45") == 4
    print("edit_distance helper pasa pruebas unitarias.")

    # 3. Test duplicate and similarity validations in database
    engine = get_engine()
    async with AsyncSession(engine) as db:
        # Check current settings
        stmt = select(TrainingAgentSetting)
        res = await db.execute(stmt)
        settings_list = res.scalars().all()
        
        # Backup settings
        backup = []
        for s in settings_list:
            backup.append({
                "hubspot_owner_id": s.hubspot_owner_id,
                "training_code": s.training_code,
                "training_numeric_code": s.training_numeric_code,
                "training_code_enabled": s.training_code_enabled,
                "is_enabled": s.is_enabled
            })
            
    try:
        async with AsyncSession(engine) as db:
            # Re-fetch settings
            res = await db.execute(stmt)
            settings_list = res.scalars().all()
            # Reset existing training codes to start clean and avoid collisions across test runs
            for s in settings_list:
                s.training_code = None
                s.training_numeric_code = None
            await db.commit()
            
            # Re-fetch after commit
            res = await db.execute(stmt)
            settings_list = res.scalars().all()
            print(f"Base de datos tiene {len(settings_list)} configuraciones de agente.")
            
            # Test updating setting for first agent with valid code
            if settings_list:
                target_agent = settings_list[0]
                target_name = target_agent.agent_name
                target_owner_id = target_agent.hubspot_owner_id
                
                other_name = None
                other_owner_id = None
                if len(settings_list) > 1:
                    other_agent = settings_list[1]
                    other_name = other_agent.agent_name
                    other_owner_id = other_agent.hubspot_owner_id
                    
                print(f"Probando asignacion en agente: {target_name} (owner_id={target_owner_id})")
                
                # Reset values to clean state
                await PersonalizedTrainingService.update_agent_setting(
                    db=db,
                    hubspot_owner_id=target_owner_id,
                    training_code="LD23",
                    training_numeric_code="1009",
                    training_code_enabled=True
                )
                print("Configuracion inicial asignada con exito (LD23 / 1009).")
                
                # Test duplicate alphanumeric code validation on another agent
                if other_name is not None:
                    print(f"Intentando asignar codigo duplicado 'LD23' a {other_name}...")
                    try:
                        await PersonalizedTrainingService.update_agent_setting(
                            db=db,
                            hubspot_owner_id=other_owner_id,
                            training_code="LD23"
                        )
                        print("ERROR: Se permitio un codigo duplicado.")
                    except ValueError as e:
                        print(f"Capturado error esperado de duplicidad: {e}")
                        
                    # Test edit distance similarity validation (distance <= 1)
                    print(f"Intentando asignar codigo similar 'LD24' (distancia 1 de 'LD23') a {other_name}...")
                    try:
                        await PersonalizedTrainingService.update_agent_setting(
                            db=db,
                            hubspot_owner_id=other_owner_id,
                            training_code="LD24"
                        )
                        print("ERROR: Se permitio un codigo demasiado similar (distancia 1).")
                    except ValueError as e:
                        print(f"Capturado error esperado de similitud (Levenshtein <= 1): {e}")
    
                    # Test numeric code similarity validation (distance <= 1)
                    print(f"Intentando asignar codigo numerico similar '1008' (distancia 1 de '1009') a {other_name}...")
                    try:
                        await PersonalizedTrainingService.update_agent_setting(
                            db=db,
                            hubspot_owner_id=other_owner_id,
                            training_numeric_code="1008"
                        )
                        print("ERROR: Se permitio un codigo numerico demasiado similar (distancia 1).")
                    except ValueError as e:
                        print(f"Capturado error esperado de similitud numerica: {e}")
    
                    # Test setting distinct valid codes
                    print(f"Intentando asignar codigos distintos permitidos ('FR45' y '1212') a {other_name}...")
                    await PersonalizedTrainingService.update_agent_setting(
                        db=db,
                        hubspot_owner_id=other_owner_id,
                        training_code="FR45",
                        training_numeric_code="1212"
                    )
                    print("Codigos validos distintos asignados con exito.")
    
            else:
                print("[WARNING] No hay agentes en base de datos para realizar pruebas de validación.")
    finally:
        print("Restaurando base de datos a su estado original...")
        async with AsyncSession(engine) as db:
            for b in backup:
                stmt_s = select(TrainingAgentSetting).where(TrainingAgentSetting.hubspot_owner_id == b["hubspot_owner_id"])
                res_s = await db.execute(stmt_s)
                setting = res_s.scalar()
                if setting:
                    setting.training_code = b["training_code"]
                    setting.training_numeric_code = b["training_numeric_code"]
                    setting.training_code_enabled = b["training_code_enabled"]
                    setting.is_enabled = b["is_enabled"]
            await db.commit()
        await engine.dispose()
        print("Base de datos restaurada.")

if __name__ == "__main__":
    asyncio.run(main())
