"""Generate an interlaced MPEG-2 TS and exercise the real FFmpeg pipeline.

Run: py -3 tests/smoke_ffmpeg.py --ffmpeg PATH --ffprobe PATH
Artifacts are retained under samples/smoke-* for inspection.
"""
import argparse
from datetime import datetime
from fractions import Fraction
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
    args = parser.parse_args()
    ffmpeg = encode.executable(args.ffmpeg)
    ffprobe = encode.executable(args.ffprobe)
    root = encode.ROOT / "samples" / datetime.now().strftime("smoke-%Y%m%d-%H%M%S-%f")
    root.mkdir(parents=True)
    source = root / "日本語 & sample.ts"
    subprocess.run([
        ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-n",
        "-f", "lavfi", "-i", "testsrc2=size=720x480:rate=60000/1001",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
        "-map", "0:v", "-map", "1:a", "-map", "2:a",
        "-vf", "tinterlace=mode=interleave_top,setsar=8/9",
        "-c:v", "mpeg2video", "-flags", "+ildct+ilme", "-field_order", "tt", "-q:v", "3",
        "-c:a", "aac", "-ac", "2", "-ar", "48000", "-b:a", "192k",
        "-metadata:s:a:0", "language=jpn", "-metadata:s:a:1", "language=eng",
        "-t", "6", "-f", "mpegts", str(source),
    ], check=True)
    input_info = encode.probe(ffprobe, source)
    input_video, input_audios = encode.select_streams(input_info)
    assert input_video["codec_name"] == "mpeg2video", input_video
    assert input_video["field_order"] in {"tt", "tb"}, input_video
    assert len(input_audios) == 2, input_audios
    report = {"ffmpeg": subprocess.check_output([ffmpeg, "-version"], text=True).splitlines()[0],
              "input": str(source), "results": []}
    for profile, codec, rate in [("hevc-balanced", "hevc", Fraction(30000, 1001)),
                                 ("hevc-bob", "hevc", Fraction(60000, 1001)),
                                 ("tv-balanced", "h264", Fraction(60000, 1001))]:
        output_dir = root / profile
        cli = ["encode", str(source), "--profile", profile, "--output-dir", str(output_dir),
               "--ffmpeg", ffmpeg, "--ffprobe", ffprobe, "--verify-full"]
        assert encode.main(cli) == 0, profile
        target = output_dir / (source.name + ".mp4")
        info = encode.probe(ffprobe, target)
        video, audios = encode.select_streams(info)
        encode.validate_output(info, 2, 6, codec)
        assert abs(Fraction(video["avg_frame_rate"]) - rate) < Fraction(1, 100), video
        assert video["field_order"] == "progressive", video
        assert video["sample_aspect_ratio"] == "8:9", video
        assert [s.get("tags", {}).get("language") for s in audios] == ["jpn", "eng"], audios
        assert all(s["channels"] == 2 for s in audios), audios
        before = target.stat().st_mtime_ns
        assert encode.main(cli) == 0, "resume"
        assert target.stat().st_mtime_ns == before, "resume unexpectedly rewrote the MP4"
        report["results"].append({"profile": profile, "file": str(target),
                                  "video_codec": video["codec_name"], "pix_fmt": video["pix_fmt"],
                                  "fps": video["avg_frame_rate"], "field_order": video["field_order"],
                                  "sar": video["sample_aspect_ratio"], "audio_tracks": len(audios),
                                  "duration": info["format"]["duration"], "full_decode": "passed",
                                  "resume": "passed"})
    # Damage only a copy: preserve TS/PES headers and timestamps, corrupt three
    # video payload packets halfway through the clip to exercise error concealment.
    damaged = bytearray(source.read_bytes())
    video_pid = int(input_video["id"], 0)
    changed = 0
    for offset in range((len(damaged) // 376) * 188, len(damaged) - 187, 188):
        packet = damaged[offset:offset + 188]
        pid = ((packet[1] & 0x1f) << 8) | packet[2]
        control = (packet[3] >> 4) & 3
        if pid != video_pid or packet[1] & 0x40 or control not in (1, 3):
            continue
        payload = 4 if control == 1 else 5 + packet[4]
        if payload >= 188:
            continue
        damaged[offset + payload:offset + 188] = b"\x55" * (188 - payload)
        changed += 1
        if changed == 3:
            break
    assert changed == 3, "not enough video packets to damage"
    damaged_source = root / "damaged.ts"
    damaged_source.write_bytes(damaged)
    base = ["encode", str(damaged_source), "--ffmpeg", ffmpeg, "--ffprobe", ffprobe, "--verify-full"]
    strict_dir = root / "damaged-strict"
    assert encode.main(base + ["--output-dir", str(strict_dir)]) == 1, "strict mode accepted damaged input"
    assert not (strict_dir / "damaged.ts.mp4").exists()
    recovered_dir = root / "damaged-recovered"
    assert encode.main(base + ["--output-dir", str(recovered_dir), "--allow-input-errors"]) == 0
    recovered = recovered_dir / "damaged.ts.mp4"
    encode.validate_output(encode.probe(ffprobe, recovered), 2, 6, "hevc")
    assert encode.read_json(recovered.with_suffix(".mp4.json"))["request"]["allow_input_errors"] is True
    report["damaged_input"] = {"damaged_video_packets": changed, "strict": "rejected",
                               "allow_input_errors": "passed", "full_decode": "passed",
                               "recovered_file": str(recovered)}
    encode.write_json(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
