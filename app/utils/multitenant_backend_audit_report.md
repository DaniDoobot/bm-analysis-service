# Informe de Auditoría Integral Backend: Multitenencia y Permisos por Rol

**Fecha**: 2026-07-27  
**Servicio**: Speech BM Backend Analysis Service (`bm-analysis-service`)  
**Estado General**: **OK (100% de Tests Pasados)**  

---

## 1. Resumen Ejecutivo

Antes de abordar la fase visual multiempresa y branding en el frontend, se ha llevado a cabo una auditoría técnica completa del backend. Se evaluó el cumplimiento del aislamiento multiempresa (*multitenancy*) y la matriz de permisos para los 5 roles del sistema:
1. `super_admin`
2. `company_admin` (Administrador de Empresa)
3. `service_manager` / `responsable_servicio` (Responsable de Servicio)
4. `team_coordinator` / `coordinador_equipo` (Coordinador de Equipo)
5. `agent` / `agente` (Agente de atención)

Se ejecutaron **73 pruebas unitarias e integrales** distribuidas en 9 suites de pruebas automáticas. El resultado es **0 errores y 0 fallos**.

---

## 2. Batería de Tests Ejecutada

| Suite de Pruebas | Archivo | Tests | Estado |
| :--- | :--- | :---: | :---: |
| **Compilación de Código** | `python -m compileall app` | N/A | **OK** |
| **Smoke Test de Rutas** | `app/utils/test_smoke_app_import.py` | 244 rutas | **OK** |
| **Jerarquía y Permisos de Usuarios** | `app/utils/test_multitenancy_users.py` | 18 | **OK** |
| **Permisos de Coordinador de Equipo** | `app/utils/test_team_coordinator_permissions.py` | 4 | **OK** |
| **Estructuras Base y Tipologías** | `app/utils/test_base_structures_typologies.py` | 19 | **OK** |
| **Derivación de Scope en Jobs Masivos** | `app/utils/test_mass_evaluation_job_derivation.py` | 9 | **OK** |
| **Ejecución Asíncrona en Background** | `app/utils/test_mass_background_run_regression.py` | 4 | **OK** |
| **Scoping de Test Análisis y Prompts** | `app/utils/test_test_analysis_service_scoping.py` | 7 | **OK** |
| **Contrato Integral Multitenant API** | `app/utils/test_multitenant_api_contract.py` | 12 | **OK** |
| **TOTAL** | | **73** | **100% OK** |

---

## 3. Resultados de Auditoría por Módulo y Endpoints

### 3.1. Auth y Contexto Multiempresa (`/bm/me`, `/bm/me/tenant-context`)
- **Verificado**: `normalized_role`, `company_id`, `allowed_service_ids`, `allowed_team_ids`, `allowed_agent_ids` y flags de permisos (`can_manage_users`, `can_manage_teams`, `can_manage_training`, `can_manage_trainer`, `can_manage_structures`).
- **Resultado**: **OK**. Todos los roles obtienen sus IDs y flags exactos.

### 3.2. Gestión de Usuarios (`/bm/users`, `/bm/admin/users`)
- **Verificado**:
  - `super_admin`: Acceso global a todos los usuarios de cualquier empresa.
  - `company_admin`: Acceso exclusivo a usuarios de su empresa (`company_id`).
  - `service_manager`: Ve `company_admin` (lectura), `service_manager` horizontales (lectura) y coordinadores/agentes asignados a sus servicios (`allowed_service_ids`). No ve usuarios de otros servicios.
  - `team_coordinator`: Ve a sí mismo, agentes de sus equipos (`allowed_team_ids`), y a su `service_manager` inmediato en solo lectura (`is_readonly: true`). No ve agentes ni coordinadores de otros equipos.
  - `agent`: Acceso a administración denegado (`403 Forbidden`).
  - Flags de gestión devueltos (`is_readonly`, `can_edit`, `can_reset_password`, `can_deactivate`, `visibility_reason`).
- **Resultado**: **OK**.

### 3.3. Gestión de Equipos (`/bm/admin/teams`)
- **Verificado**:
  - `company_admin`: Ve y gestiona todos los equipos de su empresa.
  - `service_manager`: Ve y crea/edita equipos de sus servicios permitidos. Intento en otros servicios devuelve `403 Forbidden`.
  - `team_coordinator`: Ve sus equipos asignados (`allowed_team_ids`) y sus miembros. No puede crear ni eliminar equipos (`403 Forbidden`).
  - `agent`: Acceso denegado (`403 Forbidden`).
  - Conteos de miembros utilizan `COUNT(DISTINCT user_id)` sobre `UserTeamAssociation` y `AgentTeamAssociation`.
- **Resultado**: **OK**.

### 3.4. Servicios y Tipologías (`/bm/services`, `/bm/typologies`)
- **Verificado**:
  - `company_admin` y `service_manager` ven servicios y tipologías de su empresa/servicios.
  - `team_coordinator` dispone de acceso de lectura a las tipologías de sus servicios (`allowed_service_ids`), bloqueando la creación o edición (`403 Forbidden`).
  - La creación de tipologías por servicio no interfiere con otras empresas ni servicios.
- **Resultado**: **OK**.

### 3.5. Estructuras Base y Prompts Específicos (`/bm/prompt-base-structures`, `/bm/prompts`)
- **Verificado**:
  - Solo una versión activa por `service_id` + `prompt_type`.
  - La activación de un prompt para un servicio (p. ej. Front) no desactiva el prompt activo de otro servicio (p. ej. Asesores Comerciales o ExpPa).
  - Prompts en borrador/duplicados heredan correctamente el `company_id` derivado del servicio.
  - Borrar una estructura base con prompts específicos asociados exige la confirmación explícita (`confirm_active=true`).
- **Resultado**: **OK**.

### 3.6. Test Análisis (`/bm/test-analysis/by-call-id`, `/bm/test-analysis/by-audio-upload`)
- **Verificado**:
  - La selección de un servicio utiliza la estructura activa asociada a dicho servicio.
  - Pasar `prompt_id` y `service_id` discordantes retorna error de validación `422 Unprocessable Entity`.
  - Si un servicio no tiene estructura activa, responde `422` sin realizar *fallback* indebido a Front.
- **Resultado**: **OK**.

### 3.7. Análisis Masivos Bajo Demanda (`/bm/mass-evaluation-jobs`, `/bm/mass-evaluation-runs`, `/bm/mass-evaluation-results`)
- **Verificado**:
  - La creación de un job deriva automáticamente `company_id` y `service_id` desde la estructura seleccionada.
  - `team_coordinator` solo puede crear o consultar jobs para agentes de sus equipos (`allowed_agent_ids`).
  - Las ejecuciones asíncronas en segundo plano no fallan por `MissingGreenlet` o *status shadowing*.
- **Resultado**: **OK**.

### 3.8. Automatizaciones (`/bm/mass-analysis/automations`)
- **Verificado**:
  - `service_manager` y `team_coordinator` pueden listar y programar automatizaciones dentro de su ámbito de agentes.
  - `team_coordinator` sin especificar agentes se auto-delimita a sus `allowed_agent_ids`.
- **Resultado**: **OK**.

### 3.9. Seguimiento de Agentes y Ciclos (`/bm/training/admin/*`)
- **Verificado**:
  - `GET /bm/training/admin/agents-overview`, `/settings` y `/cycles-summary` retornan a todos los agentes activos de la empresa/equipo aunque no posean ciclos o reportes previos.
  - Se auto-crean de forma transparente e idempotente los registros en `TrainingAgentSetting` para agentes sin configuración inicial.
  - `team_coordinator` únicamente ve agentes de sus equipos.
  - La creación de ciclos manuales (`/manual-cycle`) está permitida para agentes en scope y bloqueada (`403`) para agentes externos.
- **Resultado**: **OK**.

### 3.10. Trainer HTTP (`/bm/trainer/simulations`)
- **Verificado**:
  - Acceso habilitado para administración y coordinación según ámbito de servicios/equipos.
  - Agentes bloqueados de los endpoints administrativos.
- **Resultado**: **OK**.

---

## 4. Ajustes Aplicados Durante la Auditoría

- **Tipologías para Coordinadores (`app/routers/typologies.py`)**:
  - Se ajustó el endpoint de lectura (`GET /bm/typologies` y `GET /bm/typologies/{id}`) para permitir la consulta en solo lectura a los coordinadores de equipo (`team_coordinator`) sobre sus servicios asignados (`allowed_service_ids`), manteniendo bloqueados los endpoints de creación, modificación y eliminación (`403 Forbidden`).

---

## 5. Recomendaciones para la Siguiente Fase (Frontend Multiempresa y Branding)

1. **Header de Autorización y Contexto**: El frontend debe consumir `GET /bm/me/tenant-context` al iniciar sesión para inicializar el selector de empresa/servicio activo, el menú navegable y los botones de acción basados en las propiedades `can_manage_*` y `normalized_role`.
2. **Flags de Gestión en Tabla de Usuarios**: En el listado de usuarios, utilizar las propiedades `is_readonly`, `can_edit`, `can_reset_password` y `can_deactivate` de cada usuario para deshabilitar o mostrar las acciones correspondientes en la interfaz visual.
3. **Respeto de Scoping en Formularios**: En los selectores de creación de Jobs masivos, Automatizaciones o Test Análisis, filtrar los desplegables de agentes y servicios respetando las listas `allowed_service_ids` y `allowed_team_ids` entregadas por la API.
