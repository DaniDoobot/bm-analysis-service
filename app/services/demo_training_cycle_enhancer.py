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
5. Call Sessions & Realistic Multi-turn Evaluations (transcription with Agente/Cliente turns,
   scores, structured feedback, and boolean criteria)
6. Final Reports with objectives_status evaluation tracking (SUPERADO / NO SUPERADO, base scores, deltas)

NOTE: All content is generic contact center B2B. No medical/clinical references.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import random
from typing import Any, Dict, List, Optional

DEMO_COMPANY_ID = 7


# ─────────────────────────────────────────────────────────────────────────────
# Character Personas Library for Roleplay Simulations
# Generic B2B contact center clients — no medical context
# ─────────────────────────────────────────────────────────────────────────────
PERSONAS_POOL = [
    {
        "name": "Antonio Morales",
        "age": 54,
        "occupation": "Autónomo del sector logístico",
        "emotional_state": "Desconfiado y con prisa",
        "history": "Ha contactado con otros proveedores de servicio sin resultados satisfactorios. Teme perder el tiempo y duda del valor real de la propuesta.",
        "communication_style": "Corto, directo, interrumpe si siente que le leen un guion comercial."
    },
    {
        "name": "Carlos Santillana",
        "age": 47,
        "occupation": "Profesor de secundaria",
        "emotional_state": "Inseguro pero receptivo",
        "history": "Lleva tiempo con una necesidad no resuelta. Le genera inseguridad tomar la decisión solo y necesita sentir que el agente le comprende.",
        "communication_style": "Voz pausada, vacilante, necesita validación y tono empático antes de avanzar."
    },
    {
        "name": "Javier Benítez",
        "age": 62,
        "occupation": "Jubilado reciente de la banca",
        "emotional_state": "Analítico, escéptico con los precios",
        "history": "Ha comparado varias opciones en internet. Exige transparencia total y justificación del precio antes de comprometerse.",
        "communication_style": "Formal, meticuloso, compara con competidores y pregunta por detalles del servicio."
    },
    {
        "name": "Manuel Delgado",
        "age": 39,
        "occupation": "Ingeniero informático",
        "emotional_state": "Frustrado por falta de tiempo",
        "history": "Agenda muy apretada y viajes constantes. Quiso resolver su consulta el mes pasado pero no encontró la flexibilidad horaria que necesitaba.",
        "communication_style": "Rápido, pragmático, quiere respuestas concretas y sin rodeos."
    },
    {
        "name": "Roberto Vidal",
        "age": 58,
        "occupation": "Comerciante minorista",
        "emotional_state": "Reservado y poco dado a comprometerse",
        "history": "Ha tenido experiencias anteriores en las que se sintió presionado. Desconfía cuando percibe urgencia artificial en la conversación.",
        "communication_style": "Poco expresivo al inicio; si el agente genera confianza con escucha real, se muestra más colaborador."
    },
    {
        "name": "Elena Fuertes",
        "age": 44,
        "occupation": "Directora de operaciones en empresa industrial",
        "emotional_state": "Exigente y orientada a resultados",
        "history": "Gestiona varios proveedores y evalúa constantemente la calidad del servicio. Espera que el agente conozca su sector.",
        "communication_style": "Directa, evalúa la capacidad del agente para adaptarse a su contexto sin explicaciones genéricas."
    },
    {
        "name": "Luis Herrera",
        "age": 35,
        "occupation": "Responsable de compras en empresa retail",
        "emotional_state": "Orientado a precio y comparación",
        "history": "Tiene presupuesto ajustado y siempre busca la mejor relación calidad-precio. Pide descuentos con frecuencia.",
        "communication_style": "Negociador, pone a prueba la firmeza del agente en la propuesta de valor."
    },
    {
        "name": "Enrique Gómez",
        "age": 51,
        "occupation": "Abogado laboralista",
        "emotional_state": "Exigente y algo suspicaz",
        "history": "Ha tenido experiencias previas con proveedores que no cumplieron plazos. Valora mucho la transparencia y la concreción.",
        "communication_style": "Dialéctica rápida, busca inconsistencias en el discurso y evalúa la preparación del agente."
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Objectives Catalog — Generic Contact Center (NO medical references)
# Two services: "atencion-al-cliente" and "ventas"
# Each has 6 general and 6 specific objectives for meaningful rotation across 60 agents
# ─────────────────────────────────────────────────────────────────────────────
OBJECTIVES_CATALOG = {
    "atencion-al-cliente": {
        "general": [
            {
                "title": "Escucha Activa y Diagnóstico de Necesidades",
                "description": "Aplicar técnicas de escucha activa para identificar la necesidad real del cliente antes de ofrecer cualquier solución.",
                "rationale": "El 40% de las reclamaciones de segunda llamada se originan en una comprensión incorrecta de la solicitud inicial.",
                "expected_behavior": "Dejar hablar al cliente sin interrumpir durante los primeros 30 segundos, parafrasear la consulta y confirmar la comprensión antes de proponer pasos.",
                "success_indicators": [
                    "Ausencia de interrupciones en los primeros 30 segundos de exposición del cliente.",
                    "Paráfrasis de la consulta registrada en al menos el 85% de las interacciones auditadas."
                ]
            },
            {
                "title": "Resolución en Primera Llamada (FCR)",
                "description": "Resolver la consulta o incidencia del cliente en el primer contacto sin necesidad de derivación o seguimiento posterior.",
                "rationale": "Cada llamada adicional para el mismo problema reduce la satisfacción del cliente en un 18% de media.",
                "expected_behavior": "Identificar el tipo de consulta en los primeros 60 segundos, aplicar el protocolo correspondiente y confirmar con el cliente que la solución es completa antes de cerrar.",
                "success_indicators": [
                    "Tasa de FCR superior al 80% en el trimestre.",
                    "Reducción de llamadas repetidas del mismo cliente en el mismo periodo."
                ]
            },
            {
                "title": "Claridad y Estructura de la Comunicación",
                "description": "Transmitir información de forma ordenada, sin tecnicismos innecesarios y verificando que el cliente comprende cada paso.",
                "rationale": "La falta de claridad genera dudas post-llamada y derivaciones innecesarias al canal escrito.",
                "expected_behavior": "Estructurar la respuesta en tres partes: qué se va a hacer, cómo se va a hacer y en qué plazo; comprobar la comprensión antes de avanzar.",
                "success_indicators": [
                    "Puntuación mínima de 8 en claridad en las auditorías semanales.",
                    "Reducción de consultas de seguimiento del mismo cliente en un 25%."
                ]
            },
            {
                "title": "Gestión de Clientes Difíciles y Situaciones de Tensión",
                "description": "Mantener el control emocional y el tono profesional ante clientes molestos, impacientes o con alta carga emocional.",
                "rationale": "Las llamadas con escalada emocional no gestionada duplican el tiempo medio de atención y reducen la satisfacción.",
                "expected_behavior": "Aplicar la técnica de desescalada (reconocer, disculparse si procede, proponer) y no elevar nunca el tono de voz ni responder de forma defensiva.",
                "success_indicators": [
                    "Cero escaladas a supervisor sin intento previo de resolución directa.",
                    "Satisfacción post-llamada superior a 7.5 en interacciones marcadas como tensas."
                ]
            },
            {
                "title": "Empatía y Orientación al Cliente",
                "description": "Demostrar comprensión genuina de la situación del cliente y adaptar el tono y la respuesta a su estado emocional.",
                "rationale": "El 65% de los clientes que cambian de proveedor citan la falta de trato empático como causa principal.",
                "expected_behavior": "Usar fórmulas de validación emocional naturales ('Entiendo su situación', 'Tiene razón en sentirse así') y adaptar el ritmo de la conversación al del cliente.",
                "success_indicators": [
                    "Registro de al menos una fórmula de empatía genuina por interacción.",
                    "Valoración de empatía superior a 8/10 en encuestas post-contacto."
                ]
            },
            {
                "title": "Cierre Efectivo y Confirmación de Siguientes Pasos",
                "description": "Garantizar que el cliente sale de la llamada con total claridad sobre qué sucede a continuación y en qué plazo.",
                "rationale": "El 30% de las reclamaciones post-atención se deben a expectativas no alineadas sobre plazos o acciones prometidas.",
                "expected_behavior": "Antes de despedirse, resumir las acciones acordadas, confirmar datos de contacto y verificar que el cliente no tiene dudas adicionales.",
                "success_indicators": [
                    "Resumen de cierre presente en el 100% de las interacciones auditadas.",
                    "Reducción de contactos entrantes por falta de información sobre gestiones en curso."
                ]
            }
        ],
        "specific": [
            {
                "title": "Control del Ritmo y Gestión del Tiempo de Llamada",
                "description": "Canalizar conversaciones dispersas o excesivamente largas con reconducciones asertivas y respetuosas.",
                "related_criteria": ["control_tiempo", "gestion_conversacion", "eficiencia"],
                "specific_behavior_to_improve": "Reconducir al cliente cuando se desvíe del motivo de consulta con frases de enlace como 'Entiendo, y para poder ayudarle con eso necesito que...'.",
                "success_indicators": [
                    "Tiempo medio de atención (AHT) dentro del rango objetivo del servicio.",
                    "Sin sensación de prisa transmitida al cliente según auditorías."
                ]
            },
            {
                "title": "Registro y Documentación Precisa en CRM",
                "description": "Capturar la información relevante de la interacción de forma completa y estructurada durante o inmediatamente tras la llamada.",
                "related_criteria": ["documentacion_crm", "calidad_registro", "trazabilidad"],
                "specific_behavior_to_improve": "Completar todos los campos obligatorios del CRM antes de dar la llamada por cerrada; evitar registros parciales o anotaciones en post-llamada.",
                "success_indicators": [
                    "Tasa de cumplimiento de campos obligatorios del CRM superior al 95%.",
                    "Reducción de errores de tipificación en un 20%."
                ]
            },
            {
                "title": "Detección y Escalada Correcta de Incidencias Complejas",
                "description": "Identificar con precisión cuándo una consulta requiere derivación especializada y gestionar el traspaso de forma fluida.",
                "related_criteria": ["deteccion_incidencias", "escalada", "protocolo_derivacion"],
                "specific_behavior_to_improve": "No intentar resolver consultas fuera del ámbito de competencia; derivar con contexto completo para evitar que el cliente repita su exposición.",
                "success_indicators": [
                    "Tasa de derivaciones correctamente contextualizadas superior al 90%.",
                    "Reducción de quejas por gestiones mal derivadas."
                ]
            },
            {
                "title": "Bienvenida Corporativa y Tono de Marca",
                "description": "Mantener el saludo, la presentación y el tono de comunicación alineados con los estándares de calidad de la empresa.",
                "related_criteria": ["protocolo_bienvenida", "tono_marca", "imagen_corporativa"],
                "specific_behavior_to_improve": "Cumplir rigurosamente el guion de bienvenida y despedida adaptando el tono al perfil del cliente, evitando sonidos mecanizados.",
                "success_indicators": [
                    "100% de cumplimiento del protocolo de bienvenida en auditorías.",
                    "Valoración de imagen corporativa superior a 8/10 en encuestas."
                ]
            },
            {
                "title": "Manejo de Objeciones a la Solución Propuesta",
                "description": "Responder con argumentos sólidos y empáticos cuando el cliente rechaza o cuestiona la solución ofrecida.",
                "related_criteria": ["manejo_objeciones", "argumentacion", "resolucion_conflicto"],
                "specific_behavior_to_improve": "Validar la objeción antes de rebatirla; evitar respuestas defensivas o la repetición del mismo argumento en mayor volumen.",
                "success_indicators": [
                    "Resolución de objeción en primera instancia en al menos el 70% de los casos.",
                    "Ausencia de escalada innecesaria a supervisor por objeción no gestionada."
                ]
            },
            {
                "title": "Comprobación de Satisfacción al Cierre",
                "description": "Verificar activamente antes de finalizar la llamada que el cliente considera su consulta resuelta.",
                "related_criteria": ["cierre_satisfaccion", "confirmacion_resolucion", "experiencia_cliente"],
                "specific_behavior_to_improve": "Formular siempre una pregunta de cierre del tipo '¿Queda alguna duda adicional que pueda aclarar?' antes de la despedida.",
                "success_indicators": [
                    "Pregunta de comprobación de satisfacción presente en el 100% de las interacciones auditadas.",
                    "Aumento del índice de satisfacción post-contacto en un 10%."
                ]
            }
        ]
    },
    "ventas": {
        "general": [
            {
                "title": "Apertura Comercial y Generación de Rapport",
                "description": "Establecer desde los primeros segundos de la llamada un clima de confianza que predisponga al cliente a la escucha.",
                "rationale": "Las llamadas donde el agente genera rapport en los primeros 45 segundos tienen un 35% más de tasa de conversión.",
                "expected_behavior": "Presentarse con nombre y empresa, conectar brevemente con el contexto del cliente y formular una pregunta abierta inicial antes de entrar en la propuesta.",
                "success_indicators": [
                    "Tiempo de generación de rapport inferior a 60 segundos en el 80% de las llamadas.",
                    "Valoración de naturalidad y conexión inicial superior a 8/10 en auditorías."
                ]
            },
            {
                "title": "Sondeo Consultivo y Detección de Necesidades",
                "description": "Formular preguntas abiertas de impacto para comprender la situación real del cliente antes de presentar cualquier propuesta.",
                "rationale": "Las propuestas presentadas antes de entender la necesidad real son rechazadas en más del 60% de los casos.",
                "expected_behavior": "Realizar al menos tres preguntas abiertas de diagnóstico antes de pasar a la fase de propuesta; escuchar sin completar las frases del cliente.",
                "success_indicators": [
                    "Mínimo de tres preguntas abiertas registradas por llamada en el 85% de las interacciones.",
                    "Identificación clara del dolor principal del cliente anotada en CRM."
                ]
            },
            {
                "title": "Presentación de Propuesta de Valor Diferencial",
                "description": "Articular de forma clara y personalizada por qué la solución propuesta responde mejor a las necesidades del cliente que las alternativas.",
                "rationale": "Los clientes que no perciben diferenciación real deciden por precio exclusivamente, reduciendo el margen.",
                "expected_behavior": "Construir la propuesta sobre los dolores detectados en el sondeo; evitar presentaciones genéricas que no conecten con el contexto del cliente.",
                "success_indicators": [
                    "Propuesta de valor personalizada (mencionando al menos un dolor detectado) en el 90% de las llamadas.",
                    "Reducción del porcentaje de objeciones de precio en un 15%."
                ]
            },
            {
                "title": "Manejo Riguroso de Objeciones",
                "description": "Responder a las objeciones del cliente con argumentos concretos, empáticos y orientados al valor, sin entrar en confrontación.",
                "rationale": "El 70% de los cierres fallidos ocurren en la fase de objeción cuando el agente cede demasiado rápido o responde de forma defensiva.",
                "expected_behavior": "Aplicar la técnica de validar-reformular-argumentar; nunca rebajar el precio sin explorar primero el argumento de valor.",
                "success_indicators": [
                    "Superación documentada de la objeción principal en al menos 6 de cada 10 interacciones.",
                    "Ausencia de descuentos no autorizados ofrecidos espontáneamente."
                ]
            },
            {
                "title": "Cierre Asertivo con Alternativa de Decisión",
                "description": "Conducir al cliente a una decisión concreta mediante la técnica de la doble alternativa u otras técnicas de cierre estructurado.",
                "rationale": "Preguntar de forma abierta '¿Le interesa?' genera indecisión; ofrecer dos opciones concretas incrementa el cierre en un 30%.",
                "expected_behavior": "Utilizar la técnica de doble opción ('¿Prefiere iniciar la semana que viene o el día 1 del próximo mes?') y no aceptar el primer aplazamiento sin explorar la causa.",
                "success_indicators": [
                    "Incremento del ratio de conversión a compromiso en un 20%.",
                    "Técnica de doble alternativa registrada en el 80% de las llamadas auditadas."
                ]
            },
            {
                "title": "Seguimiento y Fidelización Post-Venta",
                "description": "Asegurar que los compromisos adquiridos durante la llamada se cumplen y que el cliente percibe continuidad en la relación.",
                "rationale": "El 45% del crecimiento de negocio en cuentas existentes se genera por un seguimiento proactivo bien ejecutado.",
                "expected_behavior": "Registrar todos los compromisos en CRM con fecha límite; contactar en el plazo acordado aunque no haya novedades para mantener la relación activa.",
                "success_indicators": [
                    "100% de los compromisos de llamada de seguimiento ejecutados en el plazo acordado.",
                    "Tasa de conversión de leads con seguimiento activo superior al 35%."
                ]
            }
        ],
        "specific": [
            {
                "title": "Desmontaje de la Objeción 'Tengo que pensarlo'",
                "description": "Averiguar con delicadeza qué duda concreta impide al cliente tomar la decisión y ofrecer información que la resuelva.",
                "related_criteria": ["manejo_objeciones", "desmontaje_dudas", "cierre_consultivo"],
                "specific_behavior_to_improve": "No aceptar el primer aplazamiento como respuesta definitiva; explorar con una pregunta abierta qué información adicional necesita el cliente para decidir.",
                "success_indicators": [
                    "Conversión de al menos el 40% de los aplazamientos en avances o compromisos concretos.",
                    "Pregunta de exploración de duda formulada en el 100% de los casos de postergación."
                ]
            },
            {
                "title": "Argumentación de Valor frente a Propuesta de Competidor",
                "description": "Defender la propuesta propia frente a alternativas competidoras sin descalificar a otros proveedores.",
                "related_criteria": ["posicionamiento", "argumentacion_diferencial", "etica_comercial"],
                "specific_behavior_to_improve": "Centrar la respuesta en las fortalezas y diferenciadores propios, evitando comparaciones directas que puedan generar desconfianza.",
                "success_indicators": [
                    "Mención de al menos dos diferenciadores clave en las llamadas donde aparece comparativa con competidor.",
                    "Ausencia de comentarios negativos sobre la competencia en auditorías."
                ]
            },
            {
                "title": "Generación de Urgencia o Valor Temporal sin Presión Artificial",
                "description": "Comunicar condiciones de vigencia de la propuesta o ventajas asociadas al momento de decisión de forma honesta y natural.",
                "related_criteria": ["urgencia_valor", "cierre_temporal", "naturalidad_comercial"],
                "specific_behavior_to_improve": "Mencionar plazos o condiciones reales sin recurrir a presiones artificiales del tipo 'si no decide ahora no podré asegurar el precio'.",
                "success_indicators": [
                    "Mención de condiciones de vigencia en el 70% de las llamadas con decisión pendiente.",
                    "Ausencia de quejas por presión comercial excesiva."
                ]
            },
            {
                "title": "Detección de Oportunidades de Venta Adicional o Cruzada",
                "description": "Identificar en la conversación necesidades no declaradas que puedan resolverse con servicios adicionales del catálogo.",
                "related_criteria": ["upselling", "cross_selling", "deteccion_oportunidades"],
                "specific_behavior_to_improve": "No limitar la conversación al producto inicial; explorar con una pregunta abierta si hay otras áreas en las que el cliente pueda necesitar apoyo.",
                "success_indicators": [
                    "Al menos una propuesta de venta adicional en el 30% de las llamadas con cliente existente.",
                    "Tasa de aceptación de propuesta adicional superior al 20%."
                ]
            },
            {
                "title": "Confirmación de Compromiso y Siguiente Paso Concreto",
                "description": "Asegurar que cada llamada de ventas termina con un compromiso claro y fechado por parte del cliente.",
                "related_criteria": ["cierre", "compromiso_cliente", "seguimiento_agenda"],
                "specific_behavior_to_improve": "No cerrar ninguna llamada sin acordar una acción concreta con fecha: envío de propuesta, llamada de seguimiento o formalización.",
                "success_indicators": [
                    "Siguiente paso con fecha acordado en el 95% de las llamadas de ventas.",
                    "Reducción de leads sin actividad posterior a la primera llamada."
                ]
            },
            {
                "title": "Gestión de Objeción de Precio y Defensa del Margen",
                "description": "Responder a la presión de precio del cliente manteniendo el valor de la propuesta sin ceder al descuento inmediato.",
                "related_criteria": ["defensa_margen", "objecion_precio", "negociacion"],
                "specific_behavior_to_improve": "Antes de ofrecer cualquier descuento, explorar si el rechazo es por precio real o por falta de percepción de valor; agotar los argumentos de valor primero.",
                "success_indicators": [
                    "Descuentos espontáneos no autorizados reducidos a cero.",
                    "Porcentaje de cierres a precio de lista superior al objetivo del periodo."
                ]
            }
        ]
    }
}


# ─────────────────────────────────────────────────────────────────────────────
# Strengths and Weaknesses by Service and Tier
# Variable pools with enough entries to rotate deterministically per agent
# ─────────────────────────────────────────────────────────────────────────────
_SW_POOL = {
    "atencion-al-cliente": {
        "top": {
            "strengths": [
                {
                    "title": "Escucha Activa Sostenida",
                    "description": "Mantiene la escucha sin interrumpir incluso en llamadas complejas o de larga duración, lo que genera confianza inmediata en el cliente.",
                    "evidence": "En el 94% de las interacciones auditadas no se registran interrupciones antes de que el cliente concluya su exposición inicial."
                },
                {
                    "title": "Comunicación Clara y Estructurada",
                    "description": "Explica los pasos del proceso con un orden lógico y verifica la comprensión antes de avanzar, reduciendo dudas posteriores.",
                    "evidence": "Puntuación media de 9.1/10 en claridad de comunicación en auditorías del trimestre."
                },
                {
                    "title": "Resolución Autónoma en Primera Llamada",
                    "description": "Resuelve la mayoría de las consultas sin necesidad de derivación ni seguimiento posterior, optimizando el tiempo del cliente.",
                    "evidence": "Tasa de resolución en primera llamada del 87%, por encima del objetivo del equipo en un 12%."
                },
                {
                    "title": "Gestión Eficaz de Clientes con Alta Carga Emocional",
                    "description": "Consigue desescalar situaciones de tensión sin elevar el tono y manteniendo el control de la conversación.",
                    "evidence": "Cero escaladas a supervisor por situaciones de tensión no gestionadas en el último trimestre."
                },
                {
                    "title": "Cierre Completo con Confirmación de Siguientes Pasos",
                    "description": "Garantiza que el cliente sale de la llamada con claridad total sobre las acciones acordadas y los plazos comprometidos.",
                    "evidence": "Resumen de cierre registrado en el 100% de las interacciones auditadas."
                },
                {
                    "title": "Empatía Natural y Adaptación al Estilo del Cliente",
                    "description": "Ajusta el tono y la cadencia de la conversación al perfil del cliente sin que resulte artificial o forzado.",
                    "evidence": "Valoración de empatía de 9.2/10 de media en encuestas post-contacto del periodo."
                },
            ],
            "weaknesses": [
                {
                    "title": "Optimización del Tiempo en Conversaciones Extendidas",
                    "description": "En ocasiones permite que la conversación se extienda más de lo necesario una vez que el asunto está resuelto.",
                    "evidence": "Se detectan despedidas de más de 90 segundos en 1 de cada 5 llamadas completadas."
                },
                {
                    "title": "Registro Sistemático en CRM durante la Llamada",
                    "description": "Mejorar el hábito de documentar la información clave en tiempo real en lugar de hacerlo en post-llamada.",
                    "evidence": "En el 15% de las interacciones el registro se completa más de 3 minutos después del cierre."
                },
            ],
        },
        "solid": {
            "strengths": [
                {
                    "title": "Consistencia en el Protocolo de Atención",
                    "description": "Cumple de forma rigurosa el flujo de bienvenida, identificación y presentación del servicio en todas las interacciones.",
                    "evidence": "100% de cumplimiento del protocolo de bienvenida en auditorías del mes."
                },
                {
                    "title": "Buena Gestión del Tiempo de Llamada",
                    "description": "Mantiene las interacciones dentro del rango de duración óptimo sin generar sensación de prisa en el cliente.",
                    "evidence": "Tiempo medio de atención de 4 minutos 52 segundos, alineado con el objetivo del servicio."
                },
                {
                    "title": "Resolución Adecuada de Consultas Estándar",
                    "description": "Gestiona con seguridad las tipologías de consulta más frecuentes aplicando el protocolo correspondiente.",
                    "evidence": "FCR del 76% en consultas tipificadas como estándar, dentro del rango del equipo."
                },
                {
                    "title": "Tono Profesional y Orientado al Cliente",
                    "description": "Mantiene una actitud cordial y orientada a la solución incluso en interacciones de mayor duración.",
                    "evidence": "Valoración de trato de 8.1/10 de media en encuestas de satisfacción del periodo."
                },
            ],
            "weaknesses": [
                {
                    "title": "Mayor Profundidad en el Diagnóstico de la Necesidad",
                    "description": "Tiende a avanzar hacia la solución antes de haber verificado completamente la necesidad del cliente.",
                    "evidence": "En el 35% de las llamadas auditadas se ofrece una solución antes de haber formulado una pregunta de verificación."
                },
                {
                    "title": "Uso más Consistente de Preguntas Abiertas de Exploración",
                    "description": "Utilizar preguntas abiertas con mayor frecuencia para que el cliente pueda expresar su situación con sus propias palabras.",
                    "evidence": "Solo el 45% de las llamadas incluyen al menos dos preguntas abiertas antes de la fase de resolución."
                },
            ],
        },
        "developing": {
            "strengths": [
                {
                    "title": "Receptividad al Feedback y Actitud de Mejora",
                    "description": "Aplica con rapidez las recomendaciones recibidas en sesiones de calibración, mostrando una actitud proactiva hacia el desarrollo.",
                    "evidence": "Reducción de las principales áreas de mejora identificadas en el mes anterior en un 40%."
                },
                {
                    "title": "Capacidad para Resolver Consultas Simples con Eficacia",
                    "description": "Gestiona las consultas de tipología sencilla con fluidez y sin necesidad de apoyo adicional.",
                    "evidence": "FCR del 68% en consultas básicas, por encima del objetivo inicial para este nivel de experiencia."
                },
            ],
            "weaknesses": [
                {
                    "title": "Control del Tono en Situaciones de Tensión",
                    "description": "Tiende a acelerar el ritmo del habla o a elevar ligeramente el tono cuando el cliente muestra impaciencia o enfado.",
                    "evidence": "Se detecta aumento del ritmo de voz ante presión en 4 de las últimas 10 llamadas auditadas."
                },
                {
                    "title": "Estructuración del Cierre y Confirmación de Pasos",
                    "description": "Mejorar el hábito de resumir los acuerdos alcanzados y confirmar los siguientes pasos antes de la despedida.",
                    "evidence": "Resumen de cierre ausente en el 55% de las interacciones del mes."
                },
                {
                    "title": "Escucha Completa sin Adelantar la Solución",
                    "description": "Evitar ofrecer la respuesta antes de que el cliente haya terminado de exponer su situación.",
                    "evidence": "Se registran interrupciones prematuras en el 30% de las llamadas auditadas."
                },
            ],
        },
    },
    "ventas": {
        "top": {
            "strengths": [
                {
                    "title": "Sondeo Consultivo Profundo y Personalizado",
                    "description": "Construye la conversación desde la necesidad del cliente, formulando preguntas de impacto que revelan motivaciones no declaradas.",
                    "evidence": "Media de 4.2 preguntas abiertas por llamada, frente a la media del equipo de 2.8."
                },
                {
                    "title": "Propuesta de Valor Diferenciada y Contextualizada",
                    "description": "Articula el valor de la propuesta conectando directamente con los problemas identificados en el sondeo.",
                    "evidence": "En el 91% de las llamadas la propuesta incluye referencia explícita a un dolor detectado en el sondeo."
                },
                {
                    "title": "Cierre Asertivo con Doble Alternativa",
                    "description": "Conduce al cliente a una decisión concreta con técnicas de cierre estructurado sin generar presión percibida.",
                    "evidence": "Ratio de conversión a compromiso del 68%, un 22% por encima de la media del equipo."
                },
                {
                    "title": "Manejo Sólido de Objeciones de Precio",
                    "description": "Responde a la presión de precio con argumentos de valor antes de contemplar cualquier condición especial.",
                    "evidence": "Porcentaje de cierres a precio de lista del 74%, por encima del objetivo del periodo."
                },
                {
                    "title": "Generación de Confianza y Rapport Rápido",
                    "description": "Establece una conexión genuina con el cliente en los primeros instantes que facilita la apertura a la conversación comercial.",
                    "evidence": "Valoración de naturalidad y cercanía de 9.0/10 en auditorías del mes."
                },
            ],
            "weaknesses": [
                {
                    "title": "Detección Sistemática de Oportunidades de Venta Adicional",
                    "description": "Incorporar con más consistencia una exploración de necesidades secundarias en llamadas con clientes existentes.",
                    "evidence": "Propuesta de venta cruzada presente en solo el 22% de las llamadas con cartera activa."
                },
                {
                    "title": "Documentación Inmediata del Compromiso en CRM",
                    "description": "Registrar el siguiente paso acordado durante la llamada, no en el momento de post-procesado.",
                    "evidence": "En el 18% de las llamadas el compromiso de seguimiento se anota más de 10 minutos después del cierre."
                },
            ],
        },
        "solid": {
            "strengths": [
                {
                    "title": "Buena Apertura Comercial y Generación de Interés",
                    "description": "Consigue que el cliente mantenga el interés en los primeros minutos de la llamada con una presentación clara y natural.",
                    "evidence": "Tasa de abandono en los primeros 90 segundos inferior al 8%, por debajo de la media del equipo."
                },
                {
                    "title": "Solidez en el Manejo de Objeciones Habituales",
                    "description": "Responde con seguridad a las objeciones más frecuentes sin mostrar dudas ni ceder rápidamente.",
                    "evidence": "Superación de objeción principal en el 62% de las interacciones, dentro del objetivo del equipo."
                },
                {
                    "title": "Compromiso con el Protocolo de Cierre",
                    "description": "Aplica de forma consistente los pasos de cierre definidos y acuerda un siguiente paso en la mayoría de las llamadas.",
                    "evidence": "Siguiente paso con fecha registrado en el 78% de las llamadas de ventas."
                },
                {
                    "title": "Comunicación Clara del Valor de la Propuesta",
                    "description": "Explica las características y ventajas del servicio de forma ordenada y comprensible.",
                    "evidence": "Claridad de propuesta valorada con 7.8/10 de media en auditorías del periodo."
                },
            ],
            "weaknesses": [
                {
                    "title": "Profundizar en el Sondeo antes de Presentar la Propuesta",
                    "description": "Tiende a pasar a la fase de propuesta antes de haber explorado suficientemente la situación y necesidades del cliente.",
                    "evidence": "En el 48% de las llamadas se presenta la propuesta sin haber formulado más de una pregunta de sondeo."
                },
                {
                    "title": "Mejor Respuesta ante la Objeción de Postergación",
                    "description": "Ante aplazamientos del tipo 'ya llamaré', explorar la duda concreta en lugar de aceptar la respuesta y cerrar la llamada.",
                    "evidence": "El 55% de los aplazamientos se cierran sin exploración de la causa real del retraso."
                },
            ],
        },
        "developing": {
            "strengths": [
                {
                    "title": "Actitud Proactiva y Orientación al Resultado",
                    "description": "Muestra motivación genuina por cerrar cada llamada con un resultado positivo y aplica las técnicas aprendidas con voluntad.",
                    "evidence": "Mejora progresiva del ratio de compromiso tras cada sesión de calibración."
                },
                {
                    "title": "Claridad en la Presentación Básica del Servicio",
                    "description": "Explica los elementos principales de la propuesta de forma comprensible para el cliente.",
                    "evidence": "Valoración de claridad de propuesta de 7.0/10, dentro del rango esperado para el nivel de experiencia."
                },
            ],
            "weaknesses": [
                {
                    "title": "Mayor Sondeo Antes de la Argumentación",
                    "description": "Tiende a entrar directamente en la propuesta sin haber identificado la necesidad específica del cliente, lo que reduce la efectividad del argumento.",
                    "evidence": "Media de 0.8 preguntas abiertas por llamada, por debajo del mínimo recomendado de 2."
                },
                {
                    "title": "Gestión de la Objeción de Precio sin Ceder Inmediatamente",
                    "description": "Ante la primera objeción económica, ofrece condiciones especiales antes de agotar los argumentos de valor disponibles.",
                    "evidence": "Descuento espontáneo ofrecido en el 40% de las llamadas donde el cliente objeta el precio."
                },
                {
                    "title": "Cierre con Alternativa Concreta en lugar de Pregunta Abierta",
                    "description": "Sustituir cierres abiertos del tipo '¿le interesa?' por técnicas de doble alternativa que faciliten la toma de decisión.",
                    "evidence": "Técnica de doble alternativa presente solo en el 18% de los intentos de cierre."
                },
            ],
        },
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Justification Templates per Objective Title — variable by tier and score
# ─────────────────────────────────────────────────────────────────────────────

def _generate_justification(
    obj_title: str,
    status: str,
    tier: str,
    agent_num: int,
    score: float,
    base_score: float,
    service_key: str,
) -> str:
    """
    Generates a specific justification for each objective based on its title,
    the agent's tier, score evolution, and service context.
    """
    delta = round(score - base_score, 2)
    delta_str = f"+{delta}" if delta >= 0 else str(delta)
    score_str = str(round(score, 1))
    base_str = str(round(base_score, 1))
    variant = (agent_num + hash(obj_title[:8])) % 3
    title_lower = obj_title.lower()

    if any(k in title_lower for k in ["escucha", "sondeo", "diagnóstico de necesidad", "detección"]):
        if status == "SUPERADO":
            opts = [
                f"En las simulaciones recientes {obj_title.lower()} muestra una mejora clara: el agente espera a que el cliente complete su exposición antes de responder y formula preguntas de exploración que revelan necesidades no declaradas. La puntuación ha pasado de {base_str} a {score_str} ({delta_str}).",
                f"Se consolida la mejora en {obj_title.lower()}. El agente estructura el sondeo con preguntas abiertas de impacto y utiliza los datos recogidos para personalizar la respuesta. Evolución de {base_str} a {score_str}.",
                f"El trabajo en {obj_title.lower()} es visible en los registros: el agente dedica más tiempo a la fase de exploración antes de avanzar hacia la solución, lo que mejora la precisión de la respuesta. Nota final: {score_str} (inicio: {base_str}).",
            ]
        else:
            opts = [
                f"En {obj_title.lower()} se observa mejora parcial pero insuficiente para superar el objetivo. El agente aún tiende a adelantar la respuesta antes de completar la fase de exploración. Puntuación actual: {score_str}.",
                f"El objetivo de {obj_title.lower()} requiere refuerzo. Las simulaciones muestran que el agente formula preguntas cerradas con mayor frecuencia de la esperada.",
                f"Aunque hay progreso en {obj_title.lower()}, el agente no ha alcanzado el umbral de consistencia requerido. Se recomienda práctica adicional con escenarios de exploración en el próximo ciclo.",
            ]
    elif any(k in title_lower for k in ["resolución", "fcr", "incidencia", "escalada"]):
        if status == "SUPERADO":
            opts = [
                f"El agente ha mejorado notablemente en {obj_title.lower()}: resuelve la mayoría de las consultas en el primer contacto sin necesidad de derivación. Puntuación {base_str} → {score_str} ({delta_str}).",
                f"La tasa de {obj_title.lower()} ha crecido de forma constante durante el ciclo. El agente identifica con más rapidez el protocolo correspondiente a cada tipo de consulta y lo aplica de forma autónoma.",
                f"Los registros del ciclo confirman el avance en {obj_title.lower()}. El agente gestiona con criterio cuándo resolver directamente y cuándo derivar con contexto completo. Mejora de {delta_str} puntos.",
            ]
        else:
            opts = [
                f"El objetivo de {obj_title.lower()} no se ha superado en este ciclo. El agente aún realiza derivaciones en casos que podrían resolverse directamente.",
                f"Se necesita trabajo adicional en {obj_title.lower()}. Los registros muestran que el agente no aplica el protocolo de forma consistente en todas las tipologías de consulta.",
                f"En {obj_title.lower()} la evolución es positiva pero insuficiente para el objetivo fijado. Se propone refuerzo específico con escenarios de consulta compleja en el siguiente ciclo.",
            ]
    elif any(k in title_lower for k in ["claridad", "comunicación", "estructura", "explicación"]):
        if status == "SUPERADO":
            opts = [
                f"La mejora en {obj_title.lower()} es evidente en las últimas simulaciones: el agente estructura la respuesta en partes claras y comprueba que el cliente ha entendido antes de avanzar. Evolución: {base_str} → {score_str}.",
                f"El agente ha consolidado el hábito de {obj_title.lower()} durante el ciclo. La organización de la información es más lógica y el uso de tecnicismos innecesarios ha disminuido de forma apreciable.",
                f"Los registros del ciclo muestran una comunicación más clara y ordenada. El agente utiliza resúmenes intermedios con más frecuencia. Nota {score_str}.",
            ]
        else:
            opts = [
                f"En {obj_title.lower()} todavía se observan respuestas poco estructuradas en escenarios de mayor complejidad. El agente necesita reforzar el hábito de organizar la información antes de transmitirla.",
                f"El objetivo de {obj_title.lower()} requiere más trabajo. Las auditorías reflejan que el cliente necesita pedir aclaraciones con más frecuencia de la esperada.",
                f"Aunque mejora respecto al inicio, el nivel de {obj_title.lower()} no alcanza el estándar definido.",
            ]
    elif any(k in title_lower for k in ["empatía", "tensión", "difícil", "emocional", "tono"]):
        if status == "SUPERADO":
            opts = [
                f"El agente ha demostrado una mejora significativa en {obj_title.lower()} durante el ciclo. Mantiene el control emocional y el tono profesional incluso en interacciones de alta tensión. Puntuación {score_str} ({delta_str} vs inicio).",
                f"Las simulaciones con clientes difíciles muestran que el agente aplica correctamente la desescalada: valida la emoción del cliente, propone soluciones y no eleva el tono. Objetivo superado con {score_str}.",
                f"El trabajo en {obj_title.lower()} es visible: el agente utiliza fórmulas de empatía de forma natural. Evolución de {base_str} a {score_str}.",
            ]
        else:
            opts = [
                f"En {obj_title.lower()} se observan reacciones defensivas o de aceleración del ritmo cuando el cliente eleva la presión. Es necesario reforzar las técnicas de desescalada.",
                f"El objetivo de {obj_title.lower()} requiere refuerzo. El agente aún no aplica de forma consistente las fórmulas de validación emocional cuando el cliente está molesto.",
                f"La mejora en {obj_title.lower()} ha sido parcial. En situaciones de baja tensión el agente gestiona bien, pero ante presión sostenida pierde el hilo del protocolo de desescalada.",
            ]
    elif any(k in title_lower for k in ["cierre", "siguiente", "compromiso", "confirmación"]):
        if status == "SUPERADO":
            opts = [
                f"El agente ha consolidado el hábito de cierre estructurado durante el ciclo: resume los acuerdos, confirma los datos clave y verifica que el cliente no tiene dudas antes de despedirse. Nota {score_str} ({delta_str}).",
                f"En {obj_title.lower()} la mejora es consistente en las últimas semanas. El agente aplica el protocolo de cierre en todas las interacciones, incluso en aquellas de mayor duración.",
                f"Los registros confirman el avance en {obj_title.lower()}. El siguiente paso queda acordado con fecha en el 95% de las interacciones del cierre del ciclo. Evolución de {base_str} a {score_str}.",
            ]
        else:
            opts = [
                f"El objetivo de {obj_title.lower()} no se ha alcanzado de forma consistente. En el 40% de las llamadas no se registra resumen de cierre ni confirmación de pasos con el cliente.",
                f"En {obj_title.lower()} el agente muestra irregularidad: en algunos días aplica el protocolo correctamente pero en otros lo omite bajo presión de tiempo.",
                f"Quedan aspectos a mejorar en {obj_title.lower()}. El cliente sale de algunas llamadas sin claridad sobre qué ocurrirá a continuación.",
            ]
    elif any(k in title_lower for k in ["objeción", "precio", "negociación", "margen", "valor"]):
        if status == "SUPERADO":
            opts = [
                f"En las simulaciones finales el agente identifica primero el tipo de objeción antes de responder y evita ceder inmediatamente al descuento. Consigue reconducir la conversación hacia el valor de la propuesta. Nota {score_str} ({delta_str}).",
                f"El trabajo en {obj_title.lower()} ha producido resultados medibles: el agente agota los argumentos de valor antes de contemplar condiciones especiales.",
                f"El objetivo de {obj_title.lower()} queda superado. El agente responde a la presión de precio con firmeza y empatía. Evolución {base_str} → {score_str}.",
            ]
        else:
            opts = [
                f"En {obj_title.lower()} el agente todavía ofrece descuentos espontáneos antes de agotar los argumentos de valor.",
                f"El objetivo de {obj_title.lower()} requiere más trabajo. El agente cede con demasiada rapidez ante la primera objeción económica.",
                f"Hay progreso en {obj_title.lower()} pero no suficiente para superar el objetivo. Las simulaciones muestran inconsistencia.",
            ]
    elif any(k in title_lower for k in ["rapport", "apertura", "confianza", "relación"]):
        if status == "SUPERADO":
            opts = [
                f"El agente ha mejorado de forma notable en {obj_title.lower()}: genera conexión con el cliente en los primeros 60 segundos de llamada. Puntuación {score_str}.",
                f"La mejora en {obj_title.lower()} es visible en los indicadores de retención en los primeros segundos. Evolución de {base_str} a {score_str}.",
                f"El objetivo de {obj_title.lower()} queda superado. Los registros muestran que el agente abre la conversación con una pregunta personalizada y no con el guión estándar.",
            ]
        else:
            opts = [
                f"En {obj_title.lower()} el agente aún utiliza el guión de apertura de forma mecánica sin adaptarlo al perfil del cliente.",
                f"El objetivo de {obj_title.lower()} no se ha alcanzado en este ciclo. El agente necesita trabajar la naturalidad en la apertura.",
                f"Hay espacio de mejora claro en {obj_title.lower()}. Las simulaciones muestran que el agente no genera suficiente interés en los primeros momentos.",
            ]
    elif any(k in title_lower for k in ["seguimiento", "fidelización", "adicional", "cruzada"]):
        if status == "SUPERADO":
            opts = [
                f"El agente ha incorporado de forma consistente el hábito de {obj_title.lower()} durante el ciclo. Los compromisos de seguimiento se registran con fecha y se ejecutan en el plazo acordado. Nota {score_str}.",
                f"Los registros muestran que el agente detecta con más frecuencia oportunidades de {obj_title.lower()} en llamadas con clientes existentes.",
                f"El objetivo de {obj_title.lower()} queda superado. El agente cierra cada llamada con un siguiente paso concreto y fechado.",
            ]
        else:
            opts = [
                f"En {obj_title.lower()} el agente todavía no ha incorporado el hábito de forma consistente. El 35% de las llamadas terminan sin un siguiente paso registrado en CRM.",
                f"El objetivo de {obj_title.lower()} requiere refuerzo. Las oportunidades de venta adicional no se detectan de forma sistemática.",
                f"Hay trabajo pendiente en {obj_title.lower()}. El agente necesita incorporar preguntas de exploración de necesidades adicionales.",
            ]
    else:
        if status == "SUPERADO":
            opts = [
                f"El agente ha demostrado progreso sostenido en {obj_title.lower()} durante el ciclo. La nota ha evolucionado de {base_str} a {score_str} ({delta_str}), reflejando una aplicación más consistente en las simulaciones.",
                f"El objetivo de {obj_title.lower()} queda superado. Las últimas simulaciones muestran una aplicación más estable y autónoma del comportamiento esperado. Puntuación final: {score_str}.",
                f"La mejora en {obj_title.lower()} es consistente con el perfil de {tier}. El agente aplica el comportamiento requerido con regularidad: {score_str} vs {base_str} al inicio.",
            ]
        else:
            opts = [
                f"El objetivo de {obj_title.lower()} no se ha alcanzado en este ciclo. Aunque hay mejora ({base_str} → {score_str}), la consistencia no es suficiente.",
                f"Se requiere refuerzo adicional en {obj_title.lower()}. El agente muestra conocimiento del objetivo pero no lo aplica de forma homogénea.",
                f"Hay progreso en {obj_title.lower()} pero el nivel alcanzado ({score_str}) no cumple el objetivo del ciclo.",
            ]

    return opts[variant % len(opts)]


# ─────────────────────────────────────────────────────────────────────────────
# Prompt Builder: Deep, Realistic Roleplay Prompts (>600 chars)
# Generic contact center — no medical context
# ─────────────────────────────────────────────────────────────────────────────
def build_roleplay_simulation_prompt(
    prompt_number: int,
    agent_name: str,
    service_key: str,
    persona_index: int,
    difficulty_level: str = "medio"
) -> Dict[str, Any]:
    persona = PERSONAS_POOL[persona_index % len(PERSONAS_POOL)]
    service_name = "Atención al Cliente" if service_key == "atencion-al-cliente" else "Ventas y Desarrollo Comercial"

    if service_key == "atencion-al-cliente":
        scenarios = [
            {
                "title": f"Simulación {prompt_number}: Incidencia Técnica en Servicio y Retraso en Operativa",
                "scenario_type": "roleplay",
                "focus": ["Escucha activa", "Gestión emocional", "Resolución en primera llamada"],
                "objective_summary": "Atender a un cliente que contacta molesto por una incidencia técnica no resuelta en contactos anteriores.",
                "expected_behavior": "Aplicar escucha activa, reconocer la situación sin ponerse a la defensiva y proponer una solución concreta con plazo definido.",
                "why_calling": "Llama porque su plataforma de servicio sufrió una interrupción y su consulta previa sigue sin resolverse.",
                "level_1": "Saluda con tono tenso y expone brevemente su situación esperando ser interrumpido.",
                "level_2": "Expresa frustración porque siente que le han pasado de un departamento a otro sin solución.",
                "level_3": "Afirma que si no recibe una solución hoy mismo estudiará cambiar de proveedor de servicio.",
                "level_4": "Pregunta por qué nadie le informó de los plazos reales de resolución desde el principio.",
                "level_5": "Si el agente escucha con paciencia, reconoce el error y propone un plan concreto con hora límite, el cliente acepta la solución."
            },
            {
                "title": f"Simulación {prompt_number}: Discrepancia en Facturación y Aclaración de Cargos",
                "scenario_type": "roleplay",
                "focus": ["Claridad de comunicación", "Rigor en la información", "Resolución de conflictos"],
                "objective_summary": "Aclarar de forma transparente y empática un desglose de facturación a un cliente que detecta un importe inesperado.",
                "expected_behavior": "Revisar el detalle de la factura con rigor, explicar de forma comprensible los conceptos y tramitar el ajuste si procede.",
                "why_calling": "Ha recibido una factura con un concepto adicional de servicio que no reconoce y teme un cobro indebido.",
                "level_1": "Expone su queja de forma ordenada pero con evidente tono de desconfianza.",
                "level_2": "Insiste en que lo que le dijeron al contratar no coincide con lo cobrado en este periodo.",
                "level_3": "Pide que le envíen por escrito el desglose detallado y exige la devolución inmediata.",
                "level_4": "Pregunta qué opciones tiene si considera que le aplicaron una tarifa diferente a la acordada.",
                "level_5": "Si el agente clarifica los conceptos con datos objetivos y tramita la regularización oportuna, el cliente se tranquiliza."
            },
            {
                "title": f"Simulación {prompt_number}: Solicitud Urgente con Agenda Restringida y Cliente con Prisa",
                "scenario_type": "roleplay",
                "focus": ["Gestión del tiempo", "Flexibilidad y soluciones alternativas", "Cierre y siguientes pasos"],
                "objective_summary": "Gestionar la solicitud de un cliente que necesita atención inmediata pero las opciones estándar no encajan con sus horarios.",
                "expected_behavior": "Reconducir la expectativa de inmediatez sin transmitir prisa, explorar alternativas viables y acordar una solución colaborativa.",
                "why_calling": "Tiene una urgencia operativa en su empresa y necesita soporte técnico fuera de la franja habitual.",
                "level_1": "Entra en la llamada con prisa y expone la urgencia operativa de su situación.",
                "level_2": "Muestra impaciencia ante la primera respuesta del agente sobre disponibilidad estándar.",
                "level_3": "Exige que se le asigne soporte técnico hoy mismo independientemente de los procedimientos ordinarios.",
                "level_4": "Pregunta si hay alguna vía de escalada de urgencia para agilizar la gestión.",
                "level_5": "Si el agente propone una alternativa viable y la explica con claridad, el cliente acepta y confirma el siguiente paso."
            },
            {
                "title": f"Simulación {prompt_number}: Modificación de Condiciones Contractuales y Cambio de Modalidad",
                "scenario_type": "roleplay",
                "focus": ["Información de productos", "Asesoramiento de servicio", "Claridad de condiciones"],
                "objective_summary": "Guiar a un cliente que solicita modificar su plan actual hacia la modalidad que mejor resuelve su necesidad operativa.",
                "expected_behavior": "Sondear las causas del cambio, explicar las diferencias de cobertura de forma sencilla y confirmar el trámite por escrito.",
                "why_calling": "Llama para solicitar un cambio de modalidad de servicio porque su volumen de trabajo ha cambiado.",
                "level_1": "Plantea de forma directa que quiere cambiar de plan pero desconoce las implicaciones operativas.",
                "level_2": "Duda entre dos modalidades y pide orientación concreta sobre cuál le conviene más.",
                "level_3": "Expresa temor a que el cambio suponga un periodo sin servicio o costes ocultos.",
                "level_4": "Pregunta cuándo entrarían en vigor las nuevas condiciones y si puede revertir la decisión.",
                "level_5": "Si el agente explica el proceso de forma transparente y sin tecnicismos, el cliente aprueba la modificación."
            },
            {
                "title": f"Simulación {prompt_number}: Reclamación Formal con Petición de Escalado a Responsable",
                "scenario_type": "roleplay",
                "focus": ["Técnicas de desescalada", "Contención emocional", "Protocolo de escalado"],
                "objective_summary": "Desescalar una llamada de alta tensión en la que el cliente exige hablar de inmediato con un supervisor.",
                "expected_behavior": "Mantener la calma y un tono firme y empático; solicitar la oportunidad de resolver la incidencia directamente antes de escalar.",
                "why_calling": "Ha tenido varios contactos previos sin resolución y afirma estar cansado de repetir su caso a distintos operadores.",
                "level_1": "Saluda exigiendo directamente: 'Póngame con un supervisor o con un responsable de departamento'.",
                "level_2": "Rechaza la primera explicación del agente: 'Usted no tiene potestad para resolver esto'.",
                "level_3": "Advierte de que presentará reclamación formal si no le transfieren de inmediato.",
                "level_4": "Baja la guardia ligeramente si el agente asume el liderazgo del caso y demuestra que conoce el expediente.",
                "level_5": "Si el agente ofrece una solución directa con seguimiento personal, el cliente desiste del escalado y acepta."
            },
            {
                "title": f"Simulación {prompt_number}: Gestión de Baja con Cliente Indeciso y Propuesta de Retención",
                "scenario_type": "roleplay",
                "focus": ["Detección del motivo real", "Propuesta de valor alternativa", "Cierre de retención"],
                "objective_summary": "Explorar la causa subyacente de una solicitud de baja y ofrecer alternativas personalizadas sin entrar en descuentos forzados.",
                "expected_behavior": "Preguntar con respeto el motivo real de la baja, separar problemas de servicio de factores de precio y presentar una alternativa.",
                "why_calling": "Llama para solicitar la cancelación del servicio, aunque insinúa que consideraría quedarse si el servicio mejora.",
                "level_1": "Comunica directamente que quiere cursar la baja del servicio, sin dar explicaciones iniciales.",
                "level_2": "Cuando el agente pregunta, menciona el precio como motivo pero de forma vaga.",
                "level_3": "Al explorar más, admite que hay un problema de servicio y soporte que nunca se gestionó bien.",
                "level_4": "Pregunta qué puede ofrecer la empresa para que reconsidere la cancelación.",
                "level_5": "Si el agente detecta el problema real y propone una solución concreta, el cliente acepta aplazar la baja para ver resultados."
            },
            {
                "title": f"Simulación {prompt_number}: Consulta de Información de Servicios y Funcionalidades",
                "scenario_type": "roleplay",
                "focus": ["Claridad de comunicación", "Sondeo de necesidades", "Estructura de llamada"],
                "objective_summary": "Explicar las características de un nuevo servicio de forma estructurada, verificando la comprensión del cliente en cada paso.",
                "expected_behavior": "Organizar la respuesta en partes lógicas, evitar tecnicismos innecesarios y verificar si el cliente tiene dudas.",
                "why_calling": "Ha visto una comunicación sobre nuevos módulos de servicio y quiere saber si le aplican a su contrato actual.",
                "level_1": "Pregunta con curiosidad pero de forma dispersa sobre los nuevos servicios disponibles.",
                "level_2": "Pide aclaraciones sobre si el cambio de servicio requerirá formación adicional para su equipo.",
                "level_3": "Duda de la compatibilidad con sus herramientas actuales de gestión.",
                "level_4": "Pregunta por los tiempos habituales de implantación y puesta en marcha.",
                "level_5": "Si el agente estructura la información con claridad y aporta ejemplos prácticos, el cliente solicita propuesta formal."
            },
            {
                "title": f"Simulación {prompt_number}: Aclaración de Condiciones y Expectativas no Alineadas",
                "scenario_type": "roleplay",
                "focus": ["Gestión de expectativas", "Transparencia comercial", "Empatía"],
                "objective_summary": "Alinear expectativas con un cliente que esperaba coberturas o plazos distintos a los formalizados en su acuerdo.",
                "expected_behavior": "Escuchar sin confrontar, aportar los datos de forma objetiva y ofrecer un camino de resolución viable.",
                "why_calling": "Cree que le informaron de plazos de respuesta más cortos que los que realmente ofrece el servicio contratado.",
                "level_1": "Plantea su discrepancia sobre el nivel de soporte recibido respecto a lo que esperaba.",
                "level_2": "Argumenta que en la llamada de contratación le prometieron una atención prioritaria inmediata.",
                "level_3": "Muestra frustración si el agente se limita a leer cláusulas contractuales frías.",
                "level_4": "Pregunta qué margen de mejora tiene su servicio para alcanzar las expectativas que tenía.",
                "level_5": "Si el agente demuestra empatía, contextualiza el acuerdo y propone mejoras operativas, el cliente valida el compromiso."
            },
        ]
    else:
        scenarios = [
            {
                "title": f"Simulación {prompt_number}: Captación Inicial y Apertura Comercial ante Cliente Ocupado",
                "scenario_type": "roleplay",
                "focus": ["Apertura comercial", "Generación de rapport", "Pregunta de impacto inicial"],
                "objective_summary": "Captar la atención de un lead ocupado en los primeros 45 segundos conectando con su dolor de negocio.",
                "expected_behavior": "Presentarse con claridad, conectar con un reto habitual de su sector y conseguir acuerdo para una conversación de 5 minutos.",
                "why_calling": "Llamada de contacto comercial tras descarga de material o formulario web de interés.",
                "level_1": "Atiende con tono cortante: 'Dígame rápido de qué se trata, estoy a punto de entrar en una reunión'.",
                "level_2": "Muestra escepticismo inicial: 'Ya tenemos varios proveedores para esto y no tenemos intención de cambiar'.",
                "level_3": "Plantea una objeción de tiempo: 'Mándeme un correo con la información y si me interesa ya le diré algo'.",
                "level_4": "Muestra interés si el agente formula una pregunta de impacto sobre un coste oculto en su sector.",
                "level_5": "Si el agente propone una llamada breve de 5 minutos centrada en ese punto de dolor, acepta agendar fecha."
            },
            {
                "title": f"Simulación {prompt_number}: Sondeo Consultivo y Detección de Necesidades Ocultas",
                "scenario_type": "roleplay",
                "focus": ["Preguntas abiertas de sondeo", "Escucha activa", "Detección de dolor"],
                "objective_summary": "Profundizar en la operativa del cliente mediante preguntas abiertas antes de formular cualquier propuesta de solución.",
                "expected_behavior": "Realizar un mínimo de tres preguntas abiertas de impacto, dejar hablar al cliente y resumir sus necesidades antes de avanzar.",
                "why_calling": "El cliente muestra interés genérico pero no tiene claro qué solución concreta necesita.",
                "level_1": "Respuestas cortas y poco descriptivas sobre el funcionamiento actual de su negocio.",
                "level_2": "Admite que tiene ineficiencias en sus procesos pero cree que 'son normales en este sector'.",
                "level_3": "Si el agente profundiza con preguntas abiertas, empieza a detallar los cuellos de botella que sufre.",
                "level_4": "Pregunta cómo han resuelto otros clientes similares ese mismo problema operativo.",
                "level_5": "Si el agente sintetiza con acierto los dolores detectados y los vincula a la propuesta, el cliente avanza con interés."
            },
            {
                "title": f"Simulación {prompt_number}: Objeción Frontal de Precio y Defensa de Valor frente a Competidor",
                "scenario_type": "roleplay",
                "focus": ["Defensa de margen", "Argumentación de valor", "Técnica de doble alternativa"],
                "objective_summary": "Defender el precio de la propuesta frente a una alternativa más económica de un competidor sin recurrir a descuentos.",
                "expected_behavior": "Validar la preocupación de coste, reconducir hacia el retorno de inversión y los costes ocultos de opciones más baratas.",
                "why_calling": "El cliente ha recibido una cotización de la competencia un 25% más baja y exige igualar el precio para continuar.",
                "level_1": "Menciona directamente que tiene una alternativa más barata y pregunta qué descuento le pueden aplicar.",
                "level_2": "Al escuchar el argumento de valor, objeta: 'Al final todos ofrecen lo mismo y la diferencia económica es notable'.",
                "level_3": "Objeción dura: 'No tengo presupuesto para pagar más por un servicio que no me garantiza un mejor resultado'.",
                "level_4": "Escepticismo: '¿Qué garantías medibles me dan de que la implantación merecerá la diferencia de precio?'.",
                "level_5": "Si el agente conecta el valor del servicio con el ahorro operativo real del cliente, este acepta avanzar sin descuento."
            },
            {
                "title": f"Simulación {prompt_number}: Comparativa Competitiva y Argumentación Diferencial",
                "scenario_type": "roleplay",
                "focus": ["Posicionamiento diferencial", "Ética comercial", "Propuesta de valor"],
                "objective_summary": "Destacar los factores diferenciales del servicio frente a alternativas del mercado sin desacreditar al competidor.",
                "expected_behavior": "Centrar la argumentación en fortalezas comprobadas propias (soporte local, integración, fiabilidad) con ejemplos reales.",
                "why_calling": "El cliente está dudando entre dos proveedores de características similares y pide motivos concretos para decidir.",
                "level_1": "Pregunta de forma directa: '¿Por qué debería contratar con ustedes si la empresa X me ofrece un producto muy similar?'.",
                "level_2": "Compara cláusula por cláusula y pide justificación de por qué su SLA tiene mejores condiciones.",
                "level_3": "Objeta si percibe que el agente habla mal de la competencia en lugar de defender sus propias fortalezas.",
                "level_4": "Pide referencias de empresas de su mismo volumen que hayan migrado desde el competidor.",
                "level_5": "Si el agente aporta dos diferenciadores claros y casos de éxito reales con elegancia, el cliente se decanta por la propuesta."
            },
            {
                "title": f"Simulación {prompt_number}: Desmontaje de la Objeción de Postergación 'Tengo que Pensarlo'",
                "scenario_type": "roleplay",
                "focus": ["Detección de dudas ocultas", "Exploración asertiva", "Avance de compromiso"],
                "objective_summary": "Explorar la duda concreta que paraliza la decisión del cliente sin presionar agresivamente pero sin aceptar un aplazamiento pasivo.",
                "expected_behavior": "Preguntar qué elemento concreto de la propuesta genera incertidumbre y ofrecer resolver esa duda en la misma llamada.",
                "why_calling": "El cliente pospone la firma diciendo que 'lo valorará el mes que viene'.",
                "level_1": "Se muestra de acuerdo con la propuesta en general pero dice que no puede decidir hoy.",
                "level_2": "Menciona que tiene que consultarlo internamente con otros responsables antes de comprometerse.",
                "level_3": "Resistencia: 'No quiero tomar una decisión precipitada, si me interesa ya volveré a llamar yo'.",
                "level_4": "Pregunta si las condiciones y tarifas actuales se mantendrían si decide retrasar el inicio al siguiente trimestre.",
                "level_5": "Si el agente explora con respeto la duda de fondo y propone un siguiente contacto concreto, el cliente confirma fecha."
            },
            {
                "title": f"Simulación {prompt_number}: Cliente Analítico que Exige Datos de ROI y Métricas Concretas",
                "scenario_type": "roleplay",
                "focus": ["Rigor argumental", "Gestión de expectativas", "Credibilidad profesional"],
                "objective_summary": "Responder con solvencia y datos verificables a un decisor que exige métricas de impacto antes de firmar.",
                "expected_behavior": "Aportar referencias de clientes de tipología similar, explicar el proceso de implantación y definir expectativas realistas.",
                "why_calling": "Decisor financiero que exige un modelo de retorno medible y plazos de amortización claros antes de validar el gasto.",
                "level_1": "Pregunta con tono analítico: '¿Qué porcentaje medio de mejora operativa han registrado en clientes de mi tamaño?'.",
                "level_2": "Insiste en que si no hay datos contrastados y metodología clara no presentará la propuesta al comité de compras.",
                "level_3": "Pregunta por los costes imprevistos que puedan surgir durante la fase de despliegue.",
                "level_4": "Evalúa el nivel de preparación técnica del agente formulando preguntas precisas sobre plazos.",
                "level_5": "Si el agente responde con rigor técnico, honestidad en plazos y datos concretos, el cliente agenda sesión de formalización."
            },
            {
                "title": f"Simulación {prompt_number}: Negociación y Cierre Estructurado con Técnica de Doble Alternativa",
                "scenario_type": "roleplay",
                "focus": ["Técnica de doble alternativa", "Conducción al cierre", "Compromiso con fecha"],
                "objective_summary": "Guiar a un cliente favorable a la propuesta a dar el paso de formalización mediante opciones concretas de inicio.",
                "expected_behavior": "Plantear una doble alternativa de fecha o modalidad ('¿Iniciamos la incorporación el lunes próximo o el día 1?') y cerrar los datos.",
                "why_calling": "Cliente convencido del valor de la propuesta que muestra cierta inercia para firmar el acuerdo.",
                "level_1": "Amable y conversador, muestra satisfacción con la propuesta pero evita cualquier pregunta que implique compromiso inmediato.",
                "level_2": "Pone excusas de agenda: 'Esta semana ando muy saturado de trabajo, la semana siguiente lo vemos con más calma'.",
                "level_3": "Dice: 'Mándeme el borrador de contrato por correo y ya le responderé cuando tenga un hueco para revisarlo'.",
                "level_4": "Pregunta si puede aplazar el inicio del servicio manteniendo las condiciones comerciales actuales.",
                "level_5": "Si el agente utiliza la técnica de doble alternativa con naturalidad, el cliente elige una fecha concreta y confirma."
            },
            {
                "title": f"Simulación {prompt_number}: Seguimiento Comercial de Propuesta y Formalización de Compromiso",
                "scenario_type": "roleplay",
                "focus": ["Seguimiento proactivo", "Reactivación de interés", "Compromiso de agenda"],
                "objective_summary": "Retomar contacto con una cuenta que no respondió a una propuesta anterior y reactivar la conversación hacia el cierre.",
                "expected_behavior": "Aportar una novedad o contexto relevante para justificar el contacto, revisar si las prioridades han variado y acordar siguiente paso.",
                "why_calling": "Llamada de seguimiento a un lead con propuesta comercial enviada hace diez días sin respuesta.",
                "level_1": "Saluda disculpándose: 'Perdone que no le haya contestado antes, se me acumuló el trabajo'.",
                "level_2": "Reconoce que revisó la propuesta pero que le surgieron dudas con un apartado de la integración.",
                "level_3": "Teme que la implantación consuma demasiado tiempo de su equipo en este momento del año.",
                "level_4": "Pregunta si el plan de incorporación incluye soporte dedicado durante las primeras semanas.",
                "level_5": "Si el agente resuelve las dudas de integración y detalla el acompañamiento inicial, el cliente reactiva el acuerdo."
            },
        ]

    scenario = scenarios[(prompt_number - 1 + persona_index) % len(scenarios)]

    prompt_text = f"""# SIMULACIÓN DE ENTRENAMIENTO — ROLEPLAY VOCAL DE CONTACT CENTER

## 1. IDENTIDAD Y ROL DEL BOT
- Eres un cliente interactivo en una simulación de entrenamiento por voz para agentes del servicio **{service_name}**.
- Tu objetivo es interpretar de forma ultra-realista al personaje asignado a continuación.
- **REGLAS DE ORO:**
  1. NO salgas jamás de tu personaje bajo ninguna circunstancia.
  2. NO des feedback al agente durante la llamada.
  3. NO digas que eres una inteligencia artificial, un asistente virtual ni un bot de voz.
  4. Responde como una persona normal en una conversación telefónica de negocio.

## 2. PERSONAJE Y ANTECEDENTES
- **Nombre:** {persona['name']}
- **Edad:** {persona['age']} años
- **Ocupación:** {persona['occupation']}
- **Estado emocional:** {persona['emotional_state']}
- **Historia previa:** {persona['history']}
- **Estilo de comunicación:** {persona['communication_style']}

## 3. REGLAS DE VOZ TELEFÓNICA Y NATURALIDAD
- **Respuestas breves:** Habla en turnos de 1 a 2 frases como máximo.
- **Lenguaje oral realista:** Usa expresiones coloquiales y pausas naturales.
- **No sueltes toda la información de golpe:** El agente debe ganarse tu confianza.

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

## 6. DISPARADORES DE PROGRESIÓN
- **Si el agente** interrumpe, presiona agresivamente o ignora tus emociones → **Aumenta tu resistencia**.
- **Si el agente** escucha, responde con claridad y ofrece alternativas concretas → **Desescala tu resistencia**.

## 7. CRITERIOS OBSERVABLES PARA EVALUACIÓN POSTERIOR
- Escucha activa y ausencia de interrupciones tempranas.
- Empatía y validación de la situación del cliente.
- Claridad y estructura en la propuesta o solución presentada.
- Manejo asertivo de la objeción sin confrontación ni presión excesiva.
- Cierre estructurado con confirmación del siguiente paso concreto."""

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
# Synthetic Transcription & Evaluation Generator
# Generic contact center transcriptions — no medical references
# ─────────────────────────────────────────────────────────────────────────────
def generate_simulation_evaluation_data(
    prompt_title: str,
    prompt_number: int,
    agent_name: str,
    service_key: str,
    agent_score_tier: str = "solid",
    agent_id: str = "",
) -> Dict[str, Any]:
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
            ("Agente", f"Buenos días, le atiende {agent_name}. ¿En qué puedo ayudarle hoy?"),
            ("Cliente", "Buenos días. Mire, llamo porque tengo una incidencia que lleva días sin resolverse y la verdad es que ya estoy bastante cansado de dar vueltas."),
            ("Agente", "Entiendo perfectamente su situación y le pido disculpas por las molestias. Cuénteme con detalle qué ha ocurrido para que pueda ayudarle de la mejor forma posible."),
            ("Cliente", "Pues que hice una solicitud la semana pasada, me dijeron que en 48 horas lo tendrían resuelto y aquí seguimos."),
            ("Agente", "Tiene toda la razón. Deje que localizo su expediente ahora mismo para ver exactamente en qué punto está la gestión. ¿Me puede confirmar el número de referencia?"),
            ("Cliente", "Sí, el número es el 2847-B."),
            ("Agente", "Perfecto, lo tengo. Veo que la solicitud está en proceso pero no se ha completado la última fase de verificación. Voy a escalarla ahora mismo con prioridad y me comprometo a que tiene una respuesta definitiva antes de las 17:00 de hoy."),
            ("Cliente", "Eso ya me lo dijeron la semana pasada."),
            ("Agente", "Lo entiendo y comprendo su escepticismo, es completamente lógico. Por eso le propongo que le confirme por escrito el compromiso con nombre y extensión de quien se lo está diciendo, para que tenga constancia."),
            ("Cliente", "Sí, con eso me quedaría más tranquilo. Muchas gracias por la atención."),
            ("Agente", "Gracias a usted por su paciencia. Queda registrado. Le llamamos antes de las 17:00. Que tenga un buen día.")
        ]
        feedback = (
            f"Excelente desempeño de {agent_name} en esta simulación. Demuestra dominio de la escucha activa, "
            "valida la frustración del cliente desde el primer momento sin ponerse a la defensiva y propone "
            "una solución concreta con plazo y trazabilidad. El cierre con compromiso escrito es especialmente destacable."
        )
        criteria_dict = {
            "resolucion_primera_llamada": True, "escucha_activa": True, "empatia": True,
            "claridad_comunicacion": True, "gestion_emocional": True, "cierre_estructurado": True,
            "compromiso_documentado": True, "protocolo_bienvenida": True, "siguientes_pasos_claros": True
        }
        strengths = [
            "Validación emocional inmediata sin defensividad ante la queja del cliente.",
            "Propuesta de solución concreta con plazo y trazabilidad escrita.",
            "Cierre estructurado que genera confianza y compromiso mutuo."
        ]
        weaknesses = [
            "Mantener este nivel de detalle en la documentación durante picos de alto volumen."
        ]
    elif agent_score_tier == "solid":
        if agent_id:
            score = Decimal(str(round(7.65 + (((agent_num * 13 + prompt_number * 5) % 15) * 0.06), 2)))
        else:
            score = Decimal(str(round(random.uniform(7.6, 8.4), 2)))
        transcription_turns = [
            ("Agente", f"Buenas tardes, soy {agent_name}. ¿Con quién hablo?"),
            ("Cliente", "Hola, me llamo Elena. Llamo porque necesito información sobre cómo funciona el proceso de renovación del servicio."),
            ("Agente", "Buenas tardes, Elena. Por supuesto, le explico. La renovación se puede gestionar de tres formas distintas según su preferencia..."),
            ("Cliente", "Perdón que le interrumpa, pero antes me gustaría saber si las condiciones cambian con respecto a las actuales."),
            ("Agente", "Entendido. Las condiciones económicas se mantienen si la renovación se formaliza dentro del periodo de vigencia actual."),
            ("Cliente", "¿Y qué pasa si me interesa cambiar a una modalidad diferente en la renovación?"),
            ("Agente", "Eso se puede gestionar sin problema. Hay que solicitarlo con un mínimo de 10 días antes del vencimiento. ¿Le interesa que le explique las modalidades disponibles?"),
            ("Cliente", "Sí, si es posible."),
            ("Agente", "Claro. Le envío también la información por escrito para que pueda revisarla con calma. ¿Me confirma su correo?"),
            ("Cliente", "Sí, cuando tenga un bolígrafo a mano."),
            ("Agente", "Perfecto. ¿Tiene alguna duda más antes de cerrar la llamada?"),
            ("Cliente", "De momento no, muchas gracias.")
        ]
        feedback = (
            f"Buen desempeño general de {agent_name}. El agente responde a las preguntas con claridad y mantiene "
            "un tono profesional durante toda la interacción. Como punto de mejora, conviene realizar una pregunta "
            "de sondeo inicial antes de entrar en la explicación del proceso."
        )
        criteria_dict = {
            "resolucion_primera_llamada": True, "escucha_activa": True, "empatia": True,
            "claridad_comunicacion": True, "gestion_emocional": True, "cierre_estructurado": True,
            "compromiso_documentado": True, "protocolo_bienvenida": True, "siguientes_pasos_claros": False
        }
        strengths = [
            "Respuestas claras y bien estructuradas ante las preguntas del cliente.",
            "Tono profesional y orientado al cliente durante toda la interacción."
        ]
        weaknesses = [
            "Realizar una pregunta de sondeo inicial antes de entrar en la explicación.",
            "Confirmar los siguientes pasos de forma más explícita antes del cierre."
        ]
    else:
        if agent_id:
            score = Decimal(str(round(6.75 + (((agent_num * 7 + prompt_number * 3) % 9) * 0.08), 2)))
        else:
            score = Decimal(str(round(random.uniform(6.5, 7.3), 2)))
        transcription_turns = [
            ("Agente", f"Buenas, {agent_name} al habla, dígame."),
            ("Cliente", "Hola, llamo porque tengo un problema con mi cuenta y no sé muy bien a quién llamar."),
            ("Agente", "Dígame qué le pasa."),
            ("Cliente", "Pues que hace tres días hice un cambio y todavía no se ha aplicado."),
            ("Agente", "¿Cuál es su número de cliente?"),
            ("Cliente", "Es el 44821."),
            ("Agente", "Sí, lo veo. Hay un retraso en el sistema, se está resolviendo."),
            ("Cliente", "¿Y cuándo se resolverá exactamente?"),
            ("Agente", "En unos días, más o menos."),
            ("Cliente", "¿Puede darme una fecha más concreta?"),
            ("Agente", "Le puedo decir que máximo esta semana. Si no se resuelve, vuelva a llamar."),
            ("Cliente", "Bueno, de acuerdo. Gracias.")
        ]
        feedback = (
            f"Simulación completada con margen de mejora para {agent_name}. Aunque el agente localiza la información, "
            "la interacción es demasiado telegráfica: falta empatía en la bienvenida, la estimación de plazo es imprecisa "
            "y el cierre no incluye confirmación de los siguientes pasos."
        )
        criteria_dict = {
            "resolucion_primera_llamada": True, "escucha_activa": True, "empatia": False,
            "claridad_comunicacion": True, "gestion_emocional": True, "cierre_estructurado": False,
            "compromiso_documentado": False, "protocolo_bienvenida": False, "siguientes_pasos_claros": False
        }
        strengths = [
            "Localización rápida del expediente y resolución de la consulta básica.",
            "Mantiene la calma ante la insistencia del cliente en obtener una fecha concreta."
        ]
        weaknesses = [
            "Falta de calidez y empatía en la bienvenida y durante toda la interacción.",
            "La estimación de plazo debe ser concreta; evitar respuestas vagas del tipo 'unos días'.",
            "Cerrar la llamada confirmando el siguiente paso concreto y los datos de contacto."
        ]

    transcription_lines = [f"{role}: {text}" for role, text in transcription_turns]
    transcription_text = "\n".join(transcription_lines)
    result_json = {
        "score": float(score), "feedback": feedback, "result_json": criteria_dict,
        "objectives_met": strengths, "areas_for_improvement": weaknesses, "is_valid_roleplay": True
    }
    return {
        "score": score, "feedback": feedback, "transcription": transcription_text,
        "result_json": result_json, "strengths": strengths, "weaknesses": weaknesses,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Full Cycle Enhancer Data Generator
# ─────────────────────────────────────────────────────────────────────────────
def generate_enhanced_training_cycle_data(
    agent_id: str,
    agent_name: str,
    agent_initials: str,
    service_key: str,
    status: str,
    cycle_index: int = 1
) -> Dict[str, Any]:
    """
    Produces the complete, rich data payload for a TrainingAgentReport and its simulations.
    All content is generic contact center B2B — no medical/clinical references.
    """
    agent_num = 1
    try:
        parts = agent_id.split("_")
        agent_num = int(parts[-1])
    except Exception:
        agent_num = abs(int(hashlib.md5(agent_id.encode()).hexdigest(), 16)) % 60 + 1

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

    cat_key = "ventas" if "venta" in service_key.lower() or "comercial" in service_key.lower() else "atencion-al-cliente"
    catalog = OBJECTIVES_CATALOG[cat_key]

    gen_list = catalog["general"]
    gen_count = 4 if (agent_num % 2 == 0) else 3
    gen_objs_raw = [gen_list[(i + agent_num) % len(gen_list)] for i in range(gen_count)]
    general_objectives = [{
        "title": g["title"], "description": g["description"],
        "rationale": g["rationale"], "expected_behavior": g["expected_behavior"],
        "success_indicators": g["success_indicators"]
    } for g in gen_objs_raw]

    spec_list = catalog["specific"]
    spec_count = 4 if ((agent_num + 1) % 2 == 0) else 3
    spec_objs_raw = [spec_list[(i + agent_num) % len(spec_list)] for i in range(spec_count)]
    specific_objectives = [{
        "title": s["title"], "description": s["description"],
        "related_criteria": s["related_criteria"],
        "specific_behavior_to_improve": s["specific_behavior_to_improve"],
        "success_indicators": s["success_indicators"]
    } for s in spec_objs_raw]

    sw_pool_tier = _SW_POOL[cat_key][tier]
    str_pool = sw_pool_tier["strengths"]
    wk_pool = sw_pool_tier["weaknesses"]
    str_count = 3 if tier == "top" else (3 if tier == "solid" else 2)
    wk_count = 2 if tier == "top" else (2 if tier == "solid" else 3)
    strengths_list = [str_pool[(agent_num + i) % len(str_pool)] for i in range(str_count)]
    weaknesses_list = [wk_pool[(agent_num + i) % len(wk_pool)] for i in range(wk_count)]

    notable_data_templates = {
        "top": [
            {"title": "Alto Rendimiento Consistente", "description": "El agente mantiene una puntuación media por encima del objetivo del equipo durante todo el ciclo.", "metric_or_pattern": f"Nota media del ciclo: {avg_score}/10"},
            {"title": "Referente en Resolución en Primera Llamada", "description": "La tasa de resolución en primera llamada del agente está entre las más altas del equipo.", "metric_or_pattern": "FCR del 87%, 12 puntos por encima del objetivo."},
        ],
        "solid": [
            {"title": "Evolución Positiva en el Ciclo", "description": "El agente ha mejorado de forma constante durante el ciclo en las áreas de trabajo definidas.", "metric_or_pattern": "Incremento de nota de +0.8 puntos respecto al inicio del ciclo."},
            {"title": "Regularidad en el Protocolo", "description": "El agente aplica el protocolo de atención con consistencia creciente.", "metric_or_pattern": "Cumplimiento de protocolo del 91% en auditorías del periodo."},
        ],
        "developing": [
            {"title": "Foco de Entrenamiento Prioritario", "description": "El agente requiere consolidar las habilidades base de atención y el protocolo de cierre.", "metric_or_pattern": f"Nota actual: {avg_score}/10. Objetivo del próximo ciclo: superar 7.5."},
            {"title": "Progreso Observado en Calibraciones", "description": "Las sesiones de calibración del ciclo han producido mejoras visibles en aspectos concretos.", "metric_or_pattern": "Mejora del 30% en el criterio de escucha activa respecto al inicio del ciclo."},
        ],
    }
    notable_pool = notable_data_templates[tier]
    notable_data = [notable_pool[agent_num % len(notable_pool)]]

    service_label = "atención al cliente" if cat_key == "atencion-al-cliente" else "ventas"
    tier_summary = {
        "top": "destacado rendimiento y una aplicación consistente de los protocolos de calidad",
        "solid": "un rendimiento sólido con margen de mejora en áreas específicas del ciclo",
        "developing": "un proceso de desarrollo activo con evolución positiva en las áreas de trabajo identificadas",
    }

    if status == "completed":
        summary_general = (
            f"Informe de consolidación del ciclo de formación para {agent_name} en el servicio de {service_label}. "
            f"El agente ha completado el itinerario formativo con una nota media de {avg_score}/10, mostrando "
            f"{tier_summary[tier]}. "
            "Se han alcanzado los objetivos principales del ciclo y se establecen las pautas para el siguiente periodo de mejora."
        )
    else:
        summary_general = (
            f"Ciclo de mejora en curso para {agent_name} en el servicio de {service_label}. "
            f"El objetivo principal de este ciclo es consolidar las habilidades de {service_label} mediante "
            "simulaciones prácticas con escenarios de dificultad progresiva y sesiones de calibración personalizadas. "
            f"Nota media actual: {avg_score}/10."
        )

    evolution_summary = (
        f"Evolución de {agent_name} durante el ciclo: desde el inicio se observa una mejora progresiva en la "
        "consistencia de aplicación del protocolo de atención, pasando de un enfoque más reactivo a una gestión "
        f"más estructurada y orientada al cliente en el servicio de {service_label}."
    )

    final_report = None
    if status == "completed":
        objs_status = []
        for i, obj in enumerate(general_objectives):
            base_s = round(float(avg_score) - random.uniform(0.8, 1.6), 2)
            final_s = round(float(avg_score) + random.uniform(-0.2, 0.5), 2)
            is_sup = final_s >= 7.5
            obj_status = "SUPERADO" if is_sup else "NO SUPERADO"
            objs_status.append({
                "title": obj["title"],
                "status": obj_status,
                "base_score": base_s,
                "score": final_s,
                "improvement_delta": round(final_s - base_s, 2),
                "justification": _generate_justification(
                    obj_title=obj["title"], status=obj_status, tier=tier,
                    agent_num=agent_num, score=final_s, base_score=base_s, service_key=cat_key
                ),
            })
        for i, obj in enumerate(specific_objectives):
            base_s = round(float(avg_score) - random.uniform(0.6, 1.3), 2)
            final_s = round(float(avg_score) + random.uniform(-0.1, 0.6), 2)
            is_sup = final_s >= 7.5
            obj_status = "SUPERADO" if is_sup else "NO SUPERADO"
            objs_status.append({
                "title": obj["title"],
                "status": obj_status,
                "base_score": base_s,
                "score": final_s,
                "improvement_delta": round(final_s - base_s, 2),
                "justification": _generate_justification(
                    obj_title=obj["title"], status=obj_status, tier=tier,
                    agent_num=agent_num, score=final_s, base_score=base_s, service_key=cat_key
                ),
            })
        final_report = {
            "summary_final": summary_general,
            "evolution_assessment": evolution_summary,
            "next_steps": (
                f"Mantener la frecuencia de calibración quincenal y avanzar hacia los objetivos del siguiente "
                f"bloque de formación en {service_label}, con foco en los aspectos de mayor impacto en la "
                "satisfacción del cliente."
            ),
            "objectives_status": objs_status
        }

    if status == "completed":
        prompts_count = 4 if agent_num % 4 == 0 else (3 if agent_num % 2 == 0 else 2)
    else:
        prompts_count = 3

    prompts = []
    for p_num in range(1, prompts_count + 1):
        prompt_data = build_roleplay_simulation_prompt(
            prompt_number=p_num, agent_name=agent_name, service_key=cat_key,
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
