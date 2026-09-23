"""
tests/test_trainer_context_isolation.py
=======================================
Unit and integration tests verifying context isolation in Trainer module:
1. Neutrality for Empresa Demo and general companies (no Boston Medical, no paciente, no medical terms).
2. Healthcare context preservation for Boston Medical Group (paciente, clinical rules).
3. Strict company isolation in validate_simulation_for_agent and start_phone_session.
4. AI prompt generator neutrality for non-healthcare services.
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select

from app.routers import trainer_voice
from app.routers.trainer_voice import (
    IDENTIFICATION_SYSTEM_INSTRUCTION,
    SPANISH_VOICE_RULES,
    HEALTHCARE_VOICE_RULES,
    build_turn_discipline,
)
from app.models.companies import Company
from app.models.services import Service
from app.models.trainer import (
    TrainerSimulation,
    TrainerSession,
    TrainerSimulationVersion,
    TrainerEvaluationConfig,
    TrainerEvaluation,
)
from app.models.prompts import PromptVersion
from app.models.criteria import PromptCriterion
from app.schemas.trainer import AIPromptGenerateRequest
from app.services.trainer_service import TrainerService


class TestTrainerVoiceContextIsolation(unittest.TestCase):
    """Tests verifying that voice instructions dynamically isolate context."""

    def test_identification_system_instruction_is_neutral(self):
        """IDENTIFICATION_SYSTEM_INSTRUCTION must not contain Boston Medical Group or medical terms."""
        self.assertNotIn("Boston Medical", IDENTIFICATION_SYSTEM_INSTRUCTION)
        self.assertNotIn("paciente", IDENTIFICATION_SYSTEM_INSTRUCTION.lower())
        self.assertNotIn("médico", IDENTIFICATION_SYSTEM_INSTRUCTION.lower())
        self.assertNotIn("clinica", IDENTIFICATION_SYSTEM_INSTRUCTION.lower())
        self.assertIn("Asistente de Identificación por Voz", IDENTIFICATION_SYSTEM_INSTRUCTION)

    def test_spanish_voice_rules_base_is_neutral(self):
        """SPANISH_VOICE_RULES must be neutral and suitable for any industry."""
        self.assertNotIn("Boston Medical", SPANISH_VOICE_RULES)
        self.assertNotIn("paciente", SPANISH_VOICE_RULES.lower())
        self.assertNotIn("consejos médicos", SPANISH_VOICE_RULES.lower())
        self.assertNotIn("urgencias", SPANISH_VOICE_RULES.lower())
        self.assertNotIn("profesional sanitario", SPANISH_VOICE_RULES.lower())
        # Must retain brevity rule
        self.assertIn("PRIMERA INTERVENCIÓN BREVE", SPANISH_VOICE_RULES)

    def test_build_turn_discipline_demo_is_neutral(self):
        """build_turn_discipline for non-healthcare must use cliente and neutral objections."""
        td = build_turn_discipline(is_healthcare=False, interlocutor_role="cliente")
        self.assertIn("cliente", td.lower())
        self.assertNotIn("paciente", td.lower())
        self.assertNotIn("tratamiento", td.lower())
        self.assertNotIn("primera cita", td.lower())
        self.assertIn("condiciones del servicio", td.lower())

    def test_build_turn_discipline_healthcare_uses_clinical_context(self):
        """build_turn_discipline for healthcare must use paciente and clinical objections."""
        td = build_turn_discipline(is_healthcare=True, interlocutor_role="paciente")
        self.assertIn("paciente", td.lower())
        self.assertIn("tratamiento", td.lower())
        self.assertIn("primera cita", td.lower())

    def test_healthcare_voice_rules_contains_medical_protections(self):
        """HEALTHCARE_VOICE_RULES must contain explicit medical guardrails for BMG."""
        self.assertIn("PACIENTE", HEALTHCARE_VOICE_RULES)
        self.assertIn("consejos médicos", HEALTHCARE_VOICE_RULES.lower())
        self.assertIn("Boston Medical Group", HEALTHCARE_VOICE_RULES)
        self.assertIn("profesional sanitario", HEALTHCARE_VOICE_RULES.lower())


class TestVoiceSessionInstructionAssembly(unittest.IsolatedAsyncioTestCase):
    """Verify how instruction is constructed during media-stream connection."""

    async def test_demo_company_assembly_has_zero_medical_or_boston_references(self):
        """An Empresa Demo session must not receive ANY Boston Medical, paciente, or medical terms."""
        demo_comp = Company(
            company_id=7,
            company_name="Empresa Demo",
            company_key="empresa-demo",
            is_demo=True,
            sector="customer_service",
        )
        sim = TrainerSimulation(
            simulation_id=102,
            company_id=7,
            service_id=20,
            name="Protocolo de Desescalada con Cliente Furioso",
            code="ATEN02",
            roleplay_prompt="Simula un cliente muy molesto que eleva el tono de voz; el objetivo es desescalar sin perder la compostura.",
            status="published",
        )
        sess = TrainerSession(
            session_id=999,
            simulation_id=102,
            simulation=sim,
            simulation_version_id=None,
        )

        # Simulate the resolution in media-stream
        company = demo_comp
        is_healthcare = False
        if company:
            is_demo = bool(company.is_demo or company.company_key == "empresa-demo")
            if not is_demo:
                c_sector = (company.sector or "").lower()
                c_name = (company.company_name or "").lower()
                c_key = (company.company_key or "").lower()
                if c_sector in ("healthcare", "salud", "medico", "médico", "clinica", "clínica"):
                    is_healthcare = True
                elif "boston" in c_name or "boston" in c_key or company.company_id == 1:
                    is_healthcare = True

        self.assertFalse(is_healthcare, "Empresa Demo must NEVER be flagged as healthcare.")

        interlocutor_role = "paciente" if is_healthcare else "cliente"
        turn_discipline = build_turn_discipline(is_healthcare=is_healthcare, interlocutor_role=interlocutor_role)

        instruction_parts = [sim.roleplay_prompt, turn_discipline, SPANISH_VOICE_RULES]
        if is_healthcare:
            instruction_parts.append(HEALTHCARE_VOICE_RULES)
        instruction = "\n".join(instruction_parts)

        # Strict assertions: zero leaks
        self.assertNotIn("Boston Medical", instruction)
        self.assertNotIn("paciente", instruction.lower())
        self.assertNotIn("médico", instruction.lower())
        self.assertNotIn("urgencias", instruction.lower())
        self.assertNotIn("profesional sanitario", instruction.lower())
        self.assertNotIn("tratamiento", instruction.lower())
        self.assertIn("cliente", instruction.lower())
        self.assertIn("PRIMERA INTERVENCIÓN BREVE", instruction)

    async def test_boston_medical_session_retains_clinical_context(self):
        """A Boston Medical session must retain its healthcare rules and paciente role."""
        bm_comp = Company(
            company_id=1,
            company_name="Boston Medical Group",
            company_key="boston-medical",
            is_demo=False,
            sector="healthcare",
        )
        sim = TrainerSimulation(
            simulation_id=1,
            company_id=1,
            service_id=2,
            name="Primera Consulta Informativa",
            code="MED01",
            roleplay_prompt="Simula una primera consulta donde el paciente tiene dudas sobre el tratamiento.",
            status="published",
        )

        company = bm_comp
        is_healthcare = False
        if company:
            is_demo = bool(company.is_demo or company.company_key == "empresa-demo")
            if not is_demo:
                c_sector = (company.sector or "").lower()
                c_name = (company.company_name or "").lower()
                c_key = (company.company_key or "").lower()
                if c_sector in ("healthcare", "salud", "medico", "médico", "clinica", "clínica"):
                    is_healthcare = True
                elif "boston" in c_name or "boston" in c_key or company.company_id == 1:
                    is_healthcare = True

        self.assertTrue(is_healthcare, "Boston Medical must be flagged as healthcare.")

        interlocutor_role = "paciente" if is_healthcare else "cliente"
        turn_discipline = build_turn_discipline(is_healthcare=is_healthcare, interlocutor_role=interlocutor_role)

        instruction_parts = [sim.roleplay_prompt, turn_discipline, SPANISH_VOICE_RULES]
        if is_healthcare:
            instruction_parts.append(HEALTHCARE_VOICE_RULES)
        instruction = "\n".join(instruction_parts)

        self.assertIn("Boston Medical Group", instruction)
        self.assertIn("PACIENTE", instruction)
        self.assertIn("consejos médicos", instruction.lower())
        self.assertIn("tratamiento", instruction.lower())


class TestCompanyIsolationValidation(unittest.IsolatedAsyncioTestCase):
    """Verify that agents cannot access simulations belonging to another company."""

    async def test_validate_simulation_blocks_cross_company_access(self):
        """Agent from company 7 (Demo) attempting simulation from company 1 (BMG) is blocked."""
        mock_db = AsyncMock()

        bmg_sim = TrainerSimulation(
            simulation_id=1,
            code="MED01",
            company_id=1,  # Boston Medical
            service_id=2,
            status="published",
        )

        with patch.object(TrainerService, "validate_simulation_code", new=AsyncMock(return_value=bmg_sim)):
            with patch.object(TrainerService, "get_agent_service_context", new=AsyncMock(return_value={
                "service_ids": {2},
                "primary_service_name": "Atención",
                "company_id": 7,  # Empresa Demo
            })):
                res = await TrainerService.validate_simulation_for_agent(mock_db, "MED01", "agent_demo_01")
                self.assertFalse(res["valid"])
                self.assertEqual(res["status"], "company_mismatch")
                self.assertIn("otra organización", res["message"])

    async def test_validate_simulation_allows_same_company_access(self):
        """Agent from company 7 accessing simulation from company 7 is allowed."""
        mock_db = AsyncMock()

        demo_sim = TrainerSimulation(
            simulation_id=102,
            code="ATEN02",
            company_id=7,  # Empresa Demo
            service_id=20,
            status="published",
        )

        with patch.object(TrainerService, "validate_simulation_code", new=AsyncMock(return_value=demo_sim)):
            with patch.object(TrainerService, "get_agent_service_context", new=AsyncMock(return_value={
                "service_ids": {20},
                "primary_service_name": "Atención al Cliente",
                "company_id": 7,  # Empresa Demo
            })):
                res = await TrainerService.validate_simulation_for_agent(mock_db, "ATEN02", "agent_demo_01")
                self.assertTrue(res["valid"])
                self.assertEqual(res["status"], "valid")

    async def test_start_phone_session_enforces_company_isolation(self):
        """start_phone_session raises ValueError when cross-company access is attempted."""
        mock_db = AsyncMock()

        mock_agent = {"agent_id": "agent_demo_01", "agent_name": "Agente Demo"}
        bmg_sim = TrainerSimulation(
            simulation_id=1,
            code="MED01",
            company_id=1,
            service_id=2,
            status="published",
        )

        with patch.object(TrainerService, "validate_agent_code", new=AsyncMock(return_value=mock_agent)):
            with patch.object(TrainerService, "validate_simulation_code", new=AsyncMock(return_value=bmg_sim)):
                with patch.object(TrainerService, "validate_simulation_for_agent", new=AsyncMock(return_value={
                    "valid": False,
                    "status": "company_mismatch",
                    "message": "Ese código de simulación pertenece a otra organización.",
                })):
                    with self.assertRaises(ValueError) as ctx:
                        await TrainerService.start_phone_session(mock_db, "AD01", "MED01", "call_123")
                    self.assertIn("otra organización", str(ctx.exception))


class TestAIPromptGeneratorIsolation(unittest.IsolatedAsyncioTestCase):
    """Verify that generate_roleplay_prompt_ai dynamically adapts to service/company."""

    async def test_generate_prompt_demo_service_is_neutral(self):
        """Prompt generator for Empresa Demo must not mention Boston Medical or clinic."""
        mock_db = AsyncMock()
        mock_svc = Service(
            service_id=20,
            service_name="Atención al Cliente Demo",
            company_id=7,
        )
        mock_comp = Company(
            company_id=7,
            company_name="Empresa Demo",
            company_key="empresa-demo",
            is_demo=True,
            sector="general",
        )
        mock_svc.company = mock_comp

        mock_res = MagicMock()
        mock_res.scalars.return_value.first.return_value = mock_svc
        mock_db.execute = AsyncMock(return_value=mock_res)

        captured_messages = []
        async def fake_complete_text(messages, response_format=None):
            captured_messages.extend(messages)
            return "Simula un cliente que llama para consultar su pedido."

        payload = AIPromptGenerateRequest(
            service_id=20,
            objective="Atender reclamación",
            ideas="Cliente enfadado por demora",
            difficulty="media",
            tone="realista",
        )

        with patch("app.services.openai_service.complete_text", new=fake_complete_text):
            result = await TrainerService.generate_roleplay_prompt_ai(payload, db=mock_db)

        sys_content = captured_messages[0]["content"]
        self.assertNotIn("Boston Medical", sys_content)
        self.assertNotIn("clínica", sys_content.lower())
        self.assertNotIn("paciente", sys_content.lower())
        self.assertIn("cliente o interlocutor", sys_content.lower())

    async def test_generate_prompt_healthcare_service_uses_clinical_context(self):
        """Prompt generator for Boston Medical must use clinical/paciente context."""
        mock_db = AsyncMock()
        mock_svc = Service(
            service_id=2,
            service_name="Atención Clínica",
            company_id=1,
        )
        mock_comp = Company(
            company_id=1,
            company_name="Boston Medical Group",
            company_key="boston-medical",
            is_demo=False,
            sector="healthcare",
        )
        mock_svc.company = mock_comp

        mock_res = MagicMock()
        mock_res.scalars.return_value.first.return_value = mock_svc
        mock_db.execute = AsyncMock(return_value=mock_res)

        captured_messages = []
        async def fake_complete_text(messages, response_format=None):
            captured_messages.extend(messages)
            return "Simula un paciente que llama a la clínica."

        payload = AIPromptGenerateRequest(
            service_id=2,
            objective="Dudas sobre tratamiento",
            ideas="Paciente indeciso",
            difficulty="media",
            tone="realista",
        )

        with patch("app.services.openai_service.complete_text", new=fake_complete_text):
            result = await TrainerService.generate_roleplay_prompt_ai(payload, db=mock_db)

        sys_content = captured_messages[0]["content"]
        self.assertIn("paciente", sys_content.lower())
        self.assertIn("Boston Medical Group", sys_content)


class TestTrainerEvaluationContextIsolation(unittest.IsolatedAsyncioTestCase):
    """
    Verify that evaluate_session_task isolates company context:
    - ATEN02 / Empresa Demo must NEVER contain Boston Medical Group, paciente, or clinical rules.
    - Empresa Demo criteria 'Saludo e Identificación' and 'Cierre y Despedida' must be evaluated generically.
    - Boston Medical Group sessions must retain BMG and paciente context.
    - Accidental BMG references in prompt templates are sanitized for Demo simulations.
    """

    def _make_criterion(self, c_id, name, key, out_k, desc):
        c = MagicMock(spec=PromptCriterion)
        c.criterion_id = c_id
        c.criterion_name = name
        c.criterion_key = key
        c.output_key = out_k
        c.feed_key = f"{key}_feedback"
        c.criterion_type = "score_1_10"
        c.criterion_description = desc
        c.is_active = True
        c.deleted_at = None
        c.order_index = c_id * 10
        return c

    async def test_evaluation_prompt_for_aten02_demo_has_zero_boston_or_medical_references(self):
        """ATEN02 / Empresa Demo evaluation prompt must not mention Boston Medical, paciente or clinics."""
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        demo_comp = Company(
            company_id=7,
            company_name="Empresa Demo",
            company_key="empresa-demo",
            is_demo=True,
            sector="customer_service",
        )
        demo_svc = Service(
            service_id=20,
            company_id=7,
            service_name="Atención al Cliente",
            service_key="atencion-al-cliente",
        )
        demo_svc.company = demo_comp

        mock_sim = MagicMock()
        mock_sim.simulation_id = 102
        mock_sim.company_id = 7
        mock_sim.company = demo_comp
        mock_sim.service_id = 20
        mock_sim.service = demo_svc
        mock_sim.name = "Protocolo de Desescalada con Cliente Furioso"
        mock_sim.code = "ATEN02"
        mock_sim.objective = "Desescalar cliente muy molesto sin perder compostura"
        mock_sim.difficulty = "alta"
        mock_sim.roleplay_prompt = "Cliente que eleva el tono de voz exigiendo solución."
        mock_sim.evaluation_config_id = 5

        mock_sess = MagicMock()
        mock_sess.session_id = 888
        mock_sess.simulation_id = 102
        mock_sess.simulation_version_id = None
        mock_sess.company_id = 7
        mock_sess.service_id = 20
        mock_sess.simulation = mock_sim
        mock_sess.transcript = "Agente: Buenos días, le atiende Juan de Empresa Demo. ¿En qué puedo ayudarle? Cliente: ¡Estoy harto de este cobro!"
        mock_sess.recording_url = None
        mock_sess.evaluation_status = "started"

        mock_cfg = MagicMock()
        mock_cfg.config_id = 5
        mock_cfg.company_id = 7
        mock_cfg.service_id = 20
        mock_cfg.speech_structure_id = 15
        mock_cfg.extra_instructions = ""

        mock_pv = MagicMock()
        mock_pv.prompt = "Estructura base de atención al cliente."

        crit1 = self._make_criterion(1, "Saludo e Identificación", "saludo_identificacion", "saludo_identificacion_score", "Saludo institucional formal y presentación clara con nombre y empresa.")
        crit2 = self._make_criterion(2, "Cierre y Despedida", "despedida_protocolo", "despedida_protocolo_score", "Verificación de dudas adicionales y despedida formal impecable.")

        def make_exec(value=None, scalars_list=None):
            r = MagicMock()
            r.scalars.return_value.first.return_value = value
            r.scalars.return_value.all.return_value = scalars_list if scalars_list is not None else []
            return r

        execute_calls = iter([
            make_exec(mock_sess),
            make_exec(mock_sim),
            make_exec(mock_cfg),
            make_exec(mock_pv),
            make_exec(scalars_list=[crit1, crit2]),
        ])
        mock_db.execute = AsyncMock(side_effect=lambda stmt: next(execute_calls))

        captured_messages = []
        async def fake_complete(messages, response_format=None):
            captured_messages.extend(messages)
            return '{"score": 8.0, "feedback": "Buen trato", "strengths": [], "improvement_points": [], "result_json": {"saludo_identificacion_score": 8.0, "despedida_protocolo_score": 8.0}, "criteria_evaluations": []}'

        with patch("app.services.trainer_service.openai_service.complete_text", new=fake_complete):
            await TrainerService.evaluate_session_task(mock_db, 888)

        self.assertTrue(len(captured_messages) > 0)
        sys_prompt = captured_messages[0]["content"]

        # 1. Zero Boston Medical or BMG leaks
        self.assertNotIn("Boston Medical Group", sys_prompt)
        self.assertNotIn("Boston Medical", sys_prompt)
        self.assertNotIn("BMG", sys_prompt)

        # 2. Zero clinical / patient terminology
        self.assertNotIn("paciente", sys_prompt.lower())
        self.assertNotIn("médico", sys_prompt.lower())
        self.assertNotIn("clínica", sys_prompt.lower())
        self.assertNotIn("tratamiento", sys_prompt.lower())
        self.assertNotIn("salud sexual", sys_prompt.lower())

        # 3. Present Empresa Demo and customer service context
        self.assertIn("Empresa Demo", sys_prompt)
        self.assertIn("CLIENTE SIMULADO", sys_prompt)
        self.assertIn("Saludo e Identificación", sys_prompt)
        self.assertIn("Cierre y Despedida", sys_prompt)

        # 4. Explicit isolation instruction forbidding demanding Boston Medical Group
        self.assertIn("AISLAMIENTO CORPORATIVO", sys_prompt)
        self.assertIn("DIRECTRIZ CORPORATIVA", sys_prompt)

    async def test_evaluation_prompt_for_boston_medical_retains_bmg_and_paciente(self):
        """Boston Medical sessions must retain BMG branding and paciente role in evaluation."""
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        bm_comp = Company(
            company_id=1,
            company_name="Boston Medical Group",
            company_key="boston-medical",
            is_demo=False,
            sector="healthcare",
        )
        bm_svc = Service(
            service_id=2,
            company_id=1,
            service_name="Atención Clínica",
            service_key="atencion-clinica",
        )
        bm_svc.company = bm_comp

        mock_sim = MagicMock()
        mock_sim.simulation_id = 1
        mock_sim.company_id = 1
        mock_sim.company = bm_comp
        mock_sim.service_id = 2
        mock_sim.service = bm_svc
        mock_sim.name = "Primera Consulta Médica"
        mock_sim.code = "MED01"
        mock_sim.objective = "Evaluación de paciente potencial"
        mock_sim.difficulty = "media"
        mock_sim.roleplay_prompt = "Paciente con dudas de tratamiento."
        mock_sim.evaluation_config_id = 1

        mock_sess = MagicMock()
        mock_sess.session_id = 777
        mock_sess.simulation_id = 1
        mock_sess.simulation_version_id = None
        mock_sess.company_id = 1
        mock_sess.service_id = 2
        mock_sess.simulation = mock_sim
        mock_sess.transcript = "Agente: Buenos días, clínica Boston Medical. Paciente: Hola, quería información de tratamiento."
        mock_sess.recording_url = None
        mock_sess.evaluation_status = "started"

        mock_cfg = MagicMock()
        mock_cfg.config_id = 1
        mock_cfg.company_id = 1
        mock_cfg.service_id = 2
        mock_cfg.speech_structure_id = 10
        mock_cfg.extra_instructions = ""

        mock_pv = MagicMock()
        mock_pv.prompt = "Estructura clínica BMG."

        crit1 = self._make_criterion(1, "Saludo Clínico", "saludo_clinico", "saludo_clinico_score", "El agente se identifica como Boston Medical.")

        def make_exec(value=None, scalars_list=None):
            r = MagicMock()
            r.scalars.return_value.first.return_value = value
            r.scalars.return_value.all.return_value = scalars_list if scalars_list is not None else []
            return r

        execute_calls = iter([
            make_exec(mock_sess),
            make_exec(mock_sim),
            make_exec(mock_cfg),
            make_exec(mock_pv),
            make_exec(scalars_list=[crit1]),
        ])
        mock_db.execute = AsyncMock(side_effect=lambda stmt: next(execute_calls))

        captured_messages = []
        async def fake_complete(messages, response_format=None):
            captured_messages.extend(messages)
            return '{"score": 9.0, "feedback": "Excelente", "strengths": [], "improvement_points": [], "result_json": {"saludo_clinico_score": 9.0}, "criteria_evaluations": []}'

        with patch("app.services.trainer_service.openai_service.complete_text", new=fake_complete):
            await TrainerService.evaluate_session_task(mock_db, 777)

        self.assertTrue(len(captured_messages) > 0)
        sys_prompt = captured_messages[0]["content"]

        self.assertIn("Boston Medical Group", sys_prompt)
        self.assertIn("PACIENTE SIMULADO", sys_prompt)

    async def test_evaluation_prompt_sanitizes_accidental_bmg_in_demo_prompt_template(self):
        """Accidental mentions of Boston Medical in speech structure templates must be sanitized for Demo."""
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        demo_comp = Company(
            company_id=7,
            company_name="Empresa Demo",
            company_key="empresa-demo",
            is_demo=True,
            sector="customer_service",
        )

        mock_sim = MagicMock()
        mock_sim.simulation_id = 101
        mock_sim.company_id = 7
        mock_sim.company = demo_comp
        mock_sim.service_id = 20
        mock_sim.name = "Gestión de Reclamación de Facturación Compleja"
        mock_sim.code = "ATEN01"
        mock_sim.objective = "Reclamación"
        mock_sim.difficulty = "media"
        mock_sim.roleplay_prompt = "Cliente disgustado."
        mock_sim.evaluation_config_id = 5

        mock_sess = MagicMock()
        mock_sess.session_id = 666
        mock_sess.simulation_id = 101
        mock_sess.simulation_version_id = None
        mock_sess.company_id = 7
        mock_sess.transcript = "Hola"
        mock_sess.recording_url = None
        mock_sess.evaluation_status = "started"

        mock_cfg = MagicMock()
        mock_cfg.config_id = 5
        mock_cfg.company_id = 7
        mock_cfg.speech_structure_id = 15
        mock_cfg.extra_instructions = "Asegurar protocolo de Boston Medical Group."

        mock_pv = MagicMock()
        mock_pv.prompt = "Metaprompt heredado de Boston Medical Group para llamadas."

        crit1 = self._make_criterion(1, "Saludo", "saludo", "saludo_score", "Saludo")

        def make_exec(value=None, scalars_list=None):
            r = MagicMock()
            r.scalars.return_value.first.return_value = value
            r.scalars.return_value.all.return_value = scalars_list if scalars_list is not None else []
            return r

        execute_calls = iter([
            make_exec(mock_sess),
            make_exec(mock_sim),
            make_exec(mock_cfg),
            make_exec(mock_pv),
            make_exec(scalars_list=[crit1]),
        ])
        mock_db.execute = AsyncMock(side_effect=lambda stmt: next(execute_calls))

        captured_messages = []
        async def fake_complete(messages, response_format=None):
            captured_messages.extend(messages)
            return '{"score": 8.0, "feedback": "Ok", "strengths": [], "improvement_points": [], "result_json": {"saludo_score": 8.0}, "criteria_evaluations": []}'

        with patch("app.services.trainer_service.openai_service.complete_text", new=fake_complete):
            await TrainerService.evaluate_session_task(mock_db, 666)

        sys_prompt = captured_messages[0]["content"]
        self.assertNotIn("Boston Medical Group", sys_prompt)
        self.assertIn("Empresa Demo", sys_prompt)

    async def test_evaluation_prompt_for_third_party_company_not_overridden_by_code_prefix(self):
        """A non-demo company with an ATEN code prefix is NEVER misclassified as Empresa Demo."""
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        acme_comp = Company(
            company_id=15,
            company_name="Acme Telecom",
            company_key="acme-telecom",
            is_demo=False,
            sector="telecom",
        )
        acme_svc = Service(
            service_id=50,
            company_id=15,
            service_name="Soporte Técnico Acme",
            service_key="soporte-acme",
        )
        acme_svc.company = acme_comp

        mock_sim = MagicMock()
        mock_sim.simulation_id = 501
        mock_sim.company_id = 15
        mock_sim.company = acme_comp
        mock_sim.service_id = 50
        mock_sim.service = acme_svc
        mock_sim.name = "Atención averías"
        mock_sim.code = "ATEN-ACME-01"
        mock_sim.objective = "Resolver avería"
        mock_sim.difficulty = "media"
        mock_sim.roleplay_prompt = "Cliente con corte de fibra."
        mock_sim.evaluation_config_id = 50

        mock_sess = MagicMock()
        mock_sess.session_id = 555
        mock_sess.simulation_id = 501
        mock_sess.simulation_version_id = None
        mock_sess.company_id = 15
        mock_sess.service_id = 50
        mock_sess.simulation = mock_sim
        mock_sess.transcript = "Hola"
        mock_sess.recording_url = None
        mock_sess.evaluation_status = "started"

        mock_cfg = MagicMock()
        mock_cfg.config_id = 50
        mock_cfg.company_id = 15
        mock_cfg.service_id = 50
        mock_cfg.speech_structure_id = 50
        mock_cfg.extra_instructions = ""

        mock_pv = MagicMock()
        mock_pv.prompt = "Estructura soporte telecom."

        crit1 = self._make_criterion(1, "Saludo", "saludo", "saludo_score", "Saludo")

        def make_exec(value=None, scalars_list=None):
            r = MagicMock()
            r.scalars.return_value.first.return_value = value
            r.scalars.return_value.all.return_value = scalars_list if scalars_list is not None else []
            return r

        execute_calls = iter([
            make_exec(mock_sess),
            make_exec(mock_sim),
            make_exec(mock_cfg),
            make_exec(mock_pv),
            make_exec(scalars_list=[crit1]),
        ])
        mock_db.execute = AsyncMock(side_effect=lambda stmt: next(execute_calls))

        captured_messages = []
        async def fake_complete(messages, response_format=None):
            captured_messages.extend(messages)
            return '{"score": 8.0, "feedback": "Ok", "strengths": [], "improvement_points": [], "result_json": {"saludo_score": 8.0}, "criteria_evaluations": []}'

        with patch("app.services.trainer_service.openai_service.complete_text", new=fake_complete):
            await TrainerService.evaluate_session_task(mock_db, 555)

        sys_prompt = captured_messages[0]["content"]
        # Must resolve to Acme Telecom and NOT Empresa Demo or Boston Medical Group
        self.assertNotIn("Empresa Demo", sys_prompt)
        self.assertNotIn("Boston Medical Group", sys_prompt)
        self.assertIn("Acme Telecom", sys_prompt)


if __name__ == "__main__":
    unittest.main()
