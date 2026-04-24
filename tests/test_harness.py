#!/usr/bin/env python3
"""
Fox test harness — runs complex tasks against the real Ollama agent, logs results,
and identifies failure patterns.

Usage:
    python3 tests/test_harness.py                        # run all tests (auto-detect backend)
    python3 tests/test_harness.py pptx_create            # run one test by name (positional)
    python3 tests/test_harness.py --tests pptx,csv       # comma-separated test names
    python3 tests/test_harness.py --backend openai       # force OpenAI backend
    python3 tests/test_harness.py --backend ollama       # force Ollama backend
    FOX_BACKEND=openai FOX_STRUCTURED_OUTPUT=1 python3 tests/test_harness.py --tests openafc_psd_diff
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

# Make src importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage import Storage
from src.commands import CommandRegistry
from src.mapreduce import MapReduceOrchestrator, save_user_input
from src.ollama import chat, build_system_prompt, configure_backend_for_batch


OPENAFC_PSD_PROMPT = """Analyze the OpenAFC CSV row and my debug logs below.

Do not decompose this task. Use exactly one run_python tool call.
Read the pasted data from user_input.txt and compare programmatically.
When parsing the TSV row, only use the header line that starts with FS_ID and the single data row after it.
When building dictionaries from the CSV row, handle missing fields safely instead of calling .strip() on None.
Do not use generic key=value parsing and do not use eval.
Build log_values with explicit regexes for these labels:
  - Receiver PSD
  - Loss Receivers
  - gamma
  - GRx_Disc
  - GRx_Effective
  - ap_clutterloss

Output requirements:
1. First say whether this is actually the same record or not.
2. Then print a compact table with: field, CSV value, log value, delta (log - csv).
3. Compare at least these mappings:
   - Receiver PSD -> EIRP_LIMIT (dBm) if bandwidth = 1 MHz
   - Loss Receivers -> PATH_LOSS (dB)
   - gamma -> FS_RX_ANGLE_OFF_BORESIGHT (deg)
   - GRx_Disc -> FS_ANT_GAIN_TO_RLAN (dB)
   - GRx_Effective -> FS_ANT_GAIN_TO_RLAN (dB) + FS_ANT_NEAR_FIELD_OFFSET (dB)
   - ap_clutterloss -> PATH_CLUTTER_TX (DB)
4. End with one short conclusion identifying the main reason the PSD does not match.

OpenAFC CSV row:
FS_ID\tFS_REGION\tDBNAME\tRLAN_POSN_IDX\tCALLSIGN\tFS_RX_LONGITUDE (deg)\tFS_RX_LATITUDE (deg)\tFS_RX_HEIGHT_ABOVE_TERRAIN (m)\tFS_RX_TERRAIN_HEIGHT (m)\tFS_RX_TERRAIN_SOURCE\tFS_RX_PROP_ENV\tNUM_PASSIVE_REPEATER\tIS_DIVERSITY_LINK\tSEGMENT_IDX\tSEGMENT_RX_LONGITUDE (deg)\tSEGMENT_RX_LATITUDE (deg)\tPR_REF_THETA_IN (deg)\tPR_REF_KS\tPR_REF_Q\tPR_REF_D0 (dB)\tPR_REF_D1 (dB)\tRLAN_LONGITUDE (deg)\tRLAN_LATITUDE (deg)\tRLAN_HEIGHT_ABOVE_TERRAIN (m)\tRLAN_TERRAIN_HEIGHT (m)\tRLAN_TERRAIN_SOURCE\tRLAN_PROP_ENV\tRLAN_FS_RX_DIST (km)\tRLAN_FS_RX_GROUND_DIST (km)\tRLAN_FS_RX_ELEVATION_ANGLE (deg)\tFS_RX_ANGLE_OFF_BORESIGHT (deg)\tRLAN_TX_EIRP (dBm)\tRLAN_ANTENNA_MODEL\tRLAN_ANGLE_OFF_BORESIGHT (deg)\tRLAN_DISCRIMINATION_GAIN (dB)\tBODY_LOSS (dB)\tRLAN_CLUTTER_CATEGORY\tFS_CLUTTER_CATEGORY\tBUILDING TYPE\tRLAN_FS_RX_BUILDING_PENETRATION (dB)\tBUILDING_PENETRATION_MODEL\tBUILDING_PENETRATION_CDF\tPATH_LOSS (dB)\tPATH_LOSS_MODEL\tPATH_LOSS_CDF\tPATH_CLUTTER_TX (DB)\tPATH_CLUTTER_TX_MODEL\tPATH_CLUTTER_TX_CDF\tPATH_CLUTTER_RX (DB)\tPATH_CLUTTER_RX_MODEL\tPATH_CLUTTER_RX_CDF\tRLAN BANDWIDTH (MHz)\tRLAN CHANNEL START FREQ (MHz)\tRLAN CHANNEL STOP FREQ (MHz)\tULS START FREQ (MHz)\tULS STOP FREQ (MHz)\tFS_ANT_TYPE\tFS_ANT_CATEGORY\tFS_ANT_GAIN_PEAK (dB)\tPR_TYPE (dB)\tPR_EFFECTIVE_GAIN (dB)\tPR_DISCRIMINATION_GAIN (dB)\tFS_ANT_GAIN_TO_RLAN (dB)\tFS_ANT_NEAR_FIELD_XDB\tFS_ANT_NEAR_FIELD_U\tFS_ANT_NEAR_FIELD_EFF\tFS_ANT_NEAR_FIELD_OFFSET (dB)\tRX_SPECTRAL_OVERLAP_LOSS (dB)\tPOLARIZATION_LOSS (dB)\tFS_RX_FEEDER_LOSS (dB)\tFS_RX_PWR (dBW)\tFS I/N (dB)\tEIRP_LIMIT (dBm)\tFS_SEGMENT_DIST (m)\tULS_LINK_DIST (m)\tRLAN_CENTER_FREQ (Hz)\tFS_TX_TO_RLAN_DIST (m)\tPATH_DIFFERENCE (m)\tULS_WAVELENGTH (mm)\tFRESNEL_INDEX\tCOMMENT
124017\tUS\tFSDATA\t0\tWQPJ677\t-78.19\t42.993749999999999\t27.399999999999999\t236.8652344\t3DEP 1 arcsec\t \t0\t0\t0\t-78.19\t42.993749999999999\t\t\t\t\t\t-78.17986111\t42.960972222222225\t11.5\t237.93710327148438\t3DEP 1 arcsec\tRURAL\t3.734277578541207\t3.7368893425340692\t0.023588540550747439\t9.5188356344506868\t22.989700043360187\t\t-1\t0\t0\tDECIDUOUS_TREES\t\tINDOOR_FIXED\t20.5\tFIXED VALUE\t0.5\t119.44045161064102\tINTERP_ITM_115.995482_CLAMPFSPL_ITM_116.040571_CLAMPFSPL\t0.5\t2.6821040568205978\t452_NLCD\t0.5\t0\tNONE\t0.5\t1\t5989\t5990\t5959.8499999999995\t5989.8499999999995\tWINNF-AIP-07:catB2\tOTHER\t37.899999999999999\t\t\t\t22.899999999999999\t\t\t\t0\t-14.77121255\t3\t0\t-114.9616431\t10.267144375898567\t6.722555667\t10535.08182557126\t\t5990\t6879.9917042495435\t79.18745721949017\t50.175729599906276\t3156.4048136786073

My logs:
Processed UUID 3383791,WQPJ679,CF=6226.89,Freq=(6211.89, 6241.89) Mhz,3383791WQPJ679P[1],T[L=1,A=1],R[L=2,A=1],CF=6226.89,fr_id=1|a:na
\tPermissible PSD =12.697432304465806 dBm/MHz [PSD Receiver=12.697432304465806 PSD Diversity=None PSD Passive=None]
\tR-- Receiver PSD= 12.70 Loss Receivers=125.10 Mode=itm-fspl   at AP Loc=(42.96069444444444, -78.18097222222222) ap_ht_m=11.5 dist=6894.64
\tR-- Boresight Angle gamma=5.93 deg GRx_Effective=22.90 GRx_Disc=22.90  deg AZ_Disc=-5.92 deg
\treceiver_clutterloss=0 ap_nlcd_code=81.0  ap_clutterloss=0
\tap_indoor =True Rx Ht= 42.7 m DRx Ht=None m Tx Ht =27.4 m AP ht = 1.5 m
\treceiver_elevation = 364.5205993652344 m transmitter_elev=271.7734375 access_point_elev =272.5072326660156
\tP-- Passive PSD =None Loss Passive Rx=None Mode=None at AP Loc=None
\tP-- gamma=None  GVRx_Disc=None GVRxEffective=None
\tD-- Diversity PSD =None Diversity Loss=None ap_clutterloss=None Mode=None @None
\tD-- Boresight Angle gamma=0.00 deg GDRx_Effective= 0.00
"""

RESULTS_LOG = os.path.join(os.path.dirname(__file__), "results.jsonl")


# ── Test case definition ──────────────────────────────────────────────────────

@dataclass
class TestCase:
    name:        str
    prompt:      str
    check:       Callable[[str, str], bool]   # (result_text, work_dir) -> pass
    check_desc:  str
    setup:       Optional[Callable[[str], None]] = None  # (work_dir) -> None
    timeout:     int = 180


# ── Test cases ────────────────────────────────────────────────────────────────

def check_contains(needle: str):
    def fn(result: str, _work_dir: str) -> bool:
        return needle.lower() in result.lower()
    return fn

def check_file_exists(filename: str):
    def fn(_result: str, work_dir: str) -> bool:
        # Check work_dir and cwd
        return (
            os.path.exists(os.path.join(work_dir, filename)) or
            os.path.exists(filename)
        )
    return fn

def check_file_exists_glob(pattern: str):
    """Check any file matching a glob exists in work_dir or cwd."""
    import glob as globmod
    def fn(_result: str, work_dir: str) -> bool:
        return bool(
            globmod.glob(os.path.join(work_dir, pattern)) or
            globmod.glob(pattern)
        )
    return fn

def check_any(*checks):
    def fn(result: str, work_dir: str) -> bool:
        return any(c(result, work_dir) for c in checks)
    return fn

def check_all(*checks):
    def fn(result: str, work_dir: str) -> bool:
        return all(c(result, work_dir) for c in checks)
    return fn


def _contains_number(text: str, target: float, tol: float = 0.05) -> bool:
    for match in re.findall(r"-?\d+(?:\.\d+)?", text):
        try:
            if abs(float(match) - target) <= tol:
                return True
        except ValueError:
            continue
    return False


def check_openafc_psd_analysis(result: str, _work_dir: str) -> bool:
    lower = result.lower()
    identity_mismatch = (
        ("wqpj677" in lower and "wqpj679" in lower) or
        ("callsign" in lower and any(word in lower for word in ("different", "mismatch", "not the same"))) or
        ("5989" in lower and "6226.89" in lower)
    )
    psd_compared = (
        (_contains_number(result, 12.6974, 0.05) and _contains_number(result, 6.7226, 0.05)) or
        _contains_number(result, 5.9749, 0.1)
    )
    path_loss_compared = (
        (_contains_number(result, 125.10, 0.05) and _contains_number(result, 119.4405, 0.05)) or
        _contains_number(result, 5.6595, 0.1)
    )
    return identity_mismatch and psd_compared and path_loss_compared


TESTS: list[TestCase] = [
    TestCase(
        name="simple_list",
        prompt="List the python files in the current directory using a tool.",
        check=check_contains(".py"),
        check_desc="response mentions .py files",
    ),
    TestCase(
        name="bash_calculation",
        prompt="Calculate the sum of squares of 1 through 10 (1²+2²+…+10²) and print the answer.",
        check=check_contains("385"),
        check_desc="response contains 385",
    ),
    TestCase(
        name="write_txt_file",
        prompt="Create a file named fox_hello.txt with the content 'hello from fox'.",
        check=check_any(
            check_file_exists("fox_hello.txt"),
            check_contains("wrote"),
            check_contains("created"),
            check_contains("fox_hello.txt"),
        ),
        check_desc="fox_hello.txt created or response confirms creation",
    ),
    TestCase(
        name="csv_create",
        prompt=(
            "Create a CSV file named employees.csv with columns: name,age,city "
            "and exactly 3 rows of sample data."
        ),
        check=check_any(
            check_file_exists("employees.csv"),
            check_file_exists_glob("*.csv"),
            check_contains("employees.csv"),
        ),
        check_desc="employees.csv created",
    ),
    TestCase(
        name="pptx_create",
        prompt=(
            "Create a PowerPoint file named fox_test.pptx with two slides:\n"
            "Slide 1 title: 'Fox Agent Test'\n"
            "Slide 2 title: 'Results' with bullet: 'Tool calling works'\n"
            "Use python-pptx via run_bash."
        ),
        check=check_any(
            check_file_exists("fox_test.pptx"),
            check_file_exists_glob("*.pptx"),
            check_contains("fox_test.pptx"),
        ),
        check_desc="fox_test.pptx created",
    ),
    TestCase(
        name="pptx_from_context",
        prompt=(
            "Here is slide content:\n\n"
            "Slide 1: Introduction\n"
            "- AI is transforming how we work\n"
            "- Three pillars: Speed, Quality, Scale\n\n"
            "Slide 2: Key Results\n"
            "- 40% faster delivery\n"
            "- 60% fewer defects\n\n"
            "Create a PowerPoint file named context_test.pptx from this data using python-pptx."
        ),
        check=check_any(
            check_file_exists("context_test.pptx"),
            check_file_exists_glob("context_test.pptx"),
            check_contains("context_test.pptx"),
        ),
        check_desc="context_test.pptx created from inline data",
    ),
    TestCase(
        name="internet_fetch",
        prompt="Use curl to fetch https://wttr.in/?format=3 and report what it returns.",
        check=check_any(
            check_contains("°"),
            check_contains("wttr"),
            check_contains("weather"),
            check_contains("+"),
        ),
        check_desc="response contains weather data",
    ),
    TestCase(
        name="openafc_psd_diff",
        prompt=OPENAFC_PSD_PROMPT,
        check=check_openafc_psd_analysis,
        check_desc=(
            "response flags the record mismatch and compares PSD (12.697 vs 6.723) "
            "plus path loss (125.10 vs 119.44)"
        ),
    ),
]


# ── Runner ────────────────────────────────────────────────────────────────────

@dataclass
class TestResult:
    name:        str
    passed:      bool
    result_text: str
    elapsed:     float
    error:       Optional[str] = None
    tools_used:  list[str] = field(default_factory=list)
    nudged:      bool = False


def run_test(tc: TestCase) -> TestResult:
    configure_backend_for_batch()
    work_dir = tempfile.mkdtemp(prefix=f"fox_test_{tc.name}_")
    # Use isolated DB per test to avoid DuckDB lock conflicts
    db_path = os.path.join(work_dir, "test.duckdb")
    storage = Storage(db_path=db_path)
    session_id = f"test-{uuid.uuid4().hex[:8]}"
    storage.create_session(session_id, "test", os.getcwd())

    if tc.setup:
        tc.setup(work_dir)

    registry = CommandRegistry(work_dir, storage)
    orchestrator = MapReduceOrchestrator(
        llm_fn=chat,
        command_registry=registry,
        storage=storage,
        session_id=session_id,
        work_dir=work_dir,
    )
    messages = [{"role": "system", "content": build_system_prompt(work_dir)}]

    t0 = time.time()
    error = None
    result_text = ""
    try:
        data_file = save_user_input(tc.prompt, work_dir)
        result_text = orchestrator.execute(tc.prompt, messages, data_file=data_file)
    except Exception as e:
        error = str(e)
        result_text = ""
    elapsed = time.time() - t0

    # Collect tool names used from storage
    try:
        rows = storage.query(
            f"SELECT DISTINCT tool_name FROM tool_calls WHERE session_id = '{session_id}'"
        )
        tools_used = [r["tool_name"] for r in rows]
    except Exception:
        tools_used = []

    storage.close()

    passed = False
    if not error:
        try:
            passed = tc.check(result_text, work_dir)
            # Also check cwd for files
            if not passed:
                passed = tc.check(result_text, os.getcwd())
        except Exception:
            passed = False

    return TestResult(
        name=tc.name,
        passed=passed,
        result_text=result_text[:500],
        elapsed=elapsed,
        error=error,
        tools_used=tools_used,
    )


def log_result(r: TestResult):
    with open(RESULTS_LOG, "a") as f:
        f.write(json.dumps({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "name":      r.name,
            "passed":    r.passed,
            "elapsed":   round(r.elapsed, 1),
            "tools":     r.tools_used,
            "error":     r.error,
            "result":    r.result_text,
        }) + "\n")


def print_result(r: TestResult):
    icon = "✅" if r.passed else "❌"
    print(f"\n{icon} [{r.elapsed:.0f}s] {r.name}")
    if r.error:
        print(f"   ERROR: {r.error}")
    else:
        print(f"   tools: {', '.join(r.tools_used) or '(none)'}")
        preview = r.result_text[:200].replace("\n", " ")
        print(f"   result: {preview}")
    if not r.passed:
        tc = next((t for t in TESTS if t.name == r.name), None)
        if tc:
            print(f"   ⚠ expected: {tc.check_desc}")


# ── Main ──────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fox Test Harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "positional_test", nargs="?", metavar="TEST_NAME",
        help="Single test name (positional, for backwards compatibility)",
    )
    parser.add_argument(
        "--tests", "-t", metavar="NAMES",
        help="Comma-separated test names to run (e.g. pptx_create,csv_create)",
    )
    parser.add_argument(
        "--backend", "-b", choices=["ollama", "openai"],
        help="Force backend (overrides FOX_BACKEND env var)",
    )
    parser.add_argument(
        "--structured", action="store_true", default=None,
        help="Enable structured output (sets FOX_STRUCTURED_OUTPUT=1)",
    )
    return parser.parse_args()


def main():
    args = _parse_args()

    # Apply backend override before configure_backend_for_batch is called
    if args.backend:
        os.environ["FOX_BACKEND"] = args.backend
    if args.structured:
        os.environ["FOX_STRUCTURED_OUTPUT"] = "1"

    # Build test filter: --tests wins; positional is fallback
    names_filter: list[str] = []
    if args.tests:
        names_filter = [n.strip() for n in args.tests.split(",") if n.strip()]
    elif args.positional_test:
        names_filter = [args.positional_test]

    suite = [t for t in TESTS if not names_filter or t.name in names_filter]

    if not suite:
        available = [t.name for t in TESTS]
        print(f"No matching tests. Available: {available}")
        sys.exit(1)

    backend_label = os.environ.get("FOX_BACKEND", "auto")
    structured_label = "structured=on" if os.environ.get("FOX_STRUCTURED_OUTPUT") == "1" else "structured=off"
    print(f"\n🦊 Fox Test Harness — {len(suite)} test(s) | backend={backend_label} | {structured_label}\n{'─'*50}")

    results = []
    for tc in suite:
        print(f"\n▶ Running: {tc.name}")
        print(f"  prompt: {tc.prompt[:100]}...")
        r = run_test(tc)
        log_result(r)
        print_result(r)
        results.append(r)

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    total_time = sum(r.elapsed for r in results)

    print(f"\n{'─'*50}")
    print(f"Results: {passed}/{len(results)} passed  |  {total_time:.0f}s total")
    print(f"Log: {RESULTS_LOG}\n")

    # Failure analysis
    failures = [r for r in results if not r.passed]
    if failures:
        print("Failure patterns:")
        for r in failures:
            no_tools = not r.tools_used
            used_bash = "run_bash" in r.tools_used
            print(f"  {r.name}: tools={r.tools_used or '[]'}, "
                  f"{'NO TOOLS CALLED' if no_tools else ''}"
                  f"{'bash ok but no file' if used_bash and not r.passed else ''}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
