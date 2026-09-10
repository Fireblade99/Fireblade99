#!/usr/bin/env python3
"""
Тест классификатора причин отказа.

Метка reason уходит в Prometheus: по ней строятся алерты и по ней же
разбирают инцидент. Поэтому список причин закрытый, а соответствие
"текст ошибки -> причина" зафиксировано здесь.

    python3 probe/test_classify.py
"""
import os
import socket
import sys

os.environ.setdefault("PGPROBE_DSN", "postgresql://x@127.0.0.1:1/x")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2                                             # noqa: E402
import probe                                                # noqa: E402

CASES = [
    (psycopg2.OperationalError("server closed the connection unexpectedly"), "connection_lost"),
    (psycopg2.OperationalError("connection already closed"),                 "connection_lost"),
    (psycopg2.OperationalError("[Errno 111] Connection refused"),            "network"),
    (psycopg2.OperationalError("no route to host"),                          "network"),
    (socket.timeout("timed out"),                                            "timeout"),
    (socket.gaierror("Name or service not known"),                           "dns"),
    (psycopg2.OperationalError('password authentication failed for user "m"'), "auth"),
    (psycopg2.OperationalError("sorry, too many clients already"),           "connection_limit"),
    (psycopg2.OperationalError("the database system is starting up"),        "not_ready"),
    (psycopg2.OperationalError("the database system is shutting down"),      "not_ready"),
    (psycopg2.OperationalError("SSL SYSCALL error: EOF detected"),           "connection_lost"),
    (psycopg2.OperationalError("certificate verify failed"),                 "tls"),
    (psycopg2.OperationalError("нечто невиданное"),                          "other"),
]


def main():
    failed = 0
    for exc, expected in CASES:
        actual = probe.classify(exc)
        ok = actual == expected
        failed += not ok
        mark = "OK  " if ok else "FAIL"
        note = "" if ok else f"   ожидалось {expected}, получено {actual}"
        print(f"  {mark} {expected:16s} <- {str(exc)[:50]}{note}")

    print()
    if failed:
        print(f"ПРОВАЛОВ: {failed} из {len(CASES)}")
        return 1
    print(f"Все {len(CASES)} причин классифицируются верно.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
