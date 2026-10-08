"""Throwaway file for a live Elder smoke test; this PR is closed unmerged."""


def find_user(db, name):
    query = "SELECT * FROM users WHERE name = '" + name + "'"
    return db.execute(query).fetchall()
