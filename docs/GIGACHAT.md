# Настройка GigaChat

Секрет GIGACHAT_AUTH_KEY хранится только в .env в корне проекта либо в переменной окружения сервера. Значение является готовым Base64 Authorization Key из кабинета GigaChat. Backend читает его через SecretStr, frontend его не получает. Ключ и токены не записываются в протокол проверки и Git. После изменения .env перезапустите приложение.

Публичные значения конфигурации приведены в .env.example. Scope должен соответствовать проекту провайдера. Для собственного CA используйте GIGACHAT_CA_FILE с абсолютным путем на D. Проверка TLS всегда включена, системное хранилище сертификатов не изменяется. При недоступности API исходные документы, ручная работа и поиск по словам сохраняются.

Отдельная реальная проверка из корня после dev-env: `.venv\Scripts\python.exe -B -m scripts.check_gigachat`. Она получает OAuth-токен, список моделей, короткий ответ на искусственный текст и один эмбеддинг. Безопасный результат сохраняется в data/local/gigachat-check.json. Ошибка провайдера не выдается за успешный анализ.

Используются официальные адреса и протоколы:

- https://developers.sber.ru/docs/ru/gigachat/api/reference/rest/gigachat-api
- https://developers.sber.ru/docs/ru/gigachat/api/reference/rest/post-chat
- https://developers.sber.ru/docs/ru/gigachat/api/reference/rest/post-embeddings
- https://developers.sber.ru/docs/ru/gigachat/certificates
- https://developers.sber.ru/docs/ru/gigachat/guides/structured-output
- https://developers.sber.ru/docs/ru/gigachat/models/embeddings-giga-r

Для смыслового индекса используется EmbeddingsGigaR: официальная спецификация указывает4096 токенов и2560 координат. Тексты фрагментов ограничены1400 символами и3500 UTF-8 байтами. Векторы нормализуются и сохраняются float32 в SQLite вместе с моделью и размерностью. Исходный корпус не уходит в сеть при локальном разбиении. Массовая облачная индексация отдельно включается GIGACHAT_BULK_INDEXING_ENABLED=true после разрешения на передачу корпуса, по умолчанию false. Ключ и готовые векторы остаются на D.
