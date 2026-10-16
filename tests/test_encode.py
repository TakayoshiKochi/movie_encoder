import argparse
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import encode


VIDEO = {"index": 2, "codec_type": "video", "codec_name": "mpeg2video", "duration": "10"}
AUDIO = {"index": 3, "codec_type": "audio", "channels": 2, "tags": {"language": "jpn"}}
INFO = {"programs": [{"program_id": 101, "streams": [VIDEO, AUDIO]}],
        "streams": [VIDEO, AUDIO], "format": {"duration": "10"}}
OUTPUT = {"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"},
                      {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000"}], "format": {"duration": "10"}}


class EncodingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = encode.load_profile(encode.ROOT / "profiles.json", "tv-balanced")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "日本語 & space.ts"
        self.source.write_bytes(b"original TS")
        self.target = self.root / "output" / "test.ts.mp4"
        self.args = argparse.Namespace(program=None, start=0, duration=None, verify_full=False, dry_run=False)

    def run_job(self, probe=None, run=None):
        with patch("encode.probe", side_effect=probe or [INFO, OUTPUT]), \
             patch("encode.subprocess.run", side_effect=run or self.fake_ffmpeg):
            return encode.encode_one(self.source, self.target, self.args, self.cfg, "ffmpeg", "ffprobe")

    def fake_ffmpeg(self, cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"encoded MP4")

    def test_multiple_programs_require_selection(self):
        info = copy.deepcopy(INFO)
        info["programs"].append({"program_id": 102, "streams": [{**VIDEO, "index": 8}, {**AUDIO, "index": 9}]})
        with self.assertRaisesRegex(ValueError, "Multiple video programs"):
            encode.select_streams(info)
        video, audios = encode.select_streams(info, 102)
        self.assertEqual((video["index"], audios[0]["index"]), (8, 9))

    def test_unknown_program_rejected(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            encode.select_streams(INFO, 999)

    def test_audio_stays_in_selected_program(self):
        info = copy.deepcopy(INFO)
        info["streams"].append({**AUDIO, "index": 99})
        self.assertEqual([s["index"] for s in encode.select_streams(info)[1]], [3])

    def test_no_audio_is_supported(self):
        cmd = encode.command("ffmpeg", self.source, self.target, self.cfg, VIDEO, [])
        self.assertIn("-an", cmd)
        self.assertNotIn("-c:a", cmd)

    def test_command_preserves_paths_and_field_rate(self):
        cmd = encode.command("ffmpeg", self.source, self.target, self.cfg, VIDEO, [AUDIO], 3, 5)
        self.assertEqual(cmd[cmd.index("-i") + 1], str(self.source))
        self.assertIn("bwdif=mode=send_field:parity=auto:deint=interlaced", cmd)
        self.assertNotIn("-r", cmd)
        self.assertIn("0:2", cmd)
        self.assertIn("0:3", cmd)
        self.assertIn("language=jpn", cmd)
        self.assertIn("-n", cmd)
        self.assertIn("-xerror", cmd)
        self.assertEqual(cmd[cmd.index("-crf") + 1], "20")

    def test_surround_uses_separate_bitrate(self):
        cmd = encode.command("ffmpeg", self.source, self.target, self.cfg, VIDEO, [AUDIO, {**AUDIO, "index": 4, "channels": 6}])
        self.assertEqual(cmd[cmd.index("-b:a:0") + 1], "192k")
        self.assertEqual(cmd[cmd.index("-b:a:1") + 1], "384k")
        self.assertNotIn("-ac", cmd)

    def hevc_output(self, pixel_format="yuv420p"):
        output = copy.deepcopy(OUTPUT)
        output["streams"][0].update(codec_name="hevc", pix_fmt=pixel_format, codec_tag_string="hvc1")
        return output

    def test_hevc_command_uses_input_frame_rate_and_48k_audio(self):
        cfg = encode.load_profile(encode.ROOT / "profiles.json", "hevc-balanced")
        cfg["x265_params"] = "aq-mode=3"
        cmd = encode.command("ffmpeg", self.source, self.target, cfg, VIDEO, [AUDIO])
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx265")
        self.assertEqual(cmd[cmd.index("-tag:v") + 1], "hvc1")
        self.assertEqual(cmd[cmd.index("-ar") + 1], "48000")
        self.assertEqual(cmd[cmd.index("-b:a:0") + 1], "192k")
        self.assertIn("bwdif=mode=send_frame:parity=auto:deint=interlaced", cmd)
        self.assertIn("-x265-params", cmd)
        self.assertNotIn("-x264-params", cmd)
        self.assertNotIn("-r", cmd)

    def test_hevc_publish_and_resume(self):
        self.cfg = encode.load_profile(encode.ROOT / "profiles.json", "hevc-balanced")
        self.assertEqual(self.run_job(probe=[INFO, self.hevc_output()]), "encoded")
        self.assertEqual(self.run_job(probe=[INFO, self.hevc_output()]), "skipped")

    def test_hevc_10bit_is_validated_and_h264_rejected(self):
        encode.validate_output(self.hevc_output("yuv420p10le"), 1, 10, "hevc", "yuv420p10le")
        with self.assertRaisesRegex(ValueError, "hevc"):
            encode.validate_output(OUTPUT, 1, 10, "hevc", "yuv420p")
        with self.assertRaisesRegex(ValueError, "pixel format"):
            encode.validate_output(self.hevc_output(), 1, 10, "hevc", "yuv420p10le")

    def test_hevc_rejects_h264_tune_and_params(self):
        cfg = encode.load_profile(encode.ROOT / "profiles.json", "hevc-balanced")
        path = self.root / "config.json"
        for changed in ({"tune": "film"}, {"x264_params": "aq-mode=3"}):
            encode.write_json(path, {"test": {**cfg, **changed}})
            with self.assertRaises(ValueError):
                encode.load_profile(path, "test")

    def test_audio_sample_rate_is_verified(self):
        output = copy.deepcopy(OUTPUT)
        output["streams"][1]["sample_rate"] = "44100"
        with self.assertRaisesRegex(ValueError, "48000"):
            encode.validate_output(output, 1)

    def test_invalid_crf_and_unknown_keys_fail(self):
        for value in (-1, 52, float("nan")):
            with self.assertRaises(ValueError):
                encode.load_profile(encode.ROOT / "profiles.json", "tv-balanced", value)
        config = self.root / "bad.json"
        config.write_text(json.dumps({"bad": {**self.cfg, "crff": 10}}))
        with self.assertRaises(ValueError):
            encode.load_profile(config, "bad")

    def test_dry_run_creates_no_output(self):
        self.args.dry_run = True
        self.assertEqual(self.run_job(probe=[INFO]), "planned")
        self.assertFalse(self.target.parent.exists())

    def test_success_publishes_record_and_keeps_source(self):
        self.assertEqual(self.run_job(), "encoded")
        self.assertTrue(self.target.exists())
        record = encode.read_json(self.target.with_suffix(".mp4.json"))
        self.assertEqual(record["request"]["source"]["size"], len(b"original TS"))
        self.assertEqual(self.source.read_bytes(), b"original TS")
        self.assertFalse(list(self.target.parent.glob("*.partial.mp4")))

    def test_matching_job_skips_but_changed_profile_does_not(self):
        self.run_job()
        self.assertEqual(self.run_job(), "skipped")
        self.cfg["crf"] = 18
        with self.assertRaisesRegex(ValueError, "no matching job record"):
            self.run_job(probe=[INFO])

    def test_unrelated_output_is_never_overwritten(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b"unrelated")
        with self.assertRaisesRegex(ValueError, "no matching job record"):
            self.run_job(probe=[INFO])
        self.assertEqual(self.target.read_bytes(), b"unrelated")

    def test_failed_encode_does_not_publish(self):
        def fail(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"incomplete")
            raise encode.subprocess.CalledProcessError(1, cmd)
        with self.assertRaises(encode.subprocess.CalledProcessError):
            self.run_job(probe=[INFO], run=fail)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.source.read_bytes(), b"original TS")
        self.assertFalse(list(self.target.parent.glob("*.partial.mp4")))
        self.assertTrue(list(self.target.parent.glob("*.log")))

    def test_truncated_output_is_rejected(self):
        truncated = copy.deepcopy(OUTPUT)
        truncated["format"]["duration"] = "1"
        with self.assertRaisesRegex(ValueError, "Duration mismatch"):
            self.run_job(probe=[INFO, truncated])
        self.assertFalse(self.target.exists())

    def test_changed_source_is_rejected(self):
        def modify(cmd, **kwargs):
            self.fake_ffmpeg(cmd, **kwargs)
            self.source.write_bytes(b"growing recording")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            self.run_job(run=modify)
        self.assertFalse(self.target.exists())

    def test_full_decode_failure_is_rejected(self):
        self.args.verify_full = True
        def run(cmd, **kwargs):
            if cmd[-1] == "-":
                raise encode.subprocess.CalledProcessError(1, cmd)
            self.fake_ffmpeg(cmd, **kwargs)
        with self.assertRaises(encode.subprocess.CalledProcessError):
            self.run_job(run=run)
        self.assertFalse(self.target.exists())

    def test_publish_does_not_clobber(self):
        partial = self.root / "partial.mp4"
        partial.write_bytes(b"partial")
        self.target.parent.mkdir()
        self.target.write_bytes(b"existing")
        with self.assertRaises(OSError):
            encode.publish(partial, self.target)
        self.assertEqual(self.target.read_bytes(), b"existing")

    def test_sample_duration_accounts_for_end(self):
        self.assertEqual(encode.expected_duration(INFO, VIDEO, 8, 60), 2)
        with self.assertRaises(ValueError):
            encode.expected_duration(INFO, VIDEO, 12, None)

    def test_batch_skips_recent_and_preserves_relative_paths(self):
        nested = self.root / "input" / "series"
        nested.mkdir(parents=True)
        ts = nested / "episode.TS"
        ts.write_bytes(b"recording")
        argv = ["encode", str(nested.parent), "--recursive", "--dry-run", "--output-dir", str(self.root / "result")]
        with patch("encode.executable", side_effect=lambda name: name), patch("encode.encode_one", return_value="planned") as job:
            self.assertEqual(encode.main(argv), 0)
            job.assert_not_called()
            self.assertEqual(encode.main(argv + ["--min-age", "0"]), 0)
            self.assertEqual(job.call_args.args[1], self.root / "result" / "series" / "episode.TS.mp4")

    def test_output_lock_is_exclusive_and_released(self):
        with encode.output_lock(self.root):
            with self.assertRaisesRegex(ValueError, "busy"):
                with encode.output_lock(self.root):
                    self.fail("second writer acquired lock")
        with encode.output_lock(self.root):
            pass

    def test_batch_continues_after_a_failed_job(self):
        inputs = self.root / "input"
        inputs.mkdir()
        (inputs / "a.ts").write_bytes(b"bad")
        (inputs / "b.ts").write_bytes(b"good")
        argv = ["encode", str(inputs), "--dry-run", "--min-age", "0"]
        with patch("encode.executable", side_effect=lambda name: name), \
             patch("encode.encode_one", side_effect=[ValueError("bad TS"), "planned"]) as job:
            self.assertEqual(encode.main(argv), 1)
            self.assertEqual(job.call_count, 2)


if __name__ == "__main__":
    unittest.main()
