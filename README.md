# bm-analysis-service

Backend FastAPI para el sistema de análisis de llamadas de **Boston Medical**. Sustituye progresivamente los workflows de n8n y el uso de Google Drive.

## Stack

| Componente | Tecnología |
|---|---|
| Lenguaje | Python 3.12 |
| Framework | FastAPI + Uvicorn |
| ORM | SQLAlchemy 2.x (async) |
| Driver BD | asyncpg |
| Base de datos | PostgreSQL (existente) |
| Schemas | Pydantic v2 |
| HTTP cliente | httpx |
| IA | Azure OpenAI SDK |
| Deploy | Dokploy + Docker |

---

## Ejecución local

### 1. Clonar y preparar entorno

```bash
git clone <repo>
cd bm-analysis-service
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configurar variables de entorno

```bash
cp .env.example .env
# Editar .env con DATABASE_URL y demás credenciales
```

La única variable **obligatoria** para la Fase 1 es:

```
DATABASE_URL=postgresql://user:pass@host:5432/dbname
```

### 3. Arrancar el servidor

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Swagger UI disponible en: `http://localhost:8000/docs`

---

## Despliegue en Dokploy (VPS)

1. En Dokploy, crear un nuevo servicio tipo **Application** apuntando al repositorio.
2. Configurar el **Dockerfile** (ya incluido en el repositorio).
3. Añadir las variables de entorno desde `.env.example` en el panel de Dokploy.
4. El servicio expone el puerto **8000**.

Comando que ejecuta Docker al arrancar:
```
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
```

---

## Estructura del proyecto

```
bm-analysis-service/
├── app/
│   ├── main.py                          # Entry point FastAPI
│   ├── config.py                        # Settings via pydantic-settings
│   ├── db.py                            # Engine async SQLAlchemy
│   ├── dependencies.py                  # Inyección de dependencias (DB session)
│   ├── models/
│   │   ├── prompts.py                   # Prompt, PromptVersion
│   │   ├── criteria.py                  # PromptCriterion
│   │   ├── analyses.py                  # Analysis, CallAnalysisCurrent, AnalysisResult
│   │   └── drafts.py                    # PromptDraft
│   ├── schemas/
│   │   ├── prompts.py                   # Pydantic schemas prompts
│   │   ├── criteria.py                  # Pydantic schemas criterios
│   │   ├── analyses.py                  # Pydantic schemas análisis
│   │   └── common.py                    # OkResponse, ErrorResponse
│   ├── routers/
│   │   ├── health.py                    # GET /health
│   │   ├── prompts.py                   # GET/POST /bm/prompts, /bm/save-prompt, etc.
│   │   ├── criteria.py                  # GET/POST /bm/prompt-criteria
│   │   ├── drafts.py                    # GET/POST /bm/prompt-draft
│   │   ├── analyses.py                  # GET /bm/analyses, /bm/analysis-detail
│   │   ├── audio_analysis.py            # POST /bm/analyze-audio (Fase 2)
│   │   ├── transcription_analysis.py    # POST /bm/transcribe, /bm/analyze-transcription (Fase 2)
│   │   └── prompt_builder.py            # POST /bm/prompt/build-with-ai (Fase 2)
│   ├── services/
│   │   ├── prompts_service.py           # Lógica de prompts y versiones
│   │   ├── criteria_service.py          # Lógica de criterios
│   │   ├── drafts_service.py            # Lógica de borradores
│   │   ├── analyses_service.py          # Listado y detalle de análisis
│   │   ├── analysis_persistence.py      # save_analysis() centralizado
│   │   ├── analysis_results_mapper.py   # Mapping/grouping de resultados
│   │   ├── prompt_builder.py            # Generación de prompt con IA
│   │   ├── openai_service.py            # Azure OpenAI (mantenemos nombre archivo)
│   │   ├── hubspot_service.py           # HubSpot CRM API
│   │   ├── twilio_service.py            # Descarga de audio Twilio
│   │   └── transcription_service.py     # Pipeline transcripción completo
│   └── utils/
│       ├── json_utils.py                # Parse JSON seguro (strip fences)
│       ├── dates.py                     # Parseo de fechas (HubSpot ms, ISO)
│       └── normalizers.py              # Normalización de valores booleanos/numéricos
├── requirements.txt
├── Dockerfile
├── .env.example
└── README.md
```

---

## Variables de entorno

| Variable | Obligatoria | Descripción |
|---|---|---|
| `DATABASE_URL` | ✅ Siempre | PostgreSQL connection string |
| `AZURE_OPENAI_API_VERSION` | ✅ Siempre | Versión API Azure |
| `AZURE_OPENAI_TEXT_ENDPOINT` | Para AI gen/analisis | Endpoint texto Azure OpenAI |
| `AZURE_OPENAI_TEXT_API_KEY` | Para AI gen/analisis | API Key texto Azure |
| `AZURE_OPENAI_TEXT_DEPLOYMENT` | Para AI gen/analisis | Deployment para texto |
| `AZURE_OPENAI_AUDIO_ENDPOINT` | Para análisis audio | Endpoint audio Azure OpenAI |
| `AZURE_OPENAI_AUDIO_API_KEY` | Para análisis audio | API Key audio Azure |
| `AZURE_OPENAI_AUDIO_DEPLOYMENT` | Para análisis audio | Deployment para audio |
| `AZURE_OPENAI_TRANSCRIPTION_ENDPOINT` | Para transcripción | Endpoint transcripción Azure OpenAI |
| `AZURE_OPENAI_TRANSCRIPTION_API_KEY` | Para transcripción | API Key transcripción Azure |
| `AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT` | Para transcripción | Deployment para transcripción |
| `HUBSPOT_ACCESS_TOKEN` | Fase 2 | Token acceso HubSpot |
| `HUBSPOT_PORTAL_ID` | No | Para construir URLs de HubSpot |
| `TWILIO_ACCOUNT_SID` | Fase 2 | Para descargar grabaciones |
| `TWILIO_AUTH_TOKEN` | Fase 2 | Auth Twilio |
| `CORS_ORIGINS` | No | Orígenes CORS separados por coma |

---

## Endpoints Fase 1

### Health

```bash
curl http://localhost:8000/health
# {"ok":true,"service":"bm-analysis-service"}
```

### Listar prompts

```bash
curl http://localhost:8000/bm/prompts
```

### Prompt activo por tipo

```bash
curl "http://localhost:8000/bm/prompts/active?type=audio"
curl "http://localhost:8000/bm/prompts/active?type=text"
```

### Versiones de un prompt

```bash
curl "http://localhost:8000/bm/prompt-versions?prompt_id=1"
```

### Guardar nueva versión de prompt

```bash
curl -X POST http://localhost:8000/bm/save-prompt \
  -H "Content-Type: application/json" \
  -d '{
    "prompt_id": 1,
    "prompt": "Eres un analizador de llamadas...",
    "updated_by": "Dani",
    "updated_by_email": "dani@doobot.ai",
    "change_note": "Añadido criterio de empatía"
  }'
```

### Activar versión

```bash
curl -X POST http://localhost:8000/bm/activate-prompt-version \
  -H "Content-Type: application/json" \
  -d '{"id": 15}'
```

### Criterios de un prompt (agrupados)

```bash
curl "http://localhost:8000/bm/prompt-criteria?prompt_id=1"
```

### Guardar criterio

```bash
curl -X POST http://localhost:8000/bm/prompt-criteria/save \
  -H "Content-Type: application/json" \
  -d '{
    "prompt_id": 1,
    "criterion_key": "empatia",
    "criterion_name": "Empatía del agente",
    "criterion_description": "Valora si el agente muestra empatía con el paciente",
    "criterion_type": "score_1_10",
    "output_key": "empatia",
    "feed_key": "empatia_feed",
    "order_index": 10,
    "is_required": true,
    "is_active": true
  }'
```

### Toggle criterio

```bash
curl -X POST http://localhost:8000/bm/prompt-criteria/toggle \
  -H "Content-Type: application/json" \
  -d '{"criterion_id": 42, "is_active": false}'
```

### Listar análisis

```bash
# Todos los análisis de audio
curl "http://localhost:8000/bm/analyses?type=audio"

# Con filtros
curl "http://localhost:8000/bm/analyses?type=audio&agent=Juan&date_from=2025-01-01&limit=50"
```

### Detalle de análisis

```bash
# Por analysis_id
curl "http://localhost:8000/bm/analysis-detail?analysis_id=12"

# Por call_id + type
curl "http://localhost:8000/bm/analysis-detail?call_id=491043972285&type=audio"
```

### Borrador de prompt

```bash
# Obtener
curl "http://localhost:8000/bm/prompt-draft?prompt_id=1&user_email=dani@doobot.ai"

# Guardar
curl -X POST http://localhost:8000/bm/prompt-draft/save \
  -H "Content-Type: application/json" \
  -d '{
    "prompt_id": 1,
    "draft_name": "Mi borrador",
    "draft_data": {"notas": "..."},
    "updated_by": "Dani",
    "updated_by_email": "dani@doobot.ai"
  }'

# Descartar
curl -X POST http://localhost:8000/bm/prompt-draft/discard \
  -H "Content-Type: application/json" \
  -d '{"draft_id": 5}'
```

### Analizar Transcripción (Fase 2.2)

```powershell
$body = @{
    call_id = "123456789"
    transcription = "Agente: Hola, le llamo de Boston Medical. Paciente: Hola, quería pedir cita para el jueves."
    metadata = @{
        agente_telefonico = "Juan Pérez"
        call_direction = "INBOUND"
    }
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:8000/bm/analyze-transcription" `
  -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body $body
```

### Analizar Audio (Fase 2.3)

**1. Usando URL de grabación directa (vía Twilio o público)**
```powershell
$body = @{
    call_id = "manual-audio-test-001"
    prompt_id = 1
    analysis_type = "audio"
    recording_url = "https://api.twilio.com/2010-04-01/Accounts/.../Recordings/...mp3"
    metadata = @{
        source = "manual_test"
        agente_telefonico = "Agente Prueba"
        call_direction = "OUTBOUND"
    }
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Uri "http://localhost:8000/bm/analyze-audio" `
  -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body $body
```

**2. Descargando llamada directamente desde HubSpot por call_id**
```powershell
$body = @{
    call_id = "491043972285"
    prompt_id = 1
    analysis_type = "audio"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:8000/bm/analyze-audio" `
  -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body $body
```

---

## Fase 2 — Pendiente

Los siguientes endpoints están declarados pero devuelven **501 Not Implemented** hasta que se configuren las credenciales:

| Endpoint | Requiere |
|---|---|
| `POST /bm/transcribe` | `HUBSPOT_ACCESS_TOKEN`, `TWILIO_ACCOUNT_SID`, `AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT` |

Para activar Fase 2:
1. Añadir las credenciales al `.env`
2. Implementar la lógica en los routers correspondientes (esqueleto ya preparado)
3. El servicio `analysis_persistence.save_analysis()` ya está completo y funcional

---

## Patrones de respuesta

**Éxito:**
```json
{"ok": true, "status": "completed", ...}
```

**Error controlado:**
```json
{"ok": false, "status": "error", "error_message": "Descripción del error"}
```

---

## Reglas de arquitectura

- **Lógica de negocio** → `services/`, nunca en routers
- **Criterios** → siempre desde `bm_prompt_criteria`, nunca hardcodeados ni de `draft_data`
- **Keys de análisis** → siempre `output_key` y `feed_key` de criterios activos
- **No usar** `campo_1`, `campo_2`, `campo_3` ni variantes
- **save_analysis()** es el único punto de escritura de análisis; siempre escribe las 3 tablas
- **Fechas vacías** → `None`, nunca string vacío ni crash
