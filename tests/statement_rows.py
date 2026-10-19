"""Two SB ledgers for 1-17 Oct 2026, as Gemini returns them. Made-up accounts and payees, laid out like
real ledger photos: daily ₹2,000 deposits with mostly empty narrations, other deposits and withdrawals
mixed in, 2 Oct a holiday, account A missing 8 Oct and paying it on 9 Oct ("8-10"), account B getting
₹1,500 instead of ₹2,000 on 1 Oct."""

from __future__ import annotations

ACCOUNT_A = [  # SB A/C 1001, 1-16 Oct: (day, deposit, withdraw, narration, slip)
    (1, 2000, 0, "HO", "18819"), (1, 512, 0, "SB INT30/Sep/2026", "19222"), (3, 2000, 0, "ho rent", "19172"),
    (5, 6000, 0, "", "19239"), (5, 0, 2100, "PAYEE ONE", "16511"), (5, 0, 3300, "PAYEE TWO", "16512"),
    (5, 2000, 0, "HO", "19271"), (5, 30000, 0, "NCD INCENTIVE", "19273"), (6, 6000, 0, "ncd incentive balance", "19387"),
    (6, 2000, 0, "ho", "19393"), (7, 2000, 0, "", "19475"), (9, 2000, 0, "8-10", "19657"), (9, 2000, 0, "", "19658"),
    (9, 0, 30000, "gl", "16897"), (10, 6000, 0, "TA", "19727"), (10, 2000, 0, "H O RENT", "19728"),
    (10, 0, 2100, "PAYEE ONE", "16958"), (12, 2000, 0, "HO RENT", "19792"), (12, 0, 4000, "PAYEE THREE", "17040"),
    (13, 2000, 0, "HO", "19855"), (14, 2000, 0, "", "19939"), (15, 2000, 0, "", "20112"), (16, 2000, 0, "", "20216"),
]
ACCOUNT_B = [  # SB A/C 1002, 1-17 Oct
    (1, 1500, 0, "rent", "18763"), (1, 640, 0, "SB INT30/Sep/2026", "19222"), (3, 2000, 0, "", "19106"),
    (5, 2000, 0, "", "19215"), (6, 2000, 0, "", "19370"), (7, 2000, 0, "", "19429"), (8, 2000, 0, "", "19507"),
    (9, 2000, 0, "", "19638"), (9, 0, 12000, "gl", "16896"), (10, 2000, 0, "", "19714"), (12, 2000, 0, "", "19761"),
    (13, 2000, 0, "", "19825"), (14, 2000, 0, "", "19925"), (15, 2000, 0, "", "20048"), (16, 2000, 0, "", "20199"),
    (17, 2000, 0, "", "20304"),
]
ACCOUNTS = [{"number": "1001", "name": "Asha"}, {"number": "1,002.00", "name": "Ravi"}]


def rows(data, account, image):
    return [{"date": f"2026-10-{d:02d}", "description": narr, "amount": dep or wd,
             "direction": "credit" if dep else "debit", "reference": slip, "account": account, "image": image}
            for d, dep, wd, narr, slip in data]
