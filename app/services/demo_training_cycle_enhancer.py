"""
app/services/demo_training_cycle_enhancer.py
=============================================
High-fidelity synthetic data generator and enhancer for Empresa Demo (company_id=7).

Produces realistic, comprehensive, and non-cloned:
1. General Objectives (3-4 per cycle with title, description, rationale, expected behavior, success indicators)
2. Specific Objectives (3-4 per cycle with criteria tags, specific behaviors, success indicators)
3. Strengths, Weaknesses, Notable Data (list format with concrete evidence)
4. Roleplay Simulation Prompts (multi-section briefs exceeding 600 characters with character persona,
   voice rules, 5-level resistance ladder, progression triggers, and observable criteria)
5. Call Sessions & Realistic Multi-turn Evaluations (transcription with Agente/Paciente turns,
   scores, structured feedback, and boolean criteria)
6. Final Reports with objectives_status evaluation tracking (SUPERADO / NO SUPERADO, base scores, deltas)
"""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import random
from typing import Any, Dict, List, Optional, Tuple

DEMO_COMPANY_ID = 7


# ─────────────────────────────────────────────────────────────────────────────
# Character Personas Library for Roleplay Simulations
# ─────────────────────────────────────────────────────────────────────────────
PERSONAS_POOL = [
    {
        "name": "Antonio Morales",
        "age": 54,
        "occupation": "Autónomo del sector logístico",
        "emotional_state": "Desconfiado y con prisa",
        "history": "Ha consultado anteriormente en otra clínica privada sin resultados satisfactorios. Teme perder el dinero y duda de la eficacia de los tratamientos médicos actuales.",
        "communication_style": "Corto, directo, interrumpe si siente que le leen un guion publicitario."
    },
    {
        "name": "Carlos Santillana",
        "age": 47,
        "occupation": "Profesor de secundaria",
        "emotional_state": "Inseguro, pudoroso pero receptivo",
        "history": "Lleva más de 8 meses sufriendo molestias y disminución en su rendimiento habitual. Le da mucha vergüenza hablar de su problema y teme que la llamada no sea confidencial.",
        "communication_style": "Voz baja, vacilante, necesita validación y tono empático antes de abrirse."
    },
    {
        "name": "Javier Benítez",
        "age": 62,
        "occupation": "Jubilado reciente de la banca",
        "emotional_state": "Analítico, escéptico con los precios",
        "history": "Ha leído información contradictoria en foros de internet sobre tratamientos hormonales y ondas de choque. Exige garantías por escrito y desglose económico.",
        "communication_style": "Formal, meticuloso, pregunta detalles técnicos y compara con ofertas de competidores."
    },
    {
        "name": "Manuel Delgado",
        "age": 39,
        "occupation": "Ingeniero informático",
        "emotional_state": "Frustrado por falta de tiempo",
        "history": "Horarios de trabajo intensos con viajes constantes. Quiso acudir el mes pasado pero canceló porque no le dieron flexibilidad horaria en su centro más cercano.",
        "communication_style": "Rápido, pragmático, quiere saber exactamente cuánto dura la consulta y si hay citas en horario de tarde o sábados."
    },
    {
        "name": "Roberto Vidal",
        "age": 58,
        "occupation": "Comerciante minorista",
        "emotional_state": "Muy reservado y temeroso del dolor",
        "history": "Tiene miedo a las agujas y a pruebas diagnósticas invasivas. Cree falsamente que cualquier diagnóstico requerirá cirugía o medicamentos con efectos secundarios graves.",
        "communication_style": "Poco expresivo al inicio; si se le tranquiliza con lenguaje médico claro y accesible, se muestra agradecido y colaborador."
    },
    {
        "name": "Enrique Gómez",
        "age": 51,
        "occupation": "Abogado laboralista",
        "emotional_state": "Exigente y suspicaz",
        "history": "Recibió un mensaje promocional y llama para comprobar si es una consulta informativa real o un gancho comercial. Quiere hablar con un doctor directamente.",
        "communication_style": "Dialéctica rápida, busca inconsistencias en el discurso y evalúa la preparación técnica del agente."
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Objectives Catalogs by Service / Specialty
# ─────────────────────────────────────────────────────────────────────────────
OBJECTIVES_CATALOG = {
    "atencion-al-cliente": {
        "general": [
            {
                "title": "Protocolo de Escucha Activa y Contención Emocional",
                "description": "Implementar pausas deliberadas y validación verbal inmediata ante situaciones de incertidumbre o malestar del paciente.",
                "rationale": "El 35% de las interrupciones tempranas en llamadas de atención derivan en quejas por falta de calidez o comprensión de la situación médica.",
                "expected_behavior": "Escuchar sin interrumpir durante los primeros 45 segundos, parafrasear el motivo de consulta con empatía y confirmar la necesidad antes de proponer pasos médicos.",
                "success_indicators": [
                    "Cero interrupciones detectadas antes de los 40 segundos de relato del usuario.",
                    "Puntuación mínima de 8.5 en el criterio de escucha activa en las auditorías semanales."
                ]
            },
            {
                "title": "Optimización del Flujo de Recopilación de Datos Clínicos",
                "description": "Guiar la anamnesis telefónica inicial con preguntas estructuradas abiertas y de verificación sin saturar al paciente.",
                "rationale": "La falta de precisión en los datos recopilados retrasa la asignación del especialista adecuado en un 22% de los casos.",
                "expected_behavior": "Estructurar la conversación en tres fases: motivo principal, antecedentes médicos relevantes y disponibilidad de visita médica.",
                "success_indicators": [
                    "Cumplimiento del 100% de verificación de datos básicos en el CRM.",
                    "Tiempo de captura optimizado en menos de 2 minutos y 30 segundos."
                ]
            },
            {
                "title": "Claridad en la Explicación de Protocolos y Pruebas Médicas",
                "description": "Transmitir de manera pedagógica y desdramatizada en qué consiste la primera consulta diagnóstica de Boston Medical.",
                "rationale": "El temor a pruebas invasivas es la causa del 40% de las incomparecencias a primera visita médica.",
                "expected_behavior": "Explicar las pruebas diagnósticas (ecografía doppler, análisis específico) como procedimientos seguros, indoloros y no invasivos.",
                "success_indicators": [
                    "Uso recurrente del concepto 'diagnóstico integral no invasivo' en el 100% de consultas.",
                    "Incremento del ratio de asistencia a consulta confirmada superior al 85%."
                ]
            },
            {
                "title": "Desescalada de Consultas Complejas y Quejas de Servicio",
                "description": "Manejo estructurado de llamadas de usuarios molestos por retrasos o cambios de horario.",
                "rationale": "Preservar la confianza y reputación del servicio evitando cancelaciones definitivas.",
                "expected_behavior": "Validar la molestia del usuario, disculparse en nombre de la clínica y ofrecer de inmediato 2 alternativas concretas.",
                "success_indicators": [
                    "Resolución en primera llamada (FCR) superior al 80% en casos de reclamación.",
                    "Satisfacción percibida del usuario superior a 8/10 en encuesta posterior."
                ]
            }
        ],
        "specific": [
            {
                "title": "Validación Emocional Previa a la Solución",
                "description": "Utilizar fórmulas de acompañamiento cálido ('Entiendo perfectamente su preocupación, Sr. X, estamos aquí para ayudarle').",
                "related_criteria": ["empatia_mostrada", "escucha_activa", "validacion_emocional"],
                "specific_behavior_to_improve": "Evitar saltar inmediatamente a citas o presupuestos sin haber conectado con la preocupación personal del paciente.",
                "success_indicators": [
                    "Registro de al menos dos frases de empatía genuina en cada llamada.",
                    "Ausencia de silencios incómodos de más de 5 segundos."
                ]
            },
            {
                "title": "Estructura de Despedida y Confirmación de Cita",
                "description": "Resumir fecha, hora, dirección del centro clínico y preparación requerida antes de colgar.",
                "related_criteria": ["cierre_llamada", "explicacion_siguientes_pasos", "resumen_acuerdos"],
                "specific_behavior_to_improve": "Asegurar que el paciente repita la fecha acordada o confirme la recepción del recordatorio por SMS/WhatsApp.",
                "success_indicators": [
                    "100% de llamadas con cierre estructurado y confirmación de dirección del centro.",
                    "Disminución de dudas posteriores en un 30%."
                ]
            },
            {
                "title": "Control del Tiempo de Llamada sin Perder Calidad",
                "description": "Canalizar relatos dispersos del paciente con reconducciones asertivas amables.",
                "related_criteria": ["control_tiempo", "gestion_conversacion", "eficiencia_llamada"],
                "specific_behavior_to_improve": "Reconducir al paciente cuando se desvíe del motivo de salud con frases de enlace cordiales.",
                "success_indicators": [
                    "Duración media de llamada (AHT) entre 4:30 y 6:00 minutos.",
                    "Sin sensación de prisa transmitida al usuario."
                ]
            },
            {
                "title": "Verificación de Requisitos de Salud Previos",
                "description": "Preguntar de forma protocolizada si toma medicación cardiovascular o anticoagulantes antes de fijar la cita.",
                "related_criteria": ["seguridad_paciente", "cuestionario_salud", "protocolo_clinico"],
                "specific_behavior_to_improve": "Completar sistemáticamente la casilla de seguridad médica en ficha antes de enviar la cita a agenda.",
                "success_indicators": [
                    "Tasa de cumplimiento de cuestionario médico del 100% en todas las citas agendadas."
                ]
            }
        ]
    },
    "ventas": {
        "general": [
            {
                "title": "Sondeo Consultivo Profundo y Detección de Necesidades",
                "description": "Formular preguntas abiertas de diagnóstico para comprender el impacto del problema en la calidad de vida del paciente.",
                "rationale": "Las llamadas donde se explora la dimensión personal del tratamiento logran un 45% más de compromiso en la asistencia a clínica.",
                "expected_behavior": "Realizar al menos tres preguntas abiertas de impacto antes de plantear la conveniencia de la cita diagnóstica.",
                "success_indicators": [
                    "Ratio de sondeo calificado superior al 90% en llamadas de nuevos pacientes.",
                    "Identificación clara de la motivación principal de tratamiento en la ficha del lead."
                ]
            },
            {
                "title": "Argumentación de Valor Diferencial frente a Alternativas Farmacéuticas",
                "description": "Contrastar el enfoque médico multidisciplinar y personalizado de Boston Medical frente a pastillas genéricas o automedicación.",
                "rationale": "Muchos pacientes han probado fármacos genéricos sin éxito; explicar la personalización médica disipa el escepticismo.",
                "expected_behavior": "Presentar el tratamiento no como una pastilla sino como un plan de recuperación médica supervisado por urólogos especializados.",
                "success_indicators": [
                    "Mención consistente de los 3 pilares clínicos (diagnóstico personalizado, seguimiento médico continuo, alta tasa de éxito).",
                    "Aceptación de la cita diagnóstica en más del 70% de pacientes reticentes iniciales."
                ]
            },
            {
                "title": "Manejo Riguroso de Objeciones de Precio y Distancia",
                "description": "Reframing del coste como inversión en salud y bienestar de pareja, ofreciendo facilidades y citas estratégicas.",
                "rationale": "El precio es la objeción manifestada en el 60% de las dudas iniciales de los pacientes interesados.",
                "expected_behavior": "Validar el coste de la primera consulta poniendo en valor que incluye todas las pruebas diagnósticas especializadas.",
                "success_indicators": [
                    "Superación documentada de la objeción económica en al menos 6 de cada 10 simulaciones.",
                    "Presentación fluida de facilidades de pago sin devaluar el servicio médico."
                ]
            },
            {
                "title": "Cierre Asertivo de Cita con Doble Alternativa",
                "description": "Proponer opciones de agendamiento concretas (día y franja horaria) en lugar de preguntas abiertas indeterminadas.",
                "rationale": "Preguntar '¿cuándo le viene bien?' genera postergación; ofrecer '¿prefiere martes por la mañana o jueves tarde?' incrementa el cierre.",
                "expected_behavior": "Aplicar la técnica de la doble opción ofreciendo dos huecos preferentes en agenda.",
                "success_indicators": [
                    "Incremento del ratio de conversión a cita agendada de 8 puntos porcentuales.",
                    "Compromiso explícito del paciente verbalizado durante la llamada."
                ]
            }
        ],
        "specific": [
            {
                "title": "Desmontaje de la Objeción 'Tengo que pensarlo / consultarlo'",
                "description": "Averiguar amablemente qué duda clínica concreta le impide tomar la decisión de valoración médica.",
                "related_criteria": ["manejo_objeciones", "desmontaje_dudas", "foco_beneficio"],
                "specific_behavior_to_improve": "No aceptar el primer 'lo pienso' como un no definitivo; explorar con delicadeza qué información adicional necesita.",
                "success_indicators": [
                    "Conversión de al menos el 40% de objeciones de postergación en cita informativa.",
                    "Pregunta de sondeo de cierre formulada con cortesía y respeto."
                ]
            },
            {
                "title": "Enfatización de la Confidencialidad y Discreción",
                "description": "Garantizar explícitamente el secreto médico, salas de espera privadas y acceso discreto al centro.",
                "related_criteria": ["garantia_confidencialidad", "tranquilidad_paciente", "protocolo_privacidad"],
                "specific_behavior_to_improve": "Mencionar de forma proactiva que la clínica cuenta con protocolos de máxima discreción sin que el paciente tenga que pedirlo.",
                "success_indicators": [
                    "Mención de protocolo de discreción en el 100% de llamadas con reticencia por pudor.",
                    "Disminución de dudas asociadas a privacidad a 0."
                ]
            },
            {
                "title": "Tratamiento de Comparativa con la Competencia",
                "description": "Defender el prestigio y trayectoria de Boston Medical sin descalificar a otros centros o clínicas genéricas.",
                "related_criteria": ["posicionamiento_marca", "argumentacion_clinica", "etica_comercial"],
                "specific_behavior_to_improve": "Centrar la respuesta en los más de 25 años de especialización exclusiva y equipo médico propio.",
                "success_indicators": [
                    "Puntuación máxima en ética y elegancia comercial en auditorías de calidad.",
                    "Sin mención negativa a competidores."
                ]
            },
            {
                "title": "Confirmación Inmediata de Canal de Contacto (SMS/WhatsApp)",
                "description": "Verificar en el acto la recepción del enlace de localización y guía de preparación para la cita.",
                "related_criteria": ["fidelizacion_cita", "reduccion_no_show", "soporte_multicanal"],
                "specific_behavior_to_improve": "Comprobar durante la llamada que el número de móvil puede recibir mensajes con la confirmación de la cita.",
                "success_indicators": [
                    "Reducción del ratio de incomparecencia (No-Show) por debajo del 12%."
                ]
            }
        ]
    }
}


# ─────────────────────────────────────────────────────────────────────────────
# Prompt Builder: Deep, Realistic Roleplay Prompts (>600 chars)
# ─────────────────────────────────────────────────────────────────────────────
def build_roleplay_simulation_prompt(
    prompt_number: int,
    agent_name: str,
    service_key: str,
    persona_index: int,
    difficulty_level: str = "medio"
) -> Dict[str, Any]:
    """
    Constructs a rich, multi-section roleplay prompt for speech trainer simulations.
    Ensures persona depth, conversation rules, 5-level resistance ladder, and criteria.
    """
    persona = PERSONAS_POOL[persona_index % len(PERSONAS_POOL)]
    service_name = "Atención al Paciente" if service_key == "atencion-al-cliente" else "Asesoramiento Médico y Agendamiento"
    
    if service_key == "atencion-al-cliente":
        scenarios = [
            {
                "title": f"Simulación {prompt_number}: Gestión de Paciente Preocupado por Efectos Secundarios y Confidencialidad",
                "scenario_type": "roleplay",
                "focus": ["Validación emocional", "Confidencialidad", "Explicación de pruebas diagnósticas"],
                "objective_summary": "Atender a un paciente que muestra reticencias por pudor y miedo a que su entorno familiar se entere.",
                "expected_behavior": "Aplicar escucha activa, desdramatizar el problema médico con empatía y asegurar la total privacidad de la visita.",
                "why_calling": "Quiere saber si en la clínica se cruza con otros pacientes conocidos y si las pruebas son dolorosas.",
                "level_1": "El paciente saluda tímido y pregunta qué tipo de clínica es esta.",
                "level_2": "Expresa temor a que el tratamiento le cause problemas cardíacos o dolores.",
                "level_3": "Afirma: 'Si esto va a saberlo alguien o me van a hacer pruebas raras, prefiero no ir'.",
                "level_4": "Duda de la cualificación de los médicos y pregunta si le atenderá un doctor colegiado.",
                "level_5": "Si el agente responde con calidez y rigor profesional, acepta acudir a una primera consulta confidencial."
            },
            {
                "title": f"Simulación {prompt_number}: Reclamación por Horarios y Solicitud de Cita Urgente en Horario Especial",
                "scenario_type": "roleplay",
                "focus": ["Resolución de quejas", "Flexibilidad horaria", "Orientación al paciente"],
                "objective_summary": "Gestionar la queja de un paciente con agenda muy apretada que no encuentra hueco de consulta.",
                "expected_behavior": "Reconducir la queja sin confrontación, empatizar con su falta de tiempo y buscar una solución viable en agenda.",
                "why_calling": "Llama molesto porque en la web vio un horario amplio y en el centro le dicen que el especialista solo atiende por las mañanas.",
                "level_1": "Entra quejándose del poco tiempo que tiene y de la poca flexibilidad del centro.",
                "level_2": "Muestra impaciencia ante las preguntas de protocolo habituales ('Mire, no me haga perder el tiempo con encuestas').",
                "level_3": "Amenaza con marcharse a una clínica de la competencia si no le dan una solución hoy mismo.",
                "level_4": "Exige saber por qué no abren los sábados o tardes tardías.",
                "level_5": "Si el agente valida la queja con tranquilidad y le ofrece un hueco preferente o lista de espera prioritaria, se calma y agradece el trato."
            },
            {
                "title": f"Simulación {prompt_number}: Paciente de Edad Avanzada con Dudas sobre Cobertura de Seguros Médicos",
                "scenario_type": "roleplay",
                "focus": ["Pedagogía clínica", "Claridad informativa", "Paciencia y tono pausado"],
                "objective_summary": "Explicar las condiciones del servicio privado a un paciente que pensaba que lo cubría su mutua habitual.",
                "expected_behavior": "Explicar con total honestidad y sin tecnicismos que Boston Medical es un centro monográfico privado, enfatizando la inmediatez y exclusividad.",
                "why_calling": "Pregunta si puede presentar su tarjeta de Sanitas o Adeslas para la consulta.",
                "level_1": "Pregunta de forma confusa si su seguro médico le incluye la prueba ecográfica.",
                "level_2": "Se muestra contrariado al saber que es un centro médico privado especializado.",
                "level_3": "Dice: 'En mi mutua me atienden gratis, ¿por qué tendría que pagar por ver a un urólogo aquí?'.",
                "level_4": "Pregunta si la consulta incluye algún tipo de informe para su médico de cabecera.",
                "level_5": "Si el agente explica el valor de la consulta integral sin listas de espera de meses, decide reservar una cita para valoración."
            },
            {
                "title": f"Simulación {prompt_number}: Reconducción de Paciente Desmotivado tras Tratamiento Farmacéutico Fallido",
                "scenario_type": "roleplay",
                "focus": ["Esperanza clínica basada en evidencia", "Escucha activa", "Reencuadre de expectativas"],
                "objective_summary": "Atender a un paciente que siente que su problema no tiene solución médica.",
                "expected_behavior": "Evitar promesas milagro; transmitir rigor científico sobre los diagnósticos etiológicos personalizados.",
                "why_calling": "Llama desanimado preguntando si de verdad hay alternativas cuando las pastillas habituales han dejado de funcionar.",
                "level_1": "Tono apático, dice que llama 'por probar, aunque seguro que ya no hay nada que hacer'.",
                "level_2": "Cuenta malas experiencias pasadas con efectos secundarios de fármacos comprados por internet.",
                "level_3": "Plantea que los tratamientos médicos especializados son solo para personas muy mayores.",
                "level_4": "Objeta sobre la duración del tratamiento y si tendrá que estar atado a una clínica durante años.",
                "level_5": "Si el agente explica con empatía que cada organismo responde a factores vasculares distintos y que el diagnóstico identifica la causa raíz, se anima a acudir a consulta."
            }
        ]
    else:
        # Ventas / Comercial / Retención
        scenarios = [
            {
                "title": f"Simulación {prompt_number}: Objeción Frontal de Precio y Comparación con Farmacia Habitual",
                "scenario_type": "roleplay",
                "focus": ["Manejo de objeción económica", "Puesta en valor médico", "Técnica de doble alternativa"],
                "objective_summary": "Superar la objeción del paciente sobre el coste de la primera consulta frente al precio de pastillas genéricas.",
                "expected_behavior": "No discutir el precio; resaltar que la consulta incluye pruebas ecográficas y consulta médica con urólogo experto.",
                "why_calling": "Llama tras ver un anuncio pero se frena en seco cuando se entera de que la consulta diagnóstica tiene un coste.",
                "level_1": "Pregunta directamente el precio sin querer dar ningún dato sobre su situación.",
                "level_2": "Al escuchar el coste, objeta: 'Me parece carísimo, en la farmacia una caja de pastillas me cuesta 30 euros'.",
                "level_3": "Objeción dura: 'Ustedes lo que quieren es cobrarme antes de saber si me pueden curar'.",
                "level_4": "Escepticismo: '¿Y si voy y me dicen que no tengo solución? Habré tirado el dinero'.",
                "level_5": "Si el agente explica que el diagnóstico busca la causa vascular real para no depender de fármacos temporales, acepta agendar cita médica."
            },
            {
                "title": f"Simulación {prompt_number}: Objeción 'Tengo que consultarlo con mi pareja' y Reticencia al Compromiso",
                "scenario_type": "roleplay",
                "focus": ["Detección de dudas ocultas", "Involucración de pareja", "Cierre suave"],
                "objective_summary": "Gestionar la evasiva común de postergación sin presionar agresivamente pero manteniendo la puerta abierta.",
                "expected_behavior": "Reconocer que la pareja es un apoyo fundamental y sugerir que acuda acompañado a la primera visita si lo desea.",
                "why_calling": "Ha recopilado la información pero intenta colgar con un 'ya llamaré cuando lo hable en casa'.",
                "level_1": "Se muestra de acuerdo con las explicaciones pero con actitud de posponer la decisión.",
                "level_2": "Dice: 'El tratamiento suena bien pero no puedo decidir esto hoy, tengo que hablarlo con mi mujer'.",
                "level_3": "Resistencia: 'No quiero que me presionen, si me interesa ya volveré a llamar yo'.",
                "level_4": "Pregunta si su pareja puede entrar con él a la consulta del doctor.",
                "level_5": "Si el agente normaliza la presencia de la pareja y ofrece una reserva sin penalización de cancelación, confirma fecha."
            },
            {
                "title": f"Simulación {prompt_number}: Paciente Analítico que Exige Garantías de Éxito al 100%",
                "scenario_type": "roleplay",
                "focus": ["Ética profesional", "Rigor médico", "Manejo de expectativas"],
                "objective_summary": "Tratar a un perfil exigente que exige garantías absolutas de curación antes de pisar la clínica.",
                "expected_behavior": "Mantener la seriedad deontológica; explicar que en medicina no existen garantías absolutas pero sí alta tasa de éxito basada en evidencia.",
                "why_calling": "Quiere saber si le devuelven el dinero en caso de que el tratamiento no cumpla sus expectativas.",
                "level_1": "Pregunta con tono serio: '¿Qué porcentaje exacto de pacientes curan y qué garantía por escrito me dan?'.",
                "level_2": "Insiste en que si no hay garantía contractual no está dispuesto a perder el tiempo.",
                "level_3": "Compara con clínicas de implantes o estética que ofrecen 'garantías de por vida'.",
                "level_4": "Pregunta por la formación del equipo médico y los estudios clínicos publicados.",
                "level_5": "Si el agente argumenta con seriedad médica que el diagnóstico es precisamente para determinar si es apto o no antes de iniciar cualquier terapia, respeta la honestidad y agenda."
            },
            {
                "title": f"Simulación {prompt_number}: Cierre Asertivo ante Paciente Indeciso con Múltiples Excusas de Tiempo",
                "scenario_type": "roleplay",
                "focus": ["Doble alternativa", "Sensación de oportunidad", "Cierre estructurado"],
                "objective_summary": "Conducir a un paciente interesado pero disperso a elegir una fecha concreta en la agenda clínica.",
                "expected_behavior": "Utilizar la técnica de doble opción ('¿Prefiere comienzos de semana o finales?') para facilitar la toma de decisión.",
                "why_calling": "Lleva 20 minutos preguntando detalles menores sobre la clínica pero evita comprometer una fecha.",
                "level_1": "Conversador, simpático, pero evade concretar cuándo podría acudir a la clínica.",
                "level_2": "Pone excusas de trabajo: 'Esta semana lo tengo muy liado, y la que viene viajo'.",
                "level_3": "Dice: 'Mándemelo todo por correo y ya lo miraré el fin de semana'.",
                "level_4": "Pregunta si la consulta se puede hacer por videollamada sin acudir físicamente.",
                "level_5": "Si el agente explica por qué la prueba ecográfica requiere presencia física y ofrece dos opciones horarias protegidas, elige una de ellas."
            }
        ]

    scenario = scenarios[(prompt_number - 1 + persona_index) % len(scenarios)]

    prompt_text = f"""# SIMULACIÓN DE ENTRENAMIENTO BM - ROLEPLAY VOCAL

## 1. IDENTIDAD Y ROL DEL BOT
- Eres un paciente interactivo en una simulación de entrenamiento por voz para agentes del servicio **{service_name}**.
- Tu objetivo es interpretar de forma ultra-realista al personaje asignado a continuación.
- **REGLAS DE ORO:**
  1. NO salgas jamás de tu personaje bajo ninguna circunstancia.
  2. NO des feedback al agente durante la llamada.
  3. NO digas que eres una inteligencia artificial, un asistente virtual ni un bot de voz.
  4. Responde como una persona normal en una conversación telefónica privada sobre su salud íntima.

## 2. PERSONAJE Y ANTECEDENTES
- **Nombre:** {persona['name']}
- **Edad:** {persona['age']} años
- **Ocupación:** {persona['occupation']}
- **Estado emocional:** {persona['emotional_state']}
- **Historia previa:** {persona['history']}
- **Estilo de comunicación:** {persona['communication_style']}

## 3. REGLAS DE VOZ TELEFÓNICA Y NATURALIDAD
- **Respuestas breves:** Habla en turnos de 1 a 2 frases como máximo (salvo que el agente te haga una pregunta abierta directa).
- **Lenguaje oral realista:** Usa expresiones coloquiales, titubeos ocasionales ('eh...', 'bueno...', 'es que...') y pausas naturales.
- **No sueltes toda la información de golpe:** El agente debe ganarse tu confianza formulando preguntas adecuadas.

## 4. CONTEXTO Y SITUACIÓN DE LA LLAMADA
- **Motivo de la llamada:** {scenario['why_calling']}
- **Escenario:** {scenario['objective_summary']}
- **Comportamiento esperado del agente:** {scenario['expected_behavior']}

## 5. ESCALERA DE RESISTENCIA Y DIFICULTAD (5 NIVELES)
- **Nivel 1 (Apertura):** {scenario['level_1']}
- **Nivel 2 (Sondeo / Primera duda):** {scenario['level_2']}
- **Nivel 3 (Objeción principal):** {scenario['level_3']}
- **Nivel 4 (Prueba de consistencia):** {scenario['level_4']}
- **Nivel 5 (Resolución / Cierre):** {scenario['level_5']}

## 6. DISPARADORES DE PROGRESIÓN (CONDICIONES DE AVANCE)
- **Si el agente:** Interrumpe, usa tecnicismos incomprensibles, presiona agresivamente o no valida tus emociones $\\rightarrow$ **Aumenta tu resistencia**, muéstrate más seco y muestra deseo de colgar la llamada.
- **Si el agente:** Te escucha con paciencia, te llama por tu nombre de manera respetuosa, responde con claridad a tu duda de fondo y ofrece alternativas sin juzgar $\\rightarrow$ **Desescala tu resistencia**, agradece la aclaración y avanza al siguiente nivel de colaboración.

## 7. CRITERIOS OBSERVABLES PARA EVALUACIÓN POSTERIOR
- Escucha activa y ausencia de interrupciones tempranas.
- Empatía y validación del pudor o temor del paciente.
- Claridad en la argumentación de la consulta médica y pruebas diagnósticas.
- Manejo asertivo de la objeción sin confrontación.
- Cierre estructurado con doble alternativa y confirmación de datos."""

    return {
        "title": scenario["title"],
        "scenario_type": scenario["scenario_type"],
        "objective_focus_json": {
            "focus": scenario["focus"],
            "linked_general_objectives": [scenario["focus"][0]],
            "linked_specific_objectives": [scenario["focus"][-1]],
            "objective_summary": scenario["objective_summary"],
            "expected_behavior": scenario["expected_behavior"],
        },
        "prompt_text": prompt_text.strip(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic Transcription & Evaluation Generator for Completed Simulations
# ─────────────────────────────────────────────────────────────────────────────
def generate_simulation_evaluation_data(
    prompt_title: str,
    prompt_number: int,
    agent_name: str,
    service_key: str,
    agent_score_tier: str = "solid",  # "top", "solid", "developing"
    agent_id: str = "",
) -> Dict[str, Any]:
    """
    Generates a realistic multi-turn transcription and full evaluation dict.
    Supports deterministic score variation per agent and prompt when agent_id is provided.
    """
    agent_num = 1
    if agent_id:
        try:
            agent_num = int(agent_id.split("_")[-1])
        except Exception:
            agent_num = abs(int(hashlib.md5(agent_id.encode()).hexdigest(), 16)) % 60 + 1

    if agent_score_tier == "top":
        if agent_id:
            score = Decimal(str(round(8.70 + (((agent_num * 11 + prompt_number * 7) % 8) * 0.10), 2)))
        else:
            score = Decimal(str(round(random.uniform(8.7, 9.4), 2)))
        transcription_turns = [
            ("Agente", f"Buenos días, le atiende {agent_name} de Boston Medical Group. ¿En qué puedo ayudarle hoy?"),
            ("Paciente", "Buenos días... mire, llamo porque vi información de su clínica, pero no sé muy bien cómo funciona esto y la verdad es que me da bastante reparo hablarlo por teléfono."),
            ("Agente", "Comprendo perfectamente su situación, caballero. Le garantizo que toda nuestra conversación es estrictamente confidencial y médica. Tómese el tiempo que necesite. ¿Qué es lo que más le preocupa en este momento?"),
            ("Paciente", "Pues que he probado ya un par de pastillas que me dio un amigo y al principio bien, pero luego nada. Y temo que esto sea algo grave o que no tenga solución."),
            ("Agente", "Es una duda muy común y hace muy bien en consultarlo con profesionales. Las pastillas solo son parches temporales; en Boston Medical nuestros urólogos realizan un estudio vascular completo e indoloro para identificar la causa exacta de su caso."),
            ("Paciente", "¿Y esa consulta es dolorosa? Porque yo para las pruebas médicas soy muy aprensivo."),
            ("Agente", "Para nada, es totalmente no invasiva. Se realiza una ecografía doppler especializada que dura unos minutos y no produce ninguna molestia. Al finalizar, el doctor le explica de forma transparente las opciones de tratamiento personalizadas."),
            ("Paciente", "Bueno, eso me deja más tranquilo. ¿Y qué coste tiene esa primera valoración?"),
            ("Agente", "La consulta integral incluye el tiempo con el especialista y las pruebas diagnósticas por un importe cerrado de 89 euros. Para facilitarle el acceso, ¿le vendría mejor acudir a principios de semana por la mañana o prefiere una tarde?"),
            ("Paciente", "Prefiero por la tarde, que salgo del trabajo a las seis. Si tiene un hueco el jueves a las siete me vendría perfecto."),
            ("Agente", "Perfecto, le agendo el jueves a las 19:00 en nuestro centro principal. Le envío ahora mismo un SMS con la confirmación y la ubicación exacta. ¿Tiene a mano el móvil terminado en 42?"),
            ("Paciente", "Sí, ese es mi número. Muchas gracias por la amabilidad y la discreción, de verdad."),
            ("Agente", "Gracias a usted por su confianza. Le esperamos el jueves. Que tenga un buen día.")
        ]
        feedback = (
            f"Excelente desempeño de {agent_name} en esta simulación. Demuestra un dominio sobresaliente de la escucha activa, "
            "validando el pudor del paciente en los primeros segundos de la llamada. Desdramatiza de forma pedagógica las pruebas clínicas "
            "y ejecuta un cierre impecable mediante la técnica de la doble alternativa. Muy buena naturalidad y calidez vocal."
        )
        criteria_dict = {
            "agendar_cita": True,
            "manejo_objeciones": True,
            "objetivos_cumplidos": True,
            "claridad_comunicacion": True,
            "explicacion_servicios": True,
            "empathy_shown": True,
            "call_flow_followed": True,
            "information_gathered": True,
            "next_steps_explained": True
        }
        strengths = [
            "Contención emocional y respeto al pudor del paciente desde el saludo inicial.",
            "Explicación cristalina y desdramatizada de las pruebas diagnósticas no invasivas.",
            "Cierre asertivo con técnica de doble alternativa sin generar presión innecesaria."
        ]
        weaknesses = [
            "Mantener esta misma cadencia en llamadas de alta saturación horaria."
        ]
    elif agent_score_tier == "solid":
        if agent_id:
            score = Decimal(str(round(7.65 + (((agent_num * 13 + prompt_number * 5) % 15) * 0.06), 2)))
        else:
            score = Decimal(str(round(random.uniform(7.6, 8.4), 2)))
        transcription_turns = [
            ("Agente", f"Hola, buenos días, mi nombre es {agent_name} de Boston Medical. ¿Con quién tengo el gusto de hablar?"),
            ("Paciente", "Hola, me llamo Carlos. Quería informarme sobre lo que hacen en su centro, pero me parece que cobran bastante caro."),
            ("Agente", "Buenos días, Carlos. Entiendo su punto de vista respecto al aspecto económico. Permítame explicarle qué incluye exactamente nuestra valoración médica para que pueda valorarlo con toda la información."),
            ("Paciente", "Dígame, porque en la farmacia me cuesta menos."),
            ("Agente", "Claro, la diferencia fundamental es que en la farmacia adquiere un producto genérico sin supervisión. En nuestra clínica un especialista en salud sexual masculina evalúa su caso con ecografía y analítica para solucionar la raíz del problema."),
            ("Paciente", "Ya, pero es que no sé si voy a tener tiempo esta semana."),
            ("Agente", "Disponemos de horarios amplios tanto de mañana como de tarde para adaptarnos a su jornada laboral. ¿Qué día le encajaría mejor para visitarnos?"),
            ("Paciente", "Podría ser el viernes por la tarde, sobre las cinco."),
            ("Agente", "De acuerdo, tengo un hueco disponible el viernes a las 17:30. Le tomo nota y le llegará un mensaje con los detalles."),
            ("Paciente", "Muy bien, nos vemos el viernes entonces."),
            ("Agente", "Gracias por llamar a Boston Medical, Carlos. Buen día.")
        ]
        feedback = (
            f"Buen desempeño general de {agent_name}. El agente argumenta con solidez la diferencia de valor médico frente al gasto puntual en farmacia. "
            "Como punto de mejora, se recomienda profundizar más en el sondeo previo antes de rebatir la objeción económica y afianzar la doble alternativa en el cierre."
        )
        criteria_dict = {
            "agendar_cita": True,
            "manejo_objeciones": True,
            "objetivos_cumplidos": True,
            "claridad_comunicacion": True,
            "explicacion_servicios": True,
            "empathy_shown": True,
            "call_flow_followed": True,
            "information_gathered": True,
            "next_steps_explained": True
        }
        strengths = [
            "Buena defensa del valor médico frente a soluciones farmacológicas temporales.",
            "Tono educado, claro y profesional durante toda la intervención."
        ]
        weaknesses = [
            "Conviene realizar al menos una pregunta abierta de sondeo antes de pasar a la argumentación del precio.",
            "Utilizar dos opciones concretas en el cierre en lugar de preguntar de forma abierta 'qué día le encaja'."
        ]
    else:  # developing
        if agent_id:
            score = Decimal(str(round(6.75 + (((agent_num * 7 + prompt_number * 3) % 9) * 0.08), 2)))
        else:
            score = Decimal(str(round(random.uniform(6.5, 7.3), 2)))
        transcription_turns = [
            ("Agente", f"Boston Medical, le atiende {agent_name}, dígame."),
            ("Paciente", "Hola... mire, es que llamo porque tengo dudas sobre si operan o qué hacen allí."),
            ("Agente", "No, no operamos. Hacemos tratamientos médicos. ¿Quiere que le dé cita con el médico para que lo mire?"),
            ("Paciente", "Bueno, pero es que antes de pedir cita me gustaría saber de qué precios estamos hablando, porque si se me va de presupuesto no voy."),
            ("Agente", "La primera consulta son 89 euros con las pruebas incluidas. Si quiere venir dígame qué día le viene bien."),
            ("Paciente", "Hombre, es que me lo dice usted así tan rápido y no sé ni qué pruebas son."),
            ("Agente", "Son pruebas de ecografía doppler para ver la circulación de la zona, no duelen nada y se las hace el especialista en el momento."),
            ("Paciente", "Ah, vale, eso no me lo había dicho. Bueno, si no duele me lo puedo pensar. ¿Tiene algo para el lunes que viene?"),
            ("Agente", "Sí, el lunes a las diez de la mañana. ¿Le apunto a esa hora?"),
            ("Paciente", "Sí, apúnteme el lunes a las diez."),
            ("Agente", "Vale, queda anotado. Adiós.")
        ]
        feedback = (
            f"Simulación aprobada con margen de mejora para {agent_name}. Aunque logra la concertación de la cita, se observa precipitación en la llamada "
            "y una comunicación algo telegráfica al inicio. Debe trabajar la empatía en la apertura y desgranar las pruebas clínicas antes de dar el precio para no generar rechazo."
        )
        criteria_dict = {
            "agendar_cita": True,
            "manejo_objeciones": True,
            "objetivos_cumplidos": True,
            "claridad_comunicacion": True,
            "explicacion_servicios": False,
            "empathy_shown": False,
            "call_flow_followed": True,
            "information_gathered": True,
            "next_steps_explained": False
        }
        strengths = [
            "Concreción directa y resolución de la cita en agenda.",
            "Aclaración adecuada sobre la ausencia de dolor en las pruebas diagnósticas."
        ]
        weaknesses = [
            "Falta de calidez en la bienvenida y despedida; evitar respuestas monosilábicas o apresuradas.",
            "Explicar el valor del servicio integral antes de soltar la cifra económica de la consulta.",
            "Confirmar canal de recordatorio de la cita antes de colgar."
        ]

    transcription_lines = [f"{role}: {text}" for role, text in transcription_turns]
    transcription_text = "\n".join(transcription_lines)

    result_json = {
        "score": float(score),
        "feedback": feedback,
        "result_json": criteria_dict,
        "objectives_met": strengths,
        "areas_for_improvement": weaknesses,
        "is_valid_roleplay": True
    }

    return {
        "score": score,
        "feedback": feedback,
        "transcription": transcription_text,
        "result_json": result_json,
        "strengths": strengths,
        "weaknesses": weaknesses,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Full Cycle Enhancer Data Generator
# ─────────────────────────────────────────────────────────────────────────────
def generate_enhanced_training_cycle_data(
    agent_id: str,
    agent_name: str,
    agent_initials: str,
    service_key: str,
    status: str,  # "completed" or "in_progress"
    cycle_index: int = 1
) -> Dict[str, Any]:
    """
    Produces the complete, rich data payload for a TrainingAgentReport and its simulations.
    Tailored to service_key and deterministic agent characteristics.
    """
    # Deterministic seed based on agent_id
    agent_num = 1
    try:
        parts = agent_id.split("_")
        agent_num = int(parts[-1])
    except Exception:
        agent_num = abs(int(hashlib.md5(agent_id.encode()).hexdigest(), 16)) % 60 + 1

    # Determine tier and varied deterministic scores
    if agent_num in (3, 6, 9, 12, 15, 18, 21, 24, 33, 36, 45, 48):
        tier = "top"
        offset = Decimal(str(((agent_num * 13) % 9) * 0.06))
        avg_score = (Decimal("8.65") + offset).quantize(Decimal("0.01"))
    elif agent_num in (10, 26, 27, 28, 29, 30, 39, 40, 57, 58, 59, 60):
        tier = "developing"
        offset = Decimal(str(((agent_num * 17) % 11) * 0.06))
        avg_score = (Decimal("6.85") + offset).quantize(Decimal("0.01"))
    else:
        tier = "solid"
        offset = Decimal(str(((agent_num * 19) % 15) * 0.06))
        avg_score = (Decimal("7.65") + offset).quantize(Decimal("0.01"))

    # Pick service category (default to atencion-al-cliente if unknown)
    cat_key = "ventas" if "venta" in service_key.lower() or "comercial" in service_key.lower() else "atencion-al-cliente"
    catalog = OBJECTIVES_CATALOG[cat_key]

    # Select 3-4 General Objectives (deterministically rotated per agent)
    gen_list = catalog["general"]
    gen_count = 4 if (agent_num % 2 == 0) else 3
    gen_objs_raw = [gen_list[(i + agent_num) % len(gen_list)] for i in range(gen_count)]
    general_objectives = []
    for g in gen_objs_raw:
        general_objectives.append({
            "title": g["title"],
            "description": g["description"],
            "rationale": g["rationale"],
            "expected_behavior": g["expected_behavior"],
            "success_indicators": g["success_indicators"],
        })

    # Select 3-4 Specific Objectives (deterministically rotated per agent)
    spec_list = catalog["specific"]
    spec_count = 4 if ((agent_num + 1) % 2 == 0) else 3
    spec_objs_raw = [spec_list[(i + agent_num) % len(spec_list)] for i in range(spec_count)]
    specific_objectives = []
    for s in spec_objs_raw:
        specific_objectives.append({
            "title": s["title"],
            "description": s["description"],
            "related_criteria": s["related_criteria"],
            "specific_behavior_to_improve": s["specific_behavior_to_improve"],
            "success_indicators": s["success_indicators"],
        })

    # Strengths, Weaknesses, Notable Data
    if tier == "top":
        strengths_list = [
            {
                "title": "Excelente Empatía y Validación del Pudor",
                "description": "Establece un clima de máxima confidencialidad en los primeros 30 segundos de llamada.",
                "evidence": "En el 96% de sus llamadas evaluadas validó verbalmente la preocupación del paciente antes de argumentar."
            },
            {
                "title": "Cierre Asertivo con Doble Alternativa",
                "description": "Ofrece sistemáticamente dos franjas horarias concretas evitando la indecisión del usuario.",
                "evidence": "Ratio de conversión a cita agendada un 14% por encima de la media del equipo."
            },
            {
                "title": "Claridad en Protocolos Médicos no Invasivos",
                "description": "Explica la ecografía y pruebas de forma desdramatizada transmitiendo seguridad clínica.",
                "evidence": "Cero quejas registradas sobre falta de información previa a la visita médica."
            }
        ]
        weaknesses_list = [
            {
                "title": "Optimización del Tiempo en Despedidas Largas",
                "description": "Mantiene en ocasiones conversaciones secundarias una vez acordada y confirmada la cita.",
                "evidence": "Se detecta una media de 45 segundos de cortesía residual tras el cierre en 1 de cada 4 llamadas."
            },
            {
                "title": "Registro Temprano de Datos Secundarios",
                "description": "Anotar el motivo de consulta secundario en CRM durante la llamada y no al finalizar la misma.",
                "evidence": "2 anotaciones registradas en post-llamada que demoraron el estado disponible en centralita."
            }
        ]
        notable_data = [
            {
                "title": "Alto Ratio de Asistencia Efectiva",
                "description": "Las citas agendadas por este agente presentan una tasa de presentismo a clínica del 91%.",
                "metric_or_pattern": "Tasa de Show-up: 91.2% (+8.4% vs media de servicio)"
            }
        ]
    elif tier == "solid":
        strengths_list = [
            {
                "title": "Consistencia en Protocolo de Bienvenida",
                "description": "Cumple rigurosamente el saludo corporativo y la presentación del servicio.",
                "evidence": "100% de cumplimiento en verificación de identidad y tono corporativo."
            },
            {
                "title": "Defensa del Valor Frente a Soluciones Temporales",
                "description": "Explica con solvencia la diferencia entre tratamiento médico integral y automedicación.",
                "evidence": "Superación documentada de la objeción de coste en 7 de cada 10 simulaciones."
            },
            {
                "title": "Gestión Eficiente del Tiempo de Llamada",
                "description": "Mantiene las llamadas dentro del rango óptimo de duración sin generar sensación de prisa.",
                "evidence": "AHT medio de 4 minutos y 48 segundos, alineado con el objetivo del servicio."
            }
        ]
        weaknesses_list = [
            {
                "title": "Profundización en Preguntas Abiertas de Sondeo",
                "description": "Tiende a realizar preguntas cerradas que limitan la expresión espontánea del paciente.",
                "evidence": "En el 40% de llamadas analizadas solo realizó una pregunta de exploración antes de ofrecer cita."
            },
            {
                "title": "Mayor Proactividad en Recordatorio de Privacidad",
                "description": "Mencionar la discreción de la clínica de forma preventiva sin esperar a que el usuario pregunte.",
                "evidence": "Solo verbalizó el protocolo de confidencialidad cuando el paciente manifestó dudas explícitas."
            }
        ]
        notable_data = [
            {
                "title": "Evolución Positiva en Cierre de Agenda",
                "description": "Mejora continua en la aplicación de la técnica de doble opción en las últimas dos semanas.",
                "metric_or_pattern": "Incremento de agendamientos confirmados de +12% respecto al periodo anterior."
            }
        ]
    else:  # developing
        strengths_list = [
            {
                "title": "Receptividad al Feedback y Actitud de Mejora",
                "description": "Aplica de forma inmediata las recomendaciones recibidas en sesiones de calibración.",
                "evidence": "Reducción a cero de interrupciones no deseadas en las últimas simulaciones supervisadas."
            },
            {
                "title": "Conocimiento Técnico del Catálogo Médico",
                "description": "Identifica con precisión las pruebas diagnósticas correspondientes a cada síntoma comunicado.",
                "evidence": "Asignación correcta de especialista en el 100% de los casos tipificados."
            }
        ]
        weaknesses_list = [
            {
                "title": "Contención del Tono Vocal en Situaciones de Tensión",
                "description": "Evitar acelerar el ritmo del habla cuando el usuario plantea objeciones insistentes de precio.",
                "evidence": "Aumento del ritmo a más de 170 palabras por minuto ante objeciones en 3 llamadas auditadas."
            },
            {
                "title": "Estructuración del Cierre en Fases Claras",
                "description": "Completar la verificación del canal de confirmación (SMS/WhatsApp) antes de dar por cerrada la llamada.",
                "evidence": "En 2 simulaciones se omitió comprobar si el teléfono admitía mensajería instantánea."
            },
            {
                "title": "Manejo de la Objeción 'Tengo que pensarlo'",
                "description": "Aprender a explorar con amabilidad qué duda clínica concreta motiva la postergación.",
                "evidence": "Aceptación de la primera evasiva en el 50% de las tentativas comerciales."
            }
        ]
        notable_data = [
            {
                "title": "Foco de Entrenamiento Prioritario",
                "description": "El agente requiere consolidar la cadencia conversacional y la técnica de doble alternativa.",
                "metric_or_pattern": "Frecuencia de doble alternativa: 42% (Meta del ciclo: >75%)"
            }
        ]

    # Summary General
    if status == "completed":
        summary_general = (
            f"Informe de consolidación de ciclo de entrenamiento para {agent_name} ({service_key}). "
            f"El agente ha completado el itinerario formativo con una evaluación global media de {avg_score}/10, "
            "demostrando un progreso muy significativo en el manejo de objeciones sensibles y protocolos de escucha activa. "
            "Se consolidan las fortalezas en argumentación médica y se definen pautas claras para el siguiente periodo."
        )
    else:
        summary_general = (
            f"Ciclo activo de mejora y entrenamiento personalizado para {agent_name} ({service_key}). "
            "El objetivo central de este ciclo es afianzar el protocolo de sondeo consultivo, la superación asertiva "
            "de objeciones de precio y la reducción de tiempos de vacilación en el cierre de llamada. "
            "Incluye simulaciones prácticas con escenarios de dificultad progresiva y clientes de perfil exigente."
        )

    evolution_summary = (
        f"Evolución de {agent_name}: Desde el inicio del ciclo se observa un incremento de consistencia en el "
        "seguimiento del protocolo Boston Medical, pasando de un enfoque puramente informativo a una conversación "
        "consultiva y empática con el paciente."
    )

    # Final Report with objectives_status for completed cycles
    final_report = None
    if status == "completed":
        objs_status = []
        for i, obj in enumerate(general_objectives):
            base_s = round(float(avg_score) - random.uniform(1.0, 1.8), 2)
            final_s = round(float(avg_score) + random.uniform(-0.3, 0.5), 2)
            objs_status.append({
                "title": obj["title"],
                "status": "SUPERADO",
                "base_score": base_s,
                "score": final_s,
                "improvement_delta": round(final_s - base_s, 2),
                "justification": f"Objetivo superado satisfactoriamente. Demostró aplicación consistente en las simulaciones finales."
            })
        for i, obj in enumerate(specific_objectives):
            base_s = round(float(avg_score) - random.uniform(0.8, 1.5), 2)
            final_s = round(float(avg_score) + random.uniform(-0.2, 0.6), 2)
            is_sup = final_s >= 7.5
            objs_status.append({
                "title": obj["title"],
                "status": "SUPERADO" if is_sup else "NO SUPERADO",
                "base_score": base_s,
                "score": final_s,
                "improvement_delta": round(final_s - base_s, 2),
                "justification": "Comportamiento incorporado de forma estable en la operativa telefónica." if is_sup else "Requiere refuerzo adicional en el próximo ciclo."
            })

        final_report = {
            "summary_final": summary_general,
            "evolution_assessment": evolution_summary,
            "next_steps": "Mantener la calibración quincenal y continuar con el siguiente bloque de entrenamiento avanzado.",
            "objectives_status": objs_status
        }

    # Simulation Prompts (2 to 4 for completed cycles, 3 for active)
    if status == "completed":
        if agent_num % 4 == 0:
            prompts_count = 4
        elif agent_num % 2 == 0:
            prompts_count = 3
        else:
            prompts_count = 2
    else:
        prompts_count = 3

    prompts = []
    for p_num in range(1, prompts_count + 1):
        prompt_data = build_roleplay_simulation_prompt(
            prompt_number=p_num,
            agent_name=agent_name,
            service_key=cat_key,
            persona_index=(agent_num * 3 + p_num),
            difficulty_level="alta" if p_num == prompts_count else "media"
        )
        prompts.append(prompt_data)

    return {
        "summary_general": summary_general,
        "evolution_summary": evolution_summary,
        "avg_score": avg_score,
        "tier": tier,
        "strengths_json": strengths_list,
        "weaknesses_json": weaknesses_list,
        "notable_data_json": notable_data,
        "general_objectives_json": general_objectives,
        "specific_objectives_json": specific_objectives,
        "final_report_json": final_report,
        "prompts": prompts,
    }
