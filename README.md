# EasyTaxi Flight Alert V2

Bot Telegram pour surveiller les arrivées de l'aéroport Nice Côte d'Azur.

## Variables Railway recommandées

- TELEGRAM_TOKEN
- TELEGRAM_CHAT_ID

Si le token est écrit directement dans `app.py`, le bot fonctionne aussi, mais ce n'est pas recommandé pour la sécurité.

## Lancement

Railway utilise le Procfile :

worker: python app.py
