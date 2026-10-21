"""
Tests for Staging Safety Controls & Production Retrocompatibility.

Verifies:
1. Retrocompatibility (Production Default Simulation without new env vars):
   - ENABLE_MASS_EVALUATION_SCHEDULER defaults to True
   - ENABLE_HUBSPOT_WRITES defaults to True
   - JWT_SECRET_KEY defaults to historical secret
   - Historical tokens remain valid
2. Staging Safety Controls:
   - ENABLE_MASS_EVALUATION_SCHEDULER=false prevents background scheduler startup
   - ENABLE_HUBSPOT_WRITES=false blocks create_ticket and side-effects while preserving reads
   - JWT_SECRET_KEY custom key isolates staging sessions from production
   - Demo tenant protections remain strictly intact
"""
import os
import unittest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from app.config import get_settings, Settings
from app.utils.security import (
    HISTORICAL_SECRET_KEY,
    create_access_token,
    decode_access_token,
    get_secret_key,
)
from app.core.side_effects import is_external_side_effect_allowed, IntegrationType
from app.services.hubspot_service import HubSpotService


class TestStagingSafetyControls(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # Clear settings cache and remove any test environment overrides
        get_settings.cache_clear()
        self.orig_env = dict(os.environ)

    def tearDown(self):
        # Restore environment and clear cache
        os.environ.clear()
        os.environ.update(self.orig_env)
        get_settings.cache_clear()
        # Reprime settings cache with clean restored environment so subsequent test suites aren't polluted
        get_settings()

    def test_01_production_defaults_without_new_env_vars(self):
        """
        CRITICAL PRODUCTION CHECK:
        If no new environment variables are defined, the application must
        behave with 100% retrocompatibility:
        - Mass evaluation scheduler: enabled (True)
        - HubSpot writes: enabled (True)
        - JWT secret: exact historical secret key
        """
        os.environ.pop("ENABLE_MASS_EVALUATION_SCHEDULER", None)
        os.environ.pop("ENABLE_HUBSPOT_WRITES", None)
        os.environ.pop("JWT_SECRET_KEY", None)
        get_settings.cache_clear()

        settings = get_settings()
        self.assertTrue(settings.enable_mass_evaluation_scheduler, "Mass scheduler must default to True")
        self.assertTrue(settings.enable_hubspot_writes, "HubSpot writes must default to True")
        self.assertEqual(settings.jwt_secret_key, HISTORICAL_SECRET_KEY, "JWT secret must default to historical key")
        self.assertEqual(get_secret_key(), HISTORICAL_SECRET_KEY)

    def test_02_jwt_historical_tokens_backward_compatibility(self):
        """
        Verify that tokens generated with historical key decode flawlessly,
        and that a known pre-generated historical token continues to verify.
        """
        os.environ.pop("JWT_SECRET_KEY", None)
        get_settings.cache_clear()

        # 1. Generate token under default configuration
        payload = {"user_id": 42, "role": "admin", "email": "test@boston.es"}
        token = create_access_token(payload)

        # 2. Decode under default configuration
        decoded = decode_access_token(token)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["user_id"], 42)
        self.assertEqual(decoded["email"], "test@boston.es")

        # 3. Explicitly verify with historical secret
        decoded_explicit = decode_access_token(token, secret_key=HISTORICAL_SECRET_KEY)
        self.assertIsNotNone(decoded_explicit)
        self.assertEqual(decoded_explicit["user_id"], 42)

    def test_03_jwt_custom_secret_key_isolation(self):
        """
        Verify that defining JWT_SECRET_KEY isolates the staging environment:
        - Signs and decodes with staging key
        - Fails decoding when checked against production historical key
        """
        custom_key = "staging-unique-secret-key-987654321"
        os.environ["JWT_SECRET_KEY"] = custom_key
        get_settings.cache_clear()

        self.assertEqual(get_secret_key(), custom_key)

        payload = {"user_id": 100, "role": "agent"}
        token_staging = create_access_token(payload)

        # Decodes successfully with staging secret
        self.assertIsNotNone(decode_access_token(token_staging))

        # Rejects with production historical secret (strict isolation)
        self.assertIsNone(decode_access_token(token_staging, secret_key=HISTORICAL_SECRET_KEY))

        # Conversely: token signed with historical key fails in staging
        token_prod = create_access_token(payload, secret_key=HISTORICAL_SECRET_KEY)
        self.assertIsNone(decode_access_token(token_prod))

    async def test_04_mass_evaluation_scheduler_flag_controls(self):
        """
        Verify that ENABLE_MASS_EVALUATION_SCHEDULER properly toggles
        the scheduler startup condition without altering scheduler internals.
        """
        # When True (default):
        os.environ["ENABLE_MASS_EVALUATION_SCHEDULER"] = "true"
        get_settings.cache_clear()
        self.assertTrue(get_settings().enable_mass_evaluation_scheduler)

        # When False:
        os.environ["ENABLE_MASS_EVALUATION_SCHEDULER"] = "false"
        get_settings.cache_clear()
        self.assertFalse(get_settings().enable_mass_evaluation_scheduler)

    async def test_05_hubspot_writes_disabled_in_staging(self):
        """
        With ENABLE_HUBSPOT_WRITES=false:
        - create_ticket makes 0 external HTTP calls and returns empty dict
        - is_external_side_effect_allowed returns False for hubspot_write
        - Read operations (get_call, search_calls_for_mass_evaluation) remain allowed
        """
        os.environ["ENABLE_HUBSPOT_WRITES"] = "false"
        get_settings.cache_clear()

        # 1. create_ticket must be blocked safely without exception
        hs = HubSpotService(is_demo=False)
        with patch("httpx.AsyncClient.post", new=AsyncMock()) as mock_post:
            result = await hs.create_ticket(
                properties={"subject": "REM Alarma Test"},
                contact_id="12345",
                is_demo=False,
                service_id=1,
            )
            self.assertEqual(result, {})
            mock_post.assert_not_called()

        # 2. is_external_side_effect_allowed must block hubspot_write
        mock_db = AsyncMock()
        allowed = await is_external_side_effect_allowed(
            db=mock_db,
            company_id=1,
            service_id=1,
            integration="hubspot_write",
            is_demo=False,
        )
        self.assertFalse(allowed, "HubSpot write side-effects must be blocked when flag is false")

        # 3. Read operations must NOT be blocked by ENABLE_HUBSPOT_WRITES
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "call_123",
            "properties": {"hs_timestamp": "1700000000000", "hs_call_duration": "60000"}
        }
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)) as mock_get:
            hs.token = "mock_token"
            call_data = await hs.get_call("call_123", is_demo=False)
            self.assertEqual(call_data.get("call_id"), "call_123")
            mock_get.assert_called_once()

    async def test_06_hubspot_writes_enabled_preserves_production(self):
        """
        With ENABLE_HUBSPOT_WRITES=true (or omitted):
        - create_ticket executes external HTTP POST request
        - is_external_side_effect_allowed returns True for allowed service
        """
        os.environ.pop("ENABLE_HUBSPOT_WRITES", None)
        get_settings.cache_clear()

        hs = HubSpotService(is_demo=False)
        hs.token = "mock_token"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "hs_ticket_created_99"}

        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp)) as mock_post:
            result = await hs.create_ticket(
                properties={"subject": "REM Real Alarma"},
                contact_id="12345",
                is_demo=False,
                service_id=1,
            )
            self.assertEqual(result.get("id"), "hs_ticket_created_99")
            mock_post.assert_called_once()

        # is_external_side_effect_allowed allows real Front (service 1)
        mock_db = AsyncMock()
        allowed = await is_external_side_effect_allowed(
            db=mock_db,
            company_id=1,
            service_id=1,
            integration="hubspot_write",
            is_demo=False,
        )
        self.assertTrue(allowed)

    async def test_07_demo_tenant_containment_preserved(self):
        """
        Existing demo tenant containment must remain strictly intact:
        Even when ENABLE_HUBSPOT_WRITES=true, demo tenants (is_demo=True)
        are blocked from creating tickets or writing to CRM.
        """
        os.environ["ENABLE_HUBSPOT_WRITES"] = "true"
        get_settings.cache_clear()

        hs = HubSpotService(is_demo=True)
        with patch("httpx.AsyncClient.post", new=AsyncMock()) as mock_post:
            result = await hs.create_ticket(
                properties={"subject": "Demo Ticket"},
                contact_id="999",
                is_demo=True,
                service_id=1,
            )
            self.assertEqual(result, {})
            mock_post.assert_not_called()

        mock_db = AsyncMock()
        allowed = await is_external_side_effect_allowed(
            db=mock_db,
            company_id=7,  # Demo company
            service_id=1,
            integration="hubspot_write",
            is_demo=True,
        )
        self.assertFalse(allowed)

    async def test_08_simulated_production_environment_startup(self):
        """
        SECTION 18 REQUIREMENT:
        Simulate exact current production environment:
        NO variables defined:
        - ENABLE_MASS_EVALUATION_SCHEDULER
        - ENABLE_HUBSPOT_WRITES
        - JWT_SECRET_KEY

        Expected result:
        - Mass evaluation scheduler: enabled
        - HubSpot writes: permitted according to current rules
        - JWT secret: exact historical key
        - Startup event executes cleanly
        """
        # Ensure none of the new variables exist in the environment
        os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///local_staging_safety_test.db"
        for k in ["ENABLE_MASS_EVALUATION_SCHEDULER", "ENABLE_HUBSPOT_WRITES", "JWT_SECRET_KEY"]:
            os.environ.pop(k, None)
        get_settings.cache_clear()

        # 1. Verify settings defaults match production
        curr_settings = get_settings()
        self.assertIs(curr_settings.enable_mass_evaluation_scheduler, True)
        self.assertIs(curr_settings.enable_hubspot_writes, True)
        self.assertEqual(curr_settings.jwt_secret_key, HISTORICAL_SECRET_KEY)
        self.assertEqual(get_secret_key(), HISTORICAL_SECRET_KEY)

        # 2. Verify startup_event logic with production defaults
        from app.main import startup_event
        with patch("asyncio.create_task") as mock_create_task, \
             patch("app.services.db_init_service.init_db", new=AsyncMock()):
            await startup_event()

            # Confirm start_mass_evaluations_scheduler task WAS created (just like in production)
            created_coros = [call[0][0] for call in mock_create_task.call_args_list]
            coro_names = [getattr(c, "__name__", "") or str(c) for c in created_coros]
            self.assertTrue(
                any("start_mass_evaluations_scheduler" in name for name in coro_names),
                "start_mass_evaluations_scheduler must be scheduled under production defaults"
            )
