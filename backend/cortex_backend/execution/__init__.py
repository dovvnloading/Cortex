"""Durable execution primitives and provider-independent safety contracts.

Import from the defining submodule, not from this package. Every caller in
the tree already does::

    from cortex_backend.execution.repository import ExecutionRepository
    from cortex_backend.execution.code_execution import validate_code_source

This module used to re-export 181 names through a lazy ``__getattr__``. Doing
that required the same names to be listed in three places by hand -- a
``TYPE_CHECKING`` import block for static analysis, ``__all__``, and a
name-to-submodule map for the loader -- with nothing checking that the three
stayed in agreement. A name added to ``__all__`` but missed in the map raised
``AttributeError`` at first access, at runtime.

It bought nothing: no production module imported through it. The lazy loading
existed so that a worker entrypoint importing one submodule would not pull in
the other twenty and their dependencies, which is exactly what importing the
submodule directly already does.
"""
