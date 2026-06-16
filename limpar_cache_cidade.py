#!/usr/bin/env python3
"""
Script para limpar cache de HSP de cidades no banco de dados.
Uso: python limpar_cache_cidade.py EXTREMA
"""

import sys
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

def limpar_cache(cidade):
    """Deleta registro de cache de uma cidade."""
    if not DATABASE_URL:
        print("❌ DATABASE_URL não definida nas variáveis de ambiente")
        return False

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # Deleta o registro
        cur.execute(
            "DELETE FROM cidades_hsp WHERE LOWER(nome_cidade) LIKE LOWER(%s)",
            (f"%{cidade}%",)
        )
        deletados = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()

        if deletados > 0:
            print(f"✓ {deletados} registro(s) deletado(s) para '{cidade}'")
            print("Próxima validação recalculará com novos valores de HSP.")
            return True
        else:
            print(f"⚠ Nenhum registro encontrado para '{cidade}'")
            return False

    except Exception as e:
        print(f"❌ Erro: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python limpar_cache_cidade.py EXTREMA")
        print("Ou:  python limpar_cache_cidade.py 'EXTREMA - MG'")
        sys.exit(1)

    cidade = sys.argv[1]
    limpar_cache(cidade)
