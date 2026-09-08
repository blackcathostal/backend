-- Black Cat Instagram knowledge migration
-- Safe to re-run (IF NOT EXISTS / information_schema checks).

USE bc;

-- rooms.features
SET @has_features := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'rooms' AND COLUMN_NAME = 'features'
);
SET @sql := IF(@has_features = 0, 'ALTER TABLE rooms ADD COLUMN features TEXT NULL', 'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- copy legacy caracteristicas -> features if present
SET @has_caract := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'rooms' AND COLUMN_NAME = 'caracteristicas'
);
SET @sql := IF(
  @has_caract > 0 AND @has_features + (@has_features = 0) >= 0,
  'UPDATE rooms SET features = caracteristicas WHERE (features IS NULL OR features = '''') AND caracteristicas IS NOT NULL AND caracteristicas <> ''''',
  'SELECT 1'
);
-- Re-check features after possible add
SET @has_features := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'rooms' AND COLUMN_NAME = 'features'
);
SET @sql := IF(
  @has_caract > 0 AND @has_features > 0,
  'UPDATE rooms SET features = caracteristicas WHERE (features IS NULL OR features = '''') AND caracteristicas IS NOT NULL AND caracteristicas <> ''''',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS hostel_info (
  id INT NOT NULL PRIMARY KEY,
  check_in_time VARCHAR(40) NOT NULL DEFAULT '15:00',
  check_out_time VARCHAR(40) NOT NULL DEFAULT '12:00',
  breakfast_hours VARCHAR(80) NOT NULL DEFAULT '08:00-10:00',
  has_parking TINYINT(1) NOT NULL DEFAULT 0,
  parking_notes TEXT NOT NULL,
  whatsapp_url VARCHAR(255) NOT NULL DEFAULT 'https://wa.me/56949105984',
  address VARCHAR(255) NOT NULL DEFAULT 'Compania de Jesus 1921, Barrio Brasil, Santiago, Chile',
  email VARCHAR(160) NOT NULL DEFAULT 'reservas@blackcathostal.com',
  website VARCHAR(255) NOT NULL DEFAULT 'https://blackcathostal.com',
  extra_notes TEXT NOT NULL,
  updated_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO hostel_info (
  id, check_in_time, check_out_time, breakfast_hours, has_parking, parking_notes,
  whatsapp_url, address, email, website, extra_notes
) VALUES (
  1, '15:00', '12:00', '08:00-10:00', 0,
  'No on-site parking; paid nearby options available in Barrio Brasil.',
  'https://wa.me/56949105984',
  'Compania de Jesus 1921, Barrio Brasil, Santiago, Chile',
  'reservas@blackcathostal.com',
  'https://blackcathostal.com',
  ''
);

CREATE TABLE IF NOT EXISTS common_answers (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  topic VARCHAR(80) NOT NULL DEFAULT 'general',
  keywords VARCHAR(255) NOT NULL DEFAULT '',
  question VARCHAR(255) NOT NULL,
  answer_guide TEXT NOT NULL,
  sort_order INT NOT NULL DEFAULT 0,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX ix_common_answers_id (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
