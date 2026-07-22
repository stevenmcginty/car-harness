"""Pluggable connection adapters (HOW car-harness physically talks to a car).

An adapter *type* is a built-in kind (ENET/DoIP, ELM327, J2534, ...); an
adapter *instance* is a user-configured, persisted copy created via the wizard.
See ``backend/adapters/registry.py`` for discovery and persistence.
"""
