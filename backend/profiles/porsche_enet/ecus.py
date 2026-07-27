"""Porsche Taycan (J1 / J1.1) DoIP logical addresses.

PROVENANCE -- read this before trusting a single row. Every address comes from
ONE source: a published first-hand brute-force sweep of 0x4000-0x4FFF on a
MY2022 Taycan 4S, cross-checked against a commercial scan-tool report and the
OBDb Porsche-Taycan signal set. The ASAM/ODX identifiers are real
(EV_BECM1982091, EV_ESP9BOSCHPO513 ...) and the module names match Porsche's
own service-bulletin vocabulary. Full sourcing in
docs/private/taycan-doip-research.md.

It is still ONE car, ONE model year. The J1.2 facelift (2025+) is unverified
throughout.

HONESTY CONTRACT
  This table is a **display-name lookup**, not a truth claim about any given
  car. The scan enumerates what is actually there -- a functional broadcast
  first, then an address sweep -- and asks each responder to identify itself
  via DID 0x2A30 / 0xF19E. An address that does not answer is reported as "not
  fitted", never as a fault. Modules Porsche documents but nobody has located
  (HV heater, HV A/C compressor, charge-flap controller, PDCC, rear-axle
  steering, corner radars, front camera) are deliberately absent: no address
  is invented for them, and the sweep will find them if they exist.
"""

# (name, logical address, VAG system code or None, ASAM/ODX id or None)
TAYCAN_ECUS = [
    # --- Powertrain / high voltage ------------------------------------------
    ("Gateway", 0x4010, 0x19, "EV_Gatew31xPO513"),
    ("Powertrain control (VCU)", 0x4076, 0x01, "EV_VCU00XXX0209J1909101XX"),
    ("HV battery management (BECM)", 0x407B, 0x8C, "EV_BECM1982091"),
    ("Front inverter", 0x407C, 0x51, "EV_PWR1HIAMSPO513"),
    ("Rear inverter", 0x40B8, 0xCE, "EV_PWR2HIAMSPO513"),
    ("On-board charger", 0x4044, 0xC6, "EV_OBC3Phase1KLOMLBev16B"),
    ("DC-DC converter (800V to 12V)", 0x40B7, 0x81, "EV_DCDC400VBasisPREHPO513"),
    ("HV booster (charge boost)", 0x40C7, None, "EV_HVChargBoostPREHPO513"),
    # --- Thermal -------------------------------------------------------------
    ("Thermal management", 0x4042, 0xC5, "EV_ThermContrVISAU49X"),
    ("Climate control", 0x4046, 0x08, "EV_ACClimaBHTCPO513"),
    # --- Chassis / safety ----------------------------------------------------
    ("Brakes / ESP", 0x4013, 0x03, "EV_ESP9BOSCHPO513"),
    ("Brake booster", 0x403B, None, "EV_BrakeBoostBOSCHPO513"),
    ("Power steering", 0x4012, 0x44, "EV_EPSBOPO68X"),
    ("Air suspension / chassis control", 0x4080, 0x74, "EV_ChassContrContiPO513"),
    ("Airbag", 0x4015, 0x15, "EV_AirbaVW31SMEAU65x"),
    ("Airbag (second address)", 0x40F1, 0x15, "EV_AirbaVW31SMEAU65x"),
    ("Tyre pressure monitoring", 0x400B, 0x65, "EV_RDKHUFPO68X"),
    ("Gear selector", 0x4053, None, "EV_GSMWaehlJOPPPO68X"),
    # --- Driver assistance / infotainment ------------------------------------
    ("Instrument cluster", 0x4014, 0x17, "EV_DashBoardLGEPO513"),
    ("Infotainment (PCM)", 0x4073, 0x5F, "EV_MUTI"),
    ("Adaptive cruise control", 0x4057, 0x13, "EV_ACCBOSCHAU65X"),
    ("Front sensors / driver assistance", 0x404F, 0xA5, "EV_ZFASAU516"),
    ("Telematics (Porsche Connect)", 0x4067, 0x75, "EV_ConBoxHighAU49X"),
    ("BOSE amplifier", 0x406F, 0x47, "EV_AMPMst16C4Gen2BOSE"),
    ("Software update controller", 0x4086, None, "EV_OTAFCHarmaPO513"),
    # --- Body / comfort -------------------------------------------------------
    ("Body control module 1", 0x400E, 0x09, "EV_BCM1BOSCHAU651"),
    ("Body control module 2 (comfort)", 0x408B, 0x46, "EV_BCM2HellaAU736"),
    ("Steering column electronics", 0x400C, 0x16, "EV_SMLSKLOAU736"),
    ("Door electronics, driver", 0x404A, 0x42, "EV_DCU2DriveSideMAXHCONT"),
    ("Door electronics, passenger", 0x404B, 0x52, "EV_DCU2PasseSideMAXHCONT"),
    ("Door electronics, rear passenger", 0x403F, 0xBC, "EV_DCU2RearPasseMAXHCONT"),
    ("Tailgate", 0x4023, 0x6D, "EV_DeckLidCONTIAU536"),
    ("Seat adjustment, driver", 0x404C, 0x36, "EV_SCMDriveSideCONTIAU736"),
    ("Seat adjustment, passenger", 0x404D, 0x06, "EV_SCMPasseSideCONTIAU736"),
    ("Wireless phone charging", 0x40A5, 0xDE, "EV_Charg1MobilDevicAU651"),
    # --- Lighting --------------------------------------------------------------
    ("Light control, left", 0x4096, 0xD6, "EV_LLPGen3LKEBODPO68X"),
    ("Light control, right", 0x4097, 0xD7, "EV_LLPGen3RKEBODPO68X"),
    ("Light control, left (second address)", 0x4767, None, "EV_LLPGen3LKEBODPO68X"),
    ("Light control (second address)", 0x4768, None, None),
    # --- Sound / aero ----------------------------------------------------------
    ("Interior sound actuator", 0x401C, 0xA9, "EV_ActuaForIntNoise"),
    ("Exterior sound (E-Sound)", 0x4064, 0xC0, "EV_ESoundMLBEvoS1NN"),
    ("Aerodynamics control", 0x4024, 0x6B, "EV_MASGMarquPO622"),
]

GATEWAY_ADDRESS = 0x4010
TESTER_ADDRESS = 0x0E80
FUNCTIONAL_ADDRESS = 0xE400

# The sweep range. Every known Taycan module lives in 0x40xx; two lighting
# aliases sit at 0x476x, so those are probed explicitly from the table rather
# than by widening the sweep to 4096 addresses.
SWEEP_START = 0x4000
SWEEP_END = 0x40FF

NAMES = {addr: name for name, addr, _vag, _asam in TAYCAN_ECUS}
ASAM_IDS = {addr: asam for _name, addr, _vag, asam in TAYCAN_ECUS}

# Identification DIDs, in the order a commercial Porsche tool reads them.
IDENT_DIDS = [
    (0xF19E, "asamId"),
    (0xF187, "partNumber"),
    (0xF189, "softwareVersion"),
    (0xF191, "hardwareNumber"),
    (0xF1AA, "workshopSystemId"),
    (0xF190, "vin"),
    (0x2A30, "selfIdentification"),
]


def name_for(address: int) -> str:
    """A display name for a logical address, honest when we do not know it."""
    return NAMES.get(address) or f"Unknown module @ 0x{address:04X}"


def decode_self_id(data: bytes):
    """DID 0x2A30 -> the module's own view of its identity.

    Layout: [VAG system code][DoIP address hi][DoIP address lo]. This is what
    makes the scan self-describing: a module we have no table entry for still
    tells us its VAG code, which is how it is named in workshop literature.
    """
    if len(data) < 3:
        return None
    return {
        "vagCode": f"0x{data[0]:02X}",
        "reportedAddress": f"0x{(data[1] << 8) | data[2]:04X}",
    }
