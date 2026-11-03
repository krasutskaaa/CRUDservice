# Журнал перевірки

### 1. Готовність сервісу

`GET /healthz`

Очікувано: **200**, отримано: **200** ✅

```json
{
  "status": "ok"
}
```

### 2. Створення запису

`POST /admissions`

Тіло запиту: `{"patient_code": "P000001", "department": "кардіологія", "room": "12", "admission_date": "2025-03-01", "discharge_date": null, "outcome": "на лікуванні"}`

Очікувано: **201**, отримано: **201** ✅

```json
{
  "id": 1,
  "patient_code": "P000001",
  "department": "кардіологія",
  "room": "12",
  "admission_date": "2025-03-01",
  "discharge_date": null,
  "outcome": "на лікуванні"
}
```

### 3. Невалідне створення: виписка раніше надходження

`POST /admissions`

Тіло запиту: `{"patient_code": "P000001", "department": "кардіологія", "room": "12", "admission_date": "2025-03-01", "discharge_date": "2025-02-01", "outcome": "на лікуванні"}`

Очікувано: **400**, отримано: **400** ✅

```json
{
  "error": "validation_error",
  "details": [
    {
      "field": "discharge_date",
      "message": "дата виписки не може бути ранішою за дату надходження"
    }
  ]
}
```

### 4. Невалідне створення: бракує поля

`POST /admissions`

Тіло запиту: `{"patient_code": "P000001", "department": "кардіологія", "admission_date": "2025-03-01", "discharge_date": null, "outcome": "на лікуванні"}`

Очікувано: **400**, отримано: **400** ✅

```json
{
  "error": "validation_error",
  "details": [
    {
      "field": "room",
      "message": "Field required"
    }
  ]
}
```

### 5. Невалідне створення: клієнт передає id

`POST /admissions`

Тіло запиту: `{"patient_code": "P000001", "department": "кардіологія", "room": "12", "admission_date": "2025-03-01", "discharge_date": null, "outcome": "на лікуванні", "id": 5}`

Очікувано: **400**, отримано: **400** ✅

```json
{
  "error": "validation_error",
  "details": [
    {
      "field": "id",
      "message": "Extra inputs are not permitted"
    }
  ]
}
```

### 6. Невалідне створення: невідомий результат

`POST /admissions`

Тіло запиту: `{"patient_code": "P000001", "department": "кардіологія", "room": "12", "admission_date": "2025-03-01", "discharge_date": null, "outcome": "?"}`

Очікувано: **400**, отримано: **400** ✅

```json
{
  "error": "validation_error",
  "details": [
    {
      "field": "outcome",
      "message": "допустимі значення: на лікуванні, одужання, покращення, переведення, летальний наслідок"
    }
  ]
}
```

### 7. Перелік із фільтром за відділенням

`GET /admissions?department=кардіологія`

Очікувано: **200**, отримано: **200** ✅

```json
{
  "items": [
    {
      "id": 1,
      "patient_code": "P000001",
      "department": "кардіологія",
      "room": "12",
      "admission_date": "2025-03-01",
      "discharge_date": null,
      "outcome": "на лікуванні"
    }
  ],
  "total": 1,
  "limit": 20,
  "offset": 0
}
```

### 8. Перелік без фільтра (пагінація за замовчуванням)

`GET /admissions`

Очікувано: **200**, отримано: **200** ✅

```json
{
  "items": [
    {
      "id": 1,
      "patient_code": "P000001",
      "department": "кардіологія",
      "room": "12",
      "admission_date": "2025-03-01",
      "discharge_date": null,
      "outcome": "на лікуванні"
    }
  ],
  "total": 1,
  "limit": 20,
  "offset": 0
}
```

### 9. Перелік: limit поза межами

`GET /admissions?limit=1000`

Очікувано: **400**, отримано: **400** ✅

```json
{
  "error": "validation_error",
  "details": [
    {
      "field": "limit",
      "message": "Input should be less than or equal to 100"
    }
  ]
}
```

### 10. Читання за ідентифікатором

`GET /admissions/1`

Очікувано: **200**, отримано: **200** ✅

```json
{
  "id": 1,
  "patient_code": "P000001",
  "department": "кардіологія",
  "room": "12",
  "admission_date": "2025-03-01",
  "discharge_date": null,
  "outcome": "на лікуванні"
}
```

### 11. Читання неіснуючого

`GET /admissions/999999999`

Очікувано: **404**, отримано: **404** ✅

```json
{
  "error": "not_found",
  "message": "запис не знайдено"
}
```

### 12. Заміна запису

`PUT /admissions/1`

Тіло запиту: `{"patient_code": "P000001", "department": "кардіологія", "room": "14", "admission_date": "2025-03-01", "discharge_date": "2025-03-10", "outcome": "одужання"}`

Очікувано: **200**, отримано: **200** ✅

```json
{
  "id": 1,
  "patient_code": "P000001",
  "department": "кардіологія",
  "room": "14",
  "admission_date": "2025-03-01",
  "discharge_date": "2025-03-10",
  "outcome": "одужання"
}
```

### 13. Невалідна заміна

`PUT /admissions/1`

Тіло запиту: `{"patient_code": "P000001", "department": "кардіологія", "room": "12", "admission_date": "2025-03-01", "discharge_date": "2024-01-01", "outcome": "на лікуванні"}`

Очікувано: **400**, отримано: **400** ✅

```json
{
  "error": "validation_error",
  "details": [
    {
      "field": "discharge_date",
      "message": "дата виписки не може бути ранішою за дату надходження"
    }
  ]
}
```

### 14. Заміна неіснуючого

`PUT /admissions/999999999`

Тіло запиту: `{"patient_code": "P000001", "department": "кардіологія", "room": "12", "admission_date": "2025-03-01", "discharge_date": null, "outcome": "на лікуванні"}`

Очікувано: **404**, отримано: **404** ✅

```json
{
  "error": "not_found",
  "message": "запис не знайдено"
}
```

### 15. Видалення

`DELETE /admissions/1`

Очікувано: **204**, отримано: **204** ✅

```json
(порожнє тіло)
```

### 16. Повторне видалення

`DELETE /admissions/1`

Очікувано: **404**, отримано: **404** ✅

```json
{
  "error": "not_found",
  "message": "запис не знайдено"
}
```

### 17. Читання видаленого

`GET /admissions/1`

Очікувано: **404**, отримано: **404** ✅

```json
{
  "error": "not_found",
  "message": "запис не знайдено"
}
```

### 18. [persist] Створення запису перед down

`POST /admissions`

Тіло запиту: `{"patient_code": "P000001", "department": "кардіологія", "room": "12", "admission_date": "2025-03-01", "discharge_date": null, "outcome": "на лікуванні"}`

Очікувано: **201**, отримано: **201** ✅

```json
{
  "id": 2,
  "patient_code": "P000001",
  "department": "кардіологія",
  "room": "12",
  "admission_date": "2025-03-01",
  "discharge_date": null,
  "outcome": "на лікуванні"
}
```

### 19. [persist] Запис на місці після down і up

`GET /admissions/2`

Очікувано: **200**, отримано: **200** ✅

```json
{
  "id": 2,
  "patient_code": "P000001",
  "department": "кардіологія",
  "room": "12",
  "admission_date": "2025-03-01",
  "discharge_date": null,
  "outcome": "на лікуванні"
}
```

### 20. [503] /healthz при зупиненій базі

`GET /healthz`

Очікувано: **503**, отримано: **503** ✅

```json
{
  "status": "unavailable"
}
```

### 21. [503] /healthz після повернення бази

`GET /healthz`

Очікувано: **200**, отримано: **200** ✅

```json
{
  "status": "ok"
}
```

### 22. [persist] Після down -v база порожня

`GET /admissions`

Очікувано: **200**, отримано: **200** ✅

```json
{
  "items": [],
  "total": 0,
  "limit": 20,
  "offset": 0
}
```
