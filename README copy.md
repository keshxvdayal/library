# ShelfShare
Peer-to-peer physical book lending. Flask + SQLite.

    pip install -r requirements.txt
    cp .env.example .env   # then export the vars (or use your host's env settings)
    flask --app app run    # dev;  gunicorn app:app  # production

Flow: add book (ISBN scan) → print its QR → others request ($1 held) → owner accepts → borrower scans QR at handoff
(owner earns $0.70, platform keeps $0.30) → owner marks returned. Declines/cancels refund the $1.
Set STRIPE_SECRET_KEY for real top-ups; DEV_FREE_CREDITS=1 only for local testing.
Deploy behind HTTPS (camera scanning requires it) and set SECRET_KEY.
