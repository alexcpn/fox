# Tool Reference

You have full internet access and a Linux shell.

## curl
```
curl -s "URL"                          # GET
curl -s -A "Mozilla/5.0" "URL"         # with user-agent
```
Stocks: `curl -s -A "Mozilla/5.0" "https://query1.finance.yahoo.com/v8/finance/chart/TICKER?range=1d" | jq '.chart.result[0].meta | {symbol,regularMarketPrice,currency}'`
Weather: `curl -s "https://wttr.in/City?format=3"`

## jq, grep, awk
```
cat f.json | jq '.field'
grep -rn "pattern" /path
awk -F'\t' '{print $1,$3}' file
```

## run_python — PATTERNS TO FOLLOW

IMPORTANT: Always read data from files. Never hardcode values. Use only stdlib.

### Parse TSV/CSV from a file:
```python
import csv
with open("/tmp/agent_work_xxx/user_input.txt") as f:
    text = f.read()
# Find TSV lines (lines with tabs)
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
with open("/tmp/agent_work_xxx/user_input.txt") as f:
    text = f.read()
# Extract key=value pairs
pairs = re.findall(r'(\w+)\s*=\s*([\d.\-]+)', text)
log_data = {k: float(v) for k, v in pairs}
for k, v in log_data.items():
    print(f"  {k} = {v}")
```

### Compare two datasets with a mapping:
```python
import re, csv
with open("/tmp/agent_work_xxx/user_input.txt") as f:
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
- NEVER use pandas, numpy, or third-party libs in run_python.
- NEVER hardcode data values. Read from the file and parse.
- ALWAYS print actual numbers, not just "Match" or "Mismatch".
