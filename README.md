<h1>
  <img src="docs/assets/logo-badge.svg" alt="" width="32" height="32" align="top" hspace="6">
  EV Smart Charging — блюпринт нежной зарядки для Home Assistant
</h1>

[![CI](https://github.com/saippuakauppias/ha-ev-smart-charging/actions/workflows/ci.yml/badge.svg)](https://github.com/saippuakauppias/ha-ev-smart-charging/actions/workflows/ci.yml)
[![Docs](https://github.com/saippuakauppias/ha-ev-smart-charging/actions/workflows/docs.yml/badge.svg)](https://saippuakauppias.github.io/ha-ev-smart-charging/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Блюпринт автоматизации, который нежно заряжает электромобиль или PHEV в заданном
окне и **динамически подбирает ток**, чтобы зарядка равномерно растянулась до
времени окончания, а не «выстреливала» максимумом в первый час и потом стояла.

Меньший ток — меньше нагрев кабеля и бортового зарядного устройства, мягче режим
для батареи и ровнее нагрузка на домашнюю сеть. При этом машина всё равно готова
к нужному времени.

Блюпринт не привязан к конкретной марке автомобиля или модели зарядной станции:
все сущности выбираются в интерфейсе.

```
23:00  ─────────────────────────────────────────────────────────  07:00
       ▓▓▓▓▓▓▓▓▓▓▓▓▓▓  28 A ← «зарядить как можно быстрее»
       ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  12 A ← этот блюпринт
```

## 📖 Документация

**<https://saippuakauppias.github.io/ha-ev-smart-charging/>**

Там подробно: как работает регулятор, все параметры, поведение при сбоях,
диагностика сессии и разбор трассировок.

| Раздел | О чём |
|---|---|
| [Что умеет](https://saippuakauppias.github.io/ha-ev-smart-charging/guide/features/) | Полный перечень возможностей |
| [Как работает регулятор](https://saippuakauppias.github.io/ha-ev-smart-charging/guide/how-it-works/) | Формула, обратная связь, источники плана |
| [Быстрый старт](https://saippuakauppias.github.io/ha-ev-smart-charging/getting-started/quick-start/) | Три обязательных поля и что сделать до первого запуска |
| [Настройка](https://saippuakauppias.github.io/ha-ev-smart-charging/guide/settings/) | Ток и электрика, окно, троттлинг команд |
| [Отказоустойчивость](https://saippuakauppias.github.io/ha-ev-smart-charging/guide/reliability/) | Сбои данных, вранья GPS и связи со станцией |
| [Уведомления](https://saippuakauppias.github.io/ha-ev-smart-charging/guide/notifications/) | Три хука, все причины остановки и тревоги |
| [Примеры настройки](https://saippuakauppias.github.io/ha-ev-smart-charging/getting-started/examples/) | Типовые конфигурации и готовые уведомления |
| [Диагностика](https://saippuakauppias.github.io/ha-ev-smart-charging/diagnostics/) | Вердикт, журнал, трассировки, сводка по сессии |
| [Идеи на будущее](https://saippuakauppias.github.io/ha-ev-smart-charging/ideas/) | Обдуманное, но намеренно не реализованное |

## Установка

[![Открыть блюпринт в своём Home Assistant](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fsaippuakauppias%2Fha-ev-smart-charging%2Frefs%2Fheads%2Fmain%2Fblueprints%2Fautomation%2Fev_smart_charging%2Fev_smart_charging.yaml)

Или вручную: Настройки → Автоматизации и сцены → Блюпринты →
**Импортировать блюпринт**, и вставьте ссылку:

```
https://raw.githubusercontent.com/saippuakauppias/ha-ev-smart-charging/refs/heads/main/blueprints/automation/ev_smart_charging/ev_smart_charging.yaml
```

**Требуется Home Assistant 2024.10 или новее.** Подробнее —
[в документации](https://saippuakauppias.github.io/ha-ev-smart-charging/getting-started/installation/).

## Быстрый старт

Обязательных полей всего три:

| Поле | Что выбрать |
|---|---|
| Выключатель зарядки | `switch`, разрешающий выдачу тока |
| Уставка тока | `number` с током в амперах |
| Сенсор статуса станции | текстовый сенсор со значениями вида `charging`, `charged` |

Всё остальное имеет разумные значения по умолчанию. Чтобы включить регулятор,
добавьте сенсор процента заряда батареи **или** сенсор энергии сессии
с целью в кВт·ч.

> [!IMPORTANT]
> Перед первым запуском включите запись хода зарядки в журнал и поднимите
> лимит трассировок: по умолчанию Home Assistant хранит их так мало, что
> к концу сессии от неё не остаётся ничего, и разбирать случившееся нечем.
> [Что именно сделать](https://saippuakauppias.github.io/ha-ev-smart-charging/diagnostics/traces/#сразу-поднимите-лимит-трассировок).

## Разработка

Вся логика живёт в блоке `variables:`, поэтому проверяется без запущенного
Home Assistant:

```bash
pip install -r requirements-dev.txt
pytest -m "not slow"    # весь набор кроме мутаций, около 15 секунд
pytest -m slow -n auto  # мутационное тестирование, несколько минут
```

Подробнее — [про устройство тестов](https://saippuakauppias.github.io/ha-ev-smart-charging/development/)
и [как внести изменения](CONTRIBUTING.md).

## Безопасность

Блюпринт управляет силовым оборудованием, поэтому о дефектах, из-за которых
станция может получить ток выше настроенного максимума или не выключиться,
сообщайте приватно — порядок описан в [SECURITY.md](SECURITY.md).

## Лицензия

MIT — см. [LICENSE](LICENSE) · [Изменения](CHANGELOG.md)
