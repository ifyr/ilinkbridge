#!/bin/bash
# iLinkBridge 启动脚本
cd ~/ilinkbridge && bash stop_bridge.sh
source .venv/bin/activate
rm -f ilinkbridge.log
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8
nohup python -u ilinkbridge.py > ilinkbridge.log 2>&1 &
echo "iLinkBridge 已启动 PID=$!"
