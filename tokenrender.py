import requests

API_URL = "https://solar-portal-api.onrender.com"
EMAIL = "jonassales.ecopower@gmail.com"
SENHA = "991Bog31**"

response = requests.post(f"{API_URL}/login", json={
    "email": EMAIL,
    "senha": SENHA
})

data = response.json()
token = data.get("token")

print(f"Token: {token}")
