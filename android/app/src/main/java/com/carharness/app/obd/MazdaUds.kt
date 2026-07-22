package com.carharness.app.obd

/**
 * Mazda (SkyActiv-era) module diagnostics over an ELM327: UDS (ISO 14229)
 * with standard 11-bit physical addressing — requests on 0x7E0-0x7E7, each
 * module answers on request + 8 (PCM 7E0/7E8). Port of the desktop's
 * backend/obd/uds_elm327.py + backend/profiles/mazda/scan.py.
 *
 * SCOPE: a standard OBD dongle reaches the HS-CAN bus only. Mazda's body /
 * comfort modules (locks, lights, cluster config) live on MS-CAN and are
 * invisible here — reaching them needs an MS-CAN switchable adapter.
 * Read-only apart from clearDtcs(), which the UI confirm-gates.
 */
object MazdaUds {

    // The community-mapped SkyActiv Gen6 module map (opendbc firmware queries +
    // the ND CAN projects); response id = request + 8. Which are fitted varies
    // by model/trim, and a few live on MS-CAN on some cars — an address that
    // doesn't answer just times out and shows "not present". The generic
    // 0x7E1-0x7E7 block stays at the end to catch anything unmapped.
    val ECUS = listOf(
        Triple(0x7E0, "PCM", "Engine / powertrain control"),
        Triple(0x7E1, "TCM", "Automatic gearbox (if fitted)"),
        Triple(0x720, "IC", "Instrument cluster"),
        Triple(0x726, "FBCM", "Front body control module"),
        Triple(0x730, "EPS", "Power steering"),
        Triple(0x760, "DSC", "ABS / stability control"),
        Triple(0x737, "RCM", "Airbag / restraints"),
        Triple(0x731, "SSU", "Start-stop / keyless unit (may be MS-CAN only)"),
        Triple(0x736, "RBCM", "Rear body control module (may be MS-CAN only)"),
        Triple(0x706, "FSC", "Forward camera (if fitted)"),
        Triple(0x764, "MRCC", "Radar cruise (if fitted)"),
        Triple(0x772, "HUD", "Head-up display (if fitted)"),
        Triple(0x793, "SCBS", "City brake support (if fitted)"),
        Triple(0x7E2, "ECU @ 0x7E2", "HS-CAN module"),
        Triple(0x7E3, "ECU @ 0x7E3", "HS-CAN module"),
        Triple(0x7E4, "ECU @ 0x7E4", "HS-CAN module"),
        Triple(0x7E5, "ECU @ 0x7E5", "HS-CAN module"),
        Triple(0x7E6, "ECU @ 0x7E6", "HS-CAN module"),
        Triple(0x7E7, "ECU @ 0x7E7", "HS-CAN module"),
    )

    private val IDENT_DIDS = listOf(
        0xF190 to "VIN",
        0xF187 to "Part number",
        0xF188 to "Software",
        0xF18C to "ECU serial",
    )

    // The ELM header/filter the driver is currently aimed at (avoid re-sending
    // the same five AT commands for consecutive requests to one module).
    private var target: Int? = null

    /** Mazda module diagnostics need the 11-bit / 500k CAN protocol. */
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

    /** True if anything UDS-shaped lives at this address. */
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
                ok = false, simulated = simulated, brand = "mazda", modules = emptyList(),
                message = "This car is talking ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k HS-CAN that Mazda module scanning uses. If this is " +
                    "the Mazda, turn the ignition on and retry.",
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
        val simPrefix = if (simulated) "SIMULATED — no real car connected. " else ""
        return UdsScanReport(
            ok = true,
            simulated = simulated,
            brand = "mazda",
            modules = modules,
            message = simPrefix + "${present.size} module(s) answered on HS-CAN, $faults fault" +
                (if (faults == 1) "" else "s") +
                " stored. Read-only scan — nothing was changed on the car. Body/comfort " +
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
     * One pass over the Mazda-specific mode-22 DIDs (MazdaPids.PCM_DIDS) —
     * the "better than generic OBD" live values. Skips anything this car
     * doesn't answer. Safe: ReadDataByIdentifier only.
     */
    fun readEnhanced(
        session: Elm327Session,
        onProgress: (String) -> Unit = {},
    ): List<EnhancedReading> {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            throw ObdException(
                "Not on the 11-bit/500k HS-CAN a Mazda uses.", ObdException.Kind.PROTOCOL
            )
        }
        val out = mutableListOf<EnhancedReading>()
        begin(session)
        try {
            for (def in MazdaPids.PCM_DIDS) {
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
