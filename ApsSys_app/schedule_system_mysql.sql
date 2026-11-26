-- =============================================
-- 孔雀排程系统 V3.0 — MySQL 数据库建表脚本
-- 由 SQL Server 版本转换而来
-- =============================================

CREATE DATABASE IF NOT EXISTS schedule_system CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
USE schedule_system;

SET FOREIGN_KEY_CHECKS = 0;

-- =============================================
-- 1. products — 产品/物料主数据
-- =============================================
DROP TABLE IF EXISTS products;
CREATE TABLE products (
  id VARCHAR(50) PRIMARY KEY,
  material_code VARCHAR(50),
  name VARCHAR(200),
  type VARCHAR(50),
  category VARCHAR(100),
  workshop VARCHAR(100),
  secret VARCHAR(10),
  production_time DECIMAL(10,2),
  production_cycle DECIMAL(10,2),
  batch_size INT,
  energy_coeff DECIMAL(10,2),
  priority VARCHAR(20),
  unit_price DECIMAL(18,6),
  safe_stock DECIMAL(18,3),
  unit VARCHAR(20),
  old_material_code VARCHAR(50),
  material_group VARCHAR(50),
  material_trial_no VARCHAR(100),
  storage_condition VARCHAR(200),
  size_dimension VARCHAR(100),
  created_date VARCHAR(20),
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  data_source VARCHAR(50),
  sap_matnr VARCHAR(50),
  sap_maktx VARCHAR(200),
  sap_meins VARCHAR(10),
  sap_bstme VARCHAR(10),
  sap_ersda VARCHAR(8),
  sap_bismt VARCHAR(50),
  sap_matkl VARCHAR(50),
  sap_mstae VARCHAR(10),
  sap_mhdrz VARCHAR(10),
  sap_mhdhb VARCHAR(10),
  sap_iprkz VARCHAR(2),
  sap_lbtxt VARCHAR(200),
  sap_rbtxt VARCHAR(200),
  sap_groes VARCHAR(200),
  sap_xchpf VARCHAR(2),
  sap_zscfg VARCHAR(50),
  sap_zdjfg VARCHAR(50),
  sap_werks VARCHAR(10),
  sap_zcateg VARCHAR(20),
  sap_zjwlh VARCHAR(50),
  sap_stprs DECIMAL(18,6),
  sap_peinh DECIMAL(18,3),
  sap_zflgmt VARCHAR(50),
  sap_zstock DECIMAL(18,3),
  sap_zsmstd DECIMAL(18,3),
  sap_mmsta VARCHAR(10),
  sap_lvorm VARCHAR(2),
  INDEX idx_products_material_code (material_code),
  INDEX idx_products_sap_matnr (sap_matnr)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 2. sap_planned_orders — SAP计划订单主表
-- =============================================
DROP TABLE IF EXISTS sap_planned_orders;
CREATE TABLE sap_planned_orders (
  id VARCHAR(50) PRIMARY KEY,
  plnum VARCHAR(20) NOT NULL,
  matnr VARCHAR(50),
  maktx VARCHAR(200),
  zcateg VARCHAR(20),
  zcatext VARCHAR(50),
  plwrk VARCHAR(10),
  pwwrk VARCHAR(10),
  psttr VARCHAR(8),
  pedtr VARCHAR(8),
  pertr VARCHAR(8),
  dispo VARCHAR(20),
  paart VARCHAR(10),
  gsmng DECIMAL(18,3),
  meins VARCHAR(10),
  avmng DECIMAL(18,3),
  umskz VARCHAR(10),
  kdauf VARCHAR(20),
  kdpos VARCHAR(10),
  lgort VARCHAR(10),
  paltr VARCHAR(8),
  stlan VARCHAR(10),
  stalt VARCHAR(10),
  ststa VARCHAR(10),
  verid VARCHAR(10),
  arbpl VARCHAR(20),
  rsnum VARCHAR(20),
  auffx VARCHAR(10),
  sobkz VARCHAR(10),
  knttp VARCHAR(10),
  item_count INT DEFAULT 0,
  is_scheduled TINYINT(1) DEFAULT 0,
  scheduled_at DATETIME NULL,
  schedule_order VARCHAR(4) DEFAULT NULL,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  data_source VARCHAR(50) DEFAULT 'sap_pp077b',
  INDEX idx_sap_planned_orders_plnum (plnum),
  INDEX idx_sap_planned_orders_matnr (matnr)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 3. sap_planned_order_items — SAP计划订单子件表
-- =============================================
DROP TABLE IF EXISTS sap_planned_order_items;
CREATE TABLE sap_planned_order_items (
  id VARCHAR(50) PRIMARY KEY,
  plnum VARCHAR(20) NOT NULL,
  rsnum VARCHAR(20) NOT NULL,
  rspos VARCHAR(10) NOT NULL,
  posnr VARCHAR(10),
  matnr VARCHAR(50),
  maktx VARCHAR(200),
  werks VARCHAR(10),
  lgort VARCHAR(10),
  bdter VARCHAR(8),
  bdmng DECIMAL(18,3),
  meins VARCHAR(10),
  enmng DECIMAL(18,3),
  vmeng DECIMAL(18,3),
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  data_source VARCHAR(50),
  INDEX idx_sap_planned_order_items_plnum (plnum),
  INDEX idx_sap_planned_order_items_rsnum (rsnum)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 4. work_orders — 生产工单表
-- =============================================
DROP TABLE IF EXISTS work_orders;
CREATE TABLE work_orders (
  id VARCHAR(50) PRIMARY KEY,
  order_no VARCHAR(100),
  product_id VARCHAR(50),
  quantity DECIMAL(10,2),
  start_date DATE,
  end_date DATE,
  status VARCHAR(20),
  priority VARCHAR(20),
  note LONGTEXT,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  material_code VARCHAR(50),
  material_desc VARCHAR(200),
  plant VARCHAR(10),
  workcenter_name VARCHAR(200),
  plan_qty DECIMAL(10,2),
  confirm_qty DECIMAL(10,2),
  unit VARCHAR(10),
  batch VARCHAR(20),
  client_name VARCHAR(200),
  client_code VARCHAR(50),
  sap_status_text VARCHAR(100),
  order_type VARCHAR(20),
  sales_order_id VARCHAR(50),
  responsible_name VARCHAR(100),
  sap_aufnr VARCHAR(100),
  sap_matnr VARCHAR(50),
  sap_maktx VARCHAR(200),
  sap_werks VARCHAR(10),
  sap_gamng DECIMAL(10,2),
  sap_gstrs DATE,
  sap_gltrs DATE,
  sap_gstrp DATE,
  sap_gltrp DATE,
  sap_stat VARCHAR(10),
  sap_ktxt VARCHAR(100),
  sap_ktext VARCHAR(200),
  sap_arbpl VARCHAR(20),
  sap_charg VARCHAR(20),
  sap_plbez VARCHAR(40),
  sap_dispo VARCHAR(20),
  sap_fevor VARCHAR(20),
  sap_iwerk VARCHAR(10),
  sap_ilgst VARCHAR(4),
  sap_audat DATE,
  sap_bstdk DATE,
  sap_stufe VARCHAR(2),
  sap_pspid VARCHAR(20),
  sap_psp_text VARCHAR(200),
  sap_prctr VARCHAR(10),
  sap_gewrk VARCHAR(20),
  sap_funk1 VARCHAR(20),
  sap_stort VARCHAR(20),
  sap_meins VARCHAR(10),
  sap_bmeng DECIMAL(14,4),
  sap_meinh VARCHAR(10),
  sap_kunnr VARCHAR(50),
  sap_name1 VARCHAR(200),
  INDEX idx_work_orders_sap_aufnr (sap_aufnr),
  INDEX idx_work_orders_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 5. work_orders_items — 工单组件明细
-- =============================================
DROP TABLE IF EXISTS work_orders_items;
CREATE TABLE work_orders_items (
  id VARCHAR(100) PRIMARY KEY,
  aufnr VARCHAR(100) NOT NULL,
  rsnum VARCHAR(50),
  rspos VARCHAR(50),
  posnr VARCHAR(50),
  rsmatnr VARCHAR(50),
  matnr VARCHAR(50),
  comp_matnr VARCHAR(50),
  comp_desc VARCHAR(200),
  plan_qty DECIMAL(18,3),
  unit VARCHAR(20),
  issued_qty DECIMAL(18,3),
  available_qty DECIMAL(18,3),
  requirement_date DATE,
  plant VARCHAR(20),
  storage_loc VARCHAR(20),
  item_category VARCHAR(20),
  postp VARCHAR(20),
  is_customer_supplied VARCHAR(10) DEFAULT 'N',
  comp_menge DECIMAL(18,3),
  comp_meins VARCHAR(20),
  comp_maktx VARCHAR(200),
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  data_source VARCHAR(50),
  INDEX idx_work_orders_items_aufnr (aufnr)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 6. inventory — 库存表
-- =============================================
DROP TABLE IF EXISTS inventory;
CREATE TABLE inventory (
  id VARCHAR(100) PRIMARY KEY,
  material_code VARCHAR(50),
  name VARCHAR(200),
  quantity DECIMAL(18,3),
  unit VARCHAR(20),
  location VARCHAR(100),
  min_stock DECIMAL(18,3),
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  sap_matnr VARCHAR(50),
  sap_maktx VARCHAR(200),
  sap_werks VARCHAR(20),
  sap_lgort VARCHAR(20),
  sap_charg VARCHAR(50),
  sap_labst DECIMAL(18,3),
  sap_insme DECIMAL(18,3),
  sap_speme DECIMAL(18,3),
  sap_umlme DECIMAL(18,3),
  sap_meins VARCHAR(20),
  sap_raw_json LONGTEXT,
  lgort_name VARCHAR(100),
  sap_sobkz VARCHAR(20),
  stock_type VARCHAR(10),
  INDEX idx_inventory_material_code (material_code),
  INDEX idx_inventory_sap_matnr (sap_matnr)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 7. sap_sales_orders — SAP销售订单表头
-- =============================================
DROP TABLE IF EXISTS sap_sales_orders;
CREATE TABLE sap_sales_orders (
  id VARCHAR(50) PRIMARY KEY,
  vbeln VARCHAR(20) NOT NULL,
  erdat VARCHAR(8),
  audat VARCHAR(8),
  vdatu VARCHAR(8),
  kunnr VARCHAR(20),
  name1 VARCHAR(200),
  auart VARCHAR(10),
  augru VARCHAR(10),
  waerk VARCHAR(10),
  vkorg VARCHAR(10),
  vtweg VARCHAR(10),
  spart VARCHAR(10),
  vkbur VARCHAR(10),
  vkgrp VARCHAR(10),
  bstnk VARCHAR(500),
  bstdk VARCHAR(8),
  status VARCHAR(20) DEFAULT 'open',
  item_count INT DEFAULT 0,
  note LONGTEXT,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  data_source VARCHAR(50),
  gbstk VARCHAR(10),
  lfstk VARCHAR(10),
  INDEX idx_sap_sales_orders_vbeln (vbeln)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 8. sap_sales_order_items — SAP销售订单行项目
-- =============================================
DROP TABLE IF EXISTS sap_sales_order_items;
CREATE TABLE sap_sales_order_items (
  id VARCHAR(50) PRIMARY KEY,
  vbeln VARCHAR(20) NOT NULL,
  posnr VARCHAR(10) NOT NULL,
  matnr VARCHAR(50),
  arktx VARCHAR(200),
  kwmeng DECIMAL(18,3),
  vrkme VARCHAR(10),
  netpr DECIMAL(18,6),
  kpein INT,
  kmein VARCHAR(10),
  werks VARCHAR(10),
  lgort VARCHAR(10),
  vdatu VARCHAR(8),
  abgru VARCHAR(10),
  note LONGTEXT,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  nokwmeng DECIMAL(18,3),
  lfimg DECIMAL(18,3),
  gbsta VARCHAR(50),
  lfsta VARCHAR(50),
  kzwi1 DECIMAL(18,6),
  mwsbp DECIMAL(18,6),
  pstyv VARCHAR(50),
  INDEX idx_sap_sales_order_items_vbeln (vbeln)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 9. bom — BOM表
-- =============================================
DROP TABLE IF EXISTS bom;
CREATE TABLE bom (
  bom_key VARCHAR(200) PRIMARY KEY,
  parent_matnr VARCHAR(50),
  parent_maktx VARCHAR(200),
  werks VARCHAR(10),
  stlan VARCHAR(10),
  stlal VARCHAR(10),
  posnr VARCHAR(20),
  comp_matnr VARCHAR(50),
  comp_maktx VARCHAR(200),
  comp_menge DECIMAL(18,4),
  comp_meins VARCHAR(10),
  postp VARCHAR(10),
  comp_datuv VARCHAR(20),
  comp_datub VARCHAR(20),
  lgort VARCHAR(10),
  ausch DECIMAL(10,4),
  sortf VARCHAR(20),
  data_source VARCHAR(50),
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_bom_parent (parent_matnr),
  INDEX idx_bom_comp (comp_matnr)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 10. peacock_schedule — 排程结果表
-- =============================================
DROP TABLE IF EXISTS peacock_schedule;
CREATE TABLE peacock_schedule (
  id VARCHAR(100) PRIMARY KEY,
  aufnr VARCHAR(100) NOT NULL,
  order_no VARCHAR(50),
  material_code VARCHAR(50) NOT NULL,
  old_material_code VARCHAR(50),
  material_name VARCHAR(200),
  material_type VARCHAR(50),
  plant VARCHAR(20),
  work_center VARCHAR(50),
  order_status VARCHAR(20),
  plan_qty DECIMAL(18,3),
  issued_qty DECIMAL(18,3),
  required_qty DECIMAL(18,3),
  start_date DATE,
  finish_date DATE,
  customer_code VARCHAR(50),
  customer_name VARCHAR(200),
  customer_priority INT,
  ready_status VARCHAR(20),
  status_description VARCHAR(500),
  ready_components INT,
  partial_components INT,
  missing_components INT,
  total_components INT,
  peacock_status VARCHAR(20),
  peacock_description VARCHAR(500),
  suggest_order VARCHAR(10),
  suggest_reason VARCHAR(500),
  due_date_priority INT,
  customer_priority_score INT,
  overall_priority INT,
  schedule_sequence INT,
  has_parent_bom TINYINT(1) DEFAULT 0,
  has_child_bom TINYINT(1) DEFAULT 0,
  cost_center_type VARCHAR(50),
  special_condition VARCHAR(20),
  calculated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  calculated_by VARCHAR(50),
  is_deleted TINYINT(1) DEFAULT 0,
  source_refs TEXT,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_peacock_schedule_aufnr (aufnr),
  INDEX idx_peacock_schedule_status (peacock_status),
  INDEX idx_peacock_schedule_priority (overall_priority),
  INDEX idx_peacock_schedule_material (material_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 11. peacock_material_shortage — 缺料明细表
-- =============================================
DROP TABLE IF EXISTS peacock_material_shortage;
CREATE TABLE peacock_material_shortage (
  id VARCHAR(100) PRIMARY KEY,
  schedule_id VARCHAR(100) NOT NULL,
  aufnr VARCHAR(100) NOT NULL,
  material_code VARCHAR(50) NOT NULL,
  component_code VARCHAR(50) NOT NULL,
  component_name VARCHAR(200),
  component_type VARCHAR(50),
  required_qty DECIMAL(18,3),
  available_qty DECIMAL(18,3),
  current_stock DECIMAL(18,3),
  quality_stock DECIMAL(18,3),
  frozen_stock DECIMAL(18,3),
  storage_loc VARCHAR(500),
  shortage_type VARCHAR(20),
  stock_status VARCHAR(20),
  need_qty DECIMAL(18,3),
  is_parent_material TINYINT(1) DEFAULT 0,
  parent_code VARCHAR(50),
  bom_level INT,
  component_demand_date VARCHAR(20),
  source_refs TEXT,
  use_quality_stock TINYINT(1) DEFAULT 0,
  use_frozen_stock TINYINT(1) DEFAULT 0,
  inventory_detail LONGTEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_peacock_material_shortage_aufnr (aufnr),
  INDEX idx_peacock_material_shortage_schedule (schedule_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 12. peacock_calculation_logs — 计算日志
-- =============================================
DROP TABLE IF EXISTS peacock_calculation_logs;
CREATE TABLE peacock_calculation_logs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  calculation_id VARCHAR(50) NOT NULL,
  log_type VARCHAR(50) NOT NULL,
  timestamp VARCHAR(20),
  description LONGTEXT,
  order_no VARCHAR(50),
  details LONGTEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_calculation_id (calculation_id),
  INDEX idx_order_no (order_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 13. delivery_plan_results — 发货计划结果
-- =============================================
DROP TABLE IF EXISTS delivery_plan_results;
CREATE TABLE delivery_plan_results (
  id INT AUTO_INCREMENT PRIMARY KEY,
  line_no INT NOT NULL,
  created_date VARCHAR(20),
  delivery_date VARCHAR(20),
  material_code VARCHAR(50),
  material_desc VARCHAR(200),
  quantity DECIMAL(18,3),
  unit VARCHAR(20),
  fulfillment_type VARCHAR(50),
  can_deliver VARCHAR(20),
  sales_order_no VARCHAR(20),
  customer_name VARCHAR(200),
  stock_qty DECIMAL(18,3),
  production_qty DECIMAL(18,3),
  remaining_available DECIMAL(18,3),
  plan_finish_date VARCHAR(20),
  due_date VARCHAR(20),
  lead_days INT,
  priority_option VARCHAR(50),
  calculation_batch VARCHAR(50),
  plant VARCHAR(10),
  note VARCHAR(500),
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 14. customers — 客户主数据
-- =============================================
DROP TABLE IF EXISTS customers;
CREATE TABLE customers (
  id VARCHAR(50),
  name VARCHAR(200),
  code VARCHAR(50) NOT NULL COMMENT '客户编码 KH+3位流水号，业务主键',
  contact_person VARCHAR(100),
  phone VARCHAR(50),
  email VARCHAR(100),
  address VARCHAR(500),
  city VARCHAR(100),
  priority INT DEFAULT 0,
  note LONGTEXT,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  sap_kunnr VARCHAR(20),
  sap_name1 VARCHAR(200),
  sap_name2 VARCHAR(200),
  sap_stras VARCHAR(200),
  sap_pstlz VARCHAR(20),
  sap_ort01 VARCHAR(100),
  sap_land1 VARCHAR(10),
  sap_telf1 VARCHAR(50),
  sap_telfx VARCHAR(50),
  sap_stcd1 VARCHAR(50),
  sap_stcd2 VARCHAR(50),
  sap_ktokd VARCHAR(10),
  sap_bukrs VARCHAR(10),
  sap_vkorg VARCHAR(10),
  sap_vtweg VARCHAR(10),
  sap_spart VARCHAR(10),
  sap_kdgrp VARCHAR(10),
  sap_klaun VARCHAR(10),
  sap_pltyp VARCHAR(10),
  sap_kalks VARCHAR(10),
  sap_kkber VARCHAR(10),
  sap_counc VARCHAR(10),
  sap_lifnr VARCHAR(20),
  sap_bahns VARCHAR(20),
  sap_knrza VARCHAR(20),
  sap_stenr VARCHAR(20),
  sap_datlt VARCHAR(50),
  sap_telf2 VARCHAR(50),
  INDEX idx_customers_sap_kunnr (sap_kunnr),
  PRIMARY KEY (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 14.1 suppliers — 供应商主数据
-- =============================================
DROP TABLE IF EXISTS suppliers;
CREATE TABLE suppliers (
  supplier_code  VARCHAR(50)  NOT NULL COMMENT '供应商编码 G+3位流水号',
  supplier_name  VARCHAR(200) NOT NULL COMMENT '供应商名称',
  contact_person VARCHAR(100) COMMENT '联系人',
  phone          VARCHAR(50)  COMMENT '电话',
  note           VARCHAR(500) COMMENT '备注',
  updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (supplier_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 14.2 purchase_orders — 采购入库表
-- =============================================
DROP TABLE IF EXISTS purchase_orders;
CREATE TABLE purchase_orders (
  po_no            VARCHAR(50)  NOT NULL COMMENT '采购入库单号 CR+4位流水号，主键',
  purchase_order_no VARCHAR(50) COMMENT '采购订单号（引用原采购订单，非空）',
  order_date       DATE         COMMENT '订单日期',
  supplier_code    VARCHAR(50)  COMMENT '供应商编码，引用 suppliers.supplier_code',
  supplier_name    VARCHAR(200) COMMENT '供应商名称（冗余存储）',
  material_code    VARCHAR(50)  COMMENT '物料编码，引用 products.material_code',
  material_name    VARCHAR(200) COMMENT '物料名称（冗余存储）',
  order_qty        DECIMAL(18,3) DEFAULT 0 COMMENT '订单数量',
  arrival_qty      DECIMAL(18,3) DEFAULT 0 COMMENT '计划到货数量',
  actual_arrival_qty DECIMAL(18,3) DEFAULT NULL COMMENT '实际到货数量（未填为 NULL）',
  actual_arrival_date DATE COMMENT '实际到货日期（实际到货数量已填时必填）',
  status           VARCHAR(20) DEFAULT '在途' COMMENT '状态:创建/在途/关闭/作废',
  unarrived_qty    DECIMAL(18,3) DEFAULT 0 COMMENT '未到货数量（按采购订单号自动计算）',
  plan_arrival_date DATE         COMMENT '计划到货日期',
  note             VARCHAR(500)  COMMENT '备注',
  updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (po_no),
  INDEX idx_po_supplier (supplier_code),
  INDEX idx_po_material (material_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 15. equipment — 设备表
-- =============================================
DROP TABLE IF EXISTS equipment;
CREATE TABLE equipment (
  id VARCHAR(50) PRIMARY KEY,
  name VARCHAR(200),
  type VARCHAR(50),
  workshop VARCHAR(100),
  capacity DECIMAL(10,2),
  unit VARCHAR(20),
  status VARCHAR(20) DEFAULT 'active',
  note LONGTEXT,
  start_time DATETIME DEFAULT NULL COMMENT '开始时间（保养/维修时必填）',
  end_time DATETIME DEFAULT NULL COMMENT '结束时间（保养/维修时必填）',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 16. staff — 人员表
-- =============================================
DROP TABLE IF EXISTS staff;
CREATE TABLE staff (
  id VARCHAR(50) PRIMARY KEY,
  name VARCHAR(100),
  role VARCHAR(50),
  team VARCHAR(100),
  shift VARCHAR(20),
  phone VARCHAR(50),
  status VARCHAR(20) DEFAULT 'active',
  note LONGTEXT,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 17. sales_orders — 销售订单(旧)
-- =============================================
DROP TABLE IF EXISTS sales_orders;
CREATE TABLE sales_orders (
  id VARCHAR(50) PRIMARY KEY,
  order_no VARCHAR(100),
  customer_id VARCHAR(50),
  customer_name VARCHAR(200),
  material_code VARCHAR(50),
  material_desc VARCHAR(200),
  quantity DECIMAL(18,3),
  unit VARCHAR(20),
  delivery_date DATE,
  status VARCHAR(20) DEFAULT 'open',
  priority INT DEFAULT 0,
  note LONGTEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_sales_orders_order_no (order_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 18. schedules — 排程表(旧)
-- =============================================
DROP TABLE IF EXISTS schedules;
CREATE TABLE schedules (
  id VARCHAR(50) PRIMARY KEY,
  work_order_id VARCHAR(50),
  equipment_id VARCHAR(50),
  start_time DATETIME,
  end_time DATETIME,
  status VARCHAR(20) DEFAULT 'planned',
  note LONGTEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_schedules_work_order (work_order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 19. warehouse_locations — 仓库仓位
-- =============================================
DROP TABLE IF EXISTS warehouse_locations;
CREATE TABLE warehouse_locations (
  id VARCHAR(50) PRIMARY KEY,
  warehouse_code VARCHAR(50),
  warehouse_name VARCHAR(200),
  warehouse_type VARCHAR(50),
  location_code VARCHAR(50),
  is_enabled TINYINT(1) DEFAULT 1,
  note LONGTEXT,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_warehouse_locations_code (warehouse_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 20. barcode_inventory — 条码库存
-- =============================================
DROP TABLE IF EXISTS barcode_inventory;
CREATE TABLE barcode_inventory (
  id VARCHAR(50) PRIMARY KEY,
  material_code VARCHAR(50),
  material_desc VARCHAR(200),
  quantity DECIMAL(18,3),
  unit VARCHAR(20),
  warehouse_code VARCHAR(50),
  location_code VARCHAR(50),
  supplier_batch VARCHAR(100),
  sap_batch VARCHAR(100),
  production_date VARCHAR(20),
  expiry_date VARCHAR(20),
  status VARCHAR(20) DEFAULT 'active',
  note LONGTEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_barcode_inventory_material (material_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 21. unlimited_materials — 无限量物料
-- =============================================
DROP TABLE IF EXISTS unlimited_materials;
CREATE TABLE unlimited_materials (
  id INT AUTO_INCREMENT PRIMARY KEY,
  material_code VARCHAR(50) NOT NULL,
  material_desc VARCHAR(200),
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_unlimited_material_code (material_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 22. system_config — 系统配置
-- =============================================
DROP TABLE IF EXISTS system_config;
CREATE TABLE system_config (
  config_key VARCHAR(100) PRIMARY KEY,
  config_value LONGTEXT,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 23. machine_configs — 机器配置
-- =============================================
DROP TABLE IF EXISTS machine_configs;
CREATE TABLE machine_configs (
  id VARCHAR(50) PRIMARY KEY,
  mac_address VARCHAR(50) UNIQUE,
  machine_name VARCHAR(200),
  db_host VARCHAR(100),
  db_port INT DEFAULT 3306,
  db_user VARCHAR(100),
  db_password VARCHAR(200),
  db_name VARCHAR(100),
  is_authorized TINYINT(1) DEFAULT 0,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 24. data_log — 操作日志
-- =============================================
DROP TABLE IF EXISTS data_log;
CREATE TABLE data_log (
  id INT AUTO_INCREMENT PRIMARY KEY,
  operation VARCHAR(100),
  table_name VARCHAR(100),
  record_count INT,
  operator VARCHAR(100),
  note LONGTEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 25. data_snapshots — 快照
-- =============================================
DROP TABLE IF EXISTS data_snapshots;
CREATE TABLE data_snapshots (
  id INT AUTO_INCREMENT PRIMARY KEY,
  snapshot_name VARCHAR(200),
  snapshot_data LONGTEXT,
  created_by VARCHAR(100),
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- =============================================
-- 26. energy_config — 能耗配置
-- =============================================
DROP TABLE IF EXISTS energy_config;
CREATE TABLE energy_config (
  id VARCHAR(50) PRIMARY KEY,
  equipment_id VARCHAR(50),
  energy_factor DECIMAL(10,4) DEFAULT 1.0000,
  preferred_hour INT DEFAULT 0,
  peak_start INT DEFAULT 8,
  peak_end INT DEFAULT 22,
  note LONGTEXT,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_energy_config_equipment (equipment_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SET FOREIGN_KEY_CHECKS = 1;

-- =============================================
-- 初始化默认配置数据
-- =============================================
INSERT INTO system_config (config_key, config_value) VALUES
('db_type', 'mysql'),
('db_host', '127.0.0.1'),
('db_port', '3306'),
('db_user', 'root'),
('db_password', ''),
('db_name', 'schedule_system'),
('sap_prd_host', 'sappo.hbglobal.com'),
('sap_prd_port', '8011'),
('sap_prd4_host', 'sappoprd4.hbglobal.com'),
('sap_prd4_port', '50100'),
('sap_qas_host', 'sappoqas.hbglobal.com'),
('sap_qas_port', '50000'),
('sap_user', 'bc_con'),
-- SAP 密码不在源码中明文存储，请通过系统配置界面设置
('sap_password', ''),
('app_name', '孔雀排程系统'),
('app_version', '3.0'),
('flask_port', '5000')
ON DUPLICATE KEY UPDATE config_value = VALUES(config_value);

-- 示例产品数据
INSERT INTO products (id, material_code, name, type, category, workshop, production_time, production_cycle, batch_size, unit, priority, data_source) VALUES
('P001', 'MAT-001', '基料A', '基料', '化工原料', '一车间', 2.50, 8.00, 500, 'KG', '高', 'manual'),
('P002', 'MAT-002', '中间品B', '中间品', '半制品', '一车间', 3.00, 12.00, 300, 'KG', '中', 'manual'),
('P003', 'MAT-003', '半成品C', '半成品', '制品', '二车间', 4.50, 16.00, 200, 'KG', '高', 'manual'),
('P004', 'MAT-004', '产成品D', '产成品', '成品', '二车间', 5.00, 24.00, 100, 'KG', '高', 'manual'),
('P005', 'MAT-005', '基料E', '基料', '化工原料', '一车间', 1.50, 6.00, 1000, 'KG', '低', 'manual')
ON DUPLICATE KEY UPDATE name = VALUES(name);

-- 示例库存数据
INSERT INTO inventory (id, material_code, name, quantity, unit, location, min_stock) VALUES
('INV001', 'MAT-001', '基料A', 5000.000, 'KG', '原料仓A', 1000.000),
('INV002', 'MAT-002', '中间品B', 2000.000, 'KG', '半成品仓B', 500.000),
('INV003', 'MAT-003', '半成品C', 800.000, 'KG', '半成品仓B', 200.000),
('INV004', 'MAT-004', '产成品D', 300.000, 'KG', '成品仓C', 100.000),
('INV005', 'MAT-005', '基料E', 8000.000, 'KG', '原料仓A', 2000.000)
ON DUPLICATE KEY UPDATE name = VALUES(name);

-- 示例客户数据
INSERT INTO customers (id, name, code, priority, sap_kunnr, sap_name1) VALUES
('C001', '测试客户A', 'KH001', 1, '100001', '测试客户A有限公司'),
('C002', '测试客户B', 'KH002', 2, '100002', '测试客户B有限公司'),
('C003', '测试客户C', 'KH003', 1, '100003', '测试客户C有限公司')
ON DUPLICATE KEY UPDATE name = VALUES(name);

-- 示例仓库仓位
INSERT INTO warehouse_locations (id, warehouse_code, warehouse_name, warehouse_type, is_enabled) VALUES
('W001', 'WH-A', '原料仓A', '原料', 1),
('W002', 'WH-B', '半成品仓B', '半成品', 1),
('W003', 'WH-C', '成品仓C', '成品', 1),
('W004', 'WH-D', '包材仓D', '包材', 1)
ON DUPLICATE KEY UPDATE warehouse_name = VALUES(warehouse_name);

-- 示例BOM
INSERT INTO bom (bom_key, parent_matnr, parent_maktx, werks, stlan, stlal, comp_matnr, comp_maktx, comp_menge, comp_meins, postp, lgort) VALUES
('BOM-MAT-003-MAT-001-1000-1-1', 'MAT-003', '半成品C', '1000', '1', '1', 'MAT-001', '基料A', 2.0000, 'KG', 'L', 'WH-A'),
('BOM-MAT-003-MAT-002-1000-1-1', 'MAT-003', '半成品C', '1000', '1', '1', 'MAT-002', '中间品B', 1.5000, 'KG', 'L', 'WH-B'),
('BOM-MAT-004-MAT-003-1000-1-1', 'MAT-004', '产成品D', '1000', '1', '1', 'MAT-003', '半成品C', 2.0000, 'KG', 'L', 'WH-B'),
('BOM-MAT-004-MAT-005-1000-1-1', 'MAT-004', '产成品D', '1000', '1', '1', 'MAT-005', '基料E', 0.5000, 'KG', 'L', 'WH-A')
ON DUPLICATE KEY UPDATE comp_menge = VALUES(comp_menge);

-- 示例设备
INSERT INTO equipment (id, name, type, workshop, capacity, unit, status) VALUES
('EQ001', '反应釜1号', '反应釜', '一车间', 500.00, 'KG', 'active'),
('EQ002', '反应釜2号', '反应釜', '一车间', 1000.00, 'KG', 'active'),
('EQ003', '混合机1号', '混合机', '二车间', 300.00, 'KG', 'active'),
('EQ004', '包装机1号', '包装机', '二车间', 200.00, 'KG', 'active')
ON DUPLICATE KEY UPDATE name = VALUES(name);

-- 示例无限量物料
INSERT INTO unlimited_materials (material_code, material_desc) VALUES
('MAT-005', '基料E（无限量）')
ON DUPLICATE KEY UPDATE material_desc = VALUES(material_desc);

SELECT 'Database schedule_system initialized successfully!' AS status;
