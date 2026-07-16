"""
Step the MC temperature through user-defined temperature ranges with a set time to dwell
at each setpoint through the DECS<->VISA socket server.

Example:
    python mc_temp_sequence.py --temp-range 10mK - 2.5K --step-period 120 minutes

This script logs one CSV row per sample so the run can be reviewed or attached
to engineering notes later.
"""

from __future__ import annotations

import argparse
import csv
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyvisa as visa

import Proteox_helpers as ph
from decs_visa_tools.decs_visa_settings import HOST
from decs_visa_tools.decs_visa_settings import PORT
from decs_visa_tools.decs_visa_settings import READ_DELIM
from decs_visa_tools.decs_visa_settings import SHUTDOWN
from decs_visa_tools.decs_visa_settings import WRITE_DELIM


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output"
DEFAULT_TIMEOUT_MS = 10000
DEFAULT_DWELL_MINUTES = 60.0
DEFAULT_TIMEOUT_MINUTES = 90.0

decs_visa_path = './decs_visa.py'

@dataclass
class Sample:
    timestamp: datetime
    elapsed_s: float
    step_index: int
    step_elapsed_s: float
    target_mc_setpoint_k: float
    mc_temp_k: float
    mc_setpoint_k: float
    mc_heater_power_w: float
    still_temp_k: float
    still_heater_power_w: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Step MC temperature setpoint upward and dwell at each setpoint."
        )
    )
    parser.add_argument(
        "--mc-setpoints-k",
        nargs="+",
        type=float,
        required=True,
        help="MC setpoints in K, for example: --mc-setpoints-k 0.006 0.008 0.010",
    )
    parser.add_argument(
        "--sample-period",
        type=float,
        default=15.0,
        help="Seconds between samples. Default: 15",
    )
    parser.add_argument(
        "--still-power-mw",
        type=float,
        default=0,
        help="Still heater power in mW during the run. Default: 0",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=None,
        help="Optional CSV output path. Default: src/output/<timestamp>_mc_temp_sequence.csv",
    )
    parser.add_argument(
        "--start-server",
        action="store_true",
        help="Start the local DECS<->VISA server before connecting.",
    )
    parser.add_argument(
        "--server-wait-seconds",
        type=float,
        default=2.0,
        help="Wait time after starting the local DECS<->VISA server. Default: 2",
    )
    parser.add_argument(
        "--shutdown-server",
        action="store_true",
        help="Send SHUTDOWN to the DECS<->VISA server on exit.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.sample_period <= 0:
        raise ValueError("--sample-period must be positive.")
    if any(setpoint < 0 for setpoint in args.mc_setpoints_k):
        raise ValueError("MC setpoints must be non-negative.")
    if args.mc_setpoints_k != sorted(args.mc_setpoints_k):
        raise ValueError("MC setpoints must be provided in increasing order.")
    if args.still_power_mw < 0:
        raise ValueError("--still-power-mw must be non-negative.")


def default_output_path() -> Path:
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_OUTPUT_DIR / f"{stamp}_mc_temp_sequence.csv"


def start_decs_visa_server(wait_seconds: float) -> subprocess.Popen[bytes]:
    decs_visa_path = SCRIPT_DIR / "decs_visa.py"
    command = [sys.executable, str(decs_visa_path)]
    kwargs = {"cwd": str(SCRIPT_DIR)}
    if platform.system().lower().startswith("win"):
        process = subprocess.Popen(command, **kwargs)
    else:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )
    time.sleep(wait_seconds)
    return process


def connect_decs_visa():
    rm = visa.ResourceManager("@py")
    connection = f"TCPIP0::{HOST}::{PORT}::SOCKET"
    try:
        decs_visa = rm.open_resource(connection)
        decs_visa.read_termination = WRITE_DELIM
        decs_visa.write_termination = READ_DELIM
        decs_visa.chunk_size = 204800
        decs_visa.timeout = DEFAULT_TIMEOUT_MS
    except Exception as exc:
        try:
            rm.close()
        except Exception:
            pass
        raise RuntimeError(
            "Could not connect to the local DECS<->VISA socket server at "
            f"{connection}. If the server is not already running, use "
            "--start-server or run the PowerShell launcher. If it is already "
            "running, check decs_visa.log for WAMP startup/authentication issues."
        ) from exc
    return rm, decs_visa


def set_mc_setpoint(decs_visa, setpoint_k: float) -> str:
    return decs_visa.query(f"set_MC_T:{setpoint_k}").strip()


def set_still_heater_power(decs_visa, still_power_w: float) -> str:
    if still_power_w == 0:
        return ph.still_heater_off(decs_visa).strip()
    return ph.set_still_heater_power(decs_visa, still_power_w).strip()


def read_float(decs_visa, command: str) -> float:
    return float(decs_visa.query(command).strip())


def read_status(
    decs_visa,
    step_index: int,
    step_elapsed_s: float,
    run_elapsed_s: float,
    target_mc_setpoint_k: float,
) -> Sample:
    return Sample(
        timestamp=datetime.now(),
        elapsed_s=run_elapsed_s,
        step_index=step_index,
        step_elapsed_s=step_elapsed_s,
        target_mc_setpoint_k=target_mc_setpoint_k,
        mc_temp_k=read_float(decs_visa, "get_MC_T"),
        mc_setpoint_k=read_float(decs_visa, "get_MC_T_SP"),
        mc_heater_power_w=read_float(decs_visa, "get_MC_H"),
        still_temp_k=read_float(decs_visa, "get_STILL_T"),
        still_heater_power_w=read_float(decs_visa, "get_STILL_H"),
    )


def sample_to_row(sample: Sample) -> dict[str, object]:
    return {
        "timestamp": sample.timestamp.isoformat(),
        "elapsed_s": round(sample.elapsed_s, 1),
        "step_index": sample.step_index,
        "step_elapsed_s": round(sample.step_elapsed_s, 1),
        "target_mc_setpoint_k": sample.target_mc_setpoint_k,
        # "target_mc_setpoint_mk": sample.target_mc_setpoint_k * 1000.0,
        "mc_temp_k": sample.mc_temp_k,
        # "mc_temp_mk": sample.mc_temp_k * 1000.0,
        "mc_setpoint_k": sample.mc_setpoint_k,
        # "mc_setpoint_mk": sample.mc_setpoint_k * 1000.0,
        "mc_heater_power_w": sample.mc_heater_power_w,
        # "mc_heater_power_uw": sample.mc_heater_power_w * 1e6,
        "still_temp_k": sample.still_temp_k,
        # "still_temp_mk": sample.still_temp_k * 1000.0,
        "still_heater_power_w": sample.still_heater_power_w,
        # "still_heater_power_mw": sample.still_heater_power_w * 1000.0,
    }


def log_sample(csv_writer: csv.DictWriter, sample: Sample) -> None:
    csv_writer.writerow(sample_to_row(sample))
    print(
        f"[{sample.timestamp:%Y-%m-%d %H:%M:%S}] "
        f"step={sample.step_index + 1}, "
        f"target={sample.target_mc_setpoint_k * 1000.0:.3f} mK, "
        f"MC={sample.mc_temp_k * 1000.0:.3f} mK, "
        f"MC_SP={sample.mc_setpoint_k * 1000.0:.3f} mK, "
        f"MC_H={sample.mc_heater_power_w * 1e6:.3f} uW, "
        f"Still={sample.still_temp_k * 1000.0:.3f} mK, "
        f"Still_H={sample.still_heater_power_w * 1000.0:.3f} mW"
    )


def run_step(
    decs_visa,
    csv_writer: csv.DictWriter,
    step_index: int,
    mc_setpoint_k: float,
    sample_period_s: float,
    dwell_seconds: float,
    timeout_seconds: float,
    run_start: float,
) -> bool:
    response = set_mc_setpoint(decs_visa, mc_setpoint_k)
    print(
        f"\nStep {step_index + 1}: set MC setpoint to {mc_setpoint_k * 1000.0:.3f} mK "
        f"(response: {response})"
    )

    step_start = time.time()
    next_sample_time = step_start

    while True:
        now = time.time()
        step_elapsed_s = now - step_start
        run_elapsed_s = now - run_start

        if now >= next_sample_time:
            sample = read_status(
                decs_visa=decs_visa,
                step_index=step_index,
                step_elapsed_s=step_elapsed_s,
                run_elapsed_s=run_elapsed_s,
                target_mc_setpoint_k=mc_setpoint_k,
            )
            log_sample(csv_writer, sample)
            next_sample_time += sample_period_s

        if step_elapsed_s >= dwell_seconds:
            print(
                f"Step {step_index + 1} completed dwell after {step_elapsed_s / 60.0:.1f} minutes."
            )
            return True

        if step_elapsed_s >= timeout_seconds:
            print(
                f"Step {step_index + 1} hit timeout after {step_elapsed_s / 60.0:.1f} minutes."
            )
            return False

        sleep_seconds = max(0.2, min(sample_period_s, next_sample_time - time.time()))
        time.sleep(sleep_seconds)


def maybe_shutdown_server(decs_visa) -> None:
    try:
        decs_visa.write(SHUTDOWN)
    except Exception as exc:
        print(f"Could not send server shutdown: {exc}")


def main() -> int:
    args = parse_args()
    validate_args(args)

    output_csv = args.output_csv.resolve() if args.output_csv else default_output_path()
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    server_process = None
    rm = None
    decs_visa = None
    run_start = time.time()

    try:
        if args.start_server:
            print("Starting local DECS<->VISA server...")
            server_process = start_decs_visa_server(args.server_wait_seconds)

        rm, decs_visa = connect_decs_visa()
        print(f"Connected: {decs_visa.query('*IDN?').strip()}")
        print(f"Logging to: {output_csv}")
        print(
            f"Dwell per setpoint: {DEFAULT_DWELL_MINUTES:.0f} min; "
            f"timeout per setpoint: {DEFAULT_TIMEOUT_MINUTES:.0f} min"
        )

        still_power_w = args.still_power_mw / 1000.0
        still_response = set_still_heater_power(decs_visa, still_power_w)
        print(
            f"Set still heater to {args.still_power_mw:.3f} mW "
            f"(response: {still_response})"
        )

        fieldnames = [
            "timestamp",
            "elapsed_s",
            "step_index",
            "step_elapsed_s",
            "target_mc_setpoint_k",
            "target_mc_setpoint_mk",
            "mc_temp_k",
            "mc_temp_mk",
            "mc_setpoint_k",
            "mc_setpoint_mk",
            "mc_heater_power_w",
            "mc_heater_power_uw",
            "still_temp_k",
            "still_temp_mk",
            "still_heater_power_w",
            "still_heater_power_mw",
        ]

        with output_csv.open("w", newline="", encoding="ascii") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()

            for step_index, mc_setpoint_k in enumerate(args.mc_setpoints_k):
                step_completed = run_step(
                    decs_visa=decs_visa,
                    csv_writer=writer,
                    step_index=step_index,
                    mc_setpoint_k=mc_setpoint_k,
                    sample_period_s=args.sample_period,
                    dwell_seconds=DEFAULT_DWELL_MINUTES * 60.0,
                    timeout_seconds=DEFAULT_TIMEOUT_MINUTES * 60.0,
                    run_start=run_start,
                )
                handle.flush()
                if not step_completed:
                    print("Stopping the sequence because the current step hit timeout.")
                    break

    except KeyboardInterrupt:
        print("Interrupted by user.")
        return 130
    finally:
        if decs_visa is not None:
            try:
                print("Turning still heater off...")
                ph.still_heater_off(decs_visa)
            except Exception as exc:
                print(f"Could not turn still heater off: {exc}")

            if args.shutdown_server:
                maybe_shutdown_server(decs_visa)

            try:
                decs_visa.close()
            except Exception:
                pass

        if rm is not None:
            try:
                rm.close()
            except Exception:
                pass

        if server_process is not None:
            try:
                server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
