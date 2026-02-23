import os
from datetime import date, datetime, timedelta

import requests
from pymongo import MongoClient

items = {
    "bradesco": {
        "id": "61500a5e-82a7-4eda-bd26-29a891f0903a",
        "types": ["CHECKING_ACCOUNT"],
    },
    "xp": {
        "id": "07f18bf8-6470-48f3-b3ca-a06b86e04095",
        "types": ["CREDIT_CARD", "CHECKING_ACCOUNT"],
    },
    "nubank": {
        "id": "5cbb88b7-aaa3-44b0-acbb-3c871f36adb3",
        "types": ["CHECKING_ACCOUNT", "CREDIT_CARD"],
    },
}


def authenticate():
    client_id = os.getenv("PLUGGY_CLIENT_ID")
    client_secret = os.getenv("PLUGGY_CLIENT_SECRET")

    url = "https://api.pluggy.ai/auth"

    payload = {
        "clientId": client_id,
        "clientSecret": client_secret,
    }

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
    }

    response = requests.post(url, json=payload, headers=headers)

    api_key = response.json()["apiKey"]
    return api_key


def get_accounts(itemId, apiKey):
    url = f"https://api.pluggy.ai/accounts?itemId={itemId}"

    headers = {
        "accept": "application/json",
        "X-API-KEY": apiKey,
    }

    response = requests.get(url, headers=headers)

    return response.json()


def get_transactions(accountId, from_date, apiKey):
    url = f"https://api.pluggy.ai/transactions?accountId={accountId}&from={from_date}"

    headers = {
        "accept": "application/json",
        "X-API-KEY": apiKey,
    }

    response = requests.get(url, headers=headers)

    return response.json()


apiKey = authenticate()
mg_conn_str = os.getenv("MONGO_CONNECTION_STRING")
mg_client = MongoClient(mg_conn_str)
db = mg_client["finance"]

info = db["info"]
trans = db["transactions"]

today = date.today()
last_update = today - timedelta(days=1)
infos = info.find_one()
if infos is not None:
    last_update = infos["last_update"].date()

for bank in items:
    accounts = get_accounts(items[bank]["id"], apiKey)
    filtered_accounts = [
        acc for acc in accounts["results"] if acc["subtype"] in items[bank]["types"]
    ]

    for account in filtered_accounts:
        transactions = get_transactions(
            account["id"],
            last_update.isoformat(),
            apiKey,
        )["results"]

        for tr in transactions:
            tr["bank"] = bank

        if len(transactions) > 0:
            trans.insert_many(transactions)

        print(f"Inserted {len(transactions)} transactions in {bank}")

last_update = datetime.combine(today, datetime.min.time())
info.replace_one({}, {"last_update": last_update}, upsert=True)

mg_client.close()
