import os
import pathlib
import subprocess
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "bin" / "dji-data"


class DataProfileTests(unittest.TestCase):
    def run_switch(self, apn: str) -> str:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            log = root / "nmcli.log"
            nmcli = root / "nmcli"
            nmcli.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$NMCLI_LOG\"\n"
                "[ \"$*\" = '-t -f NAME connection show' ] && printf 'cellular\\n'\n"
                "exit 0\n",
                encoding="utf-8",
            )
            nft = root / "nft"
            nft.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            nmcli.chmod(0o755)
            nft.chmod(0o755)
            (root / "sys" / "lo").mkdir(parents=True)
            env = {
                **os.environ,
                "PATH": f"{root}:{os.environ['PATH']}",
                "NMCLI_LOG": str(log),
                "CELLULAR_APN": apn,
                "WWAN_INTERFACE": "lo",
                "SYS_CLASS_NET": str(root / "sys"),
            }
            result = subprocess.run([str(SCRIPT), "on"], env=env, check=False, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return log.read_text(encoding="utf-8")

    def test_blank_apn_enables_provider_database_autoconfiguration(self):
        log = self.run_switch("")
        self.assertIn("connection modify cellular gsm.apn  gsm.auto-config yes", log)
        self.assertIn("connection up cellular", log)

    def test_manual_apn_disables_autoconfiguration(self):
        log = self.run_switch("Wholesale")
        self.assertIn("connection modify cellular gsm.auto-config no gsm.apn Wholesale", log)


if __name__ == "__main__":
    unittest.main()
