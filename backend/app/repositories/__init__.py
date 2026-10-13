"""Repositories: the only code that talks to MongoDB collections directly.

Every read/write that touches user data takes an ``owner_id`` and includes it in
the query filter. That is how ownership is enforced consistently today (with the
demo identity) and how real authentication will plug in later without rewriting
query logic.
"""
