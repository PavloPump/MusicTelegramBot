#!/bin/bash

echo "==================================="
echo "Music Telegram Bot + Web Panel"
echo "==================================="
echo ""

if [ ! -f ".env" ]; then
    echo "⚠️  Файл .env не найден!"
    echo "Создаю .env из .env.template..."
    cp .env.template .env
    echo ""
    echo "✅ Файл .env создан с настройками по умолчанию:"
    echo "   - TELEGRAM_TOKEN уже настроен"
    echo "   - YouTube Music API не требует ключей"
    echo "   - Genius API опционален (для текстов песен)"
    echo ""
    echo "Бот готов к запуску!"
    echo ""
    sleep 2
fi

if [ ! -d "venv" ]; then
    echo "📦 Создание виртуального окружения..."
    python3 -m venv venv
fi

echo "📦 Активация виртуального окружения..."
source venv/bin/activate

echo "📦 Установка зависимостей..."
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "✅ Готово!"
echo ""
echo "🎵 Запуск бота и веб-панели..."
echo "   Telegram Bot: активен"
echo "   Web Panel: http://localhost:5000"
echo "   Поиск музыки: YouTube Music API"
echo "   Скачивание: yt-dlp"
echo ""
echo "Нажмите Ctrl+C для остановки"
echo ""

python -m telebot_app.app
