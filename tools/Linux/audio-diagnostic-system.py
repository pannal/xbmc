#!/usr/bin/env python3
"""Bounded numeric host/kernel observations for the audio diagnostic collector.

This module performs no playback/service/boot changes. KernelCapture writes only
the dedicated, owner-scoped audio diagnostic control. The kernel deadline rejects
observations after expiry even if this collector disappears. Missing observations
never stand for healthy zero.
"""

import gzip
import io
import os
from pathlib import Path
import re
import secrets
import stat
import time


MAX_PROC_ENTRIES = 512
MAX_TASK_ENTRIES = 128
MAX_THREADS = 12
MAX_CPUS = 64
MAX_IRQS = 32
MAX_U64 = (1 << 64) - 1
CONTROL = "sys/class/amhdmitx/amhdmitx0/audio_diagnostic"
EVENTS = "sys/class/amhdmitx/amhdmitx0/audio_diagnostic_events"
HARDWARE = "sys/class/amhdmitx/amhdmitx0/audio_diagnostic_hw"
ROLES = {"PAPlayer": "player", "ActiveAE": "engine", "AESink": "sink"}
SOFTIRQS = {"HI", "TIMER", "NET_TX", "NET_RX", "BLOCK", "IRQ_POLL",
            "TASKLET", "SCHED", "HRTIMER", "RCU"}
CONFIG_KEYS = {"CONFIG_SCHEDSTATS", "CONFIG_SCHED_INFO", "CONFIG_TASK_DELAY_ACCT",
               "CONFIG_IRQ_TIME_ACCOUNTING"}


def unavailable(reason, error=None):
    result = {"available": False, "reason": reason}
    if error is not None:
        result["errno"] = error
    return result


def read_bounded(path, limit=4096):
    """Bound each read; never include path or arbitrary exception text in output."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb", buffering=0) as source:
            data = source.read(limit + 1)
        if data is None:
            return unavailable("would_block")
        if len(data) > limit:
            return unavailable("size_limit")
        return {"available": True, "data": data}
    except OSError as error:
        return unavailable("read_error", error.errno)


def unsigned(value):
    if not re.fullmatch(r"[0-9]{1,20}", value):
        raise ValueError("invalid_unsigned")
    result = int(value)
    if result > MAX_U64:
        raise ValueError("unsigned_overflow")
    return result


def _text(result):
    if not result.get("available"):
        return None
    try:
        return result["data"].decode("ascii")
    except UnicodeDecodeError:
        return None


def _numeric_dirs(path, limit):
    result = []
    scanned = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                scanned += 1
                if scanned > limit:
                    return result, {"available": False, "reason": "enumeration_limit",
                                    "scanned": limit, "truncated": True}
                if re.fullmatch(r"[0-9]{1,10}", entry.name):
                    result.append(int(entry.name))
        return sorted(result), {"available": True, "scanned": scanned, "truncated": False}
    except OSError as error:
        return [], unavailable("enumeration_error", error.errno)


def parse_task_stat(data):
    # comm may contain parentheses/whitespace; export none of it.
    text = _text(data)
    if text is None:
        return unavailable(data.get("reason", "invalid_stat"), data.get("errno"))
    close = text.rfind(")")
    if close < 0 or not re.match(r"^[0-9]{1,10} \(", text):
        return unavailable("invalid_stat")
    fields = text[close + 1:].split()
    if len(fields) < 40 or fields[0] not in "RSDZTtXxKWPI" or len(fields[0]) != 1:
        return unavailable("invalid_stat")
    try:
        result = {"available": True, "state": fields[0], "tid": unsigned(text.split(" ", 1)[0])}
        for key, index in (("user_ticks", 11), ("system_ticks", 12), ("start_ticks", 19),
                           ("processor", 36), ("rt_priority", 37), ("policy", 38),
                           ("block_io_ticks", 39)):
            result[key] = unsigned(fields[index])
        for key, index in (("priority", 15), ("nice", 16)):
            if not re.fullmatch(r"-?[0-9]{1,10}", fields[index]):
                raise ValueError("invalid_priority")
            result[key] = int(fields[index])
        return result
    except ValueError:
        return unavailable("invalid_stat")


def _boot_id(root):
    value = read_bounded(root / "proc/sys/kernel/random/boot_id", 64)
    text = _text(value)
    if text is None or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\n?", text):
        return unavailable(value.get("reason", "invalid_boot_id"), value.get("errno"))
    return {"available": True, "value": text.strip().lower()}


def identity(root=Path("/")):
    """Return boot identity and a unique Kodi process lifetime, with bounded search."""
    root = Path(root)
    pids, enumeration = _numeric_dirs(root / "proc", MAX_PROC_ENTRIES)
    found = []
    for pid in pids:
        base = root / "proc" / str(pid)
        name = _text(read_bounded(base / "comm", 64))
        if name not in ("kodi\n", "kodi.bin\n", "kodi", "kodi.bin"):
            continue
        value = parse_task_stat(read_bounded(base / "stat"))
        if value.get("available") and value["tid"] == pid:
            found.append({"pid": pid, "start_ticks": value["start_ticks"]})
        if len(found) >= 2:
            break
    if not enumeration.get("available"):
        kodi = unavailable("process_enumeration_incomplete")
    elif len(found) == 1:
        kodi = dict(found[0], available=True)
    else:
        kodi = unavailable("multiple_kodi_processes" if found else "kodi_not_found")
    return {"boot_id": _boot_id(root), "kodi": kodi, "process_enumeration": enumeration}


def _config(root):
    data = read_bounded(root / "proc/config.gz", 64 * 1024)
    if not data.get("available"):
        return {"available": False, "reason": data["reason"],
                **({"errno": data["errno"]} if "errno" in data else {})}
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data["data"])) as source:
            decoded = source.read(256 * 1024 + 1)
        if len(decoded) > 256 * 1024:
            return unavailable("decompressed_size_limit")
        text = decoded.decode("ascii")
    except (OSError, EOFError, UnicodeError):
        return unavailable("invalid_compressed_config")
    flags = {}
    for line in text.splitlines():
        enabled = re.fullmatch(r"(CONFIG_[A-Z0-9_]+)=([yn])", line)
        disabled = re.fullmatch(r"# (CONFIG_[A-Z0-9_]+) is not set", line)
        if enabled and enabled[1] in CONFIG_KEYS:
            flags[enabled[1]] = enabled[2] == "y"
        elif disabled and disabled[1] in CONFIG_KEYS:
            flags[disabled[1]] = False
    return {"available": True, "flags": flags}


def _runtime_schedstats(root):
    value = read_bounded(root / "proc/sys/kernel/sched_schedstats", 8)
    text = _text(value)
    if text is None or text.strip() not in ("0", "1"):
        return unavailable(value.get("reason", "invalid_runtime_toggle"), value.get("errno"))
    return {"available": True, "enabled": text.strip() == "1"}


def _status(data):
    text = _text(data)
    if text is None:
        return unavailable(data.get("reason", "invalid_status"), data.get("errno"))
    result = {"available": True}
    for line in text.splitlines():
        match = re.fullmatch(r"(voluntary_ctxt_switches|nonvoluntary_ctxt_switches):\s*([0-9]{1,20})", line)
        if match:
            try:
                result[match[1]] = unsigned(match[2])
            except ValueError:
                return unavailable("invalid_context_count")
        match = re.fullmatch(r"Cpus_allowed_list:\s*([0-9,-]{1,256})", line)
        if match:
            cpus = set()
            for item in match[1].split(","):
                numbers = item.split("-")
                if len(numbers) > 2 or any(not n.isdigit() or len(n) > 5 for n in numbers):
                    return unavailable("invalid_affinity")
                first, last = int(numbers[0]), int(numbers[-1])
                if last < first or last >= 4096 or last - first >= MAX_CPUS:
                    return unavailable("affinity_limit")
                cpus.update(range(first, last + 1))
                if len(cpus) > MAX_CPUS:
                    return unavailable("affinity_limit")
            result["allowed_cpus"] = sorted(cpus)
    if len(result) == 1:
        return unavailable("no_selected_status_fields")
    return result


def _schedstat(data):
    text = _text(data)
    if text is None:
        return unavailable(data.get("reason", "invalid_schedstat"), data.get("errno"))
    fields = text.split()
    try:
        if len(fields) != 3:
            raise ValueError("invalid_schedstat")
        values = [unsigned(item) for item in fields]
        return dict(zip(("exec_ns", "runqueue_wait_ns", "timeslices"), values), available=True)
    except ValueError:
        return unavailable("invalid_schedstat")


def _cpu(data):
    text = _text(data)
    if text is None:
        return unavailable(data.get("reason", "invalid_cpu_stat"), data.get("errno"))
    result = {"available": True, "cpus": {}, "truncated": False}
    try:
        for line in text.splitlines():
            fields = line.split()
            if not fields:
                continue
            if re.fullmatch(r"cpu(?:[0-9]{1,4})?", fields[0]):
                if len(result["cpus"]) >= MAX_CPUS + 1:
                    result["truncated"] = True
                    continue
                if len(fields) < 9:
                    raise ValueError("short_cpu_row")
                result["cpus"][fields[0]] = [unsigned(value) for value in fields[1:9]]
            elif fields[0] in ("ctxt", "procs_running", "procs_blocked"):
                if len(fields) != 2:
                    raise ValueError("invalid_cpu_scalar")
                result[fields[0]] = unsigned(fields[1])
        if not result["cpus"]:
            raise ValueError("no_cpu_rows")
        return result
    except ValueError:
        return unavailable("invalid_cpu_stat")


def _irq_category(label):
    lower = label.lower()
    if re.search(r"\bvsync(?:_viu2)?\b|\bviu[0-9]*\b", lower):
        return "display_vsync"
    if re.search(r"\bhdmitx\b|\bhdmi\b", lower):
        return "hdmi"
    if re.search(r"\bfrddr_[abc]\b|\btoddr_[abc]\b|\btdm[abc]?\b|\baudio\b|[.]audio(?:[-_.]|$)|\bddr_manager\b", lower):
        return "audio_dma"
    return None


def _interrupts(data, soft=False):
    text = _text(data)
    if text is None:
        return unavailable(data.get("reason", "invalid_interrupts"), data.get("errno"))
    lines = text.splitlines()
    if not lines:
        return unavailable("invalid_interrupts")
    names = lines[0].split()
    if not names or len(names) > MAX_CPUS or any(not re.fullmatch(r"CPU[0-9]{1,4}", item) for item in names):
        return unavailable("invalid_cpu_header")
    result = {"available": True, "cpus": [int(name[3:]) for name in names], "rows": [], "truncated": False}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        key = key.strip()
        fields = rest.split()
        if soft:
            if key not in SOFTIRQS:
                continue
            category = key
        else:
            if not re.fullmatch(r"[0-9]{1,6}", key):
                continue
            category = _irq_category(" ".join(fields[len(names):]))
            if category is None:
                continue
        if len(fields) < len(names):
            return unavailable("short_interrupt_row")
        try:
            counts = [unsigned(value) for value in fields[:len(names)]]
        except ValueError:
            return unavailable("invalid_interrupt_counts")
        if len(result["rows"]) == MAX_IRQS:
            result["truncated"] = True
            break
        row = {"category": category, "counts": counts}
        if not soft:
            row["irq"] = int(key)
        result["rows"].append(row)
    if not result["rows"]:
        return unavailable("no_selected_interrupt_rows")
    return result


class SystemSampler:
    """One bounded synchronous sample; caller supplies the one-second cadence."""
    def __init__(self, root=Path("/")):
        self.root = Path(root)
        self.config = None
        self.boot = None
        self.previous = {}
        self.previous_at = None
        self.system_previous = {}

    def _system_deltas(self, result, same_boot):
        current = {}
        for name in ("cpu", "interrupts", "softirqs"):
            value = result[name]
            if not value.get("available"):
                continue
            entries = value.get("cpus", {}) if name == "cpu" else {
                (row.get("irq"), row["category"], tuple(value["cpus"])): row["counts"]
                for row in value["rows"]}
            deltas = []
            for key, counters in entries.items():
                unique = (name, key)
                current[unique] = counters
                prior = self.system_previous.get(unique) if same_boot else None
                if prior is None:
                    delta = unavailable("new_counter_baseline")
                elif len(prior) != len(counters) or any(now < old for now, old in zip(counters, prior)):
                    delta = unavailable("counter_reset")
                else:
                    delta = {"available": True, "counts": [now - old for now, old in zip(counters, prior)]}
                    if name == "cpu":
                        total = sum(delta["counts"])
                        delta["busy_per_mille"] = ((total - delta["counts"][3] - delta["counts"][4]) * 1000 // total) if total else None
                        delta["load_semantics"] = "first_eight_jiffies_excludes_idle_iowait_no_guest_doublecount"
                deltas.append({"id": key if name == "cpu" else [key[0], key[1]], "delta": delta})
            value["deltas"] = deltas
        self.system_previous = current

    def sample(self):
        start = time.monotonic_ns()
        ident = identity(self.root)
        boot = ident["boot_id"].get("value")
        boot_change = self.boot is not None and boot != self.boot
        same_boot = boot is not None and boot == self.boot
        if self.config is None or boot_change:
            self.config = _config(self.root)
        if boot_change or boot is None:
            self.previous = {}
        self.boot = boot
        runtime = _runtime_schedstats(self.root)
        result = {"mono_ns": start, "identity": ident, "kernel_config": self.config,
                  "runtime_schedstats": runtime,
                  "schedstat_semantics": "vendor_4_9_sched_info_independent_of_extended_runtime_toggle",
                  "extended_schedstats_available": bool(runtime.get("enabled")),
                  "cpu_irq_time_available": self.config.get("flags", {}).get("CONFIG_IRQ_TIME_ACCOUNTING") is True,
                  "cpu_tick_hz": os.sysconf("SC_CLK_TCK"), "cpu": _cpu(read_bounded(self.root / "proc/stat", 64 * 1024)),
                  "interrupts": _interrupts(read_bounded(self.root / "proc/interrupts", 128 * 1024)),
                  "softirqs": _interrupts(read_bounded(self.root / "proc/softirqs", 32 * 1024), True),
                  "threads": [], "thread_limit_reached": False}
        result["interval_ns"] = start - self.previous_at if same_boot and self.previous_at is not None else None
        self._system_deltas(result, same_boot)
        current = {}
        kodi = ident["kodi"]
        if kodi.get("available"):
            pid = kodi["pid"]
            base = self.root / "proc" / str(pid) / "task"
            tids, enumeration = _numeric_dirs(base, MAX_TASK_ENTRIES)
            result["task_enumeration"] = enumeration
            # Do not present a partial enumeration as a complete set of roles.
            for tid in tids:
                task = base / str(tid)
                name = _text(read_bounded(task / "comm", 64))
                role = "main" if tid == pid else ROLES.get(name.rstrip("\n") if name is not None else "")
                if role is None:
                    continue
                if len(result["threads"]) >= MAX_THREADS:
                    result["thread_limit_reached"] = True
                    break
                stat_data = parse_task_stat(read_bounded(task / "stat"))
                status = _status(read_bounded(task / "status", 8192))
                sched = _schedstat(read_bounded(task / "schedstat", 128))
                thread = {"role": role, "tid": tid, "stat": stat_data, "status": status, "schedstat": sched}
                config_enabled = self.config.get("flags", {}).get("CONFIG_SCHEDSTATS") is True
                sched["runqueue_observation_available"] = bool(sched.get("available") and config_enabled)
                sched["enablement_basis"] = "CONFIG_SCHEDSTATS" if config_enabled else "unverified_matching_kernel_configuration"
                if (not stat_data.get("available") or stat_data.get("tid") != tid or
                        (tid == pid and stat_data.get("start_ticks") != kodi["start_ticks"])):
                    thread["delta"] = unavailable("thread_lifetime_unavailable")
                else:
                    key = (boot, pid, kodi["start_ticks"], tid, stat_data["start_ticks"])
                    counters = {"user_ticks": stat_data["user_ticks"], "system_ticks": stat_data["system_ticks"],
                                "block_io_ticks": stat_data["block_io_ticks"]}
                    counters.update({name: status[name] for name in ("voluntary_ctxt_switches", "nonvoluntary_ctxt_switches") if name in status})
                    if sched.get("available") and sched["runqueue_observation_available"]:
                        counters.update({name: sched[name] for name in ("exec_ns", "runqueue_wait_ns", "timeslices")})
                    previous = self.previous.get(key)
                    if boot is None:
                        thread["delta"] = unavailable("boot_identity_unavailable")
                    elif previous is None:
                        thread["delta"] = unavailable("new_thread_lifetime")
                    elif set(previous) != set(counters) or any(value < previous[name] for name, value in counters.items()):
                        thread["delta"] = unavailable("counter_reset_or_sources_changed")
                    else:
                        thread["delta"] = {"available": True, "interval_ns": start - self.previous_at,
                                           **{name: value - previous[name] for name, value in counters.items()}}
                    current[key] = counters
                result["threads"].append(thread)
        else:
            result["task_enumeration"] = unavailable("kodi_identity_unavailable")
        observed = {item["role"] for item in result["threads"]}
        result["unobserved_roles"] = sorted({"main", "player", "engine", "sink"} - observed)
        result["unobserved_role_semantics"] = "not_running_or_unavailable_not_healthy"
        self.previous = current
        self.previous_at = start
        result["sample_end_ns"] = time.monotonic_ns()
        result["sample_duration_ns"] = result["sample_end_ns"] - start
        return result


def parse_core(data):
    text = _text(data)
    if text is None:
        return unavailable(data.get("reason", "invalid_core_snapshot"), data.get("errno"))
    lines = text.splitlines()
    if not lines or len(lines) > 40:
        return unavailable("core_line_limit_or_empty")
    keys = ("now", "active", "owner", "start", "deadline", "lost", "seq", "overwritten")
    header = lines[0].split()
    if not header or header[0] != "AAD1" or len(header) != len(keys) + 1:
        return unavailable("invalid_core_header")
    try:
        result = {"available": True, "schema": "AAD1", "rows": []}
        for token, key in zip(header[1:], keys):
            if not token.startswith(key + "="):
                raise ValueError("header_field")
            result[key] = unsigned(token[len(key) + 1:])
        if result["active"] not in (0, 1):
            raise ValueError("invalid_active")
        if not re.fullmatch(r"END truncated=[01](?: snapshot_bytes=[0-9]{1,6})?", lines[-1]):
            raise ValueError("invalid_footer")
        result["truncated"] = lines[-1].startswith("END truncated=1")
        if "snapshot_bytes=" in lines[-1]:
            result["snapshot_bytes"] = unsigned(lines[-1].split("snapshot_bytes=")[1])
        sizes = {"R": 7, "S": 13, "E": 7}
        for line in lines[1:-1]:
            fields = line.split()
            if not fields or fields[0] not in sizes or len(fields) != sizes[fields[0]] + 1:
                raise ValueError("invalid_row")
            result["rows"].append({"kind": fields[0], "values": [unsigned(value) for value in fields[1:]]})
        result["raw_counter_semantics"] = "repeated_sticky_bits_are_not_new_faults"
        return result
    except ValueError:
        return unavailable("invalid_core_snapshot")


# Exact approved names will remain compatible with the paired kernel ABI.
HARDWARE_KEYS = {"soc", "ready_after", "n", "cts_raw", "cts3_raw", "stable",
                 "fifo_latch_raw", "fifo_mask_raw", "packet", "gcp", "clkdis",
                 "acr_auto", "inputclkfs", "pll0", "pll1"}


def parse_hardware(data):
    text = _text(data)
    if text is None:
        return unavailable(data.get("reason", "invalid_hardware_snapshot"), data.get("errno"))
    tokens = text.split()
    if not tokens or tokens[0] == "UNAVAILABLE":
        return unavailable("kernel_hardware_observation_unavailable")
    if tokens[0] != "HW1" or len(tokens) > 50:
        return unavailable("invalid_hardware_snapshot")
    result = {"available": True, "schema": "HW1",
              "semantics": "raw_read_only_no_ack_no_physical_cts_or_fifo_health_claim"}
    try:
        for token in tokens[1:]:
            fields = token.split("=")
            if len(fields) != 2 or fields[0] not in HARDWARE_KEYS or fields[0] in result:
                raise ValueError("invalid_hardware_field")
            if fields[0] == "cts_raw":
                numbers = fields[1].split(",")
                if len(numbers) != 4:
                    raise ValueError("invalid_raw_cts")
                result[fields[0]] = [unsigned(item) for item in numbers]
            else:
                result[fields[0]] = unsigned(fields[1])
        if set(result) - {"available", "schema", "semantics"} != HARDWARE_KEYS:
            raise ValueError("empty_hardware_snapshot")
        return result
    except ValueError:
        return unavailable("invalid_hardware_snapshot")


class KernelCapture:
    """Own only our diagnostic window; never stop somebody else's window."""
    def __init__(self, root=Path("/")):
        self.root = Path(root)
        self.owner = None
        self.highest_event = None
        self.observed_owner = None
        self.observer_missed_events = 0
        self.previous_lost = None
        self.previous_seq = None

    def _core(self):
        return parse_core(read_bounded(self.root / CONTROL, 4096))

    def _write(self, command):
        try:
            fd = os.open(self.root / CONTROL, os.O_WRONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    return unavailable("control_not_regular")
                encoded = command.encode("ascii")
                size = os.write(fd, encoded)
            finally:
                os.close(fd)
            if size != len(encoded):
                return unavailable("short_control_write")
            return {"available": True}
        except OSError as error:
            return unavailable("control_write_error", error.errno)

    def start(self, seconds):
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1 or seconds > 900:
            return unavailable("invalid_duration")
        before = self._core()
        if not before.get("available"):
            return {"available": False, "reason": "existing_window_state_unavailable", "before": before}
        if before["active"]:
            return {"available": False, "reason": "existing_active_window", "before": before}
        if self.owner is not None:
            return unavailable("owner_already_assigned")
        try:
            self.owner = secrets.randbits(63) or 1
        except OSError as error:
            return unavailable("owner_entropy_unavailable", error.errno)
        duration = min(900, seconds + 15)
        written = self._write(f"start {duration} {self.owner}\n")
        if not written.get("available"):
            return {"available": False, "reason": "start_write_failed", "write": written, "owner": self.owner}
        after = self._core()
        verified = bool(after.get("available") and after["active"] and after["owner"] == self.owner)
        return {"available": verified, "reason": "owned_window_verified" if verified else "start_not_verified",
                "owner": self.owner, "seconds": duration, "before": before, "after": after}

    def sample(self):
        core = self._core()
        events = parse_core(read_bounded(self.root / EVENTS, 4096))
        if core.get("available") and any(row["kind"] == "E" for row in core["rows"]):
            core = unavailable("event_rows_in_core_node")
        if events.get("available"):
            sequences = [row["values"][0] for row in events["rows"] if row["kind"] == "E"]
            if any(row["kind"] != "E" for row in events["rows"]):
                events = unavailable("non_event_rows_in_event_node")
            elif (core.get("available") and
                  (core["owner"], core["start"]) != (events["owner"], events["start"])):
                events = unavailable("snapshot_owner_changed")
            elif sequences != sorted(set(sequences)) or any(value > events["seq"] for value in sequences):
                events = unavailable("invalid_event_sequence")
            else:
                new_lifetime = (self.observed_owner != (events["owner"], events["start"]) or
                                self.previous_seq is not None and events["seq"] < self.previous_seq)
                if new_lifetime:
                    self.highest_event = None
                    self.observer_missed_events = 0
                    self.previous_lost = None
                    self.observed_owner = (events["owner"], events["start"])
                previous = self.highest_event
                new = [event for event in sequences if previous is None or event > previous]
                # Initial retained history may already have overwritten earlier events.
                baseline = previous if previous is not None else 0
                for event in new:
                    self.observer_missed_events += max(0, event - baseline - 1)
                    baseline = event
                if new:
                    self.highest_event = new[-1]
                events["observer_missed_events"] = self.observer_missed_events
                events["observer_sequence_reset"] = new_lifetime
                events["new_events"] = len(new)
                events["owned"] = self.owner is not None and events["owner"] == self.owner
                events["loss_counter_reset"] = self.previous_lost is not None and events["lost"] < self.previous_lost
                events["events_not_observed_yet"] = events["seq"] - (self.highest_event or 0)
                self.previous_lost = events["lost"]
                self.previous_seq = events["seq"]
                events["overwritten_semantics"] = "ring_reuse_not_necessarily_observer_loss"
        return {"core": core, "events": events,
                "hardware": parse_hardware(read_bounded(self.root / HARDWARE, 4096))}

    def stop(self):
        if self.owner is None:
            return unavailable("no_owned_window")
        before = self._core()
        if not before.get("available"):
            return {"available": False, "reason": "ownership_unverified_no_stop", "before": before}
        if before["owner"] != self.owner:
            return {"available": False, "reason": "different_owner_no_stop", "before": before}
        if not before["active"]:
            return {"available": True, "reason": "owned_window_already_inactive", "before": before}
        written = self._write(f"stop {self.owner}\n")
        if not written.get("available"):
            return {"available": False, "reason": "stop_write_failed", "write": written}
        after = self._core()
        verified = bool(after.get("available") and after["owner"] == self.owner and not after["active"])
        return {"available": verified, "reason": "owned_window_stopped" if verified else "stop_not_verified",
                "before": before, "after": after}
