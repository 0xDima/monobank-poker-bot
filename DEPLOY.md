# Налаштування та запуск Monobank Poker Bot

Ця інструкція описує базовий процес запуску Telegram-бота на сервері Ubuntu.

## 1. Підключення до сервера

Підключіться до сервера через SSH:

```bash
ssh root@YOUR_SERVER_IP
```

⸻

2. Встановлення необхідних пакетів

Оновіть систему та встановіть Python, git і tools для віртуального середовища:
```bash
apt update
apt install -y python3 python3-pip python3-venv git
```

⸻

3. Клонування репозиторію

Створіть папку для застосунків, перейдіть у неї та склонуйте репозиторій:
```bash
mkdir -p /root/apps
cd /root/apps
git clone https://github.com/0xDima/monobank-poker-bot.git
cd monobank-poker-bot
```

⸻

4. Створення та активація віртуального середовища
```bash
python3 -m venv venv
source venv/bin/activate
```

⸻

5. Встановлення залежностей

Якщо в проєкті є requirements.txt, встановіть залежності:
```bash
pip install -r requirements.txt
```
Якщо якихось пакетів бракує, встановіть їх окремо, наприклад:
```bash
pip install requests
pip install "python-telegram-bot[job-queue]"
```
Після цього за потреби оновіть requirements.txt:
```bash
pip freeze > requirements.txt
```

⸻

6. Локальна перевірка запуску на сервері

Перед деплоєм через systemd переконайтесь, що бот запускається вручну:
```bash
python3 bot.py
```
Якщо бот стартує без критичних помилок, зупиніть його через Ctrl+C.

⸻

7. Створення systemd service

Створіть service-файл:
```bash
nano /etc/systemd/system/pokerbot.service
```
Вставте наступну конфігурацію:
```bash
[Unit]
Description=Poker Monobank Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/apps/monobank-poker-bot
ExecStart=/root/apps/monobank-poker-bot/venv/bin/python /root/apps/monobank-poker-bot/bot.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```
Збережіть файл.

⸻

8. Запуск бота як сервісу

Після створення service-файлу виконайте:
```bash
systemctl daemon-reload
systemctl enable pokerbot
systemctl start pokerbot
```

⸻

9. Перевірка статусу

Щоб перевірити, чи бот успішно працює:
```bash
systemctl status pokerbot
```
Очікуваний результат:

Active: active (running)


⸻

10. Перегляд логів

Для перегляду логів у реальному часі:
```bash
journalctl -u pokerbot -f
```
Для перегляду останніх 100 рядків логів:
```bash
journalctl -u pokerbot -n 100
```

⸻

11. Корисні команди

Перезапуск сервісу:
```bash
systemctl restart pokerbot
```
Зупинка сервісу:
```bash
systemctl stop pokerbot
```
Повторний запуск:
```bash
systemctl start pokerbot
```
Перевірка статусу:
```bash
systemctl status pokerbot
```

⸻

12. Оновлення бота після змін у репозиторії

Після оновлення коду в GitHub:
```bash
cd /root/apps/monobank-poker-bot
git pull
source venv/bin/activate
pip install -r requirements.txt
systemctl restart pokerbot
```

⸻

13. Важливі примітки
	•	Не рекомендується зберігати секрети (наприклад, Telegram Bot Token) прямо в коді.
	•	Для тестового запуску це допустимо, але для нормального деплою краще використовувати .env.
	•	Якщо токен був десь опублікований або засвітився в логах, його потрібно перевипустити через BotFather.
	•	Для простого тестового сервера можна тимчасово працювати від root, але для більш акуратного продакшн-налаштування краще створити окремого користувача.

⸻

14. Типові проблеми

Помилка ModuleNotFoundError

Означає, що у віртуальному середовищі не встановлений потрібний пакет.

Приклад:
```bash
pip install requests
```
Помилка No JobQueue set up

Означає, що python-telegram-bot встановлений без додаткових залежностей для JobQueue.

Виправлення:
```bash
pip install "python-telegram-bot[job-queue]"
```
Бот не запускається як сервіс

Перевірте:
	•	правильність шляху в WorkingDirectory
	•	правильність шляху в ExecStart
	•	логи через journalctl -u pokerbot -f

⸻
