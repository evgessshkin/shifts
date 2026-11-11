# Shifts bot

Telegram-бот для смен на складе. Работает на EC2 как systemd-сервис `shifts-bot`.

## Подключиться к серверу

```bash
cd ~/Downloads
ssh -i shifts.pem ubuntu@ec2-16-171-26-199.eu-north-1.compute.amazonaws.com
```

## Управление ботом (на сервере)

```bash
sudo systemctl start shifts-bot      # запустить
sudo systemctl stop shifts-bot       # остановить
sudo systemctl restart shifts-bot    # перезапустить (после изменений кода или .env)
systemctl status shifts-bot          # статус

sudo systemctl enable --now shifts-bot   # включить автозапуск и сразу запустить
sudo systemctl disable shifts-bot        # убрать из автозапуска
```

Сервис настроен с `Restart=always`: если просто убить процесс, через 30 секунд он запустится снова. Чтобы остановить бота, используй `systemctl stop`.

## Логи

```bash
journalctl -u shifts-bot -f              # смотреть вживую
journalctl -u shifts-bot -n 100          # последние 100 строк
journalctl -u shifts-bot --since today   # за сегодня
```

## Обновить код на сервере

С Мака:

```bash
scp -i ~/Downloads/shifts.pem bot.py salary.py shifts_site.py requirements.txt payslips.json \
    ubuntu@ec2-16-171-26-199.eu-north-1.compute.amazonaws.com:~/shifts/
```

На сервере:

```bash
cd ~/shifts
.venv/bin/pip install -r requirements.txt   # если менялись зависимости
sudo systemctl restart shifts-bot
```

## Первая установка на сервер

```bash
cd ~/shifts
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
nano .env                                   # настройки, см. ниже
sudo cp deploy/shifts-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now shifts-bot
```

## Настройки (.env)

| Переменная | Что это |
|---|---|
| `SHIFTS_URL` | адрес страницы со сменами |
| `SHIFTS_COOKIE` | заголовок Cookie из браузера |
| `SHIFTS_USER_AGENT` | User-Agent того же браузера |
| `SHIFTS_LOGIN`, `SHIFTS_PASSWORD` | необязательно; нужны для автоматического перелогина |
| `TG_TOKEN` | токен бота от @BotFather |
| `TG_CHAT_ID` | твой chat id |
| `AUTO_SUBSCRIBE` | `1` — записываться на смены автоматически |
| `LUNCH` | `yes` / `no` при записи |
| `BOOK_ONLY` | записываться только в отдел с этим текстом (по умолчанию `IMPORT`) |
| `HOURLY_RATE` | брутто за час для `/salary` (по умолчанию `6.5`) |

## Зарплата (`/salary`)

Считается по правилам dohoda o brigádnickej práci študentov (Словакия):
часы смены минус 30 мин перерыва, ×0,98 (туалет, отметка), доплаты за
субботу 50 %, воскресенье и праздник 100 %, ночь 40 % от минимальной
часовой ставки; взносы 4 % + 3 %; налог 19 % сверх необлагаемой части;
обед 1 € вычитается из выплаты. Правила и праздники по годам — в `salary.py`.

Реальные суммы из výplatná páska дописывай в `payslips.json` (в git не
попадает), бот покажет их вместо оценки:

```json
{"2026-10": {"gross": 500.00, "net": 465.00, "payout": 455.00}}
```

## Переподбор смен на выходные

`/settings` → «Turn weekend reselect ON». Бот отменяет записанную смену
(только IMPORT, только если ещё можно отменить) и берёт более выгодную смену
в субботу, воскресенье или праздник, если обе из-за правила 10 часов отдыха
взять нельзя. Две смены на выходных он не меняет на одну. Если после отмены
взять новую смену не получилось, бот пытается вернуть отменённую и пишет об
этом. Export — только уведомления.

## Локальный запуск (Мак)

```bash
python bot.py                    # обычный запуск
python bot.py saved_page.html    # тест на сохранённой странице
```

**Не запускай бота локально, пока он работает на сервере:** два экземпляра с одним токеном конфликтуют на `getUpdates`. Сначала выполни `sudo systemctl stop shifts-bot` на сервере.
