"""Companion Link protocol primitives + an emulated Apple TV server.

A self-contained implementation of the bits needed to impersonate an Apple TV to the iPhone's native
remote: OPACK codec, ChaCha20 AEAD, HAP TLV8, SRP pairing, frame types/enums, and the pairable
``appletv`` server. First-party; no external Apple-protocol dependency.
"""
