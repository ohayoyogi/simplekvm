#!/bin/bash
# Start kvm.py at boot with systemd (scripts/simplekvm.service).
#
#   sudo scripts/install-service.sh install [USER]   install, enable and start
#   sudo scripts/install-service.sh uninstall        stop, disable and remove
#
# kvm.py runs as USER (default: the user who ran sudo) from this checkout,
# with HOME set, so ~/.config/dev_tinker (TLS key), ~/h264enc/gst and
# ~/camera are that user's. USER needs access to the video devices and the
# HID tty (groups video, dialout). Options: /etc/default/simplekvm, created
# with an example on the first install and left alone afterwards; after
# editing it, "sudo systemctl restart simplekvm". Logs: journalctl -u simplekvm
set -euo pipefail

DIR=$(cd "$(dirname "$0")/.." && pwd)
UNIT=/etc/systemd/system/simplekvm.service
DEFAULTS=/etc/default/simplekvm

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

case "${1:-}" in
install)
	U=${2:-${SUDO_USER:-}}
	[ -n "$U" ] && [ "$U" != root ] || { echo "give the user kvm.py runs as"; exit 1; }
	id "$U" >/dev/null
	[ -f "$DIR/kvm.py" ] || { echo "no kvm.py in $DIR"; exit 1; }
	sed -e "s|@USER@|$U|g" -e "s|@DIR@|$DIR|g" "$DIR/scripts/simplekvm.service" > "$UNIT"
	chmod 644 "$UNIT"
	if [ ! -f "$DEFAULTS" ]; then
		cat > "$DEFAULTS" <<'EOF'
# Options for kvm.py (see "python3 kvm.py --help"), e.g.
#   KVM_ARGS="--source csi --fps 60"      C790 / TC358743 on the CSI connector
#   KVM_ARGS="--source capture"           UVC capture stick
# Without --width/--height a TC358743 stream follows the HDMI mode.
KVM_ARGS=""
EOF
	fi
	systemctl daemon-reload
	systemctl enable --now simplekvm.service
	systemctl --no-pager status simplekvm.service | head -5
	;;
uninstall)
	systemctl disable --now simplekvm.service 2>/dev/null || true
	rm -f "$UNIT"
	systemctl daemon-reload
	echo "removed; $DEFAULTS is kept"
	;;
*)
	sed -n '2,12p' "$0"; exit 1 ;;
esac
