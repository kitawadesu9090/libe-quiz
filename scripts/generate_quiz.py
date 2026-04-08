#!/usr/bin/env python3
"""
Daily リベ大 quiz generator (Q&A pair aware).

Parses the video's chapter markers and understands the structure:
  - Top-level chapter that ends with ? → a viewer's question
  - Sub-chapter (starts with └ / ├ / │) → the answer or follow-up
  - 🦁-prefixed chapter → 学長 himself answering / asserting

Builds quiz questions in the shape of "今日のライブで質問された ○○ に対する
学長の答えは？" with other real answers as distractors.
"""

from __future__ import annotations

import html
import json
import random
import re
import sys
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

# ---------- Configuration ----------

CHANNEL_ID = "UC67Wr_9pA4I0glIxDt_Cpyw"  # 両学長 リベラルアーツ大学 (@ryogakucho)
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "quiz-data.json"
JST = timezone(timedelta(hours=9))

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Pool of generic リベ大-themed distractors used when real answers run out.
DISTRACTOR_POOL = [
    "まず固定費を見直すのが先決",
    "インデックス投資を淡々と積み立てる",
    "米国株S&P500に一括投資する",
    "新NISAの成長投資枠を活用する",
    "iDeCoに掛金上限まで拠出する",
    "ふるさと納税を上限まで使う",
    "確定申告で所得を圧縮する",
    "副業で収入の柱を増やす",
    "ブログやせどりで稼ぐ力をつける",
    "格安SIMに乗り換えて固定費カット",
    "不要な生命保険を解約する",
    "楽天経済圏でポイントを貯める",
    "転職エージェントで年収アップを狙う",
    "FIRE目標を逆算して積立額を決める",
    "仮想通貨はリスクが高いから避ける",
    "住宅ローンは無理せず繰り上げ返済",
    "小規模企業共済で節税する",
    "つみたてNISAから始める",
    "高配当ETFで不労所得を作る",
    "法人化して経費を計上する",
    "貯金を先に確保してから投資",
    "子ども名義で口座を作る",
    "変額保険は手数料が高いので不要",
    "銀行預金より国債を選ぶ",
]


# ---------- HTTP ----------


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


def _published_to_jst_date(published: str) -> str:
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        return dt.astimezone(JST).strftime("%Y-%m-%d")
    except Exception:
        return published[:10]


def fetch_latest_live_video() -> dict | None:
    """Return dict with video_id, title, published (JST YYYY-MM-DD), description."""
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


# ---------- Video page scraping fallback ----------


def fetch_video_description(video_id: str) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        raw = http_get(url).decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"[SCRAPE] fetch failed: {exc}", file=sys.stderr)
        return ""
    match = re.search(r'"shortDescription":"((?:\\.|[^"\\])*)"', raw)
    if not match:
        return ""
    escaped = match.group(1)
    try:
        return json.loads(f'"{escaped}"')
    except json.JSONDecodeError:
        return escaped.encode().decode("unicode_escape", errors="replace")


# ---------- Chapter parsing ----------


# Matches both tree markers and plain hyphen decorators used for sub-chapters.
SUB_PREFIX_CHARS = "└├│┗┣┃-━─"
LION_CHARS = "🦁"
STRIP_DECOR = re.compile(r"[🦁📺✨🔥💡📚💰🎉🎯⭐️⭐🌟]")
STRIP_BRACKETS = re.compile(r"【[^】]*】")
TRIM_EDGES = re.compile(rf"^[{re.escape(SUB_PREFIX_CHARS)}\s〜～ー・➤►▶▼▲◎●○◆◇■□]+")
TRAILING_NOISE = re.compile(r"（[^（）]*の続き）$|\([^\)]*の続き\)$|\[[^\]]+\]$")

STOPWORD_TOPICS = {
    "intro", "Intro", "INTRO", "オープニング", "opening", "Opening", "OP", "op",
    "outro", "Outro", "エンディング", "ending", "Ending", "ED", "ed",
}


@dataclass
class Chapter:
    time: str
    raw: str
    title: str
    is_sub: bool
    is_question: bool
    has_lion: bool


@dataclass
class ChapterGroup:
    parent: Chapter
    children: list[Chapter] = field(default_factory=list)


def _clean(raw: str) -> tuple[str, bool, bool]:
    """Return (clean_title, is_sub, has_lion) for a chapter line."""
    is_sub = False
    for ch in raw[:3]:
        if ch in SUB_PREFIX_CHARS:
            is_sub = True
            break
    has_lion = LION_CHARS in raw
    s = raw
    s = STRIP_DECOR.sub("", s)
    s = STRIP_BRACKETS.sub("", s)
    s = TRIM_EDGES.sub("", s)
    s = TRAILING_NOISE.sub("", s).strip()
    return s, is_sub, has_lion


# Words that signal a viewer question even without a trailing ?
QUESTION_SIGNALS = (
    "悩んでいる", "悩んでます", "悩み", "困っている", "困ってます",
    "教えて", "教えてください", "知りたい", "迷っている", "迷って",
    "どう思いますか", "どうでしょうか", "どうすれば", "どうすべき",
    "アドバイス", "意見を聞きたい", "見解", "ご意見",
)


def _is_question_like(title: str) -> bool:
    s = title.rstrip()
    if s.endswith(("？", "?")):
        return True
    return any(sig in s for sig in QUESTION_SIGNALS)


def parse_chapters(description: str) -> list[Chapter]:
    if not description:
        return []
    chapters: list[Chapter] = []
    for line in description.splitlines():
        m = re.match(r"^\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*(.+?)\s*$", line)
        if not m:
            continue
        time_s = m.group(1)
        raw = m.group(2)
        title, is_sub, has_lion = _clean(raw)
        if not title or title in STOPWORD_TOPICS:
            continue
        if not (5 <= len(title) <= 80):
            continue
        is_q = _is_question_like(title)
        chapters.append(Chapter(
            time=time_s,
            raw=raw,
            title=title,
            is_sub=is_sub,
            is_question=is_q,
            has_lion=has_lion,
        ))
    return chapters


def _title_overlap(a: str, b: str) -> int:
    """Count how many 4-char sliding substrings of the shorter string appear in the longer."""
    if len(a) < 4 or len(b) < 4:
        return 0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    hits = 0
    for i in range(len(shorter) - 3):
        if shorter[i:i + 4] in longer:
            hits += 1
    return hits


def group_by_parent(chapters: list[Chapter]) -> list[ChapterGroup]:
    """Group top-level chapters with their sub-chapters, preferring topic matches over sequential order."""
    groups: list[ChapterGroup] = []
    parent_idx_by_ch: dict[int, int] = {}

    # First pass: build a group for each top-level chapter.
    for ch in chapters:
        if not ch.is_sub:
            groups.append(ChapterGroup(parent=ch))

    if not groups:
        # Promote the first sub-chapter, if any, to a parent.
        if chapters:
            first = chapters[0]
            promoted = Chapter(
                time=first.time, raw=first.raw, title=first.title,
                is_sub=False, is_question=first.is_question, has_lion=first.has_lion,
            )
            groups.append(ChapterGroup(parent=promoted))
        return groups

    # Second pass: assign each sub-chapter to the best-matching recent parent (by topic overlap).
    last_parent_idx = 0
    parent_index = {id(g.parent): i for i, g in enumerate(groups)}
    for ch in chapters:
        if not ch.is_sub:
            last_parent_idx = parent_index.get(id(ch), last_parent_idx)
            continue
        # Consider the last 3 parents (including current) and pick the best topic overlap.
        best_idx = last_parent_idx
        best_score = 0
        for j in range(max(0, last_parent_idx - 2), last_parent_idx + 1):
            score = _title_overlap(ch.title, groups[j].parent.title)
            if score > best_score:
                best_score = score
                best_idx = j
        # Require a meaningful overlap to reassign; otherwise default to the most recent.
        if best_score >= 2:
            groups[best_idx].children.append(ch)
        else:
            groups[last_parent_idx].children.append(ch)

    return groups


# ---------- Text helpers ----------


_NORM = re.compile(r"[\s、。！？!?・「」『』【】\-ー]")


def _norm(s: str) -> str:
    return _NORM.sub("", s).lower()


def _too_similar(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) >= 5 and shorter in longer:
        return True
    common = 0
    for x, y in zip(na, nb):
        if x == y:
            common += 1
        else:
            break
    if common >= 8 and common >= min(len(na), len(nb)) * 0.7:
        return True
    return False


def shorten_question(text: str, max_len: int = 38) -> str:
    """Shorten a verbose viewer question to a tappable size."""
    s = text.strip()
    s = re.sub(r"^[「『]", "", s)
    s = re.sub(r"[」』]$", "", s)
    s = s.rstrip("？?。、.")
    # Cut at the first major punctuation after ~12 chars
    for sep in ("。", "、", "．", ",", "，"):
        idx = s.find(sep)
        if 12 <= idx <= max_len:
            return s[:idx].strip()
    if len(s) <= max_len:
        return s
    return s[:max_len].rstrip() + "…"


# ---------- Question generators ----------


QUESTION_TEMPLATES_QA = [
    '今日のライブで\n「{q}」\nという質問への学長の答えは？',
    '今日のライブで出た\n「{q}」という相談に\n学長はどう答えた？',
    '今日のライブの\n「{q}」の\n学長の回答は？',
]

QUESTION_TEMPLATES_STATEMENT = [
    '今日のライブで学長が\n「{q}」について\n語った内容は？',
    '今日のライブで学長が\n強調していたのは？',
    '今日のライブで学長が\n主張していたのは？',
]

EXPLANATION_QA = [
    '正解は「{a}」📚\n実際のライブで学長がそう答えていました✨',
    '正解は「{a}」🦁\nライブで学長がしっかり解説してましたね💡',
    '正解は「{a}」🔥\n見逃した方はアーカイブで要チェック！',
]


def _choose_answer_from_children(children: list[Chapter]) -> Chapter | None:
    """Pick the child that most likely contains 学長's answer. Never use a question as an answer."""
    if not children:
        return None
    lion = [c for c in children if c.has_lion and not c.is_question]
    if lion:
        return lion[0]
    non_q = [c for c in children if not c.is_question]
    if non_q:
        return non_q[0]
    return None  # We refuse to treat another viewer question as an answer.


def _collect_distractor_answers(all_groups: list[ChapterGroup], exclude_group: ChapterGroup) -> list[str]:
    """Gather realistic wrong answers: non-question children from OTHER groups."""
    results: list[str] = []
    for g in all_groups:
        if g is exclude_group:
            continue
        for c in g.children:
            if not c.is_question and 6 <= len(c.title) <= 34:
                results.append(c.title)
        # A 🦁-marked parent is also an assertion we can reuse
        if g.parent.has_lion and not g.parent.is_question and 6 <= len(g.parent.title) <= 34:
            results.append(g.parent.title)
    return results


def build_qa_question(group: ChapterGroup, all_groups: list[ChapterGroup]) -> dict | None:
    """Build a question where the parent is a viewer question and children contain the answer."""
    if not group.parent.is_question:
        return None
    answer_ch = _choose_answer_from_children(group.children)
    if answer_ch is None:
        return None
    correct = answer_ch.title.rstrip("？?")
    # Reject answers that are too short or too long to make a clean 4-choice question.
    if not (6 <= len(correct) <= 34):
        return None
    # Sanity check: the answer must share keywords with the parent question.
    # This prevents wrongly-grouped children from polluting the QA.
    if _title_overlap(group.parent.title, correct) == 0:
        return None

    short_q = shorten_question(group.parent.title)
    stem = random.choice(QUESTION_TEMPLATES_QA).format(q=short_q)

    # Distractor pool: (a) other groups' real answers (b) generic pool
    real_others = _collect_distractor_answers(all_groups, exclude_group=group)
    random.shuffle(real_others)

    distractors: list[str] = []
    for t in real_others:
        if len(distractors) >= 2:
            break
        if _too_similar(t, correct):
            continue
        if any(_too_similar(t, d) for d in distractors):
            continue
        distractors.append(t.rstrip("？?"))

    pool = DISTRACTOR_POOL[:]
    random.shuffle(pool)
    for t in pool:
        if len(distractors) >= 3:
            break
        if _too_similar(t, correct):
            continue
        if any(_too_similar(t, d) for d in distractors):
            continue
        distractors.append(t)

    if len(distractors) < 3:
        return None

    correct_idx = random.randint(0, 3)
    choices = list(distractors)
    choices.insert(correct_idx, correct)

    return {
        "question": stem,
        "choices": choices,
        "correctIndex": correct_idx,
        "explanation": random.choice(EXPLANATION_QA).format(a=correct),
        "_source_time": group.parent.time,
        "_source_kind": "qa",
    }


def build_statement_question(group: ChapterGroup, all_groups: list[ChapterGroup]) -> dict | None:
    """Build a question around a 学長 statement (🦁 parent, non-question)."""
    if not group.parent.has_lion or group.parent.is_question:
        return None
    correct = group.parent.title.rstrip("？?")
    if not (6 <= len(correct) <= 34):
        return None

    # Variety: pick a non-parametric template at random
    stem = random.choice([
        '今日のライブで学長が\n強調していたのは？',
        '今日のライブで学長が\n主張していたのは？',
        '今日のライブで学長が\n伝えていたのは？',
    ])

    real_others = _collect_distractor_answers(all_groups, exclude_group=group)
    random.shuffle(real_others)

    distractors: list[str] = []
    for t in real_others:
        if len(distractors) >= 2:
            break
        if _too_similar(t, correct):
            continue
        if any(_too_similar(t, d) for d in distractors):
            continue
        distractors.append(t.rstrip("？?"))

    pool = DISTRACTOR_POOL[:]
    random.shuffle(pool)
    for t in pool:
        if len(distractors) >= 3:
            break
        if _too_similar(t, correct):
            continue
        if any(_too_similar(t, d) for d in distractors):
            continue
        distractors.append(t)

    if len(distractors) < 3:
        return None

    correct_idx = random.randint(0, 3)
    choices = list(distractors)
    choices.insert(correct_idx, correct)

    return {
        "question": stem,
        "choices": choices,
        "correctIndex": correct_idx,
        "explanation": random.choice(EXPLANATION_QA).format(a=correct),
        "_source_time": group.parent.time,
        "_source_kind": "statement",
    }


def build_topic_question(chapter: Chapter, all_chapters: list[Chapter]) -> dict | None:
    """Generic fallback: 'which of these topics was discussed today?'"""
    correct = chapter.title.rstrip("？?")
    if not (6 <= len(correct) <= 34):
        return None

    others = [
        c.title.rstrip("？?")
        for c in all_chapters
        if c is not chapter and 6 <= len(c.title) <= 60
    ]
    random.shuffle(others)

    distractors: list[str] = []
    for t in others:
        if len(distractors) >= 2:
            break
        if _too_similar(t, correct):
            continue
        if any(_too_similar(t, d) for d in distractors):
            continue
        distractors.append(t)

    pool = DISTRACTOR_POOL[:]
    random.shuffle(pool)
    for t in pool:
        if len(distractors) >= 3:
            break
        if _too_similar(t, correct):
            continue
        if any(_too_similar(t, d) for d in distractors):
            continue
        distractors.append(t)

    if len(distractors) < 3:
        return None

    correct_idx = random.randint(0, 3)
    choices = list(distractors)
    choices.insert(correct_idx, correct)

    return {
        "question": "今日のライブで\n実際に話題になったのはどれ？",
        "choices": choices,
        "correctIndex": correct_idx,
        "explanation": f"正解は「{correct}」💡\n今日のライブで取り上げられました📺",
        "_source_time": chapter.time,
        "_source_kind": "topic",
    }


def generate_questions(chapters: list[Chapter]) -> list[dict]:
    groups = group_by_parent(chapters)

    questions: list[dict] = []
    used_correct: list[str] = []

    # Priority 1: Q&A groups (viewer question + answer)
    qa_groups = [g for g in groups if g.parent.is_question and g.children]
    random.shuffle(qa_groups)
    for g in qa_groups:
        if len(questions) >= 4:
            break
        q = build_qa_question(g, groups)
        if not q:
            continue
        correct = q["choices"][q["correctIndex"]]
        if any(_too_similar(correct, u) for u in used_correct):
            continue
        questions.append(q)
        used_correct.append(correct)

    # Priority 2: 🦁 statement groups
    if len(questions) < 4:
        stmt_groups = [g for g in groups if g.parent.has_lion and not g.parent.is_question]
        random.shuffle(stmt_groups)
        for g in stmt_groups:
            if len(questions) >= 4:
                break
            q = build_statement_question(g, groups)
            if not q:
                continue
            correct = q["choices"][q["correctIndex"]]
            if any(_too_similar(correct, u) for u in used_correct):
                continue
            questions.append(q)
            used_correct.append(correct)

    # Priority 3: plain topic questions
    if len(questions) < 4:
        topic_chapters = [c for c in chapters if not c.is_sub and len(c.title) >= 8]
        random.shuffle(topic_chapters)
        for c in topic_chapters:
            if len(questions) >= 4:
                break
            q = build_topic_question(c, chapters)
            if not q:
                continue
            correct = q["choices"][q["correctIndex"]]
            if any(_too_similar(correct, u) for u in used_correct):
                continue
            questions.append(q)
            used_correct.append(correct)

    # Strip internal bookkeeping fields
    for q in questions:
        q.pop("_source_time", None)
        q.pop("_source_kind", None)
    return questions[:4]


# ---------- Main ----------


def main() -> int:
    video = fetch_latest_live_video()
    if not video:
        print("[FATAL] no video found", file=sys.stderr)
        return 1
    print(f"[INFO] Target video: {video['title']} ({video['published']})")

    description = video.get("description") or ""
    if len(description) < 100:
        scraped = fetch_video_description(video["video_id"])
        if scraped and len(scraped) > len(description):
            description = scraped
    if not description:
        print("[FATAL] description not found", file=sys.stderr)
        return 1
    print(f"[INFO] Description length: {len(description)} chars")

    chapters = parse_chapters(description)
    print(f"[INFO] Parsed {len(chapters)} chapters ({sum(1 for c in chapters if c.is_sub)} sub, {sum(1 for c in chapters if c.is_question)} questions)")
    if len(chapters) < 4:
        print(f"[FATAL] not enough chapters: {len(chapters)}", file=sys.stderr)
        return 1

    questions = generate_questions(chapters)
    if len(questions) != 4:
        print(f"[FATAL] generated {len(questions)} questions, expected 4", file=sys.stderr)
        return 1

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

    # Replace "今日" in stems with the resolved label for clarity.
    if date_label != "今日":
        for q in questions:
            q["question"] = q["question"].replace("今日のライブ", f"{date_label}のライブ")

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
