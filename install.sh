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
    build-essential cmake git libasound2-dev libsqlite3-dev libjansson-dev \
    pkg-config python3 curl usbutils nftables network-manager modemmanager mobile-broadband-provider-info libqmi-utils sudo wget patch \
    libssl-dev libncurses-dev libnewt-dev libxml2-dev uuid-dev libedit-dev \
    libsrtp2-dev libspandsp-dev libcurl4-openssl-dev libcap-dev python3-dev

asterisk_version=20.21.0
asterisk_sha256=13fd6e8f1fbb19a3174af82a388dd72c87eb2c92d32fca83c4d51bfab03f686a
chan_quectel_commit=5552c365bfb319eed7cbbf6300a67028ab70db9e

download_asterisk() {
    destination=$1
    curl -fL --retry 3 \
        "https://downloads.asterisk.org/pub/telephony/asterisk/releases/asterisk-${asterisk_version}.tar.gz" \
        -o "$destination"
    printf '%s  %s\n' "$asterisk_sha256" "$destination" | sha256sum -c -
}

if command -v asterisk >/dev/null 2>&1; then
    echo "Using already installed Asterisk: $(asterisk -V)"
elif apt-cache policy asterisk | grep -q 'Candidate: [0-9]'; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y asterisk asterisk-dev
else
    echo "Asterisk is unavailable from this OS repository; building Asterisk 20 LTS..."
    asterisk_archive="asterisk-${asterisk_version}.tar.gz"
    asterisk_build=$(mktemp -d /var/tmp/dji-asterisk.XXXXXX)
    download_asterisk "$asterisk_build/$asterisk_archive"
    tar -xzf "$asterisk_build/$asterisk_archive" -C "$asterisk_build"
    cd "$asterisk_build/asterisk-$asterisk_version"
    ./configure --with-pjproject-bundled --with-jansson-bundled
    make -j2
    make install
    make install-headers
    make samples
    cd "$src_dir"

    if ! getent group asterisk >/dev/null; then groupadd --system asterisk; fi
    if ! id asterisk >/dev/null 2>&1; then
        useradd --system --home-dir /var/lib/asterisk --shell /usr/sbin/nologin --gid asterisk asterisk
    fi
    install -d -o asterisk -g asterisk -m 0750 /var/lib/asterisk /var/log/asterisk /var/spool/asterisk /var/run/asterisk
    install -m 0644 "$src_dir/systemd/asterisk-source.service" /etc/systemd/system/asterisk.service
    rm -rf "$asterisk_build"
fi

if [ ! -f /usr/include/asterisk.h ] || [ ! -f /usr/include/asterisk/buildopts.h ]; then
    echo "Installing Asterisk development headers..."
    header_build=$(mktemp -d /var/tmp/dji-asterisk-headers.XXXXXX)
    header_archive="asterisk-${asterisk_version}.tar.gz"
    download_asterisk "$header_build/$header_archive"
    tar -xzf "$header_build/$header_archive" -C "$header_build"
    cd "$header_build/asterisk-$asterisk_version"
    ./configure --with-pjproject-bundled --with-jansson-bundled
    make include/asterisk/buildopts.h
    make install-headers
    cd "$src_dir"
    rm -rf "$header_build"
fi

echo "Building chan_quectel..."
build_dir=$(mktemp -d /var/tmp/dji-chan-quectel.XXXXXX)
trap 'rm -rf "$build_dir"' EXIT INT TERM
git clone https://github.com/RoEdAl/asterisk-chan-quectel.git "$build_dir/src"
git -C "$build_dir/src" checkout --detach "$chan_quectel_commit"
patch -d "$build_dir/src" -p1 < "$src_dir/patches/chan-quectel-baiwang.patch"
cmake -S "$build_dir/src" -B "$build_dir/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$build_dir/build" -j2
cmake --install "$build_dir/build"
asterisk_moddir=$(pkg-config --variable=moddir asterisk)
install -d -m 0755 "$asterisk_moddir"
install -m 0755 "$build_dir/build/src/chan_quectel.so" "$asterisk_moddir/chan_quectel.so"

echo "Installing gateway..."
if ! id djigateway >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin --groups asterisk,dialout djigateway
else
    usermod -a -G asterisk,dialout djigateway
fi
install -d -m 0755 "$install_root/bin" "$install_root/src" "$config_root"
install -d -o djigateway -g asterisk -m 0770 /var/lib/dji-phone-gateway
if [ -e /var/lib/dji-phone-gateway/messages.sqlite3 ]; then
    chgrp asterisk /var/lib/dji-phone-gateway/messages.sqlite3
    chmod 0660 /var/lib/dji-phone-gateway/messages.sqlite3
fi
install -m 0755 "$src_dir/bin/dji-data" "$install_root/bin/dji-data"
install -m 0755 "$src_dir/bin/dji-gateway-diag" "$install_root/bin/dji-gateway-diag"
install -m 0755 "$src_dir/src/sms_bridge.py" "$install_root/src/sms_bridge.py"
install -m 0755 "$src_dir/src/web_dashboard.py" "$install_root/src/web_dashboard.py"
install -m 0644 "$src_dir/src/dji-phone-gateway-icon.svg" "$install_root/src/dji-phone-gateway-icon.svg"
install -m 0644 "$src_dir/src/apple-touch-icon.png" "$install_root/src/apple-touch-icon.png"
asterisk_agidir=$(pkg-config --variable=agidir asterisk)
install -d -o asterisk -g asterisk -m 0755 "$asterisk_agidir"
install -m 0755 "$src_dir/bin/telegram-notify" "$asterisk_agidir/telegram-notify"
install -m 0644 "$src_dir/asterisk/quectel.conf" /etc/asterisk/quectel.conf
install -m 0644 "$src_dir/asterisk/extensions.conf" /etc/asterisk/extensions.conf
install -m 0644 "$src_dir/udev/99-dji-eg25.rules" /etc/udev/rules.d/99-dji-eg25.rules
printf 'qmi_wwan\n' > /etc/modules-load.d/dji-qmi.conf
install -m 0644 "$src_dir/systemd/dji-sms-bridge.service" /etc/systemd/system/dji-sms-bridge.service
install -m 0644 "$src_dir/systemd/dji-data-off.service" /etc/systemd/system/dji-data-off.service
install -m 0644 "$src_dir/systemd/dji-dashboard.service" /etc/systemd/system/dji-dashboard.service
printf 'djigateway ALL=(root) NOPASSWD: /opt/dji-phone-gateway/bin/dji-data on, /opt/dji-phone-gateway/bin/dji-data off, /opt/dji-phone-gateway/bin/dji-data status, /usr/bin/systemctl restart dji-sms-bridge.service\n' > /etc/sudoers.d/dji-phone-gateway
chmod 0440 /etc/sudoers.d/dji-phone-gateway

if [ -e /etc/asterisk/pjsip.conf ] && grep -q '^password=' /etc/asterisk/pjsip.conf; then
    sip_password=$(sed -n 's/^password=//p' /etc/asterisk/pjsip.conf | head -n 1)
else
    if [ -t 0 ]; then
        printf "SIP password for iPhone (blank = generate): "
        stty -echo; IFS= read -r sip_password; stty echo; echo
    else
        sip_password=
    fi
    [ -n "$sip_password" ] || sip_password=$(od -An -N18 -tx1 /dev/urandom | tr -d ' \n')
fi
sed "s/@SIP_PASSWORD@/$sip_password/g" "$src_dir/asterisk/pjsip.conf.template" > /etc/asterisk/pjsip.conf

if [ -e "$config_root/gateway.env" ] && grep -q '^ASTERISK_AMI_PASSWORD=.' "$config_root/gateway.env"; then
    ami_password=$(sed -n 's/^ASTERISK_AMI_PASSWORD=//p' "$config_root/gateway.env" | head -n 1)
else
    ami_password=$(od -An -N18 -tx1 /dev/urandom | tr -d ' \n')
fi
sed "s/@AMI_PASSWORD@/$ami_password/g" "$src_dir/asterisk/manager.conf.template" > /etc/asterisk/manager.conf
chmod 0640 /etc/asterisk/pjsip.conf /etc/asterisk/manager.conf
chown root:asterisk /etc/asterisk/pjsip.conf /etc/asterisk/manager.conf

if [ ! -e "$config_root/gateway.env" ]; then
    sed "s/ASTERISK_AMI_PASSWORD=CHANGE_ME/ASTERISK_AMI_PASSWORD=$ami_password/" "$src_dir/config/gateway.env.example" > "$config_root/gateway.env"
fi
if ! grep -q '^DASHBOARD_PASSWORD=.' "$config_root/gateway.env"; then
    dashboard_password=$(od -An -N15 -tx1 /dev/urandom | tr -d ' \n')
    printf '\nDASHBOARD_HOST=0.0.0.0\nDASHBOARD_PORT=8080\nDASHBOARD_PASSWORD=%s\nGATEWAY_SETTINGS=/var/lib/dji-phone-gateway/settings.json\nSMS_DATABASE=/var/lib/dji-phone-gateway/messages.sqlite3\n' "$dashboard_password" >> "$config_root/gateway.env"
else
    dashboard_password=$(sed -n 's/^DASHBOARD_PASSWORD=//p' "$config_root/gateway.env" | tail -n 1)
fi
chmod 0640 "$config_root/gateway.env"
chown root:asterisk "$config_root/gateway.env"

cellular_apn=$(sed -n 's/^CELLULAR_APN=//p' "$config_root/gateway.env" | tail -n 1)
cellular_connection=$(sed -n 's/^CELLULAR_CONNECTION=//p' "$config_root/gateway.env" | tail -n 1)
cellular_connection=${cellular_connection:-cellular}
if ! nmcli -t -f NAME connection show | grep -Fxq "$cellular_connection"; then
    nmcli connection add type gsm ifname '*' con-name "$cellular_connection" connection.autoconnect no
fi
if [ -n "$cellular_apn" ]; then
    nmcli connection modify "$cellular_connection" gsm.auto-config no gsm.apn "$cellular_apn"
else
    nmcli connection modify "$cellular_connection" gsm.apn '' gsm.auto-config yes
fi

# AGI inherits Asterisk's environment, not the bridge service EnvironmentFile.
install -d -m 0755 /etc/systemd/system/asterisk.service.d
cat > /etc/systemd/system/asterisk.service.d/dji-gateway.conf <<EOF
[Service]
EnvironmentFile=$config_root/gateway.env
EOF

udevadm control --reload-rules
udevadm trigger
modprobe qmi_wwan 2>/dev/null || true
systemctl daemon-reload
systemctl enable asterisk dji-data-off.service dji-sms-bridge.service dji-dashboard.service
systemctl enable ModemManager.service
systemctl restart ModemManager.service
systemctl restart asterisk
systemctl start dji-data-off.service
systemctl restart dji-dashboard.service

echo
echo "Installed. SIP user: iphone"
echo "SIP password: $sip_password"
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):8080/"
echo "Dashboard user: admin"
echo "Dashboard password: $dashboard_password"
echo "Next: edit $config_root/gateway.env, then run $install_root/bin/dji-gateway-diag"
echo "The Telegram bridge remains stopped until its token and chat ID are configured."
