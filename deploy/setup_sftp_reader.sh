#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo 运行此脚本。" >&2
  exit 1
fi

PUBLIC_KEY_FILE="${1:-}"
if [[ -z "${PUBLIC_KEY_FILE}" || ! -f "${PUBLIC_KEY_FILE}" ]]; then
  echo "用法: sudo bash deploy/setup_sftp_reader.sh <Windows 客户端公钥文件>" >&2
  exit 1
fi

ARCHIVE_DIR="${SMSI_ARCHIVE_ROOT:-/data/smsi-archive}"
READER_USER="smsi-archive-reader"
ARCHIVE_GROUP="smsi-archive"
CHROOT_DIR="/srv/smsi-archive-sftp"
CHROOT_ARCHIVE="${CHROOT_DIR}/archive"
AUTHORIZED_KEYS_DIR="/etc/ssh/authorized_keys"
AUTHORIZED_KEY_FILE="${AUTHORIZED_KEYS_DIR}/${READER_USER}"
SSH_DROP_IN="/etc/ssh/sshd_config.d/smsi-archive-reader.conf"
FSTAB_ENTRY="${ARCHIVE_DIR} ${CHROOT_ARCHIVE} none bind,ro,nofail,x-systemd.requires-mounts-for=/data 0 0"

command -v sshd >/dev/null || { echo "缺少 OpenSSH Server。" >&2; exit 1; }
[[ -d "${ARCHIVE_DIR}" ]] || { echo "归档目录不存在: ${ARCHIVE_DIR}" >&2; exit 1; }

PUBLIC_KEY="$(awk 'NF && $1 !~ /^#/ {print; count++} END {if (count != 1) exit 1}' "${PUBLIC_KEY_FILE}")" || {
  echo "公钥文件必须只包含一把 SSH 公钥。" >&2
  exit 1
}
if [[ ! "${PUBLIC_KEY}" =~ ^(ssh-ed25519|sk-ssh-ed25519@openssh.com)[[:space:]]+[A-Za-z0-9+/=]+([[:space:]].*)?$ ]]; then
  echo "只接受 Ed25519 SSH 公钥。" >&2
  exit 1
fi

getent group "${ARCHIVE_GROUP}" >/dev/null || groupadd --system "${ARCHIVE_GROUP}"
if ! id "${READER_USER}" >/dev/null 2>&1; then
  useradd --system --gid "${ARCHIVE_GROUP}" --home-dir / --shell /usr/sbin/nologin "${READER_USER}"
else
  usermod --gid "${ARCHIVE_GROUP}" --home / --shell /usr/sbin/nologin "${READER_USER}"
fi
# "*" 不能用于密码登录，但不会让 sshd 在公钥认证前判定账号已锁定。
usermod --password '*' "${READER_USER}"

install -d -o root -g root -m 0755 "${CHROOT_DIR}"
if mountpoint -q "${CHROOT_ARCHIVE}"; then
  if [[ "$(stat -c '%d:%i' "${CHROOT_ARCHIVE}")" != "$(stat -c '%d:%i' "${ARCHIVE_DIR}")" ]]; then
    echo "挂载点已被其他来源占用: ${CHROOT_ARCHIVE}" >&2
    exit 1
  fi
else
  install -d -o root -g root -m 0755 "${CHROOT_ARCHIVE}"
fi
install -d -o root -g root -m 0755 "${AUTHORIZED_KEYS_DIR}"
printf '%s\n' "${PUBLIC_KEY}" > "${AUTHORIZED_KEY_FILE}"
chown root:root "${AUTHORIZED_KEY_FILE}"
# sshd 以目标用户身份读取该文件；公钥可读但仍只有 root 可以修改。
chmod 0644 "${AUTHORIZED_KEY_FILE}"

chgrp -R "${ARCHIVE_GROUP}" "${ARCHIVE_DIR}"
chmod -R g+rX,o-rwx "${ARCHIVE_DIR}"

if mountpoint -q "${CHROOT_ARCHIVE}"; then
  :
else
  mount --bind "${ARCHIVE_DIR}" "${CHROOT_ARCHIVE}"
fi
mount -o remount,bind,ro "${CHROOT_ARCHIVE}"

if ! grep -Fqx "${FSTAB_ENTRY}" /etc/fstab; then
  if awk -v target="${CHROOT_ARCHIVE}" '$2 == target {found=1} END {exit !found}' /etc/fstab; then
    echo "/etc/fstab 已有其他 ${CHROOT_ARCHIVE} 配置，拒绝自动覆盖。" >&2
    exit 1
  fi
  printf '\n%s\n' "${FSTAB_ENTRY}" >> /etc/fstab
fi

cat > "${SSH_DROP_IN}" <<EOF
Match User ${READER_USER}
    ChrootDirectory ${CHROOT_DIR}
    AuthorizedKeysFile ${AUTHORIZED_KEY_FILE}
    AuthenticationMethods publickey
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    ForceCommand internal-sftp -R -d /archive
    AllowAgentForwarding no
    AllowTcpForwarding no
    X11Forwarding no
    PermitTunnel no
    PermitTTY no
EOF
chmod 0644 "${SSH_DROP_IN}"

sshd -t
systemctl reload ssh

echo "只读 SFTP 已启用：${READER_USER}@<Ubuntu IP>:/archive"
