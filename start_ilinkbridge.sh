#!/bin/bash
# ILinkBridge 启动脚本
cd /home/ubuntu/ilinkbridge && bash stop_ilinkbridge.sh
source venv/bin/activate
rm -f ilinkbridge.log
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8
nohup python -u ilinkbridge.py > ilinkbridge.log 2>&1 &
echo "ILinkBridge 已启动 PID=$!"
