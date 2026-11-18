-- Tax documents / suppliers / audits / logs schema (prefer API restart after git pull).

CREATE TABLE IF NOT EXISTS dte_versions (
  id INT NOT NULL PRIMARY KEY,
  code VARCHAR(40) NOT NULL UNIQUE,
  name VARCHAR(80) NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS dte_types (
  id INT NOT NULL PRIMARY KEY,
  code VARCHAR(40) NOT NULL UNIQUE,
  name VARCHAR(80) NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS dte_statuses (
  id INT NOT NULL PRIMARY KEY,
  code VARCHAR(40) NOT NULL UNIQUE,
  name VARCHAR(120) NOT NULL DEFAULT '',
  tip_color VARCHAR(20) NOT NULL DEFAULT 'red'
);

CREATE TABLE IF NOT EXISTS suppliers (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  identification_number VARCHAR(40) NOT NULL UNIQUE,
  company_name VARCHAR(220) NOT NULL DEFAULT '',
  created_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audits (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id INT NULL,
  user_email VARCHAR(255) NOT NULL DEFAULT '',
  event_type VARCHAR(40) NOT NULL DEFAULT 'navigation',
  action VARCHAR(120) NOT NULL DEFAULT '',
  path VARCHAR(500) NOT NULL DEFAULT '',
  screen VARCHAR(200) NOT NULL DEFAULT '',
  detail TEXT NOT NULL,
  duration_ms INT NOT NULL DEFAULT 0,
  created_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_audits_user_id (user_id),
  INDEX ix_audits_event_type (event_type),
  INDEX ix_audits_created_at (created_at)
);

CREATE TABLE IF NOT EXISTS logs (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  source VARCHAR(40) NOT NULL DEFAULT 'backend',
  level VARCHAR(20) NOT NULL DEFAULT 'error',
  message TEXT NOT NULL,
  stack TEXT NOT NULL,
  path VARCHAR(500) NOT NULL DEFAULT '',
  user_id INT NULL,
  user_email VARCHAR(255) NOT NULL DEFAULT '',
  meta_json TEXT NOT NULL,
  created_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_logs_source (source),
  INDEX ix_logs_level (level),
  INDEX ix_logs_user_id (user_id),
  INDEX ix_logs_created_at (created_at)
);

INSERT INTO dte_versions (id, code, name) VALUES
  (1, 'issued', 'Issued'),
  (2, 'received', 'Received')
ON DUPLICATE KEY UPDATE code = VALUES(code), name = VALUES(name);

INSERT INTO dte_types (id, code, name) VALUES
  (33, '33', 'Factura'),
  (34, '34', 'Factura Exenta'),
  (39, '39', 'Boleta'),
  (61, '61', 'Nota de crédito')
ON DUPLICATE KEY UPDATE code = VALUES(code), name = VALUES(name);

INSERT INTO dte_statuses (id, code, name, tip_color) VALUES
  (1, 'pending_payment', 'Imputada por Pagar', 'red'),
  (2, 'paid', 'Imputada Pagada', 'green'),
  (3, 'waiting', 'En espera', 'amber')
ON DUPLICATE KEY UPDATE
  code = VALUES(code),
  name = VALUES(name),
  tip_color = VALUES(tip_color);

-- Rename counterpart_rut → identification_number when needed
-- ALTER TABLE tax_documents DROP INDEX uq_tax_documents_doc;
-- ALTER TABLE tax_documents CHANGE COLUMN counterpart_rut identification_number VARCHAR(40) NOT NULL DEFAULT '';
-- ALTER TABLE tax_documents DROP COLUMN counterpart_name;
-- ALTER TABLE tax_documents
--   ADD UNIQUE KEY uq_tax_documents_doc (dte_version_id, dte_type_id, folio, identification_number);

DELETE FROM dte_types WHERE code IN ('issued', 'received', 'emitido', 'recibido');
