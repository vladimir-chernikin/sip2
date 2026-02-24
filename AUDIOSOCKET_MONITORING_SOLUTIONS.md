# 🔧 AudioSocket Monitoring Solutions

## 🚨 ПРОБЛЕМА

AudioSocket зависал через 14 часов работы:
- UDP port 7575 НЕ принимал пакеты
- Логи не писались
- RTP не flows
- Звонки не работали

**Симптомы:**
```
docker logs --since 10m audiosocket  # 0 строк
docker exec audiosocket netstat -tuln | grep 7575  # ПУСТО!
```

---

## 💡 РЕШЕНИЯ

### **ВАРИАНТ 1: Health Check с Auto-Restart** ✅ РЕКОМЕНДУЕТСЯ

**Суть:** Периодическая проверка health endpoint + автоматический перезапуск

```python
# Добавить в audiosocket/main.py

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    try:
        # Проверяем что UDP сервер работает
        if udp_server and udp_server.sockets:
            # Проверяем что порт слушается
            for sock in udp_server.sockets:
                if sock.getsockname()[1] == 7575:
                    return {"status": "ok", "udp": "listening"}
        return {"status": "error", "udp": "not listening"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# Добавить в docker-compose.yml для audiosocket:
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:8888/health"]
  interval: 30s
  timeout: 10s
  retries: 3
  start_period: 40s
```

**Плюсы:**
- ✅ Docker автоматически перезапустит контейнер
- ✅ Простая реализация
- ✅ Не требует дополнительных сервисов

**Минусы:**
- ⚠️ Звонок может оборваться на полуслове
- ⚠️ 30-40 секунд downtime

---

### **ВАРИАНТ 2: Watchdog Process** ⭐️ ОПТИМАЛЬНЫЙ

**Суть:** Отдельный процесс который мониторит UDP порт и перезапускает сервис

```python
# watchdog.py в audiosocket/

import asyncio
import logging
import socket
from datetime import datetime

logger = logging.getLogger(__name__)

async def watchdog_check(interval: int = 60):
    """Проверяет что UDP порт 7575 отвечает."""
    while True:
        try:
            # Пробуем отправить тестовый UDP пакет
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1)
            sock.sendto(b"ping", ("127.0.0.1", 7575))
            sock.close()

            logger.info(f"[WATCHDOG] OK - UDP port 7575 responding - {datetime.now()}")
        except Exception as e:
            logger.error(f"[WATCHDOG] FAIL - UDP port 7575 NOT responding: {e}")
            logger.critical("[WATCHDOG] Restarting...")
            os._exit(1)  # Docker перезапустит контейнер

        await asyncio.sleep(interval)

# В main.py добавить:
asyncio.create_task(watchdog_check(interval=60))
```

**Плюсы:**
- ✅ Быстрая реакция (60 секунд)
- ✅ Логирует проблему
- ✅ Перезапускает только если реальная проблема

**Минусы:**
- ⚠️ Все еще теряются звонки на время проверки

---

### **ВАРИАНТ 3: External Monitoring** 🔥 ПРОДАКШН

**Суть:** Prometheus + Grafana + Alertmanager

```yaml
# prometheus.yml

scrape_configs:
  - job_name: 'audiosocket'
    static_configs:
      - targets: ['audiosocket:8888']
    metrics_path: '/metrics'

# Добавить в audiosocket:
from prometheus_client import Counter, Histogram, start_http_server

REQUESTS = Counter('audiosocket_requests_total', 'Total requests')
PACKETS = Counter('audiosocket_rtp_packets_total', 'RTP packets received')
ACTIVE_SESSIONS = Gauge('audiosocket_active_sessions', 'Active sessions')

# Правило alertа в Alertmanager:
- alert: AudioSocketDown
  expr: up{job="audiosocket"} == 0
  for: 1m
  annotations:
    summary: "AudioSocket is DOWN!"
```

**Плюсы:**
- ✅ Продакшн-мониторинг
- ✅ Графики, метрики, алерты
- ✅ Интеграция с PagerDuty/Slack

**Минусы:**
- ❌ Сложная настройка
- ❌ Требует дополнительные контейнеры

---

### **ВАРИАНТ 4: Heartbeat с Asterisk**

**Суть:** Asterisk отправляет heartbeat каждые 30 секунд, AudioSocket отвечает

```python
# В audiosocket/main.py

last_heartbeat = datetime.now()

@app.post("/heartbeat")
async def heartbeat():
    """Heartbeat от Asterisk."""
    global last_heartbeat
    last_heartbeat = datetime.now()
    return {"status": "alive"}

async def heartbeat_monitor():
    """Проверяет heartbeat от Asterisk."""
    while True:
        if (datetime.now() - last_heartbeat).seconds > 60:
            logger.critical("[HEARTBEAT] No heartbeat from Asterisk!")
            os._exit(1)
        await asyncio.sleep(30)

# В Asterisk extensions.conf добавить:
exten => h,1,DeadAGI(localhost:/tmp/heartbeat.py)
```

**Плюсы:**
- ✅ Проверяет полную связку
- ✅ Ловит проблемы в сети

**Минусы:**
- ⚠️ Asterisk тоже может зависнуть

---

## 📊 СРАВНИТЕЛЬНАЯ ТАБЛИЦА

| Вариант | Сложность | Downtime | Надежность | Рекомендация |
|---------|-----------|----------|------------|---------------|
| 1. Health Check | ⭐ | 30-40 сек | ⭐⭐⭐ | ✅ Быстрый старт |
| 2. Watchdog | ⭐⭐ | 60 сек | ⭐⭐⭐⭐ | ✅✅ ОПТИМАЛЬНО |
| 3. Prometheus | ⭐⭐⭐⭐ | 1 мин | ⭐⭐⭐⭐⭐ | 🔥 Продакшн |
| 4. Heartbeat | ⭐⭐⭐ | 30 сек | ⭐⭐⭐ | ❌ Избыточно |

---

## 🎯 МОЯ РЕКОМЕНДАЦИЯ

**ЭТАП 1 (сейчас):** Вариант 2 (Watchdog) + Вариант 1 (Health Check)
**ЭТАП 2 (когда будет время):** Вариант 3 (Prometheus)

---

## ✅ ЧЕК-ЛИСТ ВНЕДРЕНИЯ

**Watchdog:**
- [ ] Добавить watchdog_check() в main.py
- [ ] Настроить интервал проверки (60 сек)
- [ ] Добавить логирование при проверке
- [ ] Тестировать: убить процесс, проверить что перезапустится

**Health Check:**
- [ ] Добавить /health endpoint
- [ ] Добавить healthcheck в docker-compose.yml
- [ ] Настроить retries и timeout
- [ ] Тестировать: зависнуть, проверить что Docker перезапустит
