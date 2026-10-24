"""Actual Real-ESRGAN/Vulkan + FFmpeg test. Requires bundled models and HEVC GPUs.

Run: py -3 tests/smoke_super_resolution.py
Artifacts and logs remain in samples/superres-*; temporary PNGs must be removed.
"""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import encode


def main():
    ffmpeg, ffprobe = encode.executable("ffmpeg"), encode.executable("ffprobe")
    root = encode.ROOT / "samples" / datetime.now().strftime("superres-%Y%m%d-%H%M%S")
    root.mkdir()
    report = {"directory": str(root), "results": []}
    cases = [
        ("sd-4k", "720x480", "8/9", "hevc-nvenc", "realesr-animevideov3", 2, False, (3840, 2160)),
        ("general-amf", "128x96", "1/1", "hevc-amf", "realesrgan-x4plus", 4, False, None),
        ("vfr-bob", "128x96", "1/1", "hevc-bob", "realesr-animevideov3", 2, True, None),
    ]
    for name, size, sar, profile, model, scale, vfr, resize in cases:
        source = root / (name + ".ts")
        filters = "tinterlace=mode=interleave_top"
        if vfr:
            filters += ",select='not(eq(mod(n,3),1))'"
        filters += f",setsar={sar}"
        subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-n",
                        "-f", "lavfi", "-i", f"testsrc2=s={size}:r=60000/1001",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
                        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-vf", filters,
                        "-c:v", "mpeg2video", "-flags", "+ildct+ilme", "-field_order", "tt",
                        "-fps_mode", "vfr", "-q:v", "3", "-c:a", "aac", "-ac", "2",
                        "-metadata:s:a:0", "language=jpn", "-metadata:s:a:1", "language=eng",
                        "-t", "0.7", "-f", "mpegts", str(source)], check=True)
        cli = ["encode", str(source), "--profile", profile, "--output-dir", str(root / name),
               "--super-resolution", "--sr-model", model, "--sr-scale", str(scale), "--sr-gpu", "0",
               "--start", "0.1", "--duration", "0.4", "--verify-full", "--keep-awake"]
        if resize:
            cli += ["--resize", f"{resize[0]}x{resize[1]}"]
        assert encode.main(cli) == 0, name
        target = root / name / (name + ".ts.mp4")
        info = encode.probe(ffprobe, target)
        video, audios = encode.select_streams(info)
        assert video["field_order"] == "progressive", video
        assert [s["tags"].get("language") for s in audios] == ["jpn", "eng"], audios
        record = encode.read_json(target.with_suffix(".mp4.json"))
        assert record["super_resolution"]["timing_verified"]
        pixels = subprocess.check_output([ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
                                          "-i", str(target), "-frames:v", "1", "-vf", "scale=64:48",
                                          "-pix_fmt", "gray", "-f", "rawvideo", "-"])
        assert len(pixels) == 64 * 48 and max(pixels) - min(pixels) > 20, "black/flat model output"
        assert not list((root / name).glob(".sr-*")), "PNG workspace leaked"
        stamp = target.stat().st_mtime_ns
        assert encode.main(cli) == 0
        assert target.stat().st_mtime_ns == stamp
        if vfr:
            frames = json.loads(subprocess.check_output([
                ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
                "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(target)]))["frames"]
            times = [float(f["best_effort_timestamp_time"]) for f in frames]
            gaps = {round(b - a, 3) for a, b in zip(times, times[1:])}
            assert len(gaps) > 1, gaps
        report["results"].append({"case": name, "profile": profile, "model": model, "scale": scale,
                                  "width": video["width"], "height": video["height"],
                                  "sar": video["sample_aspect_ratio"],
                                  "frames": record["super_resolution"]["frames"],
                                  "elapsed_seconds": record["elapsed_seconds"],
                                  "full_decode": "passed", "timing": "passed", "resume": "passed",
                                  "audio_languages": [s["tags"]["language"] for s in audios]})
    encode.write_json(root / "report.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
