# Установка на Windows Server

Всё ставится одним скриптом. Он делает следующее:
- создаёт `C:\qgw` с папками `app`, `venv`, `data`, `logs` и `secrets`;
- ставит Python-пакеты через Nexus;
- ставит PostgreSQL на эту же ноду, создаёт базу `qlik_gateway` и пользователя `qgw`;
- генерирует `.env` со случайными паролями;
- регистрирует две задачи в Планировщике: `QlikGateway-API` и `QlikGateway-Worker`. Они запускаются от SYSTEM при старте Windows и перезапускаются при падении;
- открывает порт 8080 в брандмауэре Windows.

## Что нужно заранее

1. **Python 3.10+.** Ставится установщиком с python.org (или из Nexus raw) с галочкой *Install for all users*.
2. **Дистрибутив PostgreSQL для Windows от EDB**, например `postgresql-16.x-windows-x64.exe`. Скачайте его с enterprisedb.com или возьмите из Nexus и положите на ноду.
3. **Адрес PyPI-прокси в Nexus**, например `https://nexus.company.local/repository/pypi-proxy/simple`.
4. **Проект**, распакованный в любую папку, например `C:\distr\qlik-gateway`.

## Установка

PowerShell от администратора:

```powershell
cd C:\distr\qlik-gateway
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 `
    -IndexUrl https://nexus.company.local/repository/pypi-proxy/simple `
    -PgInstaller C:\distr\postgresql-16.4-1-windows-x64.exe `
    -PgDataDir D:\pgdata
```

`-PgDataDir` — папка для данных базы; укажите её на большом диске.

Если сертификат Nexus не доверенный, добавьте `-TrustedHost nexus.company.local`.

В конце скрипт напечатает **пароль admin для UI** и проверит `/healthz`. После этого откройте `http://<нода>:8080/ui/`.

Пароль суперпользователя PostgreSQL сохраняется в `C:\qgw\secrets\postgres_superuser.txt`. Доступ к этой папке есть только у Administrators и SYSTEM.

## Повседневные действия

| Что | Как |
|---|---|
| Поправить настройки | отредактировать `C:\qgw\.env`, затем выполнить `C:\qgw\restart.cmd` от администратора |
| Логи | `C:\qgw\logs\api.log`, `C:\qgw\logs\worker.log` |
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
