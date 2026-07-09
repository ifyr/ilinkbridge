#!/bin/bash
# iLinkBridge 停止脚本
pkill -f "ilinkbridge.py" 2>/dev/null
sleep 1
pgrep -f "ilinkbridge.py" > /dev/null && echo "仍有残留进程" || echo "iLinkBridge 已停止"
