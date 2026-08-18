#!/bin/bash
# 云端AI远程运维助手 启动脚本 — 脱离 Hermes 进程组（本机部署）
cd /home/ubuntu/projects/cloud-ai-remote-diag
exec ./venv/bin/python server.py >> logs/server.log 2>&1
