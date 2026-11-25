import time
import unittest

import update_news as engine


def article(article_id, title, published_ms, **overrides):
    value = {
        "id": article_id,
        "title": title,
        "summary": title,
        "content": title,
        "category": "දේශීය පුවත්",
        "image_url": "https://example.com/image.jpg",
        "published_at": "2026-10-30 10:00:00",
        "published_at_ms": published_ms,
        "fetched_at_ms": published_ms,
        "source_name": "Test",
        "source_url": f"https://example.com/{article_id}",
    }
    value.update(overrides)
    return value


class NewsEngineTests(unittest.TestCase):
    def test_irrelevant_foreign_story_is_filtered(self):
        value = article(
            "foreign-1",
            "ඉන්දියාවේ ප්‍රාදේශීය උත්සවයක් අද පැවැත්වෙයි",
            int(time.time() * 1000),
        )
        self.assertFalse(engine.should_keep_article(value))

    def test_major_global_story_is_kept_but_not_notified_by_default(self):
        value = article(
            "foreign-2",
            "ලොව විශාලතම හුවමාරුව දැවැන්ත සයිබර් හැක් කිරීමකට ලක්වෙයි",
            int(time.time() * 1000),
        )
        self.assertTrue(engine.should_keep_article(value))
        scored = engine.calculate_importance(value)
        self.assertEqual(scored["category"], "විදේශීය පුවත්")
        self.assertEqual(scored["importance_tier"], "feed_only")

    def test_major_sri_lanka_sports_win_is_high_priority(self):
        value = article(
            "sport-1",
            "ශ්‍රී ලංකා කණ්ඩායම ලෝක කුසලාන අවසන් මහා තරගය ජයගනියි",
            int(time.time() * 1000),
            category="ක්‍රීඩා පුවත්",
        )
        scored = engine.calculate_importance(value)
        self.assertIn(scored["importance_tier"], {"high", "urgent"})

    def test_ordinary_sports_update_stays_feed_only(self):
        value = article(
            "sport-2",
            "ශ්‍රී ලංකා කණ්ඩායම තරගයේ කාසියේ වාසිය දිනා ගනියි",
            int(time.time() * 1000),
            category="ක්‍රීඩා පුවත්",
        )
        scored = engine.calculate_importance(value)
        self.assertEqual(scored["importance_tier"], "feed_only")

    def test_sri_lanka_match_defeat_stays_feed_only(self):
        value = article(
            "sport-defeat",
            "පාකිස්තානයට ලකුණු 82ක දැවැන්ත ජයක් – ශ්‍රී ලංකාවට පරාජය",
            int(time.time() * 1000),
            category="ක්‍රීඩා පුවත්",
        )
        scored = engine.calculate_importance(value)
        self.assertEqual(scored["importance_tier"], "feed_only")

    def test_history_merge_prunes_stale_items_and_sorts_newest_first(self):
        now_ms = int(time.time() * 1000)
        fresh = article("new", "ඉන්ධන මිල සංශෝධනය අද ප්‍රකාශයට", now_ms - 60_000)
        retained = article("kept", "මධ්‍යම කඳුකරයට තද වැසි", now_ms - 3_600_000)
        stale = article("old", "පැරණි දේශීය පුවතක්", now_ms - 8 * 24 * 3_600_000)
        merged = engine.merge_with_history([fresh], [retained, stale], now_ms)
        self.assertEqual([item["id"] for item in merged], ["new", "kept"])

    def test_similar_cross_source_title_is_already_notified(self):
        value = article(
            "source-b",
            "ඉන්ධන මිල අද මධ්‍යම රාත්‍රියේ සිට පහළට",
            int(time.time() * 1000),
        )
        self.assertTrue(
            engine.was_story_notified(
                value,
                ["අද මධ්‍යම රාත්‍රියේ සිට ඉන්ධන මිල පහළට"],
            )
        )

    def test_fuel_gas_price_and_emergency_alerts_receive_high_priority(self):
        fuel = article("fuel-1", "අද මධ්‍යම රාත්‍රියේ සිට ඉන්ධන මිල සහ ගෑස් මිල ඉහළට", int(time.time() * 1000))
        scored = engine.calculate_importance(fuel)
        self.assertIn(scored["importance_tier"], {"high", "urgent", "emergency"})
        self.assertGreaterEqual(scored["importance_score"], 70)

        lightning = article("disaster-1", "දිස්ත්‍රික්ක 17කට ප්‍රබල අකුණු පිළිබඳ අනතුරු ඇඟවීම්", int(time.time() * 1000))
        scored_disaster = engine.calculate_importance(lightning)
        self.assertIn(scored_disaster["importance_tier"], {"high", "urgent", "emergency"})

    def test_routine_political_statements_stay_feed_only(self):
        speech = article(
            "pol-1",
            "වත්මන් ආණ්ඩුවේ අපේක්ෂාව දිළිඳුබව සම්පූර්ණයෙන් අවසන් කිරීමයි – අමාත්‍ය උපාලි පන්නිල",
            int(time.time() * 1000)
        )
        scored = engine.calculate_importance(speech)
        self.assertEqual(scored["importance_tier"], "feed_only")
        self.assertLess(scored["importance_score"], 40)

    def test_major_cyber_attack_receives_high_priority(self):
        cia_hack = article(
            "cyber-1",
            "CIA පරිගණක පද්ධතියකට සයිබර් ප්‍රහාරයක්",
            int(time.time() * 1000)
        )
        scored = engine.calculate_importance(cia_hack)
        self.assertIn(scored["importance_tier"], {"high", "urgent"})
        self.assertGreaterEqual(scored["importance_score"], 55)

    def test_utility_tariff_proposal_receives_high_priority(self):
        tariff = article(
            "tariff-1",
            "විදුලි ගාස්තු සංශෝධන යෝජනාව මහජන උපයෝගිතා කොමිසමට ඉදිරිපත් කෙරේ",
            int(time.time() * 1000)
        )
        scored = engine.calculate_importance(tariff)
        self.assertIn(scored["importance_tier"], {"high", "urgent"})
        self.assertGreaterEqual(scored["importance_score"], 70)

    def test_space_mission_stays_feed_only_no_notification(self):
        space = article(
            "space-1",
            "Shenzhou-24 අභ්‍යවකාශ යානය සාර්ථකව ගුවන්ගත කෙරේ – පාකිස්තාන ගගනගාමියෙක් ඉතිහාසයට",
            int(time.time() * 1000)
        )
        scored = engine.calculate_importance(space)
        self.assertEqual(scored["importance_tier"], "feed_only")
        self.assertEqual(scored["category"], "විශේෂාංග")
        self.assertLessEqual(scored["importance_score"], 45)


if __name__ == "__main__":
    unittest.main()
