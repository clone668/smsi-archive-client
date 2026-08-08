#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo 运行此安装脚本。" >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_ROOT="${SMSI_ARCHIVE_DATA_ROOT:-/data}"
INSTALL_DIR="${DATA_ROOT}/smsi-archive-client"
STATE_DIR="${DATA_ROOT}/smsi-archive-client-state"
ARCHIVE_DIR="${DATA_ROOT}/smsi-archive"
SERVICE_USER="smsi-archive"
UPDATER_UNIT="smsi-archive-client-updater.service"

command -v python3 >/dev/null || { echo "缺少 python3。" >&2; exit 1; }

PACKAGES=()
dpkg-query -W -f='${Status}' python3-venv 2>/dev/null | grep -q "install ok installed" || PACKAGES+=(python3-venv)
command -v rclone >/dev/null || PACKAGES+=(rclone)
if (( ${#PACKAGES[@]} > 0 )); then
  apt-get update
  apt-get install -y "${PACKAGES[@]}"
fi

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${STATE_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -m 0755 "${INSTALL_DIR}"
install -d -m 0755 /usr/local/libexec
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0700 "${STATE_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${ARCHIVE_DIR}"

if [[ "${SOURCE_DIR}" != "${INSTALL_DIR}" ]]; then
  cp -a "${SOURCE_DIR}/archive_backup" "${SOURCE_DIR}/templates" "${SOURCE_DIR}/static" "${INSTALL_DIR}/"
  install -m 0644 "${SOURCE_DIR}/app.py" "${SOURCE_DIR}/requirements.txt" "${INSTALL_DIR}/"
fi

python3 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
chown -R root:root "${INSTALL_DIR}"
REVISION="${SMSI_ARCHIVE_CLIENT_REVISION:-}"
if [[ -z "${REVISION}" ]] && command -v git >/dev/null 2>&1 && git -C "${SOURCE_DIR}" rev-parse --verify HEAD >/dev/null 2>&1; then
  REVISION="$(git -C "${SOURCE_DIR}" rev-parse HEAD)"
fi
if [[ "${REVISION}" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
  printf '%s\n' "${REVISION,,}" > "${INSTALL_DIR}/.smsi-release"
  chmod 0644 "${INSTALL_DIR}/.smsi-release"
fi

cat > /etc/smsi-archive-client.env <<EOF
SMSI_ARCHIVE_CLIENT_DATA=${STATE_DIR}
SMSI_ARCHIVE_ROOT=${ARCHIVE_DIR}
RCLONE_CONFIG=${STATE_DIR}/rclone.conf
SMSI_ARCHIVE_WEB_HOST=0.0.0.0
SMSI_ARCHIVE_WEB_PORT=8788
HOME=${STATE_DIR}
EOF
chmod 0600 /etc/smsi-archive-client.env

sed \
  -e "s|@@INSTALL_DIR@@|${INSTALL_DIR}|g" \
  -e "s|@@STATE_DIR@@|${STATE_DIR}|g" \
  -e "s|@@ARCHIVE_DIR@@|${ARCHIVE_DIR}|g" \
  "${SOURCE_DIR}/deploy/smsi-archive-client.service" \
  > /etc/systemd/system/smsi-archive-client.service
chmod 0644 /etc/systemd/system/smsi-archive-client.service
install -m 0755 "${SOURCE_DIR}/deploy/smsi-archive-client-updater.py" /usr/local/libexec/smsi-archive-client-updater.py
install -m 0644 "${SOURCE_DIR}/deploy/${UPDATER_UNIT}" "/etc/systemd/system/${UPDATER_UNIT}"
systemctl daemon-reload
systemctl enable --now smsi-archive-client.service
systemctl enable --now "${UPDATER_UNIT}"

echo
echo "安装完成：http://<Ubuntu局域网IP>:8788"
echo "初始密码：sudo cat ${STATE_DIR}/initial-password.txt"
echo "rclone 配置：sudo -u ${SERVICE_USER} rclone --config ${STATE_DIR}/rclone.conf config"
