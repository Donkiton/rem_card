# Окно выбора ролей: доработка по референсу

Основа — пользовательский концепт `a3931fd1-c8d2-4f67-aacc-227cb206369c.png`.
Фон и фотографии сгенерированы встроенным imagegen. Надписи, контуры,
пиктограммы, стрелки, часы и переключатель темы отрисовываются средствами Qt.
Файлы ресурсов включаются существующим правилом упаковки каталога `icon`.

## Ресурсы и промпты

`icon/unified_welcome_background_v3.png` — чистый фон окна; редактирование
пользовательского референса, исходный концепт сохранён без изменений.

> Edit the supplied reference into a clean reusable background asset for this exact desktop app design. Remove ALL UI overlays: title bar, logos, all text, cards and their rectangular panels, icons, arrows, quote, toggle, separators, line art. Reconstruct ONLY the continuous softly defocused photographic ICU room behind them, matching its composition, deep navy blue and cyan color grading and light. Empty ICU bed on left lower foreground, medical equipment/window structure left and center, large soft dark negative space in center, blue vital monitor partly cropped at lower right showing luminous cyan vitals. No people. No readable text except subtle monitor digits. Keep atmospheric blur and sophisticated realistic light. Full bleed landscape 16:9, high resolution. This is just a background asset, not a UI screenshot.

`icon/unified_role_photos_v3.png` — атлас 2 × 2: врач, медсестра,
экстренный и плановый оперблоки. Каждая карточка использует свой квадрант.

> Generate ONE square seamless 2 by 2 photographic texture atlas for medical desktop UI role cards. Exactly four equal quadrants edge-to-edge, NO gutters, NO borders, NO typography, NO icons, NO UI controls. Top left: close up real stethoscope on folded blue scrub fabric, deep blue cyan cinematic lighting. Top right: folded teal nurse cap / medical cloth pouch with a subtle dark cross, teal green lighting. Bottom left: stainless steel scalpels and surgical instruments on sterile purple-blue surgical drape, purple indigo grading. Bottom right: operating theatre surgical overhead lamp with multiple luminous circular lenses, desaturated steel blue silver grading. All four are atmospheric soft focus photographic still lifes, no people, no hands, objects occupy upper half of each quadrant, lower half fades into dark matching color with generous negative space for future separate UI text. Fine realistic materials, softly reflected light, sophisticated deep ICU-blue palette. Production quality texture atlas, square 2048x2048.

## Поведение

- Краткое название учреждения поступает из существующих настроек; без
  названия первый разделитель скрывается. Длинное название сокращается
  визуально многоточием, исходное значение не изменяется.
- ОАРИТ, дата и время остаются в верхней строке с разделителями.
- Слоганы, английская подпись и цитата с автором статичны.
- Внизу слева используется существующий `ThemeSwitch` с общим менеджером
  и сохранением темы рабочего места. Сохранённый режим читается до входа
  в роль. Выбор меняет сам экран приветствия, рамку, загрузочную страницу
  и сохраняется при входе в клинические роли и возврате из них.
- Кнопка настроек убрана с экрана выбора ролей. Настройки внутри ролей
  сохраняются. Кнопка «О программе» по-прежнему заменяется обновлением,
  если оно доступно.
- Во время подготовки роли переключатель блокируется вместе с действиями.
  При техработах роли заблокированы существующим механизмом.
- Фотографии находятся под живыми надписями: текст не масштабируется как
  снимок экрана при анимации окна. Пиктограммы кэшируются с учётом DPI.

## Светлый вариант

Референс: `d2c6df4f-cb28-462e-ba88-70d3f3d8c1a9.png`.
Светлые ассеты созданы встроенным imagegen; исходные файлы не изменены.

`icon/unified_welcome_background_light_v3.png`:

> Remove ALL interface overlays from the supplied image: title bar, logo, texts, four cards, icons, arrows, quote, switch. Reconstruct only the full continuous empty ICU hospital room background matching this reference. High key warm ivory and pale grey, diffuse daylight, softly defocused room, empty hospital bed lower left, soft dark vital sign monitor cropped at right, bright softly blurred equipment and surgical lighting. Keep it light and low contrast for dark navy interface text placed later. No people, no lettering or graphics other than subtle monitor vitals. Full bleed 16:9 background asset, not a screenshot.

`icon/unified_role_photos_light_v3.png`:

> One square 2x2 photographic texture atlas, four EXACT equal quadrants, no gutters or borders, NO text, NO UI, no icons. High key softly defocused light-theme medical interface background photos. Top left: stainless steel stethoscope on pale blue clinical paperwork and light fabric. Top right: white nurse cap with green cross and pale mint clinical instruments. Bottom left: silver surgical scalpels and forceps on very pale lavender sterile drape. Bottom right: operating theatre round surgical overhead lamp, warm ivory grey. Objects in upper half of each quadrant, lower half gradually fades into near-white with matching pale blue/mint/lavender/ivory tint for later dark navy text. Bright diffuse daylight, low contrast, photographic elegant hospital materials, no people. Square 2048x2048 atlas.
