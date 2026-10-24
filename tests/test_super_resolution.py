import argparse
from decimal import Decimal
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import encode
import super_resolution as sr


class SuperResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root / "realesrgan.exe"
        self.exe.write_bytes(b"exe")
        self.models = self.root / "models"
        self.models.mkdir()
        for extension in (".param", ".bin"):
            (self.models / ("realesrgan-x4plus" + extension)).write_bytes(b"model")
        self.args = argparse.Namespace(super_resolution=True, sr_exe=self.exe, sr_model_dir=None,
                                       sr_model="realesrgan-x4plus", sr_scale=4, sr_tile=128, sr_gpu=None,
                                       sr_work_dir=None, allow_input_errors=False, start=0, duration=None)
        self.cfg = encode.load_profile(encode.ROOT / "profiles.json", "hevc-nvenc")

    def configure(self):
        return sr.configure(self.args, self.cfg, lambda path: path)

    def test_disabled_does_not_require_installed_tool(self):
        self.args.super_resolution = False
        self.assertIsNone(sr.configure(self.args, self.cfg, lambda path: self.fail("resolved tool")))

    def test_config_hashes_model_changes_for_resume(self):
        first = self.configure()
        (self.models / "realesrgan-x4plus.bin").write_bytes(b"different model")
        self.assertNotEqual(first["model_sha256"], self.configure()["model_sha256"])
        self.assertEqual(first["scale"], 4)

    def test_invalid_scales_profiles_and_tiles_fail(self):
        for key, value in (("sr_scale", 2), ("sr_tile", 31), ("sr_gpu", -1)):
            old = getattr(self.args, key)
            setattr(self.args, key, value)
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.configure()
            setattr(self.args, key, old)
        self.cfg["codec"] = "libx264"
        with self.assertRaisesRegex(ValueError, "HEVC"):
            self.configure()

    def test_missing_model_is_reported_before_processing(self):
        (self.models / "realesrgan-x4plus.bin").unlink()
        with self.assertRaisesRegex(ValueError, "model files missing"):
            self.configure()

    def test_sar_normalization_before_model_scale(self):
        self.assertEqual(sr.output_size({"width": 1440, "height": 1080, "sample_aspect_ratio": "4:3"}, 2),
                         (3840, 2160))
        self.assertEqual(sr.output_size({"width": 720, "height": 480, "sample_aspect_ratio": "8:9"}, 4),
                         (2560, 1920))

    def test_vfr_manifest_keeps_gaps_and_last_duration(self):
        frames = [{"best_effort_timestamp_time": t, "duration_time": "0.016"}
                  for t in ("0.021", "0.054", "0.071", "0.104")]
        times, last = sr.frame_timeline(frames, "60000/1001")
        text = sr.manifest_text([f"{i:09d}.png" for i in range(4)], times, last, "60000/1001")
        self.assertEqual(text.count("file '"), 5)
        self.assertIn("duration 0.033000000", text)
        self.assertIn("duration 0.017000000", text)
        self.assertTrue(text.endswith("file '000000003.png'\noption framerate 60000/1001\n"))
        self.assertEqual(last, Decimal("0.016"))

    def test_invalid_timestamps_rejected(self):
        for frames in ([], [{}], [{"best_effort_timestamp_time": "NaN"}],
                       [{"best_effort_timestamp_time": "0"}] * 2):
            with self.subTest(frames=frames), self.assertRaises(ValueError):
                sr.frame_timeline(frames, "30000/1001")

    def test_workspace_removed_on_subprocess_failure(self):
        for error in (subprocess.CalledProcessError(1, ["ffmpeg"]), KeyboardInterrupt()):
            def fail(cmd, **kwargs):
                self.assertEqual(len(list(self.root.glob(".sr-*"))), 1)
                raise error
            with self.subTest(error=error), self.assertRaises(type(error)):
                with sr.prepare(self.root / "input.ts", self.args, self.cfg, {"index": 0}, [],
                                "ffmpeg", "ffprobe", {}, runner=fail, probe=None, status=lambda *a, **kw: None,
                                log=io.StringIO(), workspace_parent=self.root):
                    self.fail("preparation succeeded")
            self.assertEqual(list(self.root.glob(".sr-*")), [])

    def test_final_command_does_not_deinterlace_twice_and_maps_audio(self):
        prepared = {"intermediate": self.root / "decoded.mkv", "manifest": self.root / "frames.ffconcat",
                    "video": {"index": 0, "avg_frame_rate": "30000/1001"},
                    "audios": [{"index": 1, "channels": 2, "tags": {"language": "jpn"}}],
                    "offset": Decimal("0.021"), "duration": Decimal("1.5"), "matrix": "bt709"}
        cmd = sr.final_command(encode.command, "ffmpeg", self.root / "output.mp4", self.cfg, prepared, (3840, 2160))
        self.assertNotIn("bwdif", " ".join(cmd))
        maps = [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == "-map"]
        self.assertEqual(maps, ["0:0", "1:1"])
        self.assertIn("language=jpn", cmd)
        self.assertIn("-copyts", cmd)
        self.assertEqual(cmd[cmd.index("-itsoffset") + 1], "0.021")
        self.assertNotIn("-r", cmd)
        self.assertIn("pad=3840:2160", " ".join(cmd))

    def test_timing_verification_rejects_loss_or_shift(self):
        prepared = {"frames": 2, "times": [Decimal("0.021"), Decimal("0.054")]}
        for values in (("0.021",), ("0.0", "0.033")):
            def run(*args, **kwargs):
                return subprocess.CompletedProcess([], 0, json.dumps({"frames": [
                    {"best_effort_timestamp_time": t} for t in values]}))
            with self.subTest(values=values), self.assertRaises(ValueError):
                sr.verify_timing("ffprobe", "output.mp4", prepared, run)


if __name__ == "__main__":
    unittest.main()
