# Контейнер `audiosocket`: медиасервер и OpenAI Realtime

## Роль контейнера

Контейнер `audiosocket` (директория `media_sockets/`) — это медиасервер, который:
- Принимает RTP-потоки от Asterisk (через externalMedia)
- Декодирует и обрабатывает аудио
- Общается с OpenAI Realtime API
- Формирует ответ и отправляет обратно в Asterisk

## Технологии внутри контейнера

### 1. UDP-сервер (Python socket)
- **Порт**: `7575/UDP`
- **Роль**: Приём RTP-пакетов от Asterisk
- **Формат**: G.711 A-law (8 кГц, 160 байт = 20 мс на пакет)

### 2. OpenAI Realtime API (websockets)
- **URL**: `wss://api.openai.com/v1/realtime?model={model}`
- **Модель по умолчанию**: `gpt-4o-mini-realtime-preview`
- **Голос**: `alloy` (настраивается через `OPENAI_REALTIME_VOICE`)
- **Формат аудио**: PCM16, 24 кГц (вход и выход)

### 3. NumPy + SciPy
- **Роль**: Обработка аудио
- **NumPy**: Работа с массивами PCM-данных
- **SciPy**: Ресемплинг аудио (изменение частоты дискретизации)

### 4. Audioop (стандартная библиотека Python)
- **Роль**: Кодирование/декодирование G.711 A-law ↔ PCM16

## Структура файлов контейнера

```
media_sockets/
├── Dockerfile                    # Python образ с ffmpeg (для аудио)
├── requirements.txt             # Зависимости (numpy, scipy, websockets)
├── instructions.md              # Промпт для OpenAI ассистента
├── main.py                      # UDP-сервер, управление сессиями
└── src/
    ├── audio_websocket_client.py  # Клиент OpenAI Realtime API
    ├── audio_handler.py           # Обработка аудио (ресемплинг, кодирование)
    ├── codecs.py                  # G.711 A-law ↔ PCM16
    ├── constants.py               # Константы (порты, частоты, настройки)
    ├── jitter_buffer.py           # Jitter-buffer и output-buffer
    └── utils.py                   # Утилиты (ресемплинг через scipy)
```

## Основные компоненты

### 1. `main.py` — UDP-сервер
- Слушает порт `7575/UDP`
- Принимает RTP-пакеты от Asterisk
- Извлекает `session_uuid` из RTP-заголовка или данных
- Создаёт `UdpSession` для каждой сессии
- Управляет жизненным циклом сессий

### 2. `src/audio_websocket_client.py` — клиент OpenAI Realtime

#### Класс `AudioWebSocketClient`
- **Роль**: Управление WebSocket-сессией с OpenAI Realtime
- **Основные методы**:
  - `connect()` — подключение к Realtime API
  - `send_event()` — отправка событий в Realtime
  - `push_pcm()` — добавление PCM-данных в очередь для отправки
  - `_forward_pcm_to_openai()` — отправка PCM в Realtime
  - `_track_voice_activity()` — отслеживание VAD (Voice Activity Detection)
  - `_check_local_barge_in()` — проверка локального barge-in

#### Обработка событий от Realtime
- `response.audio.delta` — получение аудио-чанков ответа
- `input_audio_buffer.speech_started` — серверный barge-in
- `response.created` — создание нового ответа
- `response.completed` — завершение ответа

### 3. `src/audio_handler.py` — обработка аудио

#### Класс `AudioHandler`
- **Роль**: Обработка исходящего аудио от OpenAI
- **Основные методы**:
  - `enqueue_audio()` — добавление аудио в очередь воспроизведения
  - `interrupt_playback()` — прерывание воспроизведения (barge-in)
  - `stop_playback()` — остановка воспроизведения

#### Процесс обработки
1. Получает PCM16 24 кГц от OpenAI
2. Ресемплит в PCM16 8 кГц (через `AudioConverter.resample_audio`)
3. Кодирует в G.711 A-law (через `codecs.pcm16_to_alaw`)
4. Режет на RTP-фреймы (320 байт = 20 мс)
5. Отправляет обратно в Asterisk через callback

### 4. `src/jitter_buffer.py` — буферизация

#### Класс `JitterBuffer`
- **Роль**: Сглаживание джиттера входящего аудио
- **Параметры**:
  - `JITTER_BUFFER_TARGET_MS=40` — целевой размер буфера (мс)
  - `JITTER_BUFFER_MAX_FRAMES=200` — максимальный размер (фреймы)
- **Поведение**:
  - Накапливает фреймы до целевого размера
  - Отдаёт фреймы с интервалом 20 мс
  - При переполнении дропает старые фреймы

#### Класс `OutputBuffer`
- **Роль**: Сглаживание исходящего аудио
- **Параметры**:
  - `OUTPUT_BUFFER_TARGET_MS=40` — целевой размер буфера (мс)
  - `OUTPUT_BUFFER_MAX_FRAMES=200` — максимальный размер (фреймы)
- **Поведение**:
  - Накапливает PCM-чанки
  - Режет на RTP-фреймы (320 байт)
  - Отдаёт фреймы с интервалом 20 мс
  - При переполнении дропает старые чанки

### 5. `src/codecs.py` — кодирование/декодирование

#### Функции
- `alaw_to_pcm16()` — декодирование G.711 A-law → PCM16
- `pcm16_to_alaw()` — кодирование PCM16 → G.711 A-law

### 6. `src/utils.py` — утилиты

#### Класс `AudioConverter`
- **Метод**: `resample_audio()`
- **Роль**: Ресемплинг PCM-аудио между частотами
- **Использование**:
  - 8 кГц → 24 кГц (для отправки в OpenAI)
  - 24 кГц → 8 кГц (для отправки в Asterisk)

### 7. `src/constants.py` — константы

#### Основные параметры
- `HOST = "0.0.0.0"`, `PORT = 7575` — UDP-сервер
- `DEFAULT_SAMPLE_RATE = 8000` — частота для Asterisk
- `OPENAI_INPUT_RATE = 24000` — частота для OpenAI (вход)
- `OPENAI_OUTPUT_RATE = 24000` — частота для OpenAI (выход)
- `REALTIME_MODEL` — модель OpenAI (по умолчанию `gpt-4o-mini-realtime-preview`)
- `REALTIME_VOICE` — голос (по умолчанию `alloy`)

#### VAD параметры
- `VAD_RMS_THRESHOLD = 0.08` — порог RMS для детекции речи
- `VAD_SILENCE_MS = 550` — окно тишины (мс)

#### Barge-in параметры
- `ENABLE_LOCAL_BARGE_IN = true` — включить локальный barge-in
- `BARGE_IN_FRAMES_THRESHOLD = 2` — количество фреймов для детекции

## Переменные окружения

```env
OPENAI_API_KEY=...                  # обязательно
OPENAI_REALTIME_MODEL=gpt-4o-mini-realtime-preview
OPENAI_REALTIME_VOICE=alloy
ENABLE_JITTER_BUFFER=true
JITTER_BUFFER_TARGET_MS=40
OUTPUT_BUFFER_TARGET_MS=40
JITTER_BUFFER_MAX_FRAMES=200
OUTPUT_BUFFER_MAX_FRAMES=200
AUDIO_VAD_RMS_THRESHOLD=0.08
AUDIO_VAD_SILENCE_MS=550
ENABLE_LOCAL_BARGE_IN=true
BARGE_IN_FRAMES_THRESHOLD=2
```

## Поток обработки аудио

### Входящий поток (Asterisk → OpenAI)
1. UDP-сервер принимает RTP-пакет (G.711 A-law, 160 байт)
2. Декодирует A-law → PCM16 8 кГц (через `codecs.alaw_to_pcm16`)
3. Добавляет в jitter-buffer (если включен)
4. Ресемплит 8 кГц → 24 кГц (через `AudioConverter.resample_audio`)
5. Отправляет в OpenAI Realtime через WebSocket

### Исходящий поток (OpenAI → Asterisk)
1. Получает PCM16 24 кГц от OpenAI Realtime
2. Добавляет в очередь воспроизведения (`AudioHandler.enqueue_audio`)
3. Ресемплит 24 кГц → 8 кГц (через `AudioConverter.resample_audio`)
4. Кодирует PCM16 → G.711 A-law (через `codecs.pcm16_to_alaw`)
5. Добавляет в output-buffer (если включен)
6. Режет на RTP-фреймы (320 байт = 20 мс)
7. Отправляет обратно в Asterisk через UDP

## Barge-in (прерывание ответа)

### Локальный barge-in
- Отслеживает RMS (Root Mean Square) входящего аудио
- Если RMS превышает порог (`VAD_RMS_THRESHOLD`) в течение `BARGE_IN_FRAMES_THRESHOLD` фреймов подряд
- Вызывает `audio_handler.interrupt_playback()`
- Очищает очередь воспроизведения

### Серверный barge-in
- Получает событие `input_audio_buffer.speech_started` от OpenAI Realtime
- Вызывает `audio_handler.interrupt_playback()`
- Очищает очередь воспроизведения

## Взаимодействие с другими контейнерами

- **С `asterisk`**:
  - Принимает RTP на порту `7575/UDP`
  - Отправляет RTP обратно в Asterisk
  - Аудио в формате G.711 A-law (8 кГц)

- **С `backend`** (косвенно):
  - Backend создаёт externalMedia с `external_host=audiosocket:7575`
  - Передаёт `session_uuid` в поле `data`
  - UDP-сервер извлекает `session_uuid` и создаёт сессию

- **С OpenAI Realtime API** (внешний сервис):
  - WebSocket-соединение к `wss://api.openai.com/v1/realtime`
  - Отправка/приём PCM16 24 кГц
  - Получение событий (speech_started, response.created, и т.д.)

## Порты контейнера

| Порт | Протокол | Назначение | Доступ |
|------|----------|------------|--------|
| 7575 | UDP | RTP-сервер | `audiosocket:7575` (внутри Docker) |

## Логирование

Логи audiosocket можно посмотреть:
```bash
docker logs audiosocket
docker logs -f audiosocket  # в реальном времени
```

В логах вы увидите:
- Создание сессий
- Отправку/приём аудио
- Срабатывание barge-in (`[LOCAL-BARGE-IN]`, `[SERVER-BARGE-IN]`)
- Транскрипции речи пользователя и ответов бота
