#!/usr/bin/env python3
"""
Daily リベ大 quiz generator.

Runs in GitHub Actions every morning. Zero external dependencies (stdlib only).

Flow:
  1. Fetch YouTube RSS for 両学長 channel
  2. Find the most recent "live" video (ライブ / 家計改善 / 収入アップ)
  3. Scrape the video page to extract the full description with chapter markers
  4. Parse chapter markers and description lines into topic candidates
  5. Generate 4 quiz questions using real chapter titles as correct answers
  6. Write quiz-data.json for the frontend to consume
"""

from __future__ import annotations

import html
import json
import random
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

# ---------- Configuration ----------

CHANNEL_ID = "UC23N4CK3fg30hJg9mqrDvKA"  # 両学長 リベラルアーツ大学
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "quiz-data.json"
JST = timezone(timedelta(hours=9))

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Pool of generic リベ大-themed distractors (used when we need filler choices)
DISTRACTOR_POOL = [
    "インデックス投資の積立", "米国株S&P500一括買い", "全世界株式オルカン",
    "新NISA成長投資枠の活用", "iDeCoの掛金上限引き上げ", "ふるさと納税のコツ",
    "確定申告の裏ワザ", "医療費控除の申請方法", "副業の始め方", "せどりの稼ぎ方",
    "プログラミング学習法", "ブログ収益化", "固定費の見直し", "格安SIMへの乗り換え",
    "不要な保険の解約", "楽天経済圏の作り方", "家計改善の第一歩", "転職エージェントの活用",
    "FIRE目標の立て方", "仮想通貨のリスク", "資産配分の基本", "つみたてNISAとの違い",
    "住宅ローン繰り上げ返済", "奨学金の返し方", "小規模企業共済", "法人化のタイミング",
    "インボイス制度対応", "年金の繰り下げ受給", "高配当ETFの選び方", "不動産投資の注意点",
    "外貨預金のデメリット", "定期預金の代替策",
]

# Question templates (topic goes in the "correct" slot)
QUESTION_TEMPLATES = [
    "今日の学長ライブで\n話題になったのはどれ？",
    "今日のライブで学長が\n触れたトピックは？",
    "今日の学長ライブで\n取り上げられたのは？",
    "今日のライブで\n学長が解説したのは？",
    "今日の学長ライブの\n話題として正しいのは？",
    "今日のライブで\n質問が出たのはどれ？",
]

EXPLANATION_TEMPLATES = [
    "正解は「{topic}」！📺\n今日のライブでしっかり解説されていました✨",
    "正解は「{topic}」🦁\n学長が詳しく話してくれていましたね🔥",
    "正解は「{topic}」💡\nアーカイブで復習してみよう！",
    "正解は「{topic}」📚\n学長ライブは毎日学びがありますね✨",
]


# ---------- HTTP helpers ----------


def http_get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "ja,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------- RSS ----------


def fetch_latest_live_video() -> dict | None:
    """Return dict with keys: video_id, title, published (YYYY-MM-DD)."""
    try:
        data = http_get(RSS_URL)
    except Exception as exc:
        print(f"[RSS] fetch failed: {exc}", file=sys.stderr)
        return None

    root = ET.fromstring(data)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
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
        candidates.append({
            "video_id": vid,
            "title": title,
            "published": pub[:10],
        })

    if not candidates:
        return None

    # Prefer today's live, then yesterday's, then any live, then latest.
    today = datetime.now(JST).strftime("%Y-%m-%d")
    yesterday = (datetime.now(JST) - timedelta(days=1)).strftime("%Y-%m-%d")

    def is_live(v: dict) -> bool:
        t = v["title"]
        return any(k in t for k in ("ライブ", "live", "LIVE", "家計改善", "収入アップ"))

    for bucket in (
        [v for v in candidates if v["published"] == today and is_live(v)],
        [v for v in candidates if v["published"] == today],
        [v for v in candidates if v["published"] == yesterday and is_live(v)],
        [v for v in candidates if v["published"] == yesterday],
        [v for v in candidates if is_live(v)],
        candidates,
    ):
        if bucket:
            return bucket[0]

    return None


# ---------- Video page scraping ----------


def fetch_video_description(video_id: str) -> str:
    """Scrape the video page for the shortDescription field."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        raw = http_get(url).decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"[SCRAPE] fetch failed: {exc}", file=sys.stderr)
        return ""

    # shortDescription lives in a JSON blob on the page.
    match = re.search(r'"shortDescription":"((?:\\.|[^"\\])*)"', raw)
    if not match:
        return ""

    escaped = match.group(1)
    # JSON-style unescape: handle \n \" \\ \u0041 ...
    try:
        desc = json.loads(f'"{escaped}"')
    except json.JSONDecodeError:
        desc = escaped.encode().decode("unicode_escape", errors="replace")
    return desc


# ---------- Topic extraction ----------


CHAPTER_RE = re.compile(
    r"^\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*[:：]?\s*(.+?)\s*$",
    re.MULTILINE,
)
TRIM_LEADING = re.compile(r"^[└├│─\s〜～ー・\-➤►▶▼▲◎●○◆◇■□]+")
STRIP_EMOJI_DECOR = re.compile(r"[🦁📺✨🔥💡📚💰🎉🎯⭐️⭐🌟]")

# Non-informative chapter labels to skip outright.
STOPWORD_TOPICS = {
    "intro", "Intro", "INTRO", "オープニング", "opening", "Opening", "OP", "op",
    "outro", "Outro", "エンディング", "ending", "Ending", "ED", "ed",
    "挨拶", "ごあいさつ", "お知らせ", "cm", "CM",
}


def clean_topic(s: str) -> str:
    s = STRIP_EMOJI_DECOR.sub("", s).strip()
    s = TRIM_LEADING.sub("", s).strip()
    s = re.sub(r"【[^】]*】", "", s).strip()
    # Drop nested parens such as "（xx:xx の続き）"
    s = re.sub(r"（[^（）]*の続き）$", "", s).strip()
    s = re.sub(r"\([^\)]*の続き\)$", "", s).strip()
    s = re.sub(r"\[[^\]]+\]$", "", s).strip()
    return s


def _norm(s: str) -> str:
    """Normalized key used for similarity / dedup checks."""
    return re.sub(r"[\s、。！？!?・]", "", s).lower()


def _too_similar(a: str, b: str) -> bool:
    """Return True when two topics overlap enough to confuse users."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # Substring containment on short strings is almost always the same topic.
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) >= 4 and shorter in longer:
        return True
    # Share a long common prefix.
    common = 0
    for x, y in zip(na, nb):
        if x == y:
            common += 1
        else:
            break
    if common >= 8 and common >= min(len(na), len(nb)) * 0.7:
        return True
    return False


def extract_topics(description: str) -> list[str]:
    topics: list[str] = []
    seen: set[str] = set()

    # 1) Chapter markers are the gold standard — they are what the host actually discussed.
    for m in CHAPTER_RE.finditer(description):
        raw = m.group(2)
        topic = clean_topic(raw)
        if topic in STOPWORD_TOPICS:
            continue
        if not (6 <= len(topic) <= 45):
            continue
        key = _norm(topic)[:14]
        if key in seen:
            continue
        # Reject if near-duplicate of something we already collected
        if any(_too_similar(topic, t) for t in topics):
            continue
        seen.add(key)
        topics.append(topic)

    # 2) If we don't have enough chapters, also split description lines.
    if len(topics) < 6:
        for line in description.splitlines():
            line = line.strip()
            if not line or line.startswith("http"):
                continue
            if re.match(r"^\d{1,2}:\d{2}", line):
                continue
            topic = clean_topic(line)
            if 6 <= len(topic) <= 45:
                key = topic[:12]
                if key not in seen:
                    seen.add(key)
                    topics.append(topic)
            if len(topics) >= 20:
                break

    return topics


# ---------- Quiz generation ----------


def generate_quiz(topics: list[str], date_label: str) -> list[dict]:
    """Generate exactly 4 quiz questions from topic candidates."""
    if not topics:
        return []

    random.seed()  # Nondeterministic; we commit only when the result is valid.

    # Pick up to 4 distinct topics as correct answers, spacing them out for variety.
    step = max(1, len(topics) // 4)
    picked: list[str] = []
    i = 0
    while len(picked) < 4 and i < len(topics):
        if topics[i] not in picked:
            picked.append(topics[i])
        i += step if len(topics) >= 8 else 1
    # Fill any remaining slots from the head of the list.
    for t in topics:
        if len(picked) >= 4:
            break
        if t not in picked:
            picked.append(t)

    if len(picked) < 4:
        return []

    questions: list[dict] = []
    used_templates: list[int] = []
    for idx, correct in enumerate(picked[:4]):
        # Pick a template we haven't used yet.
        available = [i for i in range(len(QUESTION_TEMPLATES)) if i not in used_templates]
        if not available:
            available = list(range(len(QUESTION_TEMPLATES)))
        ti = random.choice(available)
        used_templates.append(ti)

        # Build distractors: prefer other real topics (not similar to correct), then fall back to pool.
        other_real = [t for t in topics if not _too_similar(t, correct)]
        random.shuffle(other_real)
        distractors: list[str] = []
        for t in other_real:
            if any(_too_similar(t, d) for d in distractors):
                continue
            distractors.append(t)
            if len(distractors) >= 2:
                break
        pool = [d for d in DISTRACTOR_POOL if not _too_similar(d, correct)]
        random.shuffle(pool)
        for d in pool:
            if len(distractors) >= 3:
                break
            if any(_too_similar(d, x) for x in distractors):
                continue
            distractors.append(d)

        # Randomize correct position
        correct_index = random.randint(0, 3)
        choices = list(distractors)
        choices.insert(correct_index, correct)

        explanation = random.choice(EXPLANATION_TEMPLATES).format(topic=correct)
        question_text = QUESTION_TEMPLATES[ti].replace("今日", date_label)

        questions.append({
            "question": question_text,
            "choices": choices,
            "correctIndex": correct_index,
            "explanation": explanation,
        })

    return questions


# ---------- Main ----------


def main() -> int:
    video = fetch_latest_live_video()
    if not video:
        print("[FATAL] no video found", file=sys.stderr)
        return 1

    print(f"[INFO] Target video: {video['title']} ({video['published']})")

    description = fetch_video_description(video["video_id"])
    if not description:
        print("[WARN] description not found, using title only", file=sys.stderr)

    topics = extract_topics(description) if description else []
    # Fallback: split title into coarse topics.
    if len(topics) < 4:
        title_clean = re.sub(r"【.*?】", " ", video["title"])
        title_clean = re.sub(r"[☆★祝✨🔥💡📺]", " ", title_clean)
        title_topics = [
            t.strip() for t in re.split(r"[&＆、。！!・\s]+", title_clean)
            if 4 <= len(t.strip()) <= 40
        ]
        for t in title_topics:
            if t not in topics:
                topics.append(t)

    if len(topics) < 4:
        print(f"[FATAL] not enough topics: {len(topics)}", file=sys.stderr)
        return 1

    today = datetime.now(JST)
    today_str = today.strftime("%Y-%m-%d")
    pub_date = video["published"]
    if pub_date == today_str:
        date_label = "今日"
    elif pub_date == (today - timedelta(days=1)).strftime("%Y-%m-%d"):
        date_label = "昨日"
    else:
        try:
            y, m, d = pub_date.split("-")
            date_label = f"{int(m)}/{int(d)}"
        except Exception:
            date_label = "最新"

    questions = generate_quiz(topics, date_label)
    if len(questions) != 4:
        print(f"[FATAL] generated {len(questions)} questions, expected 4", file=sys.stderr)
        return 1

    payload = {
        "date": today_str,
        "dateLabel": date_label,
        "videoId": video["video_id"],
        "videoUrl": f"https://www.youtube.com/watch?v={video['video_id']}",
        "videoTitle": video["title"],
        "generatedAt": today.isoformat(timespec="seconds"),
        "generatedBy": "github-actions",
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
