"""Throwaway file for a live Elder smoke test; this PR is closed unmerged (re-check 2)."""


def find_user(db, name):
    return db.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchall()
