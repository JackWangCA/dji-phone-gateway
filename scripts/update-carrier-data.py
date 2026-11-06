#!/usr/bin/env python3
"""Build our offline database from pinned Apache-2.0 Android source files."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import urllib.request
import xml.etree.ElementTree as ET

CARRIER_REVISION = "bca387f553a4493c88e24455172225fd1049c91f"
APN_REVISION = "73f904fbeb872f5675cb5f18cd9dfb15856425a6"


def parse_textproto(text):
    tokens = iter(re.findall(r'"(?:\\.|[^"\\])*"|[A-Za-z_][A-Za-z_0-9]*|-?\d+|[{}:]',
                            re.sub(r"//[^\n]*", "", text)))

    def message():
        result = {}
        for key in tokens:
            if key == "}":
                return result
            separator = next(tokens)
            if separator == "{":
                value = message()
            elif separator == ":":
                raw = next(tokens)
                value = json.loads(raw)
            else:
                raise ValueError("Unexpected carrier textproto syntax")
            result.setdefault(key, []).append(value)
        return result

    raw = message()
    return {"version": raw["version"][0], "carriers": [
        {"id": item["canonical_id"][0], "name": item["carrier_name"][0],
         "parent_id": item.get("parent_canonical_id", [None])[0],
         "attributes": item.get("carrier_attribute", [])} for item in raw["carrier_id"]]}


def download(project, revision, path):
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Supply an exact Git commit, not a moving branch")
    url = f"https://android.googlesource.com/{project}/+/{revision}/{path}?format=TEXT"
    content = base64.b64decode(urllib.request.urlopen(url, timeout=30).read())
    return content, {"project": project, "revision": revision, "path": path,
                     "sha256": hashlib.sha256(content).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--carrier-revision", default=CARRIER_REVISION)
    parser.add_argument("--apn-revision", default=APN_REVISION)
    args = parser.parse_args()
    raw, carrier_source = download("platform/packages/providers/TelephonyProvider", args.carrier_revision,
                                  "assets/latest_carrier_id/carrier_list.textpb")
    apns, apn_source = download("device/sample", args.apn_revision, "etc/apns-full-conf.xml")
    data = parse_textproto(raw.decode())
    data.update({"sources": [carrier_source, apn_source], "license": "Apache-2.0",
                 "apns": [dict(item.attrib) for item in ET.fromstring(apns).findall("apn")
                          if item.get("apn") and item.get("carrier_enabled", "true") != "false"
                          and set(item.get("type", "default").split(",")) & {"default", "*"}]})
    target = Path(__file__).resolve().parents[1] / "src" / "carrier-data.json"
    target.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"Built {len(data['carriers'])} carrier identities and {len(data['apns'])} Internet APN records")


if __name__ == "__main__":
    main()
