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

GEMINI_MODEL = "gemini-2.5-flash"
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
    # Primary: yt-dlp (works from GH Actions runners)
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


def fetch_video_description(video_id: str) -> str:
    # Primary: yt-dlp full extraction (works from GH Actions IPs)
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


def fetch_transcript(video_id: str) -> str:
    """Best-effort: fetch Japanese auto-captions for the video.

    Primary path: ``youtube-transcript-api`` (pip), which handles the
    proof-of-origin token workarounds needed for ASR tracks on modern
    YouTube.

    Fallback: scrape ``captionTracks`` from the watch page HTML
    (works for videos with manual captions that don't need a PoT token).
    """
    # --- Primary: yt-dlp subtitle download (works from GH Actions IPs) ---
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


# ---------- Gemini ----------


GEMINI_SYSTEM_PROMPT = """\
あなたは「両学長のリベラルアーツ大学」の熱心なファンで、配信内容から
クオリティの高い4択クイズを作る日本語の編集者です。

【目的】
指定された「本日の両学長ライブ配信」の動画タイトル・概要欄（チャプター付き）
・字幕テキスト（ある場合）を読み、リベ大ファンが楽しめる4問のクイズを作ります。

【厺守ルール】
1. **必ずその配信の内容に即した問題**にすること。配信で明確に語られた
   話題・質問・学長の回答・数値・固有名詞を根拠にする。
2. 問題文は「視聴者のお悩み相談」「学長の主張」「配信中の具体的エピソード」
   「配信中に出た数字や固有名詞」など多様に。ワンパターン禁止。
3. 選択肢は必ず4つ。正解はそのうち1つ。ダミー3つは**配信内容から見て
   それっぽいけど間違い**にすること（リベ大頻出テーマ：節税、固定費、
   iDeCo、NISA、高配当株、ふるさと納税、副業、転職、保険解約など）。
4. 正解位置はランダムに散らす。全問同じ位置はNG。
5. 問題文と選択肢はスマホで読みやすいように短く（選択肢は6〜34文字目安）。
6. 問題文の途中改行は "\\n"（バックスラッシュ n）を含める。
7. 解説は1〜2行で、なぜそれが正解かを配信内容に触れて説明する。絵文字1〜2個OK。
8. 配信内容に明確な根拠がない話題は問題にしないこな（ハルシネーション禁止）。

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
  ]
}
"""


def _build_user_prompt(video: dict, description: str, transcript: str) -> str:
    today_label = datetime.now(JST).strftime("%Y年%m月%d日")
    lines = [
        f"【日付】{today_label}",
        f"【動画タイトル】{video['title']}",
        f"【動画URL】https://www.youtube.com/watch?v={video['video_id']}",
        "",
        "【概要欄（チャプター付き）】",
        description.strip(),
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
    return questions


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

    questions_raw = call_gemini(video, description, transcript)
    questions = validate_questions(questions_raw)

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
    }

    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[OK] wrote {OUTPUT_PATH} ({len(questions)} questions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
