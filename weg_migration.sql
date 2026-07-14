-- WEG Integration Migration
-- Execute this SQL on your Render PostgreSQL database to add WEG columns

ALTER TABLE contas ADD COLUMN IF NOT EXISTS weg_email TEXT;
ALTER TABLE contas ADD COLUMN IF NOT EXISTS weg_senha TEXT;
ALTER TABLE contas ADD COLUMN IF NOT EXISTS weg_token TEXT;
ALTER TABLE contas ADD COLUMN IF NOT EXISTS weg_ultimo_sincronismo TIMESTAMP;
ALTER TABLE contas ADD COLUMN IF NOT EXISTS weg_ativo BOOLEAN DEFAULT FALSE;

-- Verify columns were created
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'contas' AND column_name LIKE 'weg_%'
ORDER BY ordinal_position;
