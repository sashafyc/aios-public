---
name: pdf
description: "Работа с PDF файлами: чтение, извлечение текста/таблиц, объединение, разделение, создание новых PDF (reportlab), конвертация HTML→PDF (Puppeteer / Chrome headless), OCR сканов. Используй когда пользователь упоминает .pdf файл, хочет создать/прочитать/объединить PDF, сделать PDF из HTML, извлечь данные из PDF."
---

# PDF Processing Guide

## Python environment

Запускай скрипты через **`$AIOS_ROOT/.venv/bin/python`** — там предустановлены `pypdf`, `reportlab`, `pdfplumber`.
Для OCR (`pytesseract`) — тяжёлая зависимость, ставь по необходимости.
Готовый PDF отправляй пользователю тегом `[FILE:/path]`.

> **НОТА: HTML→PDF требует Node.js + puppeteer** (тянет Chromium ~150MB).
> Установка: `npm install -g puppeteer` или локально (`npm install puppeteer`).
> На сервере: проверь что Chromium-зависимости стоят (`apt install -y chromium-browser`,
> либо puppeteer сам качает Chromium при установке).
> Если Node/puppeteer нет — для простых PDF из текста используй reportlab
> (Python, без внешних зависимостей).

## Overview

This guide covers essential PDF processing operations using Python libraries and command-line tools. For advanced features, JavaScript libraries, and detailed examples, see REFERENCE.md.

## Quick Start

```python
from pypdf import PdfReader, PdfWriter

# Read a PDF
reader = PdfReader("document.pdf")
print(f"Pages: {len(reader.pages)}")

# Extract text
text = ""
for page in reader.pages:
    text += page.extract_text()
```

## Python Libraries

### pypdf - Basic Operations

#### Merge PDFs
```python
from pypdf import PdfWriter, PdfReader

writer = PdfWriter()
for pdf_file in ["doc1.pdf", "doc2.pdf", "doc3.pdf"]:
    reader = PdfReader(pdf_file)
    for page in reader.pages:
        writer.add_page(page)

with open("merged.pdf", "wb") as output:
    writer.write(output)
```

#### Split PDF
```python
reader = PdfReader("input.pdf")
for i, page in enumerate(reader.pages):
    writer = PdfWriter()
    writer.add_page(page)
    with open(f"page_{i+1}.pdf", "wb") as output:
        writer.write(output)
```

#### Extract Metadata
```python
reader = PdfReader("document.pdf")
meta = reader.metadata
print(f"Title: {meta.title}")
print(f"Author: {meta.author}")
print(f"Subject: {meta.subject}")
print(f"Creator: {meta.creator}")
```

#### Rotate Pages
```python
reader = PdfReader("input.pdf")
writer = PdfWriter()

page = reader.pages[0]
page.rotate(90)  # Rotate 90 degrees clockwise
writer.add_page(page)

with open("rotated.pdf", "wb") as output:
    writer.write(output)
```

### pdfplumber - Text and Table Extraction

#### Extract Text with Layout
```python
import pdfplumber

with pdfplumber.open("document.pdf") as pdf:
    for page in pdf.pages:
        text = page.extract_text()
        print(text)
```

#### Extract Tables
```python
with pdfplumber.open("document.pdf") as pdf:
    for i, page in enumerate(pdf.pages):
        tables = page.extract_tables()
        for j, table in enumerate(tables):
            print(f"Table {j+1} on page {i+1}:")
            for row in table:
                print(row)
```

#### Advanced Table Extraction
```python
import pandas as pd

with pdfplumber.open("document.pdf") as pdf:
    all_tables = []
    for page in pdf.pages:
        tables = page.extract_tables()
        for table in tables:
            if table:  # Check if table is not empty
                df = pd.DataFrame(table[1:], columns=table[0])
                all_tables.append(df)

# Combine all tables
if all_tables:
    combined_df = pd.concat(all_tables, ignore_index=True)
    combined_df.to_excel("extracted_tables.xlsx", index=False)
```

### reportlab - Create PDFs

#### Basic PDF Creation
```python
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

c = canvas.Canvas("hello.pdf", pagesize=letter)
width, height = letter

# Add text
c.drawString(100, height - 100, "Hello World!")
c.drawString(100, height - 120, "This is a PDF created with reportlab")

# Add a line
c.line(100, height - 140, 400, height - 140)

# Save
c.save()
```

#### Create PDF with Multiple Pages
```python
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet

doc = SimpleDocTemplate("report.pdf", pagesize=letter)
styles = getSampleStyleSheet()
story = []

# Add content
title = Paragraph("Report Title", styles['Title'])
story.append(title)
story.append(Spacer(1, 12))

body = Paragraph("This is the body of the report. " * 20, styles['Normal'])
story.append(body)
story.append(PageBreak())

# Page 2
story.append(Paragraph("Page 2", styles['Heading1']))
story.append(Paragraph("Content for page 2", styles['Normal']))

# Build PDF
doc.build(story)
```

#### Subscripts and Superscripts

**IMPORTANT**: Never use Unicode subscript/superscript characters (₀₁₂₃₄₅₆₇₈₉, ⁰¹²³⁴⁵⁶⁷⁸⁹) in ReportLab PDFs. The built-in fonts do not include these glyphs, causing them to render as solid black boxes.

Instead, use ReportLab's XML markup tags in Paragraph objects:
```python
from reportlab.platypus import Paragraph
from reportlab.lib.styles import getSampleStyleSheet

styles = getSampleStyleSheet()

# Subscripts: use <sub> tag
chemical = Paragraph("H<sub>2</sub>O", styles['Normal'])

# Superscripts: use <super> tag
squared = Paragraph("x<super>2</super> + y<super>2</super>", styles['Normal'])
```

For canvas-drawn text (not Paragraph objects), manually adjust font the size and position rather than using Unicode subscripts/superscripts.

## Command-Line Tools

### pdftotext (poppler-utils)
```bash
# Extract text
pdftotext input.pdf output.txt

# Extract text preserving layout
pdftotext -layout input.pdf output.txt

# Extract specific pages
pdftotext -f 1 -l 5 input.pdf output.txt  # Pages 1-5
```

### qpdf
```bash
# Merge PDFs
qpdf --empty --pages file1.pdf file2.pdf -- merged.pdf

# Split pages
qpdf input.pdf --pages . 1-5 -- pages1-5.pdf
qpdf input.pdf --pages . 6-10 -- pages6-10.pdf

# Rotate pages
qpdf input.pdf output.pdf --rotate=+90:1  # Rotate page 1 by 90 degrees

# Remove password
qpdf --password=mypassword --decrypt encrypted.pdf decrypted.pdf
```

### pdftk (if available)
```bash
# Merge
pdftk file1.pdf file2.pdf cat output merged.pdf

# Split
pdftk input.pdf burst

# Rotate
pdftk input.pdf rotate 1east output rotated.pdf
```

## Common Tasks

### Extract Text from Scanned PDFs
```python
# Requires: pip install pytesseract pdf2image
import pytesseract
from pdf2image import convert_from_path

# Convert PDF to images
images = convert_from_path('scanned.pdf')

# OCR each page
text = ""
for i, image in enumerate(images):
    text += f"Page {i+1}:\n"
    text += pytesseract.image_to_string(image)
    text += "\n\n"

print(text)
```

### Add Watermark
```python
from pypdf import PdfReader, PdfWriter

# Create watermark (or load existing)
watermark = PdfReader("watermark.pdf").pages[0]

# Apply to all pages
reader = PdfReader("document.pdf")
writer = PdfWriter()

for page in reader.pages:
    page.merge_page(watermark)
    writer.add_page(page)

with open("watermarked.pdf", "wb") as output:
    writer.write(output)
```

### Extract Images
```bash
# Using pdfimages (poppler-utils)
pdfimages -j input.pdf output_prefix

# This extracts all images as output_prefix-000.jpg, output_prefix-001.jpg, etc.
```

### Password Protection
```python
from pypdf import PdfReader, PdfWriter

reader = PdfReader("input.pdf")
writer = PdfWriter()

for page in reader.pages:
    writer.add_page(page)

# Add password
writer.encrypt("userpassword", "ownerpassword")

with open("encrypted.pdf", "wb") as output:
    writer.write(output)
```

### HTML to PDF (Puppeteer)

Используй Puppeteer (Chrome headless) когда нужно конвертировать готовый HTML-файл (с CSS, шрифтами, картинками, тёмной темой) в PDF. Это предпочтительный способ для стилизованных документов: КП, лендингов, отчётов с кастомным дизайном. Chrome рендерит точно как браузер — корректно подтягивает Google Fonts, CSS-градиенты, base64-картинки.

```bash
node $AIOS_ROOT/skills/pdf/scripts/html_to_pdf.js <input.html> [output.pdf] [--a4]
```

- **По умолчанию** (без флага) — одна длинная страница (ширина A4 = 794px, высота = высота контента, без разрывов). Подходит для КП, лендингов и стилизованных доков с тёмной темой / base64-картинками: PDF получается как непрерывный скролл-сайт.
- **`--a4`** — обычная A4-пагинация с полями (для классических многостраничных документов).
- Если `output.pdf` не указан — рядом с входным файлом создаётся `<имя>.pdf`.

**ВАЖНО: НЕ используй `@media print` в CSS** — он ломает тёмную тему и фон (Chrome в печати применяет другие правила). `printBackground: true` в скрипте уже включён, фоны рендерятся как на экране.

## Quick Reference

| Task | Best Tool | Command/Code |
|------|-----------|--------------|
| Merge PDFs | pypdf | `writer.add_page(page)` |
| Split PDFs | pypdf | One page per file |
| Extract text | pdfplumber | `page.extract_text()` |
| Extract tables | pdfplumber | `page.extract_tables()` |
| Create PDFs from scratch | reportlab | Canvas or Platypus |
| **HTML/CSS to PDF** | **Puppeteer** | `node scripts/html_to_pdf.js in.html [out.pdf] [--a4]` |
| Command line merge | qpdf | `qpdf --empty --pages ...` |
| OCR scanned PDFs | pytesseract | Convert to image first |

## Next Steps

- For advanced pypdfium2 usage, see REFERENCE.md
- For JavaScript libraries (pdf-lib), see REFERENCE.md
- For troubleshooting guides, see REFERENCE.md
