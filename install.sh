#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo ./install.sh" >&2
    exit 1
fi

case "$(uname -m)" in armv7l|aarch64) ;; *) echo "This installer targets Raspberry Pi ARM." >&2; exit 1 ;; esac

src_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_root=/opt/dji-phone-gateway
config_root=/etc/dji-phone-gateway

echo "Installing OS packages..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    asterisk asterisk-dev build-essential cmake git libasound2-dev libsqlite3-dev \
    libjansson-dev pkg-config python3 curl usbutils nftables network-manager sudo

echo "Building chan_quectel..."
build_dir=$(mktemp -d /tmp/dji-chan-quectel.XXXXXX)
trap 'rm -rf "$build_dir"' EXIT INT TERM
git clone --depth 1 https://github.com/RoEdAl/asterisk-chan-quectel.git "$build_dir/src"
cmake -S "$build_dir/src" -B "$build_dir/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$build_dir/build" -j2
cmake --install "$build_dir/build"

echo "Installing gateway..."
install -d -m 0755 "$install_root/bin" "$install_root/src" "$config_root"
install -m 0755 "$src_dir/bin/dji-data" "$install_root/bin/dji-data"
install -m 0755 "$src_dir/bin/dji-gateway-diag" "$install_root/bin/dji-gateway-diag"
install -m 0755 "$src_dir/src/sms_bridge.py" "$install_root/src/sms_bridge.py"
install -m 0755 "$src_dir/bin/telegram-notify" /usr/share/asterisk/agi-bin/telegram-notify
install -m 0644 "$src_dir/asterisk/quectel.conf" /etc/asterisk/quectel.conf
install -m 0644 "$src_dir/asterisk/extensions.conf" /etc/asterisk/extensions.conf
install -m 0644 "$src_dir/udev/99-dji-eg25.rules" /etc/udev/rules.d/99-dji-eg25.rules
install -m 0644 "$src_dir/systemd/dji-sms-bridge.service" /etc/systemd/system/dji-sms-bridge.service
install -m 0644 "$src_dir/systemd/dji-data-off.service" /etc/systemd/system/dji-data-off.service
printf 'djigateway ALL=(root) NOPASSWD: /opt/dji-phone-gateway/bin/dji-data on, /opt/dji-phone-gateway/bin/dji-data off, /opt/dji-phone-gateway/bin/dji-data status\n' > /etc/sudoers.d/dji-phone-gateway
chmod 0440 /etc/sudoers.d/dji-phone-gateway

if ! id djigateway >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin --groups asterisk djigateway
fi

if [ -t 0 ]; then
    printf "SIP password for iPhone (blank = generate): "
    stty -echo; IFS= read -r sip_password; stty echo; echo
else
    sip_password=
fi
[ -n "$sip_password" ] || sip_password=$(od -An -N18 -tx1 /dev/urandom | tr -d ' \n')
ami_password=$(od -An -N18 -tx1 /dev/urandom | tr -d ' \n')

sed "s/@SIP_PASSWORD@/$sip_password/g" "$src_dir/asterisk/pjsip.conf.template" > /etc/asterisk/pjsip.conf
sed "s/@AMI_PASSWORD@/$ami_password/g" "$src_dir/asterisk/manager.conf.template" > /etc/asterisk/manager.conf
chmod 0640 /etc/asterisk/pjsip.conf /etc/asterisk/manager.conf
chown root:asterisk /etc/asterisk/pjsip.conf /etc/asterisk/manager.conf

if [ ! -e "$config_root/gateway.env" ]; then
    sed "s/ASTERISK_AMI_PASSWORD=CHANGE_ME/ASTERISK_AMI_PASSWORD=$ami_password/" "$src_dir/config/gateway.env.example" > "$config_root/gateway.env"
fi
chmod 0640 "$config_root/gateway.env"
chown root:asterisk "$config_root/gateway.env"

# AGI inherits Asterisk's environment, not the bridge service EnvironmentFile.
install -d -m 0755 /etc/systemd/system/asterisk.service.d
cat > /etc/systemd/system/asterisk.service.d/dji-gateway.conf <<EOF
[Service]
EnvironmentFile=$config_root/gateway.env
EOF

udevadm control --reload-rules
udevadm trigger
systemctl daemon-reload
systemctl enable asterisk dji-data-off.service dji-sms-bridge.service
systemctl restart asterisk
systemctl start dji-data-off.service

echo
echo "Installed. SIP user: iphone"
echo "SIP password: $sip_password"
echo "Next: edit $config_root/gateway.env, then run $install_root/bin/dji-gateway-diag"
echo "The Telegram bridge remains stopped until its token and chat ID are configured."
