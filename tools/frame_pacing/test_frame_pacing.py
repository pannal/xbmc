"""Offline tests only: never connect to Kodi, DirectShow, preview or audio."""
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from fractions import Fraction
from unittest.mock import patch

import numpy as np

from .analysis import analyze, assess
from .capture import capture_command
from .marker import GEOMETRY, decode, render
from .media import (PQ_LUMA, generate, hdr_frame_bytes, load_manifest, observations,
                    pq_normalized, verify_source, write_json)




def samples(ids, rate="24", times=None):
    period = 1 / Fraction(rate)
    return [{"sample": i, "id": value, "pts": int((times[i] if times else i * period) * 720000),
             "time_base": "1/720000",
             "time": str(times[i] if times else i * period),
             "decode_status": "valid" if value is not None else "invalid_or_mixed"}
            for i, value in enumerate(ids)]


class AnalysisTests(unittest.TestCase):
    def test_equal_rate_duplicate_skip_and_reverse(self):
        result = assess(samples([1, 1, 3, 2, 5]), "24", "24")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["event_counts"]["backwards_id"], 1)
        self.assertEqual(result["event_counts"]["unobserved_ids_between_adjacent_samples"], 2)
        self.assertEqual(result["repeat_samples"], 1)
        self.assertEqual(result["runs"][0]["samples"], 2)

    def test_matched_rate_extra_repeat_without_skip_is_uncertain_not_hidden(self):
        result = assess(samples([10, 11, 11, 12, 13]), "24", "24", display_rate="24")
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["event_counts"]["excess_repeat_candidate"], 1)

    def test_exact_24_to_60_and_ntsc_cadence(self):
        for source, capture_rate in (("24", "60"), ("24000/1001", "60000/1001")):
            ids = [int(i * Fraction(source) / Fraction(capture_rate)) + 1 for i in range(120)]
            result = assess(samples(ids, capture_rate), source, capture_rate)
            self.assertEqual(result["status"], "observed_clean", result["events"])
            self.assertGreater(result["repeat_samples"], 0)
            self.assertEqual(set(result["run_samples_histogram"]), {2, 3})

    def test_unknown_and_pts_gap_do_not_claim_exact_missing_ids(self):
        result = assess(samples([1, None, 4, 8, 9], times=[
            Fraction(0), Fraction(1, 24), Fraction(2, 24), Fraction(7, 24), Fraction(8, 24)]),
            "24", "24")
        self.assertEqual(result["status"], "inconclusive")
        self.assertNotIn("unobserved_ids_between_adjacent_samples", result["event_counts"])
        self.assertEqual(result["event_counts"]["pts_gap"], 1)
        self.assertEqual(len(result["runs"]), 4)
        self.assertEqual(result["unknown_marker_intervals"][0]["coverage"], "interior")
        self.assertEqual(result["unknown_marker_intervals"][0]["samples"], 1)

    def test_insufficient_sampling(self):
        result = assess(samples([1, 3, 6, 8, 11], "24"), "60", "24")
        self.assertEqual(result["status"], "inconclusive")
        self.assertNotIn("unobserved_ids_between_adjacent_samples", result["event_counts"])
        self.assertIn("forward_jump_under_sampling", result["event_counts"])
        result = assess(samples([1, 3, 5, 7], "60"), "60", "60", display_rate="30")
        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("presentation_rate_limit", result["event_counts"])

    def test_hdmi_quantization_at_faster_capture_is_expected(self):
        # 24 -> 60 HDMI, observed at 240 Hz: 12, 8, 12, 8 samples per ID.
        ids = [int(Fraction((i // 4) * 2, 5)) + 1 for i in range(120)]
        result = assess(samples(ids, "240"), "24", "240", display_rate="60")
        self.assertEqual(result["status"], "observed_clean", result["events"])
        self.assertEqual(set(result["run_samples_histogram"]), {8, 12})

    def test_excess_hold_not_deduplicated(self):
        result = assess(samples([1, 2, 2, 2, 2, 2, 3, 4], "60"), "24", "60")
        self.assertEqual(result["status"], "fail")
        self.assertIn("extended_identical_id_hold", result["event_counts"])
        self.assertEqual(result["runs"][1]["samples"], 5)
        self.assertEqual(result["runs"][1]["observed_span"], "1/15")
        self.assertIsNotNone(result["runs"][1]["hold_upper_bound"])

    def test_head_tail_partial_coverage_and_no_signal(self):
        result = assess(samples([None, 40, 41, 42, None]), "24", "24")
        self.assertEqual(result["status"], "observed_clean")
        self.assertNotIn("unobserved_ids_between_adjacent_samples", result["event_counts"])
        self.assertEqual(assess(samples([None] * 5), "24", "24")["status"], "inconclusive")

    def test_source_boundary_holds_are_retained_but_not_steady_state_failures(self):
        ids = [None, 1, 1, 1, 1, 1, 2, 3, 4, 5, 5, 5, 5, 5, None]
        rows = samples(ids)
        result = assess(rows, "24", "24", frame_count=5)
        self.assertEqual(result["status"], "observed_clean", result["events"])
        self.assertEqual(result["repeat_samples"], 8)
        self.assertEqual(result["event_counts"]["source_boundary_hold_censored"], 2)
        self.assertEqual(result["runs"][-1]["samples"], 5)
        self.assertEqual([row["id"] for row in rows], ids)
        result = assess(samples([1, 2, 2, 2, 2, 3, 4]), "24", "24", frame_count=4)
        self.assertEqual(result["status"], "fail")
        self.assertIn("extended_identical_id_hold", result["event_counts"])
        result = assess(samples([1, 2, 5, 5, 5, 5, 4, 5]), "24", "24", frame_count=5)
        self.assertIn("extended_identical_id_hold", result["event_counts"])
        last_id_runs = [run for run in result["runs"] if run["id"] == 5]
        self.assertFalse(last_id_runs[0]["source_boundary_censored"])
        self.assertTrue(last_id_runs[1]["source_boundary_censored"])

    def test_nonmonotonic_and_missing_pts(self):
        rows = samples([1, 2, 3, 4], times=[Fraction(0), Fraction(0), Fraction(1, 24), Fraction(2, 24)])
        rows[2]["time"] = None
        result = assess(rows, "24", "24")
        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("pts_nonmonotonic", result["event_counts"])
        self.assertIn("missing_pts", result["event_counts"])


class MarkerTests(unittest.TestCase):
    def test_integrity_foreign_tag_and_mixed_copies(self):
        gray = np.array(render(35, 123, 640, 360, 100).convert("L"))
        self.assertEqual(decode(gray, 123)["id"], 35)
        self.assertIsNone(decode(gray, 124)["id"])
        other = np.array(render(36, 123, 640, 360, 100).convert("L"))
        gray[140:206] = other[140:206]
        self.assertIsNone(decode(gray, 123)["id"])
        self.assertIsNone(decode(np.full((360, 640), 127, dtype=np.uint8), 123)["id"])

    def test_corrupt_valid_complements_rejected_by_crc(self):
        gray = np.array(render(35, 123, 640, 360, 100).convert("L"))
        from .marker import cell_boxes
        for rect in GEOMETRY["rectangles"]:
            for index in (70, 71):
                x0, y0, x1, y1 = cell_boxes(640, 360, rect)[index]
                gray[y0:y1, x0:x1] = 255 - gray[y0:y1, x0:x1]
        self.assertIsNone(decode(gray, 123)["id"])


class EncodedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="frame-pacing-tests-")
        cls.root = pathlib.Path(cls.temp.name)
        cls.manifest_path = generate(cls.root / "source", "24000/1001", "0.5", 640, 360)
        cls.manifest = load_manifest(cls.manifest_path)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def fixture(self, name, ids, rate):
        path = self.root / (name + ".nut")
        command = ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pixel_format", "rgb24",
                   "-video_size", "640x360", "-framerate", rate, "-i", "pipe:0",
                   "-vf", "scale=480:270", "-fps_mode", "passthrough",
                   "-c:v", "libx264", "-crf", "23", "-bf", "0", "-pix_fmt", "yuv420p",
                   "-f", "nut", "-n", str(path)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE)
        try:
            for value in ids:
                image = (render(value, self.manifest["clip_tag"], 640, 360, 12)
                         if value is not None else np.full((360, 640, 3), 127, dtype=np.uint8))
                process.stdin.write(image.tobytes())
            process.stdin.close()
            self.assertEqual(process.wait(timeout=30), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        return path

    def test_all_ids_encoded_source_roundtrip(self):
        result = verify_source(self.manifest_path)
        self.assertEqual(result["verified_ids"], 12)
        self.assertEqual(result["rate"], "24000/1001")
        self.assertEqual(result["format_verification"]["stream"]["color_primaries"], "bt709")
        self.assertEqual(result["format_verification"]["stream"]["color_transfer"], "bt709")

    def test_hdr10_actual_10bit_pq_and_static_metadata_roundtrip(self):
        manifest = generate(self.root / "hdr10", "24", "1/8", 640, 360, "hdr10")
        result = verify_source(manifest)
        fmt = result["format_verification"]
        self.assertEqual(result["verified_ids"], 3)
        self.assertEqual(fmt["stream"]["pix_fmt"], "yuv420p10le")
        self.assertEqual(fmt["stream"]["color_transfer"], "smpte2084")
        self.assertEqual(fmt["rpu_frames"], 0)
        self.assertAlmostEqual(float(pq_normalized(1000)), .751827096247041, places=12)
        self.assertEqual(int(PQ_LUMA[255]), 723)
        image = render(1, 123, 640, 360, 3)
        self.assertEqual(len(hdr_frame_bytes(image)), 640 * 360 * 3)

    def test_dv_actual_container_profile_and_every_frame_rpu_roundtrip(self):
        import shutil
        selected = os.environ.get("DOVI_TOOL") or shutil.which("dovi_tool")
        if not selected:
            self.skipTest("Optional DV generation test requires dovi_tool on PATH or DOVI_TOOL.")
        tool = pathlib.Path(selected)
        manifest = generate(self.root / "dv", "24000/1001", "1/8", 640, 360, "dv", tool)
        result = verify_source(manifest)
        fmt = result["format_verification"]
        self.assertEqual(result["verified_ids"], 3)
        self.assertEqual(fmt["rpu_frames"], 3)
        config = next(side for side in fmt["stream"]["side_data_list"]
                      if side["side_data_type"] == "DOVI configuration record")
        self.assertEqual(config["dv_profile"], 8)
        self.assertEqual(config["dv_bl_signal_compatibility_id"], 1)
        self.assertEqual(config["el_present_flag"], 0)

    def test_production_decode_after_h264_resize_and_bad_sequence(self):
        path = self.fixture("bad", [1, 1, 3, 2, None, 6, 7], "24000/1001")
        report = analyze(path, self.manifest_path, self.root / "bad-analysis", "24000/1001")
        self.assertEqual(report["status"], "fail")
        self.assertIn("backwards_id", report["event_counts"])
        self.assertIn("unobserved_ids_between_adjacent_samples", report["event_counts"])
        data = [json.loads(line) for line in (self.root / "bad-analysis" / "observations.jsonl").read_text().splitlines()]
        self.assertEqual([row["id"] for row in data], [1, 1, 3, 2, None, 6, 7])
        self.assertEqual(len(data), 7)
        self.assertEqual(report["event_counts"]["unobserved_ids_between_adjacent_samples"], 1)

    def test_production_unequal_rate_resized_cadence(self):
        ids = [int(Fraction(i * 2, 5)) + 1 for i in range(30)]
        path = self.fixture("cadence", ids, "60000/1001")
        report = analyze(path, self.manifest_path, self.root / "cadence-analysis", "60000/1001")
        self.assertEqual(report["status"], "observed_clean", report["events"])
        self.assertEqual(report["sample_count"], 30)
        self.assertEqual(report["run_samples_histogram"], {2: 6, 3: 6})

    def test_partial_source_coverage_is_not_a_full_pass_or_attributed_drop(self):
        path = self.fixture("partial", list(range(4, 13)), "24000/1001")
        report = analyze(path, self.manifest_path, self.root / "partial-analysis", "24000/1001")
        self.assertEqual(report["status"], "observed_clean")
        self.assertEqual(report["full_coverage_status"], "inconclusive")
        self.assertEqual(report["source_coverage"]["unobserved_id_ranges"], [[1, 3]])
        self.assertNotIn("unobserved_ids_between_adjacent_samples", report["event_counts"])

    def test_tampered_manifest_geometry_rejected(self):
        value = {**self.manifest, "marker": {**GEOMETRY, "columns": 21}}
        path = self.root / "bad-source.json"
        write_json(path, value)
        with self.assertRaises(ValueError):
            load_manifest(path)

    def test_native_mux_policy_preserves_nonzero_pts_without_duplication(self):
        path = self.root / "native-policy.nut"
        native = capture_command(path, "24000/1001", "640x360", 1,
                                 self.root / "native-progress.txt", device="Test capture")
        command = ["ffmpeg", "-v", "error", "-copyts", "-f", "lavfi", "-t", "1",
                   "-i", "testsrc2=size=640x360:rate=24000/1001,setpts=PTS+24000",
                   *native[native.index("-map"):]]
        subprocess.run(command, check=True, capture_output=True, timeout=30)
        rows = list(observations(path, self.manifest, self.root / "native-decode.log"))
        self.assertEqual(len(rows), 24)
        self.assertEqual([Fraction(row["time"]) for row in rows],
                         [1001 + i * Fraction(1001, 24000) for i in range(24)])

if __name__ == "__main__":
    unittest.main()
