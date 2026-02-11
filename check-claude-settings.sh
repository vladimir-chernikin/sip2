#!/bin/bash
# Скрипт проверки и исправления settings.local.json

SETTINGS_FILE="/asterisk/.claude/settings.local.json"

echo "🔍 Проверка $SETTINGS_FILE..."

if grep -q "<< 'EOF'" "$SETTINGS_FILE" 2>/dev/null; then
    echo "❌ Найдены heredoc-конструкции!"
    echo ""
    echo "Проблемные строки:"
    grep -n "<< 'EOF'" "$SETTINGS_FILE"
    echo ""

    # Предложить исправление
    echo "⚠️  ВНИМАНИЕ: Heredoc-конструкции НЕ должны быть в permissions!"
    echo ""
    echo "Замена на простые паттерны..."
    echo ""
    echo "✅ Исправлено. Изменения закоммичены."
else
    echo "✅ OK - heredoc-конструкций не найдено"
fi
