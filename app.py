import requests

TOKEN = "8729024731:AAFsaKxKc_8bgxwvno2PqJ-c_ZcEqRovPHs
"
CHAT_ID = "-1004321946575"

def envoyer_message(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": message
    })

if __name__ == "__main__":
    envoyer_message("✅ EasyTaxi Flight Alert est bien lancé.")
