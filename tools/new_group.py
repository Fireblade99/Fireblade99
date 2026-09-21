#!/usr/bin/env python3
"""
Создаёт комплект файлов для новой группы хостов.

    python3 tools/new_group.py prod-etl

На выходе три файла в grafana/:
    overview-<группа>.json    сводка по всем хостам группы
    detailed-<группа>.json    разбор одного хоста и одной базы
    alerts-<группа>.yml       пять правил с фильтром по группе

Дальше остаётся дописать хосты в prometheus.yml с той же меткой
pg_group и импортировать файлы в Grafana.

Почему генератор, а не правка руками: группа упоминается в дашборде
в трёх местах (переменная, её текущее значение, заголовок) и в алертах
семь раз. Пропустить одно — получить панели, которые молча смотрят на
чужую группу или в пустоту.

Эталоном служат файлы группы qse: правки в них подхватятся
автоматически при создании следующей группы.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAFANA = ROOT / "grafana"
TEMPLATE_GROUP = "qse"          # с какой группы снимается образец

# Имя группы попадает в метки Prometheus и в имена файлов.
VALID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")


def die(msg):
    print(f"ОШИБКА: {msg}", file=sys.stderr)
    sys.exit(1)


def make_dashboard(kind, group):
    src = GRAFANA / f"{kind}-{TEMPLATE_GROUP}.json"
    if not src.exists():
        die(f"нет образца {src.relative_to(ROOT)}")
    d = json.loads(src.read_text(encoding="utf-8"))

    # 1. заголовок: имя группы в нём обязательно — в списке ссылок между
    #    дашбордами Grafana показывает сохранённое имя и переменные в нём
    #    не раскрывает, иначе копии не различить
    d["title"] = re.sub(r"\(.*\)$", f"({group})", d["title"]).strip()
    if not d["title"].endswith(f"({group})"):
        d["title"] = f"{d['title']} ({group})"

    # 2. переменная pg_group: и запрос, и текущее значение
    found = False
    for v in d["templating"]["list"]:
        if v["name"] == "pg_group":
            v["query"] = group
            v["current"] = {"text": group, "value": group}
            found = True
    if not found:
        die(f"в образце {src.name} нет переменной pg_group")

    out = GRAFANA / f"{kind}-{group}.json"
    out.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    panels = len([p for p in d["panels"] if p["type"] != "row"])
    return out, d["title"], panels


def make_alerts(group):
    src = GRAFANA / "alerts-prometheus-format.yml"
    if not src.exists():
        die(f"нет образца {src.relative_to(ROOT)}")
    text = src.read_text(encoding="utf-8")
    if "ГРУППА" not in text:
        die(f"в {src.name} нет плейсхолдера ГРУППА — образец испорчен")
    out = GRAFANA / f"alerts-{group}.yml"
    out.write_text(text.replace("ГРУППА", group), encoding="utf-8")
    return out, text.count("ГРУППА")


def check(path):
    """Проверяет результат, а не только факт записи."""
    if path.suffix == ".json":
        json.loads(path.read_text(encoding="utf-8"))
        return "JSON валиден"
    if shutil.which("promtool"):
        r = subprocess.run(["promtool", "check", "rules", str(path)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            die(f"promtool забраковал {path.name}:\n{r.stdout}{r.stderr}")
        return r.stdout.strip().splitlines()[-1].strip()
    return "promtool не найден, проверка пропущена"


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    group = sys.argv[1]

    if not VALID.match(group):
        die("имя группы: латиница, цифры, дефис и подчёркивание, до 63 символов.\n"
            "       Оно идёт меткой в Prometheus и в имена файлов.")
    if group == TEMPLATE_GROUP:
        die(f"группа {group} — это образец, перезаписывать её нельзя")

    existing = list(GRAFANA.glob(f"*-{group}.*"))
    if existing:
        die("файлы для этой группы уже есть:\n       " +
            "\n       ".join(str(p.relative_to(ROOT)) for p in existing) +
            "\n       Удали их, если хочешь пересоздать.")

    print(f"группа: {group}   (образец: {TEMPLATE_GROUP})\n")
    for kind in ("overview", "detailed"):
        out, title, panels = make_dashboard(kind, group)
        print(f"  {out.relative_to(ROOT)}")
        print(f"      «{title}», панелей {panels} — {check(out)}")

    out, n = make_alerts(group)
    print(f"  {out.relative_to(ROOT)}")
    print(f"      фильтр pg_group=\"{group}\" в {n} местах — {check(out)}")

    print(f"""
Дальше:

  1. prometheus.yml — дописать хосты в ОБА job, с меткой pg_group: "{group}"

       - targets: ["хост:9187", ...]        # в postgres-exporter
         labels:
           pg_group: "{group}"

       - targets: ["хост:9100", ...]        # в node-exporter
         labels:
           pg_group: "{group}"

     promtool check config, перезапуск, проверить /targets

  2. Grafana — импортировать оба дашборда, папку выбрать явно

  3. Grafana — импортировать alerts-{group}.yml в ОТДЕЛЬНУЮ папку,
     снять паузу, проставить контакт и тайминги у каждого правила
""")


if __name__ == "__main__":
    main()
