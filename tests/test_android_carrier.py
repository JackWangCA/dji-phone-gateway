import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))
import android_carrier as android


def carrier(id_, name, attributes, parent=None):
    return {"id": id_, "name": name, "parent_id": parent, "attributes": attributes}


class AndroidCarrierTests(unittest.TestCase):
    def test_real_offline_database_identifies_china_telecom_and_tmobile(self):
        self.assertEqual(android.identify({"home_plmn": "46011"})["name"], "China Telecom")
        self.assertEqual(android.identify({"home_plmn": "310260"})["id"], 1)

    def test_specific_mvno_beats_parent_and_missing_gid_does_not_match(self):
        data = {"version": 1, "carriers": [carrier(1, "MNO", [{"mccmnc_tuple": ["310260"]}]),
                carrier(2, "MVNO", [{"mccmnc_tuple": ["310260"], "gid1": ["AB", "CD"]}], 1)]}
        sim = {"home_plmn": "310260", "gid1": "abffffffff"}
        self.assertEqual(android.identify(sim, data)["id"], 2)
        self.assertEqual(android.identify({"home_plmn": "310260"}, data)["id"], 1)

    def test_constraints_are_anded_and_values_are_ored(self):
        rule = {"mccmnc_tuple": ["310260", "310240"], "imsi_prefix_xpattern": ["310240xx9"], "spn": ["Example"]}
        sim = {"home_plmn": "310240", "imsi": "31024012912345", "spn": "EXAMPLE"}
        self.assertEqual(android.score(rule, sim), 256 + 128 + 2)
        self.assertEqual(android.score(rule, {**sim, "spn": "Other"}), -1)
        self.assertEqual(android.score(rule, {**sim, "imsi": "310240"}), -1)

    def test_iccid_prefix_and_unavailable_privilege_rules(self):
        self.assertTrue(android.matches("iccid_prefix", "89860", "89860012345"))
        self.assertEqual(android.score({"mccmnc_tuple": ["310260"], "privilege_access_rule": ["abc"]},
                                      {"home_plmn": "310260"}), -1)

    def test_equal_score_prefers_child_but_unrelated_tie_is_ambiguous(self):
        rule = [{"mccmnc_tuple": ["310260"]}]
        data = {"version": 1, "carriers": [carrier(1, "MNO", rule), carrier(2, "Child", rule, 1)]}
        self.assertEqual(android.identify({"home_plmn": "310260"}, data)["id"], 2)
        data["carriers"].append(carrier(3, "Other", rule))
        self.assertEqual(android.identify({"home_plmn": "310260"}, data)["match"], "ambiguous")

    def test_roaming_network_never_replaces_home_identity(self):
        self.assertEqual(android.identify({"home_plmn": "46011", "visited_plmn": "310260"})["name"], "China Telecom")
        self.assertEqual(android.identify({"home_plmn": "99999"})["match"], "unknown")

    def test_mvno_apn_beats_generic_and_rejects_ims_and_visited_apns(self):
        generic = {"mcc": "310", "mnc": "260", "apn": "generic", "type": "default"}
        data = {"apns": [generic, {**generic, "apn": "mvno", "mvno_type": "gid", "mvno_match_data": "AB"},
                         {**generic, "apn": "ims", "type": "ims"},
                         {**generic, "mcc": "460", "mnc": "11", "apn": "other"}]}
        sim = {"home_plmn": "310260", "gid1": "abff", "visited_plmn": "46011"}
        self.assertEqual(android.choose_apn(sim, {}, data)["apn"], "mvno")
        self.assertEqual(android.choose_apn({"home_plmn": "310260"}, {}, data)["apn"], "generic")

    def test_ambiguous_apns_and_unsupported_auth_fall_back_without_guessing(self):
        generic = {"mcc": "310", "mnc": "260", "apn": "first"}
        sim = {"home_plmn": "310260"}
        self.assertEqual(android.choose_apn(sim, {}, {"apns": [generic, {**generic, "apn": "second"}]})["status"],
                         "provider-database")
        self.assertEqual(android.choose_apn(sim, {}, {"apns": [{**generic, "authtype": "2"}]})["status"],
                         "provider-database")
