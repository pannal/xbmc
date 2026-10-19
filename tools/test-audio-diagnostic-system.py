#!/usr/bin/env python3
"""Fixture-only tests for the bounded collector helper, with no target access."""

import gzip
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("audio_diagnostic_system", ROOT / "tools/Linux/audio-diagnostic-system.py")
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


def task_stat(tid, start=50, user=100, system=20, name="PAPlayer", block=2):
    fields = ["0"] * 50
    fields[0] = "S"
    for index, value in ((11, user), (12, system), (15, 20), (16, 0), (19, start),
                         (36, 3), (37, 0), (38, 0), (39, block)):
        fields[index] = str(value)
    return f"{tid} ({name}) " + " ".join(fields) + "\n"


def core(owner=7, active=0, seq=0, lost=0, overwritten=0, events=(), extra=()):
    rows = [f"AAD1 now=100 active={active} owner={owner} start=1 deadline=200 lost={lost} seq={seq} overwritten={overwritten}"]
    rows.extend(extra)
    rows.extend(f"E {event} 90 1 0 2 3 4" for event in events)
    rows.append("END truncated=0 snapshot_bytes=400")
    return "\n".join(rows) + "\n"


HARDWARE = "HW1 soc=21 ready_after=1 n=4704 cts_raw=247500,247500,247500,247500 cts3_raw=3 stable=1 fifo_latch_raw=0 fifo_mask_raw=7 packet=9 gcp=1 clkdis=0 acr_auto=63 inputclkfs=4 pll0=3674277111 pll1=65536\n"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.put("proc/sys/kernel/random/boot_id", "a2345678-1234-1234-1234-123456789abc\n")
        self.put("proc/sys/kernel/sched_schedstats", "0\n")
        self.put("proc/config.gz", gzip.compress(b"CONFIG_SCHEDSTATS=y\nCONFIG_SCHED_INFO=y\nCONFIG_TASK_DELAY_ACCT=y\nCONFIG_IRQ_TIME_ACCOUNTING=y\nPRIVATE=/home/private/secret\n"))
        self.put("proc/stat", "cpu 100 0 30 500 20 5 10 0 100 0\ncpu0 60 0 20 250 10 2 6 0 60 0\ncpu1 40 0 10 250 10 3 4 0 40 0\nctxt 50\nprocs_running 1\nprocs_blocked 0\n")
        self.put("proc/interrupts", "           CPU0 CPU1\n 12: 100 0 GICv2 155 Edge ff642000.audiobus:ddr_manager\n 13: 20 30 GICv2 50 Edge vsync\n 14: 40 50 GICv2 53 Edge /home/private/credential\n")
        self.put("proc/softirqs", " CPU0 CPU1\n TIMER: 20 30\n SCHED: 5 6\n SECRET: 1 2\n")
        self.process(100)
        self.task(100, 100, "kodi.bin", start=10)
        self.task(100, 101, "PAPlayer")
        self.task(100, 102, "ActiveAE")
        self.task(100, 103, "AESink")
        self.task(100, 104, "secret-worker")
        self.put(HELPER.CONTROL, core())
        self.put(HELPER.EVENTS, core())
        self.put(HELPER.HARDWARE, HARDWARE)

    def put(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            target.write_bytes(value)
        else:
            target.write_text(value)

    def process(self, pid, start=10):
        self.put(f"proc/{pid}/comm", "kodi.bin\n")
        self.put(f"proc/{pid}/stat", task_stat(pid, start=start, name="kodi.bin"))

    def task(self, pid, tid, comm, start=50, user=100, run=10000, wait=30, switches=5):
        base = f"proc/{pid}/task/{tid}"
        self.put(base + "/comm", comm + "\n")
        self.put(base + "/stat", task_stat(tid, start=start, user=user, name=comm + ") ( private"))
        self.put(base + "/schedstat", f"{run} {wait} {switches}\n")
        self.put(base + "/status", "Name:\tprivate\nUid:\t111 222\nvoluntary_ctxt_switches:\t25\nnonvoluntary_ctxt_switches:\t4\nCpus_allowed_list:\t0-3,5\nVmRSS:\t999\n/home/private/credential\n")

    def test_identity_and_privacy(self):
        ident = HELPER.identity(self.root)
        self.assertEqual(ident["kodi"], {"available": True, "pid": 100, "start_ticks": 10})
        sample = HELPER.SystemSampler(self.root).sample()
        self.assertEqual({thread["role"] for thread in sample["threads"]}, {"main", "player", "engine", "sink"})
        exported = json.dumps(sample)
        for private in ("private", "credential", "secret-worker", "VmRSS", "Uid", "/home/", "GICv2", "audiobus"):
            self.assertNotIn(private, exported)
        self.assertEqual(len(sample["interrupts"]["rows"]), 2)
        self.assertEqual(sample["threads"][0]["status"]["allowed_cpus"], [0, 1, 2, 3, 5])

    def test_runtime_toggle_does_not_invalidate_vendor_sched_info(self):
        sample = HELPER.SystemSampler(self.root).sample()
        self.assertFalse(sample["runtime_schedstats"]["enabled"])
        self.assertFalse(sample["extended_schedstats_available"])
        self.assertTrue(sample["threads"][0]["schedstat"]["runqueue_observation_available"])
        (self.root / "proc/config.gz").unlink()
        sample = HELPER.SystemSampler(self.root).sample()
        self.assertFalse(sample["threads"][0]["schedstat"]["runqueue_observation_available"])
        self.assertNotIn("runqueue_wait_ns", sample["threads"][0]["delta"])

    def test_thread_deltas_intentional_wait_and_lifetime_reset(self):
        sampler = HELPER.SystemSampler(self.root)
        first = sampler.sample()
        self.assertFalse(first["threads"][1]["delta"]["available"])
        self.task(100, 101, "PAPlayer", user=104, run=10040, wait=42, switches=7)
        second = sampler.sample()
        delta = next(thread for thread in second["threads"] if thread["tid"] == 101)["delta"]
        self.assertTrue(delta["available"])
        self.assertEqual(delta["user_ticks"], 4)
        self.assertEqual(delta["runqueue_wait_ns"], 12)
        self.assertEqual(delta["timeslices"], 2)
        self.assertGreater(delta["interval_ns"], 0)
        self.assertGreater(second["interval_ns"], 0)
        self.task(100, 101, "PAPlayer", start=51)
        third = sampler.sample()
        delta = next(thread for thread in third["threads"] if thread["tid"] == 101)["delta"]
        self.assertEqual(delta["reason"], "new_thread_lifetime")

    def test_counter_backward_source_loss_boot_and_pid_reset(self):
        sampler = HELPER.SystemSampler(self.root)
        sampler.sample()
        self.task(100, 101, "PAPlayer", run=3)
        sample = sampler.sample()
        self.assertEqual(sample["threads"][1]["delta"]["reason"], "counter_reset_or_sources_changed")
        (self.root / "proc/100/task/101/schedstat").unlink()
        sample = sampler.sample()
        self.assertEqual(sample["threads"][1]["delta"]["reason"], "counter_reset_or_sources_changed")
        self.put("proc/sys/kernel/random/boot_id", "b2345678-1234-1234-1234-123456789abc\n")
        sample = sampler.sample()
        self.assertFalse(sample["threads"][0]["delta"]["available"])
        self.process(100, start=11)
        self.task(100, 100, "kodi.bin", start=11)
        sample = sampler.sample()
        self.assertEqual(sample["threads"][1]["delta"]["reason"], "new_thread_lifetime")

    def test_cpu_irq_deltas_and_no_guest_doublecount(self):
        sampler = HELPER.SystemSampler(self.root)
        sampler.sample()
        self.put("proc/stat", "cpu 110 0 30 510 20 5 10 0 999999 0\ncpu0 70 0 20 260 10 2 6 0 999999 0\ncpu1 40 0 10 250 10 3 4 0 0 0\n")
        self.put("proc/interrupts", " CPU0 CPU1\n12: 107 1 controller ddr_manager\n13: 25 30 controller vsync\n")
        sample = sampler.sample()
        delta = sample["cpu"]["deltas"][0]["delta"]
        self.assertEqual(delta["counts"], [10, 0, 0, 10, 0, 0, 0, 0])
        self.assertEqual(delta["busy_per_mille"], 500)
        self.assertEqual(sample["interrupts"]["deltas"][0]["delta"]["counts"], [7, 1])
        self.assertFalse(sample["cpu"]["deltas"][2]["delta"]["busy_per_mille"])

    def test_unavailable_nodes_and_missing_roles_are_explicit(self):
        (self.root / "proc/100/task/101/comm").unlink()
        (self.root / "proc/stat").unlink()
        self.put("proc/softirqs", " CPU0 CPU1\nPRIVATE: 3 4\n")
        sample = HELPER.SystemSampler(self.root).sample()
        self.assertFalse(sample["cpu"]["available"])
        self.assertFalse(sample["softirqs"]["available"])
        self.assertIn("player", sample["unobserved_roles"])
        self.process(200)
        sample = HELPER.SystemSampler(self.root).sample()
        self.assertEqual(sample["identity"]["kodi"]["reason"], "multiple_kodi_processes")
        self.assertFalse(sample["task_enumeration"]["available"])

    def test_read_enumeration_affinity_and_compressed_limits(self):
        self.put("too_big", "x" * 10)
        self.assertEqual(HELPER.read_bounded(self.root / "too_big", 5)["reason"], "size_limit")
        (self.root / "link").symlink_to(self.root / "too_big")
        self.assertFalse(HELPER.read_bounded(self.root / "link")["available"])
        self.put("proc/config.gz", gzip.compress(b"x" * (256 * 1024 + 1)))
        self.assertEqual(HELPER._config(self.root)["reason"], "decompressed_size_limit")
        with mock.patch.object(HELPER, "MAX_PROC_ENTRIES", 1):
            self.assertFalse(HELPER.identity(self.root)["kodi"]["available"])
        bad = {"available": True, "data": b"Cpus_allowed_list:\t0-999999\n"}
        self.assertFalse(HELPER._status(bad)["available"])
        with mock.patch.object(HELPER, "MAX_TASK_ENTRIES", 1):
            self.assertFalse(HELPER.SystemSampler(self.root).sample()["task_enumeration"]["available"])

    def test_numeric_parser_rejects_injection_and_overflow(self):
        for value in ("-1", "18446744073709551616", "/home/private", "1e8", "1\n"):
            with self.assertRaises(ValueError):
                HELPER.unsigned(value)
        for data in (b"1 2", b"1 2 18446744073709551616", b"private 3 4", b"0 0 0 extra"):
            self.assertFalse(HELPER._schedstat({"available": True, "data": data})["available"])
        self.put("proc/sys/kernel/random/boot_id", "private-uuid\n")
        sample = HELPER.SystemSampler(self.root).sample()
        self.assertFalse(sample["identity"]["boot_id"]["available"])
        self.assertEqual(sample["threads"][0]["delta"]["reason"], "boot_identity_unavailable")

    def test_core_numeric_contract_and_raw_hardware(self):
        text = core(seq=2, events=(1, 2), extra=("R 1 2 44100 4096 8192 3 100", "S 1 2 3 4 5 6 7 8 9 10 11 12 13"))
        parsed = HELPER.parse_core({"available": True, "data": text.encode()})
        self.assertTrue(parsed["available"])
        self.assertEqual([row["kind"] for row in parsed["rows"]], ["R", "S", "E", "E"])
        hardware = HELPER.parse_hardware({"available": True, "data": HARDWARE.encode()})
        self.assertTrue(hardware["available"])
        self.assertEqual(hardware["cts_raw"], [247500] * 4)
        self.assertIn("no_physical", hardware["semantics"])
        for text in (core().replace("lost=0", "secret=0"), core().replace("END truncated=0", "END truncated=3"),
                     core(extra=("E 1 2 3 4 5 /home/private 7",)), "PRIVATE\n", core().replace("owner=7", "owner=-7")):
            self.assertFalse(HELPER.parse_core({"available": True, "data": text.encode()})["available"])
        for text in (HARDWARE.replace("soc=21", "secret=21"), HARDWARE.replace("247500,247500,247500,247500", "1,2,3"),
                     "UNAVAILABLE private_detail", HARDWARE.replace("soc=21", "soc=21 soc=21")):
            self.assertFalse(HELPER.parse_hardware({"available": True, "data": text.encode()})["available"])

    def test_observer_counts_actual_sequence_gaps_not_sticky_ring_reuse(self):
        capture = HELPER.KernelCapture(self.root)
        self.put(HELPER.EVENTS, core(seq=3, overwritten=10, events=(1, 2, 3)))
        first = capture.sample()["events"]
        self.assertEqual(first["observer_missed_events"], 0)
        second = capture.sample()["events"]
        self.assertEqual(second["new_events"], 0)
        self.assertEqual(second["observer_missed_events"], 0)
        self.put(HELPER.EVENTS, core(seq=6, events=(5, 6)))
        third = capture.sample()["events"]
        self.assertEqual(third["observer_missed_events"], 1)
        self.put(HELPER.CONTROL, core(owner=8))
        self.put(HELPER.EVENTS, core(owner=8, seq=3, events=(3,)))
        fourth = capture.sample()["events"]
        self.assertTrue(fourth["observer_sequence_reset"])
        self.assertEqual(fourth["observer_missed_events"], 2)
        self.put(HELPER.EVENTS, core(owner=8, seq=1, events=(1,)))
        self.assertTrue(capture.sample()["events"]["observer_sequence_reset"])
        self.put(HELPER.EVENTS, core(owner=8, seq=3, events=(3, 2)))
        self.assertFalse(capture.sample()["events"]["available"])

    def test_start_refuses_existing_window_and_unavailable_state(self):
        capture = HELPER.KernelCapture(self.root)
        self.put(HELPER.CONTROL, core(active=1))
        with mock.patch.object(capture, "_write") as write:
            result = capture.start(180)
            self.assertEqual(result["reason"], "existing_active_window")
            write.assert_not_called()
        self.put(HELPER.CONTROL, "unrecognized")
        with mock.patch.object(capture, "_write") as write:
            self.assertFalse(capture.start(180)["available"])
            write.assert_not_called()
        for duration in (0, -1, True, 901, 1.5):
            self.assertEqual(capture.start(duration)["reason"], "invalid_duration")

    def test_owner_scoped_start_stop_and_expiry(self):
        capture = HELPER.KernelCapture(self.root)
        commands = []
        def kernel_write(command):
            commands.append(command)
            fields = command.split()
            if fields[0] == "start":
                self.put(HELPER.CONTROL, core(owner=int(fields[2]), active=1))
            else:
                self.put(HELPER.CONTROL, core(owner=int(fields[1]), active=0))
            return {"available": True}
        with mock.patch.object(HELPER.secrets, "randbits", return_value=42), mock.patch.object(capture, "_write", side_effect=kernel_write):
            result = capture.start(180)
            self.assertTrue(result["available"])
            self.assertEqual(commands, ["start 195 42\n"])
            self.assertTrue(capture.stop()["available"])
            self.assertEqual(commands[-1], "stop 42\n")
            self.assertEqual(capture.stop()["reason"], "owned_window_already_inactive")
            self.assertEqual(len(commands), 2)
        capture = HELPER.KernelCapture(self.root)
        with mock.patch.object(HELPER.secrets, "randbits", return_value=43), mock.patch.object(capture, "_write", side_effect=kernel_write):
            self.assertTrue(capture.start(900)["available"])
            self.assertEqual(commands[-1], "start 900 43\n")

    def test_never_stop_unowned_replaced_or_unverified_window(self):
        capture = HELPER.KernelCapture(self.root)
        with mock.patch.object(capture, "_write") as write:
            self.assertEqual(capture.stop()["reason"], "no_owned_window")
            capture.owner = 42
            self.put(HELPER.CONTROL, core(owner=43, active=1))
            self.assertEqual(capture.stop()["reason"], "different_owner_no_stop")
            self.put(HELPER.CONTROL, "malformed")
            self.assertEqual(capture.stop()["reason"], "ownership_unverified_no_stop")
            write.assert_not_called()

    def test_write_failures_no_create_symlink_or_short_write(self):
        capture = HELPER.KernelCapture(self.root)
        with mock.patch.object(HELPER.os, "write", return_value=1):
            self.assertEqual(capture._write("stop 7\n")["reason"], "short_control_write")
        (self.root / HELPER.CONTROL).unlink()
        self.assertFalse(capture._write("stop 7\n")["available"])
        self.assertFalse((self.root / HELPER.CONTROL).exists())
        self.put("protected", "unchanged")
        (self.root / HELPER.CONTROL).symlink_to(self.root / "protected")
        self.assertFalse(capture._write("stop 7\n")["available"])
        self.assertEqual((self.root / "protected").read_text(), "unchanged")

    def test_start_verification_failure_preserves_safe_stop_check(self):
        capture = HELPER.KernelCapture(self.root)
        with mock.patch.object(HELPER.secrets, "randbits", return_value=42), mock.patch.object(capture, "_write", return_value={"available": True}) as write:
            result = capture.start(180)
            self.assertEqual(result["reason"], "start_not_verified")
            self.assertFalse(result["available"])
            self.assertEqual(capture.stop()["reason"], "different_owner_no_stop")
            self.assertEqual(write.call_count, 1)
        capture = HELPER.KernelCapture(self.root)
        with mock.patch.object(capture, "_write", return_value={"available": False, "reason": "write_error", "errno": 16}):
            self.assertEqual(capture.start(180)["reason"], "start_write_failed")

    def test_kernel_missing_entropy_and_snapshot_lifetime_change(self):
        capture = HELPER.KernelCapture(self.root)
        with mock.patch.object(HELPER.secrets, "randbits", side_effect=OSError(5, "private error path")), mock.patch.object(capture, "_write") as write:
            self.assertEqual(capture.start(180), {"available": False, "reason": "owner_entropy_unavailable", "errno": 5})
            write.assert_not_called()
        self.put(HELPER.EVENTS, core(owner=8, seq=2, events=(1, 2)))
        self.assertEqual(capture.sample()["events"]["reason"], "snapshot_owner_changed")
        (self.root / HELPER.CONTROL).unlink()
        (self.root / HELPER.EVENTS).unlink()
        (self.root / HELPER.HARDWARE).unlink()
        self.assertTrue(all(not observation["available"] for observation in capture.sample().values()))


if __name__ == "__main__":
    unittest.main()
