"""
Unit-level tests for generate_criterion_description_ai.
Uses a MagicMock-based stub for every module in the dependency chain
that requires asyncpg / DB access, so these tests run without a DB.
"""
import sys, os, re, asyncio, types
from unittest.mock import MagicMock, AsyncMock


# ── Helper: create a module stub where every attribute returns a MagicMock ──
class AutoMockModule(types.ModuleType):
    def __getattr__(self, name):
        val = MagicMock()
        setattr(self, name, val)
        return val


def stub(name):
    mod = AutoMockModule(name)
    sys.modules[name] = mod
    return mod


# ── 1. Stub all heavy dependency modules before any real import ────────────
for m in [
    "asyncpg",
    "sqlalchemy", "sqlalchemy.ext", "sqlalchemy.ext.asyncio",
    "sqlalchemy.orm", "sqlalchemy.dialects",
    "sqlalchemy.dialects.postgresql", "sqlalchemy.dialects.postgresql.asyncpg",
]:
    stub(m)

# ── 2. Stub app package ────────────────────────────────────────────────────
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_app_dir  = os.path.join(_repo_root, "app")
_svc_dir  = os.path.join(_app_dir, "services")

# app package — must have __path__ pointing to the real app directory
app_pkg = AutoMockModule("app")
app_pkg.__path__ = [_app_dir]
app_pkg.__package__ = "app"
sys.modules["app"] = app_pkg

# app.db — no real engine
app_db = stub("app.db")
app_db.Base = MagicMock()
app_db.SessionLocal = MagicMock()

# app.models.*
for m in ["app.models", "app.models.criteria", "app.models.analyses",
          "app.models.prompts", "app.models.users", "app.models.typologies",
          "app.models.services"]:
    stub(m)

# app.schemas.*
for m in ["app.schemas", "app.schemas.criteria", "app.schemas.typologies",
          "app.schemas.prompts", "app.schemas.analyses"]:
    stub(m)

# app.services — MUST have __path__ pointing to the real services directory
svc_pkg = AutoMockModule("app.services")
svc_pkg.__path__ = [_svc_dir]
svc_pkg.__package__ = "app.services"
sys.modules["app.services"] = svc_pkg

# app.services.prompts_service — stub clean_whitespaces
prompts_svc = stub("app.services.prompts_service")
prompts_svc.clean_whitespaces = lambda s: s

# app.services.openai_service — stub complete_text (will be replaced per test)
openai_svc = stub("app.services.openai_service")
openai_svc.complete_text = AsyncMock(return_value="")

# fastapi stubs
for m in ["fastapi", "fastapi.exceptions"]:
    stub(m)
sys.modules["fastapi"].HTTPException = Exception
sys.modules["fastapi"].status = MagicMock()

# app.core.*
for m in ["app.core", "app.core.config", "app.core.security"]:
    stub(m)
sys.modules["app.core.config"].settings = MagicMock()

# ── 3. Import the real function ───────────────────────────────────────────
from app.services.criteria_service import generate_criterion_description_ai  # noqa: E402


# ── 4. Test helpers ───────────────────────────────────────────────────────
class DummyBody:
    def __init__(self, name, item_type, out_key, feed_key, current_desc, instr, typology_keys=None):
        self.criterion_name   = name
        self.criterion_type   = item_type
        self.output_key       = out_key
        self.feed_key         = feed_key
        self.current_description = current_desc
        self.instruction      = instr
        self.typology_keys    = typology_keys or []


FULL_SCORE_RESPONSE = """
Actúa como un auditor de calidad extremadamente exigente. Evalúa si el agente transmite cercanía.

Dimensiones de evaluación:

1. CALIDEZ Y TONO HUMANO
- Valora si el agente utiliza un tono cercano.
- Penaliza tonos fríos o mecánicos.

2. ESCUCHA ACTIVA
- Valora si el agente demuestra que escucha.
- Penaliza frases genéricas.

Reglas de puntuación:
- 1-2: Muy deficiente. El agente suena frío.
- 3-4: Deficiente. Hay educación básica pero no cercanía.
- 5-6: Aceptable pero mejorable.
- 7-8: Bueno. El agente muestra calidez.
- 9-10: Excelente. Genera confianza clara.

Criterios de penalización:
- Penaliza si ignora dudas emocionales.
- No des puntuaciones altas si solo es educado.

Cuándo devolver null:
- Devuelve null solo si la llamada no contiene interacción suficiente.

Formato de feedback:
- Justifica la puntuación con ejemplos concretos de la llamada.
- Si la nota es baja, explica qué señales faltaron.
"""


def check_score_1_10_structure(description: str, feed_key=None):
    d = description.lower()
    assert any(x in d for x in ("9-10", "excelente")),           "Missing high score rubric"
    assert any(x in d for x in ("5-6", "7-8", "aceptable", "bueno")), "Missing mid score rubric"
    assert any(x in d for x in ("1-2", "3-4", "deficiente")),    "Missing low score rubric"
    assert "dimensi" in d or "criterio" in d,                     "Missing evaluation dimensions section"
    assert "penaliz" in d,                                        "Missing penalties section"
    assert "null" in d,                                           "Missing null rule"
    if feed_key:
        assert any(x in d for x in ("justifica", "ejemplo", "cita", "evidencia", "feedback", "explica")), \
            "Missing feedback instruction"


# ── 5. Tests ─────────────────────────────────────────────────────────────────

async def test_sanitization():
    print("=== TEST 1: SANITISATION (JSON, labels, preamble) ===")
    body = DummyBody(
        name="Prueba Cercanía", item_type="score_1_10",
        out_key="prueba_cercania", feed_key="prueba_cercania_feed",
        current_desc="Evalúa cercanía.", instr="Sé estricto."
    )
    mock_response = """
Aquí tienes la descripción generada:
Actúa como un auditor exigente.
{ "output_key": "prueba_cercania", "feed_key": "prueba_cercania_feed" }
Dimensiones de evaluación:
1. CERCANÍA
- Penaliza si es frío.

Reglas de puntuación:
- 1-2: Muy deficiente.
- 3-4: Deficiente.
- 5-6: Aceptable pero mejorable.
- 7-8: Bueno.
- 9-10: Excelente.

Criterios de penalización:
- Penaliza si suena mecánico.

Cuándo devolver null:
- Devuelve null si no hay llamada.

Formato de feedback:
- Justifica con ejemplos concretos.
"""
    openai_svc.complete_text = AsyncMock(return_value=mock_response)
    res = await generate_criterion_description_ai(db=None, criterion_id=None, body=body)
    print("  Warnings:", res["warnings"])
    assert res["ok"] is True
    assert "{" not in res["description"]
    assert "}" not in res["description"]
    assert "output_key:" not in res["description"].lower()
    assert "feed_key:" not in res["description"].lower()
    assert not res["description"].lower().startswith("aquí tienes")
    print("PASSED\n")


async def test_full_structure():
    print("=== TEST 2: FULL STRUCTURE — no quality warnings ===")
    body = DummyBody(
        name="Prueba Cercanía", item_type="score_1_10",
        out_key="prueba_cercania", feed_key="prueba_cercania_feed",
        current_desc="", instr=""
    )
    openai_svc.complete_text = AsyncMock(return_value=FULL_SCORE_RESPONSE)
    res = await generate_criterion_description_ai(db=None, criterion_id=None, body=body)
    print("  Warnings:", res["warnings"])
    assert res["ok"] is True
    check_score_1_10_structure(res["description"], feed_key="prueba_cercania_feed")
    structural = [w for w in res["warnings"] if "La descripción no" in w]
    assert structural == [], f"Unexpected structural warnings: {structural}"
    print("PASSED\n")


async def test_missing_rubric_warns():
    print("=== TEST 3: MISSING RUBRIC -> quality warnings ===")
    body = DummyBody(
        name="Prueba Velocidad", item_type="score_1_10",
        out_key="prueba_velocidad", feed_key="prueba_velocidad_feed",
        current_desc="", instr="Sé estricto."
    )
    incomplete = """
Actúa como un auditor. Evalúa la velocidad.
Dimensiones de evaluación:
1. TIEMPO
- Penaliza si tarda mucho.
Cuándo devolver null:
- Si no hay tiempo, devuelve null.
"""
    openai_svc.complete_text = AsyncMock(return_value=incomplete)
    res = await generate_criterion_description_ai(db=None, criterion_id=None, body=body)
    print("  Warnings:", res["warnings"])
    assert res["ok"] is True
    w = res["warnings"]
    assert any("9-10" in x for x in w),                           "Expected warning: missing high score"
    assert any("5-8" in x or "media" in x.lower() for x in w),   "Expected warning: missing mid score"
    assert any("1-4" in x or "baja" in x.lower() for x in w),    "Expected warning: missing low score"
    print("PASSED\n")


async def test_boolean_structure():
    print("=== TEST 4: BOOLEAN STRUCTURE ===")
    body = DummyBody(
        name="Verifica Cita", item_type="boolean",
        out_key="verifica_cita", feed_key=None,
        current_desc="", instr=""
    )
    mock_resp = """
Actúa como un auditor estricto. Verifica si el agente comprueba los datos de la cita.

Regla de evaluación:
- Devuelve "Si" si confirma fecha, hora y servicio.
- Devuelve "No" si no hay verificación.
- Devuelve null si la llamada no tiene contexto de cita.

Evidencias:
- Busca frases como "le confirmo que su cita es...".
- No presupone cumplimiento si no aparece explícitamente.

Cuándo devolver null:
- Si la llamada no tiene ningún elemento de cita.
"""
    openai_svc.complete_text = AsyncMock(return_value=mock_resp)
    res = await generate_criterion_description_ai(db=None, criterion_id=None, body=body)
    print("  Warnings:", res["warnings"])
    assert res["ok"] is True
    assert "null" in res["description"].lower()
    structural = [w for w in res["warnings"] if "La descripción no" in w]
    assert structural == [], f"Unexpected structural warnings: {structural}"
    print("PASSED\n")


async def test_default_instruction():
    print("=== TEST 5: DEFAULT INSTRUCTION WHEN INSTR IS EMPTY ===")
    body = DummyBody(
        name="Empatía", item_type="score_1_10",
        out_key="empatia", feed_key="empatia_feed",
        current_desc="", instr=""
    )
    captured = {}

    async def fake(messages, **kwargs):
        captured["user"] = messages[1]["content"]
        return FULL_SCORE_RESPONSE

    openai_svc.complete_text = fake
    res = await generate_criterion_description_ai(db=None, criterion_id=None, body=body)
    assert res["ok"] is True
    assert "mini-prompt de auditoría" in captured["user"], \
        f"Default instruction not injected. User prompt:\n{captured.get('user','')[:300]}"
    print("PASSED\n")


async def test_system_prompt_templates():
    print("=== TEST 6: SYSTEM PROMPT TEMPLATES BY TYPE ===")
    checks = [
        ("score_1_10", "ESTRUCTURA OBLIGATORIA para score_1_10"),
        ("boolean",    "ESTRUCTURA OBLIGATORIA para boolean"),
        ("text",       "ESTRUCTURA OBLIGATORIA para text"),
        ("number",     "ESTRUCTURA OBLIGATORIA para number/percentage"),
        ("percentage", "ESTRUCTURA OBLIGATORIA para number/percentage"),
    ]
    for ctype, expected in checks:
        body = DummyBody(
            name="Test", item_type=ctype, out_key="key", feed_key=None,
            current_desc="", instr=""
        )
        captured = {}

        async def fake(messages, **kwargs):
            captured["system"] = messages[0]["content"]
            return FULL_SCORE_RESPONSE

        openai_svc.complete_text = fake
        await generate_criterion_description_ai(db=None, criterion_id=None, body=body)
        assert expected in captured["system"], \
            f"Missing '{expected}' in system prompt for type={ctype}"
        print(f"  {ctype} → OK")
    print("PASSED\n")


async def test_length_limit():
    print("=== TEST 7: LENGTH LIMIT (3500 chars) ===")
    body = DummyBody(
        name="Test", item_type="score_1_10", out_key="test", feed_key=None,
        current_desc="", instr=""
    )

    # Under limit — no truncation
    short = FULL_SCORE_RESPONSE.strip() + ("\nDetalle adicional. " * 60)
    assert len(short) < 3500
    openai_svc.complete_text = AsyncMock(return_value=short)
    res = await generate_criterion_description_ai(db=None, criterion_id=None, body=body)
    assert res["ok"] is True
    assert not any("truncada" in w for w in res["warnings"]), "Should not truncate short response"

    # Over limit — must truncate
    very_long = FULL_SCORE_RESPONSE.strip() + ("\nDetalle adicional muy largo aquí. " * 200)
    assert len(very_long) > 3500
    openai_svc.complete_text = AsyncMock(return_value=very_long)
    res = await generate_criterion_description_ai(db=None, criterion_id=None, body=body)
    assert res["ok"] is True
    assert len(res["description"]) <= 3600
    assert any("truncada" in w for w in res["warnings"]), "Expected truncation warning"
    print("PASSED\n")


# ── 6. Run all ────────────────────────────────────────────────────────────────
async def main():
    await test_sanitization()
    await test_full_structure()
    await test_missing_rubric_warns()
    await test_boolean_structure()
    await test_default_instruction()
    await test_system_prompt_templates()
    await test_length_limit()
    print("=" * 50)
    print("ALL TESTS PASSED ✓")


if __name__ == "__main__":
    asyncio.run(main())
