"""
Task Queue Dispatcher Abstraction.
Dispatches asynchronous tasks via threads when running under the Firebase Emulator,
and via Firebase Task Queues (firebase_admin.functions.task_queue) in production.
"""

import logging
import os
import threading
from collections.abc import Callable

from firebase_admin import functions as admin_functions

logger = logging.getLogger(__name__)


def is_emulator() -> bool:
    """
    Detects whether the application is running inside the Firebase Local Emulator Suite.
    """
    return bool(
        os.environ.get("FUNCTIONS_EMULATOR") == "true"
        or os.environ.get("FIREBASE_EMULATOR_HUB")
        or os.environ.get("FIRESTORE_EMULATOR_HOST")
        or os.environ.get("FIREBASE_AUTH_EMULATOR_HOST")
    )


def _safe_run_worker(worker_fn: Callable[[], object], queue_name: str) -> None:
    """
    Executes a worker function safely within a daemon thread, logging any unhandled exceptions.
    """
    try:
        worker_fn()
    except Exception as ex:
        logger.error(f"Error in background worker thread for queue '{queue_name}': {ex}", exc_info=True)


def dispatch_task(
    queue_name: str,
    task_data: dict[str, object],
    worker_fn: Callable[[], object],
    opts: admin_functions.TaskOptions | None = None,
) -> None:
    """
    Dispatches a task to the Cloud Tasks queue in production, or runs worker_fn in a daemon thread
    when running under the Firebase Emulator (or as a fallback if Cloud Tasks is unavailable).
    """
    if is_emulator():
        logger.info(f"Firebase Emulator detected. Dispatching task for queue '{queue_name}' via thread.")
        t = threading.Thread(target=_safe_run_worker, args=(worker_fn, queue_name), daemon=True)
        t.start()
        return

    try:
        queue = admin_functions.task_queue(queue_name)
        task_opts = opts or admin_functions.TaskOptions(dispatch_deadline_seconds=300)
        task_id = queue.enqueue(task_data, opts=task_opts)
        logger.info(f"Enqueued Firebase task '{task_id}' in queue '{queue_name}'")
    except Exception as e:
        logger.warning(
            f"Firebase task_queue.enqueue exception ({e}) for queue '{queue_name}'. "
            f"Handling fallback dispatch via thread."
        )
        t = threading.Thread(target=_safe_run_worker, args=(worker_fn, queue_name), daemon=True)
        t.start()
