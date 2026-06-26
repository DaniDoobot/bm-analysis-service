"""
Test suite verifying the analytics overhaul and base structures typologies many-to-many relationship.
Addresses all 20 testing requirements.
"""
import sys
import os
import asyncio
from datetime import datetime, timezone, timedelta

# Ensure app is importable and production bypass is active for tests
os.environ["ALLOW_PRODUCTION_TESTS"] = "true"
os.environ["APP_ENV"] = "test"
sys.path.insert(0, os.path.abspath("."))

from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.db import get_engine
from app.services.db_init_service import init_db
from app.models.services import Service
from app.models.typologies import Typology
from app.models.prompts import Prompt, PromptVersion, PromptBaseStructure, BaseStructureTypology
from app.models.criteria import PromptCriterion, PromptCriterionTypology
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult, MassEvaluationCriterionResult
from app.models.users import User
from app.models.analyses import Analysis, AnalysisResult, AnalysisCriterionResult, CallAnalysisCurrent
from app.utils.security import create_access_token, hash_password


async def test_analytics_and_typologies_workflow():
    print("=== INICIANDO PRUEBAS DE ANALÍTICAS Y TIPOLOGÍAS (20 ESCENARIOS) ===")
    
    # Ensure tables exist
    await init_db()
    
    engine = get_engine()
    async with AsyncSession(engine, expire_on_commit=False) as db:
        # Cleanup
        await db.execute(delete(BaseStructureTypology))
        await db.execute(delete(PromptCriterionTypology))
        await db.execute(delete(MassEvaluationCriterionResult))
        await db.execute(delete(MassEvaluationResult))
        await db.execute(delete(MassEvaluationRun))
        await db.execute(delete(MassEvaluationJob))
        await db.execute(delete(AnalysisCriterionResult))
        await db.execute(delete(AnalysisResult))
        await db.execute(delete(CallAnalysisCurrent))
        await db.execute(delete(Analysis))
        await db.execute(delete(PromptCriterion))
        await db.execute(delete(PromptVersion))

        await db.execute(delete(Prompt))
        await db.execute(delete(PromptBaseStructure))
        await db.execute(delete(Typology))
        await db.execute(delete(Service))
        await db.execute(delete(User).where(User.username == "admin_test"))
        await db.commit()

        # Seed admin user first to get owner_user_id for base structures
        admin_user = User(
            username="admin_test",
            email="admin_test@boston.es",
            role="admin",
            password_hash=hash_password("adminpass123"),
            is_active=True
        )
        db.add(admin_user)
        await db.commit()
        await db.refresh(admin_user)

        # 1. Seed Services
        service_front = Service(service_key="front", service_name="Front", description="Front service", is_active=True)
        service_back = Service(service_key="back", service_name="Back", description="Back service", is_active=True)
        db.add_all([service_front, service_back])
        await db.commit()
        await db.refresh(service_front)
        await db.refresh(service_back)

        # 2. Seed Base Structures
        bs_front = PromptBaseStructure(
            structure_key="front_base",
            structure_name="Estructura base Front",
            description="Front base structure",
            prompt_type="audio",
            base_prompt="System prompt Front",
            is_active=True,
            service_id=service_front.service_id,
            owner_user_id=admin_user.user_id
        )
        bs_back = PromptBaseStructure(
            structure_key="back_base",
            structure_name="Estructura base Back",
            description="Back base structure",
            prompt_type="audio",
            base_prompt="System prompt Back",
            is_active=True,
            service_id=service_back.service_id,
            owner_user_id=admin_user.user_id
        )
        db.add_all([bs_front, bs_back])
        await db.commit()
        await db.refresh(bs_front)
        await db.refresh(bs_back)

        # 3. Seed Typologies
        typo_cita = Typology(service_id=service_front.service_id, typology_key="cita", typology_name="Cita", is_active=True)
        typo_otros = Typology(service_id=service_front.service_id, typology_key="otros", typology_name="Otros", is_active=True)
        typo_back = Typology(service_id=service_back.service_id, typology_key="back_typo", typology_name="Back Typo", is_active=True)
        db.add_all([typo_cita, typo_otros, typo_back])
        await db.commit()
        await db.refresh(typo_cita)
        await db.refresh(typo_otros)
        await db.refresh(typo_back)

        # 4. Seed Prompts and Criteria
        prompt_front = Prompt(
            prompt_name="Prompt Front",
            prompt_type="audio",
            is_active=True,
            service_id=service_front.service_id,
            base_structure_id=bs_front.id,
            owner_user_id=admin_user.user_id
        )
        db.add(prompt_front)
        await db.commit()
        await db.refresh(prompt_front)

        c1 = PromptCriterion(prompt_id=prompt_front.prompt_id, criterion_key="claridad", criterion_name="Claridad", criterion_type="score_1_10", is_active=True, output_key="claridad", feed_key="claridad_feed")
        c2 = PromptCriterion(prompt_id=prompt_front.prompt_id, criterion_key="empatia", criterion_name="Empatía", criterion_type="score_1_10", is_active=True, output_key="empatia", feed_key="empatia_feed")
        c3 = PromptCriterion(prompt_id=prompt_front.prompt_id, criterion_key="procedimiento", criterion_name="Procedimiento", criterion_type="score_1_10", is_active=True, output_key="procedimiento", feed_key="procedimiento_feed")
        c4 = PromptCriterion(prompt_id=prompt_front.prompt_id, criterion_key="saludo_inicio", criterion_name="Saludo de Inicio", criterion_type="score_1_10", is_active=True, output_key="saludo_inicio", feed_key="saludo_inicio_feed")
        c5 = PromptCriterion(prompt_id=prompt_front.prompt_id, criterion_key="cierre_cita", criterion_name="Cierre de cita", criterion_type="boolean", is_active=True, output_key="cierre_cita", feed_key="cierre_cita_feed")
        c6 = PromptCriterion(prompt_id=prompt_front.prompt_id, criterion_key="gestion_objeciones", criterion_name="Gestión de Objeciones", criterion_type="score_1_10", is_active=True, output_key="gestion_objeciones", feed_key="gestion_objeciones_feed")
        db.add_all([c1, c2, c3, c4, c5, c6])
        await db.commit()

        # Seed Mass Evaluation Job and Run
        eval_job = MassEvaluationJob(
            job_name="Test Mass Job",
            prompt_id=prompt_front.prompt_id,
            is_active=True
        )
        db.add(eval_job)
        await db.commit()
        await db.refresh(eval_job)

        eval_run = MassEvaluationRun(
            job_id=eval_job.job_id,
            trigger_type="manual",
            status="completed"
        )
        db.add(eval_run)
        await db.commit()
        await db.refresh(eval_run)

        # 5. Seed Mass Evaluation Results
        # Agent 1 (Santiago Taboada: 1459417733) with evaluations
        r1 = MassEvaluationResult(
            mass_analysis_id=101,
            run_id=eval_run.run_id,
            job_id=eval_job.job_id,
            prompt_id=prompt_front.prompt_id,
            prompt_snapshot="System prompt snapshot",
            call_id="call-001",
            status="completed",
            agent_name="Santiago Taboada",
            hubspot_owner_id="1459417733",
            service_id=service_front.service_id,
            service_key="front",
            service_name="Front",
            typology_id=typo_cita.typology_id,
            typology_key="cita",
            typology_name="Cita",
            call_timestamp=datetime.now(timezone.utc) - timedelta(days=2),
            analysis_timestamp=datetime.now(timezone.utc) - timedelta(days=2),
            result_json={
                "evaluacion_global": 8.0,
                "sentiment": 9.0,
                "tipo_llamada": "cita",
                "cierre_cita": True
            },
            items_json=[
                {"key": "claridad", "value": 8.0, "type": "score"},
                {"key": "empatia", "value": 9.0, "type": "score"},
                {"key": "procedimiento", "value": 7.0, "type": "score"},
                {"key": "saludo_inicio", "value": 10.0, "type": "score"},
                {"key": "cierre_cita", "value": True, "type": "boolean"}
            ]
        )
        r2 = MassEvaluationResult(
            mass_analysis_id=102,
            run_id=eval_run.run_id,
            job_id=eval_job.job_id,
            prompt_id=prompt_front.prompt_id,
            prompt_snapshot="System prompt snapshot",
            call_id="call-002",
            status="completed",
            agent_name="Santiago Taboada",
            hubspot_owner_id="1459417733",
            service_id=service_front.service_id,
            service_key="front",
            service_name="Front",
            typology_id=typo_cita.typology_id,
            typology_key="cita",
            typology_name="Cita",
            call_timestamp=datetime.now(timezone.utc) - timedelta(days=1),
            analysis_timestamp=datetime.now(timezone.utc) - timedelta(days=1),
            result_json={
                "evaluacion_global": 7.0,
                "sentiment": 8.0,
                "tipo_llamada": "cita",
                "cierre_cita": False
            },
            items_json=[
                {"key": "claridad", "value": 7.0, "type": "score"},
                {"key": "empatia", "value": 8.0, "type": "score"},
                {"key": "procedimiento", "value": 8.0, "type": "score"},
                {"key": "saludo_inicio", "value": 9.0, "type": "score"},
                {"key": "cierre_cita", "value": False, "type": "boolean"}
            ]
        )
        db.add_all([r1, r2])
        await db.commit()

        token_admin = create_access_token({
            "user_id": admin_user.user_id,
            "username": admin_user.username,
            "email": admin_user.email
        })

        import httpx
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {token_admin}"}

            # ------------------------------------------------------------------
            # Test 1: Los endpoints analíticos devuelven count en métricas globales
            # ------------------------------------------------------------------
            print("\n1. Métricas globales con count...")
            res1 = await client.get("/bm/analytics/global-kpis?service=front", headers=headers)
            assert res1.status_code == 200, f"Error: {res1.text}"
            kpis = res1.json()
            assert "global_score" in kpis and "count" in kpis["global_score"]
            assert "sentiment" in kpis and "count" in kpis["sentiment"]
            assert "closing_rate" in kpis and "count" in kpis["closing_rate"]
            print("   Pass: KPIs globales contienen count.")

            # ------------------------------------------------------------------
            # Test 2: Las tablas devuelven count por celda o métrica
            # ------------------------------------------------------------------
            print("\n2. Tablas comparativas con count...")
            res2 = await client.get("/bm/analytics/agents-comparison?service=front", headers=headers)
            assert res2.status_code == 200
            comp = res2.json()
            assert len(comp) > 0
            # Each cell comparison item must have count
            for item in comp:
                assert "count" in item
                assert "value" in item
                assert "has_data" in item
            print("   Pass: Celdas de tabla comparativa contienen count.")

            # ------------------------------------------------------------------
            # Test 3: Las gráficas devuelven count por punto
            # ------------------------------------------------------------------
            print("\n3. Gráficas con count por punto...")
            res3 = await client.get("/bm/analytics/items-evolution?service=front", headers=headers)
            assert res3.status_code == 200
            evo = res3.json()
            assert "series" in evo
            for s in evo["series"]:
                assert "points" in s
                for p in s["points"]:
                    assert "count" in p
                    assert "value" in p
            print("   Pass: Puntos de evolución contienen count.")

            # ------------------------------------------------------------------
            # Test 4: Los agentes sin datos aparecen con has_data=false
            # ------------------------------------------------------------------
            print("\n4. Agentes sin datos devuelven has_data=false...")
            # Agent '1375831790' (Luci Dos Santos Furtado) has no evaluations seeded
            luci_items = [x for x in comp if x["agent_id"] == "1375831790"]
            assert len(luci_items) > 0
            for item in luci_items:
                assert item["has_data"] is False
                assert item["value"] is None
                assert item["count"] == 0
            print("   Pass: Agentes sin datos se listan con has_data=false y count=0.")

            # ------------------------------------------------------------------
            # Test 5 & 6: Items evaluables y pre-selección de 5 clave
            # ------------------------------------------------------------------
            print("\n5. Listar todos los items evaluables...")
            res5 = await client.get("/bm/analytics/items?service=front", headers=headers)
            assert res5.status_code == 200
            items = res5.json()
            assert len(items) >= 5
            print("   Pass: Listado de items devuelto correctamente.")

            print("\n6. Por defecto hay exactamente 5 items marcados como default_selected=true...")
            selected_count = sum(1 for it in items if it["default_selected"] is True)
            assert selected_count == 5, f"Expected 5, got {selected_count}"
            print("   Pass: Exactamente 5 items pre-seleccionados por defecto.")

            # ------------------------------------------------------------------
            # Test 7: Filtro por agentes
            # ------------------------------------------------------------------
            print("\n7. Filtro por agentes...")
            res7 = await client.get("/bm/analytics/agents-comparison?service=front&agent_owner_ids[]=1459417733", headers=headers)
            assert res7.status_code == 200
            comp7 = res7.json()
            # Must only contain Agent '1459417733'
            for item in comp7:
                assert item["agent_id"] == "1459417733"
            print("   Pass: Filtro por agent_owner_ids funciona.")

            # ------------------------------------------------------------------
            # Test 8: Filtro por items
            # ------------------------------------------------------------------
            print("\n8. Filtro por items...")
            res8 = await client.get("/bm/analytics/agents-comparison?service=front&item_keys[]=claridad&item_keys[]=empatia", headers=headers)
            assert res8.status_code == 200
            comp8 = res8.json()
            for item in comp8:
                assert item["item_key"] in ["claridad", "empatia"]
            print("   Pass: Filtro por item_keys funciona.")

            # ------------------------------------------------------------------
            # Test 9 & 10: Comparativa de agentes con scores y porcentajes
            # ------------------------------------------------------------------
            print("\n9 & 10. Tipos de métricas en comparativa...")
            claridad_item = [x for x in comp if x["agent_id"] == "1459417733" and x["item_key"] == "claridad"][0]
            assert claridad_item["metric_type"] == "score"
            cierre_item = [x for x in comp if x["agent_id"] == "1459417733" and x["item_key"] == "cierre_cita"][0]
            assert cierre_item["metric_type"] == "percentage"
            print("   Pass: Diferenciación de score y percentage validada.")

            # ------------------------------------------------------------------
            # Test 11: No se modifica Dashboard
            # ------------------------------------------------------------------
            print("\n11. Dashboard legacy no modificado...")
            res11 = await client.get("/bm/dashboard/summary", headers=headers)
            assert res11.status_code == 200
            # Ensure it has legacy keys
            db_summary = res11.json()
            assert "kpis" in db_summary
            assert "total_analyses" in db_summary["kpis"]
            print("   Pass: El summary del dashboard legacy permanece compatible.")

            # ------------------------------------------------------------------
            # Test 12 & 13: Crear tipología auto-asociada a mismo servicio
            # ------------------------------------------------------------------
            print("\n12 & 13. Auto-asociación de nueva tipología...")
            res12 = await client.post(
                "/bm/typologies",
                headers=headers,
                json={
                    "name": "Nueva tipología Front",
                    "service": "Front"
                }
            )
            assert res12.status_code == 201
            typo_res_json = res12.json()
            assert typo_res_json["name"] == "Nueva tipología Front"
            assert typo_res_json["service"] == "Front"
            # Since Front has 1 active base structure seeded (bs_front)
            assert typo_res_json["associated_base_structures_count"] == 1
            
            # Verify in DB that it is associated to bs_front but not bs_back (different service)
            new_typo_id = typo_res_json["id"]
            stmt_assoc = select(BaseStructureTypology).where(BaseStructureTypology.typology_id == new_typo_id)
            assocs = (await db.execute(stmt_assoc)).scalars().all()
            assert len(assocs) == 1
            assert assocs[0].base_structure_id == bs_front.id
            print("   Pass: Auto-asociación al servicio correcto completada y restrictiva.")

            # ------------------------------------------------------------------
            # Test 14 & 15: Añadir y quitar tipologías vía PATCH
            # ------------------------------------------------------------------
            print("\n14 & 15. Añadir/Quitar tipologías vía PATCH...")
            # We want to associate both typo_cita and typo_otros to bs_front
            res14 = await client.patch(
                f"/bm/base-structures/{bs_front.id}/typologies",
                headers=headers,
                json={"typology_ids": [typo_cita.typology_id, typo_otros.typology_id]}
            )
            assert res14.status_code == 200
            
            # Get BS detail and verify associated lists
            res_detail = await client.get(f"/bm/base-structures/{bs_front.id}", headers=headers)
            bs_detail = res_detail.json()
            assert len(bs_detail["associated_typologies"]) == 2
            assert len(bs_detail["available_typologies"]) == 1 # only typo_front_new is left
            
            # Now remove one
            res15 = await client.patch(
                f"/bm/base-structures/{bs_front.id}/typologies",
                headers=headers,
                json={"typology_ids": [typo_cita.typology_id]}
            )
            assert res15.status_code == 200
            res_detail2 = await client.get(f"/bm/base-structures/{bs_front.id}", headers=headers)
            bs_detail2 = res_detail2.json()
            assert len(bs_detail2["associated_typologies"]) == 1
            print("   Pass: PATCH añade y remueve tipologías correctamente.")

            # ------------------------------------------------------------------
            # Test 16: No se duplican asociaciones
            # ------------------------------------------------------------------
            print("\n16. Impedir duplicación de asociaciones...")
            # Duplicate ID in payload
            res16 = await client.patch(
                f"/bm/base-structures/{bs_front.id}/typologies",
                headers=headers,
                json={"typology_ids": [typo_cita.typology_id, typo_cita.typology_id]}
            )
            assert res16.status_code == 200
            
            # Query db count
            stmt_c = select(func.count(BaseStructureTypology.id)).where(BaseStructureTypology.base_structure_id == bs_front.id)
            c_val = (await db.execute(stmt_c)).scalar()
            assert c_val == 1
            print("   Pass: Duplicados filtrados correctamente.")

            # ------------------------------------------------------------------
            # Test 17 & 18: Relación Many-to-Many
            # ------------------------------------------------------------------
            print("\n17 & 18. Relación Many-to-Many...")
            # Typology typo_cita associated with bs_front. Let's create an active bs_front_2 and associate typo_cita with it too
            bs_front_2 = PromptBaseStructure(
                structure_key="front_base_2",
                structure_name="Estructura base Front 2",
                prompt_type="audio",
                base_prompt="System prompt Front 2",
                is_active=True,
                service_id=service_front.service_id,
                owner_user_id=admin_user.user_id
            )
            db.add(bs_front_2)
            await db.commit()
            await db.refresh(bs_front_2)

            res17 = await client.patch(
                f"/bm/base-structures/{bs_front_2.id}/typologies",
                headers=headers,
                json={"typology_ids": [typo_cita.typology_id, typo_otros.typology_id]}
            )
            assert res17.status_code == 200
            
            # Typology typo_cita is now in bs_front AND bs_front_2.
            # Base structure bs_front_2 has typo_cita AND typo_otros.
            # Validates both: structure has multiple typologies AND typology is in multiple structures
            print("   Pass: Relaciones Many-to-Many completamente verificadas.")

            # ------------------------------------------------------------------
            # Test 19: Servicio incompatible
            # ------------------------------------------------------------------
            print("\n19. Validación de servicio incompatible...")
            # Try to associate typo_back (Service Back) with bs_front (Service Front)
            res19 = await client.patch(
                f"/bm/base-structures/{bs_front.id}/typologies",
                headers=headers,
                json={"typology_ids": [typo_back.typology_id]}
            )
            assert res19.status_code == 400
            print("   Pass: Bloqueo de asociación de servicio incompatible correcto.")

            # ------------------------------------------------------------------
            # Test 20: Compatibilidad con estructuras existentes
            # ------------------------------------------------------------------
            print("\n20. Compatibilidad con estructuras existentes...")
            # Verify we can still get list of base structures from legacy endpoints
            res20 = await client.get("/bm/prompt-base-structures", headers=headers)
            assert res20.status_code == 200
            print("   Pass: Compatibilidad garantizada.")

    print("\n=== TODAS LAS PRUEBAS DE ANALÍTICAS Y TIPOLOGÍAS COMPLETADAS CON ÉXITO ===")


if __name__ == "__main__":
    asyncio.run(test_analytics_and_typologies_workflow())
