package com.carharness.app.obd

/**
 * Ford module diagnostics over an ELM327: UDS (ISO 14229) with standard
 * 11-bit physical addressing — each module answers on request + 8
 * (PCM 0x7E0/0x7E8, BCM 0x726/0x72E). Same shape as the Mazda walker, with
 * Ford's own module map and Ford's identification DIDs.
 *
 * SCOPE: a standard OBD dongle reaches the HS-CAN bus only. Ford's slower
 * MS-CAN body/comfort modules (parking aid, climate on some cars) are
 * invisible here — reaching them needs an MS-CAN switchable adapter.
 * Read-only apart from clearDtcs(), which the UI confirm-gates.
 */
object FordUds {

    // Ford HS-CAN 11-bit request ids (response = request + 8). Which are
    // fitted varies by model/trim; an address that doesn't answer just times
    // out and shows "not present". The generic 0x7E2-0x7E7 block stays at the
    // end to catch anything unmapped.
    val ECUS = listOf(
        Triple(0x7E0, "PCM", "Powertrain control module (engine)"),
        Triple(0x7E1, "TCM", "Transmission control module (6R80 auto)"),
        Triple(0x760, "ABS", "Anti-lock brake / stability control"),
        Triple(0x726, "BCM", "Body control module"),
        Triple(0x720, "IPC", "Instrument panel cluster"),
        Triple(0x737, "RCM", "Restraints (airbag)"),
        Triple(0x730, "PSCM", "Power steering"),
        Triple(0x7D0, "APIM", "SYNC infotainment (if fitted)"),
        Triple(0x727, "ACM", "Audio control module"),
        Triple(0x761, "TCCM", "Transfer case 4x4 (if fitted)"),
        Triple(0x765, "OCS", "Occupant classification (if fitted)"),
        Triple(0x706, "IPMA", "Camera module (if fitted)"),
        Triple(0x764, "CCM", "Radar cruise (if fitted)"),
        Triple(0x736, "PAM", "Parking aid (may be MS-CAN only)"),
        Triple(0x7C4, "HVAC", "Climate (may be MS-CAN only)"),
        Triple(0x7E2, "ECU @ 0x7E2", "HS-CAN module"),
        Triple(0x7E3, "ECU @ 0x7E3", "HS-CAN module"),
        Triple(0x7E4, "ECU @ 0x7E4", "HS-CAN module"),
        Triple(0x7E5, "ECU @ 0x7E5", "HS-CAN module"),
        Triple(0x7E6, "ECU @ 0x7E6", "HS-CAN module"),
        Triple(0x7E7, "ECU @ 0x7E7", "HS-CAN module"),
    )

    // Ford's own identification block, as IDS/FDRS reads it.
    private val IDENT_DIDS = listOf(
        0xF190 to "VIN",
        0xF188 to "Software part number",
        0xF111 to "Core assembly part number",
        0xF113 to "Delivery assembly part number",
        0xF124 to "Calibration part number",
        0xF18C to "ECU serial",
    )

    // The ELM header/filter the driver is currently aimed at (avoid re-sending
    // the same five AT commands for consecutive requests to one module).
    private var target: Int? = null

    /** Ford module diagnostics need the 11-bit / 500k CAN protocol. */
    fun protocolSupported(session: Elm327Session): Boolean =
        session.protocolNum == 6 || session.protocolNum == null

    // ------------------------------------------------------------- transport

    private fun begin(session: Elm327Session) {
        session.at("ATAT0")   // adaptive timing off: we manage the timeout
        session.at("ATST64")  // ~410ms silence window — plenty for a live module
        target = null
    }

    /** Point the ELM at one module: TX header + RX filter + flow control. */
    private fun aim(session: Elm327Session, reqId: Int) {
        if (target == reqId) return
        val respId = reqId + 8
        session.at("ATSH%03X".format(reqId))
        session.at("ATFCSH%03X".format(reqId))
        session.at("ATCRA%03X".format(respId))
        session.at("ATFCSD300000")
        session.at("ATFCSM1")
        target = reqId
    }

    /** Put the ELM back the way the generic OBD stack expects it. */
    private fun restore(session: Elm327Session) {
        for (cmd in listOf("ATCRA", "ATSH7DF", "ATFCSM0", "ATAT1", "ATST32")) {
            try {
                session.at(cmd)
            } catch (_: Exception) {
            }
        }
        target = null
    }

    /**
     * Send one UDS request to the module at ``reqId`` and return the
     * positive-response payload. Throws UdsNegativeResponse on 7F,
     * ObdException(NO_DATA) when nothing answers. Retries on "pending".
     */
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

    // ------------------------------------------------------------- services

    /** True if anything UDS-shaped lives at this address. Ladder: some ECUs
     *  never answer a naked TesterPresent, so try an ident read too, and the
     *  0x7Ex OBD block also gets the Mode 01 request an emissions ECU must
     *  answer with the ignition on even if it ignores UDS. */
    private fun testerPresent(session: Elm327Session, reqId: Int): Boolean {
        val probes = mutableListOf(0x3E to "00", 0x22 to "F190")
        if (reqId in 0x7E0..0x7E7) probes.add(0x01 to "00")
        for ((service, data) in probes) {
            try {
                request(session, reqId, service, data, 4000)
                return true
            } catch (_: UdsNegativeResponse) {
                return true  // a negative response still proves an ECU is there
            } catch (_: Exception) {
            }
        }
        return false
    }

    /**
     * The car's VIN over plain OBD Mode 09 PID 02, broadcast — the one VIN
     * request that works on any ~2001+ car, and the backup for the module
     * F190 consensus. Must run with the ELM aimed at broadcast (7DF), i.e.
     * after restore().
     */
    private fun vinFromObd(session: Elm327Session): String? = try {
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

    private fun readDtcs(session: Elm327Session, reqId: Int): List<UdsDtc> {
        // reportDTCByStatusMask; wide mask first, then a narrow fallback.
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
                ok = false, simulated = simulated, brand = "ford", modules = emptyList(),
                message = "This car is talking ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k HS-CAN that Ford module scanning uses. If this is " +
                    "the Ford, turn the ignition on and retry.",
            )
        }
        val modules = mutableListOf<UdsModuleResult>()
        begin(session)
        try {
            for ((addr, code, name) in ECUS) {
                onProgress("Probing $code…")
                if (!testerPresent(session, addr)) {
                    modules.add(UdsModuleResult(addr, code, name, false, emptyList(), emptyList(), null))
                    continue
                }
                var ident: List<Pair<String, String>> = emptyList()
                var dtcs: List<UdsDtc> = emptyList()
                var error: String? = null
                try {
                    ident = readIdent(session, addr)
                    dtcs = readDtcs(session, addr)
                } catch (e: UdsNegativeResponse) {
                    error = e.message
                } catch (e: ObdException) {
                    error = e.message
                }
                modules.add(UdsModuleResult(addr, code, name, true, ident, dtcs, error))
            }
        } finally {
            restore(session)
        }

        val present = modules.filter { it.present }
        val faults = present.sumOf { it.dtcs.size }
        val (consensusVin, vinWarning) = VinCheck.consensus(modules)
        // A module's own F190 (consensus) VIN wins; otherwise fall back to the
        // universal OBD Mode 09 read (the ELM is back on broadcast after
        // restore()).
        val vin = consensusVin ?: vinFromObd(session)
        val simPrefix = if (simulated) "SIMULATED — no real car connected. " else ""
        return UdsScanReport(
            ok = true,
            simulated = simulated,
            brand = "ford",
            modules = modules,
            vin = vin,
            message = simPrefix + (vin?.let { "VIN $it. " } ?: "") +
                "${present.size} module(s) answered on HS-CAN, $faults fault" +
                (if (faults == 1) "" else "s") +
                " stored." + vinWarning +
                " Read-only scan — nothing was changed on the car. Body/comfort " +
                "modules on MS-CAN aren't reachable through a standard OBD dongle.",
        )
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

    /**
     * One pass over the Ford-specific mode-22 DIDs (FordPids.PCM_DIDS) — the
     * "better than generic OBD" live values, from Ford's own IDS MDX tables.
     * Skips anything this car doesn't answer. Safe: ReadDataByIdentifier only.
     */
    fun readEnhanced(
        session: Elm327Session,
        onProgress: (String) -> Unit = {},
    ): List<EnhancedReading> {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            throw ObdException(
                "Not on the 11-bit/500k HS-CAN a Ford uses.", ObdException.Kind.PROTOCOL
            )
        }
        val out = mutableListOf<EnhancedReading>()
        begin(session)
        try {
            for (def in FordPids.PCM_DIDS) {
                onProgress("Reading ${def.name}…")
                try {
                    val payload = request(session, def.reqId, 0x22, "%04X".format(def.did), 2500)
                    if (payload.size < 4) continue
                    val body = payload.drop(3).toByteArray()
                    val value = def.decode(body) ?: continue
                    out.add(EnhancedReading(def.name, value, def.unit))
                } catch (_: Exception) {
                    // DID not supported on this car/ECU — normal, skip it.
                }
            }
        } finally {
            restore(session)
        }
        return out
    }
}
