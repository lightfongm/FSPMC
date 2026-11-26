# -*- coding: utf-8 -*-
"""应用配置"""

import os

# 路径
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, 'static')

# 数据库配置
DB_CONFIG = {
    'host': os.environ.get('DB_HOST', '127.0.0.1'),
    'port': int(os.environ.get('DB_PORT', 3306)),
    'user': os.environ.get('DB_USER', 'root'),
    'password': os.environ.get('DB_PASSWORD', ''),
    'database': os.environ.get('DB_NAME', 'schedule_system'),
    'charset': 'utf8mb4',
}

# Flask 配置
FLASK_CONFIG = {
    'JSON_AS_ASCII': False,
    'MAX_CONTENT_LENGTH': 100 * 1024 * 1024,  # 100MB
}

DEFAULT_PORT = 5000
