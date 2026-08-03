# Установка

!!! info "Требуется Home Assistant 2024.10 или новее"

    Блюпринт использует секции в настройках и современный синтаксис триггеров.

## Вариант 1 — импорт по ссылке (рекомендуется)

1. Настройки → Автоматизации и сцены → Блюпринты → **Импортировать блюпринт**.
2. Вставьте ссылку на файл блюпринта в этом репозитории:

```
https://raw.githubusercontent.com/saippuakauppias/ha-ev-smart-charging/refs/heads/main/blueprints/automation/ev_smart_charging/ev_smart_charging.yaml
```

## Вариант 2 — через HACS

Репозиторий оформлен как источник блюпринтов для HACS: в HACS → ⋮ →
**Custom repositories** его можно добавить как
`https://github.com/saippuakauppias/ha-ev-smart-charging` с категорией
*Blueprint*.

!!! warning "Пока не работает: нет опубликованных версий"

    HACS ставит блюпринты по git-тегам, а показ ветки по умолчанию в этом
    репозитории отключён. Пока в нём нет ни одного тега, устанавливать
    оттуда нечего — пользуйтесь импортом по ссылке.

## Вариант 3 — вручную

Скопируйте `blueprints/automation/ev_smart_charging/ev_smart_charging.yaml`
в `config/blueprints/automation/ev_smart_charging/` и перезагрузите автоматизации.

## Дальше

Переходите к [быстрому старту](quick-start.md): там три обязательных поля
и две настройки, которые стоит сделать до первой ночи.
