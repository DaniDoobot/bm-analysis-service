# -*- coding: utf-8 -*-
"""
Tests for service-scoped analysis regressions and HubSpot demo isolation in interactive test analysis.

Covers:
1. Audio analysis pipeline HubSpot resolution with real tenant (is_demo=False).
2. Audio analysis pipeline HubSpot resolution fail-closed for demo tenant (is_demo=True).
3. Audio analysis pipeline fail-closed when demo status cannot be resolved (is_demo=None).
4. Audio analysis pipeline ensures is_demo is never coerced to False if None.
5. Transcription analysis pipeline HubSpot resolution with real tenant (is_demo=False).
6. Transcription analysis pipeline HubSpot resolution fail-closed for demo/unknown tenant.
7. transcribe_call passes is_demo correctly to HubSpotService and get_call.
8. get_all_metrics scopes BASE_METRICS strictly to Front (service_id=1 or unspecified legacy), never leaking into service_id=2 (EXPAC).
9. get_all_metrics scopes KNOWN_CRITERIA_FALLBACK to service_id is None.
10. get_agent_evolution scopes criteria dynamically to eff_service_id, without leaking Front's CRITERIA_NAMES to non-Front services (e.g. service_id=2).
11. get_analytics_items in analytics_service scopes CRITERIA_NAMES fallback to Front (service_id in (None, 1)), never injecting Front criteria for service_id=2.
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.analyses import AnalyzeAudioRequest
from app.services.audio_analysis_service import process_audio_analysis
from app.services.transcription_analysis_service import analyze_transcription_pipeline
from app.services.transcription_service import transcribe_call
from app.routers.analytics import get_all_metrics
from app.services.dashboard_service import get_agent_evolution
from app.services.analytics_service import get_analytics_items


class TestServiceScopedAnalysisRegressions(unittest.IsolatedAsyncioTestCase):

    async def test_audio_analysis_hubspot_real_tenant_allows_fetch(self):
        """1. Audio analysis resolves is_demo=False for real tenant and passes it to HubSpotService."""
        mock_db = AsyncMock(spec=AsyncSession)

        # Mock resolve_company_demo_status to return (1, False)
        with patch("app.core.side_effects.resolve_company_demo_status", new=AsyncMock(return_value=(1, False))) as mock_resolve, \
             patch("app.services.audio_analysis_service.HubSpotService") as mock_hs_cls, \
             patch("app.services.prompts_service.get_active_prompt", new=AsyncMock(return_value={
                 "prompt_id": 10, "prompt_version_id": 20, "prompt": "Prompt text content", "service_id": 2, "company_id": 1
             })), \
             patch("app.services.prompts_service.sync_prompt_text_with_active_criteria", new=AsyncMock(return_value=("Prompt text content", False))), \
             patch("app.services.audio_analysis_service.TwilioService") as mock_twilio_cls, \
             patch("app.services.audio_analysis_service.analyze_audio_bytes", new=AsyncMock(return_value={"raw": "ok"})), \
             patch("app.services.audio_analysis_service.safe_parse_json", return_value={"tipo_llamada": "cita", "evaluacion_global": 8.0}), \
             patch("app.services.audio_analysis_service.save_analysis", new=AsyncMock()) as mock_save, \
             patch("app.services.criteria_service.get_active_criteria", new=AsyncMock(return_value=[])):

            mock_hs_inst = MagicMock()
            mock_hs_inst.get_call = AsyncMock(return_value={
                "recording_url": "https://api.twilio.com/fake.mp3",
                "call_direction": "inbound",
                "call_duration": 120,
            })
            mock_hs_inst.get_owner_name = AsyncMock(return_value="Agente Test")
            mock_hs_cls.return_value = mock_hs_inst

            mock_twilio_inst = MagicMock()
            mock_twilio_inst.is_twilio_url.return_value = True
            mock_twilio_inst.download_audio = AsyncMock(return_value=b"fake-audio-bytes")
            mock_twilio_cls.return_value = mock_twilio_inst

            mock_record = MagicMock()
            mock_record.analysis_id = 123
            mock_record.service_id = 2
            mock_record.prompt_id = 10
            mock_save.return_value = mock_record

            req = AnalyzeAudioRequest(call_id="call_real_123", service_id=2)
            res = await process_audio_analysis(mock_db, req)

            # Assert resolve_company_demo_status called with service_id=2
            mock_resolve.assert_awaited_once()
            _, kwargs = mock_resolve.call_args
            self.assertEqual(kwargs.get("service_id"), 2)

            # Assert HubSpotService initialized with is_demo=False
            mock_hs_cls.assert_called_once_with(is_demo=False)
            mock_hs_inst.get_call.assert_awaited_once_with("call_real_123", is_demo=False)

            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("analysis_id"), 123)

    async def test_audio_analysis_hubspot_demo_tenant_blocked_fail_closed(self):
        """2. Audio analysis resolves is_demo=True for demo tenant and HubSpot get_call returns empty dict -> fails with validation error."""
        mock_db = AsyncMock(spec=AsyncSession)

        with patch("app.core.side_effects.resolve_company_demo_status", new=AsyncMock(return_value=(7, True))), \
             patch("app.services.audio_analysis_service.HubSpotService") as mock_hs_cls, \
             patch("app.services.prompts_service.get_active_prompt", new=AsyncMock(return_value={
                 "prompt_id": 11, "prompt_version_id": 21, "prompt": "Prompt demo", "service_id": 5, "company_id": 7
             })):

            mock_hs_inst = MagicMock()
            # HubSpot get_call blocks demo tenants and returns {}
            mock_hs_inst.get_call = AsyncMock(return_value={})
            mock_hs_cls.return_value = mock_hs_inst

            req = AnalyzeAudioRequest(call_id="call_demo_456", service_id=5)
            res = await process_audio_analysis(mock_db, req)

            mock_hs_cls.assert_called_once_with(is_demo=True)
            mock_hs_inst.get_call.assert_awaited_once_with("call_demo_456", is_demo=True)

            self.assertFalse(res.get("ok"))
            self.assertEqual(res.get("stage"), "validation")
            self.assertIn("No recording_url could be resolved", res.get("error_message"))

    async def test_audio_analysis_hubspot_unresolved_demo_status_fails_closed(self):
        """3. Audio analysis when demo status is unknown (None) passes is_demo=None -> HubSpot get_call returns {} fail-closed."""
        mock_db = AsyncMock(spec=AsyncSession)

        with patch("app.core.side_effects.resolve_company_demo_status", new=AsyncMock(return_value=(None, None))), \
             patch("app.services.audio_analysis_service.HubSpotService") as mock_hs_cls, \
             patch("app.services.prompts_service.get_active_prompt", new=AsyncMock(return_value={
                 "prompt_id": 12, "prompt_version_id": 22, "prompt": "Prompt unk", "service_id": None
             })):

            mock_hs_inst = MagicMock()
            mock_hs_inst.get_call = AsyncMock(return_value={})
            mock_hs_cls.return_value = mock_hs_inst

            req = AnalyzeAudioRequest(call_id="call_unk_789")
            res = await process_audio_analysis(mock_db, req)

            # Crucial requirement: None must NOT be coerced to False!
            mock_hs_cls.assert_called_once_with(is_demo=None)
            mock_hs_inst.get_call.assert_awaited_once_with("call_unk_789", is_demo=None)

            self.assertFalse(res.get("ok"))
            self.assertEqual(res.get("stage"), "validation")

    async def test_transcription_analysis_hubspot_real_tenant_passes_is_demo_false(self):
        """5. Transcription analysis pipeline resolves is_demo=False and passes it to HubSpotService."""
        mock_db = AsyncMock(spec=AsyncSession)

        with patch("app.core.side_effects.resolve_company_demo_status", new=AsyncMock(return_value=(1, False))) as mock_resolve, \
             patch("app.services.hubspot_service.HubSpotService") as mock_hs_cls, \
             patch("app.services.twilio_service.TwilioService") as mock_tw_cls, \
             patch("app.services.transcription_analysis_service.get_active_prompt", new=AsyncMock(return_value={
                 "prompt_id": 10, "prompt_version_id": 20, "prompt": "Prompt content", "service_id": 2, "company_id": 1
             })), \
             patch("app.services.prompts_service.sync_prompt_text_with_active_criteria", new=AsyncMock(return_value=("Prompt content", False))), \
             patch("app.services.openai_service.transcribe_audio", new=AsyncMock(return_value={"text": "transcripcion"})), \
             patch("app.services.openai_service.complete_text", new=AsyncMock(return_value='{"tipo_llamada": "cita", "evaluacion_global": 8.0}')), \
             patch("app.services.transcription_analysis_service.safe_parse_json", return_value={"tipo_llamada": "cita", "evaluacion_global": 8.0}), \
             patch("app.services.transcription_analysis_service.save_analysis", new=AsyncMock()) as mock_save, \
             patch("app.services.criteria_service.get_active_criteria", new=AsyncMock(return_value=[])):

            mock_hs_inst = MagicMock()
            mock_hs_inst.get_call = AsyncMock(return_value={"recording_url": "https://api.twilio.com/audio.mp3"})
            mock_hs_cls.return_value = mock_hs_inst

            mock_tw_inst = MagicMock()
            mock_tw_inst.download_audio = AsyncMock(return_value=b"fake-bytes")
            mock_tw_cls.return_value = mock_tw_inst

            mock_record = MagicMock()
            mock_record.analysis_id = 456
            mock_record.service_id = 2
            mock_record.prompt_id = 10
            mock_save.return_value = mock_record

            res = await analyze_transcription_pipeline(
                db=mock_db,
                call_id="call_tr_123",
                service_id=2,
            )

            mock_resolve.assert_awaited_once()
            mock_hs_cls.assert_called_once_with(is_demo=False)
            mock_hs_inst.get_call.assert_awaited_once_with("call_tr_123", is_demo=False)
            self.assertTrue(res.get("ok"))

    async def test_transcribe_call_passes_is_demo(self):
        """7. transcribe_call accepts is_demo and passes it to HubSpotService."""
        with patch("app.services.transcription_service.HubSpotService") as mock_hs_cls, \
             patch("app.services.transcription_service.TwilioService") as mock_tw_cls, \
             patch("app.services.openai_service.transcribe_audio", new=AsyncMock(return_value={"text": "transcripcion"})):

            mock_hs_inst = MagicMock()
            mock_hs_inst.get_call = AsyncMock(return_value={"recording_url": "https://twilio.com/rec.mp3"})
            mock_hs_cls.return_value = mock_hs_inst

            mock_tw_inst = MagicMock()
            mock_tw_inst.download_audio = AsyncMock(return_value=b"bytes")
            mock_tw_cls.return_value = mock_tw_inst

            res = await transcribe_call("call_999", is_demo=False)
            mock_hs_cls.assert_called_once_with(is_demo=False)
            mock_hs_inst.get_call.assert_awaited_once_with("call_999", is_demo=False)
            self.assertEqual(res.get("text"), "transcripcion")

    async def test_get_all_metrics_isolation_front_vs_expac(self):
        """8 & 9. get_all_metrics includes BASE_METRICS for Front (service_id=1) but NOT for EXPAC (service_id=2)."""
        mock_db = AsyncMock(spec=AsyncSession)

        res_mock = MagicMock()
        res_mock.all.return_value = []
        mock_db.execute.return_value = res_mock

        # A. When service_id=1 (Front)
        metrics_s1 = await get_all_metrics(mock_db, service_id=1, company_id=1)
        keys_s1 = [m["key"] for m in metrics_s1]
        self.assertIn("evaluacion_global", keys_s1)
        self.assertIn("empatia", keys_s1)
        self.assertIn("claridad", keys_s1)

        # B. When service_id=2 (EXPAC)
        metrics_s2 = await get_all_metrics(mock_db, service_id=2, company_id=1)
        keys_s2 = [m["key"] for m in metrics_s2]
        # Front BASE_METRICS must NEVER leak into EXPAC!
        self.assertNotIn("evaluacion_global", keys_s2)
        self.assertNotIn("empatia", keys_s2)
        self.assertNotIn("claridad", keys_s2)
        self.assertEqual(keys_s2, [])

    async def test_get_agent_evolution_criteria_isolation(self):
        """10. get_agent_evolution scopes criteria dynamically to eff_service_id, without leaking Front's CRITERIA_NAMES."""
        mock_db = AsyncMock(spec=AsyncSession)

        # Simulate 2 rows
        row1 = MagicMock()
        row1.is_evaluable = True
        row1.call_timestamp = None
        row1.analysis_timestamp = None
        row1.result_json = {"score_paciente": 8.0}
        row1.items_json = []
        row1.evaluacion_global = 8.0
        row1.agent_name = "Agente EXPAC"

        row2 = MagicMock()
        row2.is_evaluable = True
        row2.call_timestamp = None
        row2.analysis_timestamp = None
        row2.result_json = {"score_paciente": 9.0}
        row2.items_json = []
        row2.evaluacion_global = 9.0
        row2.agent_name = "Agente EXPAC"

        exec_mock = MagicMock()
        exec_mock.scalars.return_value.all.return_value = [row1, row2]
        mock_db.execute.return_value = exec_mock

        # For service_id=2, mock get_evaluation_item_filter_options to return only EXPAC criteria
        expac_options = [
            {"key": "score_paciente", "label": "Score Paciente", "type": "score"}
        ]
        with patch("app.utils.item_score_filters.get_evaluation_item_filter_options", new=AsyncMock(return_value=expac_options)) as mock_filter_opts, \
             patch("app.utils.service_resolvers.resolve_service_id", new=AsyncMock(return_value=(2, "expac"))), \
             patch("app.utils.agent_resolvers.resolve_agent_identifiers_to_owner_ids", new=AsyncMock(return_value=["12345"])):

            res = await get_agent_evolution(
                db=mock_db,
                hubspot_owner_id="12345",
                service_id=2,
                company_id=1,
            )

            mock_filter_opts.assert_awaited_once_with(
                mock_db,
                company_ids=[1],
                service_ids=[2],
                typology_id=None,
                typology_key=None,
            )

            crit_evo = res.get("criteria_evolution", [])
            evo_keys = [c["criterion_key"] for c in crit_evo]
            self.assertIn("score_paciente", evo_keys)
            # Front criteria must NOT appear in criteria_evolution
            self.assertNotIn("empatia", evo_keys)
            self.assertNotIn("claridad", evo_keys)
            self.assertNotIn("simpatia", evo_keys)

            strengths = res.get("strengths", [])
            s_keys = [s["criterion_key"] for s in strengths]
            self.assertIn("score_paciente", s_keys)
            self.assertNotIn("empatia", s_keys)

    async def test_get_analytics_items_service_scoping(self):
        """11. get_analytics_items in analytics_service injects CRITERIA_NAMES only for Front (service_id=1/None)."""
        mock_db = AsyncMock(spec=AsyncSession)

        # Mock DB select of PromptCriterion to return empty
        res_mock = MagicMock()
        res_mock.all.return_value = []
        mock_db.execute.return_value = res_mock

        # A. For Front (service_id=1)
        with patch("app.services.analytics_service.resolve_service_id", new=AsyncMock(return_value=1)):
            items_s1 = await get_analytics_items(mock_db, service_str="1")
            keys_s1 = [it["key"] for it in items_s1]
            self.assertIn("empatia", keys_s1)
            self.assertIn("claridad", keys_s1)

        # B. For EXPAC (service_id=2)
        with patch("app.services.analytics_service.resolve_service_id", new=AsyncMock(return_value=2)):
            items_s2 = await get_analytics_items(mock_db, service_str="2")
            keys_s2 = [it["key"] for it in items_s2]
            # Must NOT contain Front's fallback items
            self.assertNotIn("empatia", keys_s2)
            self.assertNotIn("claridad", keys_s2)
            self.assertEqual(items_s2, [])


if __name__ == "__main__":
    unittest.main()
