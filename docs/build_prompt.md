# Промпт для сборки проекта Asterisk5 с нуля

## Полный промпт для Cursor AI

```
Создай проект быстрого голосового ассистента на базе Asterisk PBX и OpenAI Realtime API.

## Архитектура проекта

Проект состоит из 3 Docker-сервисов:
1. **asterisk** - Asterisk PBX сервер (SIP, ARI, RTP)
2. **backend** - FastAPI сервис для управления звонками через ARI
3. **audiosocket** - UDP сервер для обработки аудио и подключения к OpenAI Realtime API

## Структура проекта

```
Asterisk5/
├── docker-compose.yml
├── README.md
├── .env (создать: OPENAI_API_KEY=sk-...)
│
├── asterisk/
│   ├── Dockerfile
│   └── etc/asterisk/
│       ├── pjsip.conf
│       ├── extensions.conf
│       ├── ari.conf
│       ├── http.conf
│       ├── rtp.conf
│       └── sip.conf
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py
│       ├── ari_client.py
│       └── settings.py
│
└── media_sockets/
    ├── Dockerfile
    ├── requirements.txt
    ├── instructions.md
    ├── main.py
    └── src/
        ├── constants.py
        ├── codecs.py
        ├── utils.py
        ├── audio_websocket_client.py
        ├── audio_handler.py
        └── jitter_buffer.py
```

## Ключевые параметры и константы

### media_sockets/src/constants.py

**Основные константы:**
- `HOST = "0.0.0.0"`
- `PORT = 7575`
- `DEFAULT_SAMPLE_RATE = 8000` (8 кГц для телефонии)
- `DEFAULT_SAMPLE_WIDTH = 2` (16-bit PCM)
- `OPENAI_INPUT_RATE = 24000` (24 кГц - нативный формат OpenAI Realtime)
- `OPENAI_OUTPUT_RATE = 24000` (24 кГц)
- `REALTIME_INPUT_FORMAT = "pcm16"`
- `REALTIME_OUTPUT_FORMAT = "pcm16"`
- `REALTIME_MODALITIES = ["text", "audio"]`
- `REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "alloy")`
- `DRAIN_CHUNK_SIZE = 960` (~20 мс PCM при 24 kHz)
- `MIN_OPENAI_INPUT_CHUNK = 1440` (~30 мс PCM16 @ 24 kHz)
- `READER_HEADER_SIZE = 3`
- `READER_PAYLOAD_SIZE = 160`
- `INPUT_FORMAT = "g711_alaw"`
- `OUTPUT_FORMAT = "pcm16"`
- `DEFAULT_LANG = "ru"`
- `REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-mini-realtime-preview")`
- `REALTIME_URL = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"`

**VAD параметры:**
- `VAD_SILENCE_MS = int(os.getenv("AUDIO_VAD_SILENCE_MS", "550"))`
- `VAD_RMS_THRESHOLD = float(os.getenv("AUDIO_VAD_RMS_THRESHOLD", "0.08"))`

**Jitter-buffer параметры:**
- `ENABLE_JITTER_BUFFER = os.getenv("ENABLE_JITTER_BUFFER", "true").lower() == "true"`
- `JITTER_BUFFER_TARGET_MS = int(os.getenv("JITTER_BUFFER_TARGET_MS", "40"))`
- `JITTER_BUFFER_MAX_FRAMES = int(os.getenv("JITTER_BUFFER_MAX_FRAMES", "200"))`
- `OUTPUT_BUFFER_TARGET_MS = int(os.getenv("OUTPUT_BUFFER_TARGET_MS", "40"))`
- `OUTPUT_BUFFER_MAX_FRAMES = int(os.getenv("OUTPUT_BUFFER_MAX_FRAMES", "200"))`

**Barge-in параметры:**
- `ENABLE_LOCAL_BARGE_IN = os.getenv("ENABLE_LOCAL_BARGE_IN", "true").lower() == "true"`
- `BARGE_IN_FRAMES_THRESHOLD = int(os.getenv("BARGE_IN_FRAMES_THRESHOLD", "2"))`

**Функции:**
- `get_openai_api_key() -> str` - получает OPENAI_API_KEY из окружения
- `load_instructions() -> str` - загружает инструкции из instructions.md

### backend/app/settings.py

**Класс Settings:**
- `ari_base_url: str` (из env: ARI_BASE_URL)
- `ari_user: str` (из env: ARI_USER)
- `ari_password: str` (из env: ARI_PASSWORD)
- `ari_app: str` (из env: ARI_APP)

## Docker Compose конфигурация

### docker-compose.yml

**Сервис asterisk:**
- build: `./asterisk`
- container_name: `asterisk`
- restart: `always`
- ports:
  - `5060:5060/udp` (SIP)
  - `7077:7077/tcp` (legacy SIP)
  - `7077:7077/udp` (legacy SIP)
  - `8088:8088/tcp` (ARI HTTP)
  - `10000-10100:10000-10100/udp` (RTP)
- volumes: все конфиги из `./asterisk/etc/asterisk/`
- networks: `ai_voice_net`

**Сервис backend:**
- build: `./backend`
- container_name: `backend`
- depends_on: `asterisk` (condition: service_started)
- environment:
  - `ARI_BASE_URL: http://asterisk:8088/ari`
  - `ARI_USER: admin`
  - `ARI_PASSWORD: admin123`
  - `ARI_APP: ai_app`
  - `AUDIOSOCKET_HOST: audiosocket`
  - `AUDIOSOCKET_PORT: "7575"`
- ports: `9000:9000`
- networks: `ai_voice_net`

**Сервис audiosocket:**
- build: `./media_sockets`
- container_name: `audiosocket`
- depends_on: `asterisk`
- env_file: `.env`
- networks: `ai_voice_net`
- **ВАЖНО:** порт 7575 НЕ пробрасывается наружу (работает внутри Docker сети)

**Сеть:**
- `ai_voice_net` (driver: bridge)

## Asterisk конфигурация

### asterisk/etc/asterisk/pjsip.conf

**Транспорт:**
- `[transport-udp]`
  - type: `transport`
  - protocol: `udp`
  - bind: `0.0.0.0:5060`

**AOR 1001:**
- `[1001]` (type: aor)
  - max_contacts: `1`
  - remove_existing: `yes`
  - default_expiration: `60`
  - qualify_frequency: `30`

**AUTH 1001:**
- `[1001]` (type: auth)
  - auth_type: `userpass`
  - username: `1001`
  - password: `1001pass`

**ENDPOINT 1001:**
- `[1001]` (type: endpoint)
  - context: `from-internal`
  - disallow: `all`
  - allow: `alaw`, `ulaw`, `slin16`
  - transport: `transport-udp`
  - auth: `1001`
  - aors: `1001`
  - direct_media: `no`
  - dtmf_mode: `rfc4733`
  - rtp_symmetric: `yes`
  - force_rport: `yes`
  - rtp_keepalive: `5`

### asterisk/etc/asterisk/extensions.conf

**Контекст from-internal:**
- `exten => 7000,1,NoOp(Вызов в AI-ассистент)`
- `same => n,Answer()`
- `same => n,Stasis(ai_app)`
- `same => n,Hangup()`

### asterisk/etc/asterisk/ari.conf

**Пользователь:**
- `[general]`
- `enabled = yes`
- `read_only = no`
- `[admin]`
- `type = user`
- `read_only = no`
- `password = admin123`

### asterisk/etc/asterisk/http.conf

**HTTP сервер:**
- `[general]`
- `enabled = yes`
- `bindaddr = 0.0.0.0`
- `bindport = 8088`

### asterisk/etc/asterisk/rtp.conf

**RTP настройки:**
- `[general]`
- `rtpstart = 10000`
- `rtpend = 10100`

## Backend сервис

### backend/app/main.py

**FastAPI приложение:**
- Название: `"AI Voice Operator"`
- Endpoint: `GET /health` → `{"status": "ok"}`
- Lifespan: создаёт и запускает `AriWsHandler`

**Глобальные переменные:**
- `ari_handler: AriWsHandler | None = None`

**Функции:**
- `lifespan(app: FastAPI)` - async context manager для инициализации/очистки

### backend/app/ari_client.py

**Класс AriClient:**
- `__init__(base_url: str, user: str, password: str, app_name: str)`
- `create_bridge() -> str` - создаёт mixing bridge
- `create_external_media(bridge_id: str, session_uuid: str) -> str` - создаёт externalMedia канал
- `answer_channel(channel_id: str) -> None` - активирует канал
- `get_channel_details(channel_id: str) -> dict` - получает детали канала
- `add_channel_to_bridge(bridge_id: str, channel_id: str) -> None`
- `get_bridge_channels(bridge_id: str) -> list[str]`
- `hangup_channel(channel_id: str) -> None`
- `delete_bridge(bridge_id: str) -> None`

**Класс AriWsHandler:**
- `__init__(ari_client: AriClient, ws_url: str, app_name: str)`
- `handle_stasis_start(event: dict) -> None` - обрабатывает входящий звонок
- `_cleanup_by_channel(channel_id: str) -> None` - очистка ресурсов
- `run() -> None` - основной цикл WebSocket
- `stop() -> None` - остановка

**Ключевые переменные:**
- `self.channel_to_bridge: dict[str, str]` - маппинг channel_id → bridge_id
- `self.session_channels: dict[str, tuple[str, str]]` - маппинг session_uuid → (channel_id, external_channel_id)

**create_external_media параметры:**
- `external_host = f"{audiosocket_host}:{audiosocket_port}"` (из env: `audiosocket:7575`)
- `format = "alaw"`
- `direction = "both"`
- `transport = "udp"`
- `encapsulation = "rtp"`
- `data = session_uuid` (UUID сессии передаётся в RTP пакетах)

## AudioSocket сервис

### media_sockets/main.py

**UDP сервер:**
- Слушает на `0.0.0.0:7575/UDP`
- Обрабатывает RTP пакеты от Asterisk

**Класс UdpSession:**
- `__init__(session_uuid: str, remote_addr: tuple[str, int], transport: asyncio.DatagramTransport)`
- `handle_incoming_payload(data: bytes) -> None` - обработка входящих RTP пакетов
- `cleanup() -> None` - очистка ресурсов
- `_jitter_buffer_loop() -> None` - цикл jitter-buffer (если включен)

**Ключевые переменные:**
- `self.session_uuid: str`
- `self.remote_addr: tuple[str, int]`
- `self.client: AudioWebSocketClient | None = None`
- `self.audio_handler: AudioHandler | None = None`
- `self.jitter_buffer: JitterBuffer | None = None` (если ENABLE_JITTER_BUFFER)
- `self.inbound_pt: int | None = None` (payload type из RTP заголовка)
- `self.ssrc: int | None = None` (SSRC из RTP заголовка)
- `self.seq_out: int = 0` (sequence number для исходящих пакетов)
- `self.ts_out: int = 0` (timestamp для исходящих пакетов)

**Функции:**
- `make_send_pcm_callback(write_queue: asyncio.Queue[bytes]) -> Callable[[bytes], None]`
- `write_loop(transport, remote_addr, write_queue, session) -> None` - отправка RTP пакетов обратно в Asterisk
- `udp_self_test() -> None` - тест UDP соединения
- `run_udp_server() -> None` - запуск UDP сервера

**RTP параметры:**
- Payload Type для G.711 A-law: `8`
- Размер пакета: `160 байт` (20 мс при 8 кГц)
- Интервал отправки: `20 мс` (0.020 секунды)

### media_sockets/src/audio_websocket_client.py

**Класс AudioWebSocketClient:**
- `__init__(session_uuid: str, audio_handler: AudioHandler, voice: str = "alloy", transcript_callback: Callable[[str], None] | None = None)`
- `push_pcm(data: bytes) -> None` - принимает PCM16 8 кГц от Asterisk
- `connect() -> None` - подключение к OpenAI Realtime API
- `disconnect() -> None` - отключение
- `_forward_pcm_to_openai() -> None` - отправка PCM в OpenAI (с ресемплингом 8→24 кГц)
- `_handle_events() -> None` - обработка событий от OpenAI
- `_track_voice_activity(data: bytes) -> None` - отслеживание VAD и локального barge-in

**Ключевые переменные:**
- `self.session_uuid: str`
- `self.voice: str`
- `self.api_key: str`
- `self.model: str`
- `self.url: str` (WebSocket URL)
- `self.instructions: str` (загружается из instructions.md)
- `self.audio_handler: AudioHandler`
- `self.input_rate: int = 24000`
- `self.output_rate: int = 24000`
- `self.pcm_queue: asyncio.Queue[bytes]` (maxsize=200)
- `self._enable_local_barge_in: bool`
- `self._barge_in_frames_threshold: int`
- `self._consecutive_high_rms: int` (счётчик подряд идущих фреймов с высоким RMS)
- `self._recent_rms_values: list[float]`

**Session config для OpenAI:**
- `model`: из `REALTIME_MODEL`
- `modalities`: `["text", "audio"]`
- `voice`: из `REALTIME_VOICE`
- `input_audio_format`: `"pcm16"`
- `output_audio_format`: `"pcm16"`
- `input_audio_transcription`: `{"model": "whisper-1", "language": "ru"}`
- `turn_detection`: 
  - `type: "server_vad"`
  - `threshold: 0.3`
  - `prefix_padding_ms: 300`
  - `silence_duration_ms: 200`
  - `create_response: True`
  - `interrupt_response: True` (barge-in)

**События OpenAI:**
- `session.created` - сессия создана
- `session.updated` - сессия обновлена (инструкции загружены)
- `conversation.item.input_audio_buffer.committed` - пользователь закончил говорить
- `conversation.item.response.audio.delta` - аудио от бота
- `conversation.item.response.audio_transcript.delta` - транскрипция ответа бота
- `conversation.item.input_audio_transcript.committed` - полная транскрипция речи пользователя
- `response.audio.done` - ответ завершён
- `response.done` - ответ полностью готов

**Логирование:**
- `[SERVER-BARGE-IN]` - перебивание обнаружено OpenAI VAD
- `[LOCAL-BARGE-IN]` - перебивание обнаружено локальным VAD
- `🎤 ПОЛЬЗОВАТЕЛЬ говорит` - транскрипция речи пользователя
- `🤖 БОТ отвечает` - транскрипция ответа бота

### media_sockets/src/audio_handler.py

**Класс AudioHandler:**
- `__init__(session_uuid: str, send_pcm_callback: Callable[[bytes], None])`
- `enqueue_audio(audio_data: bytes) -> None` - добавляет аудио от OpenAI в очередь
- `_playback_loop() -> None` - цикл воспроизведения
- `_flush_batch(batch: bytearray, reason: str) -> None` - отправка батча в Asterisk
- `cleanup() -> None` - очистка ресурсов

**Ключевые переменные:**
- `self.session_uuid: str`
- `self.send_pcm_callback: Callable[[bytes], None]`
- `self.audio_queue: asyncio.Queue`
- `self.output_buffer: OutputBuffer | None` (если ENABLE_JITTER_BUFFER)
- `self._pending_pcm: bytearray`
- `self._packets_sent: int`

**Обработка аудио:**
- Получает PCM16 24 кГц от OpenAI
- Ресемплирует до 8 кГц через `AudioConverter`
- Кодирует в G.711 A-law через `pcm16_to_alaw`
- Отправляет через `send_pcm_callback` (в output_buffer или напрямую)

### media_sockets/src/jitter_buffer.py

**Класс JitterBuffer:**
- `__init__(output_callback: Callable[[bytes], None], target_ms: int = 40)`
- `add_frame(pcm_data: bytes, timestamp: float | None = None) -> None`
- `_output_loop() -> None` - цикл выдачи фреймов каждые 20 мс
- `stop() -> None` - остановка

**Ключевые переменные:**
- `self.buffer: deque[tuple[float, bytes]]` - буфер (timestamp, pcm_data)
- `self.target_ms: int` (40 мс по умолчанию)
- `self.frame_interval: float = 0.020` (20 мс)
- `self.running: bool`
- `self.output_task: asyncio.Task | None`

**Размер фрейма:**
- `RTP_FRAME_SIZE = DEFAULT_SAMPLE_RATE * DEFAULT_SAMPLE_WIDTH // 50` (320 байт для 20 мс при 8 кГц)

**Класс OutputBuffer:**
- `__init__(output_callback: Callable[[bytes], None], target_ms: int = 40)`
- `add_frame(pcm_data: bytes, timestamp: float | None = None) -> None`
- `_output_loop() -> None` - цикл выдачи фреймов
- `stop() -> None` - остановка

**Логика:**
- Накапливает фреймы до target_ms
- Выдаёт фреймы равномерно каждые 20 мс
- При нехватке данных вставляет тишину (zero-padding)

### media_sockets/src/codecs.py

**Функции:**
- `alaw_to_pcm16(data: bytes) -> bytes` - декодирование G.711 A-law → PCM16
- `pcm16_to_alaw(data: bytes) -> bytes` - кодирование PCM16 → G.711 A-law

**Использует:** `audioop.alaw2lin()` и `audioop.lin2alaw()`

### media_sockets/src/utils.py

**Класс AudioConverter:**
- `__init__(input_rate: int, output_rate: int, channels: int = 1)`
- `resample(data: bytes) -> bytes` - ресемплинг через scipy.signal.resample

**Использует:** `scipy.signal.resample` для ресемплинга

## Зависимости

### backend/requirements.txt
```
fastapi==0.104.1
uvicorn[standard]==0.24.0
httpx==0.25.2
websockets==12.0
pydantic==2.5.0
pydantic-settings==2.1.0
```

### media_sockets/requirements.txt
```
numpy==1.24.3
scipy==1.11.4
websockets==12.0
```

## Dockerfile'ы

### asterisk/Dockerfile
- Базовый образ: `debian:12-slim`
- Устанавливает Asterisk 20 из исходников
- Включает модули: `res_ari`, `res_ari_websockets`, `chan_pjsip`, `app_stasis`
- EXPOSE: `5060/udp 7077/tcp 7077/udp 8088/tcp 10000-10100/udp`

### backend/Dockerfile
- Базовый образ: `python:3.10-slim`
- WORKDIR: `/app`
- Копирует `requirements.txt` и устанавливает зависимости
- Копирует `app/` в `/app/app/`
- CMD: `uvicorn app.main:app --host 0.0.0.0 --port 9000`

### media_sockets/Dockerfile
- Базовый образ: `python:3.10`
- Устанавливает `ffmpeg` (для scipy, хотя не используется напрямую)
- WORKDIR: `/app`
- Копирует `requirements.txt` и устанавливает зависимости
- Копирует весь проект
- CMD: `python main.py`
- EXPOSE: `7575/udp`

## Переменные окружения (.env)

**Обязательные:**
- `OPENAI_API_KEY=sk-...`

**Опциональные (с дефолтами):**
- `OPENAI_REALTIME_MODEL=gpt-4o-mini-realtime-preview`
- `OPENAI_REALTIME_VOICE=alloy`
- `AUDIO_VAD_RMS_THRESHOLD=0.08`
- `AUDIO_VAD_SILENCE_MS=550`
- `ENABLE_JITTER_BUFFER=true`
- `JITTER_BUFFER_TARGET_MS=40`
- `OUTPUT_BUFFER_TARGET_MS=40`
- `ENABLE_LOCAL_BARGE_IN=true`
- `BARGE_IN_FRAMES_THRESHOLD=2`

## Ключевые алгоритмы

### Обработка входящего аудио (Asterisk → OpenAI)
1. RTP пакет приходит в `UdpSession.handle_incoming_payload()`
2. Извлекается payload (160 байт G.711 A-law)
3. Декодируется в PCM16 8 кГц через `alaw_to_pcm16()`
4. Если включен jitter-buffer: добавляется в `JitterBuffer.add_frame()`
5. Jitter-buffer выдаёт фреймы равномерно каждые 20 мс
6. Фреймы попадают в `AudioWebSocketClient.push_pcm()`
7. Ресемплинг 8→24 кГц в `_forward_pcm_to_openai()`
8. Отправка в OpenAI через `input_audio_buffer.append()`

### Обработка исходящего аудио (OpenAI → Asterisk)
1. Событие `response.audio.delta` приходит в `_handle_events()`
2. Аудио добавляется в `AudioHandler.enqueue_audio()`
3. Если включен output-buffer: добавляется в `OutputBuffer.add_frame()`
4. Output-buffer выдаёт фреймы равномерно
5. Ресемплинг 24→8 кГц через `AudioConverter.resample()`
6. Кодирование в G.711 A-law через `pcm16_to_alaw()`
7. Упаковка в RTP пакеты в `write_loop()`
8. Отправка обратно в Asterisk через UDP

### Локальный barge-in
1. В `_track_voice_activity()` вычисляется RMS каждого фрейма
2. Если RMS > `VAD_RMS_THRESHOLD`: инкрементируется `_consecutive_high_rms`
3. Если `_consecutive_high_rms >= BARGE_IN_FRAMES_THRESHOLD` и бот говорит:
   - Отправляется `response.create_interrupt()`
   - Логируется `[LOCAL-BARGE-IN]`

## Важные детали реализации

1. **RTP заголовок:** 12 байт (версия, PT, sequence, timestamp, SSRC)
2. **Payload Type:** 8 для G.711 A-law
3. **Размер пакета:** 160 байт = 20 мс при 8 кГц
4. **Интервал отправки:** 20 мс (0.020 секунды)
5. **Ресемплинг:** scipy.signal.resample (линейная интерполяция)
6. **Кодирование:** audioop (встроенный в Python)
7. **WebSocket:** websockets библиотека для OpenAI Realtime API
8. **ARI:** httpx для REST API, websockets для событий

## Порты и протоколы

- **5060/UDP** - SIP (PJSIP)
- **8088/TCP** - ARI HTTP и WebSocket
- **10000-10100/UDP** - RTP (аудио)
- **7575/UDP** - AudioSocket (внутри Docker сети, не пробрасывается)
- **9000/TCP** - FastAPI health check

## Логирование

Все сервисы используют стандартный Python logging:
- Формат: `"%(asctime)s - %(name)s - %(levelname)s - %(message)s"`
- Уровень: `DEBUG` для media_sockets, `INFO` для backend
- Важные события помечаются эмодзи: 🎤 (пользователь), 🤖 (бот)

## Тестирование

1. Запуск: `docker-compose up -d`
2. Проверка: `docker-compose ps` (все 3 контейнера должны быть Up)
3. Health check: `curl http://localhost:9000/health`
4. Звонок: MicroSIP → номер 7000
5. Логи: `docker-compose logs -f audiosocket`

## Критически важные моменты

1. **Порядок инициализации:** Asterisk → Backend → AudioSocket
2. **Session UUID:** передаётся в RTP пакетах через `externalMedia.data`
3. **Ресемплинг:** всегда 8→24 кГц для входа, 24→8 кГц для выхода
4. **Jitter-buffer:** опциональный, но рекомендуется для стабильности
5. **Barge-in:** работает на двух уровнях (локальный + server VAD)
6. **RTP timing:** строго 20 мс между пакетами
7. **Docker сеть:** все сервисы в одной сети `ai_voice_net`

Создай весь проект согласно этой спецификации.
```

