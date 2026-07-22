package com.carharness.app.obd

/**
 * Peugeot / PSA (Citroen, DS) module diagnostics over an ELM327: UDS
 * (ISO 14229) on the diagnostic CAN bus. Port of the desktop's
 * backend/obd/psa_uds_elm327.py + backend/profiles/peugeot/scan.py.
 *
 * WHY PSA IS DIFFERENT from the Mazda path: a normal OBD-II car answers a
 * request on 0x7E0 with 0x7E8 (request + 8). PSA does NOT — every module has
 * its own request/response pair (BSI 0x752 -> 0x652, engine 0x6A8 -> 0x688,
 * airbag 0x744 -> 0x644, ...). So aim() looks the response id up per module
 * instead of computing it, and some modules (airbag, BSI) only report faults
 * after a read-safe extended diagnostic session (0x10 0x03).
 *
 * SCOPE: 11-bit ISO 15765-4 CAN, i.e. AEE2010-era cars (~2010+). Pre-2004 VAN
 * cars can't be reached by any ELM327; many 2004-2010 KWP cars are out of
 * scope. Read-only apart from clearDtcs(), which the UI confirm-gates.
 */
object PeugeotUds {

    /** One PSA module: its request id, its (non-standard) response id, code, name. */
    data class Ecu(val req: Int, val resp: Int, val code: String, val name: String)

    // Curated PSA diagnostic table for the modules a dealer checks pre-sale.
    // Ids from the community PSA databases (ludwig-v/arduino-psa-diag, PSA-RE).
    val ECUS = listOf(
        Ecu(0x752, 0x652, "BSI", "Body Systems Interface (gateway)"),
        Ecu(0x6A8, 0x688, "CMM", "Engine / injection"),
        Ecu(0x6A9, 0x689, "BVA", "Automatic gearbox"),
        Ecu(0x6AD, 0x68D, "ABS/ESP", "Braking / stability"),
        Ecu(0x744, 0x644, "AIRBAG", "Airbags / SRS"),
        Ecu(0x75F, 0x65F, "COMBINE", "Instrument cluster"),
        Ecu(0x75D, 0x65D, "AAS", "Parking aid"),
        Ecu(0x6B5, 0x695, "DIRECTION", "Power steering"),
        Ecu(0x6B3, 0x693, "FSE", "Electric parking brake"),
        Ecu(0x76D, 0x66D, "CLIM", "Climate control"),
        Ecu(0x6AF, 0x68F, "DSG", "Tyre pressure (TPMS)"),
        Ecu(0x760, 0x660, "AUTORADIO", "Radio / audio"),
        Ecu(0x765, 0x665, "AFFICHEUR", "Central display"),
        Ecu(0x764, 0x664, "TELEMAT", "Telematics"),
        Ecu(0x6A1, 0x681, "SCR", "AdBlue / SCR (diesel)"),
        Ecu(0x6C1, 0x601, "ADDITIF", "DPF additive (diesel)"),
        Ecu(0x730, 0x710, "CPL", "Rain / light sensor"),
        Ecu(0x6B4, 0x694, "BMS", "HV battery (EV / PHEV)"),
    )

    private val RESP_FOR: Map<Int, Int> = ECUS.associate { it.req to it.resp }

    private val IDENT_DIDS = listOf(
        0xF190 to "VIN",
        0xF187 to "Part number",
        0xF188 to "Software",
        0xF18C to "ECU serial",
    )

    // UDS NRCs that mean "I might answer in a different session" — escalate once.
    private val SESSION_NRCS = setOf(0x11, 0x22, 0x7E, 0x7F)

    private var target: Int? = null

    /** PSA module diagnostics need the 11-bit / 500k CAN protocol. */
    fun protocolSupported(session: Elm327Session): Boolean =
        session.protocolNum == 6 || session.protocolNum == null

    // ------------------------------------------------------------- transport

    private fun begin(session: Elm327Session) {
        session.at("ATAT0")   // adaptive timing off: we manage the timeout
        session.at("ATST64")  // ~410ms silence window — plenty for a live module
        target = null
    }

    /** Point the ELM at one PSA module: TX header + explicit RX filter + FC. */
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
     * Best-effort DiagnosticSessionControl -> extended (0x10 0x03). Read-safe:
     * it changes only the ECU's session state (dropped on its own after a few
     * seconds) and writes nothing. Returns true on a positive 0x50 reply.
     */
    private fun startExtendedSession(session: Elm327Session, reqId: Int): Boolean = try {
        val reply = request(session, reqId, 0x10, "03", 2000)
        reply.isNotEmpty() && (reply[0].toInt() and 0xFF) == 0x50
    } catch (_: Exception) {
        false
    }

    // ------------------------------------------------------------- services

    private fun testerPresent(session: Elm327Session, reqId: Int): Boolean = try {
        request(session, reqId, 0x3E, "00", 4000)
        true
    } catch (_: UdsNegativeResponse) {
        true  // a negative response still proves an ECU is there
    } catch (_: Exception) {
        false
    }

    private fun readIdent(session: Elm327Session, reqId: Int): List<Pair<String, String>> {
        val out = mutableListOf<Pair<String, String>>()
        for ((did, label) in IDENT_DIDS) {
            try {
                val payload = request(session, reqId, 0x22, "%04X".format(did), 2500)
                if (payload.size < 4) continue
                val body = payload.drop(3)
                val text = body
                    .map { it.toInt() and 0xFF }
                    .filter { it in 32..126 }
                    .map { it.toChar() }
                    .joinToString("")
                    .trim()
                val value = if (text.length >= (body.size + 1) / 2 && text.isNotEmpty()) {
                    text
                } else {
                    body.joinToString("") { "%02X".format(it.toInt() and 0xFF) }
                }
                if (value.isNotEmpty()) out.add(label to value)
            } catch (_: Exception) {
                // DID not supported on this module — normal, skip it.
            }
        }
        return out
    }

    /** reportDTCByStatusMask, escalating to an extended session if refused. */
    private fun readDtcs(session: Elm327Session, reqId: Int): List<UdsDtc> = try {
        readDtcsOnce(session, reqId)
    } catch (e: UdsNegativeResponse) {
        if (e.nrc in SESSION_NRCS && startExtendedSession(session, reqId)) {
            readDtcsOnce(session, reqId)
        } else {
            throw e
        }
    }

    private fun readDtcsOnce(session: Elm327Session, reqId: Int): List<UdsDtc> {
        var payload: ByteArray? = null
        var lastNegative: UdsNegativeResponse? = null
        for (mask in listOf(0xAF, 0x0C)) {
            try {
                payload = request(session, reqId, 0x19, "02%02X".format(mask))
                break
            } catch (e: UdsNegativeResponse) {
                lastNegative = e
                if (e.nrc == 0x12 || e.nrc == 0x31) continue
                throw e
            }
        }
        val data = payload ?: throw (lastNegative ?: ObdException(
            "Fault read failed.", ObdException.Kind.NO_DATA
        ))

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
                ok = false, simulated = simulated, brand = "peugeot", modules = emptyList(),
                message = "This car is talking ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k CAN that PSA module scanning uses. If this is the " +
                    "Peugeot, turn the ignition on and retry. Pre-2004 (VAN) and many 2004-2010 " +
                    "(KWP) cars can't be module-scanned through a plain OBD dongle.",
            )
        }
        val modules = mutableListOf<UdsModuleResult>()
        begin(session)
        try {
            for (ecu in ECUS) {
                onProgress("Probing ${ecu.code}…")
                if (!testerPresent(session, ecu.req)) {
                    modules.add(UdsModuleResult(ecu.req, ecu.code, ecu.name, false, emptyList(), emptyList(), null))
                    continue
                }
                var ident: List<Pair<String, String>> = emptyList()
                var dtcs: List<UdsDtc> = emptyList()
                var error: String? = null
                try {
                    ident = readIdent(session, ecu.req)
                    dtcs = readDtcs(session, ecu.req)
                } catch (e: UdsNegativeResponse) {
                    error = e.message
                } catch (e: ObdException) {
                    error = e.message
                }
                modules.add(UdsModuleResult(ecu.req, ecu.code, ecu.name, true, ident, dtcs, error))
            }
        } finally {
            restore(session)
        }

        val present = modules.filter { it.present }
        val faults = present.sumOf { it.dtcs.size }
        val simPrefix = if (simulated) "SIMULATED — no real car connected. " else ""

        // Dealer check: every module that knows its VIN should agree. A module
        // reporting a different VIN was swapped in from another car (crash
        // repair, or the cluster/BSI dance used to disguise mileage).
        val vins = modules.flatMap { m ->
            m.ident.filter { it.first == "VIN" && it.second.length >= 11 }
                .map { m.code to it.second.take(17) }
        }
        val distinct = vins.map { it.second }.distinct()
        val vinWarning = if (distinct.size > 1) {
            " ⚠ VIN MISMATCH: " + vins.joinToString(", ") { "${it.first}=${it.second}" } +
                " — a control unit from another car is fitted (crash repair or clocking risk)."
        } else ""

        return UdsScanReport(
            ok = true,
            simulated = simulated,
            brand = "peugeot",
            modules = modules,
            message = simPrefix + "${present.size} module(s) answered on the diagnostic CAN, " +
                "$faults fault" + (if (faults == 1) "" else "s") +
                " stored." + vinWarning +
                " Read-only scan — nothing was changed on the car. Modules on other " +
                "PSA buses (or older KWP/VAN cars) aren't reachable through a standard OBD dongle.",
        )
    }

    // ---------------------------------------------------------- health check

    private const val KM_TO_MI = 0.621371

    /**
     * One RDBI read with per-module session escalation: some PSA modules only
     * expose measurement DIDs in an extended session (read-safe, lapses on its
     * own). Escalates at most once per module per pass. Returns the payload
     * after `62 <did>` or null if this car doesn't answer the DID.
     */
    private fun readDid(
        session: Elm327Session,
        reqId: Int,
        did: Int,
        escalated: MutableSet<Int>,
    ): ByteArray? {
        repeat(2) { attempt ->
            try {
                val payload = request(session, reqId, 0x22, "%04X".format(did), 2500)
                if (payload.size < 4) return null
                return payload.drop(3).toByteArray()
            } catch (e: UdsNegativeResponse) {
                val canEscalate = attempt == 0 && e.nrc in SESSION_NRCS &&
                    reqId !in escalated
                if (!canEscalate) return null
                escalated.add(reqId)
                if (!startExtendedSession(session, reqId)) return null
            } catch (_: Exception) {
                return null
            }
        }
        return null
    }

    /**
     * KWP service-0x21 block read: `21 <localId>` -> `61 <localId> <block>`.
     * Returns the block payload AFTER the `61 <localId>` echo (byte "A" = index
     * 0), or null if this engine doesn't answer the local id. Read-only.
     */
    private fun readBlock(session: Elm327Session, reqId: Int, localId: String): ByteArray? = try {
        val reply = request(session, reqId, 0x21, localId, 3000)
        val echoLen = 1 + localId.length / 2  // 0x61 + the local-id bytes
        if (reply.size <= echoLen) null else reply.copyOfRange(echoLen, reply.size)
    } catch (_: Exception) {
        null
    }

    /**
     * Dealer health check: one read-only pass over the PSA-specific values
     * (PeugeotPids) — DPF, injection, turbo, battery — plus any documented
     * odometer source. On an EV the HV battery answers UDS DIDs (SOH/SOC); on
     * an HDi diesel the engine answers the KWP 0x21 measurement blocks. A car
     * only answers the set that applies to it; the rest are silently skipped.
     */
    fun readEnhanced(
        session: Elm327Session,
        onProgress: (String) -> Unit = {},
    ): List<EnhancedReading> {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            throw ObdException(
                "Not on the 11-bit/500k CAN a PSA car's diagnostics use.",
                ObdException.Kind.PROTOCOL
            )
        }
        val out = mutableListOf<EnhancedReading>()
        begin(session)
        try {
            val escalated = mutableSetOf<Int>()
            for (def in PeugeotPids.DIDS) {
                onProgress("Reading ${def.name}…")
                val body = readDid(session, def.reqId, def.did, escalated) ?: continue
                val value = try {
                    def.decode(body)
                } catch (_: Exception) {
                    null
                } ?: continue
                out.add(EnhancedReading(def.name, value, def.unit))
            }

            // KWP 0x21 measurement blocks (HDi diesels): one request per block,
            // many values out of each. A non-diesel/newer engine just NAKs.
            for (block in PeugeotPids.BLOCKS) {
                onProgress("Reading ${block.label}…")
                val body = readBlock(session, block.reqId, block.localId) ?: continue
                for (field in block.fields) {
                    val value = try {
                        field.decode(body)
                    } catch (_: Exception) {
                        null
                    } ?: continue
                    out.add(EnhancedReading(field.name, value, field.unit))
                }
            }

            // Odometer cross-check: read every source that answers, show each,
            // then compare. Sources should agree to within a few miles.
            val odoKm = mutableListOf<Pair<String, Double>>()
            for (src in PeugeotPids.ODO_SOURCES) {
                onProgress("Reading odometer (${src.label})…")
                val body = readDid(session, src.reqId, src.did, escalated) ?: continue
                val km = try {
                    src.decodeKm(body)
                } catch (_: Exception) {
                    null
                } ?: continue
                odoKm.add(src.label to km)
                out.add(
                    EnhancedReading(
                        "Odometer (${src.label})",
                        "%,.0f mi (%,.0f km)".format(km * KM_TO_MI, km),
                        ""
                    )
                )
            }
            if (odoKm.size >= 2) {
                val kms = odoKm.map { it.second }
                val spreadMi = (kms.max() - kms.min()) * KM_TO_MI
                out.add(
                    if (spreadMi <= 6.0) {
                        EnhancedReading("Mileage cross-check", "✓ sources agree", "")
                    } else {
                        EnhancedReading(
                            "Mileage cross-check",
                            "⚠ sources differ by %,.0f mi — clocking risk".format(spreadMi),
                            ""
                        )
                    }
                )
            }
        } finally {
            restore(session)
        }
        return out
    }

    /**
     * Run one service reset's UDS steps against a PSA module (its request id) —
     * CHANGES THE CAR; the UI confirm-gates it. PSA's service-interval reset is
     * a BSI coding write behind a SecurityAccess unlock that isn't publicly
     * documented, so today the shipped PSA resets are dash procedures; this
     * exists for any capture-then-hardcode routine added later (e.g. DPF).
     */
    fun performReset(
        session: Elm327Session,
        reset: ServiceReset,
        onProgress: (String) -> Unit,
    ): List<String> {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            throw ObdException("Not on a PSA CAN connection.", ObdException.Kind.PROTOCOL)
        }
        begin(session)
        try {
            return ServiceResets.executeSteps(reset.steps, onProgress) { svc, dataHex, timeout ->
                request(session, reset.address, svc, dataHex, timeout)
            }
        } finally {
            restore(session)
        }
    }

    /** Clear one module's fault memory, then re-read it. Returns fresh DTCs. */
    fun clearDtcs(session: Elm327Session, reqId: Int): List<UdsDtc> {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            throw ObdException("Not on a CAN connection.", ObdException.Kind.PROTOCOL)
        }
        begin(session)
        try {
            request(session, reqId, 0x14, "FFFFFF", 8000)
            return try {
                readDtcs(session, reqId)
            } catch (_: Exception) {
                emptyList()
            }
        } finally {
            restore(session)
        }
    }
}
