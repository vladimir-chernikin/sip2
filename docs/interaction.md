# Взаимодействие контейнеров и внешних программ

## Общая схема взаимодействия

```
┌─────────────┐
│  MicroSIP   │ (SIP-клиент на хосте)
└──────┬──────┘
       │ SIP/PJSIP (G.711 A-law)
       │ 127.0.0.1:5060/UDP
       │
┌──────▼──────────────────────────────────────┐
│  Контейнер: asterisk                        │
│  - SIP-сервер (5060/UDP)                    │
│  - ARI (8088/TCP)                           │
│  - RTP (10000-10100/UDP)                     │
└──────┬──────────────────┬───────────────────┘
       │                  │
       │ ARI WebSocket    │ RTP (G.711 A-law)
       │ REST API         │ externalMedia
       │                  │
┌──────▼──────────┐  ┌────▼──────────────────┐
│  Контейнер:     │  │  Контейнер:            │
│  backend        │  │  audiosocket          │
│  - FastAPI      │  │  - UDP-сервер (7575)   │
│  - ARI-клиент   │  │  - OpenAI Realtime    │
└─────────────────┘  └────┬───────────────────┘
                          │
                          │ WebSocket
                          │ PCM16 24 кГц
                          │
                  ┌───────▼────────────┐
                  │  OpenAI Realtime   │
                  │  API (внешний)     │
                  └────────────────────┘
```

## Детальный сценарий звонка

### Шаг 1: Регистрация MicroSIP

**Участники**: MicroSIP (хост) ↔ asterisk (контейнер)

1. Пользователь настраивает MicroSIP:
   - SIP-сервер: `127.0.0.1:5060`
   - Username: `1001`
   - Password: `1001pass`

2. MicroSIP отправляет SIP REGISTER на `127.0.0.1:5060/UDP`

3. Asterisk получает запрос, проверяет credentials по `pjsip.conf`

4. При успехе регистрирует endpoint `1001`

5. MicroSIP готов принимать звонки

**Протокол**: SIP/PJSIP (UDP, порт 5060)

---

### Шаг 2: Инициация звонка

**Участники**: MicroSIP (хост) → asterisk (контейнер)

1. Пользователь набирает номер `7000` в MicroSIP

2. MicroSIP отправляет SIP INVITE на `127.0.0.1:5060/UDP`

3. Asterisk обрабатывает по `extensions.conf`:
   - Находит правило для номера `7000`
   - Передаёт вызов в ARI-приложение `ai_app`

4. Asterisk генерирует событие `StasisStart` и отправляет в ARI WebSocket

**Протокол**: SIP/PJSIP (UDP, порт 5060)

---

### Шаг 3: Backend получает событие и создаёт ресурсы

**Участники**: asterisk (контейнер) ↔ backend (контейнер)

1. Backend получает событие `StasisStart` через ARI WebSocket:
   ```
   ws://asterisk:8088/ari/events?app=ai_app
   ```

2. Backend извлекает `channel_id` из события

3. Backend генерирует `session_uuid` (UUID для сессии)

4. Backend создаёт mixing bridge через ARI REST API:
   ```http
   POST http://asterisk:8088/ari/bridges
   Authorization: Basic admin:admin123
   Content-Type: application/json
   
   {"type": "mixing"}
   ```
   Ответ: `{"id": "bridge-uuid-123", ...}`

5. Backend добавляет канал звонящего в bridge:
   ```http
   POST http://asterisk:8088/ari/bridges/{bridge_id}/addChannel?channel={channel_id}
   ```

6. Backend создаёт externalMedia канал:
   ```http
   POST http://asterisk:8088/ari/channels/externalMedia
   Content-Type: application/json
   
   {
     "app": "ai_app",
     "external_host": "audiosocket:7575",
     "format": "alaw",
     "direction": "both",
     "transport": "udp",
     "encapsulation": "rtp",
     "data": "session-uuid-456"
   }
   ```
   Ответ: `{"id": "external-channel-uuid-789", ...}`

7. Backend активирует externalMedia:
   ```http
   POST http://asterisk:8088/ari/channels/{external_channel_id}/answer
   ```

8. Backend добавляет externalMedia в bridge:
   ```http
   POST http://asterisk:8088/ari/bridges/{bridge_id}/addChannel?channel={external_channel_id}
   ```

9. Backend сохраняет маппинги:
   - `channel_id → bridge_id`
   - `external_channel_id → bridge_id`
   - `session_uuid → (channel_id, external_channel_id)`

**Протоколы**: 
- WebSocket (ARI события)
- HTTP REST API (ARI управление)

---

### Шаг 4: Asterisk подключается к audiosocket

**Участники**: asterisk (контейнер) ↔ audiosocket (контейнер)

1. Asterisk активирует externalMedia канал

2. ExternalMedia начинает отправлять RTP-пакеты на `audiosocket:7575/UDP`:
   - Формат: G.711 A-law
   - Частота: 8 кГц
   - Размер пакета: 160 байт (20 мс аудио)
   - В поле данных RTP передаётся `session_uuid`

3. UDP-сервер audiosocket принимает RTP-пакеты

4. Извлекает `session_uuid` из RTP-заголовка или данных

5. Создаёт `UdpSession` для сессии (если ещё не создана)

**Протокол**: RTP (UDP, порт 7575)

---

### Шаг 5: AudioSocket обрабатывает входящий аудио

**Участники**: asterisk (контейнер) → audiosocket (контейнер) → OpenAI Realtime (внешний)

1. UDP-сервер получает RTP-пакет (160 байт G.711 A-law)

2. Декодирует A-law → PCM16 8 кГц:
   ```python
   pcm_8khz = codecs.alaw_to_pcm16(rtp_payload)
   ```

3. Добавляет в jitter-buffer (если включен):
   - Накапливает фреймы до целевого размера (40 мс)
   - Сглаживает джиттер сети

4. Ресемплит 8 кГц → 24 кГц:
   ```python
   pcm_24khz = AudioConverter.resample_audio(pcm_8khz, 8000, 24000)
   ```

5. Отправляет в OpenAI Realtime через WebSocket:
   ```json
   {
     "type": "input_audio_buffer.append",
     "audio": "base64_encoded_pcm16_24khz"
   }
   ```

**Протоколы**:
- RTP (UDP, порт 7575) — от Asterisk
- WebSocket (TLS) — к OpenAI Realtime

---

### Шаг 6: OpenAI обрабатывает речь и генерирует ответ

**Участники**: audiosocket (контейнер) ↔ OpenAI Realtime (внешний)

1. OpenAI Realtime получает PCM16 24 кГц

2. Использует server-side VAD для детекции речи:
   - Определяет начало речи (`input_audio_buffer.speech_started`)
   - Определяет конец речи (`input_audio_buffer.speech_stopped`)

3. Транскрибирует речь в текст

4. Генерирует ответ через LLM (модель `gpt-4o-mini-realtime-preview`)

5. Синтезирует речь в PCM16 24 кГц (голос `alloy`)

6. Отправляет аудио-чанки обратно:
   ```json
   {
     "type": "response.audio.delta",
     "delta": "base64_encoded_pcm16_24khz_chunk"
   }
   ```

**Протокол**: WebSocket (TLS) — к/от OpenAI Realtime

---

### Шаг 7: AudioSocket обрабатывает ответ

**Участники**: OpenAI Realtime (внешний) → audiosocket (контейнер) → asterisk (контейнер)

1. AudioSocket получает PCM16 24 кГц от OpenAI

2. Добавляет в очередь воспроизведения (`AudioHandler.enqueue_audio`)

3. Ресемплит 24 кГц → 8 кГц:
   ```python
   pcm_8khz = AudioConverter.resample_audio(pcm_24khz, 24000, 8000)
   ```

4. Кодирует PCM16 → G.711 A-law:
   ```python
   alaw = codecs.pcm16_to_alaw(pcm_8khz)
   ```

5. Добавляет в output-buffer (если включен):
   - Накапливает чанки
   - Режет на RTP-фреймы (320 байт = 20 мс)

6. Отправляет RTP-пакеты обратно в Asterisk через UDP:
   - Адрес: `asterisk` (через externalMedia)
   - Формат: G.711 A-law, 8 кГц

**Протоколы**:
- WebSocket (TLS) — от OpenAI Realtime
- RTP (UDP, порт 7575) — к Asterisk

---

### Шаг 8: Asterisk передаёт ответ пользователю

**Участники**: asterisk (контейнер) → MicroSIP (хост)

1. Asterisk получает RTP-пакеты от externalMedia

2. Mixing bridge транскодирует аудио между каналами

3. Asterisk отправляет RTP-пакеты каналу звонящего (MicroSIP)

4. MicroSIP получает RTP, декодирует и воспроизводит аудио

5. Пользователь слышит ответ ассистента

**Протокол**: RTP (UDP, порты 10000-10100)

---

### Шаг 9: Barge-in (прерывание ответа)

**Сценарий**: Пользователь начинает говорить во время ответа бота

#### Вариант A: Локальный barge-in

1. AudioSocket отслеживает RMS входящего аудио

2. Если RMS превышает порог (`VAD_RMS_THRESHOLD=0.08`) в течение `BARGE_IN_FRAMES_THRESHOLD=2` фреймов подряд

3. Вызывает `audio_handler.interrupt_playback()`

4. Очищает очередь воспроизведения

5. Начинает слушать новую реплику пользователя

#### Вариант B: Серверный barge-in

1. OpenAI Realtime детектирует речь пользователя

2. Отправляет событие `input_audio_buffer.speech_started`

3. AudioSocket получает событие, вызывает `audio_handler.interrupt_playback()`

4. Очищает очередь воспроизведения

5. Начинает слушать новую реплику пользователя

**Протокол**: WebSocket (TLS) — от OpenAI Realtime

---

### Шаг 10: Завершение звонка

**Участники**: MicroSIP (хост) → asterisk (контейнер) → backend (контейнер)

1. Пользователь кладёт трубку в MicroSIP

2. MicroSIP отправляет SIP BYE

3. Asterisk завершает канал, генерирует события:
   - `ChannelDestroyed`
   - `StasisEnd`

4. Backend получает события через ARI WebSocket

5. Backend находит связанные ресурсы по `channel_id`:
   - `bridge_id`
   - `session_uuid`
   - `external_channel_id`

6. Backend очищает ресурсы:
   - Вызывает `hangup_channel(external_channel_id)`
   - Вызывает `delete_bridge(bridge_id)`

7. AudioSocket завершает сессию:
   - Закрывает WebSocket с OpenAI
   - Очищает буферы
   - Удаляет `UdpSession`

**Протоколы**:
- SIP/PJSIP (UDP, порт 5060) — от MicroSIP
- WebSocket (ARI события) — от Asterisk
- HTTP REST API (ARI управление) — к Asterisk

---

## Сводная таблица протоколов

| Участники | Протокол | Порт | Назначение |
|-----------|----------|------|------------|
| MicroSIP ↔ asterisk | SIP/PJSIP | 5060/UDP | Регистрация, вызовы |
| MicroSIP ↔ asterisk | RTP | 10000-10100/UDP | Аудио (G.711 A-law) |
| asterisk ↔ backend | ARI WebSocket | 8088/TCP | События (StasisStart, StasisEnd) |
| asterisk ↔ backend | ARI REST API | 8088/TCP | Управление (bridge, channels) |
| asterisk ↔ audiosocket | RTP | 7575/UDP | Аудио (G.711 A-law) |
| audiosocket ↔ OpenAI | WebSocket (TLS) | 443/TCP | Аудио (PCM16 24 кГц), события |

## Форматы аудио

| Этап | Формат | Частота | Размер пакета |
|------|--------|---------|---------------|
| MicroSIP ↔ asterisk | G.711 A-law | 8 кГц | 160 байт (20 мс) |
| asterisk ↔ audiosocket | G.711 A-law | 8 кГц | 160 байт (20 мс) |
| audiosocket → OpenAI | PCM16 | 24 кГц | переменный |
| OpenAI → audiosocket | PCM16 | 24 кГц | переменный |
| audiosocket → asterisk | G.711 A-law | 8 кГц | 320 байт (20 мс) |

## Временная диаграмма звонка

```
Время →
MicroSIP:  [REGISTER]──[INVITE 7000]──────────────────────────────[BYE]
            │           │                                           │
Asterisk:   │[REG OK]   │[StasisStart]──────────────────────────────│[StasisEnd]
            │           │                                           │
Backend:    │           │[create_bridge]──[create_externalMedia]───│[cleanup]
            │           │                                           │
Asterisk:   │           │[RTP start]───────────────────────────────│[RTP stop]
            │           │                                           │
AudioSocket:│           │[UDP session]──[WS connect]───────────────│[WS close]
            │           │              │                            │
OpenAI:     │           │              │[speech]──[response]────────│
            │           │              │                            │
AudioSocket:│           │              │[resample]──[RTP back]──────│
            │           │              │                            │
Asterisk:   │           │[RTP forward]─────────────────────────────│
            │           │                                           │
MicroSIP:   │           │[RTP receive]──[play audio]───────────────│
```

## Ключевые моменты

1. **Изоляция**: Все контейнеры работают в Docker-сети `ai_voice_net`, но порты проброшены на хост для MicroSIP

2. **Сессии**: Каждый звонок имеет уникальный `session_uuid`, который передаётся через ARI и RTP

3. **Ресемплинг**: Аудио преобразуется между 8 кГц (телефония) и 24 кГц (OpenAI Realtime)

4. **Буферизация**: Jitter-buffer и output-buffer сглаживают неравномерность сети и обработки

5. **Barge-in**: Двойной механизм (локальный + серверный) для быстрого прерывания ответа

6. **Очистка**: Backend автоматически удаляет ресурсы при завершении звонка
