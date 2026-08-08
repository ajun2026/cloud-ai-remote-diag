#!/bin/bash
# cab-server 启动脚本 — 脱离 Hermes 进程组
cd /home/ubuntu/cab-server
exec ./venv/bin/python server.py >> server.log 2>&1
