#!/bin/bash
# Автоматическая очистка heredoc-конструкций из settings.local.json

SETTINGS_FILE="/asterisk/.claude/settings.local.json"
BACKUP_FILE="/asterisk/.claude/settings.local.json.backup"

# Проверяем наличие файла
if [ ! -f "$SETTINGS_FILE" ]; then
    exit 0
fi

# Проверяем наличие heredoc-конструкций
if ! grep -q "<< 'EOF'" "$SETTINGS_FILE" 2>/dev/null; then
    exit 0
fi

# Создаём резервную копию
cp "$SETTINGS_FILE" "$BACKUP_FILE"

# Удаляем строки с heredoc-конструкциями с помощью Python
python3 << 'PYTHON_EOF'
import json
import re
import sys

settings_file = "/asterisk/.claude/settings.local.json"

try:
    with open(settings_file, 'r') as f:
        data = json.load(f)

    if 'permissions' in data and 'allow' in data['permissions']:
        # Фильтруем правила: удаляем те, что содержат heredoc
        filtered_rules = []
        for rule in data['permissions']['allow']:
            if "<< 'EOF'" not in rule:
                filtered_rules.append(rule)

        # Обновляем данные
        data['permissions']['allow'] = filtered_rules

        # Удаляем дубликаты
        data['permissions']['allow'] = list(dict.fromkeys(data['permissions']['allow']))

        # Сохраняем
        with open(settings_file, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        print("✅ Heredoc-конструкции удалены")
except Exception as e:
    print(f"❌ Ошибка: {e}", file=sys.stderr)
    sys.exit(1)
PYTHON_EOF

if [ $? -eq 0 ]; then
    echo "✅ Settings очищены от heredoc-конструкций"
    rm -f "$BACKUP_FILE"
else
    echo "❌ Ошибка очистки, восстановлен backup"
    mv "$BACKUP_FILE" "$SETTINGS_FILE"
    exit 1
fi
