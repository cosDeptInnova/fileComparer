# Comparador documental local

## Cola de trabajo

El comparador ahora usa **Celery con Redis** como broker/backend para las colas de trabajo. La API publica tareas en la cola `COMPARE_QUEUE_NAME` y `comp_docs_worker` arranca workers Celery dedicados.

## Dependencias

```bash
python -m pip install -r requirements.txt
```

> Requisito importante: `celery[redis]>=5.4,<6` y un Redis accesible para broker/backend.

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
python -m app.worker --queue compare --pool threads --concurrency 4
```

### Windows: reglas operativas

- Usa `python -m app.worker` o los scripts del directorio `scripts`.
- El wrapper usa `pool=threads` por defecto en Windows para evitar incompatibilidades de `prefork`.
- Ajusta la concurrencia con `COMPARE_WORKER_CONCURRENCY` o `./start_worker.ps1 -Concurrency N`.
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
python -m app.worker --queue compare --concurrency 4
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
- `scripts/stop-service.ps1 -Name comp_docs` baja la API y también los workers Celery asociados.
- `scripts/stop-service.ps1 -Name comp_docs_worker` primero baja `comp_docs` y luego los workers, para evitar dejar `/comparar` expuesto sin consumidores.
- Los scripts cargan `comp_docs.env` desde la raíz del repo si no existe `config\comp_docs.env`.
