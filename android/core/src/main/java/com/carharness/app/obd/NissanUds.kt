package com.carharness.app.obd

/**
 * Nissan module diagnostics over an ELM327: UDS (ISO 14229) on the 11-bit /
 * 500k HS-CAN bus. Same shape as the Ford walker, with the two things that
 * are Nissan's own:
 *
 *  - TWO addressing conventions share the one bus. Powertrain answers the
 *    standard ISO 15765-4 way (request 0x7E0-0x7E7, reply request + 8). Body,
 *    comfort and AV modules use Nissan's own scheme: request 0x740-0x74F,
 *    reply request + 0x20 (0x760-0x76F). So each module carries its own
 *    response id here instead of the walker computing one.
 *  - The Bose amp is NOT a diagnostic node of its own: its faults (B1F00 /
 *    U1263) are stored by the AV control unit, whose exact id we could not
 *    confirm from the mined dealer data. 0x74D is the best guess, so the whole
 *    0x740-0x74F block is walked and whatever answers is reported.
 *
 * Mirror of backend/profiles/nissan/pids.py NISSAN_ECU_TABLE — KEEP BOTH IN
 * SYNC. Read-only apart from clearDtcs(), which the UI confirm-gates.
 */
object NissanUds {

    /** One Nissan module: request id, its response id, code, name. */
    data class Ecu(val req: Int, val resp: Int, val code: String, val name: String)

    // Which modules are fitted varies by model and trim; an address that does
    // not answer just times out and shows "not present". The 0x7DF functional
    // broadcast is deliberately absent — it is not a module, it is every
    // powertrain ECU at once, and it would double-report the ECM.
    val ECUS = listOf(
        // ---- Standard ISO 15765-4 powertrain block (response = request + 8) --
        Ecu(0x7E0, 0x7E8, "ECM", "Engine control module"),
        Ecu(0x7E1, 0x7E9, "TCM", "Transmission control"),
        Ecu(0x7E2, 0x7EA, "ECU @ 0x7E2", "HS-CAN powertrain module"),
        Ecu(0x7E3, 0x7EB, "ECU @ 0x7E3", "HS-CAN powertrain module"),
        Ecu(0x7E4, 0x7EC, "ECU @ 0x7E4", "HS-CAN powertrain module"),
        Ecu(0x7E5, 0x7ED, "ECU @ 0x7E5", "HS-CAN powertrain module"),
        Ecu(0x7E6, 0x7EE, "ECU @ 0x7E6", "HS-CAN powertrain module"),
        Ecu(0x7E7, 0x7EF, "ECU @ 0x7E7", "HS-CAN powertrain module"),
        // ---- Nissan body / comfort / AV block (response = request + 0x20) ----
        // Only the BCM id is documented; the other names are best-effort, so a
        // row that answers under a different name is the car telling the truth.
        Ecu(0x740, 0x760, "ABS/VDC", "ABS / stability control (unverified name)"),
        Ecu(0x741, 0x761, "ECU @ 0x741", "Nissan body/comfort module"),
        Ecu(0x742, 0x762, "EPS", "Electric power steering (unverified name)"),
        Ecu(0x743, 0x763, "METER", "Combination meter / cluster (unverified name)"),
        Ecu(0x744, 0x764, "HVAC", "Air-conditioning / climate (unverified name)"),
        Ecu(0x745, 0x765, "BCM", "Body control module"),
        Ecu(0x746, 0x766, "SRS", "Airbag / occupant classification (unverified name)"),
        Ecu(0x747, 0x767, "ECU @ 0x747", "Nissan body/comfort module"),
        Ecu(0x748, 0x768, "ECU @ 0x748", "Nissan body/comfort module"),
        Ecu(0x749, 0x769, "ECU @ 0x749", "Nissan body/comfort module"),
        Ecu(0x74A, 0x76A, "ECU @ 0x74A", "Nissan body/comfort module"),
        Ecu(0x74B, 0x76B, "ECU @ 0x74B", "Nissan body/comfort module"),
        Ecu(0x74C, 0x76C, "ECU @ 0x74C", "Nissan body/comfort module"),
        Ecu(0x74D, 0x76D, "AV?", "AV control unit / head unit (holds Bose-amp DTCs)"),
        Ecu(0x74E, 0x76E, "ECU @ 0x74E", "Nissan body/comfort module"),
        Ecu(0x74F, 0x76F, "ECU @ 0x74F", "Nissan body/comfort module"),
    )

    private val RESP_FOR: Map<Int, Int> = ECUS.associate { it.req to it.resp }

    // The audio codes the AV unit stores, from the mined dealer data. Dtc's
    // generic table has no text for a Nissan B-code, so without this the one
    // fault the 370Z job is looking for would read "Unknown code".
    private val AUDIO_DTCS = mapOf(
        "B1F00" to "BOSE AMP",
        "U1263" to "Bose amp communication line to AV control unit",
    )

    // Nissan's identification block, as CONSULT reads it.
    private val IDENT_DIDS = listOf(
        0xF190 to "VIN",
        0xF188 to "Software part number",
        0xF18C to "ECU serial",
        0xF193 to "Hardware version",
        0xF195 to "Software version",
    )

    // The ELM header/filter the driver is currently aimed at (avoid re-sending
    // the same five AT commands for consecutive requests to one module).
    private var target: Int? = null

    /** Nissan module diagnostics need the 11-bit / 500k CAN protocol. */
    fun protocolSupported(session: Elm327Session): Boolean =
        session.protocolNum == 6 || session.protocolNum == null

    // ------------------------------------------------------------- transport

    private fun begin(session: Elm327Session) {
        session.at("ATAT0")   // adaptive timing off: we manage the timeout
        session.at("ATST64")  // ~410ms silence window — plenty for a live module
        target = null
    }

    /** Point the ELM at one module: TX header + RX filter + flow control. The
     *  filter comes from the table, because the body block replies on
     *  request + 0x20 and request + 8 would never be heard. */
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

    /** Build one fault row, preferring Nissan's own audio text over the
     *  generic J2012 table. */
    private fun dtcOf(b0: Int, b1: Int, b2: Int, status: Int): UdsDtc {
        val code = Dtc.decodePair(b0, b1)
        val generic = Dtc.describe(code)
        return UdsDtc(
            code = code,
            hex = "%02X%02X%02X".format(b0, b1, b2),
            status = "%02X".format(status),
            flags = UdsConst.STATUS_BITS
                .filter { (bit, _) -> status and bit != 0 }
                .map { it.second },
            description = AUDIO_DTCS[code] ?: generic,
        )
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
            dtcs.add(dtcOf(b0, b1, b2, status))
        }
        return dtcs
    }

    // ------------------------------------------------------------- top level

    /** The demo car: a 370Z-shaped bus with the Bose-amp fault the job is
     *  about, so the Modules tab can be shown with nothing plugged in. */
    private fun simulatedScan(): UdsScanReport {
        val present = setOf(0x7E0, 0x745, 0x74D)
        val idents = mapOf(
            0x7E0 to listOf("VIN" to "SIMULATEDZ34NISMO", "Software part number" to "23710-6GA0A"),
            0x745 to listOf("Software part number" to "284B1-6GA0A"),
            0x74D to listOf("Software part number" to "25915-6GA0A"),
        )
        val modules = ECUS.map { ecu ->
            if (ecu.req !in present) {
                UdsModuleResult(ecu.req, ecu.code, ecu.name, false, emptyList(), emptyList(), null)
            } else {
                val dtcs = if (ecu.req == 0x74D) {
                    listOf(
                        dtcOf(0x9F, 0x00, 0x49, 0x09),  // B1F00-49 "BOSE AMP"
                        dtcOf(0xD2, 0x63, 0x00, 0x09),  // U1263
                    )
                } else {
                    emptyList()
                }
                UdsModuleResult(
                    ecu.req, ecu.code, ecu.name, true,
                    idents[ecu.req] ?: emptyList(), dtcs, null,
                )
            }
        }
        return UdsScanReport(
            ok = true,
            simulated = true,
            brand = "nissan",
            modules = modules,
            vin = "SIMULATEDZ34NISMO",
            message = "SIMULATED — no real car connected. VIN SIMULATEDZ34NISMO. " +
                "3 module(s) answered on HS-CAN, 2 faults stored — both on the AV " +
                "control unit, which is where a Nissan keeps the Bose amp's codes.",
        )
    }

    fun scan(
        session: Elm327Session,
        simulated: Boolean,
        onProgress: (String) -> Unit = {},
    ): UdsScanReport {
        if (simulated) {
            onProgress("SIMULATED — building the demo scan…")
            return simulatedScan()
        }
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            return UdsScanReport(
                ok = false, simulated = simulated, brand = "nissan", modules = emptyList(),
                message = "This car is talking ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k HS-CAN that Nissan module scanning uses. If this is " +
                    "the Nissan, turn the ignition on and retry.",
            )
        }
        val modules = mutableListOf<UdsModuleResult>()
        begin(session)
        try {
            for (ecu in ECUS) {
                onProgress("Probing ${ecu.code}…")
                if (!testerPresent(session, ecu.req)) {
                    modules.add(
                        UdsModuleResult(
                            ecu.req, ecu.code, ecu.name, false, emptyList(), emptyList(), null
                        )
                    )
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
        val (consensusVin, vinWarning) = VinCheck.consensus(modules)
        // A module's own F190 (consensus) VIN wins; otherwise fall back to the
        // universal OBD Mode 09 read (the ELM is back on broadcast after
        // restore()).
        val vin = consensusVin ?: vinFromObd(session)
        return UdsScanReport(
            ok = true,
            simulated = simulated,
            brand = "nissan",
            modules = modules,
            vin = vin,
            message = (vin?.let { "VIN $it. " } ?: "") +
                "${present.size} module(s) answered on HS-CAN, $faults fault" +
                (if (faults == 1) "" else "s") +
                " stored." + vinWarning +
                " Read-only scan — nothing was changed on the car. The body/AV names " +
                "outside the ECM and BCM are best-effort: trust the address that " +
                "answered, not the label.",
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
}
