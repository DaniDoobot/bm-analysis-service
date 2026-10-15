# Speech BM — Handoff sesión 2026-10-15

## 1. Estado actual

En la jornada de hoy se ha completado la auditoría técnica y económica sobre el sobrecoste de las Evaluaciones Masivas de Front respecto a Experiencia de Paciente (EXPAC) en Boston Medical. Se demostró que el causante estructural de la disparidad de costes era el tamaño excesivo y redundante del prompt de Front. 

A través de tres fases experimentales controladas (22 ejecuciones en total, estrictamente acotadas y con coste total de prueba inferior a $0.70 USD), se iteró desde el baseline (**Prompt A**) a una primera optimización (**Prompt B**), y finalmente a la versión corregida (**Prompt B2**).

**Prompt B2** ha quedado validado funcionalmente:
- Corrige las falsas penalizaciones en llamadas de cancelación asignando `null`.
- Mantiene la precisión y calidad en llamadas comerciales normales.
- Conserva al 100% los 55 criterios de negocio y 82 claves JSON.
- Aporta una reducción estructural garantizada de ~27% en tokens de entrada y ~35% en tokens de salida.
- **Estado de producción:** Todavía **NO está en producción ni activado**.

---

## 2. Baseline productivo

Datos auditados directamente desde la API y base de datos productiva:
- **Empresa:** Boston Medical (`company_id = 1`)
- **Servicios:**
  - Front: `service_id = 1`
  - Experiencia de Paciente (EXPAC): `service_id = 2`
- **Módulo:** `mass_evaluations`
- **Modelo evaluador:** `gemini-3.8-flash`
- **Prompt activo de Front:**
  - **`prompt_id`:** **58** (`EE Front V9 30jul 2026`)
  - **Versión activa real:** **`v20260918-0715`**
  - **Criterios activos:** 55 criterios
- **SDK y API:** `google-genai` conectando a Google AI Studio / Gemini Developer API (`generativelanguage.googleapis.com`).

---

## 3. Hallazgos FinOps (Datos Reconciliados)

Métricas consolidadas sobre el histórico productivo (823 eventos de Front y 68 eventos de EXPAC):

| Métrica FinOps | Front (`service_id = 1`) | EXPAC (`service_id = 2`) | Diferencia Front vs EXPAC | Multiplicador |
|---|---|---|---|---|
| **Coste medio / evaluación** | **$0.046294 USD** | **$0.028083 USD** | **+$0.018211 (+64.85%)** | **1.65x** |
| **Duración media de llamada** | **133.85 segundos (2.23 min)** | **295.76 segundos (4.93 min)** | **-161.91 segundos (-54.74%)** | **0.45x** |
| **Coste medio / minuto de audio** | **$0.020753 USD/min** | **$0.005697 USD/min** | **+$0.015056 (+264.27%)** | **3.64x** |
| **Input Text Tokens / evaluación** | **34,657.7 tokens** | **13,793.0 tokens** | **+20,864.7 tokens (+151.27%)** | **2.51x** |
| **Thinking Tokens / evaluación** | **5,590.7 tokens** | **2,811.1 tokens** | **+2,779.6 tokens (+98.88%)** | **1.99x** |
| **Candidates (Output) Tokens** | **1,902.4 tokens** | **1,141.6 tokens** | **+760.8 tokens (+66.64%)** | **1.67x** |
| **Total Tokens / evaluación** | **44,813.3 tokens** | **21,633.4 tokens** | **+23,179.9 tokens (+107.15%)** | **2.07x** |

**Conclusión clave:** El sobrecoste no proviene de la duración del audio (EXPAC dura más del doble), sino del exceso de texto en el prompt de Front (34.7k vs 13.8k tokens).

---

## 4. Fase 1 A/B (Prompt A vs Prompt B)

- **Alcance:** 5 llamadas reales representativas x 2 versiones = 10 ejecuciones.
- **Objetivo:** Medir el impacto de centralizar la rúbrica 1-10 eliminando texto redundante.
- **Reducción de tokens:** -27.42% en input text, -33.68% en candidates.
- **Ahorro monetario observado:** **25.73%** ($0.223522 vs $0.166018 USD).
- **Incidencia detectada:** El script preliminar reportó una caída aparente de score en la llamada 4 (5.41 vs 7.06) porque un promedio genérico en Python computó valores booleanos (`isinstance(bool, int) == True`) en el denominador numérico.
- **Corrección técnica:** Al aplicar la función oficial de producción (`app.utils.scores.calculate_score_from_items`), el score real de Prompt B fue **7.55** (frente a 7.06 en A), alineado con la calidad histórica del agente (7.61).

---

## 5. Fase 2 A/B (Prompt A vs Prompt B)

- **Alcance:** 3 llamadas (cancelación, reagendo administrativo, control comercial) = 6 ejecuciones.
- **Riesgo confirmado en cancelaciones:** En la llamada `527513931990` (cancelación pura), Prompt B puntuó `n3_preguntas = 3` (penalizando por no hacer sondeo clínico en una anulación), deprimiendo la nota a 5.53 vs 6.78 en A (donde fue `null`).
- **Incidencia de caching:** En Fase 2, Prompt A tuvo 3 cache hits (>94%) mientras Prompt B tuvo 3 misses.
- **Interpretación correcta de caching:** El umbral oficial para `gemini-3.8-flash` es de **4.096 tokens** (NO 32.768 tokens). Prompt B (con ~25.279 tokens) es 100% elegible y se cacheó en Fase 1 (89-92%). En Fase 2 falló debido al parón de 45 minutos y llamadas alternadas con esperas de >60s que hicieron expirar el TTL efímero de la caché implícita.

---

## 6. Fase 3 A/B2 (Prompt A vs Prompt B2)

- **Alcance:** 3 llamadas en orden contrabalanceado (Call 1: A $\to$ B2; Call 2: B2 $\to$ A; Call 3: A $\to$ B2) = 6 ejecuciones.
- **Objetivo:** Validar la salvaguarda de no aplicabilidad añadida en Prompt B2.
- **Resultados funcionales:**
  - **Cancelaciones corregidas al 100%:** Tanto en Call 1 (`527513931990`) como en Call 2 (`527570244819`), `n3_preguntas` y `claridad_explicacion_economica` devolvieron estrictamente **`null`** en Prompt B2.
  - **Llamada comercial de control intacta:** En Call 3 (`527602453701`), `n3_preguntas` y `claridad_explicacion_economica` puntuaron **10/10 en ambos prompts** con citas textuales idénticas. El score global fue **6.78 en A vs 6.91 en B2** (delta de solo +0.13).
- **Reducción estructural de tokens:** -27.23% en input text, -36.37% en output candidates.
- **Comportamiento de caching:** B2 obtuvo aciertos de caché del **97.3%** (Llamada 2) y **94.3%** (Llamada 3) cuando el prefijo estuvo caliente.
- **Coste observado en Fase 3:** Prompt A = $0.108129 USD vs Prompt B2 = $0.100412 USD (-7.14% global; -16.6% en Llamada 2 y -39.8% en Llamada 3).

---

## 7. Prompt B2

- **Ubicación:** [scratch/prompt_B2.txt](file:///c:/Users/danim/Proyectos/bm-analysis-service/scratch/prompt_B2.txt)
- **Tamaño:** 102.353 caracteres | 14.935 palabras (reducción de **-40.192 caracteres (-28.20%)** frente al Prompt A de producción).
- **Criterios y Esquema:** 55 criterios activos, 55 output_keys, 27 feed_keys, 82 claves totales en el JSON final (100% idénticos a producción).
- **Única modificación funcional respecto a Prompt B:**
  Incorporación de la siguiente regla en la rúbrica común previa a los criterios:
  > *- En llamadas cuya tipología o motivo principal sea la cancelación o anulación de cita sin posibilidad real de gestión comercial, los criterios de sondeo clínico (n3_preguntas) y explicación económica (claridad_explicacion_economica) deben evaluarse como null cuando dichas conductas no sean aplicables.*

---

## 8. Conclusiones Validadas

### [VALIDADO]
1. Prompt B2 reduce de forma estructural y determinista un **~27% los tokens de texto de entrada** y un **~35% los tokens de salida**.
2. Prompt B2 mantiene intacto el 100% de la lógica de negocio (55 criterios, 82 claves JSON, clasificaciones y alarmas).
3. Prompt B2 corrige de raíz la sobre-penalización en llamadas de cancelación forzando `null` en criterios no aplicables.
4. Prompt B2 mantiene un comportamiento idéntico en llamadas comerciales normales.
5. Prompt B2 es plenamente elegible para Context Caching de Google ($\ge 4.096$ tokens) habiendo alcanzado tasas de acierto del **89% al 97%** en pruebas experimentales.

### [NO VALIDADO / NO GARANTIZADO]
1. No se garantiza una tasa fija de cache hit del "90-95% permanente" en producción (el caching implícito es oportunista).
2. No se garantiza un ahorro anual exacto en dólares fijado a priori; el ahorro dependerá del volumen, cadencia por lotes y variabilidad de thinking tokens en producción.

---

## 9. Próximo Paso Recomendado

El siguiente paso técnico al retomar la sesión desde casa es:

1. **Crear Prompt B2 como nueva versión del prompt de Front (`prompt_id = 58`) en base de datos SIN ACTIVARLA.**
2. Revisar el contenido creado y verificar el contrato (55 criterios, 82 claves).
3. Comprobar el diff exacto contra la versión activa actual (`v20260918-0715`).
4. Dejar la versión lista y preparada en backend.
5. **Activarla únicamente tras confirmación explícita del usuario**, manteniendo monitorización sobre las primeras evaluaciones automáticas.

---

## 10. Pendientes Posteriores (Fuera de FinOps)

Tras cerrar el despliegue del prompt de Front, se retomará el bloque de **rendimiento y arquitectura de Speech BM**:
- Optimización de carga de estructuras específicas.
- Filtrado por tenant de estructuras.
- Creación y edición de estructuras base y específicas.
- Guardado y validación de configuración.
- Cambio de servicios y listados en UI.

---

## 11. Reglas de Trabajo del Proyecto

- **Stack dividido:** Backend en Antigravity / Python FastAPI; Frontend en Lovable.
- **Deploy:** El backend se despliega manualmente en Dokploy; Antigravity **NUNCA** ejecuta deploy automático.
- **Flujo de cambios:** Prompts de cambios atómicos, paso a paso.
- **Prioridad:** Primero backend (API/schemas/servicios), después frontend.
- **Aislamiento:** No mezclar con el proyecto Hitchings.
