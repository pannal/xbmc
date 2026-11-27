"""Hermetic capture-boundary tests: never open a camera or control playback."""
import io
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from .capture import capture, capture_command
from .marker import GEOMETRY, cell_boxes, decode, render

import numpy as np


class NativeCaptureTests(unittest.TestCase):
    def test_pin_default_and_exact_input_negotiation_never_resample_output(self):
        args = ("capture.nut", "23.98", "3840x2160", 75, "progress.txt", "Explicit capture")
        native = capture_command(*args)
        self.assertNotIn("-framerate", native)
        self.assertNotIn("-r", native)
        self.assertNotIn("-vf", native)
        self.assertEqual(native[native.index("-i") + 1], "video=Explicit capture")
        self.assertEqual(native[native.index("-enc_time_base:v") + 1], "demux")
        self.assertEqual(native[native.index("-fps_mode") + 1], "passthrough")
        negotiated = capture_command(*args, negotiate_rate=True)
        self.assertEqual(negotiated[negotiated.index("-framerate") + 1], "1199/50")
        self.assertLess(negotiated.index("-framerate"), negotiated.index("-i"))
        with self.assertRaises(ValueError):
            capture_command(*args[:-1], device="")

    def test_no_exclusive_ack_does_not_open_capture(self):
        with patch("frame_pacing.capture.subprocess.Popen") as launch:
            with self.assertRaises(ValueError):
                capture("not-created", "24", "640x360", 1, False, "Explicit capture")
            launch.assert_not_called()

    def run_fixture(self, returncode=0, warning=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = pathlib.Path(temp.name) / "recording"
        process = Mock()
        process.pid = 123
        process.wait.return_value = returncode
        process.poll.return_value = returncode
        process.stderr = io.BytesIO(b"native first timestamp\nnative second timestamp\n")
        def launch(command, **kwargs):
            folder.joinpath("capture.nut").write_bytes(b"hermetic output")
            folder.joinpath("capture-progress.txt").write_text(
                "frame=2\ndup_frames=0\ndrop_frames=" + ("1" if warning else "0") + "\n")
            return process
        with patch("frame_pacing.capture.subprocess.Popen", side_effect=launch) as create, \
                patch("frame_pacing.capture.subprocess.check_output", return_value="ffmpeg test\n"), \
                patch("frame_pacing.capture.shutil.disk_usage", return_value=SimpleNamespace(free=10 ** 12)):
            if returncode or warning:
                with self.assertRaises(RuntimeError):
                    capture(folder, "24", "640x360", 1, True, "Explicit capture")
            else:
                capture(folder, "24", "640x360", 1, True, "Explicit capture")
            self.assertEqual(create.call_count, 1)
        return folder, process

    def test_qpc_receipts_saved_and_owned_reader_reaped(self):
        folder, process = self.run_fixture()
        rows = [json.loads(line) for line in (folder / "qpc-arrival.jsonl").read_text().splitlines()]
        self.assertEqual([row["line"] for row in rows], ["native first timestamp", "native second timestamp"])
        self.assertLessEqual(rows[0]["qpc_ns"], rows[1]["qpc_ns"])
        self.assertTrue(process.stderr.closed)
        result = json.loads((folder / "capture.json").read_text())
        self.assertEqual(result["status"], "recorded_not_yet_analyzed")
        self.assertIn("sha256", result)
        process.terminate.assert_not_called()

    def test_capture_error_is_not_success_and_never_falls_back(self):
        folder, process = self.run_fixture(returncode=9)
        result = json.loads((folder / "capture.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("sha256", result)
        self.assertTrue(process.stderr.closed)

    def test_resampling_warning_cannot_be_accepted_as_clean(self):
        folder, _ = self.run_fixture(warning=True)
        result = json.loads((folder / "capture.json").read_text())
        self.assertEqual(result["status"], "capture_warning_inconclusive")
        self.assertNotIn("sha256", result)


class MarkerIntegrityTests(unittest.TestCase):
    def test_real_complement_and_crc_corruption_are_rejected(self):
        original = np.asarray(render(1440, 123, 640, 360, 1440).convert("L")).copy()
        self.assertEqual(decode(original, 123)["id"], 1440)
        boxes = cell_boxes(640, 360, GEOMETRY["rectangles"][0])
        for indexes, error in (([64], "complement_failure"), ([64, 65], "sync_or_crc_failure")):
            bad = original.copy()
            for index in indexes:
                x0, y0, x1, y1 = boxes[index]
                bad[y0:y1, x0:x1] = 255 - bad[y0:y1, x0:x1]
            decoded = decode(bad, 123)
            self.assertIsNone(decoded["id"])
            self.assertEqual(decoded["copies"][0]["error"], error)

    def test_valid_but_disagreeing_copies_are_unknown(self):
        original = np.asarray(render(1440, 123, 640, 360, 1440).convert("L")).copy()
        different = np.asarray(render(1439, 123, 640, 360, 1440).convert("L"))
        for x0, y0, x1, y1 in cell_boxes(640, 360, GEOMETRY["rectangles"][1]):
            original[y0:y1, x0:x1] = different[y0:y1, x0:x1]
        decoded = decode(original, 123)
        self.assertIsNone(decoded["id"])
        self.assertEqual([copy["id"] for copy in decoded["copies"]], [1440, 1439, 1440])


if __name__ == "__main__":
    unittest.main()
