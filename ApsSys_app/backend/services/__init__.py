# -*- coding: utf-8 -*-
"""排产领域服务层（与 HTTP 路由解耦）。

目前承载「一键排产」的计算内核：序列编码 + 统一解码器 + 局部搜索。
路由层（backend/routes/work_orders.py）只负责取数、编排与落库。
"""
