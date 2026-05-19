# backup_historico.py
import requests
import json

API_URL = "http://127.0.0.1:8000"

# Buscar dados mensais
resp = requests.get(f"{API_URL}/clientes/2/monitoramento/mensal")
dados = resp.json()

print("Dados históricos disponíveis na API:")
for mes in dados["mensal"]:
    print(f"  {mes['mes']}/{mes['ano']}: {mes['geracao_kwh']} kWh")

print(f"\nTotal: {dados['total_periodo']} kWh")