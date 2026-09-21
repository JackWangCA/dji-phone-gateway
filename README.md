# DJI EG25 Raspberry Pi phone gateway

Turn a first-generation DJI 4G dongle (Quectel EG25-G) into a private cellular-to-SIP and SMS-to-Telegram gateway on a Raspberry Pi 3B.

## What works

- Incoming cellular calls ring a SIP client on your iPhone. Use a SIP app with CallKit support (Groundwire, Acrobits Softphone, Linphone, etc.).
- Outgoing calls from that SIP account use the SIM in the dongle.
- Incoming SMS messages are forwarded to one authorized Telegram chat.
- `/sms +15551234567 hello` sends an SMS.
- `/data_on`, `/data_off`, and `/status` control/report the modem data path. Data is off by default.
- A password-protected dashboard on port 8080 configures Telegram, presents SMS as two-way conversations, starts new messages, and switches cellular data.

Telegram bots cannot originate or receive Telegram voice calls and cannot invoke iOS CallKit. Telegram is therefore used only for SMS and controls; SIP supplies the native-call UI.

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
CELLULAR_APN=Wholesale
```

Set `CELLULAR_APN` to the value supplied by your carrier. The installer creates a non-autoconnecting NetworkManager GSM profile when this value is present; it deliberately never guesses an APN. Call/SMS registration does not require a NetworkManager data session.

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
