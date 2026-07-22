package com.carharness.app.obd

/**
 * VW / Audi (VAG group: Škoda, SEAT) module diagnostics over an ELM327 — pure
 * UDS (ISO 14229), no KWP dialect. Port of backend/obd + profiles/vw/scan.py.
 *
 * VAG uses two id blocks on 11-bit/500k (docs/private/vag-research.md §2):
 *  - Powertrain sits in the standard ISO 15765-4 block, response = request + 8
 *    (engine 0x7E0/0x7E8, gearbox 0x7E1/0x7E9, …).
 *  - Body / chassis / comfort modules use VAG's own diagnostic block: request
 *    ids in 0x700–0x76F answered on request + 0x6A (gateway 0x710/0x77A,
 *    cluster 0x714/0x77E, …). A VAG body module does NOT answer on request+8,
 *    so aim() must set the explicit +0x6A receive filter from [RESP_FOR].
 *
 * The gateway (address 19) routes every diagnostic id to the module's sub-bus,
 * so a standard dongle reaches all of these at the OBD port — unlike PSA there
 * is no separate pins-3/8 diagnostic CAN to worry about.
 *
 * SCOPE (research §1): only the UDS era is reachable over a plain dongle —
 * VW/Škoda/SEAT ~2012+ (MQB, Golf 7 on) and Audi ~2008+ (B8 on). Pre-2012 cars
 * speak TP2.0 (KWP-over-CAN) or K-line, whose timed channel setup an ELM327
 * can't hold — the scan says so honestly rather than listing every module as
 * absent. SFD on 2020+ cars blocks writes only, not the reads here (research §5).
 *
 * Read-only apart from clearDtcs(), which the UI confirm-gates.
 */
object VwUds {

    /** One VAG module: request id, its (+0x6A or +8) response id, VCDS code, name. */
    data class Ecu(val req: Int, val resp: Int, val code: String, val name: String)

    // Curated VAG UDS table for the modules a dealer checks pre-sale. ``code`` is
    // the VCDS 2-digit address (01 engine, 19 gateway, 17 cluster, …) so the
    // report reads like a VCDS auto-scan. Ids from the ODIS-extracted table
    // (ConnorHowell/vag-uds-ids) cross-checked with the +0x6A rule and the
    // carscanner MQB list — research §2. The gateway leads (VCDS lists it first
    // too, mirroring the Peugeot BSI-first order); the rest run roughly by
    // address. Modules a car isn't fitted with simply time out and read absent.
    val ECUS = listOf(
        Ecu(0x710, 0x77A, "19", "Gateway (J533)"),
        Ecu(0x7E0, 0x7E8, "01", "Engine control (ECM)"),
        Ecu(0x7E1, 0x7E9, "02", "Transmission (TCM / DSG)"),
        Ecu(0x7E2, 0x7EA, "11", "Engine control 2"),
        Ecu(0x714, 0x77E, "17", "Instrument cluster"),
        Ecu(0x713, 0x77D, "03", "ABS / brakes (ESP)"),
        Ecu(0x715, 0x77F, "15", "Airbag / SRS"),
        Ecu(0x712, 0x77C, "44", "Power steering (EPS)"),
        Ecu(0x70E, 0x778, "09", "Central electrics (BCM)"),
        Ecu(0x746, 0x7B0, "08", "Climate / A/C"),
        Ecu(0x711, 0x77B, "25", "Immobilizer"),
        Ecu(0x70C, 0x776, "16", "Steering column electronics"),
        Ecu(0x70F, 0x779, "22", "AWD / Haldex"),
        Ecu(0x732, 0x79C, "05", "Access & start (Kessy)"),
        Ecu(0x70B, 0x775, "65", "TPMS"),
        Ecu(0x70A, 0x774, "10", "Park / drive assist"),
        Ecu(0x752, 0x7BC, "53", "Parking brake"),
        Ecu(0x72A, 0x794, "—", "SCR / AdBlue (diesel)"),
        Ecu(0x773, 0x7DD, "5F", "Infotainment head unit (MIB)"),
        Ecu(0x7E5, 0x7ED, "—", "HV battery energy control (EV/PHEV)"),
        Ecu(0x7E6, 0x7EE, "—", "Drive motor control (EV)"),
    )

    private val RESP_FOR: Map<Int, Int> = ECUS.associate { it.req to it.resp }

    // Standard UDS identification DIDs (research §3). Modules support different
    // subsets; read best-effort and keep whatever answers as text.
    private val IDENT_DIDS = listOf(
        0xF190 to "VIN",
        0xF187 to "VAG part number",
        0xF189 to "Software",
        0xF191 to "Hardware",
        0xF197 to "System name",
        0xF18C to "ECU serial",
    )

    // UDS NRCs that mean "I might answer in a different session" — escalate once.
    private val SESSION_NRCS = setOf(0x11, 0x22, 0x7E, 0x7F)

    // reportDTCByStatusMask masks, laddered. No single public "VAG mask" exists
    // (research §3): 0xFF = all DTCs, 0x08 = confirmed only, 0xAF = broad tool
    // mask. A bad/unsupported mask returns NRC 0x31 (out of range) or 0x12
    // (sub-func not supported) — move to the next mask on those, like Peugeot.
    private val DTC_MASKS = listOf(0xFF, 0x08, 0xAF)
    private val MASK_RETRY_NRCS = setOf(0x12, 0x31)

    // On VAG the engine IS 0x7E0 — the same id owns the standard OBD slot, so
    // there is no separate PSA-style "OBD engine" id to switch to.
    private const val ENGINE = 0x7E0

    private var target: Int? = null

    /** VAG module diagnostics need the 11-bit / 500k CAN protocol. */
    fun protocolSupported(session: Elm327Session): Boolean =
        session.protocolNum == 6 || session.protocolNum == null

    // ------------------------------------------------------------- transport

    private fun begin(session: Elm327Session) {
        session.at("ATAT0")   // adaptive timing off: we manage the timeout
        session.at("ATST64")  // ~410ms silence window — plenty for a live module
        target = null
    }

    /** Point the ELM at one VAG module: TX header + explicit RX filter + FC. */
    private fun aim(session: Elm327Session, reqId: Int) {
        if (target == reqId) return
        val respId = RESP_FOR[reqId] ?: (reqId + 8)
        session.at("ATSH%03X".format(reqId))
        session.at("ATFCSH%03X".format(reqId))
        session.at("ATCRA%03X".format(respId))
        session.at("ATFCSD300000")
        session.at("ATFCSM1")
        target = reqId
    }

    private fun restore(session: Elm327Session) {
        for (cmd in listOf("ATCRA", "ATSH7DF", "ATFCSM0", "ATAT1", "ATST32")) {
            try {
                session.at(cmd)
            } catch (_: Exception) {
            }
        }
        target = null
    }

    private fun request(
        session: Elm327Session,
        reqId: Int,
        service: Int,
        dataHex: String,
        timeoutMs: Long = 6000,
    ): ByteArray {
        aim(session, reqId)
        val cmd = "%02X".format(service) + dataHex
        var pendingRetries = 2
        while (true) {
            val records = session.udsRecords(cmd, timeoutMs)
            var negative: UdsNegativeResponse? = null
            for (r in records) {
                if (r.isEmpty()) continue
                val first = r[0].toInt() and 0xFF
                if (first == service + 0x40) return r
                if (first == 0x7F && r.size >= 3) {
                    negative = UdsNegativeResponse(r[1].toInt() and 0xFF, r[2].toInt() and 0xFF)
                }
            }
            if (negative != null) {
                if (negative.nrc == 0x78 && pendingRetries-- > 0) {
                    Thread.sleep(300)
                    continue
                }
                throw negative
            }
            throw ObdException("No usable reply to $cmd.", ObdException.Kind.NO_DATA)
        }
    }

    /**
     * Open a UDS extended session (10 03) so a module that refused a read in the
     * default session will answer. Standard UDS — NOT PSA's 10 C0. Read-safe:
     * the session lapses on its own after a few seconds of silence.
     */
    private fun openSession(session: Elm327Session, reqId: Int, trace: Trace): Boolean {
        if (reqId in sessionOpen) return true
        try {
            request(session, reqId, 0x10, "03", 2000)
            trace.add("10 03 → extended session")
            sessionOpen.add(reqId)
            return true
        } catch (e: UdsNegativeResponse) {
            trace.add("10 03 → ${e.shortText()}")
        } catch (_: Exception) {
            trace.add("10 03 → no answer")
        }
        return false
    }

    /** Modules whose extended session is already open this scan. */
    private val sessionOpen = mutableSetOf<Int>()

    // ------------------------------------------------------------- services

    /** Per-module debug trail — what we sent and what came back. */
    private class Trace {
        val lines = mutableListOf<String>()
        fun add(line: String) {
            if (lines.size < 14) lines.add(line)
        }
    }

    private fun UdsNegativeResponse.shortText(): String =
        "NRC %02X (%s)".format(nrc, UdsConst.NRC_NAMES[nrc] ?: "unknown")

    /** True if anything UDS-shaped lives at this id pair. Some ECUs never answer
     *  a naked TesterPresent but do answer an ident read — try both. A reply of
     *  either kind (positive or negative) proves an ECU is there. */
    private fun testerPresent(session: Elm327Session, reqId: Int, trace: Trace): Boolean {
        for ((service, data) in listOf(0x3E to "00", 0x22 to "F190")) {
            try {
                request(session, reqId, service, data, 3500)
                return true
            } catch (_: UdsNegativeResponse) {
                return true
            } catch (_: Exception) {
            }
        }
        trace.add("3E 00 / 22 F190 → silence")
        return false
    }

    /** Engine probe on the standard OBD slot: Mode 01 PID 00 is the one request
     *  an emissions ECU must answer with the ignition on, even on a car whose
     *  UDS side stays quiet. On VAG the engine IS 0x7E0. */
    private fun engineAnswersStandard(session: Elm327Session): Boolean = try {
        request(session, ENGINE, 0x01, "00", 4000)
        true
    } catch (_: UdsNegativeResponse) {
        true
    } catch (_: Exception) {
        false
    }

    /** Generic OBD Mode 03 stored codes as UdsDtc rows (engine fallback). */
    private fun obdStoredCodes(session: Elm327Session): List<UdsDtc> =
        Dtc.readCodes(session, 0x03).map { code ->
            UdsDtc(
                code = code,
                hex = "",
                status = "",
                flags = listOf("confirmed"),
                description = Dtc.describe(code),
            )
        }

    /**
     * The car's VIN over plain OBD Mode 09 PID 02, broadcast — the one VIN
     * request that works on any ~2001+ car regardless of what UDS does next.
     * Must run BEFORE aim() has narrowed the receive filter to one module.
     */
    fun vinFromObd(session: Elm327Session): String? = try {
        val records = session.query(0x09, 0x02, 8000)
        val text = records.asSequence()
            .map { r ->
                r.drop(if (r.size > 2 && (r[1].toInt() and 0xFF) == 0x02) 3 else 1)
                    .map { it.toInt() and 0xFF }
                    .filter { it in 32..126 }
                    .map { it.toChar() }
                    .joinToString("")
            }
            .maxByOrNull { it.length } ?: ""
        Regex("[A-HJ-NPR-Z0-9]{17}").find(text.uppercase())?.value
    } catch (_: Exception) {
        null
    }

    private fun readIdent(
        session: Elm327Session,
        reqId: Int,
        trace: Trace,
    ): List<Pair<String, String>> {
        val out = mutableListOf<Pair<String, String>>()
        for ((did, label) in IDENT_DIDS) {
            try {
                val payload = request(session, reqId, 0x22, "%04X".format(did), 2500)
                if (payload.size < 4) continue
                val value = printable(payload.drop(3))
                if (value.isNotEmpty()) out.add(label to value)
            } catch (e: UdsNegativeResponse) {
                if (did == 0xF190) trace.add("22 F190 → ${e.shortText()}")
            } catch (_: Exception) {
                if (did == 0xF190) trace.add("22 F190 → no answer")
            }
        }
        return out
    }

    private fun printable(body: List<Byte>): String {
        val text = body
            .map { it.toInt() and 0xFF }
            .filter { it in 32..126 }
            .map { it.toChar() }
            .joinToString("")
            .trim()
        return if (text.length >= (body.size + 1) / 2 && text.isNotEmpty()) {
            text
        } else {
            body.joinToString("") { "%02X".format(it.toInt() and 0xFF) }
        }
    }

    /**
     * Fault memory, laddered. On a session-conditions NRC (0x11/0x22/0x7E/0x7F)
     * open the UDS extended session (10 03) once and retry — some modules only
     * answer 19 02 in an active session. Pure UDS: no KWP fault-read fallback.
     */
    private fun readDtcs(session: Elm327Session, reqId: Int, trace: Trace): List<UdsDtc> {
        return try {
            readDtcsUds(session, reqId, trace)
        } catch (e: UdsNegativeResponse) {
            if (e.nrc in SESSION_NRCS && openSession(session, reqId, trace)) {
                try {
                    readDtcsUds(session, reqId, trace)
                } catch (e2: UdsNegativeResponse) {
                    trace.add("19 02 after session → ${e2.shortText()}")
                    throw e2
                }
            } else {
                throw e
            }
        }
    }

    private fun readDtcsUds(session: Elm327Session, reqId: Int, trace: Trace): List<UdsDtc> {
        var payload: ByteArray? = null
        var lastNegative: UdsNegativeResponse? = null
        for (mask in DTC_MASKS) {
            try {
                payload = request(session, reqId, 0x19, "02%02X".format(mask))
                trace.add("19 02 %02X → OK".format(mask))
                break
            } catch (e: UdsNegativeResponse) {
                lastNegative = e
                if (e.nrc in MASK_RETRY_NRCS) continue
                trace.add("19 02 %02X → ${e.shortText()}".format(mask))
                throw e
            }
        }
        val data = payload ?: run {
            lastNegative?.let { trace.add("19 02 → ${it.shortText()}") }
            throw (lastNegative ?: ObdException("Fault read failed.", ObdException.Kind.NO_DATA))
        }

        // Reply: 59 02 <availabilityMask> then 4-byte records [dtc x3][status].
        val dtcs = mutableListOf<UdsDtc>()
        var i = 3
        while (i + 3 < data.size) {
            val b0 = data[i].toInt() and 0xFF
            val b1 = data[i + 1].toInt() and 0xFF
            val b2 = data[i + 2].toInt() and 0xFF
            val status = data[i + 3].toInt() and 0xFF
            i += 4
            if (b0 == 0 && b1 == 0 && b2 == 0) continue
            val code = Dtc.decodePair(b0, b1)
            dtcs.add(
                UdsDtc(
                    code = code,
                    hex = "%02X%02X%02X".format(b0, b1, b2),
                    status = "%02X".format(status),
                    flags = UdsConst.STATUS_BITS
                        .filter { (bit, _) -> status and bit != 0 }
                        .map { it.second },
                    description = Dtc.describe(code),
                )
            )
        }
        return dtcs
    }

    // ------------------------------------------------------------- top level

    fun scan(
        session: Elm327Session,
        simulated: Boolean,
        onProgress: (String) -> Unit = {},
    ): UdsScanReport {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            return UdsScanReport(
                ok = false, simulated = simulated, brand = "vw", modules = emptyList(),
                message = "This car is talking ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k CAN that VAG UDS module scanning uses. If this is the " +
                    "VW/Audi, turn the ignition on and retry. Pre-2012 (TP2.0/K-line) VAG cars " +
                    "can't be module-scanned through a plain OBD dongle.",
            )
        }

        // The car's identity first, while the receive filter is still open:
        // Mode 09 VIN works on any ~2001+ car no matter what UDS does next.
        onProgress("Reading VIN…")
        val obdVin = if (simulated) null else vinFromObd(session)

        val modules = mutableListOf<UdsModuleResult>()
        begin(session)
        sessionOpen.clear()
        try {
            for (ecu in ECUS) {
                onProgress("Probing ${ecu.code}…")
                val trace = Trace()
                var viaStandardObd = false
                var present = testerPresent(session, ecu.req, trace)
                if (!present && ecu.req == ENGINE) {
                    onProgress("Probing ${ecu.code} (standard OBD channel)…")
                    present = engineAnswersStandard(session)
                    if (present) {
                        viaStandardObd = true
                        trace.add("answered on standard OBD 7E0 (01 00)")
                    }
                }
                if (!present) {
                    modules.add(
                        UdsModuleResult(
                            ecu.req, ecu.code, ecu.name, false,
                            emptyList(), emptyList(), null, trace.lines.toList()
                        )
                    )
                    continue
                }
                var ident: List<Pair<String, String>> = emptyList()
                var dtcs: List<UdsDtc> = emptyList()
                var error: String? = null
                try {
                    ident = readIdent(session, ecu.req, trace)
                    dtcs = readDtcs(session, ecu.req, trace)
                } catch (e: Exception) {
                    if (e !is UdsNegativeResponse && e !is ObdException) throw e
                    if (viaStandardObd) {
                        // Engine on the plain OBD channel may not speak UDS
                        // fault services — generic Mode 03 codes still do.
                        try {
                            dtcs = obdStoredCodes(session)
                            trace.add("fell back to OBD Mode 03")
                        } catch (_: Exception) {
                            error = e.message
                        }
                    } else {
                        error = e.message
                    }
                }
                modules.add(
                    UdsModuleResult(
                        ecu.req, ecu.code, ecu.name, true,
                        ident, dtcs, error, trace.lines.toList()
                    )
                )
            }
        } finally {
            restore(session)
        }

        val present = modules.filter { it.present }
        val faults = present.sumOf { it.dtcs.size }
        val simPrefix = if (simulated) "SIMULATED — no real car connected. " else ""

        val (moduleVin, vinWarning) = VinCheck.consensus(modules)
        val vin = moduleVin ?: obdVin

        return UdsScanReport(
            ok = true,
            simulated = simulated,
            brand = "vw",
            modules = modules,
            vin = vin,
            message = simPrefix + (vin?.let { "VIN $it. " } ?: "") +
                "${present.size} module(s) answered, " +
                "$faults fault" + (if (faults == 1) "" else "s") +
                " stored." + vinWarning +
                " Read-only scan — nothing was changed on the car.",
        )
    }

    /** Clear one module's fault memory, then re-read it. Returns fresh DTCs.
     *  UDS 14 FF FF FF (ClearDiagnosticInformation, all groups). */
    fun clearDtcs(session: Elm327Session, reqId: Int): List<UdsDtc> {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            throw ObdException("Not on a CAN connection.", ObdException.Kind.PROTOCOL)
        }
        begin(session)
        sessionOpen.clear()
        try {
            val trace = Trace()
            request(session, reqId, 0x14, "FFFFFF", 8000)
            return try {
                readDtcs(session, reqId, trace)
            } catch (_: Exception) {
                emptyList()
            }
        } finally {
            restore(session)
        }
    }

    /**
     * Dealer live "extras": one read-only pass over the VAG-specific values
     * (VwPids) addressed to the engine ECU (0x7E0). The remapped-OBD DIDs are
     * decoded honestly; the community diesel-DPF and EV DIDs are shown RAW with
     * their exact bytes (their scaling isn't publicly confirmed — research §4b)
     * plus a caveat reading. A value this car doesn't support never answers and
     * is skipped. Safe: ReadDataByIdentifier only. Mirror of profiles/vw/pids.py.
     */
    fun readEnhanced(
        session: Elm327Session,
        onProgress: (String) -> Unit = {},
    ): List<EnhancedReading> {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            throw ObdException(
                "Not on the 11-bit/500k CAN a VAG car's live values use.",
                ObdException.Kind.PROTOCOL
            )
        }
        val out = mutableListOf<EnhancedReading>()
        begin(session)
        try {
            // (a) Confirmed remapped OBD set (DID = 0xF400 + PID) — decoded honestly.
            for (def in VwPids.OBD_REMAP_DIDS) {
                onProgress("Reading ${def.name}…")
                val body = readDid(session, VwPids.ENGINE, def.did) ?: continue
                val value = try {
                    def.decode(body)
                } catch (_: Exception) {
                    null
                } ?: continue
                out.add(EnhancedReading(def.name, value, def.unit))
            }

            // (b) Community diesel-DPF + EV DIDs — raw value + hex only, then a
            // caveat that the DIDs and scaling are community-sourced.
            var community = 0
            for (def in VwPids.DPF_DIDS + VwPids.EV_DIDS) {
                onProgress("Reading ${def.name}…")
                val body = readDid(session, VwPids.ENGINE, def.did) ?: continue
                val value = VwPids.rawReading(body) ?: continue
                out.add(EnhancedReading(def.name, value, "raw"))
                community++
            }
            if (community > 0) out.add(VwPids.COMMUNITY_CAVEAT)
        } finally {
            restore(session)
        }
        return out
    }

    /** One RDBI read on the engine ECU -> payload after `62 <did>`, or null. */
    private fun readDid(session: Elm327Session, reqId: Int, did: Int): ByteArray? = try {
        val payload = request(session, reqId, 0x22, "%04X".format(did), 2500)
        if (payload.size < 4 || (payload[0].toInt() and 0xFF) != 0x62) null
        else payload.drop(3).toByteArray()
    } catch (_: Exception) {
        null
    }
}
