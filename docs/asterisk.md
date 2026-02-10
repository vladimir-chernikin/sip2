# Контейнер `asterisk`: IP-АТС и медиашлюз

## Роль контейнера

Контейнер `asterisk` — это сердце телефонии в нашей системе. Он выполняет роль IP-АТС (телефонной станции), которая:
- Принимает SIP-звонки от клиентов (например, MicroSIP)
- Управляет RTP-потоками (аудио)
- Предоставляет ARI (Asterisk REST Interface) для программного управления

## Технологии внутри контейнера

### 1. Asterisk 20
- **Версия**: Asterisk 20 (собирается из исходников в Dockerfile)
- **Назначение**: IP-АТС с поддержкой ARI и externalMedia
- **Протоколы**: SIP/PJSIP, RTP, HTTP/WebSocket (для ARI)

### 2. PJSIP (современный SIP-стек)
- **Файл конфигурации**: `asterisk/etc/asterisk/pjsip.conf`
- **Роль**: Обработка SIP-регистраций и вызовов
- **Endpoint**: `1001` (для регистрации MicroSIP)
  - Username: `1001`
  - Password: `1001pass`
  - Порт: `5060/UDP` (проброшен на хост)

### 3. Dialplan (диалектный план)
- **Файл**: `asterisk/etc/asterisk/extensions.conf`
- **Роль**: Определяет логику обработки вызовов
- **Номер для AI-ассистента**: `7000`
  - При звонке на `7000` вызов передаётся в ARI-приложение `ai_app`

### 4. ARI (Asterisk REST Interface)
- **Файл конфигурации**: `asterisk/etc/asterisk/ari.conf`
- **HTTP-сервер**: `asterisk/etc/asterisk/http.conf` (порт `8088`)
- **Роль**: REST API и WebSocket для управления Asterisk извне
- **Доступ**: 
  - Пользователь: `admin`
  - Пароль: `admin123`
  - URL внутри Docker: `http://asterisk:8088/ari`
  - URL с хоста: `http://localhost:8088/ari`

### 5. RTP (Real-time Transport Protocol)
- **Файл конфигурации**: `asterisk/etc/asterisk/rtp.conf`
- **Диапазон портов**: `10000-10100/UDP` (проброшены на хост)
- **Роль**: Передача аудио между участниками звонка
- **Формат аудио**: G.711 A-law (8 кГц, 1 байт на сэмпл)

## Структура файлов контейнера

```
asterisk/
├── Dockerfile                    # Сборка Asterisk 20 с ARI
└── etc/asterisk/
    ├── pjsip.conf               # PJSIP конфигурация (endpoint 1001)
    ├── extensions.conf           # Dialplan (номер 7000 → ARI)
    ├── ari.conf                  # ARI настройки (admin/admin123)
    ├── http.conf                 # HTTP сервер (порт 8088)
    ├── rtp.conf                  # RTP порты (10000-10100)
    └── sip.conf                  # Старая SIP конфигурация (для совместимости)
```

## Порты контейнера

| Порт | Протокол | Назначение | Доступ |
|------|----------|------------|--------|
| 5060 | UDP | SIP-сервер | `localhost:5060` (для MicroSIP) |
| 8088 | TCP | ARI HTTP/WS | `localhost:8088` (для backend) |
| 10000-10100 | UDP | RTP-порты | `localhost:10000-10100` (для аудио) |

## Как работает контейнер

### 1. При регистрации MicroSIP
- MicroSIP отправляет SIP REGISTER на `127.0.0.1:5060`
- Asterisk проверяет credentials (`1001`/`1001pass`) по `pjsip.conf`
- При успехе регистрирует endpoint и готов принимать звонки

### 2. При звонке на 7000
- MicroSIP отправляет SIP INVITE на номер `7000`
- Asterisk обрабатывает по `extensions.conf`:
  - Находит правило для `7000`
  - Передаёт вызов в ARI-приложение `ai_app`
  - Генерирует событие `StasisStart` для backend

### 3. При создании externalMedia
- Backend через ARI создаёт канал `externalMedia`
- Asterisk создаёт виртуальный канал, который:
  - Подключается к `audiosocket:7575` по UDP/RTP
  - Передаёт туда аудио в формате G.711 A-law
  - Принимает обратно аудио от медиасервера

### 4. При работе bridge
- Backend создаёт mixing bridge
- В bridge добавляются:
  - Канал звонящего (MicroSIP)
  - Канал externalMedia (к audiosocket)
- Asterisk транскодирует аудио между каналами
- Аудио идёт: MicroSIP ↔ Asterisk ↔ externalMedia ↔ audiosocket

## Взаимодействие с другими контейнерами

- **С `backend`**: 
  - Backend подключается к ARI WebSocket (`ws://asterisk:8088/ari/events`)
  - Получает события (`StasisStart`, `StasisEnd`, `ChannelDestroyed`)
  - Управляет Asterisk через REST API (`POST /bridges`, `POST /channels/externalMedia`)

- **С `audiosocket`**:
  - Канал `externalMedia` отправляет RTP на `audiosocket:7575`
  - Принимает RTP обратно от `audiosocket`
  - Аудио передаётся в формате G.711 A-law (8 кГц)

## Важные особенности

1. **Изоляция**: Asterisk работает внутри Docker, но порты проброшены на хост, поэтому MicroSIP видит его как обычный SIP-сервер на `127.0.0.1:5060`

2. **ARI**: Позволяет программно управлять Asterisk без написания Asterisk-приложений на C

3. **ExternalMedia**: Специальный тип канала, который может отправлять/принимать RTP на внешний сервер (наш `audiosocket`)

4. **Mixing Bridge**: Объединяет несколько каналов в один аудиопоток, автоматически транскодируя форматы

## Переменные окружения

Контейнер `asterisk` не требует переменных окружения — всё настраивается через конфигурационные файлы.

## Логирование

Логи Asterisk можно посмотреть:
```bash
docker logs asterisk
docker logs -f asterisk  # в реальном времени
```
