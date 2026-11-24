"""Merge docs/raw/demo.webm with per-scene voice clips (docs/voice/sN.mp3) into
docs/raw/adforge-demo.mp4, and cut docs/demo.gif for the README."""
import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW, VOICE = ROOT / "docs" / "raw", ROOT / "docs" / "voice"
SEG = RAW / "segs"
SEG.mkdir(exist_ok=True)


def dur(p):
    return float(subprocess.check_output(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)]).strip())


scenes = json.loads((RAW / "scenes.json").read_text())
names = [f"s{i}" for i in range(1, 13)]
bounds = [scenes[n] for n in names] + [scenes["end"]]
parts, offsets, t = [], {}, 0.0
for i, n in enumerate(names):
    start, end = bounds[i], bounds[i + 1]
    v = end - start
    a = dur(VOICE / f"{n}.mp3")
    target = a + 0.7
    if v > target + 2.5:            # long wait for the model: speed it up, keep a little breathing room
        target = max(target + 1.5, v / 4)
    speed = max(1.0, v / target)
    pad = max(0.0, target - v / speed)
    out = SEG / f"{n}.mp4"
    vf = f"setpts=PTS/{speed:.4f},tpad=stop_mode=clone:stop_duration={pad + 0.5:.2f},fps=30,format=yuv420p"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", str(RAW / "demo.webm"),
                    "-i", str(VOICE / f"{n}.mp3"), "-filter_complex",
                    f"[0:v]{vf}[v];[1:a]adelay=300|300,apad,atrim=0:{target:.2f},aresample=44100[a]",
                    "-map", "[v]", "-map", "[a]", "-t", f"{target:.2f}", "-c:v", "libx264", "-crf", "21",
                    "-preset", "veryfast", "-c:a", "aac", "-b:a", "160k", str(out)], check=True)
    offsets[n] = t
    t += target
    parts.append(out)
    print(f"{n}: video {v:5.1f}s voice {a:4.1f}s -> {target:5.1f}s (x{speed:.2f})")

(SEG / "list.txt").write_text("".join(f"file '{p}'\n" for p in parts))
final = RAW / "adforge-demo.mp4"
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(SEG / "list.txt"),
                "-c", "copy", "-movflags", "+faststart", str(final)], check=True)
print("video", final, f"{dur(final):.1f}s")

# README GIF: the ad-variant scene (s3), 12 s, 900px, 10 fps
g0 = offsets["s3"]
gif = ROOT / "docs" / "demo.gif"
subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{g0:.2f}", "-t", "14", "-i", str(final), "-vf",
                "fps=10,scale=900:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer",
                str(gif)], check=True)
print("gif", gif, f"{gif.stat().st_size / 1e6:.1f} MB")
