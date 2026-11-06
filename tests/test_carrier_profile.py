import importlib.util
import unittest
import sys
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))


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

    def test_network_learning_precedes_sim_learning_and_home_profile(self):
        snapshot = {"iccid": "SIM", "home_plmn": "46011", "visited_plmn": "310260", "profile": "unknown"}
        state = {"known_good_networks": {"SIM|310260": "Commercial-TMO_VoLTE"},
                 "known_good": {"SIM": "ROW_Generic_3GPP"}}
        available = {"VoLTE_OPNMKT_CT", "Commercial-TMO_VoLTE", "ROW_Generic_3GPP"}
        self.assertEqual(carrier_profile.learned_candidates(snapshot, state, available),
                         ["Commercial-TMO_VoLTE", "ROW_Generic_3GPP", "VoLTE_OPNMKT_CT"])

    def test_healthy_connection_is_retained_and_learned_per_sim_network(self):
        snapshot = {"iccid": "SIM", "home_plmn": "46011", "visited_plmn": "310260",
                    "profile": "Commercial-TMO_VoLTE", "registration": "roaming"}
        with mock.patch.object(carrier_profile, "modem_snapshot", return_value=snapshot), \
                mock.patch.object(carrier_profile, "load_state", return_value={}), \
                mock.patch.object(carrier_profile, "save_state") as save, \
                mock.patch.object(carrier_profile, "activate_profile") as activate:
            self.assertEqual(carrier_profile.reconcile(), 0)
        activate.assert_not_called()
        self.assertEqual(save.call_args.args[0]["known_good_networks"]["SIM|310260"], "Commercial-TMO_VoLTE")
        self.assertEqual(save.call_args.args[0]["carrier_identity"]["name"], "China Telecom")

    def test_locked_or_unidentified_sim_does_not_trigger_recovery(self):
        for snapshot in ({"registration": "searching"}, {"iccid": "SIM", "unlock_required": "sim-pin"}):
            with mock.patch.object(carrier_profile, "modem_snapshot", return_value=snapshot), \
                    mock.patch.object(carrier_profile, "load_state", return_value={}), \
                    mock.patch.object(carrier_profile, "save_state"), \
                    mock.patch.object(carrier_profile, "pdc_configs") as configs:
                carrier_profile.reconcile(force=True)
                configs.assert_not_called()

    def test_manual_apn_is_preserved_and_unique_android_apn_does_not_enable_data(self):
        snapshot = {"iccid": "SIM", "home_plmn": "46011"}
        with mock.patch.dict(carrier_profile.os.environ, {"CELLULAR_APN": ""}), \
                mock.patch.object(carrier_profile, "modem_snapshot", return_value=snapshot), \
                mock.patch.object(carrier_profile, "load_state", return_value={}), \
                mock.patch.object(carrier_profile, "save_state") as save, \
                mock.patch.object(carrier_profile, "run", return_value=(0, "no\nmy-manual-apn")) as run:
            self.assertEqual(carrier_profile.configure_apn(), 0)
            self.assertEqual(run.call_count, 1)
            save.assert_not_called()
            run.reset_mock()
            run.side_effect = [(0, "yes\n"), (0, "")]
            self.assertEqual(carrier_profile.configure_apn(), 0)
            command = run.call_args_list[-1].args[0]
            self.assertIn("ctnet", command)
            self.assertNotIn("up", command)
            self.assertEqual(save.call_args.args[0]["managed_apn"]["sim"], "SIM")

    def test_sim_change_during_fallback_stops_old_sim_recovery(self):
        snapshot = {"iccid": "old", "home_plmn": "310260", "profile": "bad", "index": "0", "qmi_device": "/dev/test"}
        config = {"description": "Commercial-TMO_VoLTE", "id": "AA", "status": "inactive"}
        with mock.patch.object(carrier_profile, "modem_snapshot", return_value=snapshot), \
                mock.patch.object(carrier_profile, "load_state", return_value={}), \
                mock.patch.object(carrier_profile, "save_state") as save, \
                mock.patch.object(carrier_profile, "pdc_configs", return_value=[config]), \
                mock.patch.object(carrier_profile, "activate_profile", return_value=True) as activate, \
                mock.patch.object(carrier_profile, "wait_for_registration", return_value={"iccid": "new", "registration": "home"}):
            self.assertEqual(carrier_profile.reconcile(force=True), 0)
        self.assertEqual(activate.call_count, 1)
        self.assertIn("SIM changed", save.call_args.args[0]["status"])

    def test_failed_recovery_restores_original_profile_and_dry_run_does_not_switch(self):
        snapshot = {"iccid": "SIM", "home_plmn": "99999", "profile": "old", "index": "0", "qmi_device": "/dev/test"}
        original = {"description": "old", "id": "AA", "status": "active"}
        generic = {"description": "ROW_Generic_3GPP", "id": "BB", "status": "inactive"}
        with mock.patch.object(carrier_profile, "modem_snapshot", return_value=snapshot), \
                mock.patch.object(carrier_profile, "load_state", return_value={}), \
                mock.patch.object(carrier_profile, "save_state") as save, \
                mock.patch.object(carrier_profile, "pdc_configs", return_value=[original, generic]), \
                mock.patch.object(carrier_profile, "activate_profile", return_value=True) as activate, \
                mock.patch.object(carrier_profile, "wait_for_registration", return_value=snapshot):
            self.assertEqual(carrier_profile.reconcile(force=True, dry_run=True), 0)
            activate.assert_not_called()
            save.assert_not_called()
            self.assertEqual(carrier_profile.reconcile(force=True), 1)
            self.assertEqual(activate.call_args.args[2], original)
            self.assertEqual(save.call_args.args[0]["active_profile"], "old")

    def test_ambiguous_apn_clears_previously_managed_settings_without_enabling_data(self):
        with mock.patch.dict(carrier_profile.os.environ, {"CELLULAR_APN": ""}), \
                mock.patch.object(carrier_profile, "modem_snapshot", return_value={"iccid": "new", "home_plmn": "99999"}), \
                mock.patch.object(carrier_profile, "load_state", return_value={"managed_apn": {"apn": "old"}}), \
                mock.patch.object(carrier_profile, "save_state"), \
                mock.patch.object(carrier_profile, "run", side_effect=[(0, "no\nold"), (0, "")]) as run:
            self.assertEqual(carrier_profile.configure_apn(), 0)
            command = run.call_args_list[-1].args[0]
            self.assertEqual(command[command.index("gsm.auto-config") + 1], "yes")
            self.assertNotIn("up", command)


if __name__ == "__main__":
    unittest.main()
