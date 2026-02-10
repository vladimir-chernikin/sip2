# 🔴 ТЕХНИЧЕСКОЕ ЗАДАНИЕ: Исследование проблемы RTP потока в Asterisk + OpenAI Realtime API

## 1. АРХИТЕКТУРА СИСТЕМЫ

```
┌─────────────────┐      PJSIP      ┌──────────────┐
│   Билайн SIP    │◄──────────────►│   Asterisk   │
│  (ip.beeline.ru)│   (register/   │  (container) │
│                 │    invite)     │   172.18.0.4  │
└─────────────────┘                └──────┬───────┘
                                            │ ARI
                                            │
                                      ┌─────▼─────────┐
                                      │   Backend     │
                                      │  (Python/FastAPI)│
                                      │  (container)   │
                                      └─────┬─────────┘
                                            │
                                            │ ExternalMedia API
                                            │
                     ┌────────────────────────┼──────────────────────┐
                     │                        │                      │
                ┌────▼─────┐          ┌───────▼──────────┐    ┌─────▼────────┐
                │ Audiosocket│         │   OpenAI Realtime │    │   +79202119023│
                │(UDP:7575)  │         │      API         │    │  (тестовый)  │
                │ 172.18.0.2  │         │  (WebSocket)     │    │              │
                └────────────┘         └──────────────────┘    └──────────────┘
```

## 2. КОНФИГУРАЦИЯ SIP ТРАНКА БИЛАЙН

### 2.1. Учётные данные
```
SIP URI: SIP030FQU0451O@ip.beeline.ru
Password: ZAQ12wsx-
Server: ip.beeline.ru:5060
Телефон номера: +713433506036
```

### 2.2. PJSIP Configuration (`/asterisk/etc/asterisk/pjsip.conf`)

```conf
; ===== AUTH ДЛЯ БИЛАЙН =====
[beeline-auth]
type=auth
auth_type=userpass
username=SIP030FQU0451O@ip.beeline.ru
password=ZAQ12wsx-

; ===== AOR ДЛЯ БИЛАЙН (outbound) =====
[beeline-aor]
type=aor
contact=sip:SIP030FQU0451O@ip.beeline.ru
qualify_frequency=30
qualify_timeout=5.0

; ===== REGISTRATION ДЛЯ БИЛАЙН =====
[beeline-reg]
type=registration
transport=transport-udp
outbound_auth=beeline-auth
server_uri=sip:ip.beeline.ru:5060
client_uri=sip:SIP030FQU0451O@ip.beeline.ru
retry_interval=60
expiration=300
contact_user=SIP030FQU0451O

; ===== IDENTIFY ДЛЯ ВХОДЯЩИХ ОТ БИЛАЙН =====
[beeline-in-identify]
type=identify
endpoint=beeline-in
match=185.243.5.36
match=51.91.168.102
match=84.201.137.0

; ===== AOR ДЛЯ ВХОДЯЩИХ =====
[beeline-in-aor]
type=aor
remove_existing=yes
remove_unavailable=yes
max_contacts=1

; ===== ENDPOINT ДЛЯ ВХОДЯЩИХ =====
[beeline-in]
type=endpoint
context=from-beeline
disallow=all
allow=alaw
transport=transport-udp
aors=beeline-in-aor
direct_media=no
dtmf_mode=auto
rewrite_contact=yes
rtp_symmetric=yes
force_rport=yes
rtp_keepalive=5
from_user=SIP030FQU0451O
from_domain=ip.beeline.ru
media_address=109.172.46.197
trust_id_inbound=yes
inband_progress=yes
language=ru

; ===== ENDPOINT ДЛЯ ИСХОДЯЩИХ =====
[beeline-out]
type=endpoint
context=from-internal
disallow=all
allow=alaw
transport=transport-udp
auth=beeline-auth
outbound_auth=beeline-auth
aors=beeline-aor
direct_media=no
dtmf_mode=auto
rewrite_contact=yes
rtp_symmetric=yes
force_rport=yes
rtp_keepalive=5
from_user=SIP030FQU0451O
from_domain=ip.beeline.ru
media_address=109.172.46.197
inband_progress=yes
language=ru
```

**Критичные параметры (из telephonization.ru, октябрь 2023):**
- `inband_progress=yes` - без этого не работает progress indication
- `dtmf_mode=auto` - вместо rfc4733 (важно для Билайн!)
- `rtp_keepalive=5` - каждые 5 секунд, НЕ 0!
- `rtp_symmetric=yes` - симметричный RTP
- `rewrite_contact=yes` - перезапись Contact

### 2.3. Transport Configuration

```conf
[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060
external_media_address=109.172.46.197  # Внешний IP сервера
external_signaling_address=109.172.46.197
local_net=172.16.0.0/12
local_net=10.0.0.0/8
local_net=192.168.0.0/16
```

### 2.4. Dialplan Configuration (`extensions.conf`)

```conf
[from-beeline]
; Все входящие от Билайн → AI ассистент
exten => _.,1,NoOp(Входящий вызов от Билайн на ${EXTEN})
 same => n,Answer()
 same => n,Stasis(ai_app)
 same => n,Hangup()

[from-internal]
; Исходящие через Билайн
exten => _7XXXXXXXXXX,1,NoOp(Исходящий вызов через Билайн на ${EXTEN})
 same => n,Dial(PJSIP/${EXTEN}@beeline-out,60)
 same => n,Hangup()
```

## 3. DOCKER КОНФИГУРАЦИЯ

### 3.1. Сеть
```yaml
networks:
  ai_voice_net:
    driver: bridge
```

**IP адреса (динамические, меняются при рестарте!):**
- Asterisk: 172.18.0.4
- Audiosocket: 172.18.0.2
- Backend: 172.18.0.3

### 3.2. Environment Variables (`docker-compose.yml`)

```yaml
backend:
  environment:
    ARI_BASE_URL: http://asterisk:8088/ari
    ARI_USER: admin
    ARI_PASSWORD: admin123
    ARI_APP: ai_app
    AUDIOSOCKET_HOST: 172.18.0.2  # ⚠️ Hardcoded IP!
    AUDIOSOCKET_PORT: "7575"
```

**ПРОБЛЕМА:** Docker hostname `audiosocket` не резолвится Asterisk UnicastRTP, пришлось использовать IP.

### 3.3. RTP Configuration (`rtp.conf`)

```conf
[general]
rtpstart=10000
rtpend=10100
strictrtp=no
icesupport=yes
rtcpinterval=5
```

## 4. РЕАЛИЗАЦИЯ В BACKEND

### 4.1. Создание ExternalMedia (`/asterisk/backend/app/ari_client.py`)

```python
async def create_external_media(
    self,
    bridge_id: str,
    session_uuid: str,
) -> str:
    """Создаёт externalMedia-канал, подключающийся к AudioSocket"""
    url = f"{self.base_url}/channels/externalMedia"

    audiosocket_host = os.getenv("AUDIOSOCKET_HOST", "audiosocket")  # 172.18.0.2
    audiosocket_port = os.getenv("AUDIOSOCKET_PORT", "7575")
    external_host = f"{audiosocket_host}:{audiosocket_port}"

    payload = {
        "app": self.app_name,
        "external_host": external_host,  # "172.18.0.2:7575"
        "format": "alaw",
        "direction": "both",
        "transport": "udp",
        "encapsulation": "rtp",
        "data": session_uuid,  # UUID сессии для OpenAI
    }

    response = await client.post(url, json=payload, auth=self.auth)
    data = response.json()
    return data.get("id", "")
```

### 4.2. Обработка StasisStart (последняя версия)

```python
async def handle_stasis_start(self, event: dict) -> None:
    # Обрабатываем только PJSIP (входящий от Билайн)
    channel = event.get("channel") or {}
    channel_id = channel.get("id")
    channel_name = channel.get("name", "")

    if not channel_name.startswith("PJSIP/"):
        return  # Пропускаем UnicastRTP

    # Новый звонок
    session_uuid = str(uuid.uuid4())

    # 1. Создаём mixing bridge
    bridge_id = await self.ari_client.create_bridge()

    # 2. Добавляем абонента (PJSIP) в bridge
    await self.ari_client.add_channel_to_bridge(bridge_id, channel_id)

    # 3. Создаём ExternalMedia
    external_channel_id = await self.ari_client.create_external_media(
        bridge_id=bridge_id,
        session_uuid=session_uuid,
    )

    # 4. Добавляем ExternalMedia в bridge
    await self.ari_client.add_channel_to_bridge(bridge_id, external_channel_id)

    # 5. Answer() - запускаем RTP
    await self.ari_client.answer_channel(external_channel_id)

    # 6. Проверяем
    await asyncio.sleep(0.5)
    channels = await self.ari_client.get_bridge_channels(bridge_id)
```

## 5. СЦЕНАРИИ И ЛОГИ

### 5.1. СЦЕНАРИЙ 1: Входящий звонок от Билайн

#### Шаг 1: Звонок приходит от Билайн
```
[Feb 10 23:27:29] NOTICE[] res_pjsip/pjsip_distributor.c: Request 'INVITE' from '<sip:222@109.172.46.197>'
-> Match по IP (beeline-in-identify)
-> Отправлен 100 Trying
-> Отправлен 200 OK (auth ok)
```

#### Шаг 2: Dialplan обработка
```
[Feb 10 23:27:29] -- Executing [900713433506036@from-beeline:1] NoOp("Входящий вызов от Билайн")
[Feb 10 23:27:29] -- Executing [900713433506036@from-beeline:2] Answer()
[Feb 10 23:27:29] -- Executing [900713433506036@from-beeline:3] Stasis(ai_app)
```

#### Шаг 3: ARI создаёт Stasis приложение
```
[Feb 10 23:27:29] -- Channel PJSIP/beeline-in-00000000 joined 'simple_bridge' stasis-bridge
```

#### Шаг 4: Backend создаёт ExternalMedia
```
2026-02-10 23:27:29 - app.ari_client - INFO - Создан bridge (type=mixing, class=stasis)
2026-02-10 23:27:29 - app.ari_client - INFO - Канал PJSIP/beeline-in-00000000 добавлен в bridge
2026-02-10 23:27:29 - app.ari_client - INFO - Создание externalMedia (UDP/RTP):
   external_host=172.18.0.2:7575,
   payload={'app': 'ai_app', 'external_host': '172.18.0.2:7575', 'format': 'alaw',
           'direction': 'both', 'transport': 'udp', 'encapsulation': 'rtp',
           'data': '7fd7cf6c-656d-4f28-93ea-203fb6088499'}
```

#### Шаг 5: Asterisk создаёт UnicastRTP
```
[Feb 10 23:27:29] -- Called audiosocket:7575/c(alaw)
[Feb 10 23:27:29] -- UnicastRTP/172.18.0.2:7575-0x7d3ea8003080 answered
[Feb 10 23:27:29] -- Channel UnicastRTP/172.18.0.2:7575-0x7d3ea8003080 joined 'simple_bridge'
```

#### Шаг 6: Проверка состояния каналов
```
Channel: PJSIP/beeline-in-00000000
  State: Up (6)
  WriteFormat: alaw
  ReadFormat: alaw
  Bridge ID: 3f2ab597-f2b6-47ec-88f8-732951425d94
  BRIDGEPEER=UnicastRTP/172.18.0.2:7575-0x7d3ea8003080

Channel: UnicastRTP/172.18.0.2:7575-0x7d3ea8003080
  State: Up (6)
  WriteFormat: alaw
  ReadFormat: alaw
  State: sendrecv
  UNICASTRTP_LOCAL_PORT=10092
  UNICASTRTP_LOCAL_ADDRESS=172.18.0.4
  BRIDGEPEER=PJSIP/beeline-in-00000000
```

#### ❌ Шаг 7: AUDIOSOCKET НЕ ПОЛУЧАЕТ RTP
```
$ docker logs audiosocket --since 10m
[EMPTY] - 0 UDP пакетов!

$ tcpdump -i any udp port 7575 -c 5
[EMPTY] - Нет трафика!
```

**КРИТИЧЕСКАЯ ПРОБЛЕМА:** UnicastRTP канал создан, в состоянии Up, sendrecv, но Asterisk НЕ отправляет RTP на 172.18.0.2:7575.

### 5.2. СЦЕНАРИЙ 2: Исходящий звонок на +79202119023

#### Команда
```bash
docker exec asterisk asterisk -rx "channel originate PJSIP/79202119023@beeline-out application Wait"
```

#### Логи
```
[Feb 10 23:27:50] -- Called 79202119023@beeline-out
[Feb 10 23:27:50] -- PJSIP/beeline-out-00000000 is making progress
[Feb 10 23:27:50] -- Executing [79202119023@from-internal:3] Hangup()
[Feb 10 23:27:50] == Everyone is busy/congested at this time (1:0/0/1)
```

#### SIP trace
```
[Feb 10 23:27:50] NOTICE[] Request 'INVITE' from '<sip:222@109.172.46.197>' failed
   - No matching endpoint found
   - Failed to authenticate
```

**ПРОБЛЕМА:** Звонок "making progress" (100 Trying получен), но затем сразу "Everyone is busy". Похоже Билайн отвечает с ошибкой (403/404/486).

## 6. ДИАГНОСТИКА RTP

### 6.1. Проверка соединения
```bash
# Проверка что audiosocket слушает
$ docker exec audiosocket netstat -ulnp | grep 7575
udp        0.0.0.0:7575            0.0.0.0:*

# Проверка UDP сокета
$ docker exec audiosocket cat /proc/net/udp | grep 1D8F  # 7575 в hex
  local_address rem_address   st tx_queue rx_queue
  00000000:1D8F 00000000:0000 07 00000000:00000000
  ↑ Сокет открыт, но remote_address = 0.0.0.0:0 (нет соединения)
```

### 6.2. Анализ UnicastRTP

**Детали канала показывают:**
- `UNICASTRTP_LOCAL_PORT=10092` - Asterisk слушает на этом порту для входящего RTP от audiosocket
- `UNICASTRTP_LOCAL_ADDRESS=172.18.0.4` - IP Asterisk (внутренний)
- НЕТ переменной `UNICASTRTP_REMOTE_PORT` - куда отправлять RTP?

**Вопрос:** Должен ли Asterisk отправлять RTP на 172.18.0.2:7575, или он ждёт первый пакет ОТ audiosocket?

### 6.3. Audiosocket ожидает входящий RTP

Код audiosocket (`/app/main.py`):
```python
class UdpSession:
    def __init__(self, addr: tuple[str, int], transport):
        self.session_uuid = str(uuid.uuid4())  # ← Генерирует СЛУЧАЙНЫЙ UUID!
        # ...
        self.client = AudioWebSocketClient(session_uuid=self.session_uuid, ...)

def datagram_received(self, data: bytes, addr: tuple[str, int]):
    # Создаёт новую сессию при получении первого UDP пакета
    if addr not in self.sessions:
        self.sessions[addr] = UdpSession(addr, transport)
```

**КРИТИЧЕСКАЯ ПРОБЛЕМА:**
- Backend передаёт `data: session_uuid` в ExternalMedia
- Audiosocket НЕ получает этот UUID
- Audiosocket создаёт НОВЫЙ случайный UUID при получении первого пакета
- UUID не совпадают → нет подключения к OpenAI

## 7. СВОДКА ПРОБЛЕМ

| Проблема | Статус | Детали |
|----------|--------|--------|
| Регистрация Билайн | ✅ WORKS | Registered, exp. 140s |
| Входящий звонок reach Stasis | ✅ WORKS | PJSIP → Stasis(ai_app) |
| Bridge creation | ✅ WORKS | mixing type, 2 channels |
| ExternalMedia creation | ✅ WORKS | channel_id получен |
| UnicastRTP creation | ✅ WORKS | в состоянии Up, sendrecv |
| **RTP от Asterisk → Audiosocket** | ❌ **FAIL** | 0 пакетов за 10 минут! |
| Исходящий звонок | ❌ FAIL | "Everyone is busy" |
| Кодек | ✅ OK | alaw с обеих сторон |

## 8. ВОПРОСЫ ДЛЯ ИССЛЕДОВАНИЯ

1. **Почему Asterisk не отправляет RTP?**
   - UnicastRTP ждёт входящий RTP первым?
   - Нужно ли вызывать `start_media` или другой метод ARI?
   - Правильный ли порядок: create → add to bridge → answer?

2. **Как передаётся session_uuid от Asterisk к audiosocket?**
   - Параметр `data` в ExternalMedia отправляется в RTP?
   - Audiosocket должен парсить RTP для получения UUID?
   - Или нужен другой механизм передачи?

3. **Исходящие звонки:**
   - Почему "making progress" → "busy"?
   - Билайн отклоняет или формат номера неверный?
   - Нужно ли добавлять префикс `8` вместо `7`?

## 9. ДОПОЛНИТЕЛЬНЫЕ ЛОГИ

### 9.1. Backend логи (полные)
```log
2026-02-10 23:27:29,209 - app.ari_client - INFO - Новый звонок: channel_id=1770766072.0, session_uuid=7fd7cf6c-656d-4f28-93ea-203fb6088499
2026-02-10 23:27:29,262 - app.ari_client - INFO - Создан bridge 3f2ab597-f2b6-47ec-88f8-732951425d94d
2026-02-10 23:27:29,331 - app.ari_client - INFO - Канал 1770766072.0 добавлен в bridge
2026-02-10 23:27:29,371 - app.ari_client - INFO - Создание externalMedia: external_host=172.18.0.2:7575, data=7fd7cf6c-656d-4f28-93ea-203fb6088499
2026-02-10 23:27:29,403 - app.ari_client - INFO - Создан externalMedia канал 1770766073.1
2026-02-10 23:27:29,418 - app.ari_client - INFO - Канал 1770766073.1 добавлен в bridge
2026-02-10 23:27:29,567 - app.ari_client - INFO - ExternalMedia канал активирован (ANSWER)
2026-02-10 23:27:30,080 - app.ari_client - INFO - Детали bridge: channels=['1770766072.0', '1770766073.1']
```

### 9.2. Asterisk CLI show channels
```
Channel                                                          Location                         State   Application(Data)
PJSIP/beeline-in-00000000                                        900713433506036@from-beeline:3    Up      Stasis(ai_app)
UnicastRTP/172.18.0.2:7575-0x7d3ea8003080                        s@default:1                      Up      Stasis(ai_app,...)
```

### 9.3. Регистрация Билайн
```
beeline-reg/sip:ip.beeline.ru:5060    beeline-auth    Registered (exp. 140s)
```

---

**ВЫВОД:** Система настроена корректно, все компоненты создаются и находятся в правильном состоянии, но RTP поток от Asterisk к audiosocket не запускается. Требуется исследование механизма UnicastRTP в Asterisk и возможная корректировка порядка операций или параметров ExternalMedia.

---

## 10. 🔍 УТОЧНЯЮЩИЙ АНАЛИЗ (2025-02-10)

### 10.1. КАСТОМНЫЙ ИЛИ СТОРОННИЙ AUDIOСОКЕТ?

**Ответ: КАСТОМНЫЙ КОМПОНЕНТ**

Исходный код: `/asterisk/media_sockets/main.py` (21KB Python код)

Это собственная разработка, не сторонняя библиотека.

### 10.2. КАК AUDIOSOCKET "СЛУШАЕТ" RTP?

**Ответ: СОКЕТ ОТКРЫТ ЗАРАНЕЕ, СЕССИЯ — ПРИ ПЕРВОМ ПАКЕТЕ**

```python
# Строка 288-293 main.py
class UdpProtocol:
    def connection_made(self, transport):
        # СОКЕТ ОТКРЫВАЕТСЯ ЗАРАНЕЕ при запуске
        self.transport = transport
        logger.info("UDP AudioSocket сервер (RTP/G.711 A-law) запущен, локальный адрес: %s", sockname)
        # sockname = ('0.0.0.0', 7575) - слушает на ВСЕХ интерфейсах
```

**Важный момент: сессия создаётся ТОЛЬКО при получении ПЕРВОГО RTP:**

```python
# Строка 422-428 main.py (datagram_received)
session = self.sessions.get(addr)
if session is None:
    # 🔑 СОЗДАЁТСЯ ТОЛЬКО ПРИ ПЕРВОМ ПАКЕТЕ от адреса
    logger.info("[SESSION] Создание новой UDP-сессии для адреса %s", addr)
    session = UdpSession(addr, self.transport)
    self.sessions[addr] = session
```

### 10.3. НУЖНО ЛИ ASTERISK ПОЛУЧИТЬ RTP ПЕРВЫМ?

**Ответ: ДА, КРИТИЧЕСКИ ВАЖНО!** 🎯

**Это КОРНЕВАЯ ПРИЧИНА deadlock'а:**

1. **Asterisk UnicastRTP** не отправляет RTP, пока не получит первый пакет
2. **Audiosocket** пассивный — не может отправить RTP, пока не создаст сессию
3. Сессия не создаётся, пока не получен первый RTP

**Доказательство из кода UdpSession (строка 230-245):**

```python
def __init__(self, addr, transport):
    self.session_uuid = str(uuid.uuid4())  # ← Генерирует СЛУЧАЙНЫЙ UUID!

    # Создаём WebSocket клиент к OpenAI
    self.client = AudioWebSocketClient(session_uuid=self.session_uuid, ...)

    # Запускаем write_loop - но он ОЖИДАЕТ данные из очереди
    self.write_task = asyncio.create_task(
        write_loop(self.transport, self.remote_addr, write_queue, self)
    )
    # write_loop делает: await write_queue.get() - но очередь ПУСТА!
```

**write_loop НЕ отправляет RTP пока:**
- Не получен первый пакет от Asterisk (для инициализации)
- OpenAI не отправил аудио (write_queue пуста)

**Deadlock диаграмма:**
```
Asterisk UnicastRTP: "Я не отправлю RTP, пока не получу от тебя"
Audiosocket:    : "Я не могу отправить RTP, пока не создам сессию, а сессия требует первый пакет"
```

### 10.4. IP 172.18.0.2 - ЖЁСТКИЙ ИЛИ ДИНАМИЧЕСКИЙ?

**Ответ: ДИНАМИЧЕСКИЙ, МЕНЯЕТСЯ ПРИ ПЕРЕЗАПУСКЕ!** ⚠️

```yaml
# Docker network config
networks:
  ai_voice_net:
    driver: bridge  # = динамическое выделение IP
```

**Доказательства:**
- Подсеть: `172.18.0.0/16` (65530 доступных динамических IP)
- Bridge-сеть НЕ назначает статические IP контейнерам
- Каждый `docker compose down/up` = новые IP для контейнеров

**Факты из наблюдений:**
```
Раньше: Asterisk=172.18.0.2, Audiosocket=172.18.0.4
Позже: Asterisk=172.18.0.4, Audiosocket=172.18.0.2  # 🔀 ПОМЕНЯЛИСЬ!
```

**Риск:** Hardcoded `AUDIOSOCKET_HOST: 172.18.0.2` в docker-compose.yml сломается после следующего рестарта!

---

## 11. 🎯 КОРНЕВАЯ ПРИЧИНА ПРОБЛЕМЫ

**Проблема:** UnicastRTP в Asterisk НЕ отправляет RTP первым к внешнему хосту.

**Это архитектурное ограничение Asterisk** - UnicastRTP разработан с расчётом на то, что внешний хост (audiosocket) будет отправлять RTP первым (например, как RTP proxy или медиасервер).

### 11.1. Возможные решения

#### Вариант 1: Заставить audiosocket отправить первый RTP пакет ⭐ РЕКОМЕНДУЕМО

**Суть проблемы:** UnicastRTP ждёт входящий RTP, а audiosocket не может отправить без сессии.

**Решение:** Изменить `/asterisk/media_sockets/main.py` в `UdpSession.__init__`:

```python
class UdpSession:
    def __init__(self, addr: tuple[str, int], transport):
        self.remote_addr = addr
        self.transport = transport
        self.session_uuid = str(uuid.uuid4())

        # ... существующий код создания OpenAI клиента ...

        # 🔑 НОВОЕ: отправить silence-пакет для инициации RTP немедленно
        # 160 байт = 20ms silence для alaw (8kHz, 8bit)
        silence_packet = bytes(160)  # Все нули = silence в G.711 A-law

        transport.sendto(silence_packet, addr)
        logger.info("[INIT] Отправлен инициализирующий RTP silence-пакет для %s", addr)

        # Запускаем write_loop
        self.write_task = asyncio.create_task(
            write_loop(self.transport, self.remote_addr, self.write_queue, self)
        )
```

**Почему это работает:**
- UnicastRTP получит первый RTP пакет от audiosocket
- После этого Asterisk начнёт отправлять RTP ответные пакеты
- RTP поток запустится
- Сессия к OpenAI уже создана (UUID сгенерирован)

**Плюсы:**
- ✅ Минимальные изменения кода (одна строка + лог)
- ✅ Не требует пересборки Asterisk
- ✅ Работает с динамическими IP Docker
- ✅ Соответствует RTP best practices (sender может отправить первым)
- ✅ Silence-пакет не слышен для абонента

**Минусы:**
- ⚠️ Нужно протестировать что Asterisk корректно обработает "паразитный" пакет
- ⚠️ Может незначительно увеличить задержку старта потока на ~20ms

**Альтернатива (если выше не сработает):**
Отправить пустой RTP пакет без payload:
```python
import struct
# RTP header: V=2, P=0, X=0, CC=0, M=0, PT=8 (PCMA), seq=0, ts=0, ssrc=0x12345678
rtp_header = struct.pack(">BBHII", 0x80, 8, 0, 0, 0x12345678)
transport.sendto(rtp_header, addr)
```

#### Вариант 2: Использовать статические IP для контейнеров

```yaml
# docker-compose.yml
services:
  asterisk:
    networks:
      ai_voice_net:
        ipv4_address: 172.18.0.10  # статический IP

  audiosocket:
    networks:
      ai_voice_net:
        ipv4_address: 172.18.0.20  # статический IP
```

**Плюсы:**
- IP не меняется при рестарте
- Можно использовать hostname в docker-compose

**Минусы:**
- Жёсткая привязка к IP
- Возможны конфликты с другими сервисами в сети

#### Вариант 3: Использовать Local канал вместо ExternalMedia

Вместо создания ExternalMedia через ARI, использовать `Local` канал с Dial:

```python
# Вместо ExternalMedia
local_channel = f"Local/audiosocket@{external_host}"
await self.ari_client.create_channel(
    endpoint=local_channel,
    app=self.app_name,
    channel_id=f"local-{session_uuid}",
    other_local_channel=session_uuid,
)
```

**Плюсы:**
- Local канал может инициировать исходящий RTP
- Стандартный механизм Asterisk

**Минусы:**
- Требует переработки логики audiosocket
- Сложнее в отладке

#### Вариант 4: Использовать fmt=slin16 вместо alaw

В некоторых случаях Asterisk лучше работает с native форматом (slin16), а не с PCMA.

#### Вариант 5: Исследовать Asterisk модуль `chan_rtp`

Проверить, есть ли настройки для "aggressive RTP sending" или "immediate RTP".

---

## 12. 📋 CHECKLIST ДЛЯ ДАЛЬНЕЙШЕГО ИССЛЕДОВАНИЯ

1. **Попробовать отправить первый RTP из audiosocket**
   - Добавить функцию `send_keepalive_rtp()` в UdpProtocol
   - Вызывать её через 1 секунду после создания ExternalMedia
   - Проверить начнётся ли RTP поток

2. **Настроить статические IP для контейнеров**
   - Добавить `ipv4_address` в docker-compose.yml
   - Проверить что конфигурация работает

3. **Исследовать session_uuid передача**
   - Проверить передаётся ли `data` параметр в RTP
   - Может нужно использовать `app` параметр для передачи UUID

4. **Протестировать исходящий звонок**
   - Проверить SIP trace для +79202119023
   - Уточнить формат номера (может нужен префикс)

5. **Альтернативные подходы**
   - Попробовать `direction=out` вместо `both`
   - Исследовать `encapsulation=audiosocket` вместо `rtp`
   - Проверить Asterisk документацию по ExternalMedia

---

## 13. 🔗 ПОЛЕЗНЫЕ ССЫЛКИ

- **Исходный код audiosocket:** `/asterisk/media_sockets/main.py` (21KB)
- **Docker compose config:** `/asterisk/docker-compose.yml`
- **Backend ARI client:** `/asterisk/backend/app/ari_client.py`
- **Asterisk конфигурация:** `/asterisk/asterisk/etc/asterisk/pjsip.conf`
- **Рабочая конфигурация Билайн:** https://telephonization.ru/blog/tpost/s4aagdfr51-freepbx-asterisk-beeline-pjsip-trank-s-r (октябрь 2023)
