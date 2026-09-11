from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from . import http as requests
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT, FPS = 1080, 1920, 30
ACCENT = (199, 255, 85)


def ffmpeg(args):
    try:
        import imageio_ffmpeg
        binary = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        binary = shutil.which("ffmpeg")
    if not binary:
        raise RuntimeError("Install imageio-ffmpeg or FFmpeg")
    result = subprocess.run([binary, "-hide_banner", "-loglevel", "error",
                             "-y", "-threads", "2", *map(str, args)], capture_output=True, timeout=300)
    if result.returncode:
        raise RuntimeError("FFmpeg render failed: " + result.stderr.decode(errors="replace")[-1200:])


def font(size, bold=True):
    override = os.getenv("STUDIO_FONT", "")
    filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for path in [override, "/usr/share/fonts/truetype/dejavu/" + filename,
                 "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                 "/System/Library/Fonts/Supplemental/Arial.ttf"]:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size)
    raise RuntimeError("Install fonts-dejavu-core or set STUDIO_FONT to a Cyrillic TTF font")


def wrap(draw, text, face, max_width):
    lines, line = [], ""
    for word in text.split():
        chunks = [word]
        if draw.textlength(word, font=face) > max_width:
            chunks, part = [], ""
            for ch in word:
                if draw.textlength(part + ch, font=face) > max_width:
                    chunks.append(part)
                    part = ""
                part += ch
            if part:
                chunks.append(part)
        for piece in chunks:
            candidate = (line + " " + piece).strip()
            if line and draw.textlength(candidate, font=face) > max_width:
                lines.append(line)
                line = piece
            else:
                line = candidate
    if line:
        lines.append(line)
    return lines


def text_block(draw, text, box, size, fill=(248, 250, 253), max_lines=5):
    x, y, width, height = box
    for pts in range(size, 25, -2):
        face = font(pts)
        lines = wrap(draw, text, face, width)
        line_h = int(pts * 1.22)
        if len(lines) <= max_lines and len(lines) * line_h <= height:
            break
    else:
        raise ValueError("Text cannot fit safely in video frame")
    for line in lines:
        draw.text((x, y), line, font=face, fill=fill, stroke_width=0)
        y += line_h


def background(path: Path, category: str, variant=0):
    im = Image.new("RGB", (WIDTH, HEIGHT), (12, 19, 34))
    d = ImageDraw.Draw(im)
    for y in range(HEIGHT):
        t = y / HEIGHT
        d.line((0, y, WIDTH, y), fill=(int(11 + 12*t), int(19 + 10*t), int(34 + 15*t)))
    for x in range(variant % 45, WIDTH, 90):
        d.line((x, 0, x, HEIGHT), fill=(28, 38, 51), width=1)
    for y in range(0, HEIGHT, 90):
        d.line((0, y, WIDTH, y), fill=(28, 38, 51), width=1)
    cx, cy = 535, 770
    if category == "science":
        for k in range(3):
            r = 210 + k * 72
            d.ellipse((cx-r, cy-r//2, cx+r, cy+r//2), outline=(79, 107, 120), width=3)
        d.ellipse((cx-165, cy-165, cx+165, cy+165), fill=(74, 89, 221), outline=ACCENT, width=5)
        for k in range(65):
            x, y = (k*157+variant*41) % 1000, (k*113) % 1000+260
            d.ellipse((x, y, x+4, y+4), fill=(148, 168, 189))
    elif category == "games":
        d.rounded_rectangle((205, 580, 870, 990), 120, fill=(51, 65, 92), outline=ACCENT, width=5)
        d.rounded_rectangle((320, 665, 365, 850), 8, fill=(232, 238, 246))
        d.rounded_rectangle((252, 733, 433, 780), 8, fill=(232, 238, 246))
        for x, y, color in [(738, 668, ACCENT), (804, 734, (231, 101, 133)), (738, 800, (105, 186, 255))]:
            d.ellipse((x-25, y-25, x+25, y+25), fill=color)
    elif category in ("tech", "internet"):
        d.rounded_rectangle((310, 340, 765, 1120), 68, fill=(40, 51, 71), outline=(156, 174, 199), width=7)
        d.rounded_rectangle((338, 367, 737, 1090), 44, fill=(17, 25, 42))
        d.rounded_rectangle((465, 385, 610, 415), 14, fill=(6, 10, 20))
        for k in range(4):
            d.rounded_rectangle((377, 470+k*140, 696, 570+k*140), 20,
                                fill=(48+k*5, 60+k*7, 84+k*9))
            d.ellipse((395, 490+k*140, 450, 545+k*140), fill=ACCENT)
            d.line((480, 510+k*140, 665, 510+k*140), fill=(215, 229, 248), width=9)
    else:
        for k in range(8):
            a = k * math.tau / 8
            x, y = cx+math.cos(a)*280, cy+math.sin(a)*280
            d.line((cx, cy, x, y), fill=(76, 96, 124), width=5)
            d.ellipse((x-30, y-30, x+30, y+30), fill=ACCENT if k%2 else (91, 122, 246))
        d.rounded_rectangle((385, 615, 685, 915), 55, fill=(62, 78, 105), outline=ACCENT, width=5)
        d.text((430, 683), "AI" if category == "ai" else "NEWS", font=font(110 if category=="ai" else 60), fill=ACCENT)
    im.save(path)


def overlay(path, plan, index, total):
    im = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((82, 180, 875, 250), 20, fill=(12, 19, 34, 215))
    d.ellipse((107, 205, 125, 223), fill=ACCENT)
    text_block(d, plan["label"], (148, 192, 655, 50), 29, ACCENT, 1)
    d.text((90, 1150), f"{index+1:02d} / {total:02d}", font=font(30), fill=ACCENT)
    d.line((90, 1210, 900, 1210), fill=(80, 95, 115, 180), width=3)
    d.line((90, 1210, 90+810*(index+1)/total, 1210), fill=ACCENT, width=6)
    d.rounded_rectangle((65, 1250, 930, 1605), 30, fill=(8, 14, 26, 235))
    im.save(path)


def subtitle_image(path, text):
    im = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    text_block(ImageDraw.Draw(im), text, (95, 1290, 800, 285), 78, max_lines=3)
    im.save(path)


async def neural_voice(text, output):
    import edge_tts
    voice = os.getenv("STUDIO_VOICE", "ru-RU-DmitryNeural")
    timings = []
    communicate = edge_tts.Communicate(text, voice, rate="+3%", pitch="-2Hz", volume="+0%", boundary="WordBoundary")
    with output.open("wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                timings.append({"text": chunk["text"], "start": chunk["offset"]/10_000_000,
                                "end": (chunk["offset"]+chunk["duration"])/10_000_000})
    return timings


def voice(text, folder, demo=False):
    audio = folder / "voice.mp3"
    quality = "neural"
    if demo:
        audio = folder / "voice.wav"
        duration = max(3.0, len(text.split()) / 2.5)
        if shutil.which("espeak-ng"):
            subprocess.run(["espeak-ng", "-v", "ru", "-s", "162", "-w", str(audio), text], check=True)
        elif shutil.which("espeak"):
            subprocess.run(["espeak", "-v", "ru", "-s", "158", "-p", "42", "-w", str(audio), text], check=True)
        else:
            ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", duration, audio])
        timings, quality = [], "offline-demo"
    else:
        timings = asyncio.run(asyncio.wait_for(neural_voice(text, audio), timeout=90))
    if audio.suffix == ".wav":
        import wave
        with wave.open(str(audio)) as f:
            duration = f.getnframes() / f.getframerate() + 0.15
    else:
        from mutagen import File as AudioFile
        media = AudioFile(str(audio))
        if not media or not media.info.length:
            raise RuntimeError("TTS returned invalid audio")
        duration = float(media.info.length) + 0.15
    if not timings:
        words = text.split()
        timings = [{"text": w, "start": i*duration/len(words), "end": (i+1)*duration/len(words)}
                   for i, w in enumerate(words)]
    groups = []
    for i in range(0, len(timings), 5):
        chunk = timings[i:i+5]
        groups.append({"text": " ".join(w["text"] for w in chunk),
                       "start": 0 if i == 0 else chunk[0]["start"],
                       "end": timings[i+5]["start"] if i+5 < len(timings) else duration})
    return audio, duration, groups, quality


def footage(query, output, used_ids):
    key = os.getenv("PEXELS_API_KEY", "")
    if not key:
        return None
    r = requests.get("https://api.pexels.com/videos/search", params={"query": query,
                     "orientation": "portrait", "per_page": 8}, headers={"Authorization": key}, timeout=30)
    if not r.ok:
        return None
    for video in r.json().get("videos", []):
        if video["id"] in used_ids:
            continue
        choices = [x for x in video.get("video_files", []) if x.get("file_type") == "video/mp4"
                   and 540 <= (x.get("width") or 0) <= 1080]
        if not choices:
            continue
        selected = max(choices, key=lambda x: x["width"])
        url = selected["link"]
        host = urlparse(url).hostname or ""
        if not (host.endswith(".pexels.com") and url.startswith("https://")):
            continue
        with requests.get(url, stream=True, timeout=90, allow_redirects=False) as download:
            if download.status_code != 200:
                continue
            size = 0
            with output.open("wb") as f:
                for chunk in download.iter_content(65536):
                    size += len(chunk)
                    if size > 60_000_000:
                        output.unlink(missing_ok=True)
                        return None
                    f.write(chunk)
        used_ids.add(video["id"])
        return {"path": str(output), "id": video["id"], "source": video["url"],
                "author": video.get("user", {}).get("name", ""), "query": query,
                "license": "https://www.pexels.com/license/", "type": "illustrative-stock"}
    return None


def render(plan, folder: Path, variant=0, demo=False):
    folder.mkdir(parents=True, exist_ok=True)
    clips, assets, elapsed, used, subtitle_rows = [], [], 0.0, set(), []
    qualities = []
    for i, scene in enumerate(plan["scenes"]):
        work = folder / f"scene-{i}"
        work.mkdir(exist_ok=True)
        audio, duration, groups, quality = voice(scene["text"], work, demo)
        qualities.append(quality)
        stock = None
        if not demo:
            try:
                stock = footage(scene["query"], work / "stock.mp4", used)
            except requests.RequestException:
                pass
        bg = work / "background.png"
        ui = work / "overlay.png"
        background(bg, plan["category"], variant+i)
        overlay(ui, plan, i, len(plan["scenes"]))
        args = []
        if stock:
            args += ["-stream_loop", "-1", "-i", stock["path"]]
            assets.append(stock)
            base_filter = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30,eq=brightness=-0.12:saturation=0.9"
        else:
            args += ["-loop", "1", "-framerate", FPS, "-i", bg]
            assets.append({"type": "original-static-graphic", "query": scene["query"]})
            # No zoompan here: the old sub-pixel zoom caused visible micro-jitter on phones.
            base_filter = "scale=1080:1920,setsar=1,fps=30"
        args += ["-loop", "1", "-i", ui]
        for j, group in enumerate(groups):
            sub = work / f"sub-{j}.png"
            subtitle_image(sub, group["text"])
            args += ["-loop", "1", "-i", sub]
            subtitle_rows.append({"text": group["text"], "start": elapsed+group["start"], "end": elapsed+group["end"]})
        audio_index = 2 + len(groups)
        args += ["-i", audio]
        filters = [f"[0:v]{base_filter}[bg]", "[bg][1:v]overlay=0:0[v0]"]
        for j, group in enumerate(groups):
            filters.append(f"[v{j}][{j+2}:v]overlay=0:0:enable='between(t,{group['start']:.3f},{group['end']:.3f})'[v{j+1}]")
        output = work / "scene.mp4"
        final_v = f"[v{len(groups)}]"
        filters.append(f"{final_v}fade=t=in:st=0:d=0.16,fade=t=out:st={max(duration-0.18,0):.3f}:d=0.18[vout]")
        args += ["-filter_complex_threads", "1", "-filter_complex", ";".join(filters),
                 "-map", "[vout]", "-map", f"{audio_index}:a", "-t", f"{duration:.3f}",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                 "-maxrate", "2600k", "-bufsize", "5200k", "-threads", "2",
                 "-c:a", "aac", "-ar", "48000", "-b:a", "128k",
                 "-af", "highpass=f=70,lowpass=f=14500,acompressor=threshold=-18dB:ratio=2.2:attack=15:release=180,alimiter=limit=0.92,apad",
                 output]
        ffmpeg(args)
        clips.append(output)
        elapsed += duration
    if not demo and not 12 <= elapsed <= 45:
        raise ValueError(f"Video length {elapsed:.1f}s outside 12–45s; shorten source sentences")
    concat = folder / "concat.txt"
    concat.write_text("\n".join("file '" + str(p.resolve()).replace("'", "'\\''") + "'" for p in clips))
    out = folder / "short.mp4"
    ffmpeg(["-f", "concat", "-safe", "0", "-i", concat, "-c", "copy", "-movflags", "+faststart", out])
    if out.stat().st_size > 19_000_000:
        raise ValueError("Video exceeds safe Telegram re-download budget (19 MB)")
    ffmpeg(["-i", out, "-frames:v", "1", folder / "cover.jpg"])
    manifest = {"duration": elapsed, "resolution": "1080x1920", "fps": 30,
                "audio": qualities, "assets": assets, "captions": subtitle_rows, "demo": demo}
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return out, manifest
