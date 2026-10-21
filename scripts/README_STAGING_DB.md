# Guía de Gestión y Clonación de Base de Datos para Staging

Este directorio contiene las herramientas y procedimientos para crear, refrescar y validar de forma 100% segura la base de datos del entorno **STAGING / PRUEBAS** a partir de una réplica fiel de **PRODUCCIÓN**.

---

## 1. Inventario de Scripts

| Archivo | Tipo | Propósito | Entorno Objetivo |
| :--- | :--- | :--- | :--- |
| [`refresh_staging_db.sh`](file:///c:/Users/Dani/proyectos/bm-analysis-service/scripts/refresh_staging_db.sh) | Bash | Orquesta el volcado (`pg_dump -Fc`), restauración (`pg_restore`), neutralización y validación. | Servidor / CLI de administración |
| [`neutralize_staging_db.sql`](file:///c:/Users/Dani/proyectos/bm-analysis-service/scripts/neutralize_staging_db.sql) | SQL (DML) | Desactiva schedulers automáticos, jobs en progreso y purga tokens de reset. | **ÚNICAMENTE STAGING** |
| [`validate_staging_db.sql`](file:///c:/Users/Dani/proyectos/bm-analysis-service/scripts/validate_staging_db.sql) | SQL (Lectura) | Verifica conteos de fidelidad, presencia de las 8 vistas `vw_bm_*` y ausencia de estados de riesgo. | **ÚNICAMENTE STAGING** |

---

## 2. Salvaguardas Críticas contra Producción

Tanto el script de orquestación (`refresh_staging_db.sh`) como los scripts SQL incorporan **bloqueos técnicos absolutos** para impedir cualquier ejecución accidental sobre la base de datos de Producción:

1. **Validación de nombre de base de datos:**
   - La base de datos destino **DEBE** contener en su nombre una de las palabras clave: `staging`, `pruebas`, `test` o `dev` (ej. `speech_staging`, `bm_pruebas`).
   - Si la base de datos destino se llama `n8n`, `speechbm`, `production`, `prod`, `bm_analysis` o `postgres`, la ejecución se **aborta de inmediato**.
2. **Requisito de confirmación explícita:**
   - `refresh_staging_db.sh` requiere el parámetro `--confirm-refresh-staging`. Sin él, finaliza con código 1 sin realizar ninguna acción.
3. **Guard dentro de la transacción SQL (`neutralize_staging_db.sql`):**
   - Antes de cualquier sentencia DML, ejecuta un bloque `DO $$ ... $$` que consulta `current_database()`. Si no cumple los criterios de staging, emite `RAISE EXCEPTION` y la transacción se revierte (`ROLLBACK`).

---

## 3. Prerrequisitos

- Cliente PostgreSQL instalado en el host (`pg_dump`, `pg_restore`, `psql`).
- Variables de entorno obligatorias:
  ```bash
  export PROD_DATABASE_URL="postgresql://usuario:contraseña@servidor:5432/speechbm"
  export STAGING_DATABASE_URL="postgresql://usuario:contraseña@servidor:5432/speech_staging"
  ```
- Permiso de lectura en la base de datos origen y permiso de creación/escritura en la base de datos destino.

---

## 4. Qué hace y qué NO hace el proceso

### Lo que SÍ hace:
- **Copia completa y fiel:** Clona el 100% de tablas, secuencias, tipos, constraints y las 8 vistas analíticas (`vw_bm_*`).
- **Preserva el histórico:** Conserva análisis individuales, evaluaciones masivas, criterios, prompts, empresas y datos de facturación IA (FinOps).
- **Conserva usuarios y accesos:** Mantiene contraseñas y roles intactos; cada usuario puede acceder en Staging con sus credenciales habituales.
- **Desactiva automatizaciones:**
  - `bm_mass_evaluation_jobs.schedule_enabled = false`
  - `bm_mass_analysis_automations.is_active = false`
  - `bm_training_schedulers.is_active = false`
  - `bm_training_scheduler_settings.is_enabled = false`
- **Neutraliza ejecuciones activas:** Runs de evaluaciones masivas, automatizaciones, training y CloudTalk que estuvieran en progreso durante el snapshot pasan a estados terminales (`cancelled` / `skipped` / `failed`).
- **Purga tokens de reseteo:** Elimina tokens de recuperación activos (`bm_password_reset_tokens`).

### Lo que NO hace:
- **NO toca Dokploy:** No detiene ni arranca contenedores Docker ni modifica configuraciones de despliegue.
- **NO borra datos históricos:** No vacía tablas analíticas ni elimina configuraciones.
- **NO modifica Producción:** La base de datos origen se consulta de forma estrictamente de solo lectura (`pg_dump`).

---

## 5. Procedimiento Futuro de Ejecución (Paso a Paso)

Cuando se vaya a desplegar el entorno Staging:

### Paso 1: Exportar variables seguras
```bash
export PROD_DATABASE_URL="postgresql://db_user:secret_prod@127.0.0.1:5432/speechbm"
export STAGING_DATABASE_URL="postgresql://db_user:secret_staging@127.0.0.1:5432/speech_staging"
```

### Paso 2: Ejecutar el refresco de base de datos
```bash
chmod +x scripts/refresh_staging_db.sh
./scripts/refresh_staging_db.sh --confirm-refresh-staging
```

### Paso 3: Configurar variables del backend Staging en Dokploy
Antes de arrancar el contenedor del backend de Staging, asegurar las siguientes variables:
```env
DATABASE_URL=postgresql+asyncpg://db_user:secret_staging@127.0.0.1:5432/speech_staging
ENABLE_MASS_EVALUATION_SCHEDULER=false
ENABLE_AUTOMATION_SCHEDULER=false
ENABLE_TRAINING_SCHEDULER=false
ENABLE_CLOUDTALK_SCHEDULER=false
ENABLE_HUBSPOT_WRITES=false
JWT_SECRET_KEY=clave-secreta-exclusiva-de-staging-xyz123
```

### Paso 4: Arrancar el backend de Staging
Encender el servicio en Dokploy. El servicio [`db_init_service`](file:///c:/Users/Dani/proyectos/bm-analysis-service/app/services/db_init_service.py) arrancará limpiamente (operación NO-OP sobre esquemas ya creados).
