#!/bin/bash
set -euo pipefail
umask 077
export DEBIAN_FRONTEND=noninteractive
export LANG=C.UTF-8
export DAFEIYU_HOME=/home/mobile
export DAFEIYU_OFFLINE=/opt/offline
export DAFEIYU_SHARED_MODELS=/home/mobile/data/models.json
export PATH=/home/mobile/tools/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
mkdir -p /home/mobile/data /home/mobile/downloads /home/mobile/backups /tmp
chmod 1777 /tmp

echo '正在准备安装包内置运行组件……'
offline=/opt/offline
for required in manifest.json .deb-files.list apt/sources.list; do
    if [ ! -s "$offline/$required" ]; then
        echo '内置组件缺失，请重新安装完整的内置组件版；不会联网补下依赖。' >&2
        exit 1
    fi
done
# This is a fixed signed Ubuntu snapshot. Disable only its expiry check so an
# older APK remains installable; signatures and package hashes stay mandatory.
source_count=0
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        ''|'#'*) continue ;;
        'deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] file:/opt/offline/apt noble main universe'|\
        'deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] file:/opt/offline/apt noble-updates main universe'|\
        'deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] file:/opt/offline/apt noble-security main universe') source_count=$((source_count+1)) ;;
        *) echo '内置系统源不是允许的本地签名快照，已停止。' >&2; exit 1 ;;
    esac
done < "$offline/apt/sources.list"
[ "$source_count" -eq 3 ] || { echo '内置 Ubuntu 快照源清单不完整。' >&2; exit 1; }
apt_options=(
    -o "Dir::Etc::sourcelist=$offline/apt/sources.list"
    -o 'Dir::Etc::sourceparts=-'
    -o 'Acquire::Languages=none'
    -o 'Acquire::By-Hash=false'
    -o 'Acquire::Check-Valid-Until=false'
    -o 'Acquire::AllowInsecureRepositories=false'
    -o 'APT::Get::AllowUnauthenticated=false'
    -o 'Acquire::Retries=0'
)
apt-get "${apt_options[@]}" update -q
mkdir -p /var/cache/apt/archives/partial
count=0
install_specs=()
while IFS= read -r relative || [ -n "$relative" ]; do
    case "$relative" in apt/pool/*.deb) ;; *) echo '内置系统包清单路径无效。' >&2; exit 1 ;; esac
    case "/$relative/" in */../*|*/./*|*\\*|*$'\r'*) echo '内置系统包清单路径越界。' >&2; exit 1 ;; esac
    source="$offline/$relative"
    [ -f "$source" ] && [ ! -L "$source" ] || { echo '内置系统包缺失。' >&2; exit 1; }
    # APT cache names include the epoch whereas pool filenames often do not.
    # Read package metadata without executing maintainer code, then encode ':'
    # as APT's %3a so --no-download can find the verified local archive.
    metadata=$(dpkg-deb --show --showformat='${Package}\t${Version}\t${Architecture}' "$source")
    IFS=$'\t' read -r package version architecture <<< "$metadata"
    [[ "$package" =~ ^[a-z0-9][a-z0-9+.-]+$ && "$version" =~ ^[0-9A-Za-z.+:~-]+$ ]] || { echo '内置系统包元数据无效。' >&2; exit 1; }
    [[ "$architecture" == arm64 || "$architecture" == all ]] || { echo '内置系统包不是 ARM64/通用架构。' >&2; exit 1; }
    cache_version=${version//:/%3a}
    cp "$source" "/var/cache/apt/archives/${package}_${cache_version}_${architecture}.deb"
    # Full signed indices may also advertise newer versions. Request the exact
    # verified archives so APT cannot choose an uncached alternative candidate.
    install_specs+=("$package=$version")
    count=$((count+1))
done < "$offline/.deb-files.list"
[ "$count" -gt 0 ] || { echo '内置系统包清单为空。' >&2; exit 1; }
apt-get "${apt_options[@]}" --no-download install -y --no-install-recommends python3 python3-venv python3-pip \
    ca-certificates curl unzip xz-utils xvfb xauth ffmpeg procps dbus \
    libnss3 libgtk-3-0t64 libasound2t64 libgbm1 libxss1 libxtst6 \
    libatk-bridge2.0-0t64 libdrm2 libxcomposite1 libxrandr2 libxdamage1 \
    libnotify4 libsecret-1-0 xdg-utils libgssapi-krb5-2 fonts-wqy-microhei tzdata "${install_specs[@]}"

if [ ! -x /home/mobile/tools/bin/python3 ]; then
    python3 -m venv /home/mobile/tools
fi
export PIP_NO_INDEX=1
export PIP_FIND_LINKS="$offline/wheels"
export PIP_DISABLE_PIP_VERSION_CHECK=1
/home/mobile/tools/bin/python3 -m pip install --no-index --find-links "$offline/wheels" --only-binary=:all: --disable-pip-version-check --no-cache-dir setuptools wheel
/home/mobile/tools/bin/python3 -m pip install --no-index --find-links "$offline/wheels" --only-binary=:all: --disable-pip-version-check --no-cache-dir --no-build-isolation -r /opt/mobile/requirements.txt

echo '正在校验并准备内置 CPU 识别模型……'
/home/mobile/tools/bin/python3 - <<'PY'
import sys
sys.path.insert(0,'/opt/mobile')
from mobile_asr import install_model
install_model('/home/mobile')
PY

echo '正在安装私有 QQ；仅 QQ 客户端需从腾讯官方联网下载……'
/home/mobile/tools/bin/python3 /opt/mobile/install-qq.py

apt-get clean
/home/mobile/tools/bin/python3 - <<'PY'
import json, os, sys
sys.path.insert(0,'/opt/mobile')
from mobile_policy import initialize, config_for_mobile
initialize()
config_for_mobile()
path='/home/mobile/data/runtime-ready.json'
with open('/opt/offline/manifest.json',encoding='utf-8') as f:
    offline=json.load(f)
with open(path+'.tmp','w',encoding='utf-8') as f:
    json.dump({'schemaVersion':1,'ready':True,'offline_sha256':offline['sha256']},f)
os.chmod(path+'.tmp',0o600)
os.replace(path+'.tmp',path)
PY
echo '运行组件准备完成。请填写新小号、管理员与 AI 接口，再登录。'
