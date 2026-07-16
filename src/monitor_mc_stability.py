"""
Step the still heater through user-defined power levels while monitoring
mixing-chamber temperature stability through the DECS<->VISA socket server.

Example:
    python monitor_mc_stability.py --powers-mw 1 2 3 4 --sample-period 60

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
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev

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

decs_visa_path = './decs_visa.py'

@dataclass
class Sample:
    timestamp: datetime
    elapsed_s: float
    step_index: int
    still_power_w: float
    mc_temp_k: float
    still_temp_k: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Monitor MC temperature stability while stepping up still heater power."
        )
    )
    parser.add_argument(
        "--powers-mw",
        nargs="+",
        type=float,
        required=True,
        help="Still-heater power steps in mW after the initial 0 mW step, for example: --powers-mw 1 2 3 4",
    )
    parser.add_argument(
        "--mc-setpoint-k",
        type=float,
        default=6e-3, #setpoint 6mK
        help="Optional MC setpoint in K to apply before starting the still-heater sweep.",
    )
    parser.add_argument(
        "--sample-period",
        type=float,
        default=75.0,
        help="Seconds between samples. Default: 75",
    )
    parser.add_argument(
        "--window-minutes",
        type=float,
        default=15.0,
        help="Rolling window length used for stability checks. Default: 10",
    )
    parser.add_argument(
        "--max-step-minutes",
        type=float,
        default=30.0,
        help="Maximum dwell time at each heater step. Default: 30",
    )
    parser.add_argument(
        "--stability-percent",
        type=float,
        default=0.5,
        help="Allowed MC deviation band around the setpoint in percent. Default: 0.5",
    )
    parser.add_argument(
        "--stability-slope-mk-per-min",
        type=float,
        default=0.02,
        help="Absolute slope threshold in mK/min. Default: 0.02",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=None,
        help="Optional CSV output path. Default: src/output/<timestamp>_mc_stability.csv",
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
    if args.window_minutes <= 0:
        raise ValueError("--window-minutes must be positive.")
    if args.max_step_minutes <= 0:
        raise ValueError("--max-step-minutes must be positive.")
    if args.stability_percent <= 0:
        raise ValueError("--stability-percent must be positive.")
    if args.stability_slope_mk_per_min <= 0:
        raise ValueError("--stability-slope-mk-per-min must be positive.")
    if any(power < 0 for power in args.powers_mw):
        raise ValueError("Still-heater power steps must be non-negative.")
    if args.mc_setpoint_k is not None and args.mc_setpoint_k < 0:
        raise ValueError("--mc-setpoint-k must be non-negative.")


def default_output_path() -> Path:
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_OUTPUT_DIR / f"{stamp}_mc_stability.csv"


def start_decs_visa_server(wait_seconds: float) -> subprocess.Popen[bytes]:
    decs_visa_path = SCRIPT_DIR / "decs_visa.py"
    command = [sys.executable, str(decs_visa_path)]
    kwargs = {"cwd": str(SCRIPT_DIR)}
    if platform.system().lower().startswith("win"):
        process = subprocess.Popen(command, **kwargs)
    else:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
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


def read_temperatures(decs_visa) -> tuple[float, float]:
    mc_temp_k = float(decs_visa.query("get_MC_T").strip())
    still_temp_k = float(decs_visa.query("get_STILL_T").strip())
    return mc_temp_k, still_temp_k


def set_mc_setpoint(decs_visa, setpoint_k: float) -> str:
    return decs_visa.query(f"set_MC_T:{setpoint_k}").strip()


def linear_slope_mk_per_min(window: deque[Sample]) -> float:
    if len(window) < 2:
        return float("nan")

    x_vals = [sample.elapsed_s / 60.0 for sample in window]
    y_vals = [sample.mc_temp_k * 1000.0 for sample in window]
    x_mean = mean(x_vals)
    y_mean = mean(y_vals)

    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, y_vals))
    denominator = sum((x - x_mean) ** 2 for x in x_vals)
    if denominator == 0:
        return 0.0
    return numerator / denominator


def window_metrics(window: deque[Sample]) -> dict[str, float | bool]:
    mc_temps_mk = [sample.mc_temp_k * 1000.0 for sample in window]
    ptp_mk = max(mc_temps_mk) - min(mc_temps_mk)
    slope_mk_per_min = linear_slope_mk_per_min(window)
    std_mk = pstdev(mc_temps_mk) if len(mc_temps_mk) > 1 else 0.0
    return {
        "ptp_mk": ptp_mk,
        "slope_mk_per_min": slope_mk_per_min,
        "std_mk": std_mk,
    }


def setpoint_band_metrics(
    window: deque[Sample],
    mc_setpoint_k: float,
    stability_percent: float,
) -> dict[str, float]:
    mc_temps_mk = [sample.mc_temp_k * 1000.0 for sample in window]
    setpoint_mk = mc_setpoint_k * 1000.0
    band_mk = abs(setpoint_mk) * (stability_percent / 100.0)
    max_abs_error_mk = max(abs(temp_mk - setpoint_mk) for temp_mk in mc_temps_mk)
    return {
        "setpoint_mk": setpoint_mk,
        "band_mk": band_mk,
        "max_abs_error_mk": max_abs_error_mk,
    }


def is_stable(
    metrics: dict[str, float | bool],
    mc_setpoint_k: float | None,
    stability_percent: float,
    slope_limit_mk_per_min: float,
) -> bool:
    if mc_setpoint_k is not None:
        return (
            metrics["max_abs_error_mk"] <= metrics["band_mk"]
            and abs(metrics["slope_mk_per_min"]) <= slope_limit_mk_per_min
        )
    return (
        metrics["ptp_mk"] <= stability_percent
        and abs(metrics["slope_mk_per_min"]) <= slope_limit_mk_per_min
    )


def write_csv_header(csv_writer: csv.DictWriter) -> None:
    csv_writer.writeheader()


def sample_to_row(sample: Sample, metrics: dict[str, float], stable: bool) -> dict[str, object]:
    return {
        "timestamp": sample.timestamp.isoformat(),
        "elapsed_s": round(sample.elapsed_s, 1),
        "step_index": sample.step_index,
        "still_power_w": sample.still_power_w,
        "still_power_mw": sample.still_power_w * 1000.0,
        "mc_temp_k": sample.mc_temp_k,
        "mc_temp_mk": sample.mc_temp_k * 1000.0,
        "still_temp_k": sample.still_temp_k,
        "still_temp_mk": sample.still_temp_k * 1000.0,
        "mc_setpoint_mk": metrics.get("setpoint_mk"),
        "setpoint_band_mk": metrics.get("band_mk"),
        "window_max_abs_error_mk": metrics.get("max_abs_error_mk"),
        "window_ptp_mk": metrics["ptp_mk"],
        "window_std_mk": metrics["std_mk"],
        "window_slope_mk_per_min": metrics["slope_mk_per_min"],
        "stable": stable,
    }


def monitor_step(
    decs_visa,
    csv_writer: csv.DictWriter,
    step_index: int,
    still_power_w: float,
    sample_period_s: float,
    window_seconds: float,
    max_step_seconds: float,
    mc_setpoint_k: float | None,
    stability_percent: float,
    slope_limit_mk_per_min: float,
    run_start: float,
) -> bool:
    ph.set_still_heater_power(decs_visa, still_power_w)
    print(
        f"\nStep {step_index + 1}: set still heater to {still_power_w * 1000.0:.3f} mW"
    )

    window: deque[Sample] = deque()
    step_start = time.time()
    min_samples_for_window = max(2, int(window_seconds / sample_period_s))

    while True:
        now = time.time()
        elapsed_s = now - run_start
        step_elapsed_s = now - step_start
        mc_temp_k, still_temp_k = read_temperatures(decs_visa)

        sample = Sample(
            timestamp=datetime.now(),
            elapsed_s=elapsed_s,
            step_index=step_index,
            still_power_w=still_power_w,
            mc_temp_k=mc_temp_k,
            still_temp_k=still_temp_k,
        )
        window.append(sample)

        while window and (sample.elapsed_s - window[0].elapsed_s) > window_seconds:
            window.popleft()

        metrics = {
            "setpoint_mk": float("nan"),
            "band_mk": float("nan"),
            "max_abs_error_mk": float("nan"),
            "ptp_mk": float("nan"),
            "std_mk": float("nan"),
            "slope_mk_per_min": float("nan"),
        }
        stable = False
        if len(window) >= min_samples_for_window:
            metrics = window_metrics(window)
            if mc_setpoint_k is not None:
                metrics.update(
                    setpoint_band_metrics(window, mc_setpoint_k, stability_percent)
                )
            stable = is_stable(
                metrics,
                mc_setpoint_k,
                stability_percent,
                slope_limit_mk_per_min,
            )

        csv_writer.writerow(sample_to_row(sample, metrics, stable))

        ptp_text = f"{metrics['ptp_mk']:.3f}" if metrics["ptp_mk"] == metrics["ptp_mk"] else "n/a"
        slope_text = (
            f"{metrics['slope_mk_per_min']:.4f}"
            if metrics["slope_mk_per_min"] == metrics["slope_mk_per_min"]
            else "n/a"
        )
        error_text = (
            f"{metrics['max_abs_error_mk']:.3f}/{metrics['band_mk']:.3f}"
            if metrics["max_abs_error_mk"] == metrics["max_abs_error_mk"]
            else "n/a"
        )
        print(
            f"[{sample.timestamp:%Y-%m-%d %H:%M:%S}] "
            f"MC={sample.mc_temp_k * 1000.0:.3f} mK, "
            f"Still={sample.still_temp_k * 1000.0:.3f} mK, "
            f"setpoint_error={error_text} mK, "
            f"window_ptp={ptp_text} mK, "
            f"window_slope={slope_text} mK/min, "
            f"stable={stable}"
        )

        if stable:
            print(
                f"Step {step_index + 1} reached stability after {step_elapsed_s / 60.0:.1f} minutes."
            )
            return True

        if step_elapsed_s >= max_step_seconds:
            print(
                f"Step {step_index + 1} timed out after {step_elapsed_s / 60.0:.1f} minutes."
            )
            return False

        time.sleep(sample_period_s)


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

        fieldnames = [
            "timestamp",
            "elapsed_s",
            "step_index",
            "still_power_w",
            "still_power_mw",
            "mc_temp_k",
            "mc_temp_mk",
            "still_temp_k",
            "still_temp_mk",
            "mc_setpoint_mk",
            "setpoint_band_mk",
            "window_max_abs_error_mk",
            "window_ptp_mk",
            "window_std_mk",
            "window_slope_mk_per_min",
            "stable",
        ]

        with output_csv.open("w", newline="", encoding="ascii") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            write_csv_header(writer)

            if args.mc_setpoint_k is not None:
                response = set_mc_setpoint(decs_visa, args.mc_setpoint_k)
                print(
                    f"Set MC setpoint to {args.mc_setpoint_k * 1000.0:.3f} mK "
                    f"(response: {response})"
                )

            power_steps_mw = [0.0, *args.powers_mw]

            for step_index, power_mw in enumerate(power_steps_mw):
                step_stable = monitor_step(
                    decs_visa=decs_visa,
                    csv_writer=writer,
                    step_index=step_index,
                    still_power_w=power_mw / 1000.0,
                    sample_period_s=args.sample_period,
                    window_seconds=args.window_minutes * 60.0,
                    max_step_seconds=args.max_step_minutes * 60.0,
                    mc_setpoint_k=args.mc_setpoint_k,
                    stability_percent=args.stability_percent,
                    slope_limit_mk_per_min=args.stability_slope_mk_per_min,
                    run_start=run_start,
                )
                handle.flush()
                if not step_stable:
                    print("Stopping the sequence because the current step did not stabilize.")
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
