# -*- coding: utf-8 -*-
"""
孔雀排程系统 V3.0 — 后端 API (MySQL版)
Flask + PyMySQL — 模块化版本
"""

import os
from flask import Flask

from .config import STATIC_DIR, FLASK_CONFIG, DEFAULT_PORT


def create_app():
    """Flask 应用工厂"""
    app = Flask(__name__, static_folder=STATIC_DIR)
    app.config.update(FLASK_CONFIG)

    # 注册所有蓝图
    from .routes import register_blueprints
    register_blueprints(app)

    # 启动自动备份调度器（后台守护线程，单例）
    try:
        from .routes.data_mgmt import start_scheduler
        start_scheduler()
    except Exception:
        pass

    return app
