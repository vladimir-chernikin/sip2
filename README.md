# Asterisk — голосовой ассистент на Asterisk + OpenAI Realtime

Минимальная задержка: прямой RTP (G.711 A-law), server-side VAD и двойной barge-in (локальный по RMS + серверный по событиям OpenAI).

## Кратко: как идёт звонок
1) MicroSIP звонит на 7000 → Asterisk (PJSIP, G.711 A-law).
2) Backend по StasisStart создаёт bridge + externalMedia, подключает к AudioSocket (UDP/RTP).
3) AudioSocket принимает RTP 8 кГц A-law → декодирует → отдаёт в OpenAI Realtime как PCM16 24 кГц (jitter-buffer с лимитом).
4) Ответ OpenAI (PCM16 24 кГц) ресемплится в 8 кГц, буферизуется, кодируется в A-law и уходит RTP в Asterisk → MicroSIP.
5) Barge-in: локальный RMS и server-событие `input_audio_buffer.speech_started` прерывают текущее воспроизведение.

## Сервисы (docker-compose)
- `asterisk`: PBX, ARI (8088), SIP 5060/UDP, RTP 10000-10100.
- `backend`: FastAPI, ARI WebSocket клиент, `/health` на 9000.
- `audiosocket` (media_sockets): UDP 7575 для RTP, WebSocket к OpenAI.

## Структура и роль файлов
- `docker-compose.yml` — оркестрация сервисов.
- `asterisk/` — конфиги PJSIP/ARI/http/rtp, Dialplan (7000), Dockerfile Asterisk.
- `backend/app/main.py` — FastAPI, lifespan.
- `backend/app/ari_client.py` — ARI вызовы, таймауты, ping/pong, backoff, cleanup bridge/каналов на StasisEnd/ChannelDestroyed.
- `backend/app/settings.py` — env-конфиг.
- `media_sockets/main.py` — UDP/RTP сервер, сессии.
- `media_sockets/src/constants.py` — частоты (8 ↔ 24 кГц), форматы, OpenAI модель `gpt-4o-mini-realtime-preview` (по умолчанию), voice `alloy`, лимиты буферов.
- `media_sockets/src/jitter_buffer.py` — входной/выходной буферы с лимитами и безопасным flush.
- `media_sockets/src/audio_websocket_client.py` — клиент OpenAI Realtime, VAD, barge-in локальный/серверный.
- `media_sockets/src/audio_handler.py` — ресемпл 24→8 кГц, нарезка RTP, interrupt playback.
- `media_sockets/src/codecs.py` — A-law ↔ PCM16.
- `media_sockets/src/utils.py` — ресемплинг с защитой на пустые данные.
- `instructions.md` — системный промпт (RU, краткий).

## Технологии
- Asterisk 20 (ARI, externalMedia).
- FastAPI + websockets + httpx.
- OpenAI Realtime API (модель по умолчанию `gpt-4o-mini-realtime-preview`, voice `alloy`).
- Аудио: G.711 A-law 8 кГц ↔ PCM16 24 кГц, jitter/output buffers, SciPy resample.

## Переменные окружения (основные)
```
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
ARI_HTTP_TIMEOUT=10
```

## Запуск
```
docker compose up -d
```
Порты: Asterisk 5060/UDP, 8088 HTTP, 10000-10100 RTP; Backend 9000; AudioSocket UDP 7575.

## Проверка
1) Зарегистрируйте SIP-клиент (MicroSIP) как 1001 на 5060.
2) Позвоните 7000, говорите — ответы потоковые, barge-in прерывает речь бота.

## Логи и здоровье
- Логи: `docker compose logs -f audiosocket`, `backend`, `asterisk`.
- Health: `GET http://localhost:9000/health`.
- Метки барж-ина: `[LOCAL-BARGE-IN]`, `[SERVER-BARGE-IN]`.

## Буферы и устойчивость
- Jitter/output buffers имеют лимиты, дропают старые элементы при переполнении, делают flush перед отменой задач.
- ARI WS с ping/pong и экспоненциальным backoff до 30 c; при завершении каналов удаляются bridge и externalMedia.
