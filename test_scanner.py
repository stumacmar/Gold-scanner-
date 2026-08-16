"""Unit tests for the pure parsing / assessment functions in gold_ring_scanner.

These cover the coverage-expansion logic that's easy to get subtly wrong:
multilingual fineness marks, European comma decimals + unit variants, the
weight-confidence model (confirmed/estimated/unknown), conservative net-gold
stone subtraction, and the 15g-vs-12g floor branching.

Run with:  python3 -m unittest test_scanner -v
"""

import json
import os
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


class TestNewArrivals(unittest.TestCase):
    """first_seen / is_new: today's fresh listings pin to the top."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "scan.json")

    def _write_prev(self, results):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"meta": {}, "results": results}, fh)

    def test_baseline_run_flags_nothing_new(self):
        # No previous file -> establish a baseline, don't badge everything.
        recs = [{"url": "u1"}, {"url": "u2"}]
        g.mark_new_arrivals(recs, self.path, "T0")
        self.assertTrue(all(not r["is_new"] for r in recs))
        self.assertTrue(all(r["first_seen"] == "T0" for r in recs))

    def test_unseen_url_is_new(self):
        self._write_prev([{"url": "u1", "first_seen": "T0"}])
        recs = [{"url": "u1"}, {"url": "u2"}]
        g.mark_new_arrivals(recs, self.path, "T1")
        by = {r["url"]: r for r in recs}
        self.assertFalse(by["u1"]["is_new"])
        self.assertTrue(by["u2"]["is_new"])

    def test_first_seen_carries_forward(self):
        # A ring that persists keeps its ORIGINAL first_seen, not today's.
        self._write_prev([{"url": "u1", "first_seen": "T0"}])
        recs = [{"url": "u1"}]
        g.mark_new_arrivals(recs, self.path, "T5")
        self.assertEqual(recs[0]["first_seen"], "T0")

    def test_previous_file_without_first_seen(self):
        # Upgrading from pre-feature data: present == not new, no false flood.
        self._write_prev([{"url": "u1"}])
        recs = [{"url": "u1"}, {"url": "u2"}]
        g.mark_new_arrivals(recs, self.path, "T1")
        by = {r["url"]: r for r in recs}
        self.assertFalse(by["u1"]["is_new"])
        self.assertEqual(by["u1"]["first_seen"], "T1")   # backfilled
        self.assertTrue(by["u2"]["is_new"])

    def test_corrupt_previous_file_is_baseline(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        recs = [{"url": "u1"}]
        g.mark_new_arrivals(recs, self.path, "T1")
        self.assertFalse(recs[0]["is_new"])

    # --- identity: eBay urls are NOT stable between scans -------------------

    def test_item_id_parsed_from_url(self):
        self.assertEqual(
            g.ebay_item_id("https://www.ebay.co.uk/itm/227385454335?_skw=x&hash=y"),
            "227385454335")
        self.assertIsNone(g.ebay_item_id("https://example.com/nope"))

    def test_same_listing_different_url_is_not_new(self):
        # REGRESSION: eBay embeds the search keyword that found the listing
        # (`_skw=`) plus a rotating `amdata` blob, so the SAME ring has a
        # different url each scan. Keying on url reported every old listing as
        # a new arrival -- 13 of 13 false positives in one live run.
        self._write_prev([{
            "item_id": "227385454335", "first_seen": "T0",
            "url": "https://www.ebay.co.uk/itm/227385454335?_skw=yurman+signet"}])
        recs = [{
            "item_id": "227385454335",
            "url": "https://www.ebay.co.uk/itm/227385454335?_skw=david+yurman+mens+ring&amdata=ZZZ"}]
        g.mark_new_arrivals(recs, self.path, "T1")
        self.assertFalse(recs[0]["is_new"])
        self.assertEqual(recs[0]["first_seen"], "T0")

    def test_identity_falls_back_to_url_id_when_field_absent(self):
        # Older data files have no item_id: parse it out of the url instead.
        self._write_prev([{
            "url": "https://www.ebay.com/itm/157892974261?_skw=a", "first_seen": "T0"}])
        recs = [{"url": "https://www.ebay.com/itm/157892974261?_skw=b"}]
        g.mark_new_arrivals(recs, self.path, "T1")
        self.assertFalse(recs[0]["is_new"])

    def test_genuinely_different_listing_is_new(self):
        self._write_prev([{"item_id": "111", "url": "https://ebay.com/itm/111"}])
        recs = [{"item_id": "111", "url": "https://ebay.com/itm/111?_skw=q"},
                {"item_id": "222", "url": "https://ebay.com/itm/222"}]
        g.mark_new_arrivals(recs, self.path, "T1")
        by = {r["item_id"]: r for r in recs}
        self.assertFalse(by["111"]["is_new"])
        self.assertTrue(by["222"]["is_new"])


class TestIngotMode(unittest.TestCase):
    """Bullion: denominations, fakes, accessories and wrong-metal bars."""

    def test_standard_denominations(self):
        self.assertAlmostEqual(
            g.parse_bullion_weight("1oz Gold Bullion Bar 999.9 PAMP"), 31.103, places=2)
        self.assertEqual(g.parse_bullion_weight("100g Umicore Gold Bar 999.9"), 100.0)
        self.assertEqual(g.parse_bullion_weight("Silver Bar 1kg 999 Umicore"), 1000.0)

    def test_fineness_marks_are_not_weights(self):
        # "999" / "9999" must never be read as a gram figure.
        self.assertIsNone(g.parse_bullion_weight("Gold bar 999.9 fine bullion"))

    def test_fraction_overrides_a_stray_ounce(self):
        # REGRESSION: "Germania Mint 1/100 24k Gold Bar 1 Troy Oz" was read as
        # a full ounce (31.1g) and flagged as 97% under melt.
        self.assertAlmostEqual(
            g.parse_bullion_weight("Germania Mint 1/100 24k Gold Bullion Bar 1 Troy Oz"),
            0.311, places=2)

    def test_golden_state_mint_is_not_gold(self):
        # REGRESSION: substring matching put "Golden State Mint 1oz 999 Fine
        # SILVER Bar" on the gold screen, melt-valued as an ounce of gold.
        self.assertTrue(g.is_ingot_excluded(
            "Golden State Mint 1oz 999 Fine Silver Bar Bullion", "ingot_gold"))
        self.assertFalse(g.is_ingot_excluded(
            "Golden State Mint 1oz 999 Fine Silver Bar Bullion", "ingot_silver"))

    def test_accessories_excluded(self):
        # A holder "for 1 oz PAMP" is not an ounce of gold; nor is a mould.
        for t in ("Sterling Silver bezel frame for 1 oz PAMP Lunar Gold",
                  "Graphite Ingot Mold For Casting 10 Gram Gold Bar",
                  "Display case for 1oz gold bullion bars"):
            self.assertTrue(g.is_ingot_excluded(t, "ingot_gold"), t)

    def test_plated_and_replica_excluded(self):
        for t in ("1oz gold plated bullion bar replica",
                  "24k gold clad novelty bar", "gold foil bar souvenir"):
            self.assertTrue(g.is_ingot_excluded(t, "ingot_gold"), t)

    def test_novelty_and_feedstock_excluded(self):
        # "Finished in 24K GOLD" is plated; a decoy bar is a fake by name;
        # nuggets/scrap are refining feedstock, not a bar. All were showing
        # as 96-99% "under melt".
        for t in ("The Ned Kelly Gang 1oz Ingot Finished in 24K GOLD",
                  "1oz Gold Bar (Decoy) 10 Bars",
                  "100g Scrap gold recovery nuggets for refining"):
            self.assertTrue(g.is_ingot_excluded(t, "ingot_gold"), t)

    def test_denomination_snapping(self):
        self.assertEqual(g.snap_to_denomination(31.2), 31.1035)   # within 3%
        self.assertEqual(g.snap_to_denomination(100.0), 100.0)
        self.assertEqual(g.snap_to_denomination(311.0), 311.035)  # 10 troy oz IS real
        self.assertIsNone(g.snap_to_denomination(7.0))            # not a denomination
        self.assertIsNone(g.snap_to_denomination(0.0311))         # 1/1000oz novelty

    def test_multilingual_metal_words(self):
        # German bullion is one word -- excluding it would drop DE/AT/CH.
        self.assertFalse(g.is_ingot_excluded("1 Gramm Goldbarren PAMP Suisse", "ingot_gold"))
        self.assertFalse(g.is_ingot_excluded("100g Silberbarren 999 Feinsilber", "ingot_silver"))

    def test_base_metal_bars_excluded(self):
        # "Silver Coast" is a brand; the bar is copper.
        self.assertTrue(g.is_ingot_excluded("1 Kilo Silver Coast Copper Bar", "ingot_silver"))
        self.assertTrue(g.is_ingot_excluded("2 x 1KG COPPER BULLION BAR 99%", "ingot_silver"))

    def test_investment_gold_is_vat_exempt(self):
        # UK VAT Notice 701.21: investment gold carries no VAT, so charging
        # 20% overstated every imported gold bar by a fifth. Silver bullion
        # gets no relief.
        gold = g.landed_cost(2200, "US", vat_exempt=True)
        silver = g.landed_cost(2200, "US", vat_exempt=False)
        self.assertLess(gold, silver)
        self.assertAlmostEqual(gold, 2200 + g.POSTAGE_EST_GBP["US"]
                               + g.IMPORT_HANDLING_FEE_GBP, places=2)

    def test_early_auction_price_is_not_meaningful(self):
        # A 1oz Perth Mint bar opening at £556 with 6 days to run says
        # nothing about its sale price -- these topped the "best value" sort.
        self.assertFalse(g.price_is_meaningful("Auction", 0, "6d 11h"))
        self.assertFalse(g.price_is_meaningful("Auction", 22, "21h 49m"))
        self.assertTrue(g.price_is_meaningful("Auction", 26, "9h 32m"))
        self.assertTrue(g.price_is_meaningful("Buy now", "", "n/a"))

    def test_genuine_bullion_kept(self):
        self.assertFalse(g.is_ingot_excluded(
            "1oz Gold Bullion Bar 999.9 PAMP Suisse", "ingot_gold"))
        self.assertFalse(g.is_ingot_excluded(
            "100g Silver Bullion Bar 999 Metalor", "ingot_silver"))


class TestScrapMode(unittest.TestCase):
    def test_ewaste_and_ore_excluded(self):
        for x in ("CPU pins gold recovery scrap e-waste",
                  "gold ore refining ore sample", "motherboard gold scrap"):
            self.assertTrue(g.is_scrap_excluded(x), x)

    def test_gold_filled_and_plated_excluded(self):
        # The commonest trap in scrap listings: filled/rolled is not solid.
        for x in ("9ct GOLD FILLED scrap bundle", "gold plated scrap job lot",
                  "rolled gold scrap lot"):
            self.assertTrue(g.is_scrap_excluded(x), x)

    def test_real_scrap_kept(self):
        self.assertFalse(g.is_scrap_excluded("9ct gold scrap jewellery 22.4g broken chains"))
        self.assertFalse(g.is_scrap_excluded("375 scrap gold 18.9 grams broken rings"))

    def test_job_lots_allowed_in_scrap_only(self):
        # A job lot of broken gold is the ideal scrap listing, but a job lot
        # of RINGS breaks single-item melt maths.
        old = g.METAL
        try:
            g.METAL = "scrap"
            self.assertFalse(g.is_scrap_excluded("Job lot 9ct gold scrap broken jewellery 31g"))
            g.METAL = "gold"
            self.assertTrue(g.is_excluded("18k Gold Rings Bundle 40.9g"))
        finally:
            g.METAL = old

    def test_scrap_weight_floor_and_stone_allowance(self):
        old = g.METAL
        try:
            g.METAL = "scrap"
            self.assertTrue(g.assess_ring("9ct scrap gold 22g", 9, 22.0)["keep"])
            self.assertFalse(g.assess_ring("9ct scrap gold 3g", 9, 3.0)["keep"])
            a = g.assess_ring("9ct scrap gold 20g with diamond", 9, 20.0)
            self.assertAlmostEqual(a["net_gold_g"], 20.0 - g.STONE_ALLOWANCE_G, places=1)
        finally:
            g.METAL = old


class TestBrandMode(unittest.TestCase):
    """Named-brand signets: Yurman's peers plus the signet houses."""

    def test_makers_detected(self):
        self.assertEqual(g.detect_brand_maker("Elizabeth Gage 18ct Templar ring"),
                         "Elizabeth Gage")
        self.assertEqual(g.detect_brand_maker("John Hardy sterling signet ring"),
                         "John Hardy")
        self.assertEqual(g.detect_brand_maker("Konstantino 18k signet ring"),
                         "Konstantino")
        self.assertEqual(g.detect_brand_maker("Deakin & Francis signet ring"),
                         "Deakin & Francis")
        self.assertIsNone(g.detect_brand_maker("heavy 18ct gold signet ring"))

    def test_city_name_is_not_the_brand_lagos(self):
        """"Lagos" alone is a city -- every alias must carry a second word."""
        self.assertIsNone(g.detect_brand_maker("gold signet ring from Lagos Nigeria"))
        self.assertEqual(g.detect_brand_maker("Lagos Caviar sterling signet ring"),
                         "Lagos")

    def test_lookalikes_and_unsigned_excluded(self):
        for x in ("sterling signet ring in the style of John Hardy",
                  "signet ring attributed to Elizabeth Gage",
                  "unsigned Konstantino style gold signet ring",
                  "Chrome Hearts inspired gold plated signet ring"):
            self.assertTrue(g.is_brand_excluded(x), x)

    def test_only_rings(self):
        # These brands make plenty of brooches, cuffs and cufflinks.
        for x in ("Elizabeth Gage signet brooch gold",
                  "John Hardy signet cufflinks sterling",
                  "Konstantino signet bracelet sterling",
                  "Shaun Leane intaglio necklace gold"):
            self.assertTrue(g.is_brand_excluded(x), x)

    def test_genuine_signed_signets_kept(self):
        for x in ("Elizabeth Gage 18ct gold intaglio ring",
                  "John Hardy Classic Chain sterling silver signet ring",
                  "Konstantino sterling and 18k gold signet ring",
                  "Deakin & Francis silver signet ring",
                  "Longmire London 18ct gold armorial signet ring",
                  "Tom Wood signet ring 9k gold",
                  "Chrome Hearts sterling silver signet ring"):
            self.assertFalse(g.is_brand_excluded(x), x)

    def test_brand_but_not_a_signet_rejected(self):
        """The screen is branded SIGNETS -- cocktail rings flooded it once."""
        for x in ("John Hardy Classic Chain sterling band ring",
                  "Konstantino sterling silver cocktail ring",
                  "Gurhan 24k gold stacking ring",
                  "Stephen Webster 18k gold eternity band ring"):
            self.assertTrue(g.is_brand_excluded(x), x)

    def test_counterfeit_language_rejected(self):
        """Yurman's peers are among the most faked marks on eBay."""
        for x in ("Chrome Hearts signet ring, not authentic aftermarket",
                  "John Hardy style signet ring designer inspired",
                  "Konstantino signet ring, faux gold"):
            self.assertTrue(g.is_brand_excluded(x), x)

    def test_plated_lines_rejected(self):
        """Tom Wood and Lagos both sell gold-plated silver -- not the target."""
        for x in ("Tom Wood gold plated silver signet ring",
                  "Lagos Caviar vermeil signet ring",
                  "John Hardy gold-filled signet ring"):
            self.assertTrue(g.is_brand_excluded(x), x)

    def test_first_live_run_leaks_closed(self):
        """Four things the first real brand scan let through."""
        for x in (# hyphenated style claim beat "style of"/"in the style"
                  "Sterling Signet Ring, John Hardy-Style Brushed Finish",
                  # the seller themselves is unsure of the attribution
                  "Greek maker KONSTANTINO? 18K Yellow Gold Signet Ring",
                  # vermeil is silver under a gold wash, not a solid piece
                  "Konstantino Argento Sterling 925 Vermeil signet US 7",
                  # an empty box carrying the maker's name
                  "Elizabet Gage Green leather and Velvet Ring Box"):
            self.assertTrue(g.is_brand_excluded(x), x)

    def test_ring_sold_with_its_box_still_kept(self):
        """The accessory filter must not eat rings that include their box."""
        for x in ("John Hardy sterling signet ring in original box",
                  "Konstantino 18k gold signet ring with presentation box",
                  "Tom Wood 9k gold signet ring with case, vintage"):
            self.assertFalse(g.is_brand_excluded(x), x)

    def test_hard_accessories_rejected_even_with_metal_words(self):
        for x in ("John Hardy sterling silver signet ring box, empty box",
                  "Konstantino gold signet rings catalogue 1998",
                  "Elizabeth Gage 18ct signet display box only"):
            self.assertTrue(g.is_brand_excluded(x), x)


class TestScottishMode(unittest.TestCase):
    """Provenance screen: clan crests, cairngorm, Scottish agate, Iona."""

    def test_genuine_scottish_signets_kept(self):
        for x in ("Victorian Scottish agate signet ring silver seal",
                  "9ct gold clan crest signet ring Edinburgh hallmark",
                  "Antique cairngorm set gold seal ring Scottish",
                  "Alexander Ritchie Iona silver Celtic knot signet ring",
                  "Ortak sterling silver signet ring Orkney",
                  "Hamilton & Inches 18ct gold crest ring",
                  "9ct gold signet ring hallmarked Edinburgh 1974",
                  "Luckenbooth silver seal ring Scottish sterling"):
            self.assertFalse(g.is_scottish_excluded(x), x)

    def test_pewter_and_base_metal_tat_rejected(self):
        """Clan-crest rings sell by the thousand in pewter at a tenner."""
        for x in ("Clan crest ring pewter Scottish souvenir",
                  "Stainless steel Scottish thistle signet ring",
                  "Scottish clan crest ring gold plated"):
            self.assertTrue(g.is_scottish_excluded(x), x)

    def test_seller_location_is_not_an_assay_mark(self):
        """Half the Scottish listings on eBay merely SHIP from Glasgow."""
        self.assertTrue(g.is_scottish_excluded("Signet ring, ships from Glasgow"))
        self.assertTrue(g.is_scottish_excluded("Gold seal ring, Edinburgh seller"))
        self.assertFalse(
            g.is_scottish_excluded("9ct gold signet ring, Edinburgh assay 1932"))

    def test_iona_needs_a_word_boundary(self):
        """"iona" hides inside "Fiona"; "clan" inside "clanking"."""
        self.assertTrue(g.is_scottish_excluded("Fiona's silver signet ring"))
        self.assertIsNone(g.scottish_signal("a fiona ring"))

    def test_must_be_a_signet(self):
        for x in ("Scottish silver thistle ring",
                  "Ortak silver Celtic band ring"):
            self.assertTrue(g.is_scottish_excluded(x), x)

    def test_must_be_a_ring(self):
        for x in ("Scottish thistle brooch silver",
                  "Sheila Fleet silver pendant Orkney",
                  "Scottish clan crest cufflinks silver"):
            self.assertTrue(g.is_scottish_excluded(x), x)

    def test_style_claims_rejected(self):
        """On a provenance screen any style claim is a disqualifier."""
        for x in ("Iona style silver signet ring",
                  "Celtic style clan crest signet ring silver",
                  "Scottish-style gold seal ring"):
            self.assertTrue(g.is_scottish_excluded(x), x)

    def test_signal_is_reported(self):
        self.assertEqual(g.scottish_signal("Ortak sterling signet ring"), "maker")
        self.assertEqual(g.scottish_signal("9ct signet hallmarked Edinburgh"),
                         "hallmark")
        self.assertEqual(g.scottish_signal("cairngorm gold seal ring"), "motif")
        self.assertIsNone(g.scottish_signal("plain gold signet ring"))

    def test_makers_detected(self):
        self.assertEqual(g.detect_scottish_maker("Hamilton and Inches gold ring"),
                         "Hamilton & Inches")
        self.assertEqual(g.detect_scottish_maker("Ola M Gorie silver ring"),
                         "Ola Gorie")
        self.assertIsNone(g.detect_scottish_maker("plain 9ct signet ring"))

    def test_celtic_flood_closed(self):
        """A first live run returned 88 rings, 85 of them pagan-silver tat."""
        for x in ("Bear Paw Celtic Knot 925 Real Silver Ring Signet",
                  "Celtic Knot Triskelion Triquetra Sterling Silver 925 Signet",
                  "Snake Dragon 925 Sterling Silver Signet Celtic Knot Ouroboros",
                  "Pentagram with Runes 925 Silver Ring Celtic Knot Futhark Signet",
                  "Nordic Viking Celtic Knot Fenrir Wolf Head Silver Signet",
                  "Silver Celtic Knot Poison Signet Ring"):
            self.assertTrue(g.is_scottish_excluded(x), x)

    def test_celtic_alone_is_not_a_scottish_signal(self):
        """Celtic is pan-Celtic; only a named Scottish maker redeems it."""
        self.assertIsNone(g.scottish_signal("sterling silver celtic knot ring"))
        self.assertEqual(
            g.scottish_signal("Alexander Ritchie Iona celtic knot ring"), "maker")

    def test_scottish_rite_is_freemasonry_not_scotland(self):
        """A false friend that filled a third of the second live run."""
        for x in ("Gold Freemason Shriners Scottish Rite Men's Signet Ring",
                  "Gold Freimaurer Shriners Schottischer Ritus Herren Siegelring",
                  "Anillo de oro con sello Shriners de rito escoces para hombre",
                  "Anello uomo con sigillo rito scozzese massoni oro",
                  "32 degree Scottish rite 10K Yellow Gold Signet Ring"):
            self.assertTrue(g.is_scottish_excluded(x), x)

    def test_made_to_order_bulk_gold_rejected(self):
        """A choice of carat means a workshop taking orders, not a find."""
        for x in ("18 Kt, 22 Kt Real Solid Yellow Gold Scottish Thistle Signet",
                  "Scottish clan crest gold signet ring, made to order any size"):
            self.assertTrue(g.is_scottish_excluded(x), x)

    def test_real_finds_survive_every_filter(self):
        """The four genuine antiques the live run actually surfaced."""
        for x in ("Gents 9ct Gold & Scottish Banded Carnelian Agate Signet Ring",
                  "Antique 9K 9ct Gold Signet Ring Scottish Hallmark Shield Shape",
                  "Antique Scottish 18ct Gold Green Agate set Glasgow Signet Ring c1920",
                  "925 Sterling Silver Rampant Lion Scottish Mens Signet Ring"):
            self.assertFalse(g.is_scottish_excluded(x), x)

    def test_no_weight_or_melt_in_provenance_mode(self):
        old = g.METAL
        g.METAL = "scottish"
        try:
            a = g.assess_ring("Ortak silver signet ring Orkney", None, None)
            self.assertTrue(a["keep"])
        finally:
            g.METAL = old


class TestPriceHistory(unittest.TestCase):
    """Achieved prices banked from our own repeated observations."""

    def setUp(self):
        import tempfile
        self.path = os.path.join(tempfile.mkdtemp(), "hist.json")

    def _rec(self, iid, price, **kw):
        r = {"item_id": iid, "url": f"https://ebay.co.uk/itm/{iid}",
             "title": f"ring {iid}", "metal": "gold", "current_bid": price,
             "landed_cost": price + 4, "buying": "Buy now", "bids": "",
             "best_offer": False, "price_firm": True}
        r.update(kw)
        return r

    def test_tracks_live_items(self):
        h = g.update_price_history([self._rec("1", 100)], "T0", self.path)
        self.assertEqual(h["items"]["1"]["status"], "active")
        self.assertEqual(h["items"]["1"]["first_price"], 100)

    def test_price_movement_recorded(self):
        g.update_price_history([self._rec("1", 100)], "T0", self.path)
        h = g.update_price_history([self._rec("1", 80)], "T1", self.path)
        e = h["items"]["1"]
        self.assertEqual((e["first_price"], e["last_price"]), (100, 80))

    def test_vanished_item_banks_an_achieved_price(self):
        g.update_price_history([self._rec("1", 100), self._rec("2", 200)], "T0", self.path)
        h = g.update_price_history([self._rec("1", 100)], "T1", self.path, metal="gold")
        gone = h["items"]["2"]
        self.assertEqual(gone["status"], "ended")
        self.assertEqual(gone["achieved_price"], 200)
        self.assertEqual(gone["outcome"], "gone (sold at asking, or withdrawn)")

    def test_best_offer_price_is_a_ceiling_not_an_achieved_price(self):
        # 45% of signet listings run Best Offer. The seller may accept far
        # below asking and eBay never publishes the figure, so recording
        # asking as "achieved" would bias the database upward.
        g.update_price_history(
            [self._rec("1", 1000, best_offer=True)], "T0", self.path)
        h = g.update_price_history([], "T1", self.path, metal="gold")
        e = h["items"]["1"]
        self.assertEqual(e["price_grade"], "ceiling only")
        self.assertIsNone(e["achieved_price"])      # never published as achieved
        self.assertEqual(e["ceiling_price"], 1000)  # but kept as an upper bound

    def test_plain_fixed_price_is_usable(self):
        g.update_price_history([self._rec("1", 900)], "T0", self.path)
        h = g.update_price_history([], "T1", self.path, metal="gold")
        self.assertEqual(h["items"]["1"]["price_grade"], "asking")
        self.assertEqual(h["items"]["1"]["achieved_price"], 900)

    def test_auction_seen_late_beats_one_seen_early(self):
        # An auction last seen days out kept bidding after we looked.
        g.update_price_history(
            [self._rec("1", 500, buying="Auction", bids=8, price_firm=True),
             self._rec("2", 400, buying="Auction", bids=2, price_firm=False)],
            "T0", self.path)
        h = g.update_price_history([], "T1", self.path, metal="gold")
        self.assertEqual(h["items"]["1"]["price_grade"], "settled")
        self.assertEqual(h["items"]["1"]["achieved_price"], 500)
        self.assertEqual(h["items"]["2"]["price_grade"], "under-observed")
        self.assertIsNone(h["items"]["2"]["achieved_price"])

    def test_auction_outcome_depends_on_bids(self):
        g.update_price_history(
            [self._rec("1", 50, buying="Auction", bids=7),
             self._rec("2", 50, buying="Auction", bids=0)], "T0", self.path)
        h = g.update_price_history([], "T1", self.path, metal="gold")
        self.assertEqual(h["items"]["1"]["outcome"], "sold (bid)")
        self.assertEqual(h["items"]["2"]["outcome"], "ended, no bids")

    def test_other_screens_are_not_closed_by_this_scan(self):
        # A gold scan must not mark every silver item as ended.
        g.update_price_history([self._rec("1", 100),
                                self._rec("9", 90, metal="silver")], "T0", self.path)
        h = g.update_price_history([self._rec("1", 100)], "T1", self.path, metal="gold")
        self.assertEqual(h["items"]["9"]["status"], "active")


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
