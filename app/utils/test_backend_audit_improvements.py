"""
Targeted tests for backend improvements:
1. Password policy canonical enforcement and backward-compatibility for legacy current_password.
2. CompanyCreate schema, slug auto-generation, concurrency collision retry, and PATCH stability.
3. MyAgentCode endpoint security, scoping, and minimal contract.
4. Analytics items service_id isolation, permissions 403, and catalog population.
5. Criteria AI description postprocessing with 6k, 20k, <50k and >50k real length controls.
"""
import inspect
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from pydantic import ValidationError
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.utils.security import validate_password_policy, generate_temporary_password
from app.schemas.users import (
    BootstrapPayload,
    UserCreatePayload,
    UserAdminResetPasswordPayload,
    AdminPasswordResetPayload,
    MePasswordUpdatePayload,
    ChangePasswordPayload,
    ResetPasswordPayload,
    PasswordResetConfirmPayload,
    MyAgentCodeResponse,
)
from app.schemas.multitenancy import CompanyCreate, CompanyUpdate, _validate_company_key
from app.routers.companies import _slugify_company_name, _generate_unique_company_key, create_company, update_company
from app.routers.me import get_my_agent_code
from app.routers.analytics import get_all_metrics, get_analytics_items
from app.services.criteria_service import MAX_AI_CRITERION_DESCRIPTION_CHARS, generate_criterion_description_ai
from app.models.users import User
from app.models.personalized_training import TrainingAgentSetting
from app.models.companies import Company
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.schemas.criteria import AIDescriptionRequest


class TestPasswordPolicy(unittest.TestCase):
    def test_reject_short_password(self):
        with self.assertRaises(ValueError):
            validate_password_policy("Ab1!defg")

    def test_reject_no_special_char(self):
        with self.assertRaises(ValueError):
            validate_password_policy("Abcdefghijk123")

    def test_reject_whitespace_as_only_special(self):
        with self.assertRaises(ValueError):
            validate_password_policy("Abcdefghijk 123")

    def test_accept_valid_password(self):
        pwd = "ValidPassword123!"
        self.assertEqual(validate_password_policy(pwd), pwd)

    def test_generated_temporary_password_satisfies_policy(self):
        for _ in range(50):
            temp_pass = generate_temporary_password(16)
            self.assertGreaterEqual(len(temp_pass), 10)
            self.assertEqual(validate_password_policy(temp_pass), temp_pass)

    def test_legacy_current_password_not_blocked(self):
        # A legacy password of 8 characters without special characters must be valid as current_password
        payload = ChangePasswordPayload(
            current_password="oldpass8",
            new_password="NewSecurePassword123!",
            new_password_confirm="NewSecurePassword123!"
        )
        self.assertEqual(payload.current_password, "oldpass8")
        self.assertEqual(payload.new_password, "NewSecurePassword123!")

        me_payload = MePasswordUpdatePayload(
            current_password="simpleold",
            new_password="NewSecurePassword123!",
            new_password_confirm="NewSecurePassword123!"
        )
        self.assertEqual(me_payload.current_password, "simpleold")

    def test_schemas_enforce_password_policy(self):
        with self.assertRaises(ValidationError):
            ChangePasswordPayload(
                current_password="old",
                new_password="short",
                new_password_confirm="short"
            )

        with self.assertRaises(ValidationError):
            MePasswordUpdatePayload(
                current_password="old",
                new_password="NoSpecialChar12345",
                new_password_confirm="NoSpecialChar12345"
            )

        with self.assertRaises(ValidationError):
            AdminPasswordResetPayload(temp_password="short")
        p_none = AdminPasswordResetPayload(temp_password=None)
        self.assertIsNone(p_none.temp_password)

        with self.assertRaises(ValidationError):
            UserCreatePayload(
                username="testuser",
                email="test@example.com",
                role="agent",
                password="short"
            )


class TestCompanyKeySlugAndConcurrency(unittest.IsolatedAsyncioTestCase):
    def test_company_create_allows_none_company_key(self):
        c = CompanyCreate(company_name="Hospital Universitario")
        self.assertIsNone(c.company_key)
        self.assertEqual(c.company_name, "Hospital Universitario")

    def test_company_create_with_valid_key(self):
        c = CompanyCreate(company_name="Hospital Universitario", company_key="hosp-univ_1")
        self.assertEqual(c.company_key, "hosp-univ_1")

    def test_company_create_with_invalid_key(self):
        with self.assertRaises(ValidationError):
            CompanyCreate(company_name="Hospital", company_key="-invalid_start")

    def test_slugify_company_name(self):
        self.assertEqual(_slugify_company_name("Clínica Madrid"), "clinica-madrid")
        self.assertEqual(_slugify_company_name("  Dental & Health Care   "), "dental-health-care")
        self.assertEqual(_slugify_company_name("123 Healthcare"), "123-healthcare")
        self.assertEqual(_slugify_company_name("Ñandú Médica"), "nandu-medica")

    async def test_slug_collision_increments(self):
        mock_db = AsyncMock()
        # Simulate: 'clinica-madrid' exists (returns id=1), 'clinica-madrid-2' exists (returns id=2), 'clinica-madrid-3' free (returns None)
        mock_res_1 = MagicMock()
        mock_res_1.scalar.return_value = 1
        mock_res_2 = MagicMock()
        mock_res_2.scalar.return_value = 2
        mock_res_3 = MagicMock()
        mock_res_3.scalar.return_value = None
        mock_db.execute.side_effect = [mock_res_1, mock_res_2, mock_res_3]

        key = await _generate_unique_company_key(mock_db, "Clínica Madrid")
        self.assertEqual(key, "clinica-madrid-3")

    async def test_create_company_retry_on_concurrent_integrity_error(self):
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        # 1. dup_stmt for name check -> None
        res_name_check = MagicMock()
        res_name_check.scalars.return_value.first.return_value = None

        # 2. _generate_unique_company_key 1st time -> clinica-madrid
        res_key_check_1 = MagicMock()
        res_key_check_1.scalar.return_value = None

        # 3. _generate_unique_company_key 2nd time -> clinica-madrid-2
        res_key_check_2 = MagicMock()
        res_key_check_2.scalar.return_value = None

        mock_db.execute.side_effect = [res_name_check, res_key_check_1, res_key_check_2]

        # First commit fails with company_key IntegrityError, second succeeds
        orig_err = Exception("duplicate key value violates unique constraint 'bm_companies_company_key_key'")
        integ_err = IntegrityError("statement", "params", orig_err)
        mock_db.commit.side_effect = [integ_err, None]

        context = TenantContext(user_id=1, is_super_admin=True, raw_role="super_admin", normalized_role=InternalRole.SUPER_ADMIN)
        payload = CompanyCreate(company_name="Clínica Madrid")

        with patch("app.routers.companies._build_admin_company_response") as mock_build:
            mock_build.return_value = MagicMock()
            await create_company(payload, context, mock_db)

        # Assert rollback was called on collision and commit was retried
        self.assertEqual(mock_db.rollback.call_count, 1)
        self.assertEqual(mock_db.commit.call_count, 2)

    async def test_patch_company_name_preserves_company_key(self):
        mock_db = AsyncMock()
        existing_company = Company(
            company_id=5,
            company_name="Antiguo Nombre",
            company_key="clave-original",
            is_active=True
        )
        res_find = MagicMock()
        res_find.scalar.return_value = existing_company
        res_name_dup = MagicMock()
        res_name_dup.scalar.return_value = None
        mock_db.execute.side_effect = [res_find, res_name_dup]

        context = TenantContext(user_id=1, is_super_admin=True, raw_role="super_admin", normalized_role=InternalRole.SUPER_ADMIN)
        payload = CompanyUpdate(company_name="Nuevo Nombre", company_key=None)

        with patch("app.routers.companies._build_admin_company_response") as mock_build:
            mock_build.return_value = MagicMock()
            await update_company(5, payload, context, mock_db)

        # company_key must NOT change
        self.assertEqual(existing_company.company_name, "Nuevo Nombre")
        self.assertEqual(existing_company.company_key, "clave-original")


class TestAgentCodeSecurityAndEndpoint(unittest.IsolatedAsyncioTestCase):
    def test_endpoint_signature_strict_scoping(self):
        sig = inspect.signature(get_my_agent_code)
        params = list(sig.parameters.keys())
        # ONLY current_user and db allowed
        self.assertEqual(params, ["current_user", "db"])
        self.assertNotIn("user_id", params)
        self.assertNotIn("owner_id", params)
        self.assertNotIn("hubspot_owner_id", params)

    async def test_user_without_hubspot_owner_id(self):
        mock_db = AsyncMock()
        user = User(user_id=10, username="test_user", hubspot_owner_id=None)
        res = await get_my_agent_code(user, mock_db)
        self.assertIsNone(res.agent_code)
        self.assertFalse(res.enabled)

    async def test_user_without_setting_record(self):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        user = User(user_id=10, username="test_user", hubspot_owner_id="33013276")
        res = await get_my_agent_code(user, mock_db)
        self.assertIsNone(res.agent_code)
        self.assertFalse(res.enabled)

    async def test_case_a_numeric_and_alpha_returns_numeric(self):
        # A. training_numeric_code="7777", training_code="CM77" -> agent_code="7777", enabled=True
        mock_db = AsyncMock()
        setting = TrainingAgentSetting(
            hubspot_owner_id="111111",
            agent_name="Cristina Montenegro",
            agent_initials="CM",
            training_numeric_code="7777",
            training_code="CM77",
            is_enabled=True,
            training_code_enabled=True,
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = setting
        mock_db.execute.return_value = mock_result

        user = User(user_id=1, username="cristina", hubspot_owner_id="111111")
        res = await get_my_agent_code(user, mock_db)
        self.assertEqual(res.agent_code, "7777")
        self.assertTrue(res.enabled)

    async def test_case_b_none_numeric_with_alpha_returns_null_disabled(self):
        # B. training_numeric_code=None, training_code="CM77" -> agent_code=null, enabled=false
        mock_db = AsyncMock()
        setting = TrainingAgentSetting(
            hubspot_owner_id="111111",
            agent_name="Cristina Montenegro",
            agent_initials="CM",
            training_numeric_code=None,
            training_code="CM77",
            is_enabled=True,
            training_code_enabled=True,
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = setting
        mock_db.execute.return_value = mock_result

        user = User(user_id=1, username="cristina", hubspot_owner_id="111111")
        res = await get_my_agent_code(user, mock_db)
        self.assertIsNone(res.agent_code)
        self.assertFalse(res.enabled)

    async def test_case_c_empty_numeric_with_alpha_returns_null_disabled(self):
        # C. training_numeric_code="", training_code="CM77" -> agent_code=null, enabled=false
        mock_db = AsyncMock()
        setting = TrainingAgentSetting(
            hubspot_owner_id="111111",
            agent_name="Cristina Montenegro",
            agent_initials="CM",
            training_numeric_code="",
            training_code="CM77",
            is_enabled=True,
            training_code_enabled=True,
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = setting
        mock_db.execute.return_value = mock_result

        user = User(user_id=1, username="cristina", hubspot_owner_id="111111")
        res = await get_my_agent_code(user, mock_db)
        self.assertIsNone(res.agent_code)
        self.assertFalse(res.enabled)

    async def test_case_d_numeric_present_but_setting_disabled(self):
        # D. training_numeric_code="7777" pero setting deshabilitado -> agent_code="7777", enabled=false
        mock_db = AsyncMock()
        setting = TrainingAgentSetting(
            hubspot_owner_id="111111",
            agent_name="Cristina Montenegro",
            agent_initials="CM",
            training_numeric_code="7777",
            training_code="CM77",
            is_enabled=True,
            training_code_enabled=False,  # code disabled
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = setting
        mock_db.execute.return_value = mock_result

        user = User(user_id=1, username="cristina", hubspot_owner_id="111111")
        res = await get_my_agent_code(user, mock_db)
        self.assertEqual(res.agent_code, "7777")
        self.assertFalse(res.enabled)

    async def test_case_e_user_a_cannot_query_user_b(self):
        # E. usuario A resuelve únicamente su configuración; no puede consultar código de B
        mock_db = AsyncMock()
        setting_a = TrainingAgentSetting(
            hubspot_owner_id="111111",
            agent_name="Agente A",
            agent_initials="AA",
            training_numeric_code="7777",
            training_code="AA77",
            is_enabled=True,
            training_code_enabled=True,
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = setting_a
        mock_db.execute.return_value = mock_result

        user_a = User(user_id=1, username="user_a", hubspot_owner_id="111111")
        res = await get_my_agent_code(user_a, mock_db)

        self.assertEqual(res.agent_code, "7777")
        stmt = mock_db.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        self.assertIn("111111", compiled)
        self.assertNotIn("222222", compiled)


class TestAnalyticsItemsIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_permission_403_on_unauthorized_service_id(self):
        mock_db = AsyncMock()
        context = TenantContext(
            user_id=2,
            is_super_admin=False,
            raw_role="service_manager",
            normalized_role=InternalRole.SERVICE_MANAGER,
            allowed_company_ids=[1],
            allowed_service_ids=[1]  # Only Service 1 allowed
        )
        with patch("app.routers.analytics.resolve_service_id", return_value=(2, "expac")):
            with self.assertRaises(HTTPException) as ctx:
                await get_analytics_items(context, mock_db, service_id=2)
            self.assertEqual(ctx.exception.status_code, 403)

    async def test_service_isolation_queries(self):
        mock_db = AsyncMock()
        res_mass = MagicMock()
        res_mass.all.return_value = [("crit_front", "Criterio Front", "score")]
        res_single = MagicMock()
        res_single.all.return_value = []
        res_prompt = MagicMock()
        res_prompt.all.return_value = [("prompt_front", "Prompt Front", "score")]
        mock_db.execute.side_effect = [res_mass, res_single, res_prompt]

        # Service 1
        metrics_s1 = await get_all_metrics(mock_db, service_id=1)
        keys_s1 = {m["key"] for m in metrics_s1}

        self.assertIn("crit_front", keys_s1)
        self.assertIn("prompt_front", keys_s1)
        # Fallback must NOT be added when service_id is specified
        self.assertNotIn("saludo_inicio", keys_s1)


class TestCriteriaAiThresholdFunctional(unittest.IsolatedAsyncioTestCase):
    async def test_functional_truncation_controls(self):
        mock_db = AsyncMock()

        # A. 6,000 chars response -> NOT truncated
        text_6k = "Dimensiones:\n" + ("Este es un criterio extendido de evaluación con mucho detalle.\n" * 100)
        self.assertGreater(len(text_6k), 5500)
        self.assertLess(len(text_6k), 7000)

        with patch("app.services.openai_service.complete_text", new=AsyncMock(return_value=text_6k)):
            body = AIDescriptionRequest(
                criterion_name="Prueba",
                criterion_type="score_1_10",
                output_key="prueba_score",
                instruction="ampliar"
            )
            resp = await generate_criterion_description_ai(mock_db, criterion_id=1, body=body)
            self.assertNotIn("[Descripción truncada", resp["description"])
            self.assertNotIn("La propuesta se ha recortado para mantener una longitud manejable.", resp["warnings"])
            self.assertAlmostEqual(len(resp["description"]), len(text_6k), delta=50)

        # B. 20,000 chars response -> NOT truncated
        text_20k = "Dimensiones:\n" + ("Reglas de puntuacion detalladas con penalizaciones y casos.\n" * 350)
        self.assertGreater(len(text_20k), 18000)
        self.assertLess(len(text_20k), 22000)

        with patch("app.services.openai_service.complete_text", new=AsyncMock(return_value=text_20k)):
            body = AIDescriptionRequest(
                criterion_name="Prueba",
                criterion_type="score_1_10",
                output_key="prueba_score",
                instruction="ampliar"
            )
            resp = await generate_criterion_description_ai(mock_db, criterion_id=1, body=body)
            self.assertNotIn("[Descripción truncada", resp["description"])
            self.assertNotIn("La propuesta se ha recortado para mantener una longitud manejable.", resp["warnings"])

        # C. 45,000 chars (< 50,000) -> NOT truncated
        text_45k = "Dimensiones:\n" + ("Apartado con guia exhaustiva de calificacion para auditoria.\n" * 700)
        self.assertGreater(len(text_45k), 40000)
        self.assertLess(len(text_45k), 50000)

        with patch("app.services.openai_service.complete_text", new=AsyncMock(return_value=text_45k)):
            body = AIDescriptionRequest(
                criterion_name="Prueba",
                criterion_type="score_1_10",
                output_key="prueba_score",
                instruction="ampliar"
            )
            resp = await generate_criterion_description_ai(mock_db, criterion_id=1, body=body)
            self.assertNotIn("[Descripción truncada", resp["description"])
            self.assertNotIn("La propuesta se ha recortado para mantener una longitud manejable.", resp["warnings"])

        # D. 55,000 chars (> 50,000) -> TRUNCATED safely
        text_55k = "Dimensiones:\n\n" + ("Texto que excede el limite maximo de cincuenta mil caracteres. \n\n" * 850)
        self.assertGreater(len(text_55k), 52000)

        with patch("app.services.openai_service.complete_text", new=AsyncMock(return_value=text_55k)):
            body = AIDescriptionRequest(
                criterion_name="Prueba",
                criterion_type="score_1_10",
                output_key="prueba_score",
                instruction="ampliar"
            )
            resp = await generate_criterion_description_ai(mock_db, criterion_id=1, body=body)
            self.assertIn("[Descripción truncada por exceder la longitud máxima.]", resp["description"])
            self.assertIn("La propuesta se ha recortado para mantener una longitud manejable.", resp["warnings"])
            self.assertLessEqual(len(resp["description"]), MAX_AI_CRITERION_DESCRIPTION_CHARS + 100)


if __name__ == "__main__":
    unittest.main()
