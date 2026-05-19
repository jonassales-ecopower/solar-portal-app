def normalizar_foxess(dados_brutos: dict) -> dict:
    variaveis = {}

    dados = dados_brutos.get("result", [])
    if dados:
        for item in dados[0].get("datas", []):
            variaveis[item["variable"]] = item.get("value", 0)

    return {
        "geracao_kw": variaveis.get("pvPower", 0),
        "injecao_kw": variaveis.get("feedinPower", 0),
        "consumo_rede_kw": variaveis.get("gridConsumptionPower", 0),
        "consumo_casa_kw": variaveis.get("loadsPower", 0),
        "potencia_total_kw": variaveis.get("generationPower", 0),
        "timestamp": datetime.utcnow(),
        "status": "online"
    }