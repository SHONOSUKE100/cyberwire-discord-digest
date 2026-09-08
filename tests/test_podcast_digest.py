import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from google.genai import errors as genai_errors

from podcast_digest import (
    Episode,
    PODCASTS,
    configured_gemini_models,
    extract_episode_number,
    generate_with_model_fallback,
    mark_processed,
    post_to_discord,
    select_new_episodes,
    split_text,
    transcript_url,
)


class Entry(dict):
    __getattr__ = dict.__getitem__


class PodcastDigestTests(unittest.TestCase):
    def episode(
        self,
        number: int,
        entry_id: str | None = None,
        published: str = "2026-09-07",
    ) -> Episode:
        podcast = PODCASTS[0]
        return Episode(
            podcast=podcast,
            number=number,
            title=f"Episode {number}",
            published=published,
            page_url=f"https://thecyberwire.com/podcasts/daily-podcast/{number}/notes",
            audio_url="https://example.com/audio.mp3",
            notes="notes",
            entry_id=entry_id or f"ep-{number}",
        )

    def test_extracts_number_from_official_url(self):
        entry = Entry(
            link="https://thecyberwire.com/podcasts/daily-podcast/2631/notes",
            title="A title without a number",
            id="guid",
        )
        self.assertEqual(extract_episode_number(PODCASTS[0], entry), 2631)

    def test_extracts_number_from_title(self):
        entry = Entry(link="https://example.com", title="Ep. 402: Social engineering", id="x")
        self.assertEqual(extract_episode_number(PODCASTS[1], entry), 402)

    def test_rejects_cross_promoted_official_episode(self):
        entry = Entry(
            link="https://thecyberwire.com/podcasts/other-show/9999/notes",
            title="Episode 9999",
            id="x",
        )
        self.assertIsNone(extract_episode_number(PODCASTS[0], entry))

    def test_selects_only_unseen_new_episodes_in_order(self):
        state = {
            "last_episode_number": 2630,
            "last_published_date": "2026-09-04",
            "seen_ids": [],
        }
        episodes = [
            self.episode(2632, published="2026-09-06"),
            self.episode(2630, published="2026-09-04"),
            self.episode(2631, published="2026-09-05"),
        ]
        self.assertEqual(
            [episode.number for episode in select_new_episodes(episodes, state)],
            [2631, 2632],
        )

    def test_test_mode_selects_latest_episode(self):
        state = {
            "last_episode_number": 9999,
            "last_published_date": "2026-09-07",
            "seen_ids": [],
        }
        episodes = [
            self.episode(11617, published="2020-09-04"),
            self.episode(2632, published="2026-09-07"),
            self.episode(2631, published="2026-09-06"),
        ]
        self.assertEqual(select_new_episodes(episodes, state, True)[0].number, 2632)

    def test_old_legacy_number_is_not_treated_as_new(self):
        state = {
            "last_episode_number": 2630,
            "last_published_date": "2026-09-04",
            "seen_ids": [],
        }
        old_episode = self.episode(11617, published="2020-09-04")
        self.assertEqual(select_new_episodes([old_episode], state), [])

    def test_transcript_url_replaces_notes(self):
        self.assertEqual(
            transcript_url(self.episode(2631)),
            "https://thecyberwire.com/podcasts/daily-podcast/2631/transcript",
        )

    def test_split_text_respects_limit_and_preserves_content(self):
        text = "A" * 80 + "\n\n" + "B" * 80
        chunks = split_text(text, limit=100)
        self.assertTrue(all(len(chunk) <= 100 for chunk in chunks))
        self.assertEqual("\n\n".join(chunks), text)

    def test_discord_summary_is_sent_in_one_webhook_request(self):
        response = Mock(status_code=204, text="")
        with patch("podcast_digest.HTTP.post", return_value=response) as post:
            post_to_discord(
                "https://discord.example/webhook",
                self.episode(2631),
                "A" * 5_000,
                "公式Transcript",
                "gemini-3.8-flash",
            )
        payload = post.call_args.kwargs["json"]
        self.assertEqual(post.call_count, 1)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertLessEqual(sum(len(item["description"]) for item in payload["embeds"]), 5_500)

    def test_discord_uses_plain_messages_after_embed_400(self):
        bad_response = Mock(status_code=400, text='{"message":"Invalid Form Body"}')
        good_response = Mock(status_code=204, text="")
        with patch(
            "podcast_digest.HTTP.post",
            side_effect=[bad_response, good_response],
        ) as post:
            post_to_discord(
                "https://discord.example/webhook",
                self.episode(2631),
                "短い解説",
                "公式Transcript",
                "gemini-3.7-flash",
            )
        self.assertEqual(post.call_count, 2)
        self.assertNotIn("embeds", post.call_args.kwargs["json"])

    def test_gemini_503_falls_back_to_next_flash_model(self):
        calls = []

        def generate_content(*, model, contents):
            calls.append(model)
            if model == "gemini-3.8-flash":
                raise genai_errors.ServerError(
                    503,
                    {"error": {"code": 503, "status": "UNAVAILABLE"}},
                )
            return SimpleNamespace(text="成功")

        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
        with patch.dict(
            os.environ,
            {"GEMINI_MODELS": "gemini-3.8-flash,gemini-3.7-flash"},
        ):
            text, model = generate_with_model_fallback(client, "prompt")
        self.assertEqual(text, "成功")
        self.assertEqual(model, "gemini-3.7-flash")
        self.assertEqual(calls, ["gemini-3.8-flash", "gemini-3.7-flash"])

    def test_lite_model_is_rejected(self):
        with patch.dict(
            os.environ,
            {"GEMINI_MODELS": "gemini-3.8-flash,gemini-3.5-flash-lite"},
        ):
            with self.assertRaisesRegex(RuntimeError, "non-Lite"):
                configured_gemini_models()

    def test_mark_processed_updates_number_and_deduplicates_id(self):
        state = {
            "last_episode_number": 2630,
            "last_published_date": "2026-09-04",
            "seen_ids": [],
        }
        episode = self.episode(2631)
        mark_processed(state, episode)
        mark_processed(state, episode)
        self.assertEqual(state["last_episode_number"], 2631)
        self.assertEqual(state["last_published_date"], "2026-09-07")
        self.assertEqual(state["seen_ids"], ["ep-2631"])


if __name__ == "__main__":
    unittest.main()
