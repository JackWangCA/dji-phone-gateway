#!/usr/bin/env python3
"""Safely reconcile a Quectel modem's embedded carrier profile with its SIM."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import select
import subprocess
import sys
import termios
import time
from pathlib import Path

STATE_PATH = Path(os.getenv("CARRIER_PROFILE_STATE", "/var/lib/dji-phone-gateway/carrier-profile.json"))
LOCK_PATH = Path(os.getenv("CARRIER_PROFILE_LOCK", "/run/lock/dji-carrier-profile.lock"))
GRACE_SECONDS = int(os.getenv("CARRIER_PROFILE_GRACE", "120"))
COOLDOWN_SECONDS = int(os.getenv("CARRIER_PROFILE_COOLDOWN", "21600"))
TEST_SECONDS = int(os.getenv("CARRIER_PROFILE_TEST_TIMEOUT", "180"))


# The mapping only names profiles present in the stock QDC507GLEFM21 image. It
# is intentionally conservative: unknown/MVNO identities fall back to the
# modem's generic profile only after native selection has failed to register.
PROFILE_RULES: tuple[tuple[set[str], str], ...] = (
    ({"310030", "310070", "310090", "310150", "310170", "310280", "310380", "310410", "310560", "310680", "310950"}, "VoLTE-ATT"),
    ({"310160", "310200", "310210", "310220", "310230", "310240", "310250", "310260", "310270", "310310", "310490", "310660", "310800"}, "Commercial-TMO_VoLTE"),
    ({"310004", "310010", "310012", "310013", "310590", "310890", "311110", "311270", "311271", "311272", "311273", "311274", "311275", "311276", "311277", "311278", "311279", "311280", "311281", "311282", "311283", "311284", "311285", "311286", "311287", "311288", "311289", "311390", "311480", "311481", "311482", "311483", "311484", "311485", "311486", "311487", "311488", "311489"}, "hVoLTE-Verizon"),
    ({"46000", "46002", "46004", "46007", "46008"}, "Volte_OpenMkt-Commercial-CMCC"),
    ({"46001", "46006", "46009"}, "CU-VoLTE"),
    ({"46003", "46005", "46011"}, "VoLTE_OPNMKT_CT"),
    ({"302220"}, "Telus-Commercial_DO"),
    ({"50501"}, "Telstra-Commercial_VoLTE"),
    ({"26201", "26206"}, "Commercial-DT"),
    ({"44010"}, "Commercial-DCM"),
    ({"44020"}, "Commercial-SBM"),
    ({"44050", "44051", "44052", "44053", "44054"}, "Commercial-KDDI"),
    ({"45005"}, "Commercial-SKT"),
    ({"45008"}, "Commercial-KT"),
    ({"45006"}, "Commercial-LGU"),
)


def run(command: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
        return result.returncode, (result.stdout + result.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)


def json_command(command: list[str], timeout: int = 15) -> dict:
    code, output = run(command, timeout)
    if code:
        return {}
    try:
        value = json.loads(output)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def profile_for_plmn(plmn: str) -> str | None:
    normalized = re.sub(r"\D", "", plmn or "")
    for identities, profile in PROFILE_RULES:
        if normalized in identities:
            return profile
    return None


def parse_pdc_configs(output: str) -> list[dict[str, str]]:
    configs: list[dict[str, str]] = []
    for block in re.split(r"(?=Configuration \d+:)", output):
        description = re.search(r"^\s*Description:\s*(.+?)\s*$", block, re.MULTILINE)
        identifier = re.search(r"^\s*ID:\s*([0-9A-F:]+)\s*$", block, re.MULTILINE | re.IGNORECASE)
        status = re.search(r"^\s*Status:\s*(\w+)\s*$", block, re.MULTILINE)
        if description and identifier:
            configs.append({
                "description": description.group(1).strip(),
                "id": identifier.group(1).upper(),
                "status": status.group(1).lower() if status else "unknown",
            })
    return configs


def candidate_profiles(home_plmn: str, visited_plmn: str, available: set[str], active: str) -> list[str]:
    result: list[str] = []
    for profile in (profile_for_plmn(home_plmn), profile_for_plmn(visited_plmn), "ROW_Generic_3GPP"):
        if profile and profile in available and profile != active and profile not in result:
            result.append(profile)
    return result


def at_command(port: str, command: str, timeout: float = 5.0) -> str:
    """Send one command while ModemManager is stopped and return its response."""
    descriptor = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attributes = termios.tcgetattr(descriptor)
        attributes[0] = termios.IGNPAR
        attributes[1] = 0
        attributes[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attributes[3] = 0
        attributes[4] = termios.B115200
        attributes[5] = termios.B115200
        attributes[6][termios.VMIN] = 0
        attributes[6][termios.VTIME] = 1
        termios.tcsetattr(descriptor, termios.TCSANOW, attributes)
        termios.tcflush(descriptor, termios.TCIOFLUSH)
        os.write(descriptor, (command + "\r").encode("ascii"))
        deadline = time.monotonic() + timeout
        response = bytearray()
        while time.monotonic() < deadline:
            readable, _, _ = select.select([descriptor], [], [], 0.25)
            if readable:
                try:
                    response.extend(os.read(descriptor, 4096))
                except BlockingIOError:
                    pass
                text = response.decode("utf-8", "replace")
                if "\r\nOK\r\n" in text or "\r\nERROR\r\n" in text:
                    return text.strip()
        return response.decode("utf-8", "replace").strip()
    finally:
        os.close(descriptor)


def enable_native_auto(port: str) -> int:
    response = at_command(port, 'AT+QMBNCFG="AutoSel"')
    if re.search(r'"AutoSel"\s*,\s*1', response):
        print("Quectel native carrier selection is already enabled.")
        return 0
    response = at_command(port, 'AT+QMBNCFG="AutoSel",1')
    if "OK" not in response:
        print(f"Unable to enable native carrier selection: {response or 'no modem response'}", file=sys.stderr)
        return 1
    print("Enabled Quectel native carrier selection.")
    return 0


def modem_snapshot() -> dict[str, object]:
    listing = json_command(["mmcli", "-L", "-J"], 8)
    paths = listing.get("modem-list", [])
    if not isinstance(paths, list) or not paths:
        return {}
    index = str(paths[0]).rsplit("/", 1)[-1]
    modem = json_command(["mmcli", "-m", index, "-J"], 12).get("modem", {})
    if not isinstance(modem, dict) or not modem:
        return {}
    generic = modem.get("generic", {}) if isinstance(modem.get("generic"), dict) else {}
    gpp = modem.get("3gpp", {}) if isinstance(modem.get("3gpp"), dict) else {}
    sim_properties: dict = {}
    sim_path = generic.get("sim")
    if isinstance(sim_path, str) and sim_path not in ("", "--", "/"):
        sim_index = sim_path.rsplit("/", 1)[-1]
        sim = json_command(["mmcli", "-i", sim_index, "-J"], 8).get("sim", {})
        if isinstance(sim, dict) and isinstance(sim.get("properties"), dict):
            sim_properties = sim["properties"]
    ports = generic.get("ports", [])
    qmi_port = next((str(item).split()[0] for item in ports if "(qmi)" in str(item)), "cdc-wdm0")
    return {
        "index": index,
        "qmi_device": f"/dev/{qmi_port}",
        "profile": clean(generic.get("carrier-configuration")),
        "registration": clean(gpp.get("registration-state")),
        "visited_plmn": clean(gpp.get("operator-code")),
        "operator": clean(gpp.get("operator-name")),
        "iccid": clean(sim_properties.get("iccid")),
        "imsi": clean(sim_properties.get("imsi")),
        "home_plmn": clean(sim_properties.get("operator-code")),
    }


def clean(value: object) -> str:
    text = str(value or "").strip()
    return "" if text in ("--", "unknown", "none") else text


def load_state() -> dict:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(value: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o644)
    temporary.replace(STATE_PATH)


def pdc_configs(device: str) -> list[dict[str, str]]:
    code, output = run(["qmicli", "-p", "-d", device, "--pdc-list-configs=software"], 30)
    return parse_pdc_configs(output) if code == 0 else []


def wait_for_registration(seconds: int) -> dict[str, object]:
    deadline = time.monotonic() + seconds
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        latest = modem_snapshot()
        if latest.get("registration") in ("home", "roaming", "registered"):
            return latest
        time.sleep(5)
    return latest


def reset_modem(index: str) -> None:
    run(["mmcli", "-m", index, "--reset"], 20)
    time.sleep(8)
    run(["systemctl", "try-restart", "asterisk.service", "dji-sms-bridge.service"], 30)


def activate_profile(snapshot: dict[str, object], current: dict[str, str] | None, target: dict[str, str]) -> bool:
    device = str(snapshot["qmi_device"])
    if current and current["id"] != target["id"]:
        run(["qmicli", "-p", "-d", device, f"--pdc-deactivate-config=software,{current['id']}"], 30)
    code, _ = run(["qmicli", "-p", "-d", device, f"--pdc-activate-config=software,{target['id']}"], 45)
    if code:
        if current and current["id"] != target["id"]:
            run(["qmicli", "-p", "-d", device, f"--pdc-activate-config=software,{current['id']}"], 45)
        return False
    reset_modem(str(snapshot["index"]))
    return True


def reconcile(force: bool = False, dry_run: bool = False) -> int:
    now = int(time.time())
    snapshot = modem_snapshot()
    state = load_state()
    if not snapshot:
        save_state({**state, "mode": "automatic", "status": "No modem detected", "updated_at": now})
        print("No modem detected.")
        return 1
    sim_key = str(snapshot.get("iccid") or snapshot.get("imsi") or "unknown")
    registered = snapshot.get("registration") in ("home", "roaming", "registered")
    common = {
        "mode": "automatic",
        "sim": sim_key,
        "home_plmn": snapshot.get("home_plmn", ""),
        "visited_plmn": snapshot.get("visited_plmn", ""),
        "active_profile": snapshot.get("profile", ""),
        "registration": snapshot.get("registration", ""),
        "updated_at": now,
    }
    if registered:
        known_good = state.get("known_good", {}) if isinstance(state.get("known_good"), dict) else {}
        if snapshot.get("profile"):
            known_good[sim_key] = snapshot["profile"]
        save_state({**state, **common, "known_good": known_good, "unregistered_since": None,
                    "status": "Healthy — native selection retained"})
        print(f"Registered ({snapshot['registration']}); retaining {snapshot.get('profile') or 'current profile'}.")
        return 0

    same_sim = state.get("sim") == sim_key
    unregistered_since = int(state.get("unregistered_since") or now) if same_sim else now
    if not force and now - unregistered_since < GRACE_SECONDS:
        save_state({**state, **common, "unregistered_since": unregistered_since, "status": "Waiting for native carrier selection"})
        print("Waiting for the modem's native carrier selection and registration.")
        return 0
    last_attempt = int(state.get("last_attempt") or 0) if same_sim else 0
    if not force and last_attempt and now - last_attempt < COOLDOWN_SECONDS:
        save_state({**state, **common, "unregistered_since": unregistered_since, "status": "Fallback cooldown — no registration"})
        print("Profile fallback is in cooldown.")
        return 0

    configs = pdc_configs(str(snapshot["qmi_device"]))
    by_name = {item["description"]: item for item in configs}
    active = next((item for item in configs if item["status"] == "active"), by_name.get(str(snapshot.get("profile", ""))))
    candidates = candidate_profiles(
        str(snapshot.get("home_plmn", "")), str(snapshot.get("visited_plmn", "")), set(by_name),
        str(snapshot.get("profile", "")),
    )
    known_good = state.get("known_good", {}) if isinstance(state.get("known_good"), dict) else {}
    previous = str(known_good.get(sim_key) or "")
    if previous in by_name and previous != snapshot.get("profile") and previous not in candidates:
        candidates.insert(0, previous)
    save_state({**state, **common, "unregistered_since": unregistered_since, "last_attempt": now, "candidates": candidates,
                "status": "Dry run" if dry_run else "Testing fallback profiles"})
    if dry_run:
        print("Candidates: " + (", ".join(candidates) or "none"))
        return 0
    if not candidates:
        save_state({**load_state(), "status": "No compatible fallback profile available", "updated_at": int(time.time())})
        return 1

    original = active
    for name in candidates:
        print(f"Testing carrier profile: {name}")
        target = by_name[name]
        if not activate_profile(snapshot, active, target):
            continue
        latest = wait_for_registration(TEST_SECONDS)
        if latest.get("registration") in ("home", "roaming", "registered"):
            save_state({**load_state(), "active_profile": name, "registration": latest.get("registration", ""),
                        "status": f"Fallback selected {name}", "updated_at": int(time.time())})
            print(f"Registered with {name}.")
            return 0
        snapshot = latest or snapshot
        active = target

    if original and active and original["id"] != active["id"]:
        print(f"Restoring original profile: {original['description']}")
        activate_profile(snapshot, active, original)
    save_state({**load_state(), "active_profile": original["description"] if original else snapshot.get("profile", ""),
                "status": "No fallback profile registered; original restored", "updated_at": int(time.time())})
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="ignore grace period and cooldown")
    parser.add_argument("--dry-run", action="store_true", help="show candidates without changing the modem")
    parser.add_argument("--status", action="store_true", help="print saved automation status")
    parser.add_argument("--enable-native-auto", metavar="PORT", help="enable Quectel AutoSel while ModemManager is stopped")
    args = parser.parse_args()
    if args.status:
        print(json.dumps(load_state(), indent=2, sort_keys=True))
        return 0
    if args.enable_native_auto:
        return enable_native_auto(args.enable_native_auto)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Carrier profile reconciliation is already running.")
            return 0
        return reconcile(args.force, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
