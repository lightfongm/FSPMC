# -*- coding: utf-8 -*-
"""蓝图注册中心"""

from flask import Flask


def register_blueprints(app: Flask):
    """注册所有路由蓝图"""

    from .static_routes import bp as static_bp
    from .health import bp as health_bp
    from .schedule import bp as schedule_bp
    from .products import bp as products_bp
    from .inventory import bp as inventory_bp
    from .work_orders import bp as work_orders_bp
    from .customers import bp as customers_bp
    from .suppliers import bp as suppliers_bp
    from .purchase_orders import bp as purchase_orders_bp
    from .bom import bp as bom_bp
    from .sap_orders import bp as sap_orders_bp
    from .delivery_plan import bp as delivery_plan_bp
    from .warehouse import bp as warehouse_bp
    from .barcode import bp as barcode_bp
    from .unlimited import bp as unlimited_bp
    from .equipment import bp as equipment_bp
    from .staff import bp as staff_bp
    from .db_admin import bp as db_admin_bp
    from .system_config import bp as system_config_bp
    from .sap_interface import bp as sap_interface_bp
    from .calc_logic import bp as calc_logic_bp
    from .data_mgmt import bp as data_mgmt_bp
    from .export_excel import bp as export_excel_bp
    from .changeover import bp as changeover_bp

    app.register_blueprint(static_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(schedule_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(work_orders_bp)
    app.register_blueprint(customers_bp)
    app.register_blueprint(suppliers_bp)
    app.register_blueprint(purchase_orders_bp)
    app.register_blueprint(bom_bp)
    app.register_blueprint(sap_orders_bp)
    app.register_blueprint(delivery_plan_bp)
    app.register_blueprint(warehouse_bp)
    app.register_blueprint(barcode_bp)
    app.register_blueprint(unlimited_bp)
    app.register_blueprint(equipment_bp)
    app.register_blueprint(staff_bp)
    app.register_blueprint(db_admin_bp)
    app.register_blueprint(system_config_bp)
    app.register_blueprint(sap_interface_bp)
    app.register_blueprint(calc_logic_bp)
    app.register_blueprint(data_mgmt_bp)
    app.register_blueprint(export_excel_bp)
    app.register_blueprint(changeover_bp)
