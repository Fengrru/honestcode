"""Framework integrations for HonestCode.

Each module here binds one framework to something in the core. They are kept
out of ``honestcode.guards`` because that package is deliberately stdlib-only
and framework-agnostic — a rule ``tests/test_guards.py`` enforces — and out of
``honestcode.verify`` because that is static analysis, whereas these wrap live
execution.

Import the integration you need directly::

    from honestcode.integrations.pydantic_ai import EffectGuardCapability

Each module raises a helpful ``ImportError`` if its framework is not installed.
"""
