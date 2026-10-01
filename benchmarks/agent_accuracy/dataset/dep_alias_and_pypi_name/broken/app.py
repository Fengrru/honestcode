import yaml
from fakelib import inner


def load(text):
    return yaml.safe_load(text)


def fetch(seed):
    return inner.tool(seed)


def broken(seed):
    return inner.toll(seed)
