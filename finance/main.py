import logging
import os
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from pymongo import MongoClient
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.naive_bayes import MultinomialNB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

items = {
    "bradesco": {
        "id": "61500a5e-82a7-4eda-bd26-29a891f0903a",
        "types": ["CHECKING_ACCOUNT"],
        "accounts": {
            "CHECKING_ACCOUNT": "Assets:Brad:Corrente",
        },
    },
    "xp": {
        "id": "07f18bf8-6470-48f3-b3ca-a06b86e04095",
        "types": ["CREDIT_CARD", "CHECKING_ACCOUNT"],
        "accounts": {
            "CHECKING_ACCOUNT": "Assets:XP:Corrente",
            "CREDIT_CARD": "Liabilities:CreditCard:XP",
        },
    },
    "nubank": {
        "id": "5cbb88b7-aaa3-44b0-acbb-3c871f36adb3",
        "types": ["CHECKING_ACCOUNT", "CREDIT_CARD"],
        "accounts": {
            "CHECKING_ACCOUNT": "Assets:Nu:Corrente",
            "CREDIT_CARD": "Liabilities:CreditCard:Nu",
        },
    },
}

SOURCE_ACCOUNTS = {
    "Assets:Brad:Corrente",
    "Assets:Brad:Corrente:Reserva",
    "Assets:XP:Corrente",
    "Assets:Nu:Corrente",
    "Liabilities:CreditCard:XP",
    "Liabilities:CreditCard:Nu",
    "Equity:Opening-Balances",
    "Equity:Transfers:Brad:Investment",
    "Equity:Transfers:XP:Investment",
    "Equity:Trabalho:Adiantamento",
}

DEFAULT_CATEGORY = "Expenses:FIXME"
CURRENCY = "BRL"
TRANSACTIONS_FILE = "src/transactions.beancount"


# ---------------------------------------------------------------------------
# Pluggy API
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Step 1: Fetch transactions from Pluggy and store in MongoDB
# ---------------------------------------------------------------------------


def fetch_transactions(apiKey, db):
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
                tr["account_subtype"] = account["subtype"]

            if len(transactions) > 0:
                trans.insert_many(transactions)

            logger.info("Inserted %d transactions in %s", len(transactions), bank)

    last_update = datetime.combine(today, datetime.min.time())
    info.replace_one({}, {"last_update": last_update}, upsert=True)


# ---------------------------------------------------------------------------
# Step 2: Git operations
# ---------------------------------------------------------------------------


def _git(args, repo_dir, check=True):
    result = subprocess.run(
        ["git"] + args,
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def sync_repo(repo_dir, repo_url, branch):
    repo_path = Path(repo_dir)
    git_dir = repo_path / ".git"

    if git_dir.exists():
        logger.info("Repository exists, fetching and resetting to origin/%s", branch)
        _git(["fetch", "origin"], repo_dir)
        _git(["reset", "--hard", f"origin/{branch}"], repo_dir)
    else:
        logger.info("Cloning repository from %s", repo_url)
        repo_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--branch", branch, repo_url, str(repo_path)],
            capture_output=True,
            text=True,
            check=True,
        )

    git_user = os.getenv("GIT_USER_NAME", "Finance Auto Bot")
    git_email = os.getenv("GIT_USER_EMAIL", "bot@masahome")
    _git(["config", "user.name", git_user], repo_dir)
    _git(["config", "user.email", git_email], repo_dir)


def commit_and_push(repo_dir, message, branch):
    _git(["add", "-A"], repo_dir)

    status = _git(["status", "--porcelain"], repo_dir, check=False)
    if not status.stdout.strip():
        logger.info("No changes to commit")
        return False

    _git(["commit", "-m", message], repo_dir)
    logger.info("Committed: %s", message)

    push_result = _git(["push", "origin", branch], repo_dir, check=False)
    if push_result.returncode != 0:
        logger.error("Push failed: %s", push_result.stderr.strip())
        return False

    logger.info("Pushed to origin/%s", branch)
    return True


# ---------------------------------------------------------------------------
# Step 3-4: Classification with scikit-learn
# ---------------------------------------------------------------------------


def parse_beancount_for_training(text):
    entries = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        match = re.match(r'^\d{4}-\d{2}-\d{2}\s+\*\s+"([^"]*)"', line)
        if match:
            payee = match.group(1)
            category = None
            i += 1
            while i < len(lines):
                if lines[i].startswith("  ") or lines[i].strip() == "":
                    posting_line = lines[i].strip()
                    if (
                        posting_line
                        and not posting_line.startswith(";")
                        and not posting_line.startswith("date:")
                        and not posting_line.startswith("source_desc:")
                    ):
                        account_match = re.match(r"^([A-Z][A-Za-z0-9:]+)", posting_line)
                        if account_match:
                            account = account_match.group(1)
                            if account not in SOURCE_ACCOUNTS and category is None:
                                category = account
                    i += 1
                else:
                    break
            if payee and category:
                entries.append((payee, category))
        else:
            i += 1
    return entries


def train_classifier(training_pairs):
    if len(training_pairs) < 2:
        logger.warning(
            "Insufficient training data (%d pairs), using default category",
            len(training_pairs),
        )
        return None, None

    descriptions, categories = zip(*training_pairs)
    vectorizer = TfidfVectorizer()
    classifier = MultinomialNB()

    try:
        X = vectorizer.fit_transform(descriptions)
        classifier.fit(X, categories)
        logger.info("Classifier trained on %d samples", len(training_pairs))
    except Exception as e:
        logger.error("Failed to train classifier: %s", e)
        return None, None

    return classifier, vectorizer


def predict_category(classifier, vectorizer, description):
    if classifier is None or vectorizer is None:
        return DEFAULT_CATEGORY
    try:
        X = vectorizer.transform([description])
        return classifier.predict(X)[0]
    except Exception as e:
        logger.error("Prediction failed for '%s': %s", description, e)
        return DEFAULT_CATEGORY


# ---------------------------------------------------------------------------
# Step 5: Format and write beancount entries
# ---------------------------------------------------------------------------


def get_source_account(tr):
    bank = tr.get("bank", "")
    subtype = tr.get("account_subtype", "")
    return items.get(bank, {}).get("accounts", {}).get(subtype, DEFAULT_CATEGORY)


def normalize_amount(tr):
    amount = float(tr.get("amount", 0))
    if tr.get("account_subtype") == "CREDIT_CARD":
        amount = -amount
    return amount


def format_beancount_entry(tr, category, source_account):
    tx_date = tr.get("date", "")
    if "T" in tx_date:
        tx_date = tx_date.split("T")[0]

    description = tr.get("description", "") or ""
    description = description.replace('"', "'")

    amount = normalize_amount(tr)
    abs_amount = abs(amount)
    currency = tr.get("currencyCode", CURRENCY)

    if amount < 0:
        source_signed = -abs_amount
        category_signed = abs_amount
    else:
        source_signed = abs_amount
        category_signed = -abs_amount

    lines = [
        f'{tx_date} * "{description}"',
        f"  {source_account:<40s} {source_signed:.2f} {currency}",
        f"  {category:<40s} {category_signed:.2f} {currency}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 9: Webhook notification
# ---------------------------------------------------------------------------


def notify_webhook(url, transactions):
    if not url:
        return

    payload = {
        "date": date.today().isoformat(),
        "count": len(transactions),
        "transactions": transactions,
    }

    try:
        response = requests.post(url, json=payload, timeout=30)
        logger.info("Webhook notified (%s): %s", response.status_code, url)
    except Exception as e:
        logger.error("Webhook notification failed: %s", e)


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------


def run_job():
    logger.info("Starting finance sync job")

    mg_conn_str = os.getenv("MONGO_CONNECTION_STRING")
    mg_client = MongoClient(mg_conn_str)
    db = mg_client["finance"]

    repo_dir = os.getenv("REPO_DIR", "/repo")
    git_repo_url = os.getenv("GIT_REPO_URL", "")
    git_branch = os.getenv("GIT_BRANCH", "main")

    try:
        # Step 1: Fetch transactions from Pluggy → MongoDB
        apiKey = authenticate()
        fetch_transactions(apiKey, db)
        logger.info("Pluggy sync completed")

        # Step 2: Pull beancount repository
        sync_repo(repo_dir, git_repo_url, git_branch)
        logger.info("Repository synced")

        # Step 3: Get unclassified transactions from MongoDB
        trans = db["transactions"]
        pending = list(trans.find({}))

        if not pending:
            logger.info("No pending transactions to classify")
            return

        logger.info("Found %d pending transactions", len(pending))

        # Step 4: Train classifier on existing transactions
        transactions_path = Path(repo_dir) / TRANSACTIONS_FILE
        training_text = ""
        if transactions_path.exists():
            training_text = transactions_path.read_text(encoding="utf-8")

        training_pairs = parse_beancount_for_training(training_text)
        classifier, vectorizer = train_classifier(training_pairs)

        # Step 5: Classify and format each transaction
        new_entries = []
        classified = []

        for tr in pending:
            source_account = get_source_account(tr)
            description = tr.get("description", "") or ""
            category = predict_category(classifier, vectorizer, description)
            entry = format_beancount_entry(tr, category, source_account)
            new_entries.append(entry)

            amount = normalize_amount(tr)
            classified.append(
                {
                    "date": tr.get("date", ""),
                    "description": description,
                    "amount": amount,
                    "currency": tr.get("currencyCode", CURRENCY),
                    "category": category,
                    "source_account": source_account,
                }
            )

        # Step 6: Append classified transactions to beancount file
        with open(transactions_path, "a", encoding="utf-8") as f:
            for entry in new_entries:
                f.write("\n\n")
                f.write(entry)
            f.write("\n")

        logger.info("Added %d transactions to %s", len(new_entries), TRANSACTIONS_FILE)

        # Step 7: Delete processed transactions from MongoDB
        trans.delete_many({"_id": {"$in": [tr["_id"] for tr in pending]}})
        logger.info("Deleted %d transactions from MongoDB", len(pending))

        # Step 8: Git commit and push
        commit_msg = f"auto: updates {date.today().strftime('%d-%m-%Y')}"
        commit_and_push(repo_dir, commit_msg, git_branch)

        # Step 9: Webhook notification
        webhook_url = os.getenv("WEBHOOK_URL", "")
        notify_webhook(webhook_url, classified)

        logger.info("Finance sync job finished successfully")
    finally:
        mg_client.close()


if __name__ == "__main__":
    scheduler = BlockingScheduler(timezone=os.getenv("TZ", "America/Sao_Paulo"))
    scheduler.add_job(run_job, "cron", hour=2, minute=0, id="finance_daily_sync")
    logger.info("Scheduler started: running daily at 02:00 (%s)", scheduler.timezone)

    if os.getenv("RUN_ON_STARTUP", "false").lower() == "true":
        run_job()

    scheduler.start()
