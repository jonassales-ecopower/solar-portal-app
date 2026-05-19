"""
Script para importar dados históricos da API para o banco de dados
Execute com: python importar_historico.py
"""

import requests
import psycopg2
from datetime import datetime, timedelta
from calendar import monthrange

# Configuração do banco de dados (use as mesmas configurações do seu api.py)
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "solar_portal",
    "user": "postgres",
    "password": "991Bog31**"
}

API_URL = "http://127.0.0.1:8000"
CLIENTE_ID = 2

def conectar_banco():
    return psycopg2.connect(**DB_CONFIG)

def distribuir_por_dias(ano, mes, total_mensal):
    """
    Distribui o total mensal uniformemente pelos dias do mês
    Retorna uma lista de dicionários com data e valor diário
    """
    dias_no_mes = monthrange(ano, mes)[1]
    valor_diario = total_mensal / dias_no_mes
    
    dias = []
    for dia in range(1, dias_no_mes + 1):
        data = datetime(ano, mes, dia).date()
        dias.append({
            "data": data,
            "geracao_kwh": round(valor_diario, 2)
        })
    
    return dias

def importar_historico():
    """Busca dados mensais da API e insere no banco de dados"""
    
    print("🔍 Buscando dados históricos da API...")
    
    try:
        # Buscar dados mensais do endpoint
        resp = requests.get(f"{API_URL}/clientes/{CLIENTE_ID}/monitoramento/mensal")
        
        if resp.status_code != 200:
            print(f"❌ Erro ao buscar dados: {resp.status_code}")
            return
        
        dados = resp.json()
        mensal = dados.get("mensal", [])
        
        if not mensal:
            print("❌ Nenhum dado mensal encontrado na API")
            return
        
        print(f"✅ Encontrados {len(mensal)} meses de dados")
        
        # Conectar ao banco
        conn = conectar_banco()
        cur = conn.cursor()
        
        total_inserido = 0
        
        for mes_dado in mensal:
            ano = mes_dado["ano"]
            mes = mes_dado["mes_num"]
            total_mensal = mes_dado["geracao_kwh"]
            
            # Distribuir o total mensal pelos dias do mês
            dias = distribuir_por_dias(ano, mes, total_mensal)
            
            print(f"\n📅 {mes_dado['mes']}/{ano}: {total_mensal} kWh")
            print(f"   Distribuindo por {len(dias)} dias (~{dias[0]['geracao_kwh']} kWh/dia)")
            
            for dia in dias:
                # Verificar se já existe registro para este dia
                cur.execute("""
                    SELECT id FROM historico_geracao 
                    WHERE cliente_id = %s AND data = %s
                """, (CLIENTE_ID, dia["data"]))
                
                existe = cur.fetchone()
                
                if existe:
                    # Atualizar registro existente
                    cur.execute("""
                        UPDATE historico_geracao 
                        SET geracao_kwh = %s
                        WHERE cliente_id = %s AND data = %s
                    """, (dia["geracao_kwh"], CLIENTE_ID, dia["data"]))
                else:
                    # Inserir novo registro
                    cur.execute("""
                        INSERT INTO historico_geracao (cliente_id, data, geracao_kwh)
                        VALUES (%s, %s, %s)
                    """, (CLIENTE_ID, dia["data"], dia["geracao_kwh"]))
                
                total_inserido += 1
            
            conn.commit()
        
        cur.close()
        conn.close()
        
        print(f"\n✅ Importação concluída!")
        print(f"   Total de registros inseridos/atualizados: {total_inserido}")
        print(f"   Período: {len(mensal)} meses")
        
        # Verificar resultado
        conn = conectar_banco()
        cur = conn.cursor()
        cur.execute("""
            SELECT MIN(data) as inicio, MAX(data) as fim, COUNT(*) as total
            FROM historico_geracao 
            WHERE cliente_id = %s
        """, (CLIENTE_ID,))
        resultado = cur.fetchone()
        cur.close()
        conn.close()
        
        print(f"\n📊 Banco de dados agora tem:")
        print(f"   Início: {resultado[0]}")
        print(f"   Fim: {resultado[1]}")
        print(f"   Total de dias: {resultado[2]}")
        
    except Exception as e:
        print(f"❌ Erro: {e}")

if __name__ == "__main__":
    print("=" * 50)
    print("📥 IMPORTADOR DE HISTÓRICO DE GERAÇÃO")
    print("=" * 50)
    
    confirmar = input(f"\nDeseja importar dados para o cliente ID {CLIENTE_ID}? (s/N): ")
    
    if confirmar.lower() == 's':
        importar_historico()
    else:
        print("Operação cancelada.")