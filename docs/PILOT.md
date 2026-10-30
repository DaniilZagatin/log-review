# Запуск самостоятельного R07

Распакуйте полный ZIP в пустой каталог, перейдите в папку `process-observability`. Предыдущие архивы не требуются. Python 3.11+; команды из README работают на macOS/Linux, в PowerShell замените `python3` на `py -3`.

## Сначала локальная проверка

```bash
python3 -B scripts/release_full.py verify --zip /path/to/process-observability-mvp-r07-full.zip
python3 -B run.py demo --out output/demo-01
python3 -B run.py export-onboarding --run output/demo-01 --out output/export-01
python3 -B scripts/test_offline.py
```

Демо и тесты используют искусственные данные. Корпоративный TLS ими не проверяется. В архиве нет пользовательских бизнес-описаний, логов, сертификатов, ключей и истории пилота. Сохранённая историческая команда `--process p3283` требует отдельного предоставленного описания; для нового переноса используйте явный `--business-file`.

## Настройка модели

Создайте локальную копию config/model.example.json. Для обеих ролей `code`/`business` задайте `base_url`, `model`, `thinking`, `max_output_tokens`. `api_key_env` — имя переменной окружения, а не значение ключа. Если шлюз требует mTLS, дополните профиль полями `ca_bundle`, `client_cert`, `client_key`; пути разрешаются относительно профиля. Взаимная проверка сертификатов и доступность реальной сети остаются проверкой принимающей среды.

`python3 -B run.py check-model --config /path/to/local-model.json` выполняет настоящие сетевые запросы. Запускайте его в пределах разрешения пользователя. Не используйте адреса `.invalid` как рабочие.

## План и анализ

```bash
python3 -B run.py analyze --repo /path/to/source --include '*.py' --plan-only --out output/plan-01
python3 -B run.py analyze --config /path/to/local-model.json --repo /path/to/source --include '*.py' --business-file /path/to/business.md --goal 'Обработка заказа' --map-mode atomic --atomic-page-units 10 --triage auto --compose on --refine-max-requests 64 --out output/pilot-01
```

Сначала выбирается область исходников, затем идёт технический разбор и сводки. Новый обычный бизнес-путь: каркас этапов → прямые атомарные операции → покрытие → доработки → метрики. Малые пачки определяются проверенными единицами команд и нужными зависимостями. Индекс Python не содержит правил Saleor; остальные языки явно tentative. Даже200 запросов могут не покрыть весь большой каталог; deferred не исчезают.

После прерывания повторите параметры с `--resume-from output/pilot-01 --out output/pilot-02`. Точный технический кэш и принятые атомарные страницы проверяются по идентичности. Смена модели, источников, валидатора или запроса может потребовать новые вызовы. Старые coarse-ответы не становятся atomic-ответами.

Экспорт без модели:

```bash
python3 -B run.py export-onboarding --run output/pilot-01 --out output/export-pilot-01
python3 -B run.py feedback --run output/pilot-01 --out output/feedback-pilot-01.zip
```

Код1 может означать честно сохранённый partial. Проверьте manifest.json, diagnostics.json, atomic_map.json, command_units.json, action_catalog.json и отчёт. Повторная доставка или error-only запись не доказывает новый успешный результат. Определения метрик не равны измеренным значениям.
