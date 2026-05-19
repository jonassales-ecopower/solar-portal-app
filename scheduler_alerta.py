"""
Scheduler para verificar anomalias diariamente às 20h
Execute com: python scheduler_alerta.py
"""

import requests
import time
from datetime import datetime

API_URL = "http://127.0.0.1:8000"

def verificar_todos_clientes():
    """Busca todos os clientes e verifica anomalias"""
    try:
        # Buscar lista de clientes (endpoint público)
        resp = requests.get(f"{API_URL}/clientes")
        if resp.status_code == 200:
            clientes = resp.json()
            for cliente in clientes:
                cliente_id = cliente["id"]
                print(f"Verificando cliente {cliente['nome']} (ID: {cliente_id})...")
                
                alerta_resp = requests.get(f"{API_URL}/clientes/{cliente_id}/verificar-anomalias")
                if alerta_resp.status_code == 200:
                    dados = alerta_resp.json()
                    alertas = dados.get("alertas", [])
                    
                    for alerta in alertas:
                        if alerta["tipo"] in ["urgente", "atencao"]:
                            print(f"  ⚠️ ALERTA: {alerta['titulo']}")
                            print(f"     {alerta['mensagem']}")
                            # Aqui você pode adicionar envio de e-mail/WhatsApp
                time.sleep(1)  # Evitar sobrecarga
        else:
            print("Erro ao buscar clientes")
    except Exception as e:
        print(f"Erro: {e}")

if __name__ == "__main__":
    print(f"Iniciando verificação diária - {datetime.now()}")
    verificar_todos_clientes()
    print("Verificação concluída!")
    
    # Para rodar continuamente (descomente se quiser):
    # while True:
    #     agora = datetime.now()
    #     if agora.hour == 20 and agora.minute == 0:
    #         verificar_todos_clientes()
    #         time.sleep(60)
    #     time.sleep(30)