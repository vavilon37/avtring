# Авито Рингер

Telegram-бот для мониторинга новых объявлений на Авито.

## Установка

### 1. Установи Python 3.11+

### 2. Установи зависимости
```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Создай бота в Telegram
- Открой [@BotFather](https://t.me/BotFather)
- `/newbot` → введи имя → получи токен

### 4. Настрой .env
```bash
copy .env.example .env
```
Открой `.env` и вставь токен:
```
BOT_TOKEN=1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
CHECK_INTERVAL=60
```

`CHECK_INTERVAL` — интервал проверки в секундах (минимум рекомендуется 60).

### 5. Запуск
```bash
python main.py
```

## Использование

1. Зайди на [avito.ru](https://avito.ru), настрой поиск с нужными фильтрами
2. Скопируй URL из адресной строки браузера
3. В боте нажми `/add` и отправь ссылку
4. Бот будет присылать новые объявления сразу как они появятся

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Начало работы |
| `/add` | Добавить новый поиск |
| `/list` | Список активных поисков |
| `/stop` | Удалить поиск |
| `/help` | Помощь |

## Структура проекта

```
avito pars/
├── main.py        # Точка входа
├── bot.py         # Telegram бот (aiogram)
├── parser.py      # Парсинг Авито (playwright)
├── monitor.py     # Фоновый мониторинг
├── database.py    # SQLite база данных
├── requirements.txt
├── .env.example
└── .env           # Твои настройки (не коммитить!)
```

## Заметки

- Бот открывает реальный браузер (Chromium headless) с anti-detect — это помогает обходить защиту Авито
- Все объявления хранятся в `avito_ringer.db` — дубликаты не отправляются
- До 10 поисков на пользователя
