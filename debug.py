import hashlib
import time

API_KEY = "b7b6946e-786d-40d5-a366-8ea95d57d0ea"
path = "/op/v1/device/real/query"
timestamp = "1778066402081"  # timestamp fixo para testar

to_sign = f"{path}\r\n{API_KEY}\r\n{timestamp}"
print(f"Tamanho da chave: {len(API_KEY)}")
print(f"Chave tem espaços: {' ' in API_KEY}")
print(f"Repr da string: {repr(to_sign[:50])}")
sig = hashlib.md5(to_sign.encode("utf-8")).hexdigest()
print(f"Assinatura: {sig}")