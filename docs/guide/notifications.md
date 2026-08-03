# Уведомления

Три хука принимают произвольные действия:

| Хук | Когда | Доступные переменные |
|---|---|---|
| Старт сессии | зарядка включена | `soc`, `soc_valid`, `plan_source`, `needed_kwh`, `desired_current`, `hours_left`, `charger_status`, `cold_mode` |
| Завершение | зарядка выключена | те же + `stop_reason`, `target_missed` |
| Неисправность | см. таблицу ниже | `alarm_reason` |

Пример действия для хука завершения:

```yaml
- action: notify.mobile_app_phone
  data:
    title: Зарядка завершена
    message: >-
      {{ stop_reason }} — заряд {{ soc if soc_valid else 'н/д' }} %.
      {{ 'Цель не достигнута.' if target_missed else '' }}
```

## Причины остановки

Возможные значения `stop_reason`: `target_reached`, `charger_reports_charged`,
`fault`, `car_not_home`, `unplugged`, `status_unknown`,
`window_end`. Вне остановки — `none`.

## Причины тревоги

Возможные значения `alarm_reason`: `switch_entity_missing`, `charger_fault`,
`car_refused_charge`, `charger_reports_no_power_but_car_charging`,
`no_power_while_on`, `car_data_unavailable_check_integration_auth`,
`car_data_stale`, `car_data_frozen`.

!!! warning "Повторы блюпринт не гасит"

    Хук неисправности вызывается на **каждом** пересчёте, пока проблема
    держится, — блюпринт не помнит, сообщал ли он о ней раньше. Если повторы
    мешают, гасите их в самом действии (например, одинаковым `tag`
    в `notify`): готовые примеры и типовые конфигурации собраны
    в [примерах настройки](../getting-started/examples.md).

    Почему это не решено внутри блюпринта — в
    [идеях на будущее](../ideas.md#подавление-повторных-уведомлений-о-проблемах-с-данными).
