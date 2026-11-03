#!/bin/bash
set -euo pipefail
umask 077
export DEBIAN_FRONTEND=noninteractive
export LANG=C.UTF-8
export DAFEIYU_HOME=/home/mobile
export DAFEIYU_SHARED_MODELS=/home/mobile/data/models.json
export PATH=/home/mobile/tools/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
mkdir -p /home/mobile/data /home/mobile/downloads /home/mobile/backups /tmp
chmod 1777 /tmp

echo '正在准备本机运行组件……'
apt-get update -q
apt-get install -y --no-install-recommends python3 python3-venv python3-pip \
    ca-certificates curl unzip xz-utils xvfb xauth ffmpeg procps dbus \
    libnss3 libgtk-3-0t64 libasound2t64 libgbm1 libxss1 libxtst6 \
    libatk-bridge2.0-0t64 libdrm2 libxcomposite1 libxrandr2 libxdamage1 \
    libnotify4 libsecret-1-0 xdg-utils libgssapi-krb5-2 fonts-wqy-microhei tzdata

if [ ! -x /home/mobile/tools/bin/python3 ]; then
    python3 -m venv /home/mobile/tools
fi
/home/mobile/tools/bin/python3 -m pip install --disable-pip-version-check --no-cache-dir setuptools wheel
/home/mobile/tools/bin/python3 -m pip install --disable-pip-version-check --no-cache-dir --no-build-isolation -r /opt/mobile/requirements.txt

echo '正在校验并安装本软件私有 QQ 组件……'
/home/mobile/tools/bin/python3 /opt/mobile/install-qq.py

/home/mobile/tools/bin/python3 - <<'PY'
import json, os, sys
sys.path.insert(0,'/opt/mobile')
from mobile_policy import initialize, config_for_mobile
initialize()
config_for_mobile()
path='/home/mobile/data/runtime-ready.json'
with open(path+'.tmp','w',encoding='utf-8') as f:
    json.dump({'schemaVersion':1,'ready':True},f)
os.chmod(path+'.tmp',0o600)
os.replace(path+'.tmp',path)
PY
apt-get clean
echo '运行组件准备完成。请填写新小号、管理员与 AI 接口，再登录。'
