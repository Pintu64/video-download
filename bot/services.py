import asyncio
import re
import time
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from bot.detect import classify, instagram_shortcode

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)
CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TERA_HOSTS = (
    "https://www.terabox.com",
    "https://www.1024terabox.com",
    "https://www.1024tera.com",
    "https://www.nephobox.com",
    "https://www.mirrobox.com",
    "https://www.4funbox.com",
)
QUALITIES = (
    ("1080", "M3U8_AUTO_1080"),
    ("720", "M3U8_AUTO_720"),
    ("480", "M3U8_AUTO_480"),
    ("360", "M3U8_AUTO_360"),
)
UPLOAD_LIMIT = 49 * 1024 * 1024
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


class ProviderError(Exception):
    pass


def human_size(size: int | None) -> str:
    if not size:
        return "size unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def human_time(seconds: int | None) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def safe_name(name: str, fallback: str = "video.mp4") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip().strip(".")
    return (cleaned or fallback)[:120]


def _shortcode_id(code: str) -> str:
    number = 0
    for char in code:
        number = number * 64 + ALPHABET.index(char)
    return str(number)


def _unescape(text: str) -> str:
    text = text.replace("\\/", "/")
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match.group(1), 16)), text)
    return text.replace("&amp;", "&")


def _surl_codes(url: str) -> list[str]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    codes: list[str] = []
    if query.get("surl"):
        codes.append(query["surl"][0])
    match = re.search(r"/s/([A-Za-z0-9_-]+)", parsed.path)
    if match:
        code = match.group(1)
        if code.startswith("1") and len(code) > 2:
            code = code[1:]
        codes.append(code)
    unique = []
    for code in codes:
        if code and code not in unique:
            unique.append(code)
        bare = code[1:] if code.startswith("1") and len(code) > 2 else "1" + code
        if bare and bare not in unique:
            unique.append(bare)
    return unique


def _parse_state(html: str) -> dict:
    marker = "window.__INITIAL_STATE__="
    index = html.find(marker)
    if index < 0:
        raise ProviderError("TeraBox did not return a file list.")
    import json

    data, _ = json.JSONDecoder().raw_decode(html[index + len(marker) :])
    return data


def _js_token(html: str) -> str:
    match = re.search(r"fn%28%22(.*?)%22%29", html)
    if not match:
        raise ProviderError("TeraBox blocked the share page.")
    return match.group(1)


async def inspect(url: str) -> dict:
    kind = classify(url)
    if kind == "instagram":
        return await inspect_instagram(url)
    if kind == "terabox":
        return await inspect_terabox(url)
    raise ProviderError("Send an Instagram or TeraBox link.")


async def inspect_instagram(url: str) -> dict:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        if instagram_shortcode(url) is None:
            response = await client.get(url, headers={"User-Agent": BROWSER_UA})
            url = str(response.url)
        parsed = instagram_shortcode(url)
        if not parsed:
            raise ProviderError("Use a post, reel, or IGTV link. Profile pages have no single video.")
        shortcode, kind = parsed
        page = f"https://www.instagram.com/{kind}/{shortcode}/"
        response = await client.get(
            page,
            headers={"User-Agent": CRAWLER_UA, "Accept": "text/html"},
        )
    html = _unescape(response.text)
    media_id = _shortcode_id(shortcode)
    anchor = html.find(f'"pk":"{media_id}"')
    if anchor < 0:
        anchor = html.find("video_versions")
    if anchor < 0:
        raise ProviderError(
            "Instagram did not share this video. Private posts, expired stories, and login walls cannot be opened without an account."
        )
    nxt = html.find('"pk":"', anchor + 8)
    window = html[anchor : nxt if nxt > anchor else anchor + 220000]
    videos = list(dict.fromkeys(re.findall(r'"url":"(https://[^"]+\.mp4[^"]*)"', window)))
    images = list(dict.fromkeys(re.findall(r'"url":"(https://[^"]+\.jpg[^"]*)"', window)))
    user = re.search(r'"username":"([^"]+)"', window)
    title = re.search(r'property="og:title" content="([^"]*)"', response.text)
    author = user.group(1) if user else ""
    caption = title.group(1) if title else (f"@{author}" if author else "Instagram")
    caption = caption.replace("&quot;", '"').replace("&#39;", "'")
    items = []
    for index, media in enumerate(videos, start=1):
        items.append(
            {
                "name": safe_name(f"{author or shortcode}.mp4"),
                "kind": "video",
                "size": None,
                "width": None,
                "height": None,
                "duration": None,
                "thumb": images[0] if images else None,
                "url": media,
                "label": f"Video {index}" if len(videos) > 1 else "Video",
            }
        )
    if not items:
        for index, media in enumerate(images[:10], start=1):
            items.append(
                {
                    "name": safe_name(f"{author or shortcode}-{index}.jpg", "photo.jpg"),
                    "kind": "image",
                    "size": None,
                    "width": None,
                    "height": None,
                    "duration": None,
                    "thumb": media,
                    "url": media,
                    "label": f"Photo {index}",
                }
            )
    if not items:
        raise ProviderError("No public media was found on this Instagram link.")
    return {
        "source": "instagram",
        "title": caption[:180],
        "author": author,
        "page": page,
        "items": items[:12],
    }


async def _tera_page(client: httpx.AsyncClient, surl: str, directory: str | None = None) -> tuple[str, str, dict, str]:
    codes = [surl]
    if surl.startswith("1") and len(surl) > 2:
        codes.append(surl[1:])
    elif not surl.startswith("1"):
        codes.append("1" + surl)
    for host in TERA_HOSTS:
        for code in codes:
            params = {"surl": code}
            if directory:
                params["dir"] = directory
            try:
                response = await client.get(f"{host}/wap/share/filelist", params=params)
            except httpx.HTTPError:
                continue
            if "window.__INITIAL_STATE__=" not in response.text or "server_filename" not in response.text:
                continue
            try:
                state = _parse_state(response.text)
                token = _js_token(response.text)
            except ProviderError:
                continue
            files = (state.get("share") or {}).get("fileList") or []
            if files:
                return host, code, state, token
    raise ProviderError(
        "This TeraBox link did not open. It may be expired, private, or password protected."
    )


def _file_item(entry: dict, surl: str, host: str) -> dict:
    name = entry.get("server_filename") or "file"
    thumbs = entry.get("thumbs") or {}
    video = str(entry.get("category")) == "1" or name.lower().endswith(
        (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv")
    )
    image = name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
    return {
        "name": safe_name(name, "file.bin"),
        "kind": "video" if video else "image" if image else "file",
        "size": int(entry.get("size") or 0) or None,
        "width": int(entry.get("width") or 0) or None,
        "height": int(entry.get("height") or 0) or None,
        "duration": int(entry.get("duration") or 0) or None,
        "thumb": thumbs.get("url3") or thumbs.get("url2") or thumbs.get("url1"),
        "url": None,
        "fs_id": str(entry.get("fs_id") or ""),
        "surl": surl,
        "host": host,
        "label": name,
    }


async def inspect_terabox(url: str) -> dict:
    codes = _surl_codes(url)
    if not codes:
        raise ProviderError("That link has no TeraBox share code. Use a /s/ or ?surl= link.")
    async with httpx.AsyncClient(follow_redirects=True, timeout=30, headers={"User-Agent": MOBILE_UA}) as client:
        host, surl, state, _token = await _tera_page(client, codes[0])
        share = state["share"]
        info = share.get("shareInfo") or {}
        items: list[dict] = []

        async def walk(entries: list[dict], depth: int) -> None:
            for entry in entries:
                if len(items) >= 30:
                    return
                if str(entry.get("isdir")) == "1" and depth < 2:
                    try:
                        _host, _code, nested, _tok = await _tera_page(
                            client, surl, directory=entry.get("path")
                        )
                        await walk((nested.get("share") or {}).get("fileList") or [], depth + 1)
                        continue
                    except ProviderError:
                        pass
                items.append(_file_item(entry, surl, host))

        await walk(share.get("fileList") or [], 0)
    if not items:
        raise ProviderError("The share opened, but it has no files.")
    author = info.get("username") or "TeraBox"
    title = items[0]["name"] if len(items) == 1 else f"{len(items)} files"
    return {"source": "terabox", "title": title, "author": author, "page": url, "items": items}


async def _playlist(
    client: httpx.AsyncClient,
    host: str,
    info: dict,
    token: str,
    fs_id: str,
    code: str,
    label: str,
) -> dict | None:
    response = await client.get(
        f"{host}/share/streaming",
        params={
            "app_id": "250528",
            "clienttype": "0",
            "web": "1",
            "channel": "dubox",
            "uk": str(info.get("uk") or ""),
            "shareid": str(info.get("shareid") or ""),
            "fid": fs_id,
            "sign": info.get("sign") or "",
            "timestamp": str(info.get("timestamp") or ""),
            "jsToken": token,
            "type": code,
            "isplayer": "1",
            "check_blue": "1",
        },
        headers={"User-Agent": MOBILE_UA, "Referer": "https://www.terabox.com/"},
    )
    if not response.text.startswith("#EXTM3U"):
        return None
    size_match = re.search(r"ts_size=(\d+)", response.text)
    return {
        "url": str(response.url),
        "size": int(size_match.group(1)) if size_match else None,
        "quality": label,
    }


async def _best_playlist(item: dict, quality: str) -> dict:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30, headers={"User-Agent": MOBILE_UA}) as client:
        host, _surl, state, token = await _tera_page(client, item["surl"])
        info = (state.get("share") or {}).get("shareInfo") or {}
        order = list(QUALITIES)
        if quality != "auto":
            order = [pair for pair in QUALITIES if pair[0] == quality] + [
                pair for pair in QUALITIES if pair[0] != quality
            ]
        found = []
        for label, code in order:
            for stream_host in (host, "https://www.terabox.com"):
                playlist = await _playlist(client, stream_host, info, token, item["fs_id"], code, label)
                if playlist:
                    found.append(playlist)
                    break
            if quality == "auto" and found and found[-1]["size"] and found[-1]["size"] <= UPLOAD_LIMIT:
                return found[-1]
            if quality == label and found and found[-1]["quality"] == quality:
                break
        if not found:
            raise ProviderError(
                "TeraBox has no playable stream for this file. Videos work without an account. Other file types stay locked."
            )
        exact = [row for row in found if row["quality"] == quality]
        if exact and quality != "auto":
            return exact[0]
        fitting = [row for row in found if not row["size"] or row["size"] <= UPLOAD_LIMIT]
        if fitting:
            return fitting[0]
        return min(found, key=lambda row: row["size"] or 10**15)


def _hms(value: str) -> float:
    value = value.strip()
    if not value or value == "N/A":
        return 0
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


async def _ffmpeg(cmd: list[str], duration: int | None, progress) -> None:
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    errors: list[bytes] = []

    async def drain() -> None:
        assert process.stderr is not None
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            errors.append(line)

    task = asyncio.create_task(drain())
    last = 0.0
    assert process.stdout is not None
    while True:
        raw = await process.stdout.readline()
        if not raw:
            break
        line = raw.decode(errors="ignore").strip()
        if not line.startswith("out_time="):
            continue
        now = time.time()
        if now - last < 2:
            continue
        last = now
        done = _hms(line.split("=", 1)[1])
        percent = min(99.0, done / duration * 100) if duration else None
        await progress(percent, "Saving video")
    await task
    code = await process.wait()
    saved = Path(cmd[-1])
    if code != 0 or not saved.exists() or saved.stat().st_size == 0:
        tail = b"".join(errors)[-300:].decode(errors="ignore").strip()
        raise ProviderError(tail or "ffmpeg could not save this video.")


async def _download_http(url: str, dest: Path, headers: dict, progress) -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code >= 400:
                raise ProviderError(f"Download failed ({response.status_code}). The link may have expired.")
            total = int(response.headers.get("content-length") or 0)
            done = 0
            last = 0.0
            with dest.open("wb") as handle:
                async for chunk in response.aiter_bytes(256 * 1024):
                    handle.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if total and now - last > 2:
                        last = now
                        await progress(min(99.0, done / total * 100), "Downloading")


async def download_item(item: dict, dest_dir: Path, mode: str, quality: str, progress) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    if item.get("fs_id"):
        playlist = await _best_playlist(item, quality if mode != "audio" else "480")
        target = dest_dir / safe_name(Path(item["name"]).stem + ".mp4", "video.mp4")
        header = f"User-Agent: {MOBILE_UA}\r\nReferer: https://www.terabox.com/\r\n"
        cmd = [
            "ffmpeg",
            "-y",
            "-headers",
            header,
            "-i",
            playlist["url"],
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            "-movflags",
            "+faststart",
        ]
        if mode == "audio":
            target = dest_dir / safe_name(Path(item["name"]).stem + ".m4a", "audio.m4a")
            cmd = [
                "ffmpeg",
                "-y",
                "-headers",
                header,
                "-i",
                playlist["url"],
                "-vn",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
            ]
        cmd += ["-progress", "pipe:1", "-nostats", str(target)]
        await progress(None, f"{playlist['quality']}p stream")
        await _ffmpeg(cmd, item.get("duration"), progress)
        item["quality"] = playlist["quality"]
        return target

    if not item.get("url"):
        raise ProviderError("This file has no public download link.")
    suffix = ".mp4" if item["kind"] == "video" else Path(item["name"]).suffix or ".bin"
    target = dest_dir / safe_name(Path(item["name"]).stem + suffix, "media" + suffix)
    await _download_http(item["url"], target, {"User-Agent": BROWSER_UA, "Referer": "https://www.instagram.com/"}, progress)
    if mode == "audio":
        audio = dest_dir / safe_name(target.stem + ".m4a", "audio.m4a")
        await _ffmpeg(
            ["ffmpeg", "-y", "-i", str(target), "-vn", "-c:a", "aac", "-b:a", "160k", "-progress", "pipe:1", "-nostats", str(audio)],
            item.get("duration"),
            progress,
        )
        target.unlink(missing_ok=True)
        return audio
    if target.stat().st_size > UPLOAD_LIMIT and item["kind"] == "video":
        smaller = dest_dir / safe_name(target.stem + "-720.mp4", "video.mp4")
        await progress(None, "Compressing for Telegram")
        await _ffmpeg(
            [
                "ffmpeg", "-y", "-i", str(target),
                "-vf", "scale=-2:720",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats", str(smaller),
            ],
            item.get("duration"),
            progress,
        )
        target.unlink(missing_ok=True)
        return smaller
    return target


def card_text(probe: dict) -> str:
    source = "Instagram" if probe["source"] == "instagram" else "TeraBox"
    author = f"\n{escape(probe['author'])}" if probe.get("author") else ""
    lines = [f"<b>{source}</b>{author}", "", escape(probe["title"])]
    if len(probe["items"]) == 1:
        item = probe["items"][0]
        bits = [bit for bit in (human_time(item.get("duration")), human_size(item.get("size")), item["kind"]) if bit]
        lines += ["", " · ".join(bits)]
    else:
        lines.append("")
        for index, item in enumerate(probe["items"][:8], start=1):
            extra = human_size(item.get("size"))
            lines.append(f"{index}. {escape(item['label'][:42])} · {extra}")
        if len(probe["items"]) > 8:
            lines.append(f"+ {len(probe['items']) - 8} more")
    lines += ["", "Tap play. The video is sent in this chat."]
    return "\n".join(lines)
