# Ограниченное чтение для атомарного разбора

`atomic_sources.AtomicSources` — детерминированный инструмент для **существующего** атомарного цикла. Он не запускает модель, не строит карту, не выполняет приложение и не читает файловую систему. Вход — уже отобранный и замороженный `RepoSnapshot` и точные адреса единиц кода.

Текущий статус: инструмент реализован и проверяется отдельно. **Проводки выбора следующего источника моделью в основной цикл ещё нет.** Этот модуль сам по себе не исправляет связность существующих отчётов и не является основанием для нового платного прогона.

- `index(offset=0, limit=24, query=None)` выдаёт постраничный навигационный реестр единиц: ID, путь, символ и диапазон. `query` — буквальный поиск по имени/пути, без регулярных выражений и смыслового сопоставления.
- `search(query, offset=0, limit=24)` ищет буквальный текст в выбранном замаскированном снимке, возвращает точные строки и возможные содержащие единицы. Считает полный результат, но хранит только выбранную страницу совпадений. Поиск не доказывает вызов, принадлежность действия или порядок.
- `read(path, start_line, end_line)` и `read_unit(id)` выдают запрошенный текст целиком с номерами строк, хешем исходника, снимка и выданного текста. Используется только `SourceFile.text`, содержащий замаскированные данные. `_raw_text` не читается.
- `supplied_ranges()` и `can_cite(path, first, last)` учитывают только успешные чтения. Найденная поиском строка не становится разрешённой цитатой автоматически. Соседние интервалы объединяются, непрочитанные промежутки не заполняются. Повторное чтение имеет `new_lines=0`, `seen_before=true`; это не продвижение смыслового анализа.
- `fork()` позволяет проверить предлагаемое чтение, не меняя основной реестр. Последовательный оркестратор принимает согласованный preview через `adopt(preview)`; устаревший, повторно принятый или чужой preview отклоняется. Выданные инструментом строки ещё не доказывают доставку модели: перед следующим модельным запросом оркестратор обязан включить текст и проверить весь запрос, а цитировать разрешает только диапазоны, реально содержащиеся в этом запросе.

При поиске неподдерживаемые пути выбранного снимка пропускаются с явным счётчиком `unsupported_paths`; прямое чтение такого пути по-прежнему запрещено. Это не объявляет пропущенные источники рассмотренными.

Отказы `SourceRequestError` содержат код и параметры границы. Чужие/ненормализованные/опасные пути, неизвестные ID, некорректные диапазоны, bool вместо int и неверные лимиты отклоняются. Слишком большой ответ не обрезается и не добавляет частично выданные строки в реестр. По умолчанию чтение ограничено 120 строками и сериализованный ответ — 100 000 символами; эти ограничения не заменяют контроль **всего** модельного запроса, output/repair/transport/денежного бюджета оркестратора.

Замена или добавление объектов в выбранном снимке после создания инструмента отклоняется; прежние разрешения на цитирование не переносятся на изменившийся снимок. Это проверка host-инварианта, а не защита от произвольного вредоносного Python-кода, обходящего замороженную dataclass, и не аутентификация источника.

Индекс/поиск/чтение — наблюдения над текстом. Они не означают, что операция рассмотрена, ветка выполнялась, запись доставлена или бизнес-результат подтверждён. Реестр semantic decisions, переходы, история их уточнений и оставшаяся работа сохраняются отдельно. В модельном контексте исходники остаются недоверенными данными, а текст HTML не выполняется.


## Existing-loop integration (validator /11)

The atomic page may request one bounded `source_request` instead of a map decision.
Index/search/read/read_unit/inspect_map all consume the same `refine_max_requests`
and the same sole JSON repair. A read changes investigation order, never marks a
unit reviewed, creates an action or gives a search hit control-flow authority.
The whole next model request (including repair reserve) must fit before a read is
published. Only source text actually present in the semantic request authorizes
its citations and owned unit spans. `new_lines` in the persisted controller result
counts lines absent from earlier/current model request excerpts, not understanding.

A bounded two-hop control neighbourhood supplies callers/selectors/receivers as
navigation. The model can ask for missing source and inspect the current graph.
A real call chain still does not certify branch predicate, branch-vs-sequence,
registry choice or committed effect. Later `transition_review.withdrawals` needs
an exact existing transition ID, reason and current source refs. Original proposal,
structural support and source review remain in history; withdrawn edges never
order the final graph, scenarios or compose input. A replacement uses the ordinary
source-supported transition path. Withdrawing is semantic model judgment and
requires independent result review, not a host assertion that the reason is true.

Validation of saved observations checks typed arguments, hashes and source/unit
binding without reading target code. No helper observation/output is a live donor.
The new identity intentionally invalidates business checkpoints for changed requests;
technical worker request recipes are not changed by this feature.
