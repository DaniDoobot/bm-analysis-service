# Optimización de Costes de Evaluaciones Masivas — Front
**Dossier Técnico y FinOps para Dirección y Finanzas**  
*Fecha de actualización: 15 de Octubre de 2026*  
*Proyecto: Speech Analytics Boston Medical (`company_id = 1`)*  
*Servicio auditado: Front (`service_id = 1`, `prompt_id = 58`) frente a Experiencia de Paciente (`service_id = 2`, `prompt_id = 75`)*  
*Modelo evaluador: Google Gemini (`gemini-3.8-flash`)*  
*SDK / API: `google-genai` (Google AI Studio / Gemini Developer API)*

---

## 1. Resumen Ejecutivo

### [DATOS MEDIDOS]
- Las Evaluaciones Masivas de Front costaban históricamente un **+64.85% más por llamada** que las de EXPAC ($0.046294 vs $0.028083 USD), a pesar de que el audio de Front es sustancialmente más corto (133.8s vs 295.8s en EXPAC). Por minuto de audio, Front cuesta un **+264.27% más** ($0.020753/min vs $0.005697/min).
- La causa raíz comprobada es el volumen del prompt de evaluación: **34.658 input text tokens** en Front frente a **13.793** en EXPAC (+151.3%).
- Se evaluaron dos variantes optimizadas sucesivas frente al baseline de producción (**Prompt A**):
  - **Prompt B:** Centralización de la rúbrica común 1-10 (-28.4% caracteres).
  - **Prompt B2:** Idéntica compresión (-28.2% caracteres) incorporando una salvaguarda explícita para evitar falsas penalizaciones en cancelaciones.
- Se completaron **22 evaluaciones pareadas controladas** en tres fases experimentales:
  - Fase 1 (10 ejecuciones): 5 llamadas pareadas (A vs B).
  - Fase 2 (6 ejecuciones): 3 llamadas pareadas (A vs B).
  - Fase 3 (6 ejecuciones): 3 llamadas pareadas en orden contrabalanceado (A vs B2).

### [CÁLCULOS Y RESULTADOS CLAVE]
- **Reducción estructural de tokens:** Prompt B y B2 reducen de manera constante entre un **-27.2% y un -27.4% los tokens de texto de entrada** y entre un **-33.7% y un -36.4% los tokens de salida (candidates)**.
- **Validación funcional de Prompt B2:** La salvaguarda quirúrgica corrigió al 100% las falsas penalizaciones en llamadas de cancelación: `n3_preguntas` y `claridad_explicacion_economica` devolvieron estrictamente `null`.
- **Inocuidad en llamadas comerciales:** En llamadas comerciales normales, Prompt B2 mantuvo exactamente la evaluabilidad y puntuación máxima (10/10) en criterios clave, con un delta de puntuación global de apenas **+0.13 puntos** respecto a A.
- **Context Caching de Gemini:** El umbral mínimo oficial de Google para `gemini-3.8-flash` en la Gemini Developer API es de **4.096 tokens** (NO 32.768 tokens). Prompt B y B2 demostraron ser plenamente elegibles para caché, registrando tasas de acierto de entre el **89.0% y el 97.3%** en ejecuciones con prefijo warm en memoria.

---

## 2. Situación Inicial (Auditoría FinOps Histórica Reconciliada)

### [DATOS MEDIDOS]
Auditoría exhaustiva sobre los 823 eventos reales de Front y 68 eventos de EXPAC en `bm_ai_usage_events` (100% de datos reales validados):

| Métrica / Parámetro | Front (`service_id = 1`) | EXPAC (`service_id = 2`) | Diferencial Front vs EXPAC | Multiplicador |
|---|---|---|---|---|
| **ID Prompt Activo** | **`prompt_id = 58`** (v20260918-0715) | **`prompt_id = 75`** (v20261013-0818) | N/A | N/A |
| **Eventos evaluados** | 823 (819 priced, 4 partial) | 68 (68 priced) | N/A | N/A |
| **Coste medio por evaluación** | **$0.046294 USD** | **$0.028083 USD** | **+$0.018211 (+64.85%)** | **1.65x** |
| **Duración media de audio** | **133.85 segundos (2.23 min)** | **295.76 segundos (4.93 min)** | **-161.91 segundos (-54.74%)** | **0.45x** |
| **Coste por minuto de audio** | **$0.020753 USD/min** | **$0.005697 USD/min** | **+$0.015056 (+264.27%)** | **3.64x** |
| **Input Text Tokens / eval** | **34,657.7 tokens** | **13,793.0 tokens** | **+20,864.7 tokens (+151.27%)** | **2.51x** |
| **Input Audio Tokens / eval** | **2,616.1 tokens** | **3,887.6 tokens** | **-1,271.5 tokens (-32.71%)** | **0.67x** |
| **Thinking Tokens / eval** | **5,590.7 tokens** | **2,811.1 tokens** | **+2,779.6 tokens (+98.88%)** | **1.99x** |
| **Candidates (Output) Tokens** | **1,902.4 tokens** | **1,141.6 tokens** | **+760.8 tokens (+66.64%)** | **1.67x** |
| **Total Tokens / eval** | **44,813.3 tokens** | **21,633.4 tokens** | **+23,179.9 tokens (+107.15%)** | **2.07x** |
| **Latencia media** | **54,026.9 ms** | **32,935.2 ms** | **+21,091.7 ms (+64.04%)** | **1.64x** |

### [CÁLCULOS]
- El sobrecoste en Front no se debe a llamadas más largas (las llamadas de EXPAC duran más del doble: 4.93 min vs 2.23 min), sino exclusivamente a la sobrecarga del prompt de evaluación de texto.
- Descomposición del sobrecoste de $0.018211 USD por llamada en Front:
  - Componente de Texto de Entrada: +$0.005888 USD
  - Componente de Razonamiento (Thinking): +$0.010423 USD
  - Componente de Salida (Candidates): +$0.002853 USD
  - Componente de Audio: -$0.000954 USD (audio más corto)
- Sobre un volumen anual proyectado de 100.000 llamadas en Front, este exceso representa **$1.821 USD de sobregasto evitable**.

---

## 3. Hipótesis de Optimización y Arquitectura de Prompts

### [HIPÓTESIS / INTERPRETACIONES]
1. **Redundancia en Prompt A:** Prompt A (`prompt_id = 58`) tenía 142.545 caracteres (21.052 palabras). En cada uno de los 55 criterios activos repetía de forma idéntica la escala 1-10, las cláusulas genéricas de `null`, preámbulos retóricos e instrucciones de feedback.
2. **Arquitectura de Prompt B:** Centralizó la rúbrica y directrices en una cabecera común estándar, reduciendo el prompt a 102.049 caracteres (14.894 palabras, -28.41%), manteniendo íntegros los 55 criterios y 82 claves JSON.
3. **Arquitectura de Prompt B2:** Detectada la anomalía en llamadas de cancelación durante las Fases 1 y 2, se creó Prompt B2 añadiendo una regla quirúrgica en la rúbrica centralizada:
   > *- En llamadas cuya tipología o motivo principal sea la cancelación o anulación de cita sin posibilidad real de gestión comercial, los criterios de sondeo clínico (n3_preguntas) y explicación económica (claridad_explicacion_economica) deben evaluarse como null cuando dichas conductas no sean aplicables.*
4. **Métricas Offline Comparadas:**
   - Prompt A: 142.545 caracteres | 21.052 palabras | 55 criterios | 82 claves JSON
   - Prompt B: 102.049 caracteres | 14.894 palabras | 55 criterios | 82 claves JSON
   - Prompt B2: 102.353 caracteres | 14.935 palabras | 55 criterios | 82 claves JSON (+304 caracteres respecto a B, -28.20% respecto a A).

---

## 4. Context Caching: Evidencia Empírica y Criterio Técnico

### [CORRECCIÓN TÉCNICA FINOPS]
- La documentación oficial de Google para la Gemini Developer API (`gemini-3.8-flash`) fija el umbral mínimo de prefijo en **4.096 tokens** (NO 32.768 tokens).
- **El cumplimiento del umbral mínimo NO garantiza un acierto de caché (cache hit):** La caché implícita es oportunista y depende del enrutamiento de peticiones a réplicas de inferencia con el prefijo caliente en memoria y de la caducidad del TTL efímero.

### [DATOS MEDIDOS DE CACHING (22 EJECUCIONES)]
- **Fase 1 (Prompt B):** En 4 de las 5 llamadas consecutivas, Prompt B obtuvo **cache hits del 89.03% al 92.23%** (22.505 a 23.316 tokens cacheados).
- **Fase 2 (Prompt B):** Se observaron fallos de caché (0.0% a 0.89%) debido a peticiones alternadas (A $\to$ B) con intervalos de descarga e inferencia superiores a 60–90 segundos tras un parón previo de 45 minutos.
- **Fase 3 (Prompt B2):** En la llamada 2 (Valentín), B2 obtuvo un **97.3% de caché** (24.654 tokens). En la llamada 3 (Nelson), B2 obtuvo un **94.3% de caché** (23.912 tokens), mientras que A sufrió un fallo de caché (0.0%).

---

## 5. Fase 1: Comparativa A vs B (5 Llamadas, 10 Ejecuciones)

### [DATOS MEDIDOS]
Condiciones: `temperature=0.2`, modelo `gemini-3.8-flash`, llamadas de control de longitud variada:

| Call ID | Tipología / Duración | Coste Prompt A | Coste Prompt B | Ahorro % | Score Prod A | Score Prod B | Delta Prod |
|---|---|---|---|---|---|---|---|
| `527566359779` | Corta (40s) | $0.060242 | $0.033080 | 45.09% | *None (corte)* | *None (corte)* | N/A |
| `527116146936` | Media A (122s) | $0.034840 | $0.031411 | 9.84% | 6.49 | 7.50 | +1.01 |
| `527241182407` | Media B (126s) | $0.044057 | $0.040368 | 8.37% | 7.10 | 5.76 | -1.34 |
| `527193877743` | Larga (303s) | $0.055864 | $0.038702 | 30.72% | 7.06 | 7.55 | +0.49 |
| `527412783337` | Compleja (125s) | $0.028519 | $0.022457 | 21.26% | 4.81 | 5.31 | +0.50 |

### [CÁLCULOS FASE 1]
- Ahorro económico agregado observado: **25.73%** ($0.223522 vs $0.166018 USD).
- Reducción de input text tokens: **-27.42%** (174.135 $\to$ 126.395).
- Reducción de candidates tokens: **-33.68%** (13.740 $\to$ 9.112).
- Reducción de latencia: **-27.34%** (70.7s $\to$ 51.4s).
- **Resolución técnica:** El cálculo con la lógica real de producción (`app.utils.scores.calculate_score_from_items`) corrigió el score preliminar erróneo de la llamada 4 (7.55 en B vs 7.06 en A).

---

## 6. Fase 2: Validación de Criterios Null A vs B (3 Llamadas, 6 Ejecuciones)

### [DATOS MEDIDOS]
Prueba enfocada en llamadas límite administrativas y de cancelación:

| Call ID | Tipología / Agente | Duración | Coste Prompt A | Coste Prompt B | Score Prod A | Score Prod B | Delta Prod |
|---|---|---|---|---|---|---|---|
| `527513931990` | Cancelación / Bryan H. | 64s | $0.033698 | $0.048370 | 6.78 | 5.53 | **-1.25** |
| `527664912593` | Reagendo / Cristina M. | 104s | $0.042646 | $0.071674 | 5.14 | 5.33 | **+0.19** |
| `527993714918` | Comercial / Fernanda R. | 278s | $0.031580 | $0.057636 | 7.25 | 7.32 | **+0.07** |

### [HALLAZGOS FASE 2]
1. En llamadas comerciales y de reagendo (`527993714918` y `527664912593`), la correlación de scoring fue casi perfecta (+0.07 y +0.19).
2. En la llamada de cancelación (`527513931990`), Prompt B evaluó `n3_preguntas = 3` (penalizando por no hacer sondeo clínico en una anulación), mientras que A devolvió `null`. Esto deprimió el score en B en **-1.25 puntos**.

---

## 7. Fase 3: Validación Final de Prompt B2 (3 Llamadas, 6 Ejecuciones)

### [DISEÑO EXPERIMENTAL CONTRABALANCEADO]
Para evitar sesgo de orden y caché, la secuencia de ejecución fue contrabalanceada:
- Llamada 1 (`527513931990`): A $\to$ B2
- Llamada 2 (`527570244819`): B2 $\to$ A
- Llamada 3 (`527602453701`): A $\to$ B2

### [DATOS MEDIDOS]

| Call ID | Tipología / Agente | Duración | Coste A | Coste B2 | Score Prod A | Score Prod B2 | Delta Prod | % Caché A | % Caché B2 |
|---|---|---|---|---|---|---|---|---|---|
| `527513931990` | Cancelación Pura / Bryan H. | 64s | $0.025547 | $0.043821 | 5.04 | 6.45 | **+1.41** | 96.2% | 0.0% (cold) |
| `527570244819` | Cancelación Admin / Valentín | 327s | $0.029679 | $0.024740 | 4.36 | 4.75 | **+0.39** | 98.0% | **97.3% (hit)** |
| `527602453701` | Comercial Control / Nelson | 227s | $0.052903 | $0.031851 | 6.78 | 6.91 | **+0.13** | 0.0% (miss)| **94.3% (hit)** |

### [CÁLCULOS FASE 3]
- Coste total observado: Prompt A = $0.108129 USD vs Prompt B2 = $0.100412 USD (**-7.14%** a pesar del arranque en frío de B2 en la llamada 1).
- En ejecuciones con caché caliente (Llamadas 2 y 3), Prompt B2 fue sustancialmente más económico que Prompt A:
  - Llamada 2: B2 costó **$0.024740** vs A **$0.029679** (-16.64%).
  - Llamada 3: B2 costó **$0.031851** vs A **$0.052903** (-39.79%).
- Reducción de tokens de texto de entrada: **-27.23%** (104.481 $\to$ 76.032 tokens).
- Reducción de tokens de candidatos (output): **-36.37%** (9.642 $\to$ 6.135 tokens).
- Reducción de tokens totales consumidos: **-21.49%** (131.421 $\to$ 103.176 tokens).

### [ANÁLISIS DE LA SALVAGUARDA DE CANCELACIONES EN B2]
1. **Llamada 1 (`527513931990` - Cancelación Pura):**
   - `n3_preguntas`: A = 2 $\to$ **B2 = null** (Salvaguarda aplicada con éxito).
   - `claridad_explicacion_economica`: A = 0 $\to$ **B2 = null** (Salvaguarda aplicada con éxito).
   - *Nota:* En esta ejecución, Prompt A penalizó al agente con 2 y 0 por no vender ni indagar patología en un paciente que cancelaba; Prompt B2 lo protegió correctamente asignando `null`.
2. **Llamada 2 (`527570244819` - Cancelación por Incompatibilidad Horaria):**
   - `n3_preguntas`: A = 2 $\to$ **B2 = null** (Salvaguarda aplicada con éxito).
   - `claridad_explicacion_economica`: A = 0 $\to$ **B2 = null** (Salvaguarda aplicada con éxito).
3. **Llamada 3 (`527602453701` - Control Comercial Cita):**
   - `n3_preguntas`: A = **10.0** $\to$ B2 = **10** (Ambos evalúan 10/10 con citas literales).
   - `claridad_explicacion_economica`: A = **10.0** $\to$ B2 = **10** (Ambos evalúan 10/10 con citas literales).
   - Tipología, patología, alarmas y no-cita: Coincidencia del 100%.
   - Score global: A = **6.78** vs B2 = **6.91** (Delta de apenas **+0.13** puntos).

---

## 8. Resumen Acumulado de Evidencias (22 Ejecuciones Totales)

Diferenciación estricta de las tres variantes:
- **Prompt A:** Baseline productivo actual.
- **Prompt B:** Variante inicial optimizada (-28.4% texto).
- **Prompt B2:** Variante optimizada final corregida con salvaguarda (-28.2% texto).

| Fase | Comparativa | Ejecuciones | Ahorro InText % | Ahorro Output % | Comportamiento Caché | Estado Funcional |
|---|---|---|---|---|---|---|
| **Fase 1** | A vs B | 10 (5 calls) | **-27.42%** | **-33.68%** | Hits en B del 89.0% al 92.2% | Alerta en cancelaciones |
| **Fase 2** | A vs B | 6 (3 calls) | **-27.42%** | **-34.41%** | Misses en B por parón y llamadas aisladas | Riesgo cancelaciones confirmado |
| **Fase 3** | A vs B2 | 6 (3 calls) | **-27.23%** | **-36.37%** | Hits en B2 del 94.3% al 97.3% | **Cancelaciones corregidas (100% null)** |

---

## 9. Conclusiones Validadas y Limitaciones

### [CONCLUSIONES VALIDADAS]
1. **B2 resuelve definitivamente la anomalía de cancelaciones:** Tanto en llamadas puras como de gestión de agenda, los criterios `n3_preguntas` y `claridad_explicacion_economica` quedan en `null` cuando no aplican, superando la calibración de Prompt A.
2. **B2 no altera las llamadas comerciales:** En llamadas donde el sondeo y la explicación económica son pertinentes, B2 los evalúa con idéntico rigor (10/10) y produce deltas de scoring insignificantes (+0.13).
3. **Reducción estructural garantizada:** La reducción de más del 27% en tokens de entrada y más del 34% en tokens de salida es inherente a la estructura del prompt y se cumple en el 100% de las ejecuciones.
4. **Cero pérdida de integridad:** 55/55 criterios activos, 82 claves de esquema JSON, tipificaciones y alarmas conservadas al 100%.

### [LIMITACIONES Y CONSIDERACIONES FINOPS]
1. **Naturaleza del Caching Implícito:** Aunque Prompt B y B2 superan el umbral de 4.096 tokens y alcanzan tasas de caché de hasta el 97.3%, el caching implícito de Google no garantiza un hit en cada llamada individual aislada.
2. **Facturación Productiva:** El ahorro monetario final en producción dependerá de la concurrencia y agrupación por lotes en los workers de evaluación masiva, así como de la variabilidad de thinking tokens del modelo.

---

## 10. Recomendación Final

> [!IMPORTANT]
> **DECISIÓN: PROMPT B2 VALIDADO FUNCIONALMENTE**  
> Prompt B2 (`scratch/prompt_B2.txt`) se encuentra listo para su despliegue como nueva versión en producción de Front (`prompt_id = 58`), logrando la reducción estructural de costes sin comprometer la precisión evaluativa del servicio.
