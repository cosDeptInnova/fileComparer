# Comparador documental local

## Dependencias

```bash
python -m pip install -r requirements.txt
```

## Arranque local

```bash
cp .env.example .env
redis-server
cd scripts && ./start_worker.sh
cd scripts && ./start_api.sh
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
