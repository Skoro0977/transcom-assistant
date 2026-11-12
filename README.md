# Контур проекта

Локальная система сопровождения проектирования Транскоминжиниринг. Исходники, зависимости, данные и кэши этого проекта размещаются на D.

Подготовка окружения в PowerShell:

```powershell
. 'D:\CodexData\Projects\transcom-assistant\scripts\dev-env.ps1'
```

После установки зависимостей backend запускается из корня проекта:

```powershell
.\.venv\Scripts\python.exe -B -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

Frontend в отдельном терминале после подключения dev-env.ps1:

```powershell
Set-Location 'D:\CodexData\Projects\transcom-assistant\apps\web'
npm run dev
```

Зависимости фиксируются в requirements.lock и apps/web/package-lock.json. Фактический состав и ограничения см. docs/ADR.md, последовательность и проверки в TASKS.md и docs/WORK_LOG.md. Реальные материалы и секреты в data и .env исключены из Git.

В текущей среде используется переносимая Node 26.13.1 LTS в D:\CodexData\Runtimes\node\node-v26.13.1-win-x64, архив с официального nodejs.org проверен по SHA-256. Системная Node на C не заменяется. На другом компьютере подготовьте ту же переносимую среду на D. Для воспроизводимой установки выполните scripts/setup.ps1; для запуска backend scripts/start.ps1. Скрипты не устанавливают программы глобально и не покупают услуги.
