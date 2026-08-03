# Установка

!!! info "Требуется Home Assistant 2024.10 или новее"

    Блюпринт использует секции в настройках и современный синтаксис триггеров.

## Вариант 1 — кнопка импорта (рекомендуется)

[![Открыть блюпринт в своём Home Assistant](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fsaippuakauppias%2Fha-ev-smart-charging%2Frefs%2Fheads%2Fmain%2Fblueprints%2Fautomation%2Fev_smart_charging%2Fev_smart_charging.yaml)

Кнопка открывает диалог импорта в вашем Home Assistant — останется нажать
*Импортировать блюпринт*.

## Вариант 2 — импорт по ссылке

Если кнопка не сработала (сервис `my.home-assistant.io` не настроен или
недоступен), сделайте то же вручную:

1. Настройки → Автоматизации и сцены → Блюпринты → **Импортировать блюпринт**.
2. Вставьте ссылку на файл блюпринта:

```
https://raw.githubusercontent.com/saippuakauppias/ha-ev-smart-charging/refs/heads/main/blueprints/automation/ev_smart_charging/ev_smart_charging.yaml
```

## Вариант 3 — вручную

Скопируйте `blueprints/automation/ev_smart_charging/ev_smart_charging.yaml`
в `config/blueprints/automation/ev_smart_charging/` и перезагрузите автоматизации.

## Почему не через HACS

HACS блюпринты **не поддерживает**: в списке его категорий есть интеграции,
темы, шаблоны, python-скрипты и AppDaemon — блюпринтов там нет. Добавить
репозиторий как custom repository не выйдет, потому что при добавлении нужно
выбрать категорию, а подходящей не существует.

Обновлять блюпринт нужно самостоятельно: повторный импорт по той же ссылке
перезаписывает файл и **сохраняет все настройки** уже созданных автоматизаций.
Следить за выходом версий удобно через *Watch → Releases* на GitHub.

## Дальше

Переходите к [быстрому старту](quick-start.md): там три обязательных поля
и две настройки, которые стоит сделать до первой ночи.
