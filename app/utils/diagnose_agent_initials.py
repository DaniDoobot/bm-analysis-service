"""
Diagnose Agent Initials Script
================================
Ejecuta consultas seguras de solo lectura para auditar cómo está registrada Luci en bm_users
y qué hubspot_owner_id/agent_name se están registrando en las evaluaciones masivas e individuales.
"""
import asyncio
import os
import sys
from dotenv import load_dotenv

# Cargar variables de entorno desde .env si existe
load_dotenv()

from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
settings = get_settings()
from app.db import _make_async_url, Base
from app.models.users import User
from app.models.mass_evaluations import MassEvaluationResult
from app.models.analyses import Analysis, CallAnalysisCurrent


async def run_diagnosis():
    db_url = os.environ.get("DATABASE_URL") or settings.database_url
    if not db_url:
        print("ERROR: DATABASE_URL not set in environment or config.")
        return

    async_url = _make_async_url(db_url)
    engine = create_async_engine(async_url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        print("=" * 80)
        print("1. BUSCANDO USUARIOS EN bm_users (Luci, LD, LF, owner 1375831790)")
        print("=" * 80)

        stmt_users = select(User).where(
            or_(
                func.lower(User.name).like("%luci%"),
                func.lower(User.email).like("%luci%"),
                User.hubspot_owner_id == "1375831790",
                func.lower(User.agent_initials).in_(["ld", "lf"])
            )
        )
        res_users = await session.execute(stmt_users)
        users = res_users.scalars().all()

        if not users:
            print("❌ No se encontró ningún usuario que coincida en bm_users.")
        else:
            for u in users:
                print(f"User ID          : {u.user_id}")
                print(f"Username         : {u.username}")
                print(f"Email            : {u.email}")
                print(f"Name             : {u.name}")
                print(f"Display Name     : {u.display_name}")
                print(f"Role             : {u.role}")
                print(f"Company ID       : {u.company_id}")
                print(f"Hubspot Owner ID : {u.hubspot_owner_id}")
                print(f"Agent Initials   : {u.agent_initials}")
                print(f"Is Active        : {u.is_active}")
                print("-" * 50)

        print("\n" + "=" * 80)
        print("2. BUSCANDO RESULTADOS EN bm_mass_evaluation_results (Luci / owner 1375831790)")
        print("=" * 80)

        stmt_mass = (
            select(MassEvaluationResult)
            .where(
                or_(
                    func.lower(MassEvaluationResult.agent_name).like("%luci%"),
                    MassEvaluationResult.hubspot_owner_id == "1375831790"
                )
            )
            .order_by(MassEvaluationResult.mass_analysis_id.desc())
            .limit(10)
        )
        res_mass = await session.execute(stmt_mass)
        mass_rows = res_mass.scalars().all()

        if not mass_rows:
            print("❌ No se encontraron registros de evaluaciones masivas para Luci.")
        else:
            for r in mass_rows:
                print(f"Mass Result ID   : {r.mass_analysis_id}")
                print(f"Call ID          : {r.call_id}")
                print(f"Company ID       : {r.company_id}")
                print(f"Service ID       : {r.service_id}")
                print(f"Hubspot Owner ID : {r.hubspot_owner_id}")
                print(f"Agent Name       : {r.agent_name}")
                print(f"Typology Key     : {r.typology_key}")
                print(f"Call Timestamp   : {r.call_timestamp}")
                print(f"Status           : {r.status}")
                print("-" * 50)

        print("\n" + "=" * 80)
        print("3. BUSCANDO RESULTADOS EN bm_analyses (Luci / owner 1375831790)")
        print("=" * 80)

        try:
            stmt_analyses = (
                select(Analysis)
                .where(
                    or_(
                        func.lower(Analysis.agent_name).like("%luci%"),
                        Analysis.hubspot_owner_id == "1375831790"
                    )
                )
                .order_by(Analysis.analysis_id.desc())
                .limit(10)
            )
            res_ana = await session.execute(stmt_analyses)
            ana_rows = res_ana.scalars().all()

            if not ana_rows:
                print("ℹ️ No se encontraron análisis individuales en bm_analyses.")
            else:
                for a in ana_rows:
                    print(f"Analysis ID      : {a.analysis_id}")
                    print(f"Call ID          : {a.call_id}")
                    print(f"Company ID       : {a.company_id}")
                    print(f"Service ID       : {a.service_id}")
                    print(f"Hubspot Owner ID : {a.hubspot_owner_id}")
                    print(f"Agent Name       : {a.agent_name}")
                    print(f"Created At       : {a.created_at}")
                    print("-" * 50)
        except Exception as e_ana:
            print(f"Error consultando bm_analyses: {e_ana}")

        print("\n" + "=" * 80)
        print("4. BUSCANDO RESULTADOS EN bm_call_analysis_current (Luci / owner 1375831790)")
        print("=" * 80)

        try:
            stmt_curr = (
                select(CallAnalysisCurrent)
                .where(
                    or_(
                        func.lower(CallAnalysisCurrent.agent_name).like("%luci%"),
                        CallAnalysisCurrent.hubspot_owner_id == "1375831790"
                    )
                )
                .order_by(CallAnalysisCurrent.analysis_id.desc())
                .limit(10)
            )
            res_curr = await session.execute(stmt_curr)
            curr_rows = res_curr.scalars().all()

            if not curr_rows:
                print("ℹ️ No se encontraron registros en bm_call_analysis_current.")
            else:
                for c in curr_rows:
                    print(f"Analysis ID      : {c.analysis_id}")
                    print(f"Call ID          : {c.call_id}")
                    print(f"Hubspot Owner ID : {c.hubspot_owner_id}")
                    print(f"Agent Name       : {c.agent_name}")
                    print("-" * 50)
        except Exception as e_curr:
            print(f"Error consultando bm_call_analysis_current: {e_curr}")

        # Diagnostic summary and resolution simulation
        print("\n" + "=" * 80)
        print("5. DIAGNÓSTICO Y RESOLUCIÓN SIMULADA")
        print("=" * 80)

        luci_user = next((u for u in users if u.hubspot_owner_id == "1375831790" or "luci" in (u.name or "").lower()), None)
        if luci_user:
            print(f"Match en bm_users por ID/Nombre: ID={luci_user.user_id}, Name='{luci_user.name}', OwnerID='{luci_user.hubspot_owner_id}', Initials='{luci_user.agent_initials}'")
            if luci_user.hubspot_owner_id == "1375831790":
                print("Motivo de resolución por owner_id: matched_by_hubspot_owner_id")
            else:
                print("Motivo de resolución por nombre: matched_by_normalized_name")
        else:
            print("❌ Luci NO existe en bm_users o no tiene hubspot_owner_id/nombre coincidente.")
            print("Motivo de resolución: fallback_calculated")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_diagnosis())
