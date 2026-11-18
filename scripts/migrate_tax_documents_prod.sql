-- Tax documents schema v2 (safe-ish; prefer API restart after git pull).

-- Drop legacy track table if both exist
-- DROP TABLE IF EXISTS accounting_track_documents;

RENAME TABLE accounting_track_documents TO tax_documents;

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

INSERT INTO dte_versions (id, code, name) VALUES
  (1, 'issued', 'Emitida'),
  (2, 'received', 'Recibida')
ON DUPLICATE KEY UPDATE code = VALUES(code), name = VALUES(name);

INSERT INTO dte_types (id, code, name) VALUES
  (33, '33', 'Factura'),
  (39, '39', 'Boleta')
ON DUPLICATE KEY UPDATE code = VALUES(code), name = VALUES(name);

INSERT INTO dte_statuses (id, code, name, tip_color) VALUES
  (1, 'pending_payment', 'Imputada por Pagar', 'red'),
  (2, 'paid', 'Imputada Pagada', 'green')
ON DUPLICATE KEY UPDATE
  code = VALUES(code),
  name = VALUES(name),
  tip_color = VALUES(tip_color);

ALTER TABLE tax_documents ADD COLUMN dte_version_id INT NOT NULL DEFAULT 1;
ALTER TABLE tax_documents ADD COLUMN dte_type_id INT NULL;
ALTER TABLE tax_documents ADD COLUMN status_id INT NULL;
ALTER TABLE tax_documents ADD COLUMN period VARCHAR(40) NOT NULL DEFAULT '';

UPDATE tax_documents
SET dte_version_id = CASE
  WHEN LOWER(direction) IN ('recibida', 'received') THEN 2
  ELSE 1
END
WHERE direction IS NOT NULL;

UPDATE tax_documents
SET dte_type_id = CASE
  WHEN dte_type_id IN (33, 39) THEN dte_type_id
  WHEN LOWER(doc_type) LIKE '%boleta%' OR doc_type = '39' THEN 39
  ELSE 33
END;

UPDATE tax_documents SET status_id = 1 WHERE status_id IS NULL;
UPDATE tax_documents SET period = '' WHERE period IS NULL;

ALTER TABLE tax_documents DROP INDEX uq_accounting_track_doc;
ALTER TABLE tax_documents
  ADD UNIQUE KEY uq_tax_documents_doc (dte_version_id, dte_type_id, folio, counterpart_rut);

ALTER TABLE tax_documents DROP COLUMN direction;
ALTER TABLE tax_documents DROP COLUMN doc_type;

DELETE FROM dte_types WHERE id IN (1, 2) AND code IN ('issued', 'received', 'emitido', 'recibido');
