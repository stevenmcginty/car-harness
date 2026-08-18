package com.carharness.app.obd

/**
 * Jaguar Land Rover's own SDD 169 dealer data, on the phone.
 *
 * The desktop loads backend/profiles/jlr/data/sdd_dids.json at runtime; the
 * phone carries the same tables compiled in, because it has no backend to ask.
 * The tables below were GENERATED from that JSON, so the two cannot drift by
 * hand — regenerate rather than edit a row. Mirror of
 * backend/profiles/jlr/dealer_data.py.
 *
 * PROVENANCE. SDD 169 ships its module definitions as `.exml`, encrypted
 * per-file with 3DES-ECB; the 24-byte key lives nibble-swapped in
 * Environment.dll's CCryptFile constructor. Decrypting Xml/L494_201600/ gave
 * three things:
 *
 *   * [MODULES] — the 57-module L494 MY16 CAN address map. This is what lets
 *     the address sweep print ABS at 0x7E6 and DSM at 0x744 instead of a bare
 *     id, and it is what settled the question the AdBlue job started with:
 *     0x7E6 is the ABS module, there is NO separate SCR/reagent controller on
 *     L494, and SCR is managed by the PCM at 0x7E0.
 *   * [DIDS] — 37 diesel-PCM (306DT / AJD-V6D) data identifiers with SDD's own
 *     names, byte sizes, scaling and units.
 *   * [ROUTINES] — 24 PCM/SCR routine identifiers. REFERENCE ONLY. They are
 *     the first published SCR/DPF/EGR actuation ids this harness has for the
 *     family, and the harness sends none of them: there is no code path here
 *     that can, and there is not meant to be.
 *
 * CONFIDENCE GRADES, matching the desktop and the Vauxhall Astra-K TCM map:
 *
 *   verified   SDD scaling that reproduced a sane number from the live L494
 *              SALWA2EK8HA payload. Safe to show as a plain number and unit.
 *   candidate  SDD's NAME is authoritative — it is the manufacturer's own —
 *              but the value is not proven on this car, either because SDD
 *              publishes no scaling for the row or because nobody has checked
 *              its arithmetic against a real reading. Shown WITH the raw
 *              payload and marked, never as a bare confident number. The urea
 *              cluster 05FA-05FD sits here.
 *
 * SDD names a curated 37 of the ~191 DIDs the PCM physically answers, so
 * absence from [DIDS] does NOT mean a DID is dead.
 */

/** One SDD-named diesel-PCM identifier. [resolution] null = SDD gives no scaling. */
data class SddDid(
    val did: Int,
    val name: String,
    val byteSize: Int?,
    val dataType: String,
    val resolution: Double?,
    val offset: Double,
    val units: String,
    /** What the validation car did with it, verbatim from the extract. */
    val live: String,
    val confidence: String,
) {
    val hex: String get() = "%04X".format(did)

    /** True when the L494 SALWA2EK8HA sweep got a positive answer at this id. */
    val answersOnL494: Boolean get() = live.startsWith("answered")

    /** Display name — a candidate carries its grade so the value cannot be
     *  read as settled. */
    fun label(): String = if (confidence == "verified") name else "$name [candidate]"

    /** True when SDD published enough to turn the payload into a number. */
    val scalable: Boolean
        get() = resolution != null && byteSize != null && byteSize in 1..4 &&
            (dataType == "unsigned" || dataType == "signed")

    /** SDD's scaled value, or null when SDD publishes no scaling for the row. */
    fun decode(body: ByteArray): String? {
        if (!scalable) return null
        val size = byteSize ?: return null
        if (body.size < size) return null
        var raw = 0L
        for (i in 0 until size) raw = (raw shl 8) or (body[i].toLong() and 0xFF)
        if (dataType == "signed") {
            val top = 1L shl (size * 8 - 1)
            if (raw >= top) raw -= top shl 1
        }
        val v = raw * (resolution ?: 1.0) + offset
        return if (v == Math.floor(v) && !v.isInfinite()) {
            v.toLong().toString()
        } else {
            "%.3f".format(v).trimEnd('0').trimEnd('.')
        }
    }

    /** The provenance sentence that rides with every reading built from this. */
    fun describe(): String = buildString {
        append("SDD 169 diesel-PCM DID $hex ($PLATFORM)")
        byteSize?.let { append(". $it byte(s)") }
        if (scalable) {
            append(". SDD scaling ×$resolution offset $offset")
            if (units.isNotEmpty()) append(" $units")
        } else {
            append(". SDD publishes no scaling for this row — payload shown raw")
        }
        if (live.isNotEmpty()) append(". Live L494 SALWA2EK8HA: $live")
        append(". Confidence: $confidence.")
    }

    private companion object {
        const val PLATFORM = "L494 MY16"
    }
}

object JlrSdd {

    /** The extract is the L494's. A name is a strong hint on another JLR, not
     *  a fact, and every label says so. */
    const val PLATFORM = "L494 MY16"

    /** The urea/SCR dosing cluster: SDD-named, PCM-managed, live on the L494,
     *  and completely unscaled. The next real-world calibration target. */
    val UREA_DIDS: List<Int> = listOf(0x05FA, 0x05FB, 0x05FC, 0x05FD)

    const val UREA_WATCH_NOTE =
        "The urea/SCR cluster 05FA-05FD is SDD-named and PCM-managed (there is no " +
            "separate SCR controller on L494). SDD publishes no scaling for any of the " +
            "four, so every payload stays raw: name authoritative, number NOT. 05FC " +
            "'total mass fluid injected by urea dosing system' is a monotonic total — " +
            "log it warm over a short drive and the rate it climbs is what pins the " +
            "scaling."

    const val ROUTINES_NOTE =
        "Reference only. These are the real routine identifiers SDD sends for this " +
            "engine family — the first published SCR/DPF/EGR actuation ids the harness " +
            "has for a 306DT — and the harness sends NONE of them. They are listed so " +
            "you can see what the dealer tool would do; running one needs SDD or an " +
            "equivalent, not this app."

    // The aftertreatment-relevant routines, starred in the listing because they
    // are the ones the AdBlue/DPF job cares about and the ones no public source
    // carried before.
    private val AFTERTREATMENT_ROUTINES =
        setOf(0x4026, 0x402C, 0x4050, 0x4051, 0x4076, 0x4094, 0x4095, 0x4096)

    // ---- generated tables (see the file header) ---------------------------

    // 57 modules, decrypted from Xml/L494_201600/<MODULE>.exml.
    // (request, response, code) -- response is kept explicitly because one
    // row (DRDM 0x74A -> 0x742) breaks JLR's usual request+8 convention.
    val MODULES: List<Triple<Int, Int, String>> = listOf(
        Triple(0x068, 0x070, "NCM"),
        Triple(0x080, 0x088, "ACM"),
        Triple(0x081, 0x089, "SRM"),
        Triple(0x085, 0x08D, "DABM"),
        Triple(0x086, 0x08E, "AAM"),
        Triple(0x087, 0x08F, "TVM"),
        Triple(0x08B, 0x093, "REM"),
        Triple(0x701, 0x709, "GPSM"),
        Triple(0x706, 0x70E, "IPMA"),
        Triple(0x710, 0x718, "CHCM"),
        Triple(0x716, 0x71E, "GWM"),
        Triple(0x720, 0x728, "IPC"),
        Triple(0x722, 0x72A, "VIM"),
        Triple(0x726, 0x72E, "BCM"),
        Triple(0x730, 0x738, "PSCM"),
        Triple(0x731, 0x739, "RFA"),
        Triple(0x732, 0x73A, "GSM"),
        Triple(0x733, 0x73B, "HVAC"),
        Triple(0x735, 0x73D, "NAV"),
        Triple(0x736, 0x73E, "PAM"),
        Triple(0x737, 0x73F, "RCM"),
        Triple(0x740, 0x748, "DDM"),
        Triple(0x741, 0x749, "PDM"),
        Triple(0x743, 0x74B, "PRDM"),
        Triple(0x744, 0x74C, "DSM"),
        Triple(0x74A, 0x742, "DRDM"),
        Triple(0x751, 0x759, "TPM"),
        Triple(0x754, 0x75C, "TCU"),
        Triple(0x756, 0x75E, "PBM"),
        Triple(0x761, 0x769, "TCCM"),
        Triple(0x764, 0x76C, "CCM"),
        Triple(0x765, 0x76D, "OCS"),
        Triple(0x766, 0x76E, "RBM"),
        Triple(0x773, 0x77B, "CHCMB"),
        Triple(0x775, 0x77D, "RGTM"),
        Triple(0x781, 0x789, "CAV"),
        Triple(0x785, 0x78D, "RHVAC"),
        Triple(0x792, 0x79A, "ATCM"),
        Triple(0x795, 0x79D, "RDCM"),
        Triple(0x797, 0x79F, "SASM"),
        Triple(0x7A0, 0x7A8, "FCIMB"),
        Triple(0x7A1, 0x7A9, "FCIMC"),
        Triple(0x7A3, 0x7AB, "PSM"),
        Triple(0x7A5, 0x7AD, "FCDIM"),
        Triple(0x7A7, 0x7AF, "FCIM"),
        Triple(0x7B1, 0x7B9, "IPMB"),
        Triple(0x7B2, 0x7BA, "HUD"),
        Triple(0x7C4, 0x7CC, "SODL"),
        Triple(0x7C6, 0x7CE, "SODR"),
        Triple(0x7C7, 0x7CF, "AUCM"),
        Triple(0x7D1, 0x7D9, "SPMA"),
        Triple(0x7D4, 0x7DC, "SPMB"),
        Triple(0x7E0, 0x7E8, "PCM"),
        Triple(0x7E1, 0x7E9, "TCM"),
        Triple(0x7E4, 0x7EC, "BECM"),
        Triple(0x7E6, 0x7EE, "ABS"),
        Triple(0x7E7, 0x7EF, "EPIC"),
    )

    // 37 diesel-PCM identifiers from MDX_PCM_D / MDX_PCM_D_3LT / MDX_SCR.
    val DIDS: List<SddDid> = listOf(
        SddDid(0x0396, "Distance To Next Service", 2, "unsigned", 1.0, 0.0, "km", "answered 5AB6", "verified"),
        SddDid(0x03FE, "Injector Correction Codes for Cylinders 1 - 6", 48, "bytes", null, 0.0, "", "did not answer (dead on this calibration)", "candidate"),
        SddDid(0x0416, "Engine input/output parameters", 4, "enumerated", null, 0.0, "", "answered 16003000", "candidate"),
        SddDid(0x042C, "Diesel Particulate Filter Soot Concentration - Inferred Using Differential Pressure", 2, "unsigned", 0.00152590219, 0.0, "g/l", "did not answer (dead on this calibration)", "candidate"),
        SddDid(0x0463, "Injector Correction Codes for Cylinders 1 - 6", 64, "bytes", null, 0.0, "", "answered FC0FFF80...(64B)", "candidate"),
        SddDid(0x0479, "Stop-Start Start Counter", 3, "unsigned", 1.0, 0.0, "count", "answered 000000", "candidate"),
        SddDid(0x0518, "Injector Quantity Adjustment (IMA) values", 32, "ascii", null, 0.0, "", "did not answer (dead on this calibration)", "candidate"),
        SddDid(0x05FA, "Current air volume in urea pressure lines", 6, "", null, 0.0, "", "answered 048A00000301", "candidate"),
        SddDid(0x05FB, "Frozen cavity detection in urea tank", 2, "", null, 0.0, "", "answered 0FA0", "candidate"),
        SddDid(0x05FC, "Total mass fluid injected by urea dosing system", 4, "", null, 0.0, "", "answered 00039CC5", "candidate"),
        SddDid(0x05FD, "Total time of urea dosing value exceeding upper temperature threshold", 2, "", null, 0.0, "", "answered 0651", "candidate"),
        SddDid(0xC124, "Start authorisation status", 4, "enumerated", null, 0.0, "", "", "candidate"),
        SddDid(0xC18C, "PCM Target ID", 8, "", null, 0.0, "", "", "candidate"),
        SddDid(0xC25C, "ABS Serial Number", 6, "", null, 0.0, "", "", "candidate"),
        SddDid(0xC25D, "PCM Level 03", 5, "", null, 0.0, "", "", "candidate"),
        SddDid(0xC25E, "PCM Level 03", 5, "", null, 0.0, "", "", "candidate"),
        SddDid(0xD12F, "PCM Level 03", 5, "", null, 0.0, "", "", "candidate"),
        SddDid(0xD910, "EGR Valve Bank 1 Offset Values", 16, "unsigned", 0.01221, 0.0, "", "", "candidate"),
        SddDid(0xD911, "EGR Valve Bank 2 Offset Values", 16, "unsigned", 0.01221, 0.0, "", "", "candidate"),
        SddDid(0xD912, "EGR Throttle offset values", 16, "unsigned", 0.01221, 0.0, "", "", "candidate"),
        SddDid(0xD915, "Turbo Charger Valve Offset Values", 16, "unsigned", 0.01221, 0.0, "", "", "candidate"),
        SddDid(0xD91E, "Oil dilution data", 68, "", null, 0.0, "", "", "candidate"),
        SddDid(0xDE02, "High Pressure Pump Wear values", 12, "unsigned", null, 0.0, "", "", "candidate"),
        SddDid(0xDE03, "EGR Valve Bank 1 Offset Values", 16, "unsigned", 0.01221, 0.0, "", "", "candidate"),
        SddDid(0xDE04, "EGR Throttle Offset Values", 16, "unsigned", 0.01221, 0.0, "", "", "candidate"),
        SddDid(0xDE05, "Turbo Charger Valve Offset Values", 16, "unsigned", 0.01221, 0.0, "", "", "candidate"),
        SddDid(0xDE06, "Start Torque Correction", 2, "signed", 0.1, 0.0, "Nm", "", "candidate"),
        SddDid(0xDE07, "HLSDEM Low Setpoint Adjustment", 2, "signed", 0.5, 0.0, "rpm", "", "candidate"),
        SddDid(0xDE09, "FMO adjustment value", 130, "", null, 0.0, "", "", "candidate"),
        SddDid(0xDE0A, "FanCtl season counter", 1, "", null, 0.0, "", "", "candidate"),
        SddDid(0xDE0B, "Block offset for starting torque adjustment value", 8, "", null, 0.0, "", "", "candidate"),
        SddDid(0xDE0C, "Low idle engine speed adjustment", 58, "", null, 0.0, "", "", "candidate"),
        SddDid(0xDE10, "Particulate Filter Regeneration Statistics Load Information", 178, "", null, 0.0, "", "", "candidate"),
        SddDid(0xDE11, "Particulate Filter Values", 100, "", null, 0.0, "", "", "candidate"),
        SddDid(0xDE19, "Number of adaptions which are triggered by slip", 264, "", null, 0.0, "", "", "candidate"),
        SddDid(0xF111, "Assembly number", 24, "ascii", null, 0.0, "", "did not answer (dead on this calibration)", "candidate"),
        SddDid(0xF405, "Engine Coolant Temperature", 1, "unsigned", 1.0, -40.0, "°C", "answered 6E", "verified"),
    )

    // 24 PCM/SCR routine identifiers. REFERENCE ONLY -- never sent.
    val ROUTINES: List<Pair<Int, String>> = listOf(
        0x0202 to "Self Test",
        0x0404 to "VIN Learning",
        0x0406 to "Adaption Clear",
        0x4003 to "Speed limitation",
        0x400D to "Air Conditioning Compressor Exchanged",
        0x400F to "High Pressure Unit Exchanged",
        0x4024 to "Oil Change Reset",
        0x4026 to "Reset DPF pressure sensor adaptation value",
        0x4027 to "Fuel lift pump reset",
        0x402C to "Reset Fuel Mass Observer",
        0x4030 to "Initialise Throttle Valve Actuator",
        0x4035 to "Initialise Oxygen Sensor",
        0x4050 to "Turbine shut-off valve learn",
        0x4051 to "Initialise EGR Valve Learning",
        0x405B to "Activate service function - various",
        0x4063 to "Reset Oil Market Counter",
        0x4064 to "Reset water in fuel level",
        0x4076 to "Selective Catalytic Reductant Preparation",
        0x4089 to "DC to DC Convertor mode",
        0x408A to "HEV Operation mode",
        0x4091 to "Enable Glow Plugs",
        0x4094 to "Test Dosing Valve Dynamic Behaviour",
        0x4095 to "Test Measured Static Reductant Dosing Quantity",
        0x4096 to "Test Reductant Spray Pattern",
    )

    // ---- lookups ----------------------------------------------------------

    private val MODULE_BY_REQ: Map<Int, Triple<Int, Int, String>> =
        MODULES.associateBy { it.first }

    private val DID_BY_ID: Map<Int, SddDid> = DIDS.associateBy { it.did }

    /** The SDD module code at a request id ("ABS", "DSM"), or "" if unknown. */
    fun moduleCode(reqId: Int): String = MODULE_BY_REQ[reqId]?.third ?: ""

    /** SDD's entry for a diesel-PCM DID, or null when SDD does not name it. */
    fun did(id: Int): SddDid? = DID_BY_ID[id]

    /** SDD's name with its grade, or "" when SDD does not name the DID. */
    fun didLabel(id: Int): String = DID_BY_ID[id]?.label() ?: ""

    /** The DIDs the validation car answered that SDD names but has not proved —
     *  the candidate tier the live pass reads. */
    val CANDIDATE_DIDS: List<Int> =
        DIDS.filter { it.answersOnL494 && it.confidence != "verified" }.map { it.did }

    /**
     * The payload as one big-endian unsigned integer. NOT a decode: it claims
     * no unit and no scaling, it is the same bytes written in base 10 so a
     * monotonic counter can be seen to count. Null for anything wider than a
     * Long can hold honestly.
     */
    fun unscaledInt(body: ByteArray): Long? {
        if (body.isEmpty() || body.size > 8) return null
        var v = 0L
        for (b in body) v = (v shl 8) or (b.toLong() and 0xFF)
        return v
    }

    // ---- the reference view ----------------------------------------------

    /**
     * The whole SDD layer as card rows. Reads NOTHING from the car — this is
     * the same with the dongle unplugged, which is exactly why it is useful
     * standing next to the car deciding what to ask it.
     */
    fun reference(): List<EnhancedReading> {
        val out = mutableListOf(
            EnhancedReading(
                "Source",
                "Jaguar Land Rover SDD 169 dealer database, decrypted ($PLATFORM). " +
                    "${MODULES.size} modules · ${DIDS.size} PCM DIDs · " +
                    "${ROUTINES.size} routines.",
                "",
            ),
            EnhancedReading(
                "How to read it",
                "Rows marked [candidate] carry an authoritative SDD name against a value " +
                    "this car has not proved — the raw payload is always shown beside any " +
                    "decode. SDD names a curated ${DIDS.size} of the ~191 DIDs the PCM " +
                    "physically answers, so absence here does NOT mean a DID is dead.",
                "",
            ),
            EnhancedReading("Urea / SCR cluster", UREA_WATCH_NOTE, ""),
            EnhancedReading(
                "What this settled",
                "0x7E6 is the ABS module, not an SCR controller. There is no separate " +
                    "SCR/reagent ECU anywhere in the L494 map — SCR is managed by the PCM " +
                    "at 0x7E0. That is the question the AdBlue job opened with.",
                "",
            ),
            EnhancedReading("Module address map", "${MODULES.size} modules, $PLATFORM", ""),
        )
        for ((req, resp, code) in MODULES) {
            out.add(
                EnhancedReading(
                    "0x%03X → 0x%03X".format(req, resp),
                    code + if (resp != req + 8) {
                        "  (note: response is NOT request+8 — the usual JLR convention " +
                            "does not hold for this row)"
                    } else "",
                    "",
                )
            )
        }
        out.add(EnhancedReading("Diesel-PCM identifiers", "${DIDS.size} named by SDD", ""))
        for (d in DIDS) {
            val what = when {
                d.scalable -> "scaled ×${d.resolution}" + if (d.units.isNotEmpty()) " ${d.units}" else ""
                else -> "raw — SDD gives no scaling"
            }
            val live = when {
                d.answersOnL494 -> " · answers on SALWA2EK8HA"
                d.live.isNotEmpty() -> " · dead on SALWA2EK8HA"
                else -> ""
            }
            out.add(EnhancedReading("DID ${d.hex} · ${d.label()}", what + live, ""))
        }
        out.add(
            EnhancedReading(
                "Routine identifiers",
                "${ROUTINES.size} PCM/SCR routines. $ROUTINES_NOTE",
                "",
            )
        )
        for ((id, name) in ROUTINES) {
            val star = if (id in AFTERTREATMENT_ROUTINES) " ★" else ""
            out.add(EnhancedReading("Routine %04X$star".format(id), name, ""))
        }
        return out
    }
}
