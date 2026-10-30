# Рецепты страниц

Готовые композиции лежат в карточках группы «Композиции экранов»; здесь — порядок блоков.

## Лендинг (`PatternLanding`)

Banner → NavBar → Hero с поиском → LogoCloud → SectionHeader + FeatureTile → рамка с ShowcaseTabs → MediaCard ×3 → Testimonial → CTABand (haze) → Footer.

## Каталог и поиск

Hero с `SearchField hero` и чипами → строка `FilterBar` → сетка `MediaCard` / `FeatureTile` / `ScreenCard` → `Pagination`.

## Рабочий экран (`PatternMonitor`, `PatternDashboard`)

Окно приложения → шапка → показатели → фильтры → таблица или графики. Один акцент, штриховые полосы, статусы-пилюли.

## ИИ-страница (`PatternAssistant`)

InfoBar BETA → заголовок → Prompt → PromptCard → ToolTile → диалог.

## Галерея (`PatternGallery`)

Haze с кластером иконок и поиском → Tabs → ScreenCard ×4 → OptionGroup list + Stage → MegaMenu.

## Процесс (`PatternProcess`)

SectionHeader → ProcessTimeline с метриками → карточка LineChart.

## Тарифы

SectionHeader center → SegmentedControl (месяц/год) → три `PricingCard`, средняя `highlighted` → Accordion «Вопросы» → CTABand.

## Статья

NavBar → заголовок `display-xl` в `container-text` → дата и тег → текст `body-lg` → `TableOfContents` справа → MediaCard «Читайте также» → Footer.

## Правила сборки

Каждый экран — один главный CTA. Секции отделяет `section-y-md`, а не линия. Чередуйте белое и серую рамку. Не более двух типов карточек в одной секции.
