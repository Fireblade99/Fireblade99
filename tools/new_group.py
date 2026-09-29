#!/usr/bin/env python3
"""
Создаёт комплект файлов для новой группы хостов.

    python new_group.py prod-etl              (Windows)
    python3 tools/new_group.py prod-etl       (Linux/macOS)

Папка с файлами-образцами ищется сама: текущая, ./grafana, папка
скрипта. Не нашлась — укажи явно:

    python new_group.py prod-etl --dir C:\\путь\\к\\файлам

На выходе три файла в grafana/:
    overview-<группа>.json    сводка по всем хостам группы
    detailed-<группа>.json    разбор одного хоста и одной базы
    alerts-<группа>.yml       четырнадцать правил с фильтром по группе

Дальше остаётся дописать хосты в prometheus.yml с той же меткой
pg_group и импортировать файлы в Grafana.

Почему генератор, а не правка руками: группа упоминается в дашборде
в трёх местах (переменная, её текущее значение, заголовок) и в алертах
двадцать два раза. Пропустить одно — получить панели, которые молча смотрят на
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

TEMPLATE_GROUP = "qse"          # с какой группы снимается образец


def find_dir(explicit=None):
    """
    Ищет папку с файлами-образцами.

    Раскладка у всех разная: у кого-то это клон репозитория, у кого-то
    просто папка со скачанными файлами. Поэтому не гадаем, а проверяем
    несколько очевидных мест и берём первое, где образец реально лежит.
    """
    marker = f"overview-{TEMPLATE_GROUP}.json"
    here = Path.cwd()
    mine = Path(__file__).resolve().parent

    if explicit:
        d = Path(explicit).expanduser().resolve()
        if not (d / marker).exists():
            die(f"в папке {d} нет файла {marker}")
        return d

    candidates = [
        here,                    # запустили прямо в папке с файлами
        here / "grafana",        # запустили из корня репозитория
        mine,                    # скрипт лежит рядом с файлами
        mine / "grafana",
        mine.parent / "grafana", # раскладка репозитория: tools/ рядом с grafana/
    ]
    seen = []
    for d in candidates:
        if d in seen:
            continue
        seen.append(d)
        if (d / marker).exists():
            return d

    die(f"не нашёл {marker}. Искал в:\n       " +
        "\n       ".join(str(d) for d in seen) +
        "\n       Укажи папку явно:  new_group.py <группа> --dir <путь>")

# Имя группы попадает в метки Prometheus и в имена файлов.
VALID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")


def die(msg):
    print(f"ОШИБКА: {msg}", file=sys.stderr)
    sys.exit(1)


def check_templates(GRAFANA):
    """
    Проверяет ВСЕ образцы до того, как что-либо создано.

    Раньше проверка шла по ходу дела, и нехватка последнего образца
    оставляла половину комплекта на диске — а следующий запуск упирался
    в защиту «файлы этой группы уже есть» и требовал ручной уборки.
    """
    need = [f"overview-{TEMPLATE_GROUP}.json",
            f"detailed-{TEMPLATE_GROUP}.json",
            "alerts-prometheus-format.yml"]
    missing = [n for n in need if not (GRAFANA / n).exists()]
    if missing:
        die("в папке " + str(GRAFANA) + " не хватает образцов:\n       " +
            "\n       ".join(missing) +
            "\n\n       Нужны все три: два дашборда и шаблон алертов.\n"
            "       Ничего не создано.")


def make_dashboard(kind, group, GRAFANA):
    src = GRAFANA / f"{kind}-{TEMPLATE_GROUP}.json"
    if not src.exists():
        die(f"нет образца {src}")
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


def make_alerts(group, GRAFANA):
    src = GRAFANA / "alerts-prometheus-format.yml"
    if not src.exists():
        die(f"нет образца {src}")
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
    args = sys.argv[1:]
    explicit = None
    if "--dir" in args:
        i = args.index("--dir")
        if i + 1 >= len(args):
            die("после --dir нужен путь")
        explicit = args[i + 1]
        del args[i:i + 2]
    if len(args) != 1:
        sys.exit(__doc__)
    group = args[0]

    GRAFANA = find_dir(explicit)
    print(f"папка с файлами: {GRAFANA}")
    check_templates(GRAFANA)

    if not VALID.match(group):
        die("имя группы: латиница, цифры, дефис и подчёркивание, до 63 символов.\n"
            "       Оно идёт меткой в Prometheus и в имена файлов.")
    if group == TEMPLATE_GROUP:
        die(f"группа {group} — это образец, перезаписывать её нельзя")

    existing = list(GRAFANA.glob(f"*-{group}.*"))
    if existing:
        die("файлы для этой группы уже есть:\n       " +
            "\n       ".join(p.name for p in existing) +
            "\n       Удали их, если хочешь пересоздать.")

    print(f"группа: {group}   (образец: {TEMPLATE_GROUP})\n")
    for kind in ("overview", "detailed"):
        out, title, panels = make_dashboard(kind, group, GRAFANA)
        print(f"  {out.name}")
        print(f"      «{title}», панелей {panels} — {check(out)}")

    out, n = make_alerts(group, GRAFANA)
    print(f"  {out.name}")
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
