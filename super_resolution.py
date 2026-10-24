"""Real-ESRGAN ncnn Vulkan staging, preserving decoded video timestamps and audio."""
from contextlib import contextmanager
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess
import struct
import tempfile

MODELS = {"realesrgan-x4plus", "realesrgan-x4plus-anime", "realesrnet-x4plus", "realesr-animevideov3"}


def output_size(video, scale):
    try:
        sar = Fraction(video.get("sample_aspect_ratio", "1:1").replace(":", "/"))
        if sar <= 0:
            sar = Fraction(1)
    except (ValueError, ZeroDivisionError):
        sar = Fraction(1)
    return max(2, int(video["width"] * sar) // 2 * 2) * scale, video["height"] * scale


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def configure(args, cfg, executable):
    if not args.super_resolution:
        return None
    if cfg["codec"] not in {"libx265", "hevc_nvenc", "hevc_amf"} or cfg["pix_fmt"] != "yuv420p":
        raise ValueError("--super-resolution requires an 8-bit HEVC profile")
    if args.sr_model != "realesr-animevideov3" and args.sr_scale != 4:
        raise ValueError("This Real-ESRGAN model supports --sr-scale 4 only; animevideov3 supports 2/3/4")
    if args.sr_tile != 0 and args.sr_tile < 32:
        raise ValueError("--sr-tile must be 0 (auto) or at least 32")
    if args.sr_gpu is not None and args.sr_gpu < 0:
        raise ValueError("--sr-gpu must be nonnegative")
    tool = Path(executable(str(args.sr_exe))).resolve()
    models = (args.sr_model_dir or tool.parent / "models").resolve()
    # Native x4 models use a single file pair. AnimeVideo has scale-specific pairs.
    stem = args.sr_model + (f"-x{args.sr_scale}" if args.sr_model == "realesr-animevideov3" else "")
    files = [models / (stem + extension) for extension in (".param", ".bin")]
    if any(not path.is_file() for path in files):
        raise ValueError(f"Real-ESRGAN model files missing: {stem}.param/.bin in {models}")
    return {"exe": str(tool), "exe_sha256": digest(tool), "model_dir": str(models),
            "model": args.sr_model, "scale": args.sr_scale,
            "model_sha256": {p.name: digest(p) for p in files}, "tile": args.sr_tile, "gpu": args.sr_gpu,
            "pipeline_version": 2}


def frame_timeline(frames, fallback_rate):
    """Return original PTS and final frame duration, without assuming CFR."""
    if not frames:
        raise ValueError("No decoded frames for super-resolution")
    try:
        times = [Decimal(frame["best_effort_timestamp_time"]) for frame in frames]
        if any(not t.is_finite() for t in times) or any(b <= a for a, b in zip(times, times[1:])):
            raise ValueError("Non-increasing video timestamps")
        last = Decimal(frames[-1].get("duration_time") or frames[-1].get("pkt_duration_time") or "0")
        if not last.is_finite() or last <= 0:
            last = times[-1] - times[-2] if len(times) > 1 else Decimal(1) / Decimal(str(float(Fraction(fallback_rate))))
        if last <= 0:
            raise ValueError("Invalid last frame duration")
    except (KeyError, ArithmeticError, ValueError, TypeError) as exc:
        raise ValueError(f"Cannot preserve super-resolution frame timestamps: {exc}") from exc
    return times, last


def manifest_text(names, times, last_duration, rate):
    """Paths are generated ASCII basenames, relative to the enhanced directory."""
    lines = ["ffconcat version 1.0"]
    for index, name in enumerate(names):
        duration = times[index + 1] - times[index] if index + 1 < len(times) else last_duration
        lines += [f"file '{name}'", f"option framerate {rate}", f"duration {duration:.9f}"]
    # The sentinel supplies the final duration; -t below excludes this extra frame.
    lines += [f"file '{names[-1]}'", f"option framerate {rate}"]
    return "\n".join(lines) + "\n"


@contextmanager
def prepare(source, args, cfg, video, audios, ffmpeg, ffprobe, options, *,
            runner, probe, status, log, workspace_parent):
    """Yield the generated video manifest plus the original-timing lossless source."""
    parent = args.sr_work_dir or workspace_parent
    Path(parent).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sr-", dir=parent) as temporary:
        root = Path(temporary)
        original, enhanced = root / "frames", root / "enhanced"
        original.mkdir()
        enhanced.mkdir()
        intermediate = root / "decoded.mkv"

        def run(cmd, stage):
            status(f"SUPERRES {stage}", style="progress")
            status(subprocess.list2cmdline(cmd), style="muted")
            log.write(f"\nSTAGE: super-resolution {stage}\n{subprocess.list2cmdline(cmd)}\n")
            log.flush()
            runner(cmd, stdout=log, stderr=subprocess.STDOUT, check=True)

        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-n"]
        if not args.allow_input_errors:
            cmd += ["-xerror"]
        cmd += ["-probesize", "100M", "-analyzeduration", "100M", "-i", str(source)]
        if args.start:
            cmd += ["-ss", str(args.start)]
        if args.duration is not None:
            cmd += ["-t", str(args.duration)]
        for stream in [video] + audios:
            cmd += ["-map", f"0:{stream['index']}"]
        filters = []
        if cfg["deinterlace"] != "off":
            mode = "send_field" if cfg["deinterlace"] == "bob" else "send_frame"
            filters += [f"bwdif=mode={mode}:parity={cfg['parity']}:deint={cfg['deint_frames']}"]
        filters += ["scale=w='max(2,trunc(iw*if(gt(sar,0),sar,1)/2)*2)':h=ih", "setsar=1"]
        cmd += ["-map_metadata", "-1", "-sn", "-dn", "-vf", ",".join(filters),
                "-c:v", "ffv1", "-level", "3", "-pix_fmt", "yuv420p", "-fps_mode", "vfr",
                # Apply source error recovery before inference; damaged AAC must
                # not be copied into the strict final encoding stage.
                "-c:a", "pcm_s24le", "-f", "matroska", str(intermediate)]
        run(cmd, "decode/deinterlace and normalize SAR")
        info = probe(ffprobe, intermediate, runner=runner)
        streams = info["streams"]
        decoded = next(s for s in streams if s.get("codec_type") == "video")
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        if len(audio_streams) != len(audios):
            raise ValueError("Super-resolution intermediate audio stream count mismatch")
        for stream, original_audio in zip(audio_streams, audios):
            stream["tags"] = original_audio.get("tags", {}).copy()
        result = runner([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
                         "-show_entries", "frame=best_effort_timestamp_time,duration_time,pkt_duration_time",
                         "-of", "json", str(intermediate)],
                        capture_output=True, encoding="utf-8", errors="replace", check=True)
        times, last = frame_timeline(json.loads(result.stdout)["frames"], decoded["avg_frame_rate"])
        # PNG uses RGB; explicitly retain the input matrix when returning to YUV.
        matrix = decoded.get("color_space")
        if matrix not in {"bt709", "bt470bg", "smpte170m", "smpte240m", "bt2020nc"}:
            matrix = "bt709" if decoded["height"] >= 720 else "smpte170m"
        run([ffmpeg, "-hide_banner", "-nostdin", "-n", "-xerror", "-i", str(intermediate),
             "-map", "0:v:0", "-vf", f"scale=in_color_matrix={matrix}",
             "-fps_mode", "passthrough", "-pix_fmt", "rgb24", str(original / "%09d.png")], "extract frames")
        names = sorted(p.name for p in original.glob("*.png"))
        if len(names) != len(times):
            raise ValueError(f"Decoded frame count mismatch: {len(names)} PNGs vs {len(times)} timestamps")
        cmd = [options["exe"], "-i", str(original), "-o", str(enhanced),
               "-m", options["model_dir"], "-n", options["model"], "-s", str(options["scale"]),
               "-t", str(options["tile"]), "-f", "png", "-j", "1:1:1"]
        if options["gpu"] is not None:
            cmd += ["-g", str(options["gpu"])]
        run(cmd, f"Real-ESRGAN {options['model']} x{options['scale']} ({len(names)} frames)")
        if sorted(p.name for p in enhanced.glob("*.png")) != names:
            raise ValueError("Real-ESRGAN output frame set mismatch")
        size = (decoded["width"] * options["scale"], decoded["height"] * options["scale"])
        for name in names:
            with (enhanced / name).open("rb") as image:
                header = image.read(24)
            if (len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n"
                    or struct.unpack(">II", header[16:24]) != size):
                raise ValueError(f"Invalid Real-ESRGAN image dimensions: {name}")
        manifest = enhanced / "frames.ffconcat"
        manifest.write_text(manifest_text(names, times, last, decoded["r_frame_rate"]), encoding="utf-8")
        yield {"manifest": manifest, "intermediate": intermediate, "video": decoded,
               "audios": audio_streams, "offset": times[0], "duration": times[-1] + last,
               "frames": len(names), "times": times, "last_duration": last, "matrix": matrix,
               "size": size}


def final_command(command, ffmpeg, target, cfg, prepared, resize):
    cfg = {**cfg, "deinterlace": "off"}
    cmd = command(ffmpeg, prepared["intermediate"], target, cfg, prepared["video"],
                  prepared["audios"], resize=resize)
    cmd[1:1] = ["-copyts"]
    pos = cmd.index("-i")
    # ffconcat starts at zero; retain a nonzero first-video offset relative to audio.
    cmd[pos:pos + 2] = ["-itsoffset", str(prepared["offset"]), "-f", "concat", "-safe", "0",
                        "-i", str(prepared["manifest"]), "-i", str(prepared["intermediate"])]
    for index, value in enumerate(cmd):
        if index and cmd[index - 1] == "-map":
            cmd[index] = "0:0" if value == f"0:{prepared['video']['index']}" else "1:" + value.split(":", 1)[1]
    if "-vf" in cmd:
        index = cmd.index("-vf") + 1
        cmd[index] += f",scale=out_color_matrix={prepared['matrix']}:out_range=tv,setsar=1"
    else:
        cmd[-1:-1] = ["-vf", f"scale=out_color_matrix={prepared['matrix']}:out_range=tv,setsar=1"]
    default_primaries = "bt2020" if prepared["matrix"] == "bt2020nc" else prepared["matrix"]
    primaries = prepared["video"].get("color_primaries", default_primaries)
    transfer = prepared["video"].get("color_transfer", "bt709")
    if primaries == "unknown":
        primaries = default_primaries
    if transfer == "unknown":
        transfer = "bt709"
    cmd[-1:-1] = ["-t", str(prepared["duration"]),
                  "-enc_time_base:v", "1:1000000", "-video_track_timescale", "1000000",
                  "-colorspace", prepared["matrix"], "-color_range", "tv",
                  "-color_primaries", primaries, "-color_trc", transfer]
    return cmd


def verify_timing(ffprobe, target, prepared, runner):
    result = runner([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
                     "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(target)],
                    capture_output=True, encoding="utf-8", errors="replace", check=True)
    frames = json.loads(result.stdout)["frames"]
    if len(frames) != prepared["frames"]:
        raise ValueError(f"Super-resolution frame count changed: {prepared['frames']} -> {len(frames)}")
    actual = [Decimal(frame["best_effort_timestamp_time"]) for frame in frames]
    if any(abs(a - b) > Decimal("0.002") for a, b in zip(actual, prepared["times"])):
        raise ValueError(f"Super-resolution output timestamps changed ({actual[:3]} vs {prepared['times'][:3]}); output will not be published")
