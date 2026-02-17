# Управление PCAP файлами (Docker tcpdump)

## Текущая ситуация

### Запущенные процессы tcpdump:
```
PID 2871322: tcpdump -i any -s 0 -w /root/call_to_79202119023_20260214_150905.pcap port 5060 and host 62.141.113.134
PID 2873979: tcpdump -i any -s 0 -w /root/call_79202119023_test_20260214_151426.pcap port 5060
```

**Оба процесса запущены с 14 февраля 2026** (PPID 1 - демонизированы)

### Самые большие файлы:
| Файл | Размер | Дата создания |
|------|--------|---------------|
| `/root/sip_traffic_truncated.pcap` | **4.4 GB** | 15 фев 05:21 |
| `/root/call_79202119023_test_20260214_151426.pcap` | 2.2 GB | 14 фев 16:18 |
| `/root/external_traffic.pcap` | 36 MB | 4 окт |

---

## Остановить tcpdump

### Остановить конкретный процесс:
```bash
kill 2871322   # Остановить первый tcpdump
kill 2873979   # Остановить второй tcpdump
```

### Остановить все tcpdump:
```bash
killall tcpdump
```

### Принудительная остановка:
```bash
killall -9 tcpdump
```

---

## Настроить ротацию pcap файлов

### Автоматическая ротация (УЖЕ НАСТРОЕНО!)

**Скрипт:** `/usr/local/bin/rotate-pcap.sh`
**Cron:** `/etc/cron.d/asterisk-maintenance` (каждый час)

**Что делает:**
- Удаляет pcap файлы старше 24 часов
- Удаляет pcap файлы больше 2GB (защита от переполнения)

**Логи ротации:**
```bash
journalctl -u cron | grep rotate-pcap
```

### Ручная ротация через logrotate (опционально):

Создать `/etc/logrotate.d/pcap`:
```
/root/*.pcap {
    daily
    rotate 1
    maxage 1
    missingok
    notifempty
    compress
    delaycompress
    size 100M
}
```

---

## Отключить автоматический захват

### 1. Проверить автоматический запуск:
```bash
# Проверить systemd
systemctl list-timers | grep -i dump

# Проверить cron
crontab -l
cat /etc/cron.d/* | grep tcpdump
```

### 2. Проверить Docker контейнеры:
```bash
# Если tcpdump запускается внутри контейнера
docker exec asterisk ps aux | grep tcpdump
```

### 3. Удалить из автозагрузки:
```bash
# Если это systemd service
systemctl disable tcpdump-capture
systemctl stop tcpdump-capture

# Если это cron
crontab -e  # Удалить строку с tcpdump
```

---

## Как правильно запускать tcpdump (с ограничением!)

### ❌ ПЛОХО (без ограничений):
```bash
tcpdump -i any -s 0 -w /root/capture.pcap port 5060
```

### ✅ ХОРОШО (с ограничением размера и времени):
```bash
# Ограничение по размеру (100MB) и количеству пакетов
tcpdump -i any -s 0 -w /root/capture.pcap -C 100 -W 5 port 5060

# Ограничение по времени (60 секунд)
timeout 60 tcpdump -i any -s 0 -w /root/capture.pcap port 5060

# Ограничение по количеству пакетов (10000)
tcpdump -i any -s 0 -w /root/capture.pcap -c 10000 port 5060
```

### Параметры:
- `-C 100` - создавать новый файл каждые 100 MB
- `-W 5` - хранить максимум 5 файлов
- `-c 10000` - захватить 10000 пакетов и остановиться
- `timeout 60` - остановиться через 60 секунд

---

## Проверка

### Проверить активные захваты:
```bash
ps aux | grep tcpdump
ls -lh /root/*.pcap
```

### Проверить размер pcap файлов:
```bash
du -sh /root/*.pcap | sort -rh
```

### Проверить диск:
```bash
df -h /root
```

---

## Рекомендация

**УДАЛИТЬ старые pcap файлы сейчас:**
```bash
# Удалить файлы старше 1 дня
find /root -name "*.pcap" -mtime +1 -delete

# Удалить файлы больше 1GB
find /root -name "*.pcap" -size +1G -delete

# Или удалить все pcap файлы (ОСТОРОЖНО!)
rm /root/*.pcap
```

**Остановить dangling tcpdump процессы:**
```bash
killall tcpdump
```

---

## Автоматическая очистка (УЖЕ НАСТРОЕНО!)

### Docker cache cleanup:
- **Скрипт:** `/usr/local/bin/cleanup-docker-cache.sh`
- **Cron:** Ежедневно в 3:00 утра
- **Что делает:** Удаляет build cache старше 24 часов

### Pcap rotation:
- **Скрипт:** `/usr/local/bin/rotate-pcap.sh`
- **Cron:** Каждый час
- **Что делает:** Удаляет pcap файлы старше 24 часов и больше 2GB

### Проверка cron jobs:
```bash
cat /etc/cron.d/asterisk-maintenance
```
