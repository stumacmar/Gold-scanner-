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

    def test_size_number_not_weight(self):
        # Regression: "misura 15 g. 6,20" is Italian for "size 15, 6.20g" --
        # a 6.2g ring was recorded as 15g and flagged value. With the
        # size-guard + digit-guard it now parses as UNKNOWN (dropped in
        # strict mode) -- no weight beats a wrong weight.
        self.assertIsNone(
            g.parse_weight_grams("oro 750 Citrino misura 15 g. 6,20"))
        self.assertIsNone(g.parse_weight_grams("size 12 gold ring"))

    def test_unit_first_formats(self):
        self.assertEqual(g.parse_weight_grams("anello oro grammi 12,5"), 12.5)
        self.assertEqual(g.parse_weight_grams("bague or g. 6,20"), 6.2)

    def test_german_size_not_weight(self):
        # "Gr. 60" is German for SIZE 60 -- must not parse as 60 grams.
        self.assertIsNone(g.parse_weight_grams("Goldring 585 Gr. 60"))


class TestSilverMode(unittest.TestCase):
    def test_fineness_marks(self):
        self.assertEqual(g.detect_silver_fineness("Siegelring Silber 925")[0], 0.925)
        self.assertEqual(g.detect_silver_fineness("chevalière argent 800")[0], 0.800)
        self.assertEqual(g.detect_silver_fineness("silver ring 835 continental")[0], 0.835)

    def test_sterling_word_means_925(self):
        frac, mark, assumed = g.detect_silver_fineness("sterling silver signet ring")
        self.assertEqual((frac, mark, assumed), (0.925, "925", False))

    def test_no_mark_assumes_925(self):
        frac, mark, assumed = g.detect_silver_fineness("solid silver signet ring")
        self.assertEqual((frac, assumed), (0.925, True))

    def test_gold_ring_excluded_from_silver(self):
        # A gold signet mentioning silver must not leak into the silver screen.
        self.assertTrue(g.is_silver_excluded("9ct gold signet ring with silver box"))
        self.assertTrue(g.is_silver_excluded("Goldring 585 Siegelring"))

    def test_plated_and_vermeil_excluded(self):
        self.assertTrue(g.is_silver_excluded("silver plated signet ring"))
        self.assertTrue(g.is_silver_excluded("vermeil sterling signet ring"))

    def test_solid_sterling_kept(self):
        self.assertFalse(g.is_silver_excluded("sterling silver signet ring 925 22g"))


class TestYurmanMode(unittest.TestCase):
    def test_non_brand_search_noise_excluded(self):
        self.assertTrue(g.is_yurman_excluded("sterling silver signet ring mens"))

    def test_lookalikes_excluded(self):
        # "Genuine brand claims only" -- style/inspired/replica are rejected.
        self.assertTrue(g.is_yurman_excluded("David Yurman style cable signet ring"))
        self.assertTrue(g.is_yurman_excluded("signet ring inspired by Yurman"))
        self.assertTrue(g.is_yurman_excluded("Yurman replica mens ring 925"))

    def test_genuine_claim_kept(self):
        self.assertFalse(g.is_yurman_excluded(
            "David Yurman Streamline Signet Ring Sterling Silver 925 Size 10"))

    def test_no_weight_needed_in_brand_mode(self):
        # Brand hunt: a weightless Yurman signet is still kept.
        old = g.METAL
        g.METAL = "yurman"
        try:
            a = g.assess_ring("David Yurman signet ring sterling", None, None)
            self.assertTrue(a["keep"])
        finally:
            g.METAL = old


class TestClassifySeller(unittest.TestCase):
    def test_private_individual(self):
        t, fb, priv = g.classify_seller(
            {"sellerAccountType": "INDIVIDUAL", "feedbackScore": 883})
        self.assertEqual((t, fb, priv), ("private", 883, True))

    def test_business_never_private(self):
        t, fb, priv = g.classify_seller(
            {"sellerAccountType": "BUSINESS", "feedbackScore": 45181})
        self.assertEqual((t, priv), ("business", False))

    def test_big_feedback_individual_is_a_shop(self):
        # "Individual" accounts with huge feedback are shops in practice.
        _, _, priv = g.classify_seller(
            {"sellerAccountType": "INDIVIDUAL", "feedbackScore": 25000})
        self.assertFalse(priv)

    def test_top_rated_never_private(self):
        _, _, priv = g.classify_seller(
            {"sellerAccountType": "INDIVIDUAL", "feedbackScore": 200},
            top_rated=True)
        self.assertFalse(priv)

    def test_missing_seller(self):
        t, fb, priv = g.classify_seller(None)
        self.assertEqual((t, fb, priv), (None, 0, False))


class TestPlatedAndMixed(unittest.TestCase):
    def test_multilingual_silver_mix_excluded(self):
        # Regression: "Anello Uomo Argento 18k 750" (silver+gold Yurman) was
        # melt-valued on its full weight -- most of that weight is silver.
        self.assertTrue(g.is_plated("David Yurman Anello Uomo Argento 18k 750 Sigillo"))
        self.assertTrue(g.is_plated("Siegelring Silber vergoldet 925"))
        self.assertFalse(g.is_plated("9ct yellow gold signet ring 19g"))

    def test_weight_then_unit_then_size(self):
        # Regression: "4,5 Gramm 61" is 4.5g, RING SIZE 61 -- the unit-first
        # pass read "Gramm 61" as 61g and flagged 4.5g lapis rings as 57g
        # value candidates across DE/AT/CH/IT.
        self.assertEqual(g.parse_weight_grams("Siegelring 585 Lapislazuli 4,5 Gramm 61"), 4.5)
        self.assertEqual(g.parse_weight_grams("Anello Sigillo 585 Lapis 5,6 Grammi 60 Misura"), 5.6)
        self.assertEqual(g.parse_weight_grams("Siegelring Carneol 585er Gold 10,3 Gramm 19,5 mm"), 10.3)


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

    def test_8k_and_20ct(self):
        # Regression: "8K Gold Signet" fell through to the scan's default
        # carat, overstating melt by ~75%.
        self.assertEqual(g.detect_carat("Vintage European 8K Gold Signet Ring")[0], 8)
        self.assertEqual(g.detect_carat("20ct gold band")[0], 20)

    def test_multilingual_stones(self):
        # Regression: Italian/German stone words slipped through has_stones,
        # so a 56g tourmaline statement ring was melt-valued as all-gold.
        self.assertTrue(g.has_stones("Anello Designer In Ametista Rosa 585"))
        self.assertTrue(g.has_stones("18kt Gelbgold Diamanten grüne Turmaline"))
        self.assertTrue(g.has_stones("bague or émeraude 750"))
        self.assertFalse(g.has_stones("9ct gold signet ring plain"))

    def test_stone_carat_ignored(self):
        # "22ct smoky quartz" is a stone weight, not 22ct gold.
        c, assumed = g.detect_carat("9ct gold ring with 22ct smoky quartz")
        self.assertEqual(c, 9)


class TestEstimateWeightLow(unittest.TestCase):
    def test_no_signal_returns_none(self):
        self.assertIsNone(g.estimate_weight_low("plain gold ring", 9))

    def test_plain_signet_returns_none(self):
        # Regression: a plain "signet ring" carries NO weight information.
        # Archetype-only guesses polluted the value list in a live run.
        self.assertIsNone(g.estimate_weight_low("9ct gold signet ring", 9))

    def test_solid_is_not_heavy(self):
        # Regression: "solid gold" means not-plated, not heavy. A ladies'
        # small solid signet must not be estimated at 12g+.
        self.assertIsNone(
            g.estimate_weight_low("9ct Solid Yellow Gold Signet Ring Small Oval", 9))
        # Same for the continental equivalents.
        self.assertIsNone(g.estimate_weight_low("Goldring massiv 585 Siegelring", 14))
        self.assertIsNone(g.estimate_weight_low("anello oro massiccio sigillo", 18))

    def test_heavy_signet_estimates(self):
        est = g.estimate_weight_low("9ct heavy chunky gold signet ring", 9)
        self.assertIsNotNone(est)
        self.assertGreater(est, 0)

    def test_slim_reduces_estimate(self):
        full = g.estimate_weight_low("9ct heavy gold band", 9)
        slim = g.estimate_weight_low("9ct slim dainty gold band", 9)
        self.assertLess(slim, full)

    def test_fine_gold_is_not_slim(self):
        # "fine gold" = pure gold, not a slim ring -> no weight signal at all.
        self.assertIsNone(g.estimate_weight_low("22ct fine gold signet ring", 22))

    def test_density_scales_with_carat(self):
        # Same archetype, higher carat -> denser -> heavier estimate.
        nine = g.estimate_weight_low("heavy signet ring", 9)
        twentytwo = g.estimate_weight_low("heavy signet ring", 22)
        self.assertGreater(twentytwo, nine)

    def test_large_size_bumps_estimate(self):
        base = g.estimate_weight_low("9ct heavy gold signet ring", 9)
        big = g.estimate_weight_low("9ct heavy gold signet ring size 12", 9)
        self.assertGreater(big, base)


class TestAssessRing(unittest.TestCase):
    """Strict mode: signet/intaglio only, confirmed weight inside 15-20g."""

    def test_signet_in_window_kept(self):
        a = g.assess_ring("9ct gold signet ring 19g", 9, 19.0)
        self.assertTrue(a["keep"])
        self.assertEqual(a["confidence"], "confirmed")
        self.assertEqual(a["net_gold_g"], 19.0)

    def test_window_boundaries(self):
        # 15g and up: below the floor is dropped, heavy rings are kept.
        self.assertFalse(g.assess_ring("9ct signet ring 14.9g", 9, 14.9)["keep"])
        self.assertTrue(g.assess_ring("9ct signet ring 15g", 9, 15.0)["keep"])
        self.assertTrue(g.assess_ring("9ct signet ring 22g", 9, 22.0)["keep"])
        self.assertTrue(g.assess_ring("9ct signet ring 41g", 9, 41.0)["keep"])

    def test_non_signet_rejected(self):
        # A heavy plain band in the window is still off-target: signets and
        # intaglios only.
        a = g.assess_ring("9ct heavy gold band 18g hallmarked", 9, 18.0)
        self.assertFalse(a["keep"])
        self.assertEqual(a["confidence"], "off-target")

    def test_intaglio_counts_as_target(self):
        a = g.assess_ring("18ct gold intaille ring 19g", 18, 19.0)
        self.assertTrue(a["keep"])

    def test_stone_subtraction_applies_to_window(self):
        # 16g gross with a small stone -> 14.5g net gold falls below the floor.
        a = g.assess_ring("9ct gold signet ring 16g with diamond", 9, 16.0)
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

    def test_multi_gem_showcase_rejected(self):
        # Regression: "Diamanten grüne Turmaline" statement ring, 56g counted
        # as all gold. Two distinct gem families = the gold is just the mount.
        a = g.assess_ring(
            "18kt Gelbgold Ring Diamanten grüne Turmaline 750 Statement 56g",
            18, 56.0)
        self.assertFalse(a["keep"])
        self.assertEqual(a["confidence"], "reject")

    def test_single_gem_signet_still_kept(self):
        # One stone family is normal for a signet -- must NOT be rejected.
        a = g.assess_ring("9ct gold bloodstone signet ring 19g", 9, 19.0)
        self.assertTrue(a["keep"])

    def test_no_stated_weight_dropped_in_strict_mode(self):
        # Strict mode: no seller-stated weight -> not shown, even for a
        # heavy-sounding signet (estimates are never a substitute).
        heavy = g.assess_ring("9ct heavy chunky gold signet ring", 9, None)
        plain = g.assess_ring("vintage 9ct gold signet ring size P", 9, None)
        self.assertFalse(heavy["keep"])
        self.assertFalse(plain["keep"])

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


class TestValueFlag(unittest.TestCase):
    """The buy signal itself: only a CONFIRMED weight may flag value."""

    def test_confirmed_below_threshold_flags(self):
        self.assertTrue(g.value_flag(1000.0, 1200.0, "confirmed"))

    def test_confirmed_above_threshold_does_not(self):
        self.assertFalse(g.value_flag(1000.0, 1400.0, "confirmed"))

    def test_estimated_never_flags(self):
        # Regression: 224 of 232 value flags in a live run rested on guessed
        # weights. A buy signal must never come from an estimate.
        self.assertFalse(g.value_flag(1000.0, 500.0, "estimated"))

    def test_unknown_never_flags(self):
        self.assertFalse(g.value_flag(1000.0, 500.0, "unknown"))

    def test_missing_inputs_never_flag(self):
        self.assertFalse(g.value_flag(None, 500.0, "confirmed"))
        self.assertFalse(g.value_flag(1000.0, None, "confirmed"))

    def test_too_good_to_be_true_demoted(self):
        # Regression: a 56g "gold" ring at 4.7x melt was a gem showcase whose
        # stones were counted as gold. Nobody sells at <40% of scrap.
        self.assertFalse(g.value_flag(4000.0, 865.0, "confirmed"))
        # Just under the suspect line still flags.
        self.assertTrue(g.value_flag(2000.0, 900.0, "confirmed"))


class TestLandedCost(unittest.TestCase):
    def test_uk_adds_only_domestic_postage(self):
        self.assertEqual(g.landed_cost(100.0, "UK"), 100.0 + g.UK_POSTAGE_GBP)

    def test_import_adds_vat_and_postage(self):
        landed = g.landed_cost(100.0, "US")
        self.assertGreater(landed, 100.0 + g.POSTAGE_EST_GBP["US"])


if __name__ == "__main__":
    unittest.main()
