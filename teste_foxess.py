import requests
import hashlib
import time

API_KEY = "b7b6946e-786d-40d5-a366-8ea95d57d0ea"
DEVICE_SN = "J0MF502043LK010"

path_assinatura = "/op/v1/device/real/query"
url = "https://www.foxesscloud.com/op/v1/device/real/query"

timestamp = str(int(time.time() * 1000))
to_sign = path_assinatura + chr(13) + chr(10) + API_KEY + chr(13) + chr(10) + timestamp
print(f"String assinada: {repr(to_sign)}")
signature = hashlib.md5(to_sign.encode("utf-8")).hexdigest()
print(f"Assinatura: {signature}")

headers = {
    "Token": API_KEY,
    "Lang": "en",
    "Timestamp": timestamp,
    "Signature": signature,
    "Timezone": "America/Sao_Paulo",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Content-Type": "application/json"
}

body = {"sns": [DEVICE_SN]}

resp = requests.post(url, json=body, headers=headers, timeout=15)
print(f"Status: {resp.status_code}")
print(f"Resposta: {resp.json()}")