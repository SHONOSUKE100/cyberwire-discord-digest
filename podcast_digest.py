#!/usr/bin/env python3
"""Create Japanese explanations for new CyberWire podcast episodes."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from google import genai


ROOT = Path(__file__).resolve().parent
STATE_PATH = Path(os.getenv("STATE_PATH", ROOT / "state" / "episodes.json"))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
TEST_MODE = os.getenv("TEST_MODE", "false").lower() in {"1", "true", "yes"}
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_BYTES", str(500 * 1024 * 1024)))
MAX_DISCORD_SUMMARY_CHARS = 5_500

HTTP = requests.Session()
HTTP.headers.update(
    {"User-Agent": "cyberwire-discord-digest/1.0 (+GitHub Actions)"}
)


@dataclass(frozen=True)
class Podcast:
    key: str
    name: str
    feed_url: str
    episode_path: str
    baseline_episode: int
    baseline_date: str


@dataclass(frozen=True)
class Episode:
    podcast: Podcast
    number: int
    title: str
    published: str
    page_url: str
    audio_url: str | None
    notes: str
    entry_id: str


PODCASTS = (
    Podcast(
        key="cyberwire_daily",
        name="CyberWire Daily",
        feed_url="https://feeds.megaphone.fm/cyberwire-daily-podcast",
        episode_path="daily-podcast",
        baseline_episode=2630,
        baseline_date="2026-09-04",
    ),
    Podcast(
        key="hacking_humans",
        name="Hacking Humans",
        feed_url="https://feeds.megaphone.fm/hacking-humans",
        episode_path="hacking-humans",
        baseline_episode=401,
        baseline_date="2026-09-03",
    ),
)


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            state = json.load(handle)
    else:
        state = {"feeds": {}}

    feeds = state.setdefault("feeds", {})
    for podcast in PODCASTS:
        feed_state = feeds.setdefault(podcast.key, {})
        feed_state.setdefault("last_episode_number", podcast.baseline_episode)
        feed_state.setdefault("last_published_date", podcast.baseline_date)
        feed_state.setdefault("seen_ids", [])
    return state


def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temp_path.replace(path)


def extract_episode_number(podcast: Podcast, entry: Any) -> int | None:
    candidates = [
        str(entry.get("link", "")),
        str(entry.get("title", "")),
        str(entry.get("id", "")),
    ]
    path_pattern = rf"/podcasts/{re.escape(podcast.episode_path)}/(\d+)(?:/|$)"
    title_patterns = (
        r"\bEp(?:isode)?\.?\s*#?\s*(\d+)\b",
        r"\bEpisode\s+(\d+)\b",
    )
    for value in candidates:
        match = re.search(path_pattern, value, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    official_link = candidates[0].lower()
    if "thecyberwire.com/podcasts/" in official_link:
        return None
    for value in candidates:
        for pattern in title_patterns:
            match = re.search(pattern, value, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
    return None


def clean_html(raw: str) -> str:
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def get_notes(entry: Any) -> str:
    content = entry.get("content") or []
    if content and content[0].get("value"):
        return clean_html(content[0]["value"])
    return clean_html(str(entry.get("summary", "")))


def get_audio_url(entry: Any) -> str | None:
    for enclosure in entry.get("enclosures", []):
        href = enclosure.get("href") or enclosure.get("url")
        media_type = str(enclosure.get("type", ""))
        if href and (media_type.startswith("audio/") or not media_type):
            return str(href)
    for link in entry.get("links", []):
        href = link.get("href")
        if href and (
            link.get("rel") == "enclosure"
            or str(link.get("type", "")).startswith("audio/")
        ):
            return str(href)
    return None


def normalize_page_url(podcast: Podcast, entry: Any, number: int) -> str:
    link = str(entry.get("link", "")).strip()
    if link and "thecyberwire.com" in link:
        return link.rstrip("/")
    return f"https://thecyberwire.com/podcasts/{podcast.episode_path}/{number}/notes"


def published_date(entry: Any) -> str:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc).date().isoformat()
    raw = str(entry.get("published", entry.get("updated", "公開日不明")))
    return raw


def episode_from_entry(podcast: Podcast, entry: Any) -> Episode | None:
    number = extract_episode_number(podcast, entry)
    if number is None:
        return None
    page_url = normalize_page_url(podcast, entry, number)
    entry_id = str(entry.get("id") or entry.get("guid") or page_url)
    return Episode(
        podcast=podcast,
        number=number,
        title=html.unescape(str(entry.get("title", f"Episode {number}"))).strip(),
        published=published_date(entry),
        page_url=page_url,
        audio_url=get_audio_url(entry),
        notes=get_notes(entry),
        entry_id=entry_id,
    )


def fetch_feed(podcast: Podcast) -> list[Episode]:
    response = HTTP.get(podcast.feed_url, timeout=30)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"RSS parse failed for {podcast.name}: {parsed.bozo_exception}")
    episodes = [episode_from_entry(podcast, entry) for entry in parsed.entries]
    return [episode for episode in episodes if episode is not None]


def select_new_episodes(
    episodes: Iterable[Episode], feed_state: dict[str, Any], test_mode: bool = False
) -> list[Episode]:
    episodes = list(episodes)
    if not episodes:
        return []
    if test_mode:
        return [max(episodes, key=episode_sort_key)]

    last_number = int(feed_state["last_episode_number"])
    last_date = parse_iso_date(str(feed_state["last_published_date"]))
    seen = set(feed_state.get("seen_ids", []))
    selected = [
        episode
        for episode in episodes
        if episode_sort_key(episode) > (last_date, last_number)
        and episode.entry_id not in seen
    ]
    return sorted(selected, key=episode_sort_key)


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return date.min


def episode_sort_key(episode: Episode) -> tuple[date, int]:
    return parse_iso_date(episode.published), episode.number


def transcript_url(episode: Episode) -> str:
    base = episode.page_url.rstrip("/")
    base = re.sub(r"/(?:notes|transcript)$", "", base)
    return f"{base}/transcript"


def fetch_transcript(episode: Episode) -> str | None:
    response = HTTP.get(transcript_url(episode), timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    transcript = soup.select_one("div.transcript")
    if transcript is None:
        return None
    text = clean_html(str(transcript))
    return text if len(text) >= 500 else None


def build_prompt(episode: Episode, source_type: str, source_text: str | None) -> str:
    source_instruction = (
        "以下の公式Transcriptだけを番組内容の根拠として使ってください。"
        if source_type == "公式Transcript"
        else "添付された公式音声を番組内容の根拠として使ってください。"
        if source_type == "公式音声"
        else "以下の公式Show Notesだけを番組内容の根拠として使ってください。"
    )
    source_block = f"\n\n--- 公式資料 ---\n{source_text}" if source_text else ""
    return f"""
あなたはサイバーセキュリティ分野の日本語解説者です。
英語の逐語訳ではなく、初学者が背景から理解できる、正確で読みやすい日本語にしてください。
{source_instruction}

番組名: {episode.podcast.name}
エピソード番号: {episode.number}
原題: {episode.title}
公開日: {episode.published}
公式ページ: {episode.page_url}

次の見出しをこの順で必ず使ってください。
## 3文で概要
正確に3つの箇条書きで、全体像を説明する。

## 主要トピック
重要な話題を3〜6項目。それぞれ短い説明を付ける。

## 用語と背景
理解に必要な専門用語や事件背景を、初学者向けに説明する。

## 重要な論点
なぜ重要か、攻撃側・防御側・社会への影響を整理する。

## ソフトウェア・AI・セキュリティ学習との接点
開発者や学習者が何を学べるかを具体的に示す。

## 実生活・開発で意識すること
読者が今日から取れる行動を、現実的な箇条書きで示す。

制約:
- 番組中の事実と一般的な背景解説を混同しない。背景を補足する場合は「背景」と明示する。
- 確認できない内容を推測で埋めない。「公式資料からは確認できない」と書く。
- URL、APIキー、個人情報、広告文を創作しない。
- Markdownで4,500〜5,000字以内を目安にする。
{source_block}
""".strip()


def wait_for_file(client: genai.Client, uploaded: Any) -> Any:
    for _ in range(60):
        state = getattr(uploaded, "state", None)
        state_name = getattr(state, "name", str(state or ""))
        if state_name != "PROCESSING":
            if state_name == "FAILED":
                raise RuntimeError("Gemini audio processing failed")
            return uploaded
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)
    raise TimeoutError("Gemini audio processing did not finish in time")


def generate_text_summary(client: genai.Client, episode: Episode, source_type: str, text: str) -> str:
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=build_prompt(episode, source_type, text),
    )
    if not response.text:
        raise RuntimeError("Gemini returned an empty response")
    return response.text.strip()


def download_audio(url: str) -> Path:
    suffix = Path(urlparse(url).path).suffix or ".mp3"
    with HTTP.get(url, stream=True, timeout=(30, 300)) as response:
        response.raise_for_status()
        content_length = int(response.headers.get("content-length", 0))
        if content_length > MAX_AUDIO_BYTES:
            raise RuntimeError(f"Audio is too large: {content_length} bytes")
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            path = Path(handle.name)
            total = 0
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_AUDIO_BYTES:
                    path.unlink(missing_ok=True)
                    raise RuntimeError(f"Audio exceeds {MAX_AUDIO_BYTES} bytes")
                handle.write(chunk)
    return path


def generate_audio_summary(client: genai.Client, episode: Episode) -> str:
    if not episode.audio_url:
        raise RuntimeError("RSS did not include an audio enclosure")
    audio_path = download_audio(episode.audio_url)
    uploaded = None
    try:
        uploaded = client.files.upload(file=audio_path)
        uploaded = wait_for_file(client, uploaded)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[build_prompt(episode, "公式音声", None), uploaded],
        )
        if not response.text:
            raise RuntimeError("Gemini returned an empty response")
        return response.text.strip()
    finally:
        audio_path.unlink(missing_ok=True)
        if uploaded is not None and getattr(uploaded, "name", None):
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                logging.warning("Could not delete uploaded Gemini file", exc_info=True)


def summarize_episode(client: genai.Client, episode: Episode) -> tuple[str, str]:
    try:
        transcript = fetch_transcript(episode)
    except requests.RequestException:
        logging.warning("Transcript fetch failed; trying audio", exc_info=True)
        transcript = None

    if transcript:
        return generate_text_summary(client, episode, "公式Transcript", transcript), "公式Transcript"

    try:
        return generate_audio_summary(client, episode), "公式音声"
    except Exception:
        logging.warning("Audio summary failed; trying Show Notes", exc_info=True)
        if len(episode.notes) < 100:
            raise
        return generate_text_summary(client, episode, "公式Show Notes", episode.notes), "公式Show Notes"


def split_text(text: str, limit: int = 3_500) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > limit:
            split_at = paragraph.rfind("\n", 0, limit)
            if split_at < limit // 2:
                split_at = paragraph.rfind("。", 0, limit)
                split_at = split_at + 1 if split_at >= limit // 2 else limit
            chunks.append(paragraph[:split_at].strip())
            paragraph = paragraph[split_at:].strip()
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


def post_to_discord(
    webhook_url: str,
    episode: Episode,
    summary: str,
    source_type: str,
    test_mode: bool = False,
) -> None:
    label = "🧪 テスト投稿" if test_mode else "🎙️ 新着エピソード"
    content = (
        f"{label} | **{episode.podcast.name} Ep {episode.number}**\n"
        f"**{episode.title}**\n"
        f"公開日: {episode.published}\n"
        f"🔗 {episode.page_url}"
    )
    if len(summary) > MAX_DISCORD_SUMMARY_CHARS:
        summary = summary[: MAX_DISCORD_SUMMARY_CHARS - 20].rstrip() + "\n\n（以下省略）"
    chunks = split_text(summary)
    embeds = []
    for index, chunk in enumerate(chunks):
        embed: dict[str, Any] = {
            "description": chunk,
            "color": 0x2563EB,
        }
        if index == len(chunks) - 1:
            embed["footer"] = {"text": f"内容の根拠: {source_type}"}
        embeds.append(embed)

    response = HTTP.post(
        webhook_url,
        json={
            "username": "CyberWire 日本語解説",
            "content": content,
            "embeds": embeds,
            "allowed_mentions": {"parse": []},
        },
        timeout=30,
    )
    response.raise_for_status()


def mark_processed(feed_state: dict[str, Any], episode: Episode) -> None:
    current_key = (
        parse_iso_date(str(feed_state["last_published_date"])),
        int(feed_state["last_episode_number"]),
    )
    if episode_sort_key(episode) > current_key:
        feed_state["last_episode_number"] = episode.number
        feed_state["last_published_date"] = episode.published[:10]
    seen = list(feed_state.get("seen_ids", []))
    if episode.entry_id not in seen:
        seen.append(episode.entry_id)
    feed_state["seen_ids"] = seen[-100:]
    feed_state["last_processed_at"] = datetime.now(timezone.utc).isoformat()


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    api_key = require_env("GEMINI_API_KEY")
    webhook_url = require_env("DISCORD_WEBHOOK_URL")
    state = load_state()
    client = genai.Client(api_key=api_key)
    processed = 0

    for podcast in PODCASTS:
        logging.info("Checking %s", podcast.name)
        episodes = fetch_feed(podcast)
        feed_state = state["feeds"][podcast.key]
        pending = select_new_episodes(episodes, feed_state, TEST_MODE)
        if not pending:
            logging.info("No new episodes for %s", podcast.name)
            continue

        for episode in pending:
            logging.info("Processing %s Ep %s", podcast.name, episode.number)
            try:
                summary, source_type = summarize_episode(client, episode)
                post_to_discord(webhook_url, episode, summary, source_type, TEST_MODE)
            except Exception:
                logging.exception("Episode processing failed; leaving it unprocessed")
                break
            if not TEST_MODE:
                mark_processed(feed_state, episode)
                save_state(state)
            processed += 1

    logging.info("Done. Posted %d episode(s).", processed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
