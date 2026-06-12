#!/bin/bash
# Rendered by deploy pipeline — Baota panel unattended install
set -euo pipefail

BT_USER="{{ bt_user }}"
BT_PASS="{{ bt_password }}"
BT_PORT="{{ bt_port }}"
BT_SAFE="{{ bt_safe_path }}"
SERVER_OS="{{ server_os }}"

if [ -d /www/server/panel ]; then
  echo "Baota panel already installed"
  bt default 2>/dev/null || true
  exit 0
fi

FLAGS=(-u "$BT_USER" -p "$BT_PASS" -P "$BT_PORT" --safe-path "$BT_SAFE" --ssl-disable -y)

install_with_os() {
  local os="$1"
  case "$os" in
    ubuntu|debian)
      wget -O install.sh https://download.bt.cn/install/install-ubuntu_6.0.sh
      bash install.sh "${FLAGS[@]}"
      ;;
    centos)
      yum install -y wget
      wget -O install.sh https://download.bt.cn/install/install_6.0.sh
      sh install.sh "${FLAGS[@]}"
      ;;
    *)
      if [ -f /usr/bin/curl ]; then
        curl -sSO https://download.bt.cn/install/install_panel.sh
      else
        wget -O install_panel.sh https://download.bt.cn/install/install_panel.sh
      fi
      bash install_panel.sh "${FLAGS[@]}"
      ;;
  esac
}

if ! install_with_os "$SERVER_OS"; then
  if [ "$SERVER_OS" != "generic" ]; then
    echo "专用安装脚本失败，降级使用通用安装脚本..."
    install_with_os generic
  else
    exit 1
  fi
fi

bt default 2>/dev/null || true
