# Защита Asterisk от брутфорса

## Что настроено:

### 1. Fail2Ban (установлен, jails для Asterisk созданы)
- `/etc/fail2ban/jail.d/asterisk.conf` - правила jails (пока отключены, включите позже)
- `/etc/fail2ban/filter.d/asterisk.conf` - фильтры для Asterisk
- `/etc/fail2ban/filter.d/asterisk-nat.conf` - фильтры для NAT

### 2. IPTables правила (активны)
```
- Rate limiting SIP порта 5060: 10 запросов/сек, burst 20
- RTP порты 10000-10100 открыты
- Локальные сети разрешены
```

### 3. Скрипт мониторинга (systemd service)
- `/usr/local/bin/asterisk-security.sh` - мониторинг логов
- `/etc/systemd/system/asterisk-security.service` - systemd unit
- Блокирует IP после 5 неудачных попыток на 1 час

### 4. Логи
- `/var/log/asterisk/security_log` - лог защиты
- `/var/log/asterisk/messages` - логи Asterisk

## Управление:

```bash
# Проверить статус защиты
systemctl status asterisk-security.service

# Перезапустить защиту
systemctl restart asterisk-security.service

# Просмотреть заблокированные IP
iptables -L -n | grep DROP

# Разблокировать IP (замените x.x.x.x)
iptables -D INPUT -s x.x.x.x -j DROP

# Проверить fail2ban jails
fail2ban-client status

# Включить fail2ban jail для Asterisk (когда контейнер запущен)
sed -i 's/enabled = false/enabled = true/' /etc/fail2ban/jail.d/asterisk.conf
systemctl reload fail2ban
```

## Рекомендации:

1. **Используйте сильные пароли** для SIP акков (сейчас 1001pass - СМЕНИТЕ!)
2. **Ограничьте доступ по IP** в firewall, если возможно
3. **Мониторьте логи** регулярно: `tail -f /var/log/asterisk/security_log`
4. **Рассмотрите VPN** для доступа к SIP серверу

## Текущий белый IP: 109.172.46.197
