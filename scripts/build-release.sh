#!/bin/sh
set -eu

src_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
version=${1:-$(cat "$src_dir/VERSION")}
repository=${2:-${GITHUB_REPOSITORY:-}}

if [ -z "$repository" ]; then
    echo "Usage: $0 <version> <github-owner/repository>" >&2
    exit 1
fi
case "$repository" in
    */*) ;;
    *) echo "Repository must use owner/name format." >&2; exit 1 ;;
esac

dist_dir="$src_dir/dist"
stage_root=$(mktemp -d "${TMPDIR:-/tmp}/dji-gateway-package.XXXXXX")
trap 'rm -rf "$stage_root"' EXIT INT TERM
package_dir="$stage_root/dji-phone-gateway-$version"
mkdir -p "$package_dir"

for path in VERSION README.md install.sh asterisk bin config patches src systemd udev; do
    cp -R "$src_dir/$path" "$package_dir/$path"
done

mkdir -p "$dist_dir"
rm -f "$dist_dir/dji-phone-gateway.tar.gz" "$dist_dir/dji-phone-gateway.tar.gz.sha256" "$dist_dir/install.sh"
tar --exclude='*/__pycache__' --exclude='*.pyc' \
    -czf "$dist_dir/dji-phone-gateway.tar.gz" -C "$stage_root" "dji-phone-gateway-$version"
(
    cd "$dist_dir"
    sha256sum dji-phone-gateway.tar.gz > dji-phone-gateway.tar.gz.sha256
)
sed "s|@REPOSITORY@|$repository|g" "$src_dir/packaging/bootstrap.sh.in" > "$dist_dir/install.sh"
chmod 0755 "$dist_dir/install.sh"

echo "Built release $version for $repository in $dist_dir"
