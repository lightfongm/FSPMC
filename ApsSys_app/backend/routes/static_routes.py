# -*- coding: utf-8 -*-
"""静态文件路由"""

import os
from flask import Blueprint, jsonify, send_from_directory
from ..config import STATIC_DIR

bp = Blueprint('static_routes', __name__)


@bp.route('/')
def index():
    return send_from_directory(STATIC_DIR, 'enhanced.html')


@bp.route('/favicon.ico')
def favicon():
    return '', 204


@bp.route('/<path:filename>')
def static_files(filename):
    full_path = os.path.join(STATIC_DIR, filename)
    if os.path.isfile(full_path):
        return send_from_directory(STATIC_DIR, filename)
    return jsonify({'error': 'Not found'}), 404
