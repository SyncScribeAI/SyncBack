"""
Celery-приложение SyncScribe.

Требования к воркеру
--------------------
Задачи в analysis_tasks.py используют asyncio.run() для запуска async-кода из
синхронного Celery-таска. asyncio.run() создаёт новый event loop на каждый
вызов и закрывает его по завершении. Это безопасно только при использовании
пула процессов (prefork, по умолчанию).

НЕЛЬЗЯ запускать воркер с пулами:
  - gevent  (celery worker -P gevent)
  - eventlet (celery worker -P eventlet)
  - solo     (однопоточный, только для отладки в dev)

При этих пулах asyncio.run() вызывает RuntimeError("уже запущен event loop")
или создаёт вложенный цикл. Допустимая команда запуска воркера:

    celery -A app.workers.celery_app worker --loglevel=info
    # или явно:
    celery -A app.workers.celery_app worker --loglevel=info -P prefork

Очереди
-------
Задача анализа v2 (analyze_document_for_job) идёт в очередь ANALYSIS_V2_QUEUE
(по умолчанию analysis_v2), остальные — в общую очередь celery. ML-конвейер выдерживает
ограниченное число одновременных прогонов: если общий воркер с --concurrency=4 отправит
больше, лишние получат 429 и после повторов упадут с LLM_UNAVAILABLE, хотя конвейер просто
занят. Отдельный воркер очереди с --concurrency, равным числу слотов конвейера, держит
документы в очереди брокера, где ожидание не ограничено лимитами задачи. Повтор задачи
(self.retry) маршрутизируется по имени и остаётся в той же очереди.

    celery -A app.workers.celery_app worker -Q celery --concurrency=4
    celery -A app.workers.celery_app worker -Q analysis_v2 --concurrency=1

Если очередь analysis_v2 никто не слушает, задачи v2 остаются в статусе dispatched.

Если в будущем потребуется перейти на gevent/eventlet —
замените asyncio.run() на loop.run_until_complete() с получением
текущего loop через asyncio.get_event_loop().
"""

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

DEFAULT_QUEUE = "celery"

# Лимиты времени (soft 540 с / hard 570 с) заданы на самой задаче
# process_source_for_analysis_job (см. analysis_tasks.py) вместе с lease захвата
# источника (600 с), чтобы инвариант «lease больше жёсткого лимита» жил в одном
# месте и не распространялся на finalize_analysis_job и будущие задачи.

celery_app = Celery(
    "syncscribe",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.workers.tasks.analysis_tasks",
        "app.workers.tasks.analysis_v2_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    # При падении воркера незавершённая задача возвращается в очередь,
    # а не теряется. Работает в паре с task_acks_late=True.
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    result_expires=86400,
    task_default_queue=DEFAULT_QUEUE,
    task_routes={"analyze_document_for_job": {"queue": settings.analysis_v2_queue}},
)
