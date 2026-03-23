COSMOS – Scripts de despliegue/arranque (PowerShell)
===================================================

1. Objetivo
-----------
Esta carpeta contiene scripts PowerShell para:
- Arrancar/parar todos los microservicios (start-all / stop-all)
- Arrancar/parar servicios individuales (start-service / stop-service)
- Consultar el estado de ejecución (status)
- Exportar variables de entorno de una sesión PowerShell a ficheros config\<servicio>.env
- Gestionar PID files y logs de salida/error
- Liberar puertos ocupados de forma robusta (incluye parada de servicios Windows y kill tree)

2. Requisitos previos
---------------------
Sistema: Windows (PowerShell) con permisos suficientes.
Requisitos recomendados:
- Anaconda/Miniconda instalado
- Entorno conda con dependencias del proyecto (ver services.psd1 -> CondaEnv)
- Docker Desktop (si se despliegan redis/postgresql/qdrant/clamav por docker-compose)
- Puertos libres según configuración (services.psd1)

NOTA: Algunos cierres de procesos/servicios requieren ejecutar PowerShell "Como Administrador".

3. Estructura creada automáticamente
------------------------------------
Al ejecutar los scripts, se crearán (si no existen) en el repo:
- run\        -> PID files + launchers generados
- logs\       -> logs *.out.log y *.err.log por servicio
- config\     -> ficheros .env consolidados
- scripts\    -> (esta carpeta)
- EVIDENCIAS_HITO2\ (carpeta de evidencias)

4. Configuración (services.psd1)
--------------------------------
Los scripts leen scripts\services.psd1 (PowerShell data file), donde se define:
- CondaEnv (nombre del entorno conda)
- GlobalEnvFile (opcional: .env global)
- FailFast (opcional)
- StartOrder (orden recomendado de arranque)
- Services: listado con Name, Path, Port, Args, etc.

IMPORTANTE: Mantener actualizado services.psd1 cuando se añadan o modifiquen microservicios.

5. Arranque/Parada/Estado
-------------------------
Abrir PowerShell en la carpeta /scripts y ejecutar:

Arrancar todo:
  .\start-all.ps1

Parar todo:
  .\stop-all.ps1

Ver estado:
  .\status.ps1

Arrancar un servicio:
  .\start-service.ps1 -Name <NombreServicio>

Parar un servicio:
  .\stop-service.ps1 -Name <NombreServicio>


Ejemplo (reinicio de un solo microservicio tras cambios en su código):
  .\stop-service.ps1 -Name cosmos_mcp -ShowStatus
  .\start-service.ps1 -Name cosmos_mcp -ShowStatus

Los logs se almacenan en:
  ..\logs\<servicio>.out.log
  ..\logs\<servicio>.err.log

Servicio `comp_docs` y worker dedicado
--------------------------------------
Para `comp_docs` ahora existen dos entradas gestionadas por scripts y apuntan al worker real:
- `comp_docs` -> proceso web FastAPI en el puerto 8007
- `comp_docs_worker` -> worker dedicado del comparador (`python -m app.compare_worker`)

Al arrancar `comp_docs` con `start-service.ps1`, el script también levanta automáticamente
su servicio compañero `comp_docs_worker`. El arranque es idempotente: si el worker ya
está en ejecución, no se duplica el proceso.

Ejemplos:
  .\start-service.ps1 -Name comp_docs
  .\start-service.ps1 -Name comp_docs_worker
  .\start-service.ps1 -Name comp_docs -CompDocsWorkerCount 12
  .\start-service.ps1 -Name comp_docs_worker -CompDocsWorkerConcurrency 24
  .\start-service.ps1 -Name comp_docs_worker -CompDocsWorkerCount 12 -CompDocsWorkerConcurrency 24
  .\start-all.ps1 -CompDocsWorkerCount 12
  .\start-all.ps1 -CompDocsWorkerConcurrency 24
  .\stop-service.ps1 -Name comp_docs_worker

`comp_docs` y `comp_docs_worker` cargan `config\ALL_EXPORT.env` como base global y `config\comp_docs.env`
como configuración efectiva del comparador. Las variables operativas relevantes son, entre otras:
- `TEXT_COMPARE_MAX_FILE_MB`
- `COMPARE_REQUIRE_ACTIVE_WORKERS`
- `COMPARE_PROGRESS_TTL_SECONDS`
- `COMPARE_RESULT_TTL_SECONDS`
- `COMPARE_CLEANUP_INTERVAL_SECONDS`
- `COMPARE_TEMP_FILE_TTL_SECONDS`
- `COMPARE_EXTRACT_TIMEOUT_SECONDS`
- `COMPARE_DOCLING_TIMEOUT_SECONDS`
- `COMPARE_OCR_TIMEOUT_SECONDS`
- `COMPARE_LLM_TIMEOUT_SECONDS`

`comp_docs_worker` arranca con `ProcessCount=12`, `COMPARE_WORKER_CONCURRENCY=16` y `MAX_CONCURRENT_JOBS=16` por defecto. Puedes cambiar el número de procesos en `scripts\services.psd1` o en tiempo de arranque con `-CompDocsWorkerCount`, y ajustar la concurrencia interna con `-CompDocsWorkerConcurrency`.


6. Gestión robusta de puertos
-----------------------------
Antes de arrancar un servicio con "Port" definido en services.psd1, el arranque:
- Detecta procesos escuchando en el puerto (Get-NetTCPConnection y fallback netstat)
- Opcionalmente para servicios Windows asociados a ese PID
- Mata el árbol de procesos (Stop-Process y fallback taskkill /T /F)
- Reintenta hasta liberar el puerto

Si el puerto no se libera, se muestra diagnóstico (PID, nombre proceso, cmdline y servicios).

7. Exportación de variables de entorno a config\<servicio>.env
--------------------------------------------------------------
Script: export-session-env-to-config.ps1

Uso típico (desde la carpeta del microservicio, p.ej. /auth, /chatdoc, etc.):
  ..\scripts\export-session-env-to-config.ps1

Opciones:
-RepoRoot <ruta>         -> si no puede autodetectar el repo (busca docker-compose.yml hacia arriba)
-Merge:$true|$false      -> fusiona con config\<svc>.env existente (por defecto true)
-ImportLocalDotEnv       -> importa .env local si existe (por defecto true)
-Show                    -> muestra resultado por consola (enmascara secretos)
-ShowSecrets             -> muestra secretos sin enmascarar (NO recomendado)
-Backup:$true|$false     -> crea backup del fichero antes de sobrescribir (por defecto true)

NOTA: Este script extrae claves de entorno del historial (Get-History) buscando asignaciones tipo:
  $env:VAR = ...
Por tanto, se debe ejecutar en la misma consola donde se exportaron esas variables.

8. Consideraciones de seguridad
-------------------------------
- Los ficheros config\<svc>.env pueden contener secretos (tokens/keys/passwords). Protegerlos.
- Evitar usar -ShowSecrets en entornos compartidos.
- Ejecutar como admin solo cuando sea necesario (liberar puertos/servicios).
- Revisar logs ante fallos de arranque o timeouts.

9. Troubleshooting rápido
-------------------------
- "Cannot find 'conda'": ejecutar 'conda init powershell' y reiniciar la consola, o asegurar conda en PATH.
- "port XXXX still busy": ejecutar PowerShell como administrador y relanzar stop-all / start-all.
- "timeout port did not open": revisar logs/<svc>.err.log y comprobar args/paths/env.