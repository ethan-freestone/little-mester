#!/usr/bin/env python3
"""Side-effect-free parsing and deadline-aware host metrics snapshot collection."""

from __future__ import annotations

import csv
import errno
import os
import importlib.util
import math
import pathlib
import selectors
import stat
import subprocess
import time


def _load_contract():
    path = pathlib.Path(__file__).with_name("parent-metrics.py")
    spec = importlib.util.spec_from_file_location("parent_metrics_contract", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("protected parent metrics contract is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = _load_contract()


class CollectorError(ValueError):
    """A source could not be parsed into the fixed metrics contract."""


NVIDIA_QUERY = ("--query-gpu=name,temperature.gpu,utilization.gpu,memory.total,"
                "memory.used,memory.free", "--format=csv,noheader,nounits")
NVIDIA_OUTPUT_LIMIT = 8192


def nvidia_smi_adapter(*, executable: str = "/usr/bin/nvidia-smi",
                       clock=time.monotonic_ns, popen=subprocess.Popen,
                       output_limit: int = NVIDIA_OUTPUT_LIMIT):
    """Return an opt-in bounded NVIDIA CSV adapter; no probe happens at creation.

    The absolute executable and fixed query prevent PATH/argument injection. The
    child is reaped on timeout or output overflow, and stdout is capped while read.
    """
    if (not isinstance(executable, str) or not os.path.isabs(executable)
            or "\0" in executable or type(output_limit) is not int
            or not 1 <= output_limit <= NVIDIA_OUTPUT_LIMIT):
        raise CollectorError("malformed")
    executable = os.path.realpath(executable)

    def collect(deadline_ns: int) -> str:
        if type(deadline_ns) is not int or deadline_ns <= clock():
            raise TimeoutError
        try:
            info = os.stat(executable, follow_symlinks=False)
        except FileNotFoundError as error:
            raise CollectorError("no_device") from error
        except OSError as error:
            raise CollectorError("unsupported") from error
        if not stat.S_ISREG(info.st_mode) or not os.access(executable, os.X_OK):
            raise CollectorError("unsupported")
        selector = selectors.DefaultSelector()
        try:
            process = popen([executable, *NVIDIA_QUERY], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            close_fds=True, env={"PATH": "/usr/bin:/bin", "LANG": "C"})
        except FileNotFoundError as error:
            selector.close()
            raise CollectorError("no_device") from error
        except OSError as error:
            selector.close()
            raise CollectorError("unsupported") from error

        output = bytearray()

        def stop_and_reap():
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    # Reap this exact Popen child after SIGKILL. The grace period
                    # is bounded; wait() is required to avoid leaving a zombie.
                    process.wait()
            else:
                process.wait()

        try:
            if process.stdout is None:
                raise CollectorError("collector_failed")
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = (deadline_ns - clock()) / 1_000_000_000
                if remaining <= 0:
                    raise TimeoutError
                events = selector.select(remaining)
                if not events:
                    raise TimeoutError
                for key, _ in events:
                    chunk = os.read(key.fd, min(4096, output_limit + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > output_limit:
                        raise CollectorError("malformed")
            remaining = (deadline_ns - clock()) / 1_000_000_000
            if remaining <= 0:
                raise TimeoutError
            try:
                return_code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as error:
                raise TimeoutError from error
            if return_code != 0:
                raise CollectorError("unsupported")
            if clock() >= deadline_ns:
                raise TimeoutError
            try:
                return output.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise CollectorError("malformed") from error
        except BaseException:
            try:
                stop_and_reap()
            except (OSError, subprocess.TimeoutExpired):
                # Preserve the collection failure. Popen retains responsibility
                # for later reaping if the kernel cannot complete termination now.
                pass
            raise
        finally:
            selector.close()
            if process.stdout is not None:
                process.stdout.close()

    return collect


def read_bounded_file(path: str | os.PathLike, maximum_bytes: int) -> str:
    """Read a bounded regular file without following a final-component symlink."""
    if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= 65536:
        raise CollectorError("malformed")
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except PermissionError:
        raise
    except OSError as error:
        code = "malformed" if error.errno == errno.ELOOP else "unsupported"
        raise CollectorError(code) from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CollectorError("malformed")
        chunks = bytearray()
        while len(chunks) <= maximum_bytes:
            chunk = os.read(fd, min(4096, maximum_bytes + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > maximum_bytes:
            raise CollectorError("malformed")
        try:
            return chunks.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise CollectorError("malformed") from error
    finally:
        os.close(fd)


def linux_proc_adapters(*, proc_root: str | os.PathLike = "/proc",
                        cpu_count=os.cpu_count, read_text=read_bounded_file,
                        clock=time.monotonic_ns, gpu_adapter=None,
                        temperatures_adapter=None) -> dict:
    """Build lazy Linux adapters for proc metrics plus optional injected devices.

    Calling this function performs no reads or device probes. CPU/RAM access is
    limited to three fixed proc files; GPU and thermal access stay explicit optional
    adapters so no vendor library or sysfs tree is probed implicitly.
    """
    root = pathlib.Path(proc_root)
    if not root.is_absolute() or "\n" in str(root) or "\0" in str(root):
        raise CollectorError("malformed")

    def check_deadline(deadline_ns):
        if clock() >= deadline_ns:
            raise TimeoutError

    def cpu(deadline_ns):
        check_deadline(deadline_ns)
        cpuinfo = read_text(root / "cpuinfo", 65536)
        check_deadline(deadline_ns)
        loadavg = read_text(root / "loadavg", 256)
        check_deadline(deadline_ns)
        count = cpu_count()
        check_deadline(deadline_ns)
        if type(count) is not int or count < 1:
            raise CollectorError("malformed")
        return cpuinfo, loadavg, count

    def memory(deadline_ns):
        check_deadline(deadline_ns)
        content = read_text(root / "meminfo", 65536)
        check_deadline(deadline_ns)
        return content

    return {"cpu": cpu, "memory": memory, "gpus": gpu_adapter,
            "temperatures": temperatures_adapter}


def parse_cpu(cpuinfo: str, loadavg: str, online_cpus: int) -> dict:
    if (not isinstance(cpuinfo, str) or len(cpuinfo.encode("utf-8", errors="replace")) > 65536
            or not isinstance(loadavg, str) or len(loadavg) > 256):
        raise CollectorError("malformed")
    model = "unknown"
    for line in cpuinfo.splitlines():
        if line.lower().startswith(("model name", "hardware")) and ":" in line:
            candidate = line.split(":", 1)[1].strip()
            if candidate:
                model = candidate[:128]
                break
    fields = loadavg.split()
    if len(fields) < 3:
        raise CollectorError("malformed")
    try:
        loads = [float(value) for value in fields[:3]]
    except ValueError as error:
        raise CollectorError("malformed") from error
    result = {"model": model, "load_avg_1m": loads[0], "load_avg_5m": loads[1],
              "load_avg_15m": loads[2], "num_cpus": online_cpus}
    if not contract._cpu(result):
        raise CollectorError("malformed")
    return result


def parse_memory(meminfo: str) -> dict:
    if not isinstance(meminfo, str) or len(meminfo) > 65536:
        raise CollectorError("malformed")
    values = {}
    try:
        for line in meminfo.splitlines():
            key, separator, remainder = line.partition(":")
            if separator and key in {"MemTotal", "MemAvailable"}:
                fields = remainder.split()
                if len(fields) >= 2 and fields[1] == "kB":
                    values[key] = int(fields[0])
    except ValueError as error:
        raise CollectorError("malformed") from error
    total_kb = values.get("MemTotal", 0)
    available_kb = values.get("MemAvailable", 0)
    if total_kb <= 0 or available_kb < 0 or available_kb > total_kb:
        raise CollectorError("malformed")
    used_kb = total_kb - available_kb
    divisor = 1024 * 1024
    result = {
        "total_gb": round(total_kb / divisor, 1),
        "used_gb": round(used_kb / divisor, 1),
        "available_gb": round(available_kb / divisor, 1),
        "percent": round(used_kb / total_kb * 100, 1),
    }
    if not contract._memory(result):
        raise CollectorError("malformed")
    return result


def parse_gpu_csv(output: str) -> list[dict]:
    """Parse the fixed nvidia-smi query columns; no command is run here."""
    if not isinstance(output, str) or len(output.encode("utf-8", errors="replace")) > 8192:
        raise CollectorError("malformed")
    rows = list(csv.reader(output.splitlines(), skipinitialspace=True))
    if not rows or len(rows) > 8:
        raise CollectorError("no_device" if not rows else "malformed")
    gpus = []
    try:
        for index, row in enumerate(rows):
            if len(row) != 6:
                raise CollectorError("malformed")
            name = row[0].strip()
            values = [int(float(value.strip())) for value in row[1:]]
            gpus.append({"index": index, "name": name, "temperature_c": values[0],
                         "utilization_percent": values[1], "memory_total_mb": values[2],
                         "memory_used_mb": values[3], "memory_free_mb": values[4]})
    except (ValueError, OverflowError) as error:
        raise CollectorError("malformed") from error
    if not contract._gpus(gpus):
        raise CollectorError("malformed")
    return gpus


def parse_temperatures(entries) -> list[dict]:
    """Normalize injected (chip, sensor, Celsius) entries from sysfs adapters."""
    if not isinstance(entries, (list, tuple)):
        raise CollectorError("malformed")
    if len(entries) > 64:
        raise CollectorError("malformed")
    result = []
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            raise CollectorError("malformed")
        chip, sensor, raw_celsius = entry
        if not isinstance(chip, str) or not isinstance(sensor, str):
            raise CollectorError("malformed")
        try:
            temperature = round(float(raw_celsius), 1)
        except (TypeError, ValueError, OverflowError) as error:
            raise CollectorError("malformed") from error
        result.append({"chip": chip[:64], "sensor": sensor[:64],
                       "temperature_c": temperature})
    if not contract._temperatures(result):
        raise CollectorError("malformed")
    return result


def _category(adapter, parser, *, deadline_ns: int, clock, unavailable_code: str):
    def timed_out():
        return clock() >= deadline_ns

    if timed_out():
        return contract.category("unavailable", None, "timeout")
    if adapter is None:
        return contract.category("unavailable", None, unavailable_code)
    try:
        raw = adapter(deadline_ns)
        if timed_out():
            return contract.category("unavailable", None, "timeout")
        parsed = parser(raw)
        if timed_out():
            return contract.category("unavailable", None, "timeout")
        return contract.category("ok", parsed)
    except TimeoutError:
        return contract.category("unavailable", None, "timeout")
    except PermissionError:
        return contract.category("unavailable", None,
                                 "timeout" if timed_out() else "permission_denied")
    except CollectorError as error:
        return contract.category("unavailable", None,
                                 "timeout" if timed_out() else str(error))
    except OSError:
        return contract.category("unavailable", None,
                                 "timeout" if timed_out() else "unsupported")
    except Exception:
        # Do not serialize exception text or host paths into the fixed frame.
        if timed_out():
            return contract.category("unavailable", None, "timeout")
        return contract.category("error", None, "collector_failed")


def collect_snapshot(*, run_id: str, sequence: int, adapters: dict,
                     budget_seconds: float = 0.4, clock=time.monotonic_ns,
                     wall_clock=None) -> dict:
    """Collect a bounded-schema sample through injected deadline-aware adapters.

    Each adapter is called synchronously with an absolute monotonic-nanosecond
    deadline and must itself honor it. After each call the collector checks for
    overrun and discards late data. This cannot preempt a non-cooperating adapter;
    production process/hardware adapters must enforce their own bounded call.
    """
    if (not isinstance(adapters, dict)
            or set(adapters) != {"cpu", "memory", "gpus", "temperatures"}
            or type(budget_seconds) not in (int, float)
            or not math.isfinite(budget_seconds) or not 0 < budget_seconds <= 0.5):
        raise contract.Refused("metrics collector requires fixed adapters and a budget up to 500ms")
    started = clock()
    if type(started) is not int or started < 0:
        raise contract.Refused("metrics collector clock returned an invalid value")
    deadline = started + int(budget_seconds * 1_000_000_000)
    parsers = {"cpu": lambda value: parse_cpu(*value), "memory": parse_memory,
               "gpus": parse_gpu_csv, "temperatures": parse_temperatures}
    fallbacks = {"cpu": "unsupported", "memory": "unsupported",
                 "gpus": "no_device", "temperatures": "no_sensors"}
    categories = {}
    for name in ("cpu", "memory", "gpus", "temperatures"):
        categories[name] = _category(adapters[name], parsers[name], deadline_ns=deadline,
                                     clock=clock, unavailable_code=fallbacks[name])
    finished = clock()
    if type(finished) is not int or finished < started:
        raise contract.Refused("metrics collector clock moved backwards or returned an invalid value")
    duration_ms = (finished - started) // 1_000_000
    if duration_ms > 5000:
        raise contract.Refused("metrics sampling duration exceeded the fixed schema maximum")
    wall_time = wall_clock() if wall_clock is not None else None
    snapshot = {
        "version": 1, "run_id": run_id, "sequence": sequence,
        "sampled_at": contract.utc_sample_time(wall_time),
        "sampling_duration_ms": duration_ms,
        "cpu": categories["cpu"], "memory": categories["memory"],
        "gpus": categories["gpus"], "temperatures": categories["temperatures"],
    }
    contract.validate_snapshot(snapshot)
    return snapshot
