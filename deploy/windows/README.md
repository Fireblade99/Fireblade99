# Установка на Windows Server

Всё ставится одним скриптом. Он делает следующее:
- создаёт `C:\qgw` с папками `app`, `venv`, `data`, `logs` и `secrets`;
- ставит Python-пакеты через Nexus;
- ставит PostgreSQL на эту же ноду (установщиком или распаковкой архива), создаёт базу `qlik_gateway` и пользователя `qgw`;
- генерирует `.env` со случайными паролями;
- регистрирует две задачи в Планировщике: `QlikGateway-API` и `QlikGateway-Worker`. Они запускаются от SYSTEM при старте Windows и перезапускаются при падении;
- открывает порт 8080 в брандмауэре Windows.

## Что нужно заранее

Выберите вариант в зависимости от того, разрешены ли на сервере установщики.

### Вариант А: установщики запрещены политикой (ошибка `0x80070659`)

Ничего не устанавливается, всё распаковывается из архивов в `C:\qgw`. Скачайте на свой ПК и перенесите на ноду:

| Что | Откуда | Файл |
|---|---|---|
| Python (переносимый) | https://www.nuget.org/packages/python/3.12.10 → **Download package** (или nuget-прокси в Nexus) | `python.3.12.10.nupkg` |
| PostgreSQL (переносимый) | https://www.enterprisedb.com/download-postgresql-binaries → Windows x86-64, версия 16 | `postgresql-16.x-windows-x64-binaries.zip` |
| Проект | архив ветки из GitHub | распаковать, например, в `C:\distr\qlik-gateway` |

PowerShell от администратора:

```powershell
cd C:\distr\qlik-gateway
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 `
    -IndexUrl https://<nexus>/repository/<pypi-proxy>/simple `
    -PythonPackage C:\distr\python.3.12.10.nupkg `
    -PgZip C:\distr\postgresql-16.x-windows-x64-binaries.zip `
    -PgDataDir D:\pgdata
```

- Postgres регистрируется как служба Windows `qgw-postgresql` и слушает только `localhost:5432`.
- Если `initdb` ругается на `VCRUNTIME140.dll`, на сервере нет Visual C++ Redistributable 2015–2022. Попросите админов его поставить: это стандартный пакет, обычно он уже есть.

### Вариант Б: установщики разрешены

Поставьте Python 3.10+ с галочкой *Install for all users*, скачайте EDB-установщик PostgreSQL и выполните:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 `
    -IndexUrl https://<nexus>/repository/<pypi-proxy>/simple `
    -PgInstaller C:\distr\postgresql-16.x-windows-x64.exe -PgDataDir D:\pgdata
```

### Общее

- `-PgDataDir` — папка для данных базы; укажите её на большом диске.
- Если сертификат Nexus не доверенный, добавьте `-TrustedHost <nexus-host>`.
- В конце скрипт напечатает **пароль admin для UI** и проверит `/healthz`. После этого откройте `http://<нода>:8080/ui/`.
- Пароль суперпользователя Postgres сохраняется в `C:\qgw\secrets\postgres_superuser.txt`. Доступ к этой папке есть только у Administrators и SYSTEM.

## Повседневные действия

| Что | Как |
|---|---|
| Поправить настройки | отредактировать `C:\qgw\.env`, затем выполнить `C:\qgw\restart.cmd` от администратора |
| Логи | `C:\qgw\logs\api.log`, `C:\qgw\logs\worker.log` |
| Перезапустить | `C:\qgw\restart.cmd` от администратора |
| Сменить пароль admin | `C:\qgw\manage.cmd create-admin admin` |
| Статус задач | `Get-ScheduledTask QlikGateway-*` или Планировщик заданий |
| Обновить версию | распаковать новый архив и заново запустить `install.ps1` с теми же параметрами: `.env` и база сохранятся, пароль пользователя БД `qgw` перегенерируется и пропишется в `.env` |
| Удалить | `deploy\windows\uninstall.ps1`: задачи и правило брандмауэра удаляются, данные остаются |

## Переход на настоящий Qlik

1. Сгенерируйте ключ JWT. Если на ноде нет `openssl`, подойдёт Git for Windows (в нём есть `openssl.exe`), либо сгенерируйте ключ на любой Linux-машине. Файл `qlik_jwt_private.pem` положите в `C:\qgw\secrets\`, а `qlik_jwt_public.crt` передайте админу Qlik. Порядок настройки для админа — в `docs/qlik-setup.md`.
2. В `C:\qgw\.env` пропишите:
   ```
   QGW_QLIK_MODE=jwt
   QGW_QLIK_BASE_URL=https://<qlik>/airflowgw
   QGW_QLIK_JWT_USER_DIRECTORY=<ваш домен>
   ```
   Если сертификат Qlik выпущен корпоративным CA, укажите `QGW_QLIK_VERIFY_SSL=C:/qgw/secrets/corp-ca.pem`.
3. Выполните `restart.cmd`, затем в UI откройте «Задачи Qlik» и нажмите «Синхронизировать».
