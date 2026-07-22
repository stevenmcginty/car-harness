"""Generic OBD-II diagnostics engine (works on all cars, Torque-style).

Speaks ELM327 over serial/COM, WiFi (TCP) and Bluetooth LE transports and
implements the standard OBD-II services: fault codes (read/clear), live data
PIDs and VIN. Everything reports honestly -- simulated data only ever appears
in the clearly-labelled sim mode.
"""
