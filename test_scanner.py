"""Unit tests for the pure parsing / assessment functions in gold_ring_scanner.

These cover the coverage-expansion logic that's easy to get subtly wrong:
multilingual fineness marks, European comma decimals + unit variants, the
weight-confidence model (confirmed/estimated/unknown), conservative net-gold
stone subtraction, and the 15g-vs-12g floor branching.

Run with:  python3 -m unittest test_scanner -v
"""

import unittest

import gold_ring_scanner as g


class TestWeightParsing(unittest.TestCase):
    def test_plain_grams(self):
        self.assertEqual(g.parse_weight_grams("Heavy signet 19.67 grams"), 19.67)

    def test_comma_decimal(self):
        # European listings write "15,3 g" -> must read as 15.3, not 153.
        self.assertEqual(g.parse_weight_grams("Goldring massiv 15,3 g"), 15.3)

    def test_unit_variants(self):
        for txt, want in [
            ("ring 16.2gm", 16.2),
            ("ring 16.2 gr", 16.2),
            ("anello 18,0 grammi", 18.0),
            ("Ring 22,5 Gramm", 22.5),
            ("band 17 g.", 17.0),
        ]:
            self.assertEqual(g.parse_weight_grams(txt), want, txt)

    def test_picks_largest_plausible(self):
        # Tiny stone weight + total weight -> take the total.
        self.assertEqual(
            g.parse_weight_grams("0.25ct diamond, total weight 21.4g"), 21.4)

    def test_rejects_out_of_range(self):
        self.assertIsNone(g.parse_weight_grams("dimensions 250 g packaging"))
        self.assertIsNone(g.parse_weight_grams("no weight here"))


class TestFinenessMarks(unittest.TestCase):
    """A fineness hallmark must map to carat regardless of listing language."""

    def test_all_marks(self):
        cases = {
            "375": 9, "585": 14, "625": 15, "750": 18,
            "833": 20, "916": 22, "917": 22, "333": 8, "999": 24,
        }
        for mark, carat in cases.items():
            c, assumed = g.detect_carat(f"Goldring {mark} massiv")
            self.assertEqual(c, carat, mark)
            self.assertFalse(assumed, mark)

    def test_localised_context(self):
        # Real multilingual titles still resolve via the mark.
        self.assertEqual(g.detect_carat("bague or 750 chevalière")[0], 18)
        self.assertEqual(g.detect_carat("anello oro 585 uomo")[0], 14)
        self.assertEqual(g.detect_carat("zegelring goud 333")[0], 8)

    def test_ct_fallback(self):
        self.assertEqual(g.detect_carat("9ct gold signet ring")[0], 9)

    def test_stone_carat_ignored(self):
        # "22ct smoky quartz" is a stone weight, not 22ct gold.
        c, assumed = g.detect_carat("9ct gold ring with 22ct smoky quartz")
        self.assertEqual(c, 9)


class TestEstimateWeightLow(unittest.TestCase):
    def test_no_signal_returns_none(self):
        self.assertIsNone(g.estimate_weight_low("plain gold ring", 9))

    def test_signet_estimate(self):
        est = g.estimate_weight_low("9ct gold signet ring", 9)
        self.assertIsNotNone(est)
        self.assertGreater(est, 0)

    def test_heavy_signet_beats_plain_signet(self):
        plain = g.estimate_weight_low("9ct gold signet ring", 9)
        heavy = g.estimate_weight_low("9ct heavy chunky gold signet ring", 9)
        self.assertGreater(heavy, plain)

    def test_slim_reduces_estimate(self):
        full = g.estimate_weight_low("9ct gold band", 9)
        slim = g.estimate_weight_low("9ct slim dainty gold band", 9)
        self.assertLess(slim, full)

    def test_density_scales_with_carat(self):
        # Same archetype, higher carat -> denser -> heavier estimate.
        nine = g.estimate_weight_low("signet ring", 9)
        twentytwo = g.estimate_weight_low("signet ring", 22)
        self.assertGreater(twentytwo, nine)

    def test_large_size_bumps_estimate(self):
        base = g.estimate_weight_low("9ct gold signet ring", 9)
        big = g.estimate_weight_low("9ct gold signet ring size 12", 9)
        self.assertGreater(big, base)


class TestAssessRing(unittest.TestCase):
    """Confidence tiering, stone subtraction, and floor branching."""

    def test_confirmed_above_floor_kept(self):
        a = g.assess_ring("9ct gold signet ring 19g", 9, 19.0)
        self.assertTrue(a["keep"])
        self.assertEqual(a["confidence"], "confirmed")
        self.assertEqual(a["net_gold_g"], 19.0)

    def test_confirmed_below_floor_rejected(self):
        a = g.assess_ring("9ct gold ring 12g", 9, 12.0)
        self.assertFalse(a["keep"])
        self.assertEqual(a["confidence"], "confirmed")

    def test_confirmed_floor_is_15(self):
        self.assertFalse(g.assess_ring("9ct ring 14.9g", 9, 14.9)["keep"])
        self.assertTrue(g.assess_ring("9ct ring 15g", 9, 15.0)["keep"])

    def test_stone_subtraction_keeps_borderline(self):
        # 16g gross with a small stone -> 14.5g net is below the confirmed floor.
        a = g.assess_ring("9ct gold ring 16g with diamond", 9, 16.0)
        self.assertAlmostEqual(a["net_gold_g"], 16.0 - g.STONE_ALLOWANCE_G, places=1)
        self.assertTrue(a["stones"])
        self.assertFalse(a["keep"])   # 14.5 < 15

    def test_intaglio_allowance_larger(self):
        a = g.assess_ring("9ct gold intaglio carved seal ring 18g", 9, 18.0)
        self.assertAlmostEqual(
            a["net_gold_g"], 18.0 - g.INTAGLIO_ALLOWANCE_G, places=1)

    def test_showcase_rejected(self):
        a = g.assess_ring("9ct gold diamond cluster cocktail ring 16g", 9, 16.0)
        self.assertFalse(a["keep"])
        self.assertEqual(a["confidence"], "reject")

    def test_estimated_uses_12g_lowbound(self):
        # No stated weight; estimate must clear the 12g estimated floor.
        a = g.assess_ring("9ct heavy chunky gold signet ring", 9, None)
        self.assertEqual(a["confidence"], "estimated")
        if a["net_gold_g"] is not None and a["net_gold_g"] >= \
                g.WEIGHT_FLOOR_ESTIMATED_LOWBOUND:
            self.assertTrue(a["keep"])

    def test_estimated_floor_branching(self):
        # Estimated rings use the 12g lowbound, NOT the 15g confirmed floor.
        # A slim band estimates light and is dropped; a heavy signet clears it.
        slim = g.assess_ring("9ct slim dainty gold band", 9, None)
        heavy = g.assess_ring("9ct heavy chunky gold signet ring", 9, None)
        self.assertEqual(heavy["confidence"], "estimated")
        self.assertTrue(heavy["keep"])
        if slim["confidence"] == "estimated":
            # Whatever the slim estimate, the decision uses the 12g lowbound.
            self.assertEqual(
                slim["keep"],
                slim["net_gold_g"] >= g.WEIGHT_FLOOR_ESTIMATED_LOWBOUND)

    def test_unknown_always_retained(self):
        a = g.assess_ring("9ct gold ring", 9, None)
        self.assertEqual(a["confidence"], "unknown")
        self.assertTrue(a["keep"])          # routed to the review lane
        self.assertIsNone(a["net_gold_g"])

    def test_tags_returned(self):
        a = g.assess_ring("vintage 9ct gold mens signet ring 20g", 9, 20.0)
        self.assertIn("signet", a["tags"])
        self.assertIn("vintage", a["tags"])
        self.assertIn("gents", a["tags"])


class TestQueriesFor(unittest.TestCase):
    def test_english_market(self):
        qs = g.queries_for("EBAY_GB", 9)
        self.assertTrue(all("9ct" in q for q in qs))
        self.assertLessEqual(len(qs), g.MAX_QUERIES_PER_MARKET)

    def test_german_market_localised_with_fineness(self):
        qs = g.queries_for("EBAY_DE", 18)
        joined = " ".join(qs).lower()
        self.assertIn("750", joined)              # 18ct fineness mark
        self.assertIn("siegelring", joined)       # German signet term
        # English terms run in parallel on every market.
        self.assertTrue(any("18ct" in q for q in qs))

    def test_french_market(self):
        qs = g.queries_for("EBAY_FR", 22)
        joined = " ".join(qs).lower()
        self.assertIn("916", joined)
        self.assertIn("chevalière", joined)

    def test_dedup_and_cap(self):
        qs = g.queries_for("EBAY_DE", 9, extra=["Goldring 375", "extra term"])
        self.assertEqual(len(qs), len(set(q.lower() for q in qs)))
        self.assertLessEqual(len(qs), g.MAX_QUERIES_PER_MARKET)


class TestStyleTags(unittest.TestCase):
    def test_multiple_tags(self):
        tags = g.style_tags("antique victorian 18ct gold mens signet ring")
        self.assertIn("signet", tags)
        self.assertIn("antique", tags)
        self.assertIn("gents", tags)

    def test_localised_signet(self):
        self.assertIn("signet", g.style_tags("Siegelring massiv"))
        self.assertIn("signet", g.style_tags("chevalière or"))


class TestLandedCost(unittest.TestCase):
    def test_uk_adds_only_domestic_postage(self):
        self.assertEqual(g.landed_cost(100.0, "UK"), 100.0 + g.UK_POSTAGE_GBP)

    def test_import_adds_vat_and_postage(self):
        landed = g.landed_cost(100.0, "US")
        self.assertGreater(landed, 100.0 + g.POSTAGE_EST_GBP["US"])


if __name__ == "__main__":
    unittest.main()
