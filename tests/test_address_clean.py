import csv
import hashlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from address_clean import clean_address
from risk import Order, PhoneHistory, assess_order, score_order
from gateway import hash_phone


class AddressCleanTests(unittest.TestCase):
    # Verify table of 30 plus addresses.
    def test_table_of_30_plus_addresses(self):
        path = Path(__file__).parent / "addresses.csv"
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertGreaterEqual(len(rows), 30)
        for row in rows:
            with self.subTest(address=row["address"]):
                result = clean_address(row["address"])
                self.assertEqual(result["city"] or "", row["expected_city"])
                self.assertEqual(result["area"] or "", row["expected_area"])
                self.assertTrue(set(filter(None, row["expected_flag"].split("|"))).issubset(result["flags"]))
                self.assertGreaterEqual(result["quality_score"], 0)
                self.assertLessEqual(result["quality_score"], 1)

    # Verify cleaning twice is identical.
    def test_cleaning_twice_is_identical(self):
        address = "House 5, DHA Phase 5, Lahore"
        self.assertEqual(clean_address(address), clean_address(address))

    # Verify compact house block society city address.
    def test_compact_house_block_society_city_address(self):
        result = clean_address("341 A1 DHA Lahore")
        self.assertEqual(result["house_no"], "341")
        self.assertEqual(result["block_phase_sector"], "Block A1")
        self.assertEqual(result["area"], "DHA")
        self.assertEqual(result["city"], "Lahore")
        self.assertNotIn("missing_house_number", result["flags"])
        self.assertGreaterEqual(result["quality_score"], 0.9)

    # Verify equivalent spellings have the same hash.
    def test_equivalent_spellings_have_the_same_hash(self):
        first = clean_address("House 5, DHA Phase 5, Lahore")
        second = clean_address("H#5 D.H.A. Ph V LHR")
        self.assertEqual(first["addr_hash"], second["addr_hash"])

    # Verify phone and address hashes share configured salt.
    def test_phone_and_address_hashes_share_configured_salt(self):
        with patch.dict(os.environ, {"HASH_SALT": "test-salt"}):
            phone = hash_phone("0300-1234567")
            address = clean_address("House 5, DHA Phase 5, Lahore")
            expected_phone = hashlib.sha256(b"test-salt923001234567").hexdigest()
            expected_address = hashlib.sha256(b"test-salt5|phase 5|dha|lahore").hexdigest()
        self.assertEqual(phone, expected_phone)
        self.assertEqual(address["addr_hash"], expected_address)

    # Verify handles empty none and emoji.
    def test_handles_empty_none_and_emoji(self):
        for value in ("", None, "🏠🙂", "😀 123 🧭"):
            with self.subTest(value=value):
                result = clean_address(value)
                self.assertEqual(len(result["addr_hash"]), 64)
                self.assertGreaterEqual(result["quality_score"], 0)
                self.assertLessEqual(result["quality_score"], 1)

    # Verify urdu script city area and house number.
    def test_urdu_script_city_area_and_house_number(self):
        address = "".join(map(chr, [0x06af, 0x06be, 0x0631, 0x0020, 0x0035, 0x060c, 0x0020,
                                    0x0688, 0x06cc, 0x0020, 0x0627, 0x06cc, 0x0686, 0x0020,
                                    0x0627, 0x06d2, 0x060c, 0x0020, 0x0644, 0x0627, 0x06c1,
                                    0x0648, 0x0631]))
        result = clean_address(address)
        self.assertEqual(result["house_no"], "5")
        self.assertEqual(result["area"], "DHA")
        self.assertEqual(result["city"], "Lahore")

    # Verify target risk examples.
    def test_target_risk_examples(self):
        quality = clean_address("House 5, DHA Phase 5, Lahore")
        score, level, _ = score_order(1000, "House 5, DHA Phase 5, Lahore", True, 12, 0, False,
                                      quality["quality_score"], quality["flags"])
        self.assertEqual(score, 0.1)
        self.assertEqual(level, "low")
        dha = clean_address("DHA")
        score, level, reasons = score_order(1000, "DHA", True, 12, 0, False,
                                            dha["quality_score"], dha["flags"])
        self.assertEqual(score, 0.53)
        self.assertEqual(level, "medium")
        self.assertTrue(any(flag in " ".join(reasons) for flag in dha["flags"]))

    # Verify old score order call shape and expected score.
    def test_old_score_order_call_shape_and_expected_score(self):
        score, level, reasons = score_order(12000, "house", True, 3, 3, True)
        self.assertEqual(score, 0.524)
        self.assertEqual(level, "medium")
        self.assertTrue(reasons)

    # Verify original order assessment compatibility.
    def test_original_order_assessment_compatibility(self):
        good = "House 12, Street 5, Gulberg III, Lahore"
        result = assess_order(Order(2500, good, 15, phone="0321-4455667",
                                    history=PhoneHistory(delivered=3)))
        self.assertEqual(result.level, "low")
        self.assertEqual(result.action, "approve")

    # Verify original rule based risk behaviors.
    def test_original_rule_based_risk_behaviors(self):
        good = "House 12, Street 5, Gulberg III, Lahore"
        low = assess_order(Order(2500, good, 15, history=PhoneHistory(delivered=3)))
        obvious = assess_order(Order(18500, "house", 3, name="asdf", linked_identities=3,
                                     history=PhoneHistory(refused=2, days_since_last_refusal=20)))
        self.assertEqual(low.level, "low")
        self.assertEqual(obvious.level, "high")
        self.assertEqual(assess_order(Order(500, good, blocklisted=True)).action, "reject")
        self.assertEqual(assess_order(Order(50000, "x", is_cod=False)).action, "approve")

        landmark = assess_order(Order(3000, "near Jamia Masjid, Gulberg, Lahore", 14)).score
        city_only = assess_order(Order(3000, "lahore", 14)).score
        self.assertLess(landmark, city_only)

        recent = assess_order(Order(3000, good, 14,
                                    history=PhoneHistory(refused=1, days_since_last_refusal=10))).score
        old = assess_order(Order(3000, good, 14,
                                 history=PhoneHistory(refused=1, days_since_last_refusal=720))).score
        self.assertGreater(recent, old)

        below = assess_order(Order(9999, good, 14)).score
        above = assess_order(Order(10001, good, 14)).score
        self.assertLess(abs(above - below), 0.01)
