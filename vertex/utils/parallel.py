"""Run per-item work in a small thread pool, each worker with its own SQLite connection (writes serialize)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from vertex.db.connection import connect
from vertex.settings import get_settings


def parallel_map(fn: Callable[[sqlite3.Connection, Any], Any], items: Iterable[Any], workers: int = 4,
                 on_result: Callable[[Any, Any], None] | None = None, on_error: Callable[[Any, Exception], None] | None = None) -> list[Any]:
    items = list(items)
    results: list[Any] = []
    db_path = get_settings().db_path

    def _run(item: Any) -> Any:
        # Autocommit connection: no transaction is held open across the (slow) LLM call, so concurrent
        # workers never deadlock on SQLite's shared->reserved lock upgrade. Writers use short transactions.
        conn = connect(db_path)
        try:
            return fn(conn, item)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(_run, it): it for it in items}
        for fut in as_completed(futs):
            it = futs[fut]
            try:
                res = fut.result()
                results.append(res)
                if on_result:
                    on_result(it, res)
            except Exception as e:  # noqa: BLE001
                if on_error:
                    on_error(it, e)
    return results
