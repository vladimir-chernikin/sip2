# Контейнер `backend`: логика управления звонком (ARI-клиент)

## Роль контейнера

Контейнер `backend` — это Python-приложение на FastAPI, которое:
- Подключается к Asterisk через ARI (Asterisk REST Interface)
- Управляет жизненным циклом звонков
- Создаёт bridge и externalMedia каналы
- Очищает ресурсы при завершении звонков

## Технологии внутри контейнера

### 1. FastAPI
- **Роль**: HTTP-фреймворк для Python
- **Порт**: `9000` (проброшен на хост)
- **Endpoints**:
  - `GET /health` — проверка работоспособности

### 2. WebSockets (websockets)
- **Роль**: Подключение к ARI WebSocket для получения событий
- **URL**: `ws://asterisk:8088/ari/events?app=ai_app`
- **События**:
  - `StasisStart` — начало звонка
  - `StasisEnd` — завершение звонка
  - `ChannelDestroyed` — уничтожение канала

### 3. HTTPX (httpx)
- **Роль**: Асинхронный HTTP-клиент для ARI REST API
- **Использование**: 
  - Создание bridge (`POST /bridges`)
  - Создание externalMedia (`POST /channels/externalMedia`)
  - Управление каналами (`POST /channels/{id}/answer`, `DELETE /channels/{id}`)
  - Удаление bridge (`DELETE /bridges/{id}`)

### 4. Pydantic Settings
- **Роль**: Загрузка настроек из переменных окружения
- **Файл**: `backend/app/settings.py`

## Структура файлов контейнера

```
backend/
├── Dockerfile                    # Python образ
├── requirements.txt             # Зависимости (FastAPI, httpx, websockets, pydantic)
└── app/
    ├── main.py                  # FastAPI приложение, lifespan, /health
    ├── ari_client.py            # ARI клиент (AriClient, AriWsHandler)
    └── settings.py              # Настройки из env (ARI URL, credentials)
```

## Основные компоненты

### 1. `main.py` — точка входа
- Создаёт FastAPI приложение
- Инициализирует ARI-клиент и WebSocket handler в `lifespan`
- Предоставляет `/health` endpoint

### 2. `ari_client.py` — ARI-клиент

#### Класс `AriClient`
- **Роль**: Синхронные/асинхронные вызовы к ARI REST API
- **Методы**:
  - `create_bridge()` — создаёт mixing bridge
  - `create_external_media()` — создаёт externalMedia канал
  - `answer_channel()` — активирует канал (переводит в Up)
  - `add_channel_to_bridge()` — добавляет канал в bridge
  - `get_channel_details()` — получает информацию о канале
  - `get_bridge_channels()` — получает список каналов в bridge
  - `hangup_channel()` — безопасно завершает канал
  - `delete_bridge()` — удаляет bridge

#### Класс `AriWsHandler`
- **Роль**: Обработка событий от ARI WebSocket
- **Методы**:
  - `run()` — основной цикл подключения к WebSocket
  - `handle_stasis_start()` — обработка начала звонка
  - `_cleanup_by_channel()` — очистка ресурсов при завершении

### 3. `settings.py` — настройки
- Загружает из переменных окружения:
  - `ARI_BASE_URL` — базовый URL ARI
  - `ARI_USER` — пользователь ARI
  - `ARI_PASSWORD` — пароль ARI
  - `ARI_APP` — имя ARI-приложения

## Переменные окружения

```env
ARI_BASE_URL=http://asterisk:8088/ari
ARI_USER=admin
ARI_PASSWORD=admin123
ARI_APP=ai_app
AUDIOSOCKET_HOST=audiosocket
AUDIOSOCKET_PORT=7575
ARI_HTTP_TIMEOUT=10  # таймаут HTTP-запросов (секунды)
```

## Жизненный цикл звонка

### 1. При событии `StasisStart`
1. Backend получает событие через ARI WebSocket
2. Извлекает `channel_id` из события
3. Генерирует `session_uuid` (UUID для сессии)
4. Создаёт mixing bridge через `POST /bridges`
5. Добавляет канал звонящего в bridge
6. Создаёт externalMedia канал через `POST /channels/externalMedia`
   - Указывает `external_host=audiosocket:7575`
   - Передаёт `session_uuid` в поле `data`
7. Активирует externalMedia через `POST /channels/{id}/answer`
8. Добавляет externalMedia в bridge
9. Сохраняет маппинг: `channel_id → bridge_id`, `session_uuid → (channel_id, external_channel_id)`

### 2. При событии `StasisEnd` или `ChannelDestroyed`
1. Backend получает событие
2. Извлекает `channel_id`
3. Находит связанный `bridge_id` и `session_uuid`
4. Вызывает `hangup_channel()` для канала
5. Вызывает `delete_bridge()` для bridge
6. Очищает маппинги

## Устойчивость и оптимизации

### 1. Таймауты HTTP
- Все HTTP-запросы к ARI имеют таймаут (`ARI_HTTP_TIMEOUT`)
- По умолчанию: 10 секунд
- Предотвращает зависания при проблемах с Asterisk

### 2. WebSocket переподключение
- При обрыве соединения автоматически переподключается
- Экспоненциальный backoff: 5, 10, 15, ... до 30 секунд
- Ping/pong для поддержания соединения:
  - `ping_interval=20` секунд
  - `ping_timeout=20` секунд

### 3. Очистка ресурсов
- При завершении звонка автоматически удаляются:
  - Каналы (через `hangup_channel`)
  - Bridge (через `delete_bridge`)
- Предотвращает утечки ресурсов в Asterisk

## Взаимодействие с другими контейнерами

- **С `asterisk`**:
  - Подключение к ARI WebSocket (`ws://asterisk:8088/ari/events`)
  - REST API вызовы (`http://asterisk:8088/ari/...`)
  - Управление bridge и каналами

- **С `audiosocket`** (косвенно):
  - При создании externalMedia указывает `external_host=audiosocket:7575`
  - Передаёт `session_uuid` в поле `data`
  - Asterisk сам подключается к audiosocket по RTP

## Порты контейнера

| Порт | Протокол | Назначение | Доступ |
|------|----------|------------|--------|
| 9000 | TCP | FastAPI HTTP | `localhost:9000` (для health-check) |

## Логирование

Логи backend можно посмотреть:
```bash
docker logs backend
docker logs -f backend  # в реальном времени
```

В логах вы увидите:
- Создание bridge и externalMedia
- Обработку событий StasisStart/StasisEnd
- Очистку ресурсов
- Ошибки переподключения WebSocket

## Health Check

```bash
curl http://localhost:9000/health
# Ответ: {"status": "ok"}
```

Используется для мониторинга работоспособности сервиса.
