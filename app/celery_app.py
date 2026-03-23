from __future__ import annotations

from typing import Any, Callable

from app.settings import settings

try:
    from celery import Celery
except Exception as exc:  # pragma: no cover - lightweight environments may not have celery installed
    _CELERY_IMPORT_ERROR = exc

    class _MissingInspect:
        def active_queues(self):
            raise RuntimeError(
                "Celery no está instalado en este entorno. Instala las dependencias de requirements.txt antes de usar la cola. "
                f"Error original: {_CELERY_IMPORT_ERROR}"
            )

    class _MissingControl:
        def inspect(self, timeout: float | None = None):
            return _MissingInspect()

    class _MissingTask:
        def __init__(self, func: Callable[..., Any], name: str):
            self._func = func
            self.__name__ = getattr(func, "__name__", name)
            self.__doc__ = getattr(func, "__doc__")
            self.name = name

        def __call__(self, *args: Any, **kwargs: Any):
            return self._func(*args, **kwargs)

        def apply_async(self, *args: Any, **kwargs: Any):
            raise RuntimeError(
                "Celery no está instalado en este entorno. Instala las dependencias de requirements.txt antes de encolar tareas. "
                f"Error original: {_CELERY_IMPORT_ERROR}"
            )

    class _FallbackConf(dict):
        def update(self, *args: Any, **kwargs: Any):
            super().update(*args, **kwargs)

    class _FallbackCelery:
        def __init__(self):
            self.conf = _FallbackConf()
            self.control = _MissingControl()

        def task(self, *dargs: Any, **dkwargs: Any):
            def decorator(func: Callable[..., Any]):
                return _MissingTask(func, dkwargs.get("name") or getattr(func, "__name__", "task"))

            if dargs and callable(dargs[0]) and not dkwargs:
                return decorator(dargs[0])
            return decorator

        def autodiscover_tasks(self, *args: Any, **kwargs: Any):
            return None

        def worker_main(self, argv: list[str]):
            raise RuntimeError(
                "Celery no está instalado en este entorno. Instala las dependencias de requirements.txt antes de arrancar workers. "
                f"Error original: {_CELERY_IMPORT_ERROR}"
            )

    celery_app = _FallbackCelery()
else:
    celery_app = Celery(
        "comp_docs",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
    )
    celery_app.conf.update(
        imports=("app.services.jobs",),
        task_default_queue=settings.compare_queue_name,
        task_track_started=True,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        result_expires=60 * 60 * 24,
        worker_prefetch_multiplier=1,
        task_acks_late=False,
        broker_connection_retry_on_startup=True,
        task_time_limit=max(1, int(settings.llm_timeout_seconds * 4)),
    )
    celery_app.autodiscover_tasks(["app.services"])