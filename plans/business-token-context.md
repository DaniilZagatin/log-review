# Бюджет бизнес-контекста в токенах

Пользователь попросил окно 1 млн токенов и явно выбрал остановку run16 с повтором бизнес-стадий. Технический разбор и сводки переиспользуются только после проверки настоящих checkpoints. Run16 завершён намеренно с130; его результаты и runtime неизменны.

1. Добавить opt-in счётчик по локальному tokenizer.json, без remote code и автоматической сети. Общий бюджет включает вход, резерв chat framing и настроенный максимальный ответ. Бюджет символов остаётся отдельным ограничением памяти. По умолчанию прежний путь без зависимости.
2. Подключить проверку к packing/map/refinement и к окончательной отправке обоих attempts. Идентичность tokenizer/бюджета входит в конфигурацию; технический кеш не меняется. Зарезервировать full business text до кандидатов; остаток кандидатов идёт на следующие страницы или явно deferred.
3. RED/GREEN на границы/Unicode/repair/неизменность текста/учёт страниц; full offline, strict demo/export. Новый frozen runtime и production-path preflight с307technical/536summaries/8857leaves, нулём новых технических вызовов и неизменностью ledger.
4. Новый обычный approval и бизнес-запуск с теми же моделью/low/output131072, ценами и throughput/fallbacks/ignoreRelace. Провайдеры должны поддерживать окно и вывод. Сохранить историю/unknown reserves/window20. Независимая живая приёмка отдельно; synthetic preflight не становится источником продолжения.

Область: token_budget.py, workflow.py, action_refinement.py, cli.py, их тесты и документация; task-local runner/frozen runtime. Обязательные schema/source/identity проверки не ослаблять. Невалидные операции не принимать ради успешного статуса. ZIP R06 и предыдущие поставки неизменны.

Статус: реализация/offline приёмка завершены.450full/strictdemo/export/hash/негативные guards PASS; реальнаяofflineстраница698кандидатов/fullbusiness/835063texttokens,0API. Ускоренный prefix search проверен до/после (18проверок старого алгоритма против не более8на том жеfixture). Новый run17 normally approved, unified18915/PID88201/caffeinate88202. До первого live businessIO подтверждены307technical+536summaries/8857leaves/6failed и0новыхтехническихnetworkcalls. Бизнес-стадии выполняются; живая смысловая/E2Eприёмка остаётся отдельной.
