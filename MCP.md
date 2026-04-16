# Tool Reference

You have full internet access and a Linux shell.

## search_examples — look up working playbooks

Before attempting a complex or unfamiliar task (file creation, data processing, API calls), call:
```
search_examples(query="create a pptx presentation")
```
Returns the exact tool sequences from similar completed tasks — copy them rather than guessing.


## run_bash — shell & internet

```bash
# HTTP
curl -s "URL"
curl -s -A "Mozilla/5.0" "URL"          # with user-agent

# Stocks
curl -s -A "Mozilla/5.0" "https://query1.finance.yahoo.com/v8/finance/chart/TICKER?range=1d" \
  | jq '.chart.result[0].meta | {symbol,regularMarketPrice,currency}'

# Weather
curl -s "https://wttr.in/City?format=3"

# Install a package (always append && echo OK — pip -q is silent on success)
pip install python-pptx -q && echo OK

# jq / grep / awk
cat f.json | jq '.field'
grep -rn "pattern" /path
awk -F'\t' '{print $1,$3}' file
```

## Binary file creation — ALWAYS use run_bash

write_file is for text files only (.py, .txt, .csv, .md, .json). For binary formats use run_bash.

### .pptx (python-pptx)
```bash
pip install python-pptx -q && echo OK
```
```python
from pptx import Presentation
from pptx.util import Inches, Pt

prs = Presentation()
layout = prs.slide_layouts[1]          # title + content

slide = prs.slides.add_slide(layout)
slide.shapes.title.text = "Slide Title"
slide.placeholders[1].text = "Bullet 1\nBullet 2\nBullet 3"

prs.save("/path/to/output.pptx")
print("Saved /path/to/output.pptx")
```

### .xlsx (openpyxl)
```bash
pip install openpyxl -q && echo OK
```
```python
from openpyxl import Workbook

wb = Workbook()
ws = wb.active
ws.title = "Sheet1"
ws.append(["Name", "Value"])           # header row
ws.append(["Alpha", 42])

wb.save("/path/to/output.xlsx")
print("Saved /path/to/output.xlsx")
```

### images (pillow)
```bash
pip install pillow -q && echo OK
```
```python
from PIL import Image, ImageDraw

img = Image.new("RGB", (800, 600), "white")
draw = ImageDraw.Draw(img)
draw.text((100, 100), "Hello", fill="black")
img.save("/path/to/output.png")
print("Saved /path/to/output.png")
```

## run_python — data processing (stdlib only)

### Parse TSV/CSV from user_input.txt:
```python
import csv
with open("/tmp/fox_work_xxx/user_input.txt") as f:
    text = f.read()
lines = text.strip().split("\n")
tsv_lines = [l for l in lines if "\t" in l and l.count("\t") >= 2]
if tsv_lines:
    reader = csv.DictReader(tsv_lines, delimiter="\t")
    for row in reader:
        for k, v in row.items():
            print(f"  {k.strip()} = {v.strip()}")
```

### Parse key=value log lines:
```python
import re
with open("/tmp/fox_work_xxx/user_input.txt") as f:
    text = f.read()
pairs = re.findall(r'(\w+)\s*=\s*([\d.\-]+)', text)
log_data = {k: float(v) for k, v in pairs}
for k, v in log_data.items():
    print(f"  {k} = {v}")
```

### Compare two datasets:
```python
import re, csv
with open("/tmp/fox_work_xxx/user_input.txt") as f:
    text = f.read()

# 1. Parse TSV
lines = text.strip().split("\n")
tsv_lines = [l for l in lines if "\t" in l and l.count("\t") >= 2]
reader = csv.DictReader(tsv_lines, delimiter="\t")
csv_row = next(reader)
csv_data = {k.strip(): v.strip() for k, v in csv_row.items()}

# 2. Parse key=value from logs
pairs = re.findall(r'(\w+)\s*=\s*([\d.\-]+)', text)
log_data = {k: v for k, v in pairs}

# 3. Define mapping: log_key -> csv_key
mapping = {
    "Loss": "PATH_LOSS (dB)",
    "gamma": "FS_RX_ANGLE_OFF_BORESIGHT (deg)",
}

# 4. Compare
print(f"{'Log Field':<20} {'Log Value':<15} {'CSV Field':<35} {'CSV Value':<15} {'Diff'}")
print("-" * 100)
for log_key, csv_key in mapping.items():
    log_val = float(log_data.get(log_key, 0))
    csv_val = float(csv_data.get(csv_key, 0))
    diff = log_val - csv_val
    print(f"{log_key:<20} {log_val:<15.4f} {csv_key:<35} {csv_val:<15.4f} {diff:+.4f}")
```

## RULES
- NEVER say you cannot access the internet. Use curl.
- NEVER use pandas, numpy, or third-party libs in run_python (stdlib only).
- NEVER hardcode data values. Read from the file and parse programmatically.
- ALWAYS print actual numbers, not just "Match" or "Mismatch".
- BINARY FILES: write_file cannot create .pptx/.xlsx/.docx/.pdf/.png — use the patterns above.
- pip install: always append `&& echo OK` — silent success otherwise looks like failure.
