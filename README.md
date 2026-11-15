# Транскоминжиниринг: помощник руководителя проекта

Ветка `feat/portable-windows` позволяет установить локальный пилот на Windows без диска D. Для компьютера Ярослава предусмотрен явный режим `-Portable`. Данные, зависимости, Node, загрузки, кэши и временные файлы этого режима находятся внутри его копии проекта. На основном компьютере владельца сохраняется прежний режим D без этого параметра.

## Установка на C

Нужны Git for Windows и Python 3.11. [Официальная инструкция установки Python на Windows](https://docs.python.org/3/using/windows.html). Если используется новый Python install manager, версия 3.11 устанавливается командой `py install 3.11`; уже установленную 3.11 переустанавливать не нужно. После установки откройте новое окно PowerShell. Node 26.13.1 скрипт скачает сам с nodejs.org и проверит закрепленный SHA256. Глобальные настройки и программы скрипт не меняет.

Для новой копии выполните в PowerShell:

```powershell
git clone --branch feat/portable-windows https://github.com/Skoro0977/transcom-assistant.git C:\Projects\transcom-assistant-portable
Set-Location 'C:\Projects\transcom-assistant-portable'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Portable
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1 -Portable
```

Открыть [http://127.0.0.1:8000](http://127.0.0.1:8000). Терминал запуска оставить открытым, Ctrl+C останавливает приложение. Параметр ExecutionPolicy действует только для запущенного процесса. Скрипт завершает работу при ошибке и оставляет сообщение в этом терминале.

Логины и случайные индивидуальные пароли создаются при первом запуске в `data/local/accounts.csv` внутри этой копии. Создаются 10 admin и 5 user. Повторный запуск сохраняет учетные записи. Файл закрыт локальными правами и исключен из Git. База, исходные документы, ключ владельца и демонстрационный проект из его рабочего пилота в клоне отсутствуют.

Если репозиторий уже склонирован, сохраните свои изменения и перейдите в его папку, затем:

```powershell
git fetch origin
git switch feat/portable-windows
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Portable
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1 -Portable
```

## Разработка и T25

[T25, задача №1](https://github.com/Skoro0977/transcom-assistant/issues/1) закреплена за Ярославом, GitHub `kozlovskkiy`. Эта ветка подготавливает его окружение. Сам дефект контекста T25 здесь не исправляется.

Для работы над T25 из этой ветки:

```powershell
git switch -c fix/t25-chat-context
```

Запускайте проверки из корня в PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
. .\scripts\dev-env.ps1 -Portable
& .\.venv\Scripts\python.exe -B -m ruff check apps/api scripts tests migrations/env.py
& .\.venv\Scripts\python.exe -B -m pytest tests --basetemp (Join-Path $env:TEMP 'pytest-manual')
npm.cmd --prefix apps/web run build
```

После коммита отправить рабочую ветку:

```powershell
git push -u origin fix/t25-chat-context
```

Создавайте PR из рабочей ветки в `feat/portable-windows`, пока переносимый режим не объединен в main. Не редактируйте общую папку на двух компьютерах.

## GigaChat и рабочие данные

Установка создает `.env` из публичного `.env.example`, только если файл еще отсутствует. Для GigaChat задайте собственный `GIGACHAT_AUTH_KEY` и подходящий `GIGACHAT_SCOPE` в локальной `.env`, затем перезапустите приложение. Без ключа доступны ручная работа и локальный поиск. Передавать ключ в Git, чат или PR нельзя. Массовая индексация по умолчанию выключена.

Пилотный архив владельца не включен в Git. Для разработки используйте искусственные документы. [Описание T25, воспроизведение и установка](docs/PORTABLE_WINDOWS.md).

## Документация

- [Архитектура](docs/ADR.md), [контракт](docs/CONTRACT.md).
- [GigaChat](docs/GIGACHAT.md), [источники](docs/SOURCES.md), [интеграции](docs/INTEGRATIONS.md).
- [Резервные копии](docs/BACKUP.md), [приемка](docs/ACCEPTANCE.md).
- [Совместная работа](docs/COLLABORATION.md), [задачи](TASKS.md), [журнал](docs/WORK_LOG.md).

Для основного компьютера на D прежние команды setup/start/dev-env без `-Portable` сохраняют строгую политику D. Переносимый режим включается параметром скрипта и передается backend через переменную процесса TRANSCOM_PORTABLE=true.
