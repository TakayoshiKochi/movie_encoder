"""Verify display aspect ratio and black padding using actual decoded pixels.

Run: py -3 tests/smoke_resize.py --ffmpeg PATH --ffprobe PATH [--nvenc]
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import encode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--nvenc", action="store_true")
    args = parser.parse_args()
    ffmpeg, ffprobe = encode.executable(args.ffmpeg), encode.executable(args.ffprobe)
    root = encode.ROOT / "samples" / datetime.now().strftime("resize-%Y%m%d-%H%M%S-%f")
    inputs, outputs = root / "input", root / "output"
    inputs.mkdir(parents=True)
    cases = [("four-three", "720x480", "8/9", (160, 0, 960, 720)),
             ("anamorphic-hd", "1440x1080", "4/3", (0, 0, 1280, 720)),
             ("wide", "1920x960", "1/1", (0, 40, 1280, 640)),
             ("portrait", "480x720", "1/1", (400, 0, 480, 720))]
    for name, size, sar, rect in cases:
        subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-n", "-f", "lavfi",
                        "-i", f"color=c=white:s={size}:r=60000/1001",
                        "-vf", f"tinterlace=mode=interleave_top,setsar={sar}", "-c:v", "mpeg2video",
                        "-flags", "+ildct+ilme", "-field_order", "tt", "-q:v", "2", "-t", "1",
                        "-f", "mpegts", str(inputs / (name + ".ts"))], check=True)
    cli = ["encode", str(inputs), "--output-dir", str(outputs), "--resize", "1280x720", "--min-age", "0",
           "--ffmpeg", ffmpeg, "--ffprobe", ffprobe, "--verify-full",
           "--profile", "hevc-nvenc" if args.nvenc else "hevc-balanced"]
    assert encode.main(cli + ["--jobs", "2"]) == 0
    report = {"resize": [1280, 720], "results": []}
    timestamps = {}
    for name, size, sar, rect in cases:
        target = outputs / (name + ".ts.mp4")
        info = encode.probe(ffprobe, target)
        encode.validate_output(info, 0, 1, "hevc", "yuv420p", (1280, 720))
        video, _ = encode.select_streams(info)
        assert video["field_order"] == "progressive", video
        raw = subprocess.check_output([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-xerror",
                                       "-i", str(target), "-frames:v", "1", "-pix_fmt", "gray", "-f", "rawvideo", "-"])
        assert len(raw) == 1280 * 720
        left, top, width, height = rect
        content, padding = bytearray(), bytearray()
        for y in range(720):
            row = raw[y * 1280:(y + 1) * 1280]
            if top <= y < top + height:
                content.extend(row[left:left + width])
                padding.extend(row[:left])
                padding.extend(row[left + width:])
            else:
                padding.extend(row)
        assert min(content) >= 200, (name, "content stretched, misplaced or black", min(content))
        assert not padding or max(padding) <= 24, (name, "padding not black", max(padding))
        timestamps[name] = target.stat().st_mtime_ns
        report["results"].append({"source": size, "input_sar": sar, "content_rect": rect,
                                  "output": str(target), "output_sar": video["sample_aspect_ratio"],
                                  "content_min_luma": min(content), "padding_max_luma": max(padding) if padding else None,
                                  "pixel_check": "passed", "full_decode": "passed"})
    assert encode.main(cli + ["--jobs", "1"]) == 0
    assert all((outputs / (name + ".ts.mp4")).stat().st_mtime_ns == stamp for name, stamp in timestamps.items())
    report["resume"] = "passed"
    encode.write_json(root / "report.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
