# Настройка стороны Qlik Sense (QSEoW)

Цель: в Qlik заходит только шлюз, под одной сервисной учёткой, через отдельный Virtual Proxy с JWT, а reload'ы из Airflow уходят на выделенные ноды (пул из 3 шедулеров).

> Названия пунктов QMC даны по актуальным версиях QSEoW. Шаги 4 и 5 (балансировка) сверьте с вашей версией и с вендором: механизм выбора ноды для reload отличается от релиза к релизу.

## 1. Ключ и сертификат для JWT

Выполняется на хосте шлюза. Приватный ключ хранится только там.

```bash
mkdir -p secrets && cd secrets
openssl req -x509 -nodes -days 730 -newkey rsa:4096 \
  -keyout qlik_jwt_private.pem -out qlik_jwt_public.crt \
  -subj "/CN=qlik-gateway"
chmod 600 qlik_jwt_private.pem
```

`qlik_jwt_public.crt` понадобится для виртуального прокси. Чтобы перевыпустить ключ (например, при компрометации), сгенерируйте новую пару и замените сертификат в прокси. Старые JWT после этого перестанут приниматься.

## 2. Virtual Proxy с JWT

QMC → Virtual proxies → Create new:

| Поле | Значение |
|---|---|
| Description | Qlik Gateway (Airflow и платформы) |
| Prefix | `airflowgw` |
| Session cookie header name | `X-Qlik-Session-airflowgw` |
| Authentication method | **JWT** |
| JWT certificate | содержимое `qlik_jwt_public.crt` |
| JWT attribute for user ID | `userId` |
| JWT attribute for user directory | `userDirectory` |
| Load balancing nodes | только engine выделенных нод |
| Host allow list | FQDN, по которому шлюз обращается к Qlik |

Затем **Associated items → Proxies** → привязать к нужному proxy-сервису (обычно central) и применить.

В `.env` шлюза:

```
QGW_QLIK_MODE=jwt
QGW_QLIK_BASE_URL=https://qlik.company.local/airflowgw
QGW_QLIK_JWT_PRIVATE_KEY_PATH=/app/secrets/qlik_jwt_private.pem
QGW_QLIK_JWT_USER_ID=svc_qlik_gateway
QGW_QLIK_JWT_USER_DIRECTORY=CORP
```

Проверка: `GET https://qlik.company.local/airflowgw/qrs/about?xrfkey=0123456789abcdef` с заголовками `X-Qlik-Xrfkey: 0123456789abcdef` и `Authorization: Bearer <jwt>` должен вернуть 200. Шлюз сам делает это в «Задачи Qlik» → «Синхронизировать».

## 3. Права сервисной учётки (минимально необходимые)

Пользователь `CORP\svc_qlik_gateway` создаётся при первом входе по JWT. Роль RootAdmin ему **не** выдавайте. Нужно отдельное правило безопасности (QMC → Security rules → Create new):

* **Resource filter**: `ReloadTask_*, ExecutionResult_*, ExecutionSession_*, App_*`
* **Actions**: Read, Update (Update нужен для старта и остановки задачи)
* **Conditions**:
  ```
  user.name = "svc_qlik_gateway" and user.userDirectory = "CORP"
  and ((resource.resourcetype = "App" and resource.@Source = "Airflow")
       or (resource.resourcetype = "ReloadTask" and resource.app.@Source = "Airflow")
       or resource.resourcetype like "Execution*")
  ```
* **Context**: Only in QMC. Если скачивание лога скрипта отдаёт 403, поставьте Both.

Так шлюз физически не сможет запускать задачи, не помеченные `Source=Airflow`, даже при ошибке в конфигурации шлюза.

## 4. Custom property и выделенные ноды

1. QMC → Custom properties → Create:
   * `Source`, значения `Airflow`, resource types: **Apps** (при желании также Reload tasks);
   * `NodePurpose`, значение `Airflow`, resource type: **Nodes**.
2. Поставьте `Source=Airflow` на всех приложениях, которые перезагружаются через API.
3. Поставьте `NodePurpose=Airflow` на 3 ноды, выделенные под эти reload (на них должны быть Scheduler в роли worker и Engine).

## 5. Балансировка reload на выделенные ноды

Совет вендора: «задачи, запускаемые через API, отметить custom property `Source=Airflow` и балансировать их на выделенный узел». QMC → Security rules → Create new → **Load balancing**:

* **Resource filter**: `App_*`
* **Actions**: Load balancing
* **Conditions**:
  ```
  (resource.@Source = "Airflow" and node.@NodePurpose = "Airflow")
  or (resource.@Source != "Airflow" and node.@NodePurpose != "Airflow")
  ```

Затем отключите или ограничьте дефолтное правило `ResourcesOnNonCentralNodes` (или добавьте в него условие `resource.@Source != "Airflow"`). Иначе приложения с `Source=Airflow` по-прежнему могут уйти на общие ноды.

Проверка: запустите задачу через шлюз и откройте карточку запуска в UI. Поле «Нода» (`executingNodeName` из QRS) должно показывать одну из трёх выделенных нод.

## 6. Закрыть прямой доступ Airflow к Qlik

Письмо вендора: в QSEoW **нельзя отозвать выданный клиентский сертификат**. Поэтому:

1. **Firewall**: запретить хостам Airflow обращаться к Qlik на 4242 (QRS), 4243, 443/80 (proxy), 4747 (engine). К Qlik ходит только хост шлюза.
2. Удалить клиентский сертификат Qlik из хранилища или файлов на стороне Airflow.
3. Если сертификат мог утечь, перегенерировать корневой сертификат Qlik на central node и распространить его по кластеру (процедура вендора). Это инвалидирует **все** ранее экспортированные клиентские сертификаты.
4. Отключить системные учётки, которыми пользовался Airflow.

После этого единственный путь из Airflow в Qlik идёт через шлюз, а доступ отзывается кнопкой «Заблокировать» в UI.

## 7. Мониторинг ресурсов нод (опционально)

Панель «Ресурсы нод» показывает CPU, RAM и число загруженных приложений по `engine/healthcheck`:

```
QGW_NODE_HEALTH_URLS=https://qlik-node1.company.local/airflowgw/engine/healthcheck,https://qlik-node2.company.local/airflowgw/engine/healthcheck,https://qlik-node3.company.local/airflowgw/engine/healthcheck
```

Нужно, чтобы виртуальный прокси балансировал на engine этих нод, а у сервисной учётки было право на healthcheck.

> Qlik не отдаёт потребление CPU и RAM отдельной reload-задачи. Шлюз показывает то, что можно получить: длительность, ноду, время ожидания в очереди и загрузку нод в момент выполнения. Этого хватает, чтобы найти тяжёлые задачи и источник нагрузки.

## 8. Где смотреть логи на стороне Qlik

Отдельных журналов вызовов API в Qlik нет, поэтому основной журнал ведёт шлюз (UI → «Журнал», там же вызовы шлюза в Qlik). Для разборов на стороне Qlik (по письму вендора):

* **Repository** (`C:\ProgramData\Qlik\Sense\Log\Repository\Trace`) — вызовы QRS от `svc_qlik_gateway`;
* **Scheduler** (`…\Log\Scheduler\Trace`) — старт и выполнение задачи; ищите по `Execution ID` из карточки запуска;
* **Proxy** (`…\Log\Proxy\Trace`) — вход по префиксу `airflowgw`;
* **Script** (`…\Log\Script`) — детали reload, доступны и из UI шлюза («Лог скрипта»).
