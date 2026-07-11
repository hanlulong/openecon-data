"""Guard: concurrent FTS searches must not corrupt each other.

The lookup service is called from asyncio.to_thread workers in parallel. A
single shared sqlite3 connection (check_same_thread=False) corrupted
in-flight cursors under concurrency (sqlite3.InterfaceError "bad parameter
or other API misuse" / IndexError observed in production, ~40/day), which
silently degraded the lexical retrieval arm. Connections are now per-thread.
"""

import threading

from backend.services.indicator_database import get_indicator_lookup


def test_concurrent_searches_do_not_error():
    lookup = get_indicator_lookup()
    errors = []

    def hammer():
        try:
            for _ in range(10):
                lookup.search("unemployment rate", provider="FRED", limit=5)
                lookup.search("gdp growth", provider="WORLDBANK", limit=5)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=hammer) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors


def test_per_thread_connections_are_distinct():
    from backend.services.indicator_database import IndicatorDatabase, DB_PATH

    db = IndicatorDatabase(DB_PATH)
    conns = {}

    def grab(name):
        conns[name] = id(db._get_connection())

    t1 = threading.Thread(target=grab, args=("a",))
    t2 = threading.Thread(target=grab, args=("b",))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert conns["a"] != conns["b"]
