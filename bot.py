#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Matt
"""matrix-link-bot: re-posts Instagram and YouTube videos as inline Matrix videos."""
import asyncio, json, logging, os, re, shutil, subprocess, tempfile
from pathlib import Path
import yt_dlp
from nio import (AsyncClient, InviteMemberEvent, LoginResponse, ReactionEvent,
                 RoomMessageText, UploadResponse)

HS, USER, PW = os.environ["HOMESERVER"], os.environ["BOT_USER"], os.environ["BOT_PASSWORD"]
MB = 1024 * 1024
MAX_MB = int(os.environ.get("MAX_MB", "100"))            # inline video cap
FILE_MAX_MB = int(os.environ.get("FILE_MAX_MB", "500"))  # "send as file" cap
YT_MAX_S = int(os.environ.get("YT_MAX_MIN", "30")) * 60
PROMPT_TIMEOUT_S = 600
ARCHIVE = Path(os.environ["ARCHIVE_DIR"]) if os.environ.get("ARCHIVE_DIR") else None
SERVER = USER.split(":", 1)[1]
BASE = Path("/opt/matrix-link-bot")
DL, SESSION, COOKIES = BASE / "downloads", BASE / "store" / "session.json", BASE / "cookies.txt"
HEIGHTS = [1080, 720, 480, 360, 240, 144]
YES, NO = {"✅", "✔", "☑"}, {"❌", "✖", "❎"}
AUDIO = {"🎵", "🎶", "🎧"}
PROMPT = ("This video is too long to be displayed inline. Send it as a file, or just the audio? "
          "(react with ✅ for video, 🎵 for audio, or ❌)")

IG_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:[\w.]+/)?(?:p|reels?|tv)/[\w-]+[^\s>]*", re.I)
YT_RE = re.compile(r"https?://(?:(?:www|m|music)\.)?(?:youtube\.com/(?:watch\?(?:\S*?&)?v=|shorts/|live/|embed/)"
                   r"|youtu\.be/)([\w-]{11})", re.I)
_THX = r"(?:thanks|thank\s*you|thank\s*u|thx|ty|tysm|tyvm|cheers)"
_BOT = r"link[\s_-]*bot"
THANKS_RE = re.compile(rf"\b{_THX}\b.*\b{_BOT}\b|\b{_BOT}\b.*\b{_THX}\b", re.I | re.S)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("linkbot")
client = AsyncClient(HS, USER)
pending = {}  # prompt event_id -> {room, src, url, info, user}


# ---------- download helpers (run in threads) ----------

def base_opts(workdir=None):
    o = {"quiet": True, "no_warnings": True}
    if workdir is not None:
        o.update(outtmpl=str(workdir / "%(id)s.%(ext)s"), merge_output_format="mp4")
    if COOKIES.exists():
        o["cookiefile"] = str(COOKIES)
    return o


def source_mp4s(workdir: Path):
    return [p for p in workdir.iterdir() if p.suffix == ".mp4" and not p.stem.endswith("_ios")]


def ensure_h264(path: Path) -> Path:
    """Transcode to H.264 if needed (iOS can't play VP9/AV1); always add faststart."""
    codec = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=codec_name", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, check=True).stdout.strip()
    out = path.with_name(path.stem + "_ios.mp4")
    if codec == "h264":
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-c", "copy", "-movflags", "+faststart", str(out)]
    else:
        log.info("transcoding %s -> h264", codec)
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "23", "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
               "-c:a", "copy", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)
    return out


def probe(path: Path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True)
    j = json.loads(out.stdout)
    s = j["streams"][0]
    return int(s["width"]), int(s["height"]), int(float(j["format"]["duration"]) * 1000)


def thumbnail(path: Path) -> Path:
    t = path.with_suffix(".jpg")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vframes", "1", "-q:v", "4", str(t)], check=True)
    return t


def download_ig(url: str, workdir: Path, cap_mb: int):
    opts = base_opts(workdir)
    opts.update(format="b[ext=mp4][vcodec!=none][acodec!=none]/bv*[ext=mp4]+ba[ext=m4a]/b",
                playlist_items="1", max_filesize=cap_mb * MB)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    vids = source_mp4s(workdir)
    if not vids:
        raise RuntimeError("no video (image-only post or over size cap)")
    return ensure_h264(vids[0]), info


def yt_info(url: str):
    with yt_dlp.YoutubeDL(base_opts() | {"noplaylist": True}) as ydl:
        return ydl.extract_info(url, download=False)


def est_size(f, dur):
    return f.get("filesize") or f.get("filesize_approx") or (f.get("tbr") or 0) * 125 * dur


def yt_candidates(info, cap):
    """Yield (height, format_id) from highest to lowest resolution whose estimated size fits the cap."""
    dur = info.get("duration") or 0
    fmts = info.get("formats") or []
    audios = [f for f in fmts if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    audio = max([f for f in audios if f.get("ext") == "m4a"] or audios,
                key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
    vids = [f for f in fmts if f.get("vcodec") not in (None, "none") and f.get("height")
            and f.get("protocol") in ("https", "http")]
    pool = [f for f in vids if str(f.get("vcodec")).startswith("avc1")] or vids  # prefer H.264: no transcode
    seen = set()
    for h in HEIGHTS:
        tier = [f for f in pool if f["height"] <= h]
        if not tier:
            continue
        v = max(tier, key=lambda f: (f["height"], f.get("tbr") or 0))
        if v.get("acodec") not in (None, "none"):
            fid, size = v["format_id"], est_size(v, dur)
        elif audio:
            fid, size = f"{v['format_id']}+{audio['format_id']}", est_size(v, dur) + est_size(audio, dur)
        else:
            continue
        if fid in seen or size > cap * 0.95:
            continue
        seen.add(fid)
        yield v["height"], fid


def download_yt(url: str, info, workdir: Path, cap_mb: int):
    cap = cap_mb * MB
    for height, fid in yt_candidates(info, cap):
        for p in workdir.iterdir():
            p.unlink()
        opts = base_opts(workdir)
        opts.update(format=fid, noplaylist=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        vids = source_mp4s(workdir)
        if not vids:
            continue
        out = ensure_h264(vids[0])
        size = out.stat().st_size
        if size <= cap:
            log.info("youtube %s: %dp, %.1f MB", info.get("id"), height, size / MB)
            return out, info
        log.info("youtube %s: %dp too big (%.1f MB), stepping down", info.get("id"), height, size / MB)
    raise RuntimeError(f"no format fits under {cap_mb} MB")


def download_audio(url: str, workdir: Path, cap_mb: int):
    opts = base_opts(workdir)
    opts.update(format="ba", noplaylist=True,
                postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}])
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    auds = [p for p in workdir.iterdir() if p.suffix == ".mp3"]
    if not auds:
        raise RuntimeError("audio extraction failed")
    if auds[0].stat().st_size > cap_mb * MB:
        raise RuntimeError(f"audio over {cap_mb} MB")
    return auds[0], info


# ---------- matrix helpers ----------

def safe_name(info, ext=".mp4") -> str:
    title = re.sub(r"[^\w\- .]", "", info.get("title") or "").strip()[:80]
    return f"{title or info.get('id', 'video')}{ext}"


async def upload(path: Path, mime: str):
    size = path.stat().st_size
    with open(path, "rb") as f:
        resp, _ = await client.upload(f, content_type=mime, filename=path.name, filesize=size)
    if not isinstance(resp, UploadResponse):
        raise RuntimeError(f"upload failed: {resp}")
    return resp.content_uri, size


async def react(room_id: str, event_id: str, key: str):
    r = await client.room_send(room_id, "m.reaction",
                               {"m.relates_to": {"rel_type": "m.annotation", "event_id": event_id, "key": key}})
    return getattr(r, "event_id", None)


async def deliver(room_id: str, event_id: str, fetch, kind="video"):
    """Run fetch(workdir) -> (path, info), then post the result as a reply."""
    hourglass = await react(room_id, event_id, "⏳")
    ok = False
    try:
        with tempfile.TemporaryDirectory(dir=DL) as d:
            path, info = await asyncio.to_thread(fetch, Path(d))
            name = safe_name(info, path.suffix)
            mime = "audio/mpeg" if kind == "audio" else "video/mp4"
            v_uri, v_size = await upload(path, mime)
            if kind == "audio":
                content = {"msgtype": "m.audio", "body": name, "filename": name, "url": v_uri,
                           "info": {"mimetype": mime, "size": v_size,
                                    "duration": int((info.get("duration") or 0) * 1000)}}
            elif kind == "file":
                content = {"msgtype": "m.file", "body": name, "filename": name, "url": v_uri,
                           "info": {"mimetype": "video/mp4", "size": v_size}}
            else:
                w, h, dur = await asyncio.to_thread(probe, path)
                thumb = await asyncio.to_thread(thumbnail, path)
                t_uri, t_size = await upload(thumb, "image/jpeg")
                content = {"msgtype": "m.video", "body": name, "url": v_uri,
                           "info": {"mimetype": "video/mp4", "size": v_size, "w": w, "h": h, "duration": dur,
                                    "thumbnail_url": t_uri,
                                    "thumbnail_info": {"mimetype": "image/jpeg", "size": t_size, "w": w, "h": h}}}
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": event_id}}
            await client.room_send(room_id, "m.room.message", content)
            ok = True
            if ARCHIVE:
                dest = ARCHIVE / f"{info.get('extractor_key', 'video').lower()}_{info.get('id', 'x')}{path.suffix}"
                try:
                    await asyncio.to_thread(shutil.copyfile, path, dest)
                except Exception as e:
                    log.warning("archive copy failed: %s", e)
    except Exception as e:
        log.warning("failed: %s", e)
    if hourglass:
        await client.room_redact(room_id, hourglass)
    if not ok:
        await react(room_id, event_id, "❌")


# ---------- handlers ----------

async def handle_ig(room_id: str, event_id: str, url: str):
    log.info("processing %s", url)
    await deliver(room_id, event_id, lambda d: download_ig(url, d, MAX_MB))


async def handle_yt(room_id: str, event_id: str, sender: str, url: str):
    log.info("processing %s", url)
    try:
        info = await asyncio.to_thread(yt_info, url)
    except Exception as e:
        log.warning("info failed %s: %s", url, e)
        await react(room_id, event_id, "❌")
        return
    if info.get("is_live"):
        await react(room_id, event_id, "❌")
        return
    if (info.get("duration") or 0) > YT_MAX_S:
        r = await client.room_send(room_id, "m.room.message", {
            "msgtype": "m.notice", "body": PROMPT,
            "m.relates_to": {"m.in_reply_to": {"event_id": event_id}}})
        pid = getattr(r, "event_id", None)
        if pid:
            pending[pid] = {"room": room_id, "src": event_id, "url": url, "info": info, "user": sender}
            await react(room_id, pid, "✅")
            await react(room_id, pid, "🎵")
            await react(room_id, pid, "❌")
            asyncio.create_task(expire_prompt(pid))
        return
    await deliver(room_id, event_id, lambda d: download_yt(url, info, d, MAX_MB))


async def expire_prompt(pid: str):
    await asyncio.sleep(PROMPT_TIMEOUT_S)
    p = pending.pop(pid, None)
    if p:
        await client.room_redact(p["room"], pid, reason="prompt expired")


async def on_reaction(room, event: ReactionEvent):
    p = pending.get(event.reacts_to)
    if not p or event.sender != p["user"]:
        return  # only the person who posted the link can answer
    key = event.key.replace("\ufe0f", "")
    if key not in YES | NO | AUDIO:
        return
    pending.pop(event.reacts_to, None)
    await client.room_redact(room.room_id, event.reacts_to)
    if key in YES:
        log.info("sending %s as file", p["url"])
        await deliver(p["room"], p["src"], lambda d: download_yt(p["url"], p["info"], d, FILE_MAX_MB), kind="file")
    elif key in AUDIO:
        log.info("sending %s as audio", p["url"])
        await deliver(p["room"], p["src"], lambda d: download_audio(p["url"], d, FILE_MAX_MB), kind="audio")


async def on_message(room, event: RoomMessageText):
    if event.sender == client.user_id:
        return
    if event.source.get("content", {}).get("m.relates_to", {}).get("rel_type") == "m.replace":
        return  # ignore edits
    body = "\n".join(l for l in event.body.splitlines() if not l.startswith(">"))  # skip quoted reply fallback
    if THANKS_RE.search(body):
        await react(room.room_id, event.event_id, "❤️")
    for url in dict.fromkeys(re.split(r"[?#]", u, maxsplit=1)[0] for u in IG_RE.findall(body)):
        asyncio.create_task(handle_ig(room.room_id, event.event_id, url))
    for url in dict.fromkeys(f"https://www.youtube.com/watch?v={v}" for v in YT_RE.findall(body)):
        asyncio.create_task(handle_yt(room.room_id, event.event_id, event.sender, url))


async def on_invite(room, event: InviteMemberEvent):
    if event.state_key == client.user_id and event.membership == "invite" and event.sender.endswith(":" + SERVER):
        log.info("joining %s (invited by %s)", room.room_id, event.sender)
        await client.join(room.room_id)


async def main():
    if SESSION.exists():
        s = json.loads(SESSION.read_text())
        client.access_token, client.device_id, client.user_id = s["access_token"], s["device_id"], USER
    else:
        r = await client.login(PW, device_name="matrix-link-bot")
        if not isinstance(r, LoginResponse):
            raise SystemExit(f"login failed: {r}")
        SESSION.write_text(json.dumps({"access_token": r.access_token, "device_id": r.device_id}))
        SESSION.chmod(0o600)
    await client.sync(timeout=30000, full_state=True)  # skip backlog
    for room_id in list(client.invited_rooms):
        await client.join(room_id)
    client.add_event_callback(on_message, RoomMessageText)
    client.add_event_callback(on_invite, InviteMemberEvent)
    client.add_event_callback(on_reaction, ReactionEvent)
    log.info("running as %s", USER)
    await client.sync_forever(timeout=30000)


if __name__ == "__main__":
    asyncio.run(main())
