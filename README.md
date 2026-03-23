# Comparador documental local

## Bug raíz

El worker por defecto de RQ (`rq worker` / `rq.Worker`) intenta usar `os.fork()`. En Windows eso rompe con `AttributeError: module 'os' has no attribute 'fork'`. Este proyecto ya no documenta ni arranca ese worker en Windows: hay una fábrica centralizada en `app/worker.py` que usa `SpawnWorker` cuando está disponible y aborta con un error explícito si alguien fuerza el worker clásico en Windows.

## Dependencias

```bash
python -m pip install -r requirements.txt
```

> Requisito importante: `rq>=2.2,<3` para poder usar `rq.worker.SpawnWorker` en Windows.

## Arranque local en Windows

### 1) Redis

Opción robusta y repetible en Windows: Redis en Docker Desktop.

```powershell
docker run --name comp-docs-redis --rm -p 6379:6379 redis:7-alpine
```

### 2) API

```powershell
cd scripts
./start_api.ps1
```

### 3) Worker

```powershell
cd scripts
./start_worker.ps1
```

También funciona directamente así:

```powershell
cd /ruta/al/repo
$env:COMPARE_WINDOWS_WORKER_MODE = "production"
python -m app.worker --queue compare
```

### Windows: reglas operativas

- **No uses `rq worker` en Windows.**
- `python -m app.worker` selecciona `SpawnWorker` automáticamente si RQ 2.2+ está instalado.
- Si `SpawnWorker` no está disponible:
  - `COMPARE_WINDOWS_WORKER_MODE=production` aborta con error y pide subir RQ / mover producción a Linux.
  - `COMPARE_WINDOWS_WORKER_MODE=development` permite fallback controlado a `SimpleWorker`, **solo para desarrollo**.
- Para producción estable se recomienda ejecutar API + worker + Redis en **Linux, WSL2 o Docker**.

## Arranque local en Linux/macOS

### 1) Redis

```bash
redis-server
```

### 2) Worker

```bash
cd scripts && ./start_worker.sh
```

### 3) API

```bash
cd scripts && ./start_api.sh
```

O directamente:

```bash
python -m app.worker --queue compare
python -m uvicorn app.main:app --host 0.0.0.0 --port 8007 --reload
```

## Prueba rápida

```bash
curl -i http://127.0.0.1:8007/csrf-token
curl -X POST http://127.0.0.1:8007/comparar \
  -H 'X-CSRFToken: TOKEN' \
  -b 'csrftoken_app=TOKEN' \
  -F 'file_a=@tests/fixtures/base_a.txt' \
  -F 'file_b=@tests/fixtures/base_b.txt'
```


## Scripts de servicio en Windows

- `scripts/start-service.ps1 -Name comp_docs` arranca la API y su companion `comp_docs_worker`.
- `scripts/stop-service.ps1 -Name comp_docs` baja la API y también los workers RQ asociados.
- `scripts/stop-service.ps1 -Name comp_docs_worker` primero baja `comp_docs` y luego los workers, para evitar dejar `/comparar` expuesto sin consumidores.
- Los scripts cargan `comp_docs.env` desde la raíz del repo si no existe `config\comp_docs.env`.
