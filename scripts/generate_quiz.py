#!/usr/bin/env python3
"""
Daily リベ大 quiz generator (Gemini-powered).

Pipeline:
  1. Pull the latest 両学長 live video from RSS (@ryogakucho).
  2. Fetch the full video description (chapters + summary).
  3. Fetch auto-captions from YouTube timedtext API when available.
  4. Ask Gemini 2.5 Flash to synthesize 4 high-quality quiz questions
     as structured JSON.
  5. Validate the result and write ``quiz-data.json``.

Only the Python standard library is used so the script runs on a
stock GitHub Actions runner without any ``pip install`` step.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

# ---------- Configuration ----------

CHANNEL_ID = "UC67Wr_9pA4I0glIxDt_Cpyw"  # 両学長 リベラルアーツ大学 (@ryogakucho)
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "quiz-data.json"
JST = timezone(timedelta(hours=9))

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ---------- HTTP helpers ----------


def http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "ja,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_post_json(url: str, payload: dict, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------- RSS ----------


def _published_to_jst_date(published: str) -> str:
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        return dt.astimezone(JST).strftime("%Y-%m-%d")
    except Exception:
        return published[:10]


def fetch_latest_live_video_api() -> dict | None:
    """YouTube Data API v3 search で最新のライブ配信動画を取得。

    yt-dlp はデータセンターIPで古い動画を拾ってしまうことがあるため、
    正規APIで確実に最新動画を取得する。
    """
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        print("[SEARCH] YOUTUBE_API_KEY not set, skipping API search", file=sys.stderr)
        return None

    url = (
        "https://www.googleapis.com/youtube/v3/search"
        f"?part=snippet&channelId={CHANNEL_ID}"
        "&order=date&maxResults=15&type=video"
        f"&key={api_key}"
    )
    try:
        raw = http_get(url, timeout=15)
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        print(f"[SEARCH] YouTube API search failed: {exc}", file=sys.stderr)
        return None

    items = data.get("items") or []
    if not items:
        print("[SEARCH] YouTube API returned no items", file=sys.stderr)
        return None

    candidates: list[dict] = []
    for item in items:
        snip = item.get("snippet", {})
        vid = (item.get("id") or {}).get("videoId")
        title = snip.get("title") or ""
        pub_raw = snip.get("publishedAt") or ""
        if not vid or not title:
            continue
        # HTML entities (like &amp;) may appear in API responses
        title = (title.replace("&amp;", "&").replace("&quot;", '"')
                      .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
        candidates.append({
            "video_id": vid,
            "title": title.strip(),
            "published": _published_to_jst_date(pub_raw),
            "description": snip.get("description") or "",
        })

    # Drop Shorts
    candidates = [
        v for v in candidates
        if "#shorts" not in v["title"].lower() and "#short" not in v["title"].lower()
    ]

    now_jst = datetime.now(JST)
    today = now_jst.strftime("%Y-%m-%d")
    yesterday = (now_jst - timedelta(days=1)).strftime("%Y-%m-%d")
    # Live streams typically end around 8:30-9:00 JST. Before 9:00 JST,
    # today's live is likely still ongoing, so prefer yesterday's live.
    prefer_yesterday = now_jst.hour < 9

    def is_live(v: dict) -> bool:
        t = v["title"]
        return any(k in t for k in ("ライブ", "live", "LIVE", "家計改善", "収入アップ"))

    if prefer_yesterday:
        priority = (
            [v for v in candidates if v["published"] == yesterday and is_live(v)],
            [v for v in candidates if v["published"] == today and is_live(v)],
            [v for v in candidates if is_live(v)],
            [v for v in candidates if v["published"] == yesterday],
            [v for v in candidates if v["published"] == today],
            candidates,
        )
    else:
        priority = (
            [v for v in candidates if v["published"] == today and is_live(v)],
            [v for v in candidates if v["published"] == yesterday and is_live(v)],
            [v for v in candidates if is_live(v)],
            [v for v in candidates if v["published"] == today],
            [v for v in candidates if v["published"] == yesterday],
            candidates,
        )

    for bucket in priority:
        if bucket:
            picked = bucket[0]
            print(f"[SEARCH] YouTube API picked {picked['video_id']} ({picked['published']}, prefer_yesterday={prefer_yesterday})", file=sys.stderr)
            return picked
    return None


def fetch_latest_live_video_ytdlp() -> dict | None:
    """Use yt-dlp to enumerate the channel's recent live streams.

    YouTube returns HTTP 404 for the RSS feed when requested from GitHub
    Actions runner IPs, so yt-dlp (with its built-in IP rotation logic
    and proper YouTube client emulation) is used as the primary path.
    """
    try:
        from yt_dlp import YoutubeDL  # type: ignore
    except Exception as exc:
        print(f"[YTDLP] import failed: {exc}", file=sys.stderr)
        return None

    candidates: list[dict] = []
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": 15,
        "skip_download": True,
    }
    for url in (
        f"https://www.youtube.com/channel/{CHANNEL_ID}/streams",
        f"https://www.youtube.com/channel/{CHANNEL_ID}/videos",
    ):
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            print(f"[YTDLP] {url} failed: {exc}", file=sys.stderr)
            continue
        for entry in (info or {}).get("entries", []) or []:
            vid = entry.get("id")
            title = entry.get("title") or ""
            if not vid or not title:
                continue
            ts = entry.get("timestamp") or entry.get("release_timestamp")
            if ts:
                pub = datetime.fromtimestamp(ts, tz=JST).strftime("%Y-%m-%d")
            else:
                pub = entry.get("upload_date") or ""
                if len(pub) == 8:
                    pub = f"{pub[:4]}-{pub[4:6]}-{pub[6:]}"
            candidates.append({
                "video_id": vid,
                "title": title.strip(),
                "published": pub,
                "description": "",
            })
        if candidates:
            break

    if not candidates:
        return None

    candidates = [
        v for v in candidates
        if "#shorts" not in v["title"].lower() and "#short" not in v["title"].lower()
    ]
    today = datetime.now(JST).strftime("%Y-%m-%d")
    yesterday = (datetime.now(JST) - timedelta(days=1)).strftime("%Y-%m-%d")

    def is_live(v: dict) -> bool:
        t = v["title"]
        return any(k in t for k in ("ライブ", "live", "LIVE", "家計改善", "収入アップ"))

    for bucket in (
        [v for v in candidates if v["published"] == today and is_live(v)],
        [v for v in candidates if v["published"] == yesterday and is_live(v)],
        [v for v in candidates if is_live(v)],
        [v for v in candidates if v["published"] == today],
        [v for v in candidates if v["published"] == yesterday],
        candidates,
    ):
        if bucket:
            return bucket[0]
    return None


def fetch_latest_live_video() -> dict | None:
    """Return dict with video_id, title, published (JST YYYY-MM-DD), description."""
    # Primary: YouTube Data API v3 search (most reliable for latest video)
    via_api = fetch_latest_live_video_api()
    if via_api:
        return via_api

    # Secondary: yt-dlp
    via_ytdlp = fetch_latest_live_video_ytdlp()
    if via_ytdlp:
        return via_ytdlp

    # Fallback: RSS feed (often 404s from datacenter IPs)
    try:
        data = http_get(RSS_URL)
    except Exception as exc:
        print(f"[RSS] fetch failed: {exc}", file=sys.stderr)
        return None

    root = ET.fromstring(data)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    entries = root.findall("atom:entry", ns)
    if not entries:
        print("[RSS] no entries found", file=sys.stderr)
        return None

    candidates: list[dict] = []
    for entry in entries[:15]:
        title_el = entry.find("atom:title", ns)
        vid_el = entry.find("yt:videoId", ns)
        pub_el = entry.find("atom:published", ns)
        if title_el is None or vid_el is None or pub_el is None:
            continue
        title = (title_el.text or "").strip()
        vid = (vid_el.text or "").strip()
        pub = (pub_el.text or "").strip()
        desc = ""
        mg = entry.find("media:group", ns)
        if mg is not None:
            desc_el = mg.find("media:description", ns)
            if desc_el is not None and desc_el.text:
                desc = desc_el.text
        candidates.append({
            "video_id": vid,
            "title": title,
            "published": _published_to_jst_date(pub),
            "description": desc,
        })

    # Drop Shorts
    candidates = [
        v for v in candidates
        if "#shorts" not in v["title"].lower() and "#short" not in v["title"].lower()
    ]

    today = datetime.now(JST).strftime("%Y-%m-%d")
    yesterday = (datetime.now(JST) - timedelta(days=1)).strftime("%Y-%m-%d")

    def is_live(v: dict) -> bool:
        t = v["title"]
        return any(k in t for k in ("ライブ", "live", "LIVE", "家計改善", "収入アップ"))

    for bucket in (
        [v for v in candidates if v["published"] == today and is_live(v)],
        [v for v in candidates if v["published"] == yesterday and is_live(v)],
        [v for v in candidates if is_live(v)],
        [v for v in candidates if v["published"] == today],
        [v for v in candidates if v["published"] == yesterday],
        candidates,
    ):
        if bucket:
            return bucket[0]
    return None


# ---------- Full description & transcript ----------


def fetch_video_description_api(video_id: str) -> str:
    """YouTube Data API v3 で概要欄を取得（データセンターIPでもブロックされない）。"""
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        print("[DESC] YOUTUBE_API_KEY not set, skipping API", file=sys.stderr)
        return ""
    url = (
        f"https://www.googleapis.com/youtube/v3/videos"
        f"?part=snippet&id={video_id}&key={api_key}"
    )
    try:
        raw = http_get(url, timeout=15)
        data = json.loads(raw.decode("utf-8"))
        items = data.get("items") or []
        if items:
            desc = items[0].get("snippet", {}).get("description", "")
            if desc:
                print(f"[DESC] YouTube API ok, {len(desc)} chars", file=sys.stderr)
                return desc
        print("[DESC] YouTube API returned no description", file=sys.stderr)
    except Exception as exc:
        print(f"[DESC] YouTube API failed: {exc}", file=sys.stderr)
    return ""


def fetch_video_description(video_id: str) -> str:
    # Primary: YouTube Data API v3 (reliable from datacenter IPs)
    api_desc = fetch_video_description_api(video_id)
    if api_desc:
        return api_desc

    # Secondary: yt-dlp full extraction
    try:
        from yt_dlp import YoutubeDL  # type: ignore
        with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
        desc = (info or {}).get("description") or ""
        if desc:
            print(f"[DESC] yt-dlp ok, {len(desc)} chars", file=sys.stderr)
            return desc
    except Exception as exc:
        print(f"[DESC] yt-dlp failed: {exc}", file=sys.stderr)

    # Fallback: scrape watch page HTML
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        raw = http_get(url).decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"[DESC] fetch failed: {exc}", file=sys.stderr)
        return ""
    match = re.search(r'"shortDescription":"((?:\\.|[^"\\])*)"', raw)
    if not match:
        return ""
    escaped = match.group(1)
    try:
        return json.loads(f'"{escaped}"')
    except json.JSONDecodeError:
        return escaped.encode().decode("unicode_escape", errors="replace")


def _fetch_transcript_innertube(video_id: str) -> str:
    """Innertube API (youtubei/v1/player) で字幕URLを取得し、テキストを返す。

    認証不要でデータセンターIPからもアクセス可能。
    """
    innertube_url = "https://www.youtube.com/youtubei/v1/player"
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20240101.00.00",
                "hl": "ja",
                "gl": "JP",
            }
        },
        "videoId": video_id,
    }
    try:
        resp = http_post_json(innertube_url, payload, timeout=20)
    except Exception as exc:
        print(f"[CC] Innertube player request failed: {exc}", file=sys.stderr)
        return ""

    captions = resp.get("captions", {}).get("playerCaptionsTracklistRenderer", {})
    tracks = captions.get("captionTracks") or []
    if not tracks:
        print("[CC] Innertube: no captionTracks", file=sys.stderr)
        return ""

    # 日本語を優先、手動字幕 > ASR
    def _score(t: dict) -> int:
        lang = (t.get("languageCode") or "").lower()
        kind = (t.get("kind") or "").lower()
        s = 0
        if lang.startswith("ja"):
            s += 100
        if kind != "asr":
            s += 10
        return s

    tracks_sorted = sorted(tracks, key=_score, reverse=True)
    for t in tracks_sorted:
        base_url = t.get("baseUrl") or ""
        if not base_url:
            continue
        try:
            raw = http_get(base_url, timeout=30).decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[CC] Innertube caption fetch failed: {exc}", file=sys.stderr)
            continue
        text = _extract_transcript_text(raw)
        if len(text) > 200:
            lang = t.get("languageCode", "?")
            kind = t.get("kind") or "manual"
            print(f"[CC] Innertube {lang}/{kind}, {len(text)} chars", file=sys.stderr)
            return text
    print("[CC] Innertube: no usable caption text", file=sys.stderr)
    return ""


def fetch_transcript(video_id: str) -> str:
    """Best-effort: fetch Japanese auto-captions for the video.

    Priority:
      1. Innertube API (no auth needed, datacenter-friendly)
      2. yt-dlp subtitle download
      3. youtube-transcript-api
      4. watch page captionTracks scrape
    """
    # --- Primary: Innertube API ---
    innertube_text = _fetch_transcript_innertube(video_id)
    if innertube_text:
        return innertube_text

    # --- Secondary: yt-dlp subtitle download ---
    try:
        from yt_dlp import YoutubeDL  # type: ignore
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["ja", "ja-JP", "en"],
            "subtitlesformat": "vtt",
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
        subs = (info or {}).get("subtitles") or {}
        autos = (info or {}).get("automatic_captions") or {}
        for src in (subs, autos):
            for lang in ("ja", "ja-JP", "ja-orig", "en"):
                tracks = src.get(lang) or []
                for tr in tracks:
                    url = tr.get("url")
                    if not url:
                        continue
                    try:
                        raw = http_get(url, timeout=30).decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    # VTT or XML; reuse existing extractor for XML, simple parse for VTT
                    if "<text" in raw or raw.startswith("<?xml"):
                        text = _extract_transcript_text(raw)
                    else:
                        # VTT: drop timestamps + WEBVTT header
                        lines = []
                        for ln in raw.splitlines():
                            ln = ln.strip()
                            if not ln or ln.startswith("WEBVTT") or "-->" in ln or ln.isdigit():
                                continue
                            lines.append(re.sub(r"<[^>]+>", "", ln))
                        text = "\n".join(lines)
                    if len(text) > 200:
                        print(
                            f"[CC] yt-dlp {lang}, {len(text)} chars",
                            file=sys.stderr,
                        )
                        return text
    except Exception as exc:
        print(f"[CC] yt-dlp subtitle failed: {exc}", file=sys.stderr)

    # --- Secondary: youtube-transcript-api ---
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=["ja", "ja-JP", "en"])
        segs = fetched.to_raw_data()
        text = "\n".join(
            (s.get("text") or "").replace("\n", " ").strip()
            for s in segs
            if (s.get("text") or "").strip()
        )
        if len(text) > 200:
            print(
                f"[CC] youtube-transcript-api ok, {len(segs)} segs, {len(text)} chars",
                file=sys.stderr,
            )
            return text
    except Exception as exc:
        print(f"[CC] youtube-transcript-api failed: {exc}", file=sys.stderr)

    # --- Fallback: watch page captionTracks scrape ---
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        html_raw = http_get(watch_url, timeout=20).decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"[CC] watch page fetch failed: {exc}", file=sys.stderr)
        return ""

    m = re.search(r'"captionTracks":(\[.*?\])', html_raw)
    if not m:
        print("[CC] no captionTracks found on watch page", file=sys.stderr)
        return ""

    tracks_json = m.group(1)
    try:
        # baseUrl etc. come escaped (\u0026, \/) — use json.loads to normalize.
        tracks = json.loads(tracks_json)
    except json.JSONDecodeError as exc:
        print(f"[CC] captionTracks JSON parse failed: {exc}", file=sys.stderr)
        return ""

    def _score(t: dict) -> int:
        lang = (t.get("languageCode") or "").lower()
        kind = (t.get("kind") or "").lower()
        score = 0
        if lang.startswith("ja"):
            score += 100
        if kind != "asr":
            # Prefer manual captions over asr when both exist
            score += 10
        return score

    tracks_sorted = sorted(tracks, key=_score, reverse=True)
    for t in tracks_sorted:
        base_url = t.get("baseUrl") or ""
        if not base_url:
            continue
        # Raw JSON may still contain \u0026; json.loads already decoded it.
        try:
            raw = http_get(base_url, timeout=30).decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[CC] baseUrl fetch failed: {exc}", file=sys.stderr)
            continue
        if not raw.strip():
            continue
        text = _extract_transcript_text(raw)
        if len(text) > 200:
            lang = t.get("languageCode", "?")
            kind = t.get("kind", "manual")
            print(f"[CC] using {lang}/{kind}, {len(text)} chars", file=sys.stderr)
            return text
    return ""


def _extract_transcript_text(raw: str) -> str:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return ""
    parts: list[str] = []
    for el in root.iter():
        if el.tag.endswith("text") and el.text:
            t = el.text.replace("\n", " ").strip()
            t = re.sub(r"&#39;", "'", t)
            t = re.sub(r"&amp;", "&", t)
            t = re.sub(r"&quot;", '"', t)
            t = re.sub(r"<[^>]+>", "", t)
            if t:
                parts.append(t)
    return "\n".join(parts)


# ---------- ジャンケン検出 ----------


def _get_video_duration_seconds(video_id: str) -> int | None:
    """yt-dlp で動画の尺（秒）を取得。失敗時は None。"""
    try:
        from yt_dlp import YoutubeDL  # type: ignore
        with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
        dur = (info or {}).get("duration")
        if isinstance(dur, (int, float)):
            return int(dur)
    except Exception as exc:
        print(f"[JANKEN-VISION] duration fetch failed: {exc}", file=sys.stderr)
    return None


def detect_janken_hand_vision(video_id: str) -> int | None:
    """Gemini Vision で YouTube 動画のエンディングを解析し、学長じゃんけんの手を検出。

    両学長のじゃんけんは配信の **最後** で行われるため、動画の末尾 2分を解析する。
    字幕がない配信でも使える。

    Returns:
        0=グー, 1=チョキ, 2=パー, None=検出失敗
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("[JANKEN-VISION] GEMINI_API_KEY not set", file=sys.stderr)
        return None

    # 動画の尺を取得してエンディング部分を特定
    duration = _get_video_duration_seconds(video_id)
    if duration and duration > 180:
        # 末尾2分を解析（ライブは通常60分以上）
        start_offset = f"{max(0, duration - 150)}s"
        end_offset = f"{duration}s"
    else:
        # 尺が取れなかった場合はデフォルトで最後5分をリクエスト（長い動画想定）
        start_offset = "3450s"  # 57.5分
        end_offset = "3600s"    # 60分

    print(
        f"[JANKEN-VISION] analyzing video {video_id} "
        f"duration={duration}s range={start_offset}-{end_offset}",
        file=sys.stderr,
    )

    youtube_url = f"https://www.youtube.com/watch?v={video_id}"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "fileData": {
                            "fileUri": youtube_url,
                            "mimeType": "video/mp4",
                        },
                        "videoMetadata": {
                            "startOffset": start_offset,
                            "endOffset": end_offset,
                        },
                    },
                    {
                        "text": (
                            "この動画は両学長のリベラルアーツ大学のライブ配信のエンディング部分です。"
                            "配信の最後に両学長は「学長じゃんけん！じゃんけん〇〇！バイバイ！」"
                            "という形でじゃんけんを視聴者に向かって出します。\n\n"
                            "学長が「バイバイ」と言う直前、じゃんけんで出した**最終的な手**を特定してください。\n\n"
                            "判断基準:\n"
                            "- グー（✊）: 握りこぶし、5本の指すべてを握りしめている\n"
                            "- チョキ（✌️）: 人差し指と中指を立てたVサイン、他の指は握っている\n"
                            "- パー（✋）: 5本の指を全て開いて広げた手\n\n"
                            "注意事項:\n"
                            "- 「じゃん、けん、ぽん！」のリズムで複数フレームで手を出すので、"
                            "「ぽん！」の瞬間（または「バイバイ」直前）の手を採用する\n"
                            "- 手のひらの向き（前向き/手の甲向き）に関係なく、指の形で判定する\n"
                            "- 確信が持てない場合は「不明」にする（推測しない）\n\n"
                            "以下のJSONだけを返してください（他の文字は一切不要）:\n"
                            '{"hand": "グー" | "チョキ" | "パー" | "不明", "reasoning": "判断根拠を一文で"}'
                        )
                    },
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 512,
            "responseMimeType": "application/json",
        },
    }

    url = f"{GEMINI_URL}?key={api_key}"
    try:
        resp = http_post_json(url, payload, timeout=180)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[JANKEN-VISION] HTTP {e.code}: {body[:300]}", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"[JANKEN-VISION] request failed: {exc}", file=sys.stderr)
        return None

    cands = resp.get("candidates") or []
    if not cands:
        print(f"[JANKEN-VISION] empty candidates: {json.dumps(resp)[:300]}", file=sys.stderr)
        return None
    parts = cands[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        print("[JANKEN-VISION] empty response", file=sys.stderr)
        return None

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            print(f"[JANKEN-VISION] non-JSON response: {text[:200]}", file=sys.stderr)
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    hand = str(data.get("hand", "")).strip()
    reason = str(data.get("reasoning", "")).strip()
    print(f"[JANKEN-VISION] response: hand={hand}, reason={reason[:100]}", file=sys.stderr)
    mapping = {"グー": 0, "ぐー": 0, "チョキ": 1, "ちょき": 1, "パー": 2, "ぱー": 2}
    if hand in mapping:
        return mapping[hand]
    print(f"[JANKEN-VISION] unrecognized hand: {hand}", file=sys.stderr)
    return None


def detect_janken_hand(transcript: str) -> int | None:
    """両学長ライブ配信エンディングの「学長じゃんけん」で出した手を検出する。

    両学長のじゃんけんは配信の **最後** で「学長じゃんけん！じゃんけん〇〇！バイバイ」
    という流れで行われる。したがって字幕の末尾を優先的に探索する。

    ASR による省略も考慮:
      - 「グ」だけ（「グー」が短縮） → グー
      - 「チョ」だけ（「チョキ」が短縮） → チョキ
      - 「ぱー」のひらがな表記 → パー

    Returns:
        0=グー, 1=チョキ, 2=パー, None=検出できず
    """
    if not transcript:
        return None

    # 配信末尾（最後の3000文字）に「学長じゃんけん」→手がある
    tail = transcript[-3000:] if len(transcript) > 3000 else transcript

    # パターン1: 「学長じゃんけん」「じゃんけんぽん」の後に出る手を検出
    # ASR省略に対応: グー/グ, チョキ/チョ/ちょ, パー/ぱー
    janken_patterns = [
        # 「学長じゃんけん...手」の厳密マッチ
        (r"学長じゃんけん.{0,8}?(グー|ぐー|チョキ|ちょき|パー|ぱー)(?![ーっ])", "strict"),
        # ASR省略: グ単独 (バイバイが続くことが多い)
        (r"学長じゃんけん.{0,8}?グ(?:バイ|、|\s|$)", "gu_short"),
        (r"学長じゃんけん.{0,8}?チョ(?:キ)?(?:バイ|、|\s|$)", "choki_short"),
        (r"学長じゃんけん.{0,8}?パ(?:ー)?(?:バイ|、|\s|$)", "pa_short"),
        # 末尾の「じゃんけん→バイバイ」の間にある手
        (r"じゃんけん[じゃんけんぽん]*\s*(グー|ぐー|チョキ|ちょき|パー|ぱー).{0,20}?バイバイ", "before_bye"),
        (r"じゃんけん.{0,15}?バイバイ", "bye_context"),
    ]

    for pattern, kind in janken_patterns:
        m = re.search(pattern, tail)
        if not m:
            continue
        matched = m.group(0)
        # 手を判定
        if "グー" in matched or "ぐー" in matched or re.search(r"じゃんけん.{0,8}?グ(?!ー)", matched):
            return 0
        elif "チョキ" in matched or "ちょき" in matched or "チョ" in matched:
            return 1
        elif "パー" in matched or "ぱー" in matched or "パ" in matched:
            return 2

    # フォールバック: 末尾「バイバイ」手前に出現する手
    bye_idx = tail.rfind("バイバイ")
    if bye_idx > 0:
        context = tail[max(0, bye_idx - 50):bye_idx]
        for pattern, hand_id in [
            (r"グ[ーー]?(?!オ)", 0),  # グー or グ（単独）
            (r"チョ[キ]?", 1),         # チョキ or チョ
            (r"パ[ーー]?", 2),         # パー or パ
            (r"ぐー", 0),
            (r"ちょき", 1),
            (r"ぱー", 2),
        ]:
            if re.search(pattern, context):
                return hand_id

    return None


# ---------- Gemini ----------


GEMINI_SYSTEM_PROMPT = """\
あなたは「両学長のリベラルアーツ大学」の熱心なファンで、配信内容から
クオリティの高い4択クイズを作る日本語の編集者です。

【目的】
指定された「本日の両学長ライブ配信」の動画タイトル・概要欄（チャプター付き）
・字幕テキスト（ある場合）を読み、リベ大ファンが楽しめる4問のクイズを作ります。

【厳守ルール】
1. **必ずその配信の内容（トピック）に即した問題**にすること。
   動画タイトルやチャプター名に出てくる「そのライブ配信の本題（AI活用、節税、投資判断など）」
   に関する問題だけを作る。
2. **以下のようなメタ情報・定型文からの出題は絶対禁止**:
   - ライブ配信のアーカイブ期間（「120時間で消える」「リベシティで公開」等）
   - リベシティの会員特典・広告文言（「82.9%が資産アップ実感」等）
   - プレゼント企画・応募フォーム・ぬいぐるみ等のキャンペーン情報
   - 関連資料のURL、関連動画、楽曲リンク等のリンク情報
   - 「概要欄に◯◯と書かれている」系のメタ問題
   これらは配信の「本題」ではなく定型テンプレートなので、問題にしない。
3. 問題文は「視聴者のお悩み相談」「学長の主張・考え方」「配信中の具体的エピソード」
   「配信中に出た数字や固有名詞」「学長の推奨/非推奨」など多様に。
4. 選択肢は必ず4つ。正解はそのうち1つ。ダミー3つは**配信内容から見て
   それっぽいけど間違い**にすること（リベ大頻出テーマ：節税、固定費、
   iDeCo、NISA、高配当株、ふるさと納税、副業、転職、保険解約など）。
5. 正解位置はランダムに散らす。全問同じ位置はNG。
6. 問題文と選択肢はスマホで読みやすいように短く（選択肢は6〜34文字目安）。
7. 問題文の途中改行は "\\n"（バックスラッシュ n）を含める。
8. 解説は1〜2行で、なぜそれが正解かを配信内容に触れて説明する。絵文字1〜2個OK。
   ※「概要欄に記載されている」ではなく「学長は◯◯と主張した」のように
   配信の中身に言及すること。
9. 配信内容に明確な根拠がない話題は問題にしないこと（ハルシネーション禁止）。
10. 字幕テキストが提供されている場合は字幕の発言を最優先の根拠にする。
    字幕がない場合は、概要欄から**テンプレート部分を除いた「その回固有の話題・チャプター」**
    を根拠にする。タイトルやチャプター名に出てくるキーワードがその日の本題。

【じゃんけん検出】
字幕テキストの冒頭部分に学長のじゃんけんが含まれている場合、
学長が出した手を検出してください。「じゃんけん」のあとに「グー」「チョキ」
「パー」のどれが出たかを見ます。

【出力形式】
以下のJSONだけを返してください。マークダウンのコードブロックや前後の
説明文は一切不要です。

{
  "questions": [
    {
      "question": "問題文（必要なら \\n で改行）",
      "choices": ["選択肢1", "選択肢2", "選択肢3", "選択肢4"],
      "correctIndex": 0,
      "explanation": "正解の根拠を1〜2行で"
    },
    ... 合計4問 ...
  ],
  "jankenHand": 0
}

jankenHand: 学長が出した手。0=グー, 1=チョキ, 2=パー。
字幕からじゃんけんが検出できない場合は null にしてください。
"""


def _strip_description_boilerplate(description: str) -> str:
    """概要欄からテンプレ定型文を除去し、その回固有のトピック部分だけを返す。

    除去対象:
      - アーカイブ期間の説明（120時間で消える等）
      - リベシティ会員特典・広告文言（82.9%等）
      - プレゼント企画・応募フォーム
      - 関連資料・URL羅列
      - ハッシュタグ行
    """
    if not description:
        return ""

    # 定型セクションの開始目印（これ以降を切り落とす）
    section_cutoffs = [
        "ライブは120時間",
        "ライブ配信は120時間",
        "リベシティ」の紹介",
        "リベシティの紹介",
        "■リベシティ",
        "▼リベシティ",
        "【リベシティ",
        "会員の82.9%",
        "会員の82.9％",
        "関連資料",
        "▼関連資料",
        "■関連資料",
        "【関連資料",
        "関連動画",
        "▼関連動画",
        "■関連動画",
        "ぬいぐるみプレゼント",
        "プレゼント企画",
        "プレゼント受取",
        "■プレゼント",
        "▼プレゼント",
        "免責事項",
        "▼免責",
        "■免責",
    ]

    text = description
    earliest_cut = len(text)
    for marker in section_cutoffs:
        idx = text.find(marker)
        if idx != -1 and idx < earliest_cut:
            earliest_cut = idx
    text = text[:earliest_cut]

    # 行単位でさらにノイズを除去
    cleaned_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        # URLだけの行、ハッシュタグだけの行は除去
        if re.fullmatch(r"https?://\S+", stripped):
            continue
        if stripped.startswith("#") and " " not in stripped:
            continue
        # リベシティの宣伝文句
        if "リベシティ" in stripped and any(
            k in stripped for k in ("資産アップ", "会員限定", "特典", "無料体験", "30日間")
        ):
            continue
        cleaned_lines.append(line)

    result = "\n".join(cleaned_lines).strip()
    return result


def _build_user_prompt(video: dict, description: str, transcript: str) -> str:
    today_label = datetime.now(JST).strftime("%Y年%m月%d日")
    cleaned_desc = _strip_description_boilerplate(description)
    if len(cleaned_desc) < 100 and len(description) > len(cleaned_desc):
        # 除去しすぎた場合は元の概要欄を使う
        cleaned_desc = description.strip()
    lines = [
        f"【日付】{today_label}",
        f"【動画タイトル】{video['title']}",
        f"【動画URL】https://www.youtube.com/watch?v={video['video_id']}",
        "",
        "【概要欄（その回のトピック・チャプターのみ）】",
        cleaned_desc,
    ]
    if transcript:
        # Keep transcript modest; 16k chars ≈ enough context for gemini-2.5-flash
        snippet = transcript.strip()
        if len(snippet) > 16000:
            snippet = snippet[:16000] + "…（以下省略）"
        lines += ["", "【自動字幕（抜粋）】", snippet]
    lines += [
        "",
        "上記の配信内容に基づいて、ルールに従って4問のクイズをJSONで返してください。",
    ]
    return "\n".join(lines)


def call_gemini(video: dict, description: str, transcript: str) -> list[dict]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    user_prompt = _build_user_prompt(video, description, transcript)
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_prompt}],
            }
        ],
        "systemInstruction": {
            "parts": [{"text": GEMINI_SYSTEM_PROMPT}],
        },
        "generationConfig": {
            "temperature": 0.4,
            "topP": 0.9,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }
    url = f"{GEMINI_URL}?key={api_key}"
    import time
    last_err: Exception | None = None
    resp: dict | None = None
    for attempt in range(5):
        try:
            resp = http_post_json(url, payload, timeout=90)
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            last_err = RuntimeError(f"Gemini HTTP {e.code}: {body[:500]}")
            # 429 (quota) / 500 / 503 are worth retrying; 4xx auth errors are not
            if e.code in (429, 500, 502, 503, 504):
                wait = 5 * (attempt + 1)
                print(
                    f"[Gemini] HTTP {e.code}, retry {attempt + 1}/5 in {wait}s",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue
            raise last_err from e
        except Exception as e:
            last_err = e
            wait = 5 * (attempt + 1)
            print(f"[Gemini] {e}, retry {attempt + 1}/5 in {wait}s", file=sys.stderr)
            time.sleep(wait)
    if resp is None:
        raise last_err or RuntimeError("Gemini: unknown failure after retries")

    cands = resp.get("candidates") or []
    if not cands:
        raise RuntimeError(f"Gemini empty candidates: {json.dumps(resp)[:500]}")
    parts = cands[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError(f"Gemini empty text: {json.dumps(resp)[:500]}")

    # Gemini should have honored responseMimeType and returned pure JSON.
    # Fall back to extracting a JSON object if wrapped in prose.
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise RuntimeError(f"Gemini non-JSON response: {text[:300]}")
        data = json.loads(m.group(0))

    questions = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(questions, list):
        raise RuntimeError(f"Gemini response has no 'questions' list: {text[:300]}")
    janken_hand = data.get("jankenHand") if isinstance(data, dict) else None
    return questions, janken_hand


# ---------- Validation ----------


def validate_questions(raw: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for i, q in enumerate(raw[:4]):
        if not isinstance(q, dict):
            raise RuntimeError(f"Q{i} is not an object")
        stem = q.get("question")
        choices = q.get("choices")
        correct = q.get("correctIndex")
        explanation = q.get("explanation")
        if not isinstance(stem, str) or not stem.strip():
            raise RuntimeError(f"Q{i} has empty question")
        if not isinstance(choices, list) or len(choices) != 4:
            raise RuntimeError(f"Q{i} must have exactly 4 choices, got {choices}")
        if not all(isinstance(c, str) and c.strip() for c in choices):
            raise RuntimeError(f"Q{i} choices contain empty values")
        if not isinstance(correct, int) or not (0 <= correct <= 3):
            raise RuntimeError(f"Q{i} correctIndex invalid: {correct}")
        if not isinstance(explanation, str) or not explanation.strip():
            explanation = f"正解は「{choices[correct]}」"
        cleaned.append({
            "question": stem.strip(),
            "choices": [c.strip() for c in choices],
            "correctIndex": correct,
            "explanation": explanation.strip(),
        })
    if len(cleaned) != 4:
        raise RuntimeError(f"expected 4 questions, got {len(cleaned)}")
    return cleaned


# ---------- Main ----------


def main() -> int:
    video = fetch_latest_live_video()
    if not video:
        print("[FATAL] no video found", file=sys.stderr)
        return 1
    print(f"[INFO] Target video: {video['title']} ({video['published']})")

    description = video.get("description") or ""
    if len(description) < 200:
        scraped = fetch_video_description(video["video_id"])
        if scraped and len(scraped) > len(description):
            description = scraped
    if not description:
        print("[WARN] description not found, proceeding with title only", file=sys.stderr)
        description = video["title"]
    print(f"[INFO] Description length: {len(description)} chars")

    transcript = fetch_transcript(video["video_id"])
    print(f"[INFO] Transcript length: {len(transcript)} chars")

    questions_raw, gemini_janken = call_gemini(video, description, transcript)
    questions = validate_questions(questions_raw)

    # ジャンケン検出:
    #   1. 字幕から直接検出（最高精度・ただし字幕が生成済みであることが前提）
    #   2. Gemini Vision で動画冒頭60秒を解析（字幕なしでも使える）
    #   3. クイズ生成時に Gemini が字幕から検出した結果
    janken_hand = detect_janken_hand(transcript)
    if janken_hand is not None:
        print(f"[JANKEN] 字幕から検出: {['グー','チョキ','パー'][janken_hand]}", file=sys.stderr)
    else:
        # 字幕が取れなかった場合は Gemini Vision で動画を解析
        vision_result = detect_janken_hand_vision(video["video_id"])
        if vision_result is not None:
            janken_hand = vision_result
            print(f"[JANKEN] Gemini Visionで検出: {['グー','チョキ','パー'][janken_hand]}", file=sys.stderr)
        elif isinstance(gemini_janken, int) and 0 <= gemini_janken <= 2:
            janken_hand = gemini_janken
            print(f"[JANKEN] Gemini字幕分析で検出: {['グー','チョキ','パー'][janken_hand]}", file=sys.stderr)
        else:
            print("[JANKEN] 検出できず（サイト側でランダム生成）", file=sys.stderr)

    today = datetime.now(JST)
    today_str = today.strftime("%Y-%m-%d")
    if video["published"] == today_str:
        date_label = "今日"
    elif video["published"] == (today - timedelta(days=1)).strftime("%Y-%m-%d"):
        date_label = "昨日"
    else:
        try:
            y, m, d = video["published"].split("-")
            date_label = f"{int(m)}/{int(d)}"
        except Exception:
            date_label = "最新"

    payload = {
        "date": today_str,
        "dateLabel": date_label,
        "videoId": video["video_id"],
        "videoUrl": f"https://www.youtube.com/watch?v={video['video_id']}",
        "videoTitle": video["title"],
        "generatedAt": today.isoformat(timespec="seconds"),
        "generatedBy": "github-actions (gemini-2.5-flash)",
        "questions": questions,
        "jankenHand": janken_hand,  # 0=グー, 1=チョキ, 2=パー, null=不明
    }

    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[OK] wrote {OUTPUT_PATH} ({len(questions)} questions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
