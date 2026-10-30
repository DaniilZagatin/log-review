# Исправления по независимому ревью CREDIT02

Статус: после лимита Claude пользователь прямо поручил Codex завершить реализацию и проверить её. 29 октября 2026. Основания: `.local/map-first-credit-quality-claude-01/VERIFIED-REVIEW.md`. Новых live E2E/OpenRouter-вызовов, метрик и архивов эта задача не запускает.

## Объём и ответственность

Claude — единственный автор исправлений в изолированной копии consumer/producer; Codex — baseline, проверка полного diff, независимые контроли, full/demo/export, интеграция после SHA-сверки. Допустимы непересекающиеся подпоручения Claude. Исходные результаты CREDIT01/02, референс, активные/исторические runtime, ledger/window20/unknown-reserves, ключи и private config не изменяются. Точный allowlist: `.local/map-first-quality-fixes-01/allowlist.json`.

Новое прямое поручение «пусть правит клод — ты проверь» расширяет прежнее artifact-only ограничение производителя до перечисленных ниже исправлений завершения/представления. Не вводятся новый исследовательский этап, новая архитектура, смена модели/low или неограниченные повторы.

| Шаг | Зависимости / файлы | Инварианты | Выход |
|---|---|---|---|
| Q0 | baseline/English brief/allowlist | все существующие правки сохранены, target/private не исполнять/не читать вне допуска | внешние копии и SHA; реальный Claude с Write/Skill/Agent |
| Q1 | Q0; consumer logging_navigation/java_logging_graph/java_declarations/log_discovery | типы/extends/super/перегрузки; неоднозначность явная; имя метода не доказательство emitter | generic Java positive/negative fixtures, source-bound wrapper chain; старый discovery/cache contract сохранён |
| Q2 | Q0; consumer logging_review/map_input/logging_changes/contracts/operation_coverage | обязательные refs строгие; один bounded JSON repair на логический ответ; invalid change не уничтожает верную оценку; вопросы runtime отдельно; request не доказывает применение | focused controls с реальными механизмами ошибок; C/P/U/X=N; некорректные источники не принимаются |
| Q3 | Q0; producer render + consumer map_presentation/logging_report + package data | reuse доверенного renderer, не inputHTML; immutable map, отдельный coverage overlay/счётчики; CSP/шрифты/selfcontained; без overlay прежнее поведение | циклы/маршруты/инспектор/группы/клики сохраняются, new saved-bundle export без модели |
| Q4 | Q0; producer agent/context/schema/quality/runner/stages | различать aborted и finished-with-gaps; boundary/frontier/дубли видимы; отсутствие в оборванной карте не объявлять отсутствием кода; не дорисовывать связи | scripted empty/length recovery ограничена существующим счётчиком/бюджетом, повторный отказ честно partial; no hidden reasoning/model switch |
| Q5 | Q1–Q4; независимая проверка Codex | source hashes/старые результаты/ledger неизменны, tests не подменяют live качество | focused/full producer+consumer, audit-guarded synthetic config, strict legacy demo/export, clean safe-copy tests; весь diff и outside-write audit |
| Q6 | Q5; SHA сравнение и 3-way при параллельных правках | не reset/stash/checkout, не копировать поверх новых правок; не переименовать/дорисовать старую карту | принятые файлы в root/upstream, локальные status/приёмка, отдельное новое представление сохранённого partial; живой результат не объявлять исправленным без нового live |

## Обязательные контроли

Java: прямой лог и наследуемая цепочка, одинаковые имена в разных классах, overload, непрочитанное звено, не-логирующий publish/command, граница внешнего SDK. Конфигурация/тип/источники подтверждают журналирование; delivery/commit не выводить из статического текста. Все новые имена/языковые примеры синтетические, без dictionary под эту репу.

Consumer: request до HTTP не превращается в C для успешного результата; ошибка core refs остаётся X; одно плохое предложение отдельно диагностируется; runtime-вопрос не равен незавершённому поиску; неверная форма ссылки получает точный feedback; диапазон за концом файла не повторяется с той же непонятной ошибкой; раскрытие блоков/цвета/фильтры не теряют операции или unmapped registry.

Producer: invalid entry/terminal IDs, точный дубль и legitimate repeated invocation различаются; несколько законных компонент не соединяются искусственно. Обрыв несёт причину/frontier в группировку и HTML. Сохранение артефактов и source masking не регрессируют. Новый retry в коде не является разрешением выполнить его сейчас на живой модели.

## Приёмка

Безальтернативное требование — адекватный бизнес-смысл, а не exit0/число тестов. В этой задаче live не запускается; подтверждается исправленная offline реализация и честное представление неизменного partial. Метрики frozen, архив deferred. Browser file policy не обходить; без разрешённой visual QA писать об этом явно.


### Уточнение пользователя: язык доработок

Публичные problem/what/why/timing/verification и пояснения привязок/задач — на русском, не только статические подписи. Имена полей/код/исходные сообщения логов/цитаты/пути сохраняются дословно. В UI действие add_event имеет русскую подпись, внутренний enum не переписывается. Не добавлять отдельный платный переводчик, бесконечные repair или словарь подмен английских слов. Исторический CREDIT02 JSON не менять и не выдавать перевод за исходный ответ. Приёмка проверяет запрос к модели и содержимое рекомендаций, а не только наличие русских кнопок.

Уточнение: основной промпт бизнес-модели второго агента и примеры содержательных строк переводятся на русский. Ключи JSON/enum/код/идентификаторы/исходные сообщения остаются исходными. Это основной механизм языка результата; отдельный переводящий вызов и избыточные языковые retry не добавляются.

### Codex takeover: источник контракта обёртки

Реальный текстовый probe обнаружил: 75 Java-кандидатов достигают SDK, но все остаются unconfirmed_external без ручных контрактов. В той же смысловой проверке разрешена явная интерпретация исходного интерфейса и конкретного адаптера. Хост выдаёт ограниченные кандидаты с точным типовым маршрутом; модель выбирает точный ID и объясняет назначение, цитирует интерфейс, всю реализацию и звенья. Это не операторский контракт и не автоматическое доказательство назначения: authority=model_interpretation_of_source сохраняется с ограничениями. Никаких имён SDK/классов данной репы в правилах, нового LLM-этапа или платных вызовов. Непрочитанные/чужие/неоднозначные цепочки отклоняются.

## Контрольная точка после интеграции

Q0–Q6 закрыты в пределах offline-реализации: чистый consumer1068pass; producer546pass/2knownskip; focus157; независимые3+2; strictlegacy6SHA; реальный export/CSP/fonts;49изменённыхфайлов вallowlist, evidenceнеизменны, ROOT/upstreamSHAсовпали сbaseline до записи. Доказательства `.local/map-first-quality-fixes-01/acceptance.json`. Новая source-contract/русская/repair дельта Codex пока без повторного ревью Claude из-за его лимита; это отдельный открытый пункт. Live бизнес-приёмка CREDIT02 отклонена и этим не меняется. Следующий платный прогон этим checkpoint не разрешён.

Последнее прямое уточнение человека «прогон» отдельно разрешило CREDIT03 до ожидающего Claude-ревью. Новый запуск20:42Мск, `.local/map-first-credit-e2e-03`; реальная смысловая приёмка pending, старый CREDIT02 неизменён.

CREDIT03 исчерпал исторический512cap до группировки/consumer. Человек отдельно «разрешаю» разрешил дополнительныепопытки: CREDIT04 толькоrebuild_from_map(фактическая03карта)→consumer,132дополнительно=4+128physical,новыйtotal644.22offlineгейта; прежний12USD и всяистория сохранены; actualsemanticacceptancepending.

## Q7 — протокол по реальным отказам CREDIT04

Прямое поручение «фикси баги». Файлы: logging_review.py/map_input.py и focused tests/docs. Зависимости — сохранённые исходные ответы04. Цитаты извлекает код; внешние отступы совместимы, текст/диапазоны/read-доступ строгие. Выдан bounded каталог ID с явным omitted без изменения знаменателя. SDK source_contract выбирает модель, прочитанные ссылки собирает хост; структурированный repair объединяет ошибки без нового API-цикла.

Выход offline:1080cleanfull/169focus; новые тесты RED на frozen04;37реальныхссылок old2/new33,4правильныхотказа сохранены;16реальныхfailedreplies не превращены вliveуспех;44первичныхпакета/16repair помещаются в180000chars.6legacySHA/2exportSHA/26исходныхфайловцелы,0модельныхвызовов. Доказательства `.local/logging-protocol-fix-01/acceptance.json`. Live semantic acceptance и Claude peer review открыты.

После Q7 человек прямо «запускай — только не с нуля»: CREDIT05 толькоconsumer по реальным04map/hierarchy/researchsidecars.44операции/13групп побайтно прежние;110оставшихсяconsumerphysicalattempts беззаимствования3groupingslots,прежний644/12USD.10offlineгейтов. Живаяприёмкаpending.

## Q8 — границы кандидатов по реальному CREDIT05

Прямое поручение: остановить, исправить все-X и продолжить сохранённую карту. Зависимости: immutable05 replies/map, Q7. Файлы: logging_review.py/logging_navigation.py/java_logging_graph.py; test_map_first_candidate_scope.py. Причина — все вызовы большого метода, вызывающие операции и внутренности SDK ошибочно становились отдельными обязательствами. Scope по опорам операции и доказанным вызываемым методам; отдельные ссылки внутренних звеньев на проверяемый внешний кандидат; адресные исключения; один repair пропущенных обязательных ответов. Карта/реестр/знаменатель/строгость источников сохраняются.

Offline1090clean/179focused,10новыхконтролей RED на frozen05;6legacySHA/exports/protectedfiles целы. Диагностический replay16неизменённыхответов не live/resume. Доказательства `.local/logging-scope-fix-01/acceptance.json`. Live business acceptance и Claude peer review открыты. Следующий шаг CREDIT06 толькоconsumer с теми же44операциями/13группами;75оставшихся физических попыток, прежние644/12USD.

## Q9 — guard-stop CREDIT06 и адресные диапазоны repair

Последнее прямое поручение: смотреть каждый вызов и останавливать, если не по плану.06штатно остановлен exit130 при4операциях с одинаковым primary/repair referencefailure;7завершеныдоSIGINT.15physicalcalls,60consumerпопыток осталось, прежние644/12USD. Промежуточный P не финальный: reconcile выявил совместное присвоение записи; итог44Xpartial сохранён.

Зависимости: реальные8отклонённыхответов06, Q8. Scope logging_review.py/test_map_first_reference_feedback.py; инварианты — source span строгий, один repair, нет автодописывания evidence/ID/coverage, старый06runtime/results/ledger не менять. Выход — адресное field/site/fullrange/readfeedback для32реальныхдефектов;7новыхREDнаfrozen06; focused186; cleanfull и hash/export/protectedfiles. Per-call rejected/inspect checkpoint до следующего dispatch. Независимая живая смысловая приёмка и Claude peerreview остаются открытыми. No automaticpaidrestart после остановки. Доказательства `.local/logging-reference-feedback-01/`.

Q9offlineпринят:1097cleanfull/186focused/7новыхконтролей,6legacySHA/exports/protected06verified; все32реальныеошибкиссылокадресованы, исходныйJSONпо-прежнемуreject. Runtime06неизменён. Новыйliveпослеguard-stopне запускался; бизнес/карта/sharedbinding/Claudepeerreview всё ещёopen. `.local/logging-reference-feedback-01/acceptance.json`.

## Q10 — прямой перезапуск с контролем каждого ответа

User: savedmaponly, inspecteveryanswer, stop immediately afterfailedrepair. Scope logging_review.py/logging_cli.py/cli.py/test_map_first_stop_guard.py и tasklocal07response_gate/launch. DependQ9accepted. Optin --stop-on-failed-repair останавливает ДОfallback/следующегоIO; defaultlegacyrecovery прежний. CallbackFalse даёт gracefulobserverstop с honest stop_reason, allN/source/map/checkpoints сохранены.07ждёт Codexdecision по exactbookSHA после каждой semanticresponse; failedrepair автоматически завершает без ack/новогоAPI.

Offline1106cleanfull/195focus/9guard/5barrier/10launchquotaPASS;6legacySHA/исходный06неизменны;0APIдоlaunch.60оставшихсяconsumerphysical в прежних644/12USD/window20/history/unknowns.44ops/71edges/13groups/112source reuse exact. Livebusinessacceptance и Claudepeerreview открыты. `.local/per-call-guard-01/acceptance.json`.

Q10actual07terminal:2API/0accepted/exit1 послеrepair. Primary4badmultiline refs, Codexchecked/allowedONErepair; repairHTTP200/length/completion131072 (Parasail), noacceptedJSON; hardstopbefore3rdcall WORKED. Map/source/ledgerprefix/exportsSHAunchanged. Reported0.045826291USD/newunknown0, remaining58consumer/global644/12USD. Receipt `.local/map-first-credit-e2e-07/independent-acceptance.json`. Новый primaryselector/boundrepairJSON предложен в `.local/primary-contract-review-01/PLAN.md`, NOTimplemented, не считать план liveисправлением.

## Q11 — primary selections, host evidence, bound repair JSON

Humanexplicitlyauthorizes implementation+checks+savedmaplaunch/peranswerreview/stopfailedrepair. Scope logging_review.py/redaction.py and newsynthetictests. DependenciesQ10+actual07. Evidenceknownsite/chain extracted ONLYdelivered; explicitbadrefs/not_log/publish/foreignroute/readgap/lifecycle remainstrict. RepaircontainsrealpublicpriorJSON+SHA/requesthash, resetperresponse/operation, wholepayloadcharged,ONErepair/no3rdafterfailure. TypedLLMerrorspersistedbeforestop. Oldquadraticredactor exposedby50k-publicfieldtestfixedwithidentifierboundary/fullprefix;3negativeprivacy/longtokencontrols+112actualsource maskcomparisons unchanged.

214focus+3redaction/1128cleanfullPASS; ClaudeREADONLYindependentreviewINPROGRESS in immutableclean, no newDeepSeekcalls. Prepared08same44ops/13groups/112source,58remainingphysical in644/12USD. Localwhole-request chars200k vs180k reservespriorJSON(firstworstfixture180124); unchanged1mcontext/output131072 withwholewireofficialTokenizer/8192framing boundBEFOREreserve/IO.12launchquota/context+5responsebarrierfakewirePASS. Admission/paidlaunchpendingreview/compatibilityreceipts. No syntheticfixture/donor promotedlive.


Q11 принят offline30Oct:1130full/216focus,12launch/context+5peranswer,strictlegacy6SHA/export/source112hashes; peerreviewfindings1/2/4/5closed/independentlyverified. CREDIT08 actualstarted21:38:10UTC/unified59024fromexact07map/groups/source;58remainingconsumer,12USD/sharedhistorypreserved. Каждый ответ — Codexbarrier; failedrepair immediate stop. Livebusinessacceptance pending; metricsfrozen/archive deferred.


### Q11 live outcome / Q12 открыт

CREDIT08 два actualprimary HTTP200 безrepair,0новыхнеизвестныхрасходов,reportedactual0.009976008USD. Codex observerstop послеstep-2 до третьегоAPI: при complete sources и известном отсутствии positive-resultлога модель задаёт риторический blocking source-вопрос => X. Из44операций2просмотрены,42не запускались; partialC0/P1/U0/X43 не приёмка E2E. Карта/source/ledgerprefix иrawответы сохранены.

Q12 (не реализован/не запускает paid): зависит отQ11real `call-review/independent-0001/0002`. Файлы предполагаемоlogging_review.py иgeneric synthetictests; exactscopeпри реализации. Инварианты: knownmissing не unresolvedsource; неизвестные источники не повышать доP/U/C; не угадывать корреляцию/другуюоперацию; никакого словаря под эту репу/автоподмены rawответа/карты. Выход: отличать риторический вопрос от настоящего открытого source-вопроса в модельном decisionконтракте и generic eval; meaningful offline+peerreview, живая приёмка отдельно. Новыйpaidrunпослеterminalне назначен.

Q12 NEWhumanauthorization: implement+continue savedmap; rootnewsourcebasis contract/defaultreview_tasks=[] and7syntheticcontrols,1137cleanfull/223focus/12launch+5barrier/strictdemo6SHA/export/protected08PASS. Actual08diagnostic is NOTdonor. Claude47878read-onlypeerreviewpending. Candidate09consumer56remaining in644/shared12USD; paidlaunch notyetdone.

Q12 acceptedoffline1140full/226focus/10genericcontrols/12launch+5barrier/strictlegacy6SHA/free08export/old08seal,2read-onlypeerreviews; actual09started22:11:34UTC/unified75473fromexact08savedmap/source/groups. Freshsource/mapquestionsmustidentifyunresolvedinput,stringsreject,knownmissing=>proposal; no automaticsemanticpromotion. EveryanswerCodexbarrier;56consumerremaining/shared12USD/historypreserved; liveacceptancepending.


### Q12 actual09 и следующий Q13

Actual09:4HTTP200/0новыхunknown/reported0.0139531308USD; каждыйответCodexreviewed. Knownmissingвобеихоперацияхявноmissing+changes/review_tasks=[],старыйриторическийXнеповторился. Step-1PпослеadapterIDrepair. Step-2failedrepairсновым logger.expression (полныйновыйвызовнепоказанвстаромsource) =>automaticstopдо5API,exit1. Source/map/sidecars/runtime/old08/ledgerprefix/2exportsSHAсохранены. PartialC0/P1/U0/X43,N44:2проверены/42не запускались. НетliveE2Eприёмки и новогоpaidrestart.

Q13 (не реализован): dependency actual09call3/4+Q12. Предполагаемыескоуп logging_review.py/_repair_details+generictests/primaryloggerwording. Нужноадресное сообщение changes[i].logger.expression и точныйдоступныйexistingcalleeизпрочитанныхrefs; предложенныйновыйкод хранить вwhat. Неослаблятьпроверку доступности, source refs, ONErepair/strictstop, неменятьстарый09runtime/ответы. Выход: REDgenericfixture/положительныйодинrepair/negative unavailablelogger/full/focus/export/peerreview. Отдельный provisional риск sharednegative-record step-1/step-2 требуетразборадофинальнойприёмки; в09оннетерминальнаяпричина.

Q13 NEWhumancontinue+Claudeanti-overfitreview. Ownloggerfeedback/proofsamewitnessimplemented1145full/231focus/5genericcontrols(4domain/languagevariants)+12launch/5barrierPASS. FreshClaude13511/c07e0534read-onlypending. Shared-fact genericprobeP/P=>X/X recordedforindependentreview; no guardweakened.10preparedexact09savedmap/source,52remainingconsumer/historypreserved; no paidlaunchyet.

Q13 currentv4: independent freshClaude review exposed structural F1-F4 and N1-N6, reproduced/corrected by Codex. Current clean1164/focus250/strictlegacy6SHA/free09export/protected09 PASS. Shared source-purpose/scope predicate, stdlibsymtable, Java gap/block rules, neutral not_established; syntheticcustomadapter positive preserves model authority. No targetname dictionaries/forcedcoverage/sharedpolicyweakening. Final narrow peerclosure pending;10 notadmitted/launched. Detailed acceptance .local/logger-generalization-01/GENERALIZATION-REVIEW.md andPLAN.

Q13 acceptedoffline1167full/253focus/22holdout+5matrix/3actualpeerreviews. Last3lowfindings fixedafterfinalpeeranswerandindependentlyRED/green, exactlastdiffnotrereviewed. Genericloggingpurpose/scope/staticbindingrules, no targetdictionaries/coverageweakening.10admitted/started2026-10-29T23:07:01UTC/PID89630/unified65587 fromexact09savedmap;52remainingconsumer,eachanswerCodexbarrier/failedrepairimmediatestop; pendingrealacceptance.

Q13 actual10:3HTTP200,primaryemptyestablishes->repairproductiveinspect->formalPwithvalidloggerreceiver. IndependentCodexstoppedbefore4thAPI:positive-successproposal location67isbreakinnegativebranch. OriginalJSON/HTMLretained,liveE2Erejected; source/map/4sidecars/runtime/old09/ledgerprefix/exportSHAallPASS. Q14OPEN: genericproposal source placement/branch/timing consistency, no line-number/message dictionary; notimplemented, no newpaidrestart. .local/map-first-credit-e2e-10/SEMANTIC-REVIEW.md+independent-acceptance.json. Remainingconsumer49of128/global644/shared12USD.

### Q14 — native goal active (30 October2026)

User explicitly authorized generic placement fix, Claude review, savedmap continuation with EVERYresponse manually checked; semanticerrorORfailedrepair immediately stops currentrun. After everyrun jointqualityanalysis/corrections, within existingbudget. Goalacceptance is stable useful logbindings/coverage/meaningfulproposals with actualfullreport+jointsemanticreview and independentcrossdomain controls, not justtests. Metricsfrozen/archive deferred. NativegoalACTIVE; no manuallyreducedcompletion.

Firstunit: two unrelated syntheticPython/Java cases reproduce currentacceptance of successprose atnegativeearlyreturn/break anchor; no targetexecute/paidcall/donor. .local/proposal-placement-01/current-reproduction.json. Codexownsimplementation, Claude1197read-onlyreviewstoppedactual10andminimalgenericdesign/holdouts. Scope/dependencies/invariants/exitcriteria/prewrite in .local/proposal-placement-01/PLAN.md. No11launched;49consumerremaining/128/global644/shared12USD preserved.


### Q17 — согласованный первичный контракт и продолжение14

Зависимости: actual13 semanticstop, Q14-Q16 source/lifecycle/context mechanics. Scope/diff/invariants/exitcriteria: `.local/primary-coherence-01/PLAN.md`.1282root/clean,10новыхnegativecontrols, strictdemo6SHA/free10–13exports/immutablechecks и дваClaudeкодревью. Offlineaccepted, livequalityopen. CREDIT14 actualstarted03:02:14UTC толькоsameactual13map44/71/13/source112;44remainingconsumer/shared12USD/644, eachanswerCodexbarrier, semanticerror/failedrepair=>stop. Никакихновыхархивов/метрик.


### Q18 — согласованность покрытия и адресных выводов

Зависимости: actual14primary+repair/observerstop, Q17, jointqualityreview71273. Scope10файлов:2product/8tests; подробности `.local/coverage-consistency-01/PLAN.md` и `changes-final.diff`. Недостаточная оценка требует реального missing либо объясняющего ограничения; постороннийlimit не скрывает дефект. Адресные причины/выборcontextselector в том же repair, без автопривязки/повышенияC. Rawhostfields не участвуют в диагностике. Старые профили/export/источники/карта сохранены.1292root/clean,326focused/10generic,21504эквивалентныхрешения, strictdemo/export/hash; peer91514 findings1–3 закрыты31212. Offlineпринят, liveopen.42remainingconsumerна44ops, запрос+64припрежнем12USD ждёт человека; ни одного новогоpaidcalls после14stop.

Q18 continuation preparation: CREDIT15prepared_not_admitted;224runtime/112source/44ops/71edges/13groups, actual14sidecars exact.12launchbudget+5barrier+10focus PASS;353files verified; both entrypoints refuse absent admission before key/process/network;0paid. No admission/seal/dispatch. Existing42remaining/128consumer/644global/12USD retained; +64 question pending. Proof `.local/map-first-credit-e2e-15/preparation-checks.json`. Full semantic acceptance remains open.

Native goal BLOCKED after third consecutive allocation audit: unchanged42remaining for44operations; +64 decision not received. Previous turn completed offline preparation; no remaining independent work enables full live acceptance. Resume requires explicit allocation answer; no expansion, launch, new Claude or ledger mutation.

30Oct human approved+64consumer calls,12USD unchanged. Allocation192consumer/708global,106remaining. CREDIT15 launched unified9489/PID99763 from actual14map only; no rejectedconsumer donor; each-response gate active. Semantic acceptance pending.


## Q19 — границы repair и понятность отчёта

Зависимости: реальный CREDIT15, Q18, проверенный разбор Claude53694. Codex изменил logging_review/proposal_placement и добавил межпредметные контроли; Claude6927 проверил весь diff, существенных находок нет. Исходная неверная позиция не принимается: repair получает условную подсказку для полной границы с существующим соседним логом. Primary не задаёт source_contract:null в примерах и не разрешает отрицать уже признанный факт.

Отдельное поручение человека: убрать длинные повторные оговорки и сделать понятными момент записи и связь по идентификаторам. logging_report фильтрует ровно два общих сообщения только в представлении, сохраняет конкретные проблемы и rawJSON. Общая строка называет модель автором смысловой оценки; пользовательские данные/обёртки не заменяются выдуманными ID. Реальные raw/export10–15 сохранены, strict demo6SHA и negative placement controls PASS. Финальные full/Claude13531 и admission16 открыты. `.local/report-copy-clarity-01/`.


## Q20 — явный выбор контракта и соседнее свидетельство

Зависимости actual16/Claude6705/Q19; scope logging_review.py+5tests, без схем/автопривязок/измененияhostcache/validator. `.local/primary-affordance-01/PLAN.md`.7initial+2peerRED/green,1312root/clean,9focused,strictdemo6SHA/export10–16/immutable/44initialsizes и дваClaudeревью. Скопированная заготовка теперь отклоняется существующей проверкой пустогоmeaning; чужие статусы/причины сохраняются. Offlineaccepted, liveсемантикаopen. CREDIT17 запущен08:58:14UTC/unified1331 наactual16savedmaponly;104remainingconsumer/shared12USD,каждыйответCodexgate. Повтор того же структурного/ownership-onlyдефекта опровергает гипотезу; без бесконечного добавленияpromptwarnings/сменыstop-policy.


## Q21 — сборка единственной проверенной связи с кодом

Зависимости: фактический CREDIT17 и исправленный совместный диагноз. Объём: новый logging_binding_sources.py, интеграция logging_review.py, generic Python/Java тесты и корректировка прежнего теста диагностики. Хост меняет только свою копию ссылки после выбора записи моделью; смысл, покрытие и исходный JSON не исправляет. Явный конфликт, неоднозначность, непрочитанный источник, участок без владельца или неподтверждённый факт сохраняют строгую проверку. Учитываются вложенные вызовы, а не только сокращённый список соседей. 16 focused и 1328 root/clean PASS; strict demo и сохранность export10–17 подтверждены. Первое ревью Claude нашло воспроизведённый дефект, исправление на повторном ревью93390. CREDIT18 пока только подготовлен; независимая приёмка и запуск остаются открыты.

Q21 принят offline: финальное Claude93390 без существенных замечаний, независимая проверка подтверждена. CREDIT18 запущен09:30:25UTC/unified63824 только с сохранённой картой CREDIT17.12 launch +5 barrier +16 focused проверок перед запуском, 102 оставшихся вызова/$12. Живая смысловая приёмка открыта.


## Q22 — признанный факт должен быть учтён или иметь конкретный пробел

Зависимость: actual18 и совместный разбор Claude15593. Кодовая ошибка согласованности отделена от смысловой ошибки модели в толковании ветки. Реализация: delivered-call/2, общий typed-field контроль в live/saved, совпадающая repair-диагностика, один заменённый абзац. Ни автопривязки, ни автоматического C, ни исправления исходного текста.10 focused, негативные контроли Python/Java и replay настоящего18 без API; прежние export10–18/strictdemo6SHA сохранены. Full root/clean и Claude80353 ещё в проверке; CREDIT19 только подготовлен,101consumer/$12, картаactual18. Детали `.local/prior-fact-accounting-01/PLAN.md`.

Q22 acceptedoffline30Oct:1342root/clean,14focus,strictdemo/export10–18/immutable, peers80353/94143 corrected,96986closure. CREDIT19 actualstarted09:57:03UTC/unified40633 fromsame44/71/13/112 actual18maponly,101remainingconsumer/global708/$12. EachanswerCodexbarrier,semanticerror/failedsole repair=>stop. Fullbusinessacceptanceopen.

## Q23 — ссылки анализатора не являются полями записи

Depends actual19/Claude53942; scope/invariants/exit criteria `.local/correlation-selectors-01/PLAN.md`. Exact host-issued selectors forbidden only in correlation, no closed partial field list or automatic meaning/coverage rewrite. Newprofile/savednamespace/cache rebuild preserve old exports.14focus/1356root/clean/strictlegacy/export10–19 PASS; Claude39029 reviewpending.20prepared_notadmitted/100remainingconsumer/708/$12; eachreplygateandsemanticstop unchanged.

Q23 acceptedoffline:1356root/clean/14focus/strictdemo/export10–19/hash,Claude39029 no material findings. CREDIT20actualstarted10:16:34UTC/unified75289 fromsame19maponly,100remainingconsumer/shared12USD/global708. FirstpreflightpathmismatchcaughtbeforeIO,preservedandcorrected. Peranswerstopruleunchanged;fullbusinessacceptanceopen.


## Q24 — независимые основания совместного использования записи

Зависимость: actual CREDIT20 C/C→X/X и Claude34477/61563. Код: operation_coverage.py, logging_review.py; per-use новый reconcile_policy, прежние exports без ключа сохраняют политику. Каждая привязка отвечает за свой источник; чужая слабая привязка или относительная роль не обнуляет обоснованные строки. Семантика всё ещё проверяется моделью и независимым наблюдением, источник не доказывает смысл. N — операции карты, не исполнения.

16focused и соседние contextpolicy контроли; окончательный Claude85519. Человек попросил сокращённый цикл: повтор полного root/clean после устойчивого живого результата, не после каждой локальной правки. Предыдущие1372/1372 относятся к промежуточной версии. CREDIT21 подготовлен по actual20 map/112source,97consumer/192/global708/$12, без consumer donor. До peer closure запуск не сделан.


## CREDIT22 — приоритет полного просматриваемого отчёта

30октября человек прямо попросил один отчёт до конца, чтобы посмотреть логи и доработки. Остановку по каждой смысловой неточности и failed repair заменяет сбор замечаний с продолжением других операций. Строгий validator источников/ID/данных и бюджет сохраняются; неподтверждённые строки остаются X, raw не исправляется. Только saved map, прежний core Q24, без новых фиксов/метрик/архива.96consumer/$12, task-local observer сохраняет каждый ответ без паузы. Финальная приёмка честно различает полный проход и качество.


## Q25 — причины X и исправления без остановки полного прохода

Разрешено пользователем30октября после группировки18X. Изолированная совместная реализация, раздельное владение и взаимное ревью; подробные зависимости/файлы/инварианты/выходные критерии в `.local/x-cause-fixes-01/PLAN.md`. CREDIT22 проходит до полного HTML независимо. Только после готового отчёта/ревью/остатка бюджета — consumer с настоящей сохранённой картой, не новый producer. Семантические неопределённости не превращать автоматически в покрытие.
