"""Regression corpus: a receiver rebound to another type mid-body.

The binding walk is not flow-sensitive; the verifier must refuse to judge
``client.push()`` (which runs against ``AClient``) instead of applying the
last constructor to every use and reporting an invented ``BClient.push()``.
"""

from lib import AClient, BClient


def run(flag):
    client = AClient()
    client.push()
    if flag:
        client = BClient()
    return client


def run_sequential():
    # The pre-fix verifier applied the *last* constructor to every use, which
    # reported this valid call as an invented ``BClient.push()``.
    client = AClient()
    client.push()
    client = BClient()
    return client
