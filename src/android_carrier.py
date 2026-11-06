# Matching rules adapted from Android CarrierResolver.
# Copyright (C) The Android Open Source Project; Apache License, Version 2.0.
"""Offline AOSP carrier identification and conservative Internet APN selection.

Matching semantics and field weights follow Android CarrierResolver (Apache-2.0).
Unknown SIM fields never satisfy a constrained rule. No carrier privileges/PNN
are guessed from the currently visited network's name.
"""
import json
from functools import lru_cache
from pathlib import Path
import re

DATABASE = Path(__file__).with_name("carrier-data.json")
WEIGHTS = {"mccmnc_tuple": 256, "imsi_prefix_xpattern": 128, "iccid_prefix": 64,
           "gid1": 32, "gid2": 16, "plmn": 8, "privilege_access_rule": 4,
           "spn": 2, "preferred_apn": 1}
FIELDS = {"mccmnc_tuple": "home_plmn", "imsi_prefix_xpattern": "imsi", "iccid_prefix": "iccid",
          "preferred_apn": "preferred_apn"}


@lru_cache(maxsize=1)
def database():
    return json.loads(DATABASE.read_text())


def matches(field, pattern, value):
    if value is None or value == "":
        return False
    if field == "privilege_access_rule":
        return isinstance(value, list) and pattern.lower() in [str(v).lower() for v in value]
    value, pattern = str(value).lower(), str(pattern).lower()
    if field == "imsi_prefix_xpattern":
        return len(value) >= len(pattern) and all(p == "x" or p == v for p, v in zip(pattern, value))
    if field in ("gid1", "gid2", "iccid_prefix"):
        return value.startswith(pattern)
    return value == pattern


def score(attributes, sim):
    total = 0
    for field, values in attributes.items():
        if field not in WEIGHTS or not values:
            return -1
        if not any(matches(field, pattern, sim.get(FIELDS.get(field, field))) for pattern in values):
            return -1
        total += WEIGHTS[field]
    return total if attributes.get("mccmnc_tuple") else -1


def identify(sim, data=None):
    data = data if data is not None else database()
    candidates = []
    for carrier in data["carriers"]:
        best = max((score(rule, sim) for rule in carrier["attributes"]), default=-1)
        if best >= 0:
            candidates.append((best, carrier))
    if not candidates:
        return {"id": None, "name": "Unknown carrier", "parent_id": None, "match": "unknown",
                "database_version": data["version"]}
    best_score = max(item[0] for item in candidates)
    tied = [item[1] for item in candidates if item[0] == best_score]
    # Android prefers a matching child over its parent when scores are equal.
    parents = {item["parent_id"] for item in tied}
    tied = [item for item in tied if item["id"] not in parents]
    if len(tied) != 1:
        return {"id": None, "name": "Ambiguous carrier", "parent_id": None, "match": "ambiguous",
                "database_version": data["version"]}
    carrier = tied[0]
    return {"id": carrier["id"], "name": carrier["name"], "parent_id": carrier["parent_id"],
            "match": "specific" if best_score > 256 else "network", "database_version": data["version"]}


def choose_apn(sim, identity, data=None):
    """Only select a single unambiguous compatible Internet APN; never an IMS APN."""
    data = data if data is not None else database()
    candidates = []
    for apn in data.get("apns", []):
        if apn.get("mcc", "") + apn.get("mnc", "") != sim.get("home_plmn"):
            continue
        if apn.get("proxy") or apn.get("port") or apn.get("carrier_enabled") == "false":
            continue
        if apn.get("authtype", "-1") not in ("-1", "0"):
            continue
        if not set(apn.get("type", "default").split(",")) & {"default", "*"}:
            continue
        rank = 0
        if apn.get("carrier_id"):
            if apn["carrier_id"] != str(identity.get("id")):
                continue
            rank += 64
        mvno, pattern = apn.get("mvno_type"), apn.get("mvno_match_data", "")
        if mvno:
            fields = {"spn": "spn", "imsi": "imsi_prefix_xpattern", "gid": "gid1", "iccid": "iccid_prefix"}
            field = fields.get(mvno)
            if not field or not pattern or not any(matches(field, p.strip(), sim.get(FIELDS.get(field, field)))
                                                  for p in pattern.split(",")):
                continue
            rank += 32
        protocol = apn.get("protocol", "IP").upper()
        if protocol not in ("IP", "IPV6", "IPV4V6"):
            continue
        # Unsupported routing/auth variants are left to NetworkManager.
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", apn["apn"]):
            continue
        settings = {"apn": apn["apn"], "protocol": protocol, "user": apn.get("user", ""),
                    "password": apn.get("password", ""), "auth": apn.get("authtype", "-1")}
        candidates.append((rank, settings))
    if not candidates:
        return {"status": "provider-database", "reason": "No supported Android Internet APN match"}
    rank = max(item[0] for item in candidates)
    unique = {json.dumps(settings, sort_keys=True): settings for score_, settings in candidates if score_ == rank}
    if len(unique) != 1:
        return {"status": "provider-database", "reason": "Multiple Android APNs match; keeping provider autoconfiguration"}
    return {"status": "matched", **next(iter(unique.values()))}
