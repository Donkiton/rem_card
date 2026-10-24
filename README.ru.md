<p align="center">
  <a href="README.md"><kbd>English</kbd></a>
  <a href="README.ru.md"><kbd><strong>Русский</strong> ✓</kbd></a>
  <a href="README.zh-CN.md"><kbd>中文（简体）</kbd></a>
  <a href="README.ja.md"><kbd>日本語</kbd></a>
  <a href="README.de.md"><kbd>Deutsch</kbd></a>
</p>

<p align="center">
  <img src="docs/assets/readme/remcard-hero.svg" width="100%" alt="Рем Карта — единая карта пациента и смены для реанимации и оперблока с 08:00 до 08:00">
</p>

<p align="center">
  <strong>Карта пациента и совместная работа смены в реанимации и оперблоке.</strong><br>
  Одно Windows-приложение для врача, медсестры, плановой и экстренной операционной.
</p>

<p align="center">
  <a href="VERSION">v5.1.10</a> · Windows · Python 3.11 · PySide6 · SQLite<br>
  <a href="https://github.com/Donkiton/rem_card/releases">Релизы</a> ·
  <a href="#что-почитать">Документация</a> ·
  <a href="https://donkiton.github.io/rem_site/">Сайт</a> ·
  <a href="LICENSE">Лицензия</a>
</p>

> [!WARNING]
> **Экспериментальный проект, не медицинское изделие.** Клиническое применение требует независимой технической и клинической проверки, приёмки в конкретной среде и резервного рабочего процесса.

## Посмотреть интерфейс

Демонстрационные пациенты · скриншоты **5.1.0** · интерфейс на русском. Раскройте нужную группу.

<details>
<summary><strong>Рабочие места и выбор роли</strong></summary>

[![Врач, медсестра, плановый и экстренный оперблок](docs/assets/readme/remcard-roles-5-1.jpg)](docs/assets/readme/remcard-roles-5-1.jpg)

Четыре рабочих места из единого окна, светлая и тёмная темы.

</details>

<details>
<summary><strong>Пациенты, койки и поступления</strong></summary>

[![Список активных пациентов и переход к карте](docs/assets/readme/remcard-patients-5-1.jpg)](docs/assets/readme/remcard-patients-5-1.jpg)

[![Занятые и свободные койки с карточкой пациента](docs/assets/readme/remcard-beds-5-1.jpg)](docs/assets/readme/remcard-beds-5-1.jpg)

Состояние коек, оформление поступления и быстрый переход к карте пациента.

</details>

<details>
<summary><strong>Назначения и баланс жидкости</strong></summary>

[![Суточные назначения и отметки выполнения](docs/assets/readme/remcard-orders-5-1.jpg)](docs/assets/readme/remcard-orders-5-1.jpg)

[![Введение, выведение и почасовой баланс жидкости](docs/assets/readme/remcard-balance-5-1.jpg)](docs/assets/readme/remcard-balance-5-1.jpg)

Назначения, отметки выполнения и баланс жидкости на шкале смены 08:00–08:00.

</details>

## Коротко о возможностях

- **Карта пациента** — назначения и выполнение, витальные показатели, баланс, ИВЛ, питание, процедуры и анализы.
- **Командная работа** — рабочие места врача и медсестры, плановый и экстренный оперблок, передача пациента из РАО.
- **Клинические инструменты** — расчёты инфузии, электролитов и ожогов, PDF-отчёты.
- **Архив и аналитика** — история пациентов и операций, фильтры, сравнения и графики.

## Начать работу

В установленной версии запустите **`RemCard.exe`** из полной папки поставки вместе с `_internal`.

Запуск из исходников, выбор папки данных и обновления описаны в [руководстве по запуску](docs/guide/setup.ru.md). Для разработки и испытаний нужна отдельная тестовая база.

## Что почитать

| Руководство | Содержание |
| --- | --- |
| [Возможности](docs/guide/features.ru.md) | Рабочие места, карта, питание, калькуляторы и аналитика. |
| [Запуск и обслуживание](docs/guide/setup.ru.md) | Исходники, установка, обновления, безопасность данных и проверки. |
| [Архитектура](docs/guide/architecture.ru.md) | Общие данные, синхронизация и интерактивная карта кода. |

Подробные технические регламенты собраны в [карте документации](docs/README.md).

<p align="center">
  <a href="https://cliniciansthatcode.com/directory/#p-vadim-bityutsky"><img src="docs/assets/readme/clinicians-that-code-verified.svg" width="260" height="28" alt="Карточка автора RemCard в каталоге Clinicians That Code"></a><br>
  <sub>Автор — анестезиолог-реаниматолог Вадим Битюцкий · <a href="mailto:menfise@mail.ru">Связаться</a></sub>
</p>
