---
name: xlsx
description: Создание, чтение и редактирование Excel-таблиц (.xlsx). Используй когда нужно создать таблицу, посчитать формулы, отформатировать данные, построить график, или прочитать/изменить существующий .xlsx/.csv файл. Триггеры — "таблица", "excel", "xlsx", "посчитай в таблице", "сделай отчёт в таблице".
---

# xlsx — работа с Excel-таблицами

Создавай и редактируй `.xlsx` через **openpyxl** (Python). Результат отправляй пользователю тегом `[FILE:/path]`.

## Python и библиотеки

Запускай скрипты через **`$AIOS_ROOT/.venv/bin/python`** — там предустановлены `openpyxl` и `pandas`.
Если библиотеки вдруг нет: `$AIOS_ROOT/.venv/bin/pip install openpyxl`.

## Создание таблицы
```python
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

wb = Workbook()
ws = wb.active
ws.title = "Отчёт"

# Заголовки
headers = ["Месяц", "Доход", "Расход", "Прибыль"]
ws.append(headers)
for cell in ws[1]:
    cell.font = Font(bold=True, color="FFFFFF")
    cell.fill = PatternFill("solid", fgColor="4472C4")
    cell.alignment = Alignment(horizontal="center")

# Данные
ws.append(["Январь", 100000, 60000, "=B2-C2"])   # формула прямо в ячейке
ws.append(["Февраль", 120000, 70000, "=B3-C3"])

# Ширина колонок
for col, width in zip("ABCD", [12, 12, 12, 12]):
    ws.column_dimensions[col].width = width

wb.save("/abs/path/report.xlsx")
```

## Чтение существующего файла
```python
from openpyxl import load_workbook
wb = load_workbook("/abs/path/data.xlsx", data_only=True)  # data_only=True → значения формул
ws = wb.active
for row in ws.iter_rows(values_only=True):
    print(row)
```

## График
```python
from openpyxl.chart import BarChart, Reference
chart = BarChart()
data = Reference(ws, min_col=2, min_row=1, max_col=4, max_row=ws.max_row)
cats = Reference(ws, min_col=1, min_row=2, max_row=ws.max_row)
chart.add_data(data, titles_from_data=True)
chart.set_categories(cats)
ws.add_chart(chart, "F2")
```

## CSV
Для `.csv` используй stdlib `csv` или `pandas` (если установлен). Для конвертации CSV→XLSX — прочитай csv, запиши через openpyxl.

## Правила
- Пути — абсолютные (внутри AIOS_ROOT или /tmp).
- Готовый файл → `[FILE:/path/report.xlsx]`.
- Большие данные не печатай в чат — сразу в файл.
