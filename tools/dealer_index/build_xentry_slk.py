"""Build backend/profiles/mercedes/data/xentry_dids.json from the parsed
XENTRY OpenShell Caesar CBFs for Steve's SLK (R172) engine ECUs.

Inputs (derived, outside git):
  C:\\Users\\steve\\Desktop\\mercedes\\mined\\SIM271DE20.json   (M271 EVO CGI turbo, SLK200/250)
  C:\\Users\\steve\\Desktop\\mercedes\\mined\\MED177.json        (M276 DE35 V6, SLK350)

Both were produced by docs/private/mercedes-data/OpenVehicleDiag/CBFParser
from XENTRY's own Caesar containers:
  xdos\\caesar_cbf\\Xentry\\MB_PKW\\Caesar\\cbf\\SIM271DE20.CBF / MED177.CBF

Nothing here talks to a car: this is static dealer-data mining, graded
candidate until steps 0-2 of captures/mercedes/slk.md run live.
"""
import json, math, os, re, sys

MINED = r"C:\Users\steve\Desktop\mercedes\mined"
OUT = r"C:\Users\steve\Desktop\car-harness\backend\profiles\mercedes\data\xentry_dids.json"

# (did, match-substring-or-None, our-name, family)
SIM271DE20 = [
    ("2000", None, "Engine speed", "baseline"),
    ("2001", None, "Relative load", "baseline"),
    ("2007", None, "Air mass (MAF)", "baseline"),
    ("2009", None, "O2 sensor bank 1 upstream, raw sensor voltage", "baseline"),
    ("2012", None, "O2 sensor bank 1 downstream, raw sensor voltage", "baseline"),
    ("2013", None, "Coolant temperature", "baseline"),
    ("2014", None, "Intake air temperature (pre-throttle)", "baseline"),
    ("2018", None, "Manifold absolute pressure", "baseline"),
    ("201A", None, "System voltage (measured)", "baseline"),
    ("2046", None, "Lambda upstream catalyst, engineering value", "mixture"),
    ("2070", None, "Fuel pressure sensor, raw voltage", "fuel_pressure"),
    ("2071", None, "Low-pressure fuel pressure (engineering)", "fuel_pressure"),
    ("2095", None, "Oil temperature", "baseline"),
    ("2098", None, "Low-pressure fuel pressure at electric fuel pump", "fuel_pressure"),
    ("6011", "MIS_CTR_B[0]", "Misfire counter cylinder 1", "misfire"),
    ("6013", "MIS_CTR_B[1]", "Misfire counter cylinder 2", "misfire"),
    ("6014", "MIS_CTR_B[2]", "Misfire counter cylinder 3", "misfire"),
    ("6012", "MIS_CTR_B[3]", "Misfire counter cylinder 4", "misfire"),
    ("4050", "TI_1_HOM[0]", "Injection time cylinder 1", "injector"),
    ("4051", "TI_1_HOM[3]", "Injection time cylinder 2", "injector"),
    ("4052", "TI_1_HOM[1]", "Injection time cylinder 3", "injector"),
    ("4053", "TI_1_HOM[2]", "Injection time cylinder 4", "injector"),
    ("6041", "IGA_CYL_KNK[0]", "Ignition angle knock correction cylinder 1", "ignition"),
    ("6043", "IGA_CYL_KNK[1]", "Ignition angle knock correction cylinder 2", "ignition"),
    ("6044", "IGA_CYL_KNK[2]", "Ignition angle knock correction cylinder 3", "ignition"),
    ("6042", "IGA_CYL_KNK[3]", "Ignition angle knock correction cylinder 4", "ignition"),
    ("2100", "KNKS[0]", "Knock sensor signal voltage cylinder 1", "ignition"),
    ("2102", "KNKS[1]", "Knock sensor signal voltage cylinder 2", "ignition"),
    ("2103", "KNKS[2]", "Knock sensor signal voltage cylinder 3", "ignition"),
    ("2101", "KNKS[3]", "Knock sensor signal voltage cylinder 4", "ignition"),
    ("6103", None, "Mixture adaptation, lower range (long-term)", "mixture"),
    ("6105", None, "Mixture adaptation, upper range (long-term)", "mixture"),
    ("6132", None, "Mixture control closed-loop correction (short-term)", "mixture"),
    ("4010", None, "Camshaft exhaust-side position (actual)", "camshaft"),
    ("4012", None, "Camshaft intake-side position (actual)", "camshaft"),
    ("4036", None, "Quantity-control-valve pressure adaptation", "fuel_pressure"),
]

MED177 = [
    ("2000", None, "Engine speed", "baseline"),
    ("2001", None, "Relative load", "baseline"),
    ("2009", None, "O2 sensor bank 1 upstream, raw voltage", "baseline"),
    ("2008", None, "O2 sensor bank 2 upstream, raw voltage", "baseline"),
    ("2013", "Kuehlmitteltemperatur gefiltert", "Coolant temperature", "baseline"),
    ("2014", "Ansauglufttemperatur in HFM", "Intake air temperature pre-throttle", "baseline"),
    ("201C", None, "Intake air temperature in manifold", "baseline"),
    ("2018", None, "Manifold absolute pressure", "baseline"),
    ("2040", None, "Ambient pressure", "baseline"),
    ("2046", None, "Lambda upstream catalyst bank 1", "mixture"),
    ("2047", None, "Lambda upstream catalyst bank 2", "mixture"),
    ("206F", None, "Rail pressure actual, filtered (high pressure)", "fuel_pressure"),
    ("2071", None, "Rail pressure after fuel pump (HP pump outlet)", "fuel_pressure"),
    ("2098", None, "Fuel pressure before fuel pump (low pressure)", "fuel_pressure"),
    ("2077", None, "Charge-air pressure P2 before throttle, corrected", "charge_pressure"),
    ("207A", None, "Charge-air pressure P2 before throttle, corrected (alt)", "charge_pressure"),
    ("2095", None, "Oil temperature", "baseline"),
    ("6011", None, "Misfire counter cylinder", "misfire"),
    ("6012", None, "Misfire counter cylinder", "misfire"),
    ("6013", None, "Misfire counter cylinder", "misfire"),
    ("6014", None, "Misfire counter cylinder", "misfire"),
    ("6015", None, "Misfire counter cylinder", "misfire"),
    ("6016", None, "Misfire counter cylinder", "misfire"),
    ("6017", None, "Misfire counter cylinder", "misfire"),
    ("6018", None, "Misfire counter cylinder", "misfire"),
    ("65A0", None, "Misfire counter homogeneous mode cylinder", "misfire"),
    ("65B0", None, "Misfire counter stratified mode cylinder", "misfire"),
    ("6570", "Kleinmengenadaptionswerte", "Injector small-quantity adaptation cylinder", "injector"),
    ("6571", None, "Injector small-quantity adaptation cylinder 2", "injector"),
    ("6572", None, "Injector small-quantity adaptation cylinder 3", "injector"),
    ("6573", None, "Injector small-quantity adaptation cylinder 4", "injector"),
    ("6574", None, "Injector small-quantity adaptation cylinder 5", "injector"),
    ("6575", None, "Injector small-quantity adaptation cylinder 6", "injector"),
    ("6576", None, "Injector small-quantity adaptation cylinder 7", "injector"),
    ("6577", None, "Injector small-quantity adaptation cylinder 8", "injector"),
    ("6550", None, "Piezo injector stroke-correction compensation factor cylinder 1", "injector"),
    ("6560", None, "Piezo injector charge-control actuating variable cylinder 1", "injector"),
    ("6105", "Multiplikativer Gemischadaptionsfaktor Bank 1", "Multiplicative mixture adaptation factor bank 1 (part load)", "mixture"),
    ("6106", "Multiplikativer Gemischadaptionsfaktor Bank 2", "Multiplicative mixture adaptation factor bank 2 (part load)", "mixture"),
    ("6109", None, "Temperature-dependent mixture adaptation factor bank 1", "mixture"),
    ("6110", None, "Temperature-dependent mixture adaptation factor bank 2", "mixture"),
    ("6111", None, "Mixture adaptation short-test result bank 1", "mixture"),
    ("6112", None, "Mixture adaptation short-test result bank 2", "mixture"),
    ("4010", None, "Camshaft exhaust bank 1 position", "camshaft"),
    ("4011", None, "Camshaft exhaust bank 2 position", "camshaft"),
    ("4012", None, "Camshaft intake bank 1 position", "camshaft"),
    ("4013", None, "Camshaft intake bank 2 position", "camshaft"),
    ("4064", None, "Camshaft reference adaptation angle difference intake right", "camshaft"),
    ("606D", None, "Rough-run (Laufunruhe) fuel-on adaptation test value", "misfire"),
]


def _fix(s):
    """cbf_parser emits some strings double-encoded (UTF-8 read back as
    Latin-1); undo exactly one layer where it round-trips."""
    if not isinstance(s, str):
        return s
    try:
        fixed = s.encode("latin-1").decode("utf-8")
        return fixed if len(fixed) <= len(s) else s
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def conv_of(param):
    df = param.get("data_format")
    if isinstance(df, dict):
        kind, val = next(iter(df.items()))
        if kind == "Linear":
            return {"type": "linear",
                    "multiplier": val.get("multiplier"), "offset": val.get("offset")}
        if kind == "Bool":
            return {"type": "enum", "states": [_fix(val.get("neg_name")), _fix(val.get("pos_name"))]}
        if kind == "Table":
            states = [{"value": s.get("start"), "name": _fix(s.get("name"))} for s in val]
            return {"type": "enum", "states": states}
        if kind == "Identical":
            return {"type": "raw"}
        if kind == "String":
            return {"type": "string"}
        return {"type": kind.lower()}
    return {"type": "unknown"}


def find_service(bigjson, did, needle):
    """First variant whose downloads contain a `22 <did>` payload with an
    output param matching the needle (or the first param if needle is None)."""
    for v in bigjson["variants"]:
        for dl in v.get("downloads", []):
            p = dl.get("payload") or ""
            if p.lower() != f"22{did.lower()}":
                continue
            for op in dl.get("output_params", []):
                if needle is None or needle.lower() in op["name"].lower():
                    return v["name"], dl, op
    return None, None, None


def build(ecu_file, ecu_tag, title, selection):
    big = json.load(open(os.path.join(MINED, ecu_file), encoding="utf-8"))
    conns = []
    for c in big.get("connections", []):
        ct = c.get("connection_type", {})
        conns.append({
            "baud": c.get("baud"), "request_id": hex(c.get("send_id", 0)),
            "response_id": hex(c.get("recv_id", 0)),
            "protocol": c.get("server_type"),
        })
    out, missing = [], []
    for did, needle, name, family in selection:
        variant, dl, op = find_service(big, did, needle)
        if op is None:
            missing.append(f"{ecu_tag} {did} {needle} {name}")
            continue
        start_bit = int(op["start_bit"]); bits = int(op["length_bits"])
        rec = {
            "did": did,
            "request": f"22 {did[:2]} {did[2:]}".upper(),
            "name": name,
            "source_name": _fix(op["name"]),
            "family": family,
            "byte_offset": start_bit // 8,
            "bit_offset": start_bit % 8,
            "length_bits": bits,
            "byte_size": math.ceil((start_bit % 8 + bits) / 8),
            "unit": _fix(op.get("unit", "")),
            "conversion": conv_of(op),
            "grade": "candidate",
            "variant_seen": variant,
        }
        out.append(rec)
    return {"ecu": ecu_tag, "title": title, "connections": conns, "dids": out}, missing


def main():
    de, miss_de = build("SIM271DE20.json", "SIM271DE20",
                        "SIM271 DE20 - M271 EVO CGI turbo 4-cyl (SLK200/SLK250)", SIM271DE20)
    me, miss_me = build("MED177.json", "MED177",
                        "MED177 - M276 DE35 V6 (SLK350); CBF also carries M278 variants", MED177)
    doc = {
        "title": "Mercedes SLK R172 petrol - XENTRY engine-ECU actual values (rough-running capture)",
        "provenance": {
            "package": "XENTRY_Diagnostics_OpenShell_26.6.4.zip (InstallShield wrapper, downloaded 2026-08-22, CRC-verified)",
            "how_opened": ("zip central directory -> per-GUID MSI payloads extracted to "
                           "C:/Users/steve/Desktop/mercedes/extracted (msiexec administrative install) -> "
                           "Caesar packages unpacked -> CBFs parsed statically"),
            "parser": "docs/private/mercedes-data/OpenVehicleDiag/CBFParser (built with rustup 1.98, release)",
            "source_files": {
                "SIM271DE20": "xdos/caesar_cbf/Xentry/MB_PKW/Caesar/cbf/SIM271DE20.CBF -> mined/SIM271DE20.json",
                "MED177": "xdos/caesar_cbf/Xentry/MB_PKW/Caesar/cbf/MED177.CBF -> mined/MED177.json",
            },
            "cross_reference": ("XENTRY gmf trees reference these ECUs under "
                                "MB_PKW/Steuergeraete/Antrieb/Benzin/{SIM271DE,MED177}_ODX; "
                                "MED177 simulation xml confirms UDS on HSCAN 500k, 0x7E0/0x7E8"),
        },
        "notes": [
            "Grading: every entry is 'candidate'. Nothing here has answered on a car yet; "
            "steps 0-2 of captures/mercedes/slk.md promote entries to verified by adding "
            "a live_<vin> key exactly like backend/profiles/jlr/data/sdd_dids.json.",
            "Byte/bit offsets are relative to the positive-response payload AFTER the "
            "62 <DID> header bytes.",
            "SIM271DE20 exposes no explicit per-cylinder injector quantity deviation; "
            "per-cylinder injection time (TI_1_HOM) is the closest channel. MED177 does "
            "expose true per-cylinder injector adaptations (6570..).",
            "MED177 CBF covers several variants; which DIDs answer depends on the car's "
            "actual variant (step 0 of the plan identifies it).",
            "The GMF2 guided-diagnosis trees are encrypted and NOT mined; everything here "
            "comes from the Caesar layer.",
            "Raw-corpora locations and the not-mined list live in docs/dataset-registry.md.",
        ],
        "sim271de20_m271_turbo": de,
        "med177_m276_v6": me,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(doc, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    n = len(de["dids"]) + len(me["dids"])
    print(f"wrote {OUT}: {len(de['dids'])} SIM271DE20 + {len(me['dids'])} MED177 DIDs")
    if miss_de or miss_me:
        print("MISSING:")
        for m in miss_de + miss_me:
            print("  ", m)


if __name__ == "__main__":
    main()
