import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import sys
import threading
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
        self.args = argparse.Namespace(program=None, start=0, duration=None, verify_full=False, dry_run=False,
                                       allow_input_errors=False, resize=None)

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

    def test_resize_dimensions_and_filter_order(self):
        self.assertEqual(encode.resize_size("1280x720"), (1280, 720))
        self.assertEqual(encode.resize_size("640X480"), (640, 480))
        cmd = encode.command("ffmpeg", self.source, self.target, self.cfg, VIDEO, [AUDIO], resize=(1280, 720))
        filters = cmd[cmd.index("-vf") + 1]
        self.assertLess(filters.index("bwdif="), filters.index("scale="))
        self.assertLess(filters.index("scale="), filters.index("setsar=1"))
        self.assertLess(filters.index("setsar=1"), filters.index("pad="))
        self.assertIn("720*dar", filters)
        self.assertIn("1280/dar", filters)
        self.assertIn("pad=1280:720", filters)
        self.assertIn("color=black", filters)

    def test_invalid_resize_rejected_before_launching_tools(self):
        for value in ("1280", "1280:720", "0x720", "1279x720", "1280x719", "-2x720", "1280x720,setsar=2"):
            with self.subTest(value=value), redirect_stderr(io.StringIO()), \
                 patch("encode.executable") as launch, self.assertRaises(SystemExit) as error:
                encode.main(["encode", str(self.source), "--resize", value])
            self.assertEqual(error.exception.code, 2)
            launch.assert_not_called()

    def test_progressive_resize_and_unchanged_default(self):
        cfg = encode.load_profile(encode.ROOT / "profiles.json", "hevc-progressive")
        self.assertEqual(encode.video_filter(cfg), "")
        filters = encode.video_filter(cfg, (1280, 720))
        self.assertNotIn("bwdif", filters)
        self.assertIn("scale=", filters)
        self.assertIn("setsar=1", filters)

    def test_resize_is_recorded_and_resume_matches_json(self):
        self.args.resize = (1280, 720)
        output = copy.deepcopy(OUTPUT)
        output["streams"][0].update(width=1280, height=720, sample_aspect_ratio="1:1")
        self.assertEqual(self.run_job(probe=[INFO, output]), "encoded")
        record = encode.read_json(self.target.with_suffix(".mp4.json"))
        self.assertEqual(record["request"]["resize"], [1280, 720])
        self.assertEqual(self.run_job(probe=[INFO, output]), "skipped")
        self.args.resize = (1920, 1080)
        with self.assertRaisesRegex(ValueError, "no matching job record"):
            self.run_job(probe=[INFO])
        self.args.resize = None
        with self.assertRaisesRegex(ValueError, "no matching job record"):
            self.run_job(probe=[INFO])

    def test_resize_rejects_wrong_size_or_pixel_aspect(self):
        self.args.resize = (1280, 720)
        for changes, message in (({"width": 960, "height": 720, "sample_aspect_ratio": "1:1"}, "size"),
                                 ({"width": 1280, "height": 720, "sample_aspect_ratio": "4:3"}, "square pixels")):
            output = copy.deepcopy(OUTPUT)
            output["streams"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                self.run_job(probe=[INFO, output])
            self.assertFalse(self.target.exists())

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

    def test_nvenc_command_uses_cq_and_shared_filters(self):
        cfg = encode.load_profile(encode.ROOT / "profiles.json", "hevc-nvenc", cq=26)
        cmd = encode.command("ffmpeg", self.source, self.target, cfg, VIDEO, [AUDIO])
        for flag, value in (("-c:v", "hevc_nvenc"), ("-cq", "26"), ("-rc", "vbr"),
                            ("-b:v", "0"), ("-preset", "p6"), ("-tune", "hq"),
                            ("-rc-lookahead", "32"), ("-multipass", "fullres"),
                            ("-bf", "3"), ("-spatial-aq", "1"), ("-temporal-aq", "1"),
                            ("-ar", "48000"), ("-tag:v", "hvc1"), ("-pix_fmt", "yuv420p")):
            self.assertEqual(cmd[cmd.index(flag) + 1], value)
        self.assertIn("bwdif=mode=send_frame:parity=auto:deint=interlaced", cmd)
        for option in ("-crf", "-x264-params", "-x265-params", "-hwaccel"):
            self.assertNotIn(option, cmd)

    def test_nvenc_publish_and_resume(self):
        self.cfg = encode.load_profile(encode.ROOT / "profiles.json", "hevc-nvenc")
        self.assertEqual(self.run_job(probe=[INFO, self.hevc_output()]), "encoded")
        record = encode.read_json(self.target.with_suffix(".mp4.json"))
        self.assertIn("encode_seconds", record)
        self.assertEqual(record["request"]["profile"]["cq"], 24)
        self.assertEqual(self.run_job(probe=[INFO, self.hevc_output()]), "skipped")

    def test_crf_and_cq_cannot_be_mixed(self):
        for profile, kwargs in (("hevc-nvenc", {"crf": 22}), ("hevc-balanced", {"cq": 22})):
            with self.assertRaises(ValueError):
                encode.load_profile(encode.ROOT / "profiles.json", profile, **kwargs)
        for value in (0, -1, 52, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                encode.load_profile(encode.ROOT / "profiles.json", "hevc-nvenc", cq=value)

    def test_nvenc_profile_rejects_unsupported_options(self):
        cfg = encode.load_profile(encode.ROOT / "profiles.json", "hevc-nvenc")
        path = self.root / "nvenc.json"
        changes = [{"preset": "medium"}, {"tune": "animation"}, {"x265_params": "aq-mode=3"},
                   {"pix_fmt": "yuv420p10le"}, {"crf": 22}]
        for key, value in (("lookahead", 33), ("bframes", -1), ("spatial_aq", 1),
                           ("multipass", "two-pass")):
            changes.append({"nvenc": {**cfg["nvenc"], key: value}})
        for change in changes:
            encode.write_json(path, {"test": {**cfg, **change}})
            with self.subTest(change=change), self.assertRaises(ValueError):
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

    def test_input_error_recovery_is_opt_in_and_recorded(self):
        self.args.allow_input_errors = True
        self.run_job()
        record = encode.read_json(self.target.with_suffix(".mp4.json"))
        self.assertTrue(record["request"]["allow_input_errors"])
        self.assertNotIn("-xerror", record["command"])
        self.assertNotIn("-err_detect", record["command"])
        self.assertNotIn("-fflags", record["command"])
        self.args.allow_input_errors = False
        with self.assertRaisesRegex(ValueError, "no matching job record"):
            self.run_job(probe=[INFO])

    def test_input_recovery_keeps_output_verification_strict(self):
        self.args.allow_input_errors = True
        self.args.verify_full = True
        calls = []
        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[-1] == "-":
                self.assertIn("-xerror", cmd)
                raise encode.subprocess.CalledProcessError(1, cmd)
            self.assertNotIn("-xerror", cmd)
            self.fake_ffmpeg(cmd, **kwargs)
        with self.assertRaises(encode.subprocess.CalledProcessError):
            self.run_job(run=run)
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.target.exists())

    def test_input_recovery_does_not_accept_truncated_output(self):
        self.args.allow_input_errors = True
        truncated = copy.deepcopy(OUTPUT)
        truncated["format"]["duration"] = "1"
        with self.assertRaisesRegex(ValueError, "Duration mismatch"):
            self.run_job(probe=[INFO, truncated])
        self.assertFalse(self.target.exists())

    def test_input_recovery_still_rejects_fatal_encode_failure(self):
        self.args.allow_input_errors = True
        def fail(cmd, **kwargs):
            self.fake_ffmpeg(cmd, **kwargs)
            raise encode.subprocess.CalledProcessError(1, cmd)
        with self.assertRaises(encode.subprocess.CalledProcessError):
            self.run_job(probe=[INFO], run=fail)
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

    def test_parallel_batch_respects_limit_and_continues_after_failure(self):
        inputs = self.root / "queue"
        inputs.mkdir()
        for name in ("a.ts", "b.ts", "c.ts", "d.ts"):
            (inputs / name).write_bytes(b"TS")
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active = peak = 0
        visited = []

        def job(path, *args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                visited.append(path.name)
            try:
                barrier.wait(timeout=5)
                if path.name == "a.ts":
                    raise ValueError("bad TS")
                return "planned"
            finally:
                with lock:
                    active -= 1

        output = io.StringIO()
        with patch("encode.executable", side_effect=lambda name: name), \
             patch("encode.encode_one", side_effect=job), redirect_stdout(output):
            code = encode.main(["encode", str(inputs), "--jobs", "2", "--min-age", "0", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertEqual(peak, 2)
        self.assertCountEqual(visited, ["a.ts", "b.ts", "c.ts", "d.ts"])
        starts = [line.split()[1] for line in output.getvalue().splitlines() if line.startswith("INPUT [")]
        self.assertEqual(starts, ["[1/4]", "[2/4]", "[3/4]", "[4/4]"])
        lines = output.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("BATCH START "))
        self.assertIn("ETA=calculating", lines[0])
        progress = [line for line in lines if line.startswith("PROGRESS [")]
        self.assertEqual([line.split()[1] for line in progress], ["[1/4]", "[2/4]", "[3/4]", "[4/4]"])
        self.assertTrue(all("elapsed=" in line and "ETA=" in line and "calculating" not in line for line in progress))
        self.assertTrue(any(line.startswith("BATCH END ") for line in lines))
        counts = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual((counts["planned"], counts["failed"]), (3, 1))

    def test_finish_estimate_uses_wall_throughput_and_crosses_midnight(self):
        started = datetime(2026, 10, 18, 23, 59, tzinfo=timezone(timedelta(hours=9)))
        self.assertIsNone(encode.estimated_finish(started, 10, 0, 10))
        # Two completed files in 30 wall-clock seconds: 10 files take ~150s,
        # irrespective of the number of workers. Never divide by jobs a second time.
        self.assertEqual(encode.estimated_finish(started, 30, 2, 10),
                         datetime(2026, 10, 19, 0, 1, 30, tzinfo=started.tzinfo))
        self.assertEqual(encode.estimated_finish(started, 100, 10, 10), started + timedelta(seconds=100))
        self.assertEqual(encode.estimated_finish(started, 0, 0, 0), started)

    def test_invalid_jobs_rejected_before_launching_tools(self):
        for value in ("0", "-1", "1.5"):
            with self.subTest(value=value), redirect_stderr(io.StringIO()), \
                 patch("encode.executable") as launch, self.assertRaises(SystemExit) as error:
                encode.main(["encode", str(self.source), "--jobs", value])
            self.assertEqual(error.exception.code, 2)
            launch.assert_not_called()

    def test_parallel_interrupt_stops_workers_without_starting_remaining_jobs(self):
        supervisor = encode.ProcessSupervisor()
        started = []
        barrier = threading.Barrier(3)

        def worker(job):
            started.append(job)
            barrier.wait(timeout=5)
            self.assertTrue(supervisor.cancelled.wait(timeout=5))
            return "pending"

        def interrupt(*args, **kwargs):
            barrier.wait(timeout=5)
            raise KeyboardInterrupt

        with patch("encode.wait", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            list(encode.run_jobs(list(range(10)), worker, 2, supervisor))
        self.assertCountEqual(started, [0, 1])
        self.assertTrue(supervisor.cancelled.is_set())

    def test_supervisor_captures_output_and_propagates_failure(self):
        supervisor = encode.ProcessSupervisor()
        result = supervisor.run([sys.executable, "-c", "print('ok')"], capture_output=True, encoding="utf-8", check=True)
        self.assertEqual(result.stdout.strip(), "ok")
        with self.assertRaises(encode.subprocess.CalledProcessError) as error:
            supervisor.run([sys.executable, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(7)"],
                           capture_output=True, encoding="utf-8", check=True)
        self.assertEqual(error.exception.returncode, 7)
        self.assertIn("bad", error.exception.stderr)
        self.assertFalse(supervisor.active)

    def test_cancellation_terminates_child_and_prevents_new_launches(self):
        supervisor = encode.ProcessSupervisor()
        spawned = threading.Event()
        original_popen = encode.subprocess.Popen
        children = []

        def spawn(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            children.append(child)
            spawned.set()
            return child

        with patch("encode.subprocess.Popen", side_effect=spawn), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(supervisor.run, [sys.executable, "-c", "import time; time.sleep(60)"], check=True)
            try:
                self.assertTrue(spawned.wait(timeout=5))
            finally:
                supervisor.cancel()
            with self.assertRaises(InterruptedError):
                future.result(timeout=5)
        self.assertIsNotNone(children[0].poll())
        self.assertFalse(supervisor.active)
        with patch("encode.subprocess.Popen") as spawn, self.assertRaises(InterruptedError):
            supervisor.run([sys.executable, "-c", "pass"])
        spawn.assert_not_called()

    def test_cancelled_encode_does_not_publish_partial_output(self):
        supervisor = encode.ProcessSupervisor()

        def finish_then_cancel(cmd, **kwargs):
            self.fake_ffmpeg(cmd)
            supervisor.cancel()

        with patch("encode.probe", side_effect=[INFO, OUTPUT]), \
             patch.object(supervisor, "run", side_effect=finish_then_cancel), self.assertRaises(InterruptedError):
            encode.encode_one(self.source, self.target, self.args, self.cfg, "ffmpeg", "ffprobe", supervisor=supervisor)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.target.parent.glob("*.partial.mp4")))
        self.assertEqual(self.source.read_bytes(), b"original TS")


if __name__ == "__main__":
    unittest.main()
