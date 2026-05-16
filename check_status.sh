#!/bin/bash
echo "🔍 Проверка здоровья Zvonko Bot"
echo "================================"

# Бот
if systemctl is-active --quiet zvonko-bot; then
    echo "✅ Бот работает"
else
    echo "❌ Бот не работает"
fi

# Nginx
if systemctl is-active --quiet nginx; then
    echo "✅ Nginx работает"
else
    echo "❌ Nginx не работает"
fi

# Flask
if curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/ | grep -q 200; then
    echo "✅ Flask отвечает (200)"
else
    echo "❌ Flask не отвечает"
fi

# Домен
if curl -s -o /dev/null -w '%{http_code}' https://pavlosos.online/ | grep -q 200; then
    echo "✅ Домен доступен (200)"
else
    echo "❌ Домен недоступен"
fi

# Процессы
PROC_COUNT=$(ps aux | grep -c "[p]ython.*telebot")
echo "📊 Процессов Python: $PROC_COUNT"

# Порт 5000
if lsof -i :5000 >/dev/null 2>&1; then
    echo "✅ Порт 5000 занят (Flask работает)"
else
    echo "❌ Порт 5000 свободен"
fi

echo "================================"
echo "📝 Последние 5 строк лога:"
tail -5 /root/CascadeProjects/MusicTelegramBot/bot.log 2>/dev/null || echo "Логи недоступны"
