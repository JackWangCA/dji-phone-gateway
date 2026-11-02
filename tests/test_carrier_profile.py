import importlib.util
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "carrier_profile", Path(__file__).parents[1] / "src" / "carrier_profile.py"
)
carrier_profile = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(carrier_profile)


class CarrierProfileTests(unittest.TestCase):
    def test_profile_mapping(self):
        self.assertEqual(carrier_profile.profile_for_plmn("310260"), "Commercial-TMO_VoLTE")
        self.assertEqual(carrier_profile.profile_for_plmn("46011"), "VoLTE_OPNMKT_CT")
        self.assertEqual(carrier_profile.profile_for_plmn("311480"), "hVoLTE-Verizon")
        self.assertIsNone(carrier_profile.profile_for_plmn("99999"))

    def test_parse_pdc_configs(self):
        output = """Configuration 1:
 Description: ROW_Generic_3GPP
 Status: Inactive
 ID: 74:FE:66:31
Configuration 2:
 Description: Commercial-TMO_VoLTE
 Status: Active
 ID: 66:3E:83:00
"""
        self.assertEqual(carrier_profile.parse_pdc_configs(output), [
            {"description": "ROW_Generic_3GPP", "id": "74:FE:66:31", "status": "inactive"},
            {"description": "Commercial-TMO_VoLTE", "id": "66:3E:83:00", "status": "active"},
        ])

    def test_candidates_prefer_home_then_visited_then_generic(self):
        available = {"VoLTE_OPNMKT_CT", "Commercial-TMO_VoLTE", "ROW_Generic_3GPP"}
        self.assertEqual(
            carrier_profile.candidate_profiles("46011", "310260", available, "Commercial-TMO_VoLTE"),
            ["VoLTE_OPNMKT_CT", "ROW_Generic_3GPP"],
        )

    def test_candidates_only_use_installed_profiles(self):
        self.assertEqual(
            carrier_profile.candidate_profiles("46011", "310260", {"ROW_Generic_3GPP"}, ""),
            ["ROW_Generic_3GPP"],
        )

    def test_current_profile_is_not_retested(self):
        available = {"Commercial-TMO_VoLTE", "ROW_Generic_3GPP"}
        self.assertEqual(
            carrier_profile.candidate_profiles("310260", "310260", available, "Commercial-TMO_VoLTE"),
            ["ROW_Generic_3GPP"],
        )


if __name__ == "__main__":
    unittest.main()
