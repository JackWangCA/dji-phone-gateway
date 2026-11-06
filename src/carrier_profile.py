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

import android_carrier

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
        "spn": clean(sim_properties.get("operator-name")),
        "gid1": clean(sim_properties.get("gid1")).replace(":", ""),
        "gid2": clean(sim_properties.get("gid2")).replace(":", ""),
        "unlock_required": clean(generic.get("unlock-required")),
    }


def clean(value: object) -> str:
    text = str(value or "").strip()
    return "" if text in ("--", "unknown", "none", "/") else text


def identify_snapshot(snapshot: dict) -> dict:
    try:
        return android_carrier.identify(snapshot)
    except (OSError, ValueError, KeyError):
        return {"id": None, "name": "Unknown carrier", "match": "unavailable", "parent_id": None}


def learned_candidates(snapshot: dict, state: dict, available: set[str]) -> list[str]:
    """Prefer a proven SIM/network pairing, then identified carrier and PLMN fallbacks."""
    sim = str(snapshot.get("iccid") or snapshot.get("imsi") or "")
    visited = str(snapshot.get("visited_plmn") or (state.get("visited_plmn") if state.get("sim") == sim else "") or "")
    by_network = state.get("known_good_networks", {})
    by_sim = state.get("known_good", {})
    learned = by_network.get(sim + "|" + visited) if isinstance(by_network, dict) else None
    previous = by_sim.get(sim) if isinstance(by_sim, dict) else None
    identity = identify_snapshot(snapshot)
    try:
        records = {item["id"]: item for item in android_carrier.database()["carriers"]}
        carrier = records.get(identity.get("id"))
        # Include parent MNO identities for MVNOs, but only firmware profiles we have.
        matched_plmns = []
        seen = set()
        while carrier and carrier["id"] not in seen:
            seen.add(carrier["id"])
            matched_plmns.extend(plmn for rule in carrier["attributes"] for plmn in rule.get("mccmnc_tuple", []))
            carrier = records.get(carrier.get("parent_id"))
    except (OSError, ValueError, KeyError):
        matched_plmns = []
    proposed = [learned, previous, *(profile_for_plmn(p) for p in matched_plmns)]
    proposed += candidate_profiles(str(snapshot.get("home_plmn", "")), visited, available, "")
    result = []
    for name in proposed:
        if name and name in available and name != snapshot.get("profile") and name not in result:
            result.append(name)
    return result


def configure_apn() -> int:
    """Configure the next explicit data connection; never bring it up here."""
    if os.getenv("CELLULAR_APN", ""):
        return 1  # The data switch handles the user's explicit override.
    snapshot = modem_snapshot()
    if not snapshot.get("home_plmn") or not snapshot.get("iccid"):
        return 1
    identity = identify_snapshot(snapshot)
    try:
        selected = android_carrier.choose_apn(snapshot, identity)
    except (OSError, ValueError, KeyError):
        return 1
    connection = os.getenv("CELLULAR_CONNECTION", "cellular")
    code, current = run(["nmcli", "-g", "gsm.auto-config,gsm.apn", "connection", "show", connection], 5)
    if code:
        return 1
    state = load_state()
    lines = current.splitlines()
    automatic = bool(lines and lines[0].strip() in ("yes", "true"))
    current_apn = lines[1].strip() if len(lines) > 1 else ""
    managed = state.get("managed_apn", {})
    if not automatic and (not isinstance(managed, dict) or managed.get("apn") != current_apn):
        print("Retaining manually configured APN.")
        return 0
    if selected["status"] != "matched":
        code, _ = run(["nmcli", "connection", "modify", connection, "gsm.apn", "", "gsm.auto-config", "yes",
                       "gsm.username", "", "gsm.password", "", "ipv4.method", "auto", "ipv6.method", "auto"], 10)
        managed = {}
    else:
        protocol = selected["protocol"]
        code, _ = run(["nmcli", "connection", "modify", connection, "gsm.auto-config", "no",
                       "gsm.apn", selected["apn"], "gsm.username", selected["user"], "gsm.password", selected["password"],
                       "ipv4.method", "disabled" if protocol == "IPV6" else "auto",
                       "ipv6.method", "disabled" if protocol == "IP" else "auto"], 10)
        managed = {"apn": selected["apn"], "sim": snapshot["iccid"], "source": "Android"}
    if code:
        return 1
    # Re-read after nmcli; never overwrite concurrent profile-learning state.
    save_state({**load_state(), "managed_apn": managed, "apn_selection": selected["status"]})
    print("Android APN selected." if managed else "Using provider APN autoconfiguration.")
    return 0


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
        "visited_plmn": snapshot.get("visited_plmn") or (state.get("visited_plmn", "") if state.get("sim") == sim_key else ""),
        "active_profile": snapshot.get("profile", ""),
        "registration": snapshot.get("registration", ""),
        "updated_at": now,
        "carrier_identity": identify_snapshot(snapshot),
    }
    if sim_key == "unknown" or snapshot.get("unlock_required") not in (None, "", "sim-pin2"):
        save_state({**state, **common, "status": "Waiting for an unlocked SIM"})
        return 0
    if registered:
        known_good = state.get("known_good", {}) if isinstance(state.get("known_good"), dict) else {}
        if snapshot.get("profile"):
            known_good[sim_key] = snapshot["profile"]
        networks = state.get("known_good_networks", {}) if isinstance(state.get("known_good_networks"), dict) else {}
        if snapshot.get("profile") and snapshot.get("visited_plmn"):
            networks[sim_key + "|" + str(snapshot["visited_plmn"])] = snapshot["profile"]
        save_state({**state, **common, "known_good": known_good, "unregistered_since": None,
                    "known_good_networks": networks, "status": "Registered — working profile retained",
                    "service_validation": "Network registration confirmed; SMS/IMS not independently verified"})
        print(f"Registered ({snapshot['registration']}); retaining {snapshot.get('profile') or 'current profile'}.")
        return 0

    same_sim = state.get("sim") == sim_key
    unregistered_since = int(state.get("unregistered_since") or now) if same_sim else now
    if not force and now - unregistered_since < GRACE_SECONDS:
        save_state({**state, **common, "unregistered_since": unregistered_since, "status": "Waiting for native carrier selection"})
        print("Waiting for the modem's native carrier selection and registration.")
        return 0
    last_attempt = int(state.get("last_attempt") or 0) if same_sim else 0
    if snapshot.get("visited_plmn") and state.get("visited_plmn") != snapshot["visited_plmn"]:
        last_attempt = 0
    if not force and last_attempt and now - last_attempt < COOLDOWN_SECONDS:
        save_state({**state, **common, "unregistered_since": unregistered_since, "status": "Fallback cooldown — no registration"})
        print("Profile fallback is in cooldown.")
        return 0

    configs = pdc_configs(str(snapshot["qmi_device"]))
    by_name = {item["description"]: item for item in configs}
    active = next((item for item in configs if item["status"] == "active"), by_name.get(str(snapshot.get("profile", ""))))
    candidates = learned_candidates(snapshot, state, set(by_name))
    if dry_run:
        print("Candidates: " + (", ".join(candidates) or "none"))
        return 0
    save_state({**state, **common, "unregistered_since": unregistered_since, "last_attempt": now, "candidates": candidates,
                "status": "Testing fallback profiles"})
    if not candidates:
        save_state({**load_state(), "status": "No compatible fallback profile available", "updated_at": int(time.time())})
        return 1

    original = active
    for name in candidates:
        before = modem_snapshot()
        if str(before.get("iccid") or before.get("imsi") or "") != sim_key:
            save_state({**load_state(), "status": "SIM changed before recovery; waiting for a stable SIM",
                        "unregistered_since": None, "last_attempt": 0})
            return 0
        snapshot = before
        print(f"Testing carrier profile: {name}")
        target = by_name[name]
        if not activate_profile(snapshot, active, target):
            continue
        latest = wait_for_registration(TEST_SECONDS)
        latest_sim = str(latest.get("iccid") or latest.get("imsi") or "")
        if latest_sim != sim_key:
            save_state({**load_state(), "status": "SIM changed during recovery; waiting for the new SIM",
                        "unregistered_since": None, "last_attempt": 0})
            return 0
        if latest.get("registration") in ("home", "roaming", "registered"):
            learned = load_state().get("known_good", {})
            learned = learned if isinstance(learned, dict) else {}
            learned[sim_key] = latest.get("profile") or name
            networks = load_state().get("known_good_networks", {})
            networks = networks if isinstance(networks, dict) else {}
            if latest.get("visited_plmn"):
                networks[sim_key + "|" + str(latest["visited_plmn"])] = latest.get("profile") or name
            save_state({**load_state(), "active_profile": latest.get("profile") or name, "registration": latest.get("registration", ""),
                        "known_good": learned, "known_good_networks": networks,
                        "service_validation": "Network registration confirmed; SMS/IMS not independently verified",
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
    parser.add_argument("--configure-apn", action="store_true", help="configure an Android APN for the next data connection")
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
            return 1 if args.configure_apn else 0
        if args.configure_apn:
            return configure_apn()
        return reconcile(args.force, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
