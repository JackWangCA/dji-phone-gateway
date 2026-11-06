# DJI EG25 Raspberry Pi phone gateway

Turn a first-generation DJI 4G dongle (Quectel EG25-G) into a private cellular-to-SIP and SMS-to-Telegram gateway on a Raspberry Pi 3B.

## What works

- Incoming cellular calls ring a SIP client on your iPhone. Use a SIP app with CallKit support (Groundwire, Acrobits Softphone, Linphone, etc.).
- Outgoing calls from that SIP account use the SIM in the dongle.
- Incoming SMS messages are forwarded to one authorized Telegram chat.
- `/sms +15551234567 hello` sends an SMS.
- `/data_on`, `/data_off`, and `/status` control/report the modem data path. Data is off by default.
- An iPhone-inspired dashboard on port 8080 presents SMS as two-way conversations, shows live SIM registration, signal, carrier, phone number, radio, APN, and service health, configures Telegram, and switches cellular data.

Telegram bots cannot originate or receive Telegram voice calls and cannot invoke iOS CallKit. Telegram is therefore used only for SMS and controls; SIP supplies the native-call UI.

## Cloudflare Access single sign-in

The dashboard can accept a verified Cloudflare Access login on its public hostname,
while retaining its password login on local addresses. In Cloudflare Zero Trust,
create a self-hosted Access application covering the entire dashboard hostname
(including API paths) and an Allow policy limited to your account. Keep the tunnel
route pointed at `http://localhost:8080`.

Add these three values to `/etc/dji-phone-gateway/gateway.env`:

```sh
CLOUDFLARE_ACCESS_ISSUER=https://YOUR-TEAM.cloudflareaccess.com
CLOUDFLARE_ACCESS_AUD=YOUR_APPLICATION_AUDIENCE_TAG
CLOUDFLARE_ACCESS_HOSTNAME=messages.example.com
```

Find the team domain in Zero Trust settings and the Application Audience (AUD) tag
in the Access application's settings. Install the current requirements with the
gateway venv, then restart `dji-dashboard.service`. The server verifies RS256
signatures, issuer, audience, expiry, and user identity against your team's public
keys. A local password cookie cannot bypass Access on the configured public host.
Sign out through the public hostname to end the Cloudflare Access session.
Do not expose port 8080 directly to the Internet. Cloudflare policies and session
duration remain managed in Cloudflare; the app does not extend an Access session.

## Contacts and conversation controls

Manage saved sender names in **Settings → Contacts**, or open a conversation and
choose **Contact & options**. Names are stored on the Pi and used in the web app,
Telegram messages, and push titles. Saved phone contacts are also suggested in the
new-message composer. Removing a name preserves that conversation and its rules.

Conversation options include pin, mute, archive, and block forwarding. Pinned
threads sort first. Archived threads are hidden from All/Unread/Sent and remain in
Archived until restored. Mute suppresses push alerts and sends Telegram messages
silently. Block forwarding suppresses both destinations while still storing SMS
locally. These rules apply to both Asterisk and ModemManager receive paths.

Incoming numeric verification codes near recognized authentication phrases have a
Copy button. Detection runs locally; no message contents go to an AI service.

Read cursors are shared on the Pi, with a one-time migration from this browser's
previous local read state. The Home Screen badge counts unread conversations,
excluding archived, muted, blocked, and deleted threads. The open app updates and
clears its badge as read state changes; push messages carry a count so iOS can
update it while the app is closed. Other closed devices refresh their badge on
their next incoming push or app opening. No silent badge-only push is sent.
On iPhone, install the app on the Home Screen and enable its push notifications
and system badge permission. Unsupported browsers simply omit the app-icon badge.

## Automatic SIM and carrier settings

The Pi bundles Android's Apache-2.0 carrier identification and Internet APN tables
for offline use. It matches the SIM's home MCC/MNC, IMSI patterns, ICCID prefixes,
SPN, and GID1/GID2 when exposed by ModemManager. Missing identifiers do not satisfy
MVNO rules. Unsupported carrier privilege rules/PNN are not guessed. This is an
AOSP database snapshot, not Google's proprietary Pixel settings or firmware.

The profile monitor detects SIM changes, retains a registered profile, and learns
profiles by SIM and visited network. If registration fails after the grace period,
recovery prioritizes the learned network pairing, then the learned SIM profile,
identified-carrier profiles, home/visited PLMN mappings, and the generic profile.
Only profiles already installed in the dongle are candidates. Failed recovery
restores the previous profile where possible and observes a cooldown. No profile
files are downloaded or flashed. Registration is recorded separately from
unverified SMS/IMS readiness; the system does not send chargeable test messages.

On an explicit **data on** request, the gateway selects an unambiguous supported
Android Internet APN for the current SIM. IMS/MMS-only APNs are excluded. Multiple
matches or unsupported settings fall back to NetworkManager's provider database.
Explicit `CELLULAR_APN` and existing manual NetworkManager APNs take precedence.
Updating the carrier database or running the profile monitor does not enable data
or change a working APN. A previously managed Android APN is replaced on the next
data-on request if the SIM changes.

The Status tab's SIM home operator uses the detected Android carrier identity.
The visited carrier remains separate, so a China Telecom SIM roaming on T-Mobile
is identified as China Telecom. State and source-version information are available
with `sudo /opt/dji-phone-gateway/src/carrier_profile.py --status`.

`src/carrier-data.json` records exact upstream commits and source SHA-256 digests.
To rebuild it, run `python3 scripts/update-carrier-data.py`; to update it, supply
reviewed exact commits with `--carrier-revision` and `--apn-revision`, then deploy
the resulting database with the app. Attribution and license are in
`src/carrier-data-NOTICE` and `src/carrier-data-LICENSE`.

## Hardware and network requirements

- Raspberry Pi 3B running 64-bit Raspberry Pi OS Lite. Trixie is supported; the installer builds Asterisk 20 LTS when the OS repository does not provide Asterisk.
- DJI 4G module connected over **USB**. SPI is not used by this modem.
- A separately powered USB hub or modem carrier. LTE transmit bursts can exceed what a Pi 3B USB port supplies reliably.
- A voice/VoLTE-enabled SIM and an EG25 firmware/carrier combination that supports voice. SMS/data working does not prove that voice is provisioned.
- iPhone and Pi reachable through the same trusted Wi-Fi or a VPN such as Tailscale. Do not expose SIP/AMI to the public Internet.

The one-time USB identity conversion (`2ca3:4006` to `2c7c:0125`) is described in the [reference project](https://github.com/wlzh/dji-4g-vohive-mac). The installer can also bind the original DJI ID temporarily, but the Quectel identity is more reliable.

## Install

### One-line release install

Published releases include a checksum-verified bootstrap installer. After replacing
`OWNER/REPOSITORY` with this project's GitHub location, a new Raspberry Pi can be
installed with:

```sh
curl -fsSL https://github.com/OWNER/REPOSITORY/releases/latest/download/install.sh | sudo sh
```

The bootstrap downloads the matching release archive, verifies its SHA-256 digest,
and runs the installer from that verified bundle. It accepts
`DJI_GATEWAY_RELEASE_URL` as an override for offline mirrors and release testing.

To publish a release, update `VERSION`, commit the change, and push a matching tag:

```sh
git tag v0.1.0
git push origin main v0.1.0
```

GitHub Actions tests the project and attaches `install.sh`, the release archive,
and its checksum to the GitHub Release automatically.

### Install from a checkout

Clone/copy this directory onto the Pi, then:

```sh
sudo ./install.sh
sudo nano /etc/dji-phone-gateway/gateway.env
sudo /opt/dji-phone-gateway/bin/dji-gateway-diag
sudo systemctl restart asterisk dji-sms-bridge
```

The installer asks for a SIP password and writes generated Asterisk configuration. It does not enable cellular data.

Create a Telegram bot with BotFather and place its token in `TELEGRAM_BOT_TOKEN`. Send any message to the bot, then run:

```sh
sudo /opt/dji-phone-gateway/bin/dji-gateway-diag --telegram
```

Copy the reported chat ID into `TELEGRAM_CHAT_ID`; restart the bridge. Only that exact chat ID is accepted.

Alternatively, open `http://<pi-address>:8080/`, sign in as `admin` with the generated dashboard password, paste the BotFather token, and choose **Find chat IDs**. After you have sent the bot any Telegram message, the dashboard shows your chat ID. Enter it and choose **Save & send test**.

Configure the iPhone SIP client:

- Server: the Pi's Wi-Fi/VPN IP
- Transport: UDP (trusted LAN/VPN only)
- Port: `5060`
- Username: `iphone`
- Password: the password chosen during installation

Dial normal phone numbers from the SIP app. Incoming cellular calls ring extension `iphone` for 45 seconds.

## Modem setup

After USB conversion, Linux normally creates several `/dev/ttyUSB*` ports. On the original DJI `2ca3:4006` firmware tested here, AT commands use interface 3 and serial audio uses interface 1; the installer creates stable `/dev/dji-modem-at` and `/dev/dji-modem-audio` links. Converted Quectel firmware normally uses AT interface 2. Confirm the links with the diagnostic tool before starting Asterisk.

For UAC audio, the channel driver normally exposes an ALSA device named `Android`. If it is absent, enable it once from the Asterisk console:

```sh
sudo asterisk -rx 'quectel uac apply quectel0'
```

The modem reboots. Then inspect `aplay -l`, restart Asterisk, and rerun diagnostics. Some firmware uses serial audio instead; set `uac=off` and `audio=/dev/ttyUSB1` in `/etc/asterisk/quectel.conf` for that case.

## Data switching

`/data_on` allows the configured WWAN interface and brings up `CELLULAR_CONNECTION` through NetworkManager. `/data_off` tears it down and adds an output firewall block on that interface. Set these in `gateway.env`:

```ini
CELLULAR_CONNECTION=cellular
WWAN_INTERFACE=wwan0
CELLULAR_APN=
```

Leave `CELLULAR_APN` blank to select the APN automatically from the SIM's operator identity using NetworkManager and the Mobile Broadband Provider database. If a carrier is missing from that database, set `CELLULAR_APN` to the value supplied by the carrier as a manual override. The profile never autoconnects, and call/SMS registration does not require a NetworkManager data session.

Carrier firmware selection is also automatic. `dji-carrier-profile.timer` discovers the current ModemManager index, gives the modem two minutes to select and register with its embedded carrier bundle, and leaves any working registration untouched. If registration fails, it tests only compatible profiles already installed in the modem, restores the original when none work, and waits six hours before retrying. Inspect its decision with:

```sh
sudo /opt/dji-phone-gateway/src/carrier_profile.py --status
sudo /opt/dji-phone-gateway/src/carrier_profile.py --dry-run --force
```

## iPhone notifications

Open the gateway's HTTPS address and add it to your iPhone Home Screen. Open the Home Screen app and use **Forwarding → Destinations → Push notifications** to allow notifications on that device, then use **Send test notification**. Telegram's switch, setup, and test message are also under **Forwarding**. iOS 16.4 or later is required. A regular Safari bookmark and a plain HTTP LAN address cannot receive iPhone Web Push.

The independent **Telegram** switch controls gateway-wide Telegram forwarding. You can use Telegram, Web Push, or both. Turning either off does not stop SMS storage. Push subscriptions apply per device and survive service restarts; enablement starts with new incoming messages, not the existing history. Tapping a notification opens its conversation. The Pi sends encrypted Web Push directly to the browser's push provider; no Apple Developer account is needed.

Push credentials and per-device delivery cursors are stored in `/var/lib/dji-phone-gateway/push.sqlite3` and `push-vapid.pem`. Keep these files across upgrades. Set `PUSH_VAPID_SUBJECT` to your contact URL or `mailto:` address if desired. The installer creates the Python virtual environment and installs `requirements-push.txt`.

## Security

- Keep SIP on Wi-Fi/VPN; the generated PJSIP ACL accepts RFC1918/ULA addresses only.
- The Telegram token is stored root-readable (`0640`) and messages from every other chat are ignored.
- Asterisk AMI listens on localhost only.
- Change the generated SIP and AMI passwords before deployment if the installation output was recorded.

## Files

- `src/sms_bridge.py` — Telegram polling, authorization, SMS send and data controls.
- `bin/dji-data` — idempotent NetworkManager/nftables data switch.
- `bin/dji-gateway-diag` — modem, audio, Asterisk, data and Telegram diagnostics.
- `asterisk/` — PJSIP, AMI, modem and dialplan templates.
- `systemd/` and `udev/` — service and stable modem device name.

## Known constraints

- Emergency calls are intentionally blocked in the example dialplan. This is not a replacement for a normal phone.
- Carrier VoLTE support varies by EG25 firmware, SIM, region, and IMEI policy.
- The installer builds the archived `chan_quectel` driver; future Raspberry Pi OS/Asterisk releases may require a maintained fork.
- DJI's original `2ca3:4006` firmware reports its manufacturer as `Baiwang`; the installer applies a small compatibility patch so the Quectel driver follows its normal initialization path.
- Audio quality and stability depend heavily on clean external modem power.
