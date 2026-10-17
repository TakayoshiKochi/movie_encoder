#!/usr/bin/env python3
"""TS -> H.265 (x265/NVENC) or H.264 (x264) / AAC MP4. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent
EXTENSIONS = {".ts", ".m2ts", ".mts"}
PRINT_LOCK = threading.Lock()


def status(message, *, file=None):
    with PRINT_LOCK:
        print(message, file=file or sys.stdout, flush=True)


class ProcessSupervisor:
    """Track this batch's FFmpeg/ffprobe children, including during cancellation."""

    def __init__(self):
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.active = set()

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise InterruptedError("Batch cancelled")

    def run(self, cmd, *, check=False, capture_output=False, **kwargs):
        if capture_output:
            kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Cancellation cannot miss a process between checking and registering it.
        with self.lock:
            self.check_cancelled()
            child = subprocess.Popen(cmd, **kwargs)
            self.active.add(child)
        try:
            stdout, stderr = child.communicate()
            self.check_cancelled()
            result = subprocess.CompletedProcess(cmd, child.returncode, stdout, stderr)
            if check:
                result.check_returncode()
            return result
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
            with self.lock:
                self.active.discard(child)

    def cancel(self):
        with self.lock:
            self.cancelled.set()
            for child in self.active:
                if child.poll() is None:
                    try:
                        child.terminate()
                    except ProcessLookupError:
                        pass  # The child exited between poll and terminate.


def run_jobs(jobs, worker, concurrency, supervisor):
    """Yield results as slots become free; never queue the entire batch at once."""
    if concurrency == 1:
        try:
            for job in jobs:
                yield worker(job)
        except BaseException:
            supervisor.cancel()
            raise
        return
    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="encode")
    pending = set()
    remaining = iter(jobs)
    try:
        for _ in range(concurrency):
            job = next(remaining, None)
            if job is None:
                break
            pending.add(pool.submit(worker, job))
        while pending:
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                yield future.result()
                job = next(remaining, None)
                if job is not None:
                    pending.add(pool.submit(worker, job))
    except BaseException:
        supervisor.cancel()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def executable(name):
    found = shutil.which(name)
    if not found:
        raise ValueError(f"Executable not found: {name}. Install FFmpeg / set --ffmpeg and --ffprobe.")
    return found


def probe(tool, source, *, runner=None):
    result = (runner or subprocess.run)(
        [tool, "-v", "error", "-probesize", "100M", "-analyzeduration", "100M",
         "-show_programs", "-show_streams", "-show_format", "-of", "json", str(source)],
        capture_output=True, encoding="utf-8", errors="replace", check=True,
    )
    return json.loads(result.stdout)


def select_streams(info, program=None, audio="all"):
    programs = [p for p in info.get("programs", [])
                if any(s.get("codec_type") == "video" for s in p.get("streams", []))]
    if program is not None:
        matches = [p for p in programs if int(p["program_id"]) == program]
        if not matches:
            raise ValueError(f"Video program {program} not found; run inspect first.")
        streams = matches[0]["streams"]
    elif len(programs) > 1:
        ids = [p["program_id"] for p in programs]
        raise ValueError(f"Multiple video programs {ids}; select --program explicitly.")
    else:
        streams = programs[0]["streams"] if programs else info.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")]
    if len(videos) != 1:
        raise ValueError(f"Expected one video stream, found {len(videos)}. Inspect/split the TS first.")
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if audio == "first":
        audios = audios[:1]
    return videos[0], audios


def load_profile(path, name, crf=None, cq=None):
    profiles = read_json(path)
    if name not in profiles:
        raise ValueError(f"Unknown profile {name}; choose from {', '.join(profiles)}")
    cfg = profiles[name].copy()
    # Existing H.264 profiles remain readable without the new optional keys.
    cfg.setdefault("codec", "libx264")
    cfg.setdefault("pix_fmt", "yuv420p")
    cfg.setdefault("x265_params", "")
    cfg.setdefault("audio_sample_rate", 48000)
    nvenc = cfg["codec"] == "hevc_nvenc"
    required = {"preset", "tune", "deinterlace", "parity", "deint_frames",
                "audio", "audio_bitrate", "surround_bitrate", "x264_params",
                "codec", "pix_fmt", "x265_params", "audio_sample_rate"}
    required |= {"cq", "nvenc"} if nvenc else {"crf"}
    if set(cfg) != required:
        raise ValueError(f"Profile keys must be: {', '.join(sorted(required))}")
    if nvenc and crf is not None:
        raise ValueError("NVENC uses --cq, not --crf; their quality scales are not equivalent")
    if not nvenc and cq is not None:
        raise ValueError("--cq requires a hevc_nvenc profile; use --crf for x264/x265")
    quality_key = "cq" if nvenc else "crf"
    override = cq if nvenc else crf
    if override is not None:
        cfg[quality_key] = override
    value = cfg[quality_key]
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 51
            or (nvenc and value == 0)):
        raise ValueError(f"{quality_key} must be a number {'greater than 0 and up to 51' if nvenc else 'from 0 to 51'}")
    if cfg["codec"] not in {"libx264", "libx265", "hevc_nvenc"}:
        raise ValueError("codec must be libx264, libx265 or hevc_nvenc")
    if cfg["pix_fmt"] not in ({"yuv420p", "yuv420p10le"} if cfg["codec"] == "libx265" else {"yuv420p"}):
        raise ValueError("Unsupported pixel format for the selected codec")
    presets = ({f"p{i}" for i in range(1, 8)} if nvenc else
               {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"})
    if cfg["preset"] not in presets:
        raise ValueError("Invalid encoder preset")
    tunes = {"hq", "ll", "ull"} if nvenc else {None, "animation", "grain", "fastdecode", "zerolatency", "psnr", "ssim"}
    if cfg["codec"] == "libx264":
        tunes |= {"film", "stillimage"}
    if cfg["tune"] not in tunes:
        raise ValueError(f"Invalid tune for {cfg['codec']}")
    if cfg["audio_sample_rate"] != 48000:
        raise ValueError("audio_sample_rate must be 48000 for the TV workflow")
    for key, allowed in {"deinterlace": {"bob", "frame", "off"}, "parity": {"auto", "tff", "bff"},
                         "deint_frames": {"all", "interlaced"}, "audio": {"all", "first"}}.items():
        if cfg[key] not in allowed:
            raise ValueError(f"Invalid {key}: {cfg[key]}")
    for key in ("audio_bitrate", "surround_bitrate"):
        value = cfg[key]
        if not isinstance(value, str) or not value.endswith("k") or not value[:-1].isdigit() or int(value[:-1]) <= 0:
            raise ValueError(f"{key} must be a positive kbps string, e.g. 192k")
    for key in ("x264_params", "x265_params"):
        if not isinstance(cfg[key], str):
            raise ValueError(f"{key} must be a string")
        active_codec = "libx264" if key == "x264_params" else "libx265"
        if cfg["codec"] != active_codec and cfg[key]:
            raise ValueError(f"{key} must be empty for {cfg['codec']}")
    if nvenc:
        options = cfg["nvenc"]
        if not isinstance(options, dict) or set(options) != {"lookahead", "spatial_aq", "temporal_aq", "multipass", "bframes"}:
            raise ValueError("nvenc requires lookahead, spatial_aq, temporal_aq, multipass and bframes")
        for key, low, high in (("lookahead", 0, 32), ("bframes", 0, 4)):
            if type(options[key]) is not int or not low <= options[key] <= high:
                raise ValueError(f"nvenc.{key} must be an integer from {low} to {high}")
        for key in ("spatial_aq", "temporal_aq"):
            if type(options[key]) is not bool:
                raise ValueError(f"nvenc.{key} must be true or false")
        if options["multipass"] not in {"disabled", "qres", "fullres"}:
            raise ValueError("Invalid nvenc.multipass")
    return cfg


def video_filter(cfg, resize=None):
    filters = []
    if cfg["deinterlace"] != "off":
        mode = "send_field" if cfg["deinterlace"] == "bob" else "send_frame"
        filters.append(f"bwdif=mode={mode}:parity={cfg['parity']}:deint={cfg['deint_frames']}")
    if resize is not None:
        width, height = resize
        # dar includes input SAR: 1440x1080 with SAR 4:3 is 16:9, not 4:3.
        # Round content dimensions down to even pixels for YUV 4:2:0 encoders.
        filters += [
            f"scale=w='max(2,trunc(min({width},{height}*dar)/2)*2)'"
            f":h='max(2,trunc(min({height},{width}/dar)/2)*2)':flags=lanczos",
            "setsar=1",
            f"pad={width}:{height}:x='trunc((ow-iw)/4)*2':y='trunc((oh-ih)/4)*2':color=black",
        ]
    return ",".join(filters)


def command(ffmpeg, source, target, cfg, video, audios, start=0, duration=None,
            allow_input_errors=False, resize=None):
    args = [ffmpeg, "-hide_banner", "-nostdin", "-n", "-loglevel", "info"]
    if not allow_input_errors:
        args += ["-xerror"]
    args += ["-probesize", "100M", "-analyzeduration", "100M"]
    # Output-side seeking is slower but avoids relying on TS seek accuracy for comparisons.
    args += ["-i", str(source)]
    if start:
        args += ["-ss", str(start)]
    if duration is not None:
        args += ["-t", str(duration)]
    args += ["-map", f"0:{video['index']}"]
    for stream in audios:
        args += ["-map", f"0:{stream['index']}"]
    args += ["-map_metadata", "-1", "-map_chapters", "-1", "-sn", "-dn"]
    filters = video_filter(cfg, resize)
    if filters:
        args += ["-vf", filters]
    args += ["-c:v", cfg["codec"], "-preset", cfg["preset"],
             "-pix_fmt", cfg["pix_fmt"], "-fps_mode", "vfr"]
    if cfg["codec"] == "hevc_nvenc":
        options = cfg["nvenc"]
        args += ["-rc", "vbr", "-cq", str(cfg["cq"]), "-b:v", "0",
                 "-rc-lookahead", str(options["lookahead"]),
                 "-spatial-aq", str(int(options["spatial_aq"])),
                 "-temporal-aq", str(int(options["temporal_aq"])),
                 "-multipass", options["multipass"], "-bf", str(options["bframes"])]
    else:
        args += ["-crf", str(cfg["crf"])]
    if cfg["codec"] in {"libx265", "hevc_nvenc"}:
        args += ["-tag:v", "hvc1"]
    if cfg["tune"]:
        args += ["-tune", cfg["tune"]]
    params_key = "x265_params" if cfg["codec"] == "libx265" else "x264_params"
    if cfg[params_key]:
        args += ["-" + params_key.replace("_", "-"), cfg[params_key]]
    if audios:
        args += ["-c:a", "aac", "-ar", str(cfg["audio_sample_rate"])]
        for index, stream in enumerate(audios):
            rate = cfg["surround_bitrate"] if stream.get("channels", 2) > 2 else cfg["audio_bitrate"]
            args += [f"-b:a:{index}", rate]
            for key in ("language", "title"):
                value = stream.get("tags", {}).get(key)
                if value:
                    args += [f"-metadata:s:a:{index}", f"{key}={value}"]
        args += ["-disposition:a", "0", "-disposition:a:0", "default"]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", "-f", "mp4", str(target)]
    return args


def fingerprint(source):
    stat = source.stat()
    return {"path": str(source.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def validate_output(info, audio_count, expected_duration=None, video_codec="h264", pixel_format="yuv420p", resize=None):
    streams = info.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if len(videos) != 1 or videos[0].get("codec_name") != video_codec:
        raise ValueError(f"Output does not contain exactly one {video_codec} video stream")
    if videos[0].get("pix_fmt") != pixel_format:
        raise ValueError(f"Output pixel format is not {pixel_format}")
    if video_codec == "hevc" and videos[0].get("codec_tag_string") != "hvc1":
        raise ValueError("Output HEVC sample entry is not hvc1")
    if resize is not None:
        if (videos[0].get("width"), videos[0].get("height")) != tuple(resize):
            raise ValueError(f"Output size does not match resize {resize[0]}x{resize[1]}")
        if videos[0].get("sample_aspect_ratio") != "1:1":
            raise ValueError("Resized output must have square pixels (SAR 1:1)")
    if len(audios) != audio_count or any(s.get("codec_name") != "aac" for s in audios):
        raise ValueError("Output AAC stream count mismatch")
    if any(str(s.get("sample_rate")) != "48000" for s in audios):
        raise ValueError("Output audio sample rate is not 48000 Hz")
    seconds = float(info.get("format", {}).get("duration", 0))
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Output duration is invalid")
    if expected_duration is not None and abs(seconds - expected_duration) > max(3.0, expected_duration * 0.01):
        raise ValueError(f"Duration mismatch: expected ~{expected_duration:.2f}s, got {seconds:.2f}s")


def expected_duration(info, video, start, duration):
    # Use selected video duration, not the duration of an unrelated TS service.
    raw = video.get("duration")
    if raw is None:
        raw = info.get("format", {}).get("duration") if len(info.get("programs", [])) <= 1 else None
    try:
        available = float(raw) - start
    except (ValueError, TypeError):
        return None  # Probe metadata cannot always supply reliable TS duration.
    if not math.isfinite(available) or available <= 0:
        raise ValueError("Start is beyond the end of the selected video")
    return min(available, duration) if duration is not None else available


@contextmanager
def output_lock(directory):
    path = directory / ".encode.lock"
    # OS lock releases after a crash; keep the file to avoid unlink/reopen races.
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(f"Output directory is busy: {directory}") from exc
        yield
    finally:
        handle.close()


def publish(partial, target):
    # Both paths are in the same directory. Never replace an existing final file.
    if os.name == "nt":
        os.rename(partial, target)
    else:
        os.link(partial, target)
        partial.unlink()


def encode_one(source, target, args, cfg, ffmpeg, ffprobe, *, supervisor=None):
    runner = supervisor.run if supervisor else subprocess.run
    if supervisor:
        supervisor.check_cancelled()
    before = fingerprint(source)
    info = probe(ffprobe, source, runner=runner)
    video, audios = select_streams(info, args.program, cfg["audio"])
    output_codec = "hevc" if cfg["codec"] in {"libx265", "hevc_nvenc"} else "h264"
    output_duration = expected_duration(info, video, args.start, args.duration)
    request = {"source": before, "profile": cfg, "program": args.program,
               "start": args.start, "duration": args.duration, "verify_full": args.verify_full}
    # Omit the default flag so completed jobs from earlier versions still match.
    if args.allow_input_errors:
        request["allow_input_errors"] = True
    if args.resize is not None:
        request["resize"] = list(args.resize)
    sidecar = target.with_suffix(".mp4.json")
    if target.exists():
        if not sidecar.exists() or read_json(sidecar).get("request") != request:
            raise ValueError(f"Existing output has no matching job record; choose another output directory: {target}")
        validate_output(probe(ffprobe, target, runner=runner), len(audios), output_duration, output_codec, cfg["pix_fmt"], args.resize)
        status(f"SKIP {target}")
        return "skipped"
    partial = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.partial.mp4")
    cmd = command(ffmpeg, source, partial, cfg, video, audios, args.start, args.duration,
                  allow_input_errors=args.allow_input_errors, resize=args.resize)
    if args.allow_input_errors:
        status(f"WARNING {source}: Input error recovery enabled; inspect the log and picture/audio near damaged sections.",
               file=sys.stderr)
    status(subprocess.list2cmdline(cmd))
    if args.dry_run:
        return "planned"
    target.parent.mkdir(parents=True, exist_ok=True)
    log = target.with_name(target.name + f".{uuid.uuid4().hex[:8]}.log")
    started = time.monotonic()
    stage = "encode input"
    try:
        with log.open("w", encoding="utf-8") as handle:
            handle.write(f"STAGE: {stage}\n" + subprocess.list2cmdline(cmd) + "\n")
            handle.flush()
            runner(cmd, stdout=handle, stderr=subprocess.STDOUT, check=True)
            encode_seconds = round(time.monotonic() - started, 3)
            stage = "probe and validate output"
            handle.write(f"\nSTAGE: {stage}\n")
            handle.flush()
            output = probe(ffprobe, partial, runner=runner)
            validate_output(output, len(audios), output_duration, output_codec, cfg["pix_fmt"], args.resize)
            if args.verify_full:
                stage = "verify-full (decode output MP4)"
                handle.write(f"\nSTAGE: {stage}\n")
                handle.flush()
                runner([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-xerror",
                                "-i", str(partial), "-map", "0:v", "-map", "0:a?",
                                "-f", "null", "-"], stdout=handle, stderr=subprocess.STDOUT, check=True)
        stage = "check source and publish output"
        if supervisor:
            supervisor.check_cancelled()
        if fingerprint(source) != before:
            raise ValueError("Source changed during encoding; output will not be published")
        record = {"request": request, "command": cmd, "output_probe": output,
                  "encode_seconds": encode_seconds,
                  "elapsed_seconds": round(time.monotonic() - started, 2),
                  "completed_at": datetime.now(timezone.utc).isoformat(), "log": str(log)}
        # Write the record before publishing; an interrupted job cannot be mistaken for a complete MP4.
        write_json(sidecar, record)
        publish(partial, target)
        status(f"DONE {target} ({record['elapsed_seconds']}s)")
        return "encoded"
    except BaseException:
        status(f"Job failed during {stage}. Log: {log}", file=sys.stderr)
        raise
    finally:
        partial.unlink(missing_ok=True)


def finite_nonnegative(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return number


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be an integer of at least 1")
    return number


def resize_size(value):
    match = re.fullmatch(r"([0-9]+)[xX]([0-9]+)", value)
    if not match:
        raise argparse.ArgumentTypeError("resize must be WIDTHxHEIGHT, e.g. 1280x720")
    size = tuple(int(part) for part in match.groups())
    if any(dimension < 2 or dimension % 2 for dimension in size):
        raise argparse.ArgumentTypeError("resize width and height must be even integers of at least 2")
    return size


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "encode"])
    parser.add_argument("input", type=Path, help="TS file, or directory for encode")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--config", type=Path, default=ROOT / "profiles.json")
    parser.add_argument("--profile", default="hevc-nvenc")
    parser.add_argument("--crf", type=float)
    parser.add_argument("--cq", type=float, help="NVENC target quality (greater than 0, up to 51); not equivalent to CRF")
    parser.add_argument("--program", type=int, help="TS service/program ID, as shown by inspect")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--resize", type=resize_size, metavar="WIDTHxHEIGHT",
                        help="fit after deinterlacing, preserving display aspect ratio with black padding and square pixels")
    parser.add_argument("--jobs", type=positive_int, default=1,
                        help="maximum simultaneous files, including probing and output verification (default: 1)")
    parser.add_argument("--min-age", type=finite_nonnegative, default=300,
                        help="skip directory entries modified more recently than this many seconds")
    parser.add_argument("--start", type=finite_nonnegative, default=0)
    parser.add_argument("--duration", type=finite_nonnegative)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-full", action="store_true")
    parser.add_argument("--allow-input-errors", action="store_true",
                        help="allow FFmpeg's normal input error recovery; output validation remains strict")
    args = parser.parse_args(argv)
    if args.duration == 0:
        parser.error("--duration must be greater than zero")
    source = args.input.resolve()
    ffprobe = executable(args.ffprobe)
    if args.action == "inspect":
        if not source.is_file():
            raise ValueError("inspect requires a file")
        print(json.dumps(probe(ffprobe, source), ensure_ascii=False, indent=2))
        return 0
    ffmpeg = executable(args.ffmpeg)
    cfg = load_profile(args.config, args.profile, args.crf, args.cq)
    if source.is_file():
        files = [source]
    elif source.is_dir():
        files = sorted(p for p in (source.rglob("*") if args.recursive else source.iterdir())
                       if p.is_file() and p.suffix.lower() in EXTENSIONS)
    else:
        raise ValueError(f"Input does not exist: {source}")
    destination = args.output_dir.resolve()
    # Retain the input extension in the name: foo.ts and foo.m2ts cannot collide.
    jobs = [(p, destination / (p.relative_to(source) if source.is_dir() else Path(p.name)).with_suffix(p.suffix + ".mp4"))
            for p in files]
    if not args.dry_run:
        destination.mkdir(parents=True, exist_ok=True)
    counts = {"encoded": 0, "skipped": 0, "planned": 0, "pending": 0, "failed": 0}

    supervisor = ProcessSupervisor()

    def worker(job):
        path, target = job
        try:
            supervisor.check_cancelled()
            if source.is_dir() and time.time() - path.stat().st_mtime < args.min_age:
                status(f"PENDING (recently modified) {path}")
                return "pending"
            status(f"INPUT {path}")
            return encode_one(path, target, args, cfg, ffmpeg, ffprobe, supervisor=supervisor)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            status(f"FAILED {path}: {exc}", file=sys.stderr)
            return "failed"

    def process():
        for result in run_jobs(jobs, worker, args.jobs, supervisor):
            counts[result] += 1
        print(json.dumps(counts), flush=True)
        return 1 if counts["failed"] else 0

    if args.dry_run:
        return process()
    with output_lock(destination):
        return process()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted; source TS files were kept.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
