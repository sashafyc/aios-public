---
name: pptx
description: Создание и редактирование презентаций PowerPoint (.pptx). Используй когда нужно сделать презентацию, слайды, питч-дек, или отредактировать существующий .pptx. Триггеры — "презентация", "слайды", "powerpoint", "pptx", "питч-дек", "сделай презентацию".
---

# pptx — работа с презентациями

Создавай `.pptx` через **python-pptx**. Результат отправляй тегом `[FILE:/path]`.

## Python и библиотеки

Запускай скрипты через **`$AIOS_ROOT/.venv/bin/python`** — там предустановлен `python-pptx`.
Если нет: `$AIOS_ROOT/.venv/bin/pip install python-pptx`.

## Создание презентации
```python
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor

prs = Presentation()

# Титульный слайд
slide = prs.slides.add_slide(prs.slide_layouts[0])
slide.shapes.title.text = "Заголовок презентации"
slide.placeholders[1].text = "Подзаголовок / автор"

# Слайд с маркированным списком
slide = prs.slides.add_slide(prs.slide_layouts[1])
slide.shapes.title.text = "Ключевые пункты"
body = slide.placeholders[1].text_frame
body.text = "Первый пункт"
for txt in ["Второй пункт", "Третий пункт"]:
    p = body.add_paragraph()
    p.text = txt
    p.level = 0

# Слайд с заголовком и свободным текстом
slide = prs.slides.add_slide(prs.slide_layouts[5])
slide.shapes.title.text = "Раздел"
box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(8), Inches(3))
tf = box.text_frame
tf.text = "Произвольный текст"
tf.paragraphs[0].font.size = Pt(24)
tf.paragraphs[0].font.color.rgb = RGBColor(0x44, 0x72, 0xC4)

prs.save("/abs/path/deck.pptx")
```

## Картинка на слайде
```python
slide.shapes.add_picture("/abs/path/image.png", Inches(1), Inches(1), width=Inches(4))
```

## Чтение/редактирование
```python
prs = Presentation("/abs/path/existing.pptx")
for slide in prs.slides:
    for shape in slide.shapes:
        if shape.has_text_frame:
            print(shape.text_frame.text)
```

## Полезные layout-индексы (стандартный шаблон)
- `0` — титульный, `1` — заголовок+контент, `5` — только заголовок, `6` — пустой.

## Правила
- Пути — абсолютные (внутри AIOS_ROOT или /tmp).
- Готовый файл → `[FILE:/path/deck.pptx]`.
- Картинки для слайдов сначала создай/скачай, потом вставляй.
