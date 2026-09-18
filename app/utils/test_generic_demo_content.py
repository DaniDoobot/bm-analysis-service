"""
app/utils/test_generic_demo_content.py
=======================================
Focalized tests verifying:
1. Complete absence of medical/clinical keywords (médico, paciente, clínica, doppler, urólogo, tratamiento, etc.)
2. Genuine differentiation between Atención al Cliente and Ventas
3. Variability of strengths and weaknesses across agents
4. Individualized, varied justifications per objective in final_report_json
5. Rich roleplay simulation prompts (>600 chars) with 5-level resistance ladder and generic business personas
6. Serialization compatibility with PersonalizedTrainingService
"""
import re
import unittest
from decimal import Decimal

from app.services.demo_training_cycle_enhancer import (
    DEMO_COMPANY_ID,
    OBJECTIVES_CATALOG,
    PERSONAS_POOL,
    generate_enhanced_training_cycle_data,
    generate_simulation_evaluation_data,
    build_roleplay_simulation_prompt,
)
from app.models.personalized_training import TrainingAgentReport


class TestGenericDemoContent(unittest.TestCase):
    """Focalized validation suite for non-medical generic contact center content."""

    MEDICAL_REGEX = re.compile(
        r"(?i)\b(?:médic[oa]s?|pacientes?|clínicas?|ecografías?|urólog[oa]s?|tratamientos?|anamnesis|patologías?|fármacos?|Boston\s+Medical)\b"
    )

    def test_personas_and_catalog_have_no_medical_references(self):
        """Verify PERSONAS_POOL and OBJECTIVES_CATALOG contain no clinical/medical terms."""
        for p in PERSONAS_POOL:
            text_block = f"{p['name']} {p['occupation']} {p['emotional_state']} {p['history']} {p['communication_style']}"
            matches = self.MEDICAL_REGEX.findall(text_block)
            self.assertEqual(matches, [], f"Medical terms found in persona {p['name']}: {matches}")

        for svc_key, cat in OBJECTIVES_CATALOG.items():
            for obj in cat["general"] + cat["specific"]:
                text_block = f"{obj['title']} {obj['description']} {obj.get('rationale', '')} {obj.get('expected_behavior', '')}"
                matches = self.MEDICAL_REGEX.findall(text_block)
                self.assertEqual(matches, [], f"Medical terms found in objective {obj['title']}: {matches}")

    def test_all_60_agents_generate_zero_medical_terms(self):
        """Simulate all 60 agents across atencion-al-cliente and ventas; zero medical matches."""
        for i in range(1, 61):
            agent_id = f"demo_agent_{i}"
            service_key = "ventas" if (i % 3 == 0) else "atencion-al-cliente"
            status = "completed" if i <= 48 else "in_progress"
            data = generate_enhanced_training_cycle_data(
                agent_id=agent_id,
                agent_name=f"Agente Demo {i}",
                agent_initials=f"AD{i}",
                service_key=service_key,
                status=status,
            )
            # Scan summary_general, evolution_summary, strengths, weaknesses, final_report, prompts
            full_text = " ".join([
                data["summary_general"],
                data["evolution_summary"],
                str(data["strengths_json"]),
                str(data["weaknesses_json"]),
                str(data["general_objectives_json"]),
                str(data["specific_objectives_json"]),
                str(data.get("final_report_json") or ""),
                " ".join(p["prompt_text"] for p in data["prompts"]),
            ])
            matches = self.MEDICAL_REGEX.findall(full_text)
            self.assertEqual(
                matches, [],
                f"Medical terms found for agent {agent_id} ({service_key}, {status}): {set(matches)}"
            )

    def test_strengths_and_weaknesses_variability_across_agents(self):
        """Verify strengths/weaknesses rotate and vary across agents rather than being identical."""
        strengths_seen = set()
        weaknesses_seen = set()
        for i in range(1, 15):
            data = generate_enhanced_training_cycle_data(
                agent_id=f"demo_agent_{i}",
                agent_name=f"Agente {i}",
                agent_initials=f"A{i}",
                service_key="atencion-al-cliente",
                status="completed",
            )
            for s in data["strengths_json"]:
                strengths_seen.add(s["title"])
            for w in data["weaknesses_json"]:
                weaknesses_seen.add(w["title"])

        self.assertGreaterEqual(len(strengths_seen), 6, "Must rotate through multiple distinct strengths")
        self.assertGreaterEqual(len(weaknesses_seen), 4, "Must rotate through multiple distinct weaknesses")

    def test_service_differentiation_atencion_vs_ventas(self):
        """Verify clear operational differences between atencion-al-cliente and ventas."""
        atencion = generate_enhanced_training_cycle_data(
            "demo_agent_1", "Agente ATC", "ATC", "atencion-al-cliente", "completed"
        )
        ventas = generate_enhanced_training_cycle_data(
            "demo_agent_1", "Agente VNT", "VNT", "ventas", "completed"
        )
        self.assertIn("atención al cliente", atencion["summary_general"])
        self.assertIn("ventas", ventas["summary_general"])

        atc_titles = {o["title"] for o in atencion["general_objectives_json"]}
        vnt_titles = {o["title"] for o in ventas["general_objectives_json"]}
        self.assertNotEqual(atc_titles, vnt_titles, "Objectives must differ between services")

    def test_individual_justifications_in_final_report(self):
        """Verify each objective in final_report_json has a specific, differentiated justification."""
        data = generate_enhanced_training_cycle_data(
            "demo_agent_5", "Agente 5", "A5", "atencion-al-cliente", "completed"
        )
        final_rep = data["final_report_json"]
        self.assertIsNotNone(final_rep)
        objs_status = final_rep["objectives_status"]
        self.assertGreaterEqual(len(objs_status), 6)

        justifications = [o["justification"] for o in objs_status]
        # Ensure not all justifications are identical
        self.assertGreater(
            len(set(justifications)), 3,
            "Justifications must not be cloned or hardcoded to a single string"
        )
        for obj in objs_status:
            self.assertIn("status", obj)
            self.assertIn(obj["status"], ("SUPERADO", "NO SUPERADO"))
            self.assertIn("score", obj)
            self.assertIn("base_score", obj)
            self.assertIn("improvement_delta", obj)
            self.assertGreater(len(obj["justification"]), 20)

    def test_simulation_prompts_quality(self):
        """Verify prompts are detailed contact center briefs exceeding 600 characters with 5 levels."""
        for svc in ("atencion-al-cliente", "ventas"):
            p = build_roleplay_simulation_prompt(1, "Agente Prueba", svc, 2, "media")
            self.assertGreater(len(p["prompt_text"]), 600)
            self.assertIn("SIMULACIÓN DE ENTRENAMIENTO", p["prompt_text"])
            self.assertIn("ESCALERA DE RESISTENCIA Y DIFICULTAD (5 NIVELES)", p["prompt_text"])
            self.assertIn("Nivel 1", p["prompt_text"])
            self.assertIn("Nivel 5", p["prompt_text"])

    def test_evaluations_contain_cliente_not_paciente(self):
        """Verify synthetic evaluations use Agente/Cliente and contact center criteria."""
        eval_data = generate_simulation_evaluation_data(
            prompt_title="Gestión de Objeciones",
            prompt_number=1,
            agent_name="Laura Gómez",
            service_key="ventas",
            agent_score_tier="top",
            agent_id="demo_agent_6",
        )
        self.assertIn("Agente:", eval_data["transcription"])
        self.assertIn("Cliente:", eval_data["transcription"])
        self.assertNotIn("Paciente:", eval_data["transcription"])
        matches = self.MEDICAL_REGEX.findall(eval_data["transcription"])
        self.assertEqual(matches, [])

    def test_mapper_produces_valid_structure(self):
        """Verify _map_report_to_dict serializes strengths, weaknesses, and objectives correctly."""
        data = generate_enhanced_training_cycle_data(
            "demo_agent_7", "Agente 7", "A7", "atencion-al-cliente", "completed"
        )
        from app.services.personalized_training_service import PersonalizedTrainingService
        report = TrainingAgentReport(
            training_report_id=101,
            company_id=DEMO_COMPANY_ID,
            hubspot_owner_id="demo_agent_7",
            agent_name="Agente 7",
            agent_initials="A7",
            status="completed",
            summary_general=data["summary_general"],
            evolution_summary=data["evolution_summary"],
            strengths_json=data["strengths_json"],
            weaknesses_json=data["weaknesses_json"],
            notable_data_json=data["notable_data_json"],
            general_objectives_json=data["general_objectives_json"],
            specific_objectives_json=data["specific_objectives_json"],
            final_report_json=data["final_report_json"],
            is_current=True,
        )
        mapped = PersonalizedTrainingService._map_report_to_dict(report)

        self.assertIsInstance(mapped["strengths_json"], list)
        self.assertGreater(len(mapped["strengths_json"]), 0)
        self.assertIn("title", mapped["strengths_json"][0])
        self.assertIn("evidence", mapped["strengths_json"][0])

        self.assertIsInstance(mapped["weaknesses_json"], list)
        self.assertGreater(len(mapped["weaknesses_json"]), 0)

        self.assertIsInstance(mapped["general_objectives_json"], list)
        self.assertIsInstance(mapped["specific_objectives_json"], list)
        self.assertIsNotNone(mapped["final_report_json"])


if __name__ == "__main__":
    unittest.main()
