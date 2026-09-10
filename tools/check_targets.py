#!/usr/bin/env python3
"""
Сверяет РЕАЛЬНЫЕ эндпоинты с тем, что требуют правила алертов.

Зачем: имена метрик в postgres_exporter менялись от версии к версии.
Экспортёр на хосте БД ставили не мы и версию его не выбирали, поэтому
перед тем как поверить алертам, надо убедиться, что метрики, на которые
они ссылаются, там действительно есть.

Метрика, которой нет, не роняет Prometheus — правило просто молча
никогда не срабатывает. Это худший вид поломки мониторинга, и ловится
он только такой проверкой.

    python3 tools/check_targets.py \\
        --postgres-exporter http://db-host:9187/metrics \\
        --node-exporter     http://db-host:9100/metrics \\
        --probe             http://127.0.0.1:9899/metrics

Выход 0 — все нужные метрики на месте, 1 — чего-то не хватает.
"""
import argparse
import re
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:                                         # pragma: no cover
    sys.exit("нужен pyyaml: pip install pyyaml")

RULES_DIR = Path(__file__).resolve().parent.parent / "prometheus" / "rules"

# Всё, что синтаксис PromQL, а не имя метрики.
PROMQL_BUILTINS = {
    "rate", "irate", "increase", "delta", "idelta", "deriv", "predict_linear",
    "sum", "avg", "min", "max", "count", "count_values", "group",
    "stddev", "stdvar", "topk", "bottomk", "quantile", "histogram_quantile",
    "abs", "ceil", "floor", "round", "exp", "ln", "log2", "log10", "sqrt",
    "clamp", "clamp_max", "clamp_min", "time", "timestamp", "absent",
    "absent_over_time", "changes", "resets", "label_replace", "label_join",
    "vector", "scalar", "sort", "sort_desc", "hour", "minute", "month", "year",
    "avg_over_time", "min_over_time", "max_over_time", "sum_over_time",
    "count_over_time", "quantile_over_time", "stddev_over_time",
    "stdvar_over_time", "last_over_time", "present_over_time",
    "by", "on", "ignoring", "group_left", "group_right", "without",
    "and", "or", "unless", "offset", "bool", "le", "inf", "nan",
}

# Какие источники что отдают — чтобы сказать, на какой эндпоинт ругаться.
# Эндпоинт Prometheus перечисляет только те метрики, у которых СЕЙЧАС
# есть серии. Для этих отсутствие — норма, а не поломка: серия
# появится, когда появится само явление.
CONDITIONAL = {
    "pg_replication_lag_seconds":            "нет реплик",
    "pg_replication_slots_active":           "нет слотов репликации",
    "pg_replication_slots_pg_wal_lsn_diff":  "нет слотов репликации",
    "pgprobe_statement_mean_time_ms":        "не установлен pg_stat_statements либо не прогнан sql/02",
    "pg_stat_archiver_failed_count":         "не настроено архивирование WAL",
}

SOURCE_PREFIX = {
    "pg_": "postgres-exporter",
    "node_": "node-exporter",
    "pgprobe_": "pg-probe",
    "up": "prometheus",
}


def source_of(metric):
    if metric == "up":
        return "prometheus"
    for prefix, source in SOURCE_PREFIX.items():
        if prefix != "up" and metric.startswith(prefix):
            return source
    return "?"


# Модификаторы, за которыми в скобках идут имена МЕТОК, а не метрик.
_LABEL_CLAUSE = re.compile(
    r"\b(?:by|on|ignoring|without|group_left|group_right)\s*\([^)]*\)")


def extract_metrics(expr):
    """Имена метрик из PromQL-выражения."""
    found = set()
    # 1. metric{...} — имя стоит перед фигурной скобкой
    for name in re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\{", expr):
        found.add(name)
    # 2. Убираем содержимое матчеров: там имена ЛЕЙБЛОВ, не метрик
    stripped = re.sub(r"\{[^}]*\}", " ", expr)
    # 3. И списки меток у by/on/ignoring/without/group_left/group_right —
    #    иначе pg_instance из by (pg_instance) уедет в «метрики»
    stripped = _LABEL_CLAUSE.sub(" ", stripped)
    # 4. Оставшиеся голые идентификаторы
    for name in re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b", stripped):
        found.add(name)
    return {
        m for m in found
        if m not in PROMQL_BUILTINS
        and not m.isdigit()
        and not re.fullmatch(r"[a-z]", m)      # одиночные буквы из d/h/m/s
    }


def required_metrics():
    """{метрика: {правила, которым она нужна}}, без recording-правил."""
    recorded, needed = set(), defaultdict(set)
    docs = []
    for path in sorted(RULES_DIR.glob("*.yml")):
        docs.append(yaml.safe_load(path.read_text(encoding="utf-8")))
    for doc in docs:
        for group in doc["groups"]:
            for rule in group["rules"]:
                if "record" in rule:
                    recorded.add(rule["record"])
    for doc in docs:
        for group in doc["groups"]:
            for rule in group["rules"]:
                name = rule.get("alert") or rule.get("record")
                for metric in extract_metrics(rule["expr"]):
                    # recording-правила Prometheus вычисляет сам —
                    # их не должно быть на эндпоинтах
                    if metric in recorded or ":" in metric:
                        continue
                    needed[metric].add(name)
    return needed


def fetch(url):
    # Без прокси: цели локальные или в приватной сети.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=15) as resp:
        return resp.read().decode("utf-8", "replace")


def metrics_in(payload):
    names = set()
    for line in payload.splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)", line)
        if match:
            name = match.group(1)
            names.add(name)
            # Гистограммы отдают _bucket/_sum/_count; правила ссылаются
            # на базовое имя через _bucket, его и оставляем как есть.
            for suffix in ("_bucket", "_sum", "_count", "_total"):
                if name.endswith(suffix):
                    names.add(name[: -len(suffix)])
    return names


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--postgres-exporter", metavar="URL")
    ap.add_argument("--node-exporter", metavar="URL")
    ap.add_argument("--probe", metavar="URL")
    args = ap.parse_args()

    endpoints = {
        "postgres-exporter": args.postgres_exporter,
        "node-exporter": args.node_exporter,
        "pg-probe": args.probe,
    }
    if not any(endpoints.values()):
        ap.error("укажи хотя бы один эндпоинт")

    available, checked = set(), set()
    for source, url in endpoints.items():
        if not url:
            continue
        try:
            payload = fetch(url)
        except Exception as exc:                            # noqa: BLE001
            print(f"  ОШИБКА  {source:20s} {url}\n          {exc}")
            continue
        names = metrics_in(payload)
        available |= names
        checked.add(source)
        print(f"  OK      {source:20s} {len(names)} имён метрик")

    print()
    needed = required_metrics()
    missing = defaultdict(list)
    conditional = []
    present = 0
    for metric, rules in sorted(needed.items()):
        source = source_of(metric)
        if source not in checked:
            continue                                        # эндпоинт не проверяли
        if metric in available:
            present += 1
        elif metric in CONDITIONAL:
            conditional.append((metric, CONDITIONAL[metric]))
        else:
            missing[source].append((metric, sorted(rules)))

    print(f"Метрик требуется правилами: {sum(1 for m in needed if source_of(m) in checked)}")
    print(f"Найдено на эндпоинтах:      {present}")

    if conditional:
        print("\nОтсутствуют, но это НОРМАЛЬНО — серия появится вместе с явлением:")
        for metric, why in sorted(conditional):
            print(f"  {metric:38s} {why}")

    if not missing:
        print("\nВсё на месте. Правила будут работать на этих версиях экспортёров.")
        return 0

    print("\nНЕ НАЙДЕНЫ — эти алерты никогда не сработают:\n")
    for source, items in sorted(missing.items()):
        print(f"  [{source}]")
        for metric, rules in items:
            print(f"    {metric}")
            print(f"      нужна для: {', '.join(rules)}")
    print("\nВероятная причина — другая версия экспортёра: имена метрик")
    print("между версиями меняются. Сообщи, какие именно отсутствуют,")
    print("и правила надо будет поправить под твою версию.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
