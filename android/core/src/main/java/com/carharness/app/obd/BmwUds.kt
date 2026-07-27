package com.carharness.app.obd

/**
 * BMW F-series module diagnostics over an ELM327: UDS (ISO 14229) with BMW's
 * extended 11-bit addressing — the tester transmits on CAN ID 6F1 with the
 * target module's address as the first data byte, and each module replies on
 * 0x600 + its address with 0xF1 (the tester) as the first byte.
 *
 * Read-only apart from clearDtcs(). Coding/flashing is deliberately absent —
 * that stays E-Sys's job over the ENET cable.
 */
object BmwUds {

    // F-series diagnostic addresses, incl. UKL-platform MINIs (F54/F55/F56/
    // F60) where the BDC took over the 0x40 body slot — same table as the
    // desktop's ENET scan. Probing an address that isn't fitted is harmless:
    // the gateway never answers and we mark it "not fitted".
    val ECUS = listOf(
        Triple(0x10, "ZGW", "Central gateway"),
        Triple(0x12, "DME/DDE", "Engine"),
        Triple(0x18, "EGS", "Automatic transmission"),
        Triple(0x01, "ACSM", "Airbag / crash safety"),
        Triple(0x29, "DSC", "ABS / stability control"),
        Triple(0x30, "EPS", "Power steering"),
        Triple(0x40, "FEM/BDC", "Body electronics"),
        Triple(0x72, "REM", "Rear body electronics"),
        Triple(0x60, "KOMBI", "Instrument cluster"),
        Triple(0x63, "HU", "Head unit (nav / radio)"),
        Triple(0x62, "HU-L", "Head unit (basic radio, if fitted)"),
        Triple(0x78, "IHKA", "Climate control"),
        Triple(0x5D, "KAFAS", "Camera / driver assistance (if fitted)"),
        Triple(0x5E, "GWS", "Gear selector (if fitted)"),
        Triple(0x56, "FZD", "Roof function centre (if fitted)"),
        Triple(0x67, "ZBE", "iDrive controller (if fitted)"),
        Triple(0x3D, "HUD", "Head-up display (if fitted)"),
        Triple(0x71, "AHM", "Trailer module (if fitted)"),
        Triple(0x36, "TCB", "Telematics (if fitted)"),
        Triple(0x21, "VDM", "Adaptive suspension (if fitted)"),
        Triple(0x64, "PMA", "Parking assistant (if fitted)"),
    )

    private val IDENT_DIDS = listOf(
        0xF190 to "VIN",
        0xF187 to "Part number",
        0xF189 to "Software",
        0xF191 to "Hardware",
    )

    private const val ENGINE = 0x12

    /** True when the detected protocol is the 11-bit / 500k CAN BMWs use. */
    fun protocolSupported(session: Elm327Session): Boolean = session.protocolNum == 6

    // ------------------------------------------------------------- transport

    /** One-off ELM setup for a module session. Undone by restore(). */
    private fun begin(session: Elm327Session) {
        session.at("ATFCSH6F1")
        session.at("ATFCSM1")
        session.at("ATAT0")   // adaptive timing off: we manage the timeout
        session.at("ATST64")  // ~410ms silence window — plenty for a live module
    }

    /** Point the ELM at one module's address. The flow control uses a finite
     *  block size (8 frames, 2ms gap) so a big fault dump pauses for our ACK
     *  every 8 frames instead of flooding the dongle's tiny buffer. */
    private fun target(session: Elm327Session, dst: Int) {
        session.at("ATFCSH6F1")  // re-assert: the engine fallback moves it to 7E0
        session.at("ATSH6F1")
        session.at("ATCEA%02X".format(dst))
        session.at("ATCRA6%02X".format(dst))
        session.at("ATFCSD%02X300802".format(dst))
    }

    /** Standard 11-bit OBD addressing (7E0 → 7E8) — some engine ECUs ignore
     *  the 6F1 gateway scheme but always answer on the plain OBD channel. */
    private fun targetStandard(session: Elm327Session) {
        session.at("ATCEA")      // extended addressing off
        session.at("ATSH7E0")
        session.at("ATFCSH7E0")
        session.at("ATCRA7E8")
        session.at("ATFCSD300802")
    }

    /** Put the ELM back the way the generic OBD stack expects it. */
    private fun restore(session: Elm327Session) {
        for (cmd in listOf("ATCEA", "ATCRA", "ATSH7DF", "ATFCSM0", "ATAT1", "ATST32")) {
            try {
                session.at(cmd)
            } catch (_: Exception) {
            }
        }
    }

    /**
     * Send one UDS request and return the positive-response payload.
     * Throws UdsNegativeResponse on 7F, ObdException(NO_DATA) when nothing
     * answers. Retries a couple of times on "response pending".
     */
    private fun request(
        session: Elm327Session,
        service: Int,
        dataHex: String,
        timeoutMs: Long = 6000,
    ): ByteArray {
        val cmd = "%02X".format(service) + dataHex
        var pendingRetries = 6
        while (true) {
            val records = session.udsRecords(cmd, timeoutMs)
            var negative: UdsNegativeResponse? = null
            for (raw in records) {
                // Some clones leave the 0xF1 tester byte in front — strip it.
                // A UDS first byte is never legitimately 0xF1.
                var r = raw
                if (r.isNotEmpty() && (r[0].toInt() and 0xFF) == 0xF1) {
                    r = r.copyOfRange(1, r.size)
                }
                if (r.isEmpty()) continue
                val first = r[0].toInt() and 0xFF
                if (first == service + 0x40) return r
                if (first == 0x7F && r.size >= 3) {
                    negative = UdsNegativeResponse(r[1].toInt() and 0xFF, r[2].toInt() and 0xFF)
                }
            }
            if (negative != null) {
                if (negative.nrc == 0x78 && pendingRetries-- > 0) {
                    Thread.sleep(400)
                    continue
                }
                throw negative
            }
            throw ObdException("No usable reply to $cmd.", ObdException.Kind.NO_DATA)
        }
    }

    // ------------------------------------------------------------- services

    // Gateway-channel probes: TesterPresent, then real ident reads — BMW's own
    // tooling opens contact with ReadDataByIdentifier, and some ECUs (the DME
    // foremost) never answer a naked 3E 00 at all.
    private val UDS_PROBES = listOf(0x3E to "00", 0x22 to "F190", 0x22 to "F150")

    // Standard-channel probes: Mode 01 PID 00 first — the one request an
    // emissions ECU MUST answer with the ignition on, even if it ignores UDS
    // on this channel entirely (plenty of DMEs do).
    private val OBD_PROBES = listOf(0x01 to "00", 0x22 to "F190", 0x3E to "00")

    /** True if anything answers at the currently-targeted address. */
    private fun answersHere(
        session: Elm327Session,
        probes: List<Pair<Int, String>> = UDS_PROBES,
    ): Boolean {
        for ((service, data) in probes) {
            try {
                request(session, service, data, 2500)
                return true
            } catch (_: UdsNegativeResponse) {
                return true  // a negative response still proves an ECU is there
            } catch (_: Exception) {
            }
        }
        return false
    }

    /** True if anything UDS-shaped lives at this address. */
    private fun testerPresent(session: Elm327Session, dst: Int): Boolean {
        target(session, dst)
        return answersHere(session)
    }

    private fun readIdent(session: Elm327Session): List<Pair<String, String>> {
        val out = mutableListOf<Pair<String, String>>()
        for ((did, label) in IDENT_DIDS) {
            try {
                val payload = request(session, 0x22, "%04X".format(did))
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

    /** Mode 09 identity on the standard channel — the emissions-mandated VIN
     *  and calibration id. Works on engines that ignore UDS there entirely. */
    private fun obdIdent(session: Elm327Session): List<Pair<String, String>> {
        val out = mutableListOf<Pair<String, String>>()
        for ((pid, label) in listOf("02" to "VIN", "04" to "Software")) {
            try {
                val text = request(session, 0x09, pid, 4000).drop(3)
                    .map { it.toInt() and 0xFF }
                    .filter { it in 33..126 }
                    .map { it.toChar() }
                    .joinToString("")
                    .trim()
                if (text.length >= 4) {
                    out.add(label to text.take(if (label == "VIN") 17 else 40))
                }
            } catch (_: Exception) {
                // PID not supported — normal on some engines, skip it.
            }
        }
        return out
    }

    private fun readDtcs(session: Elm327Session): List<UdsDtc> {
        // reportDTCByStatusMask. 0x0C (confirmed+pending) is what BMW's own
        // tooling asks for; wider/narrower masks are fallbacks. A chassis
        // module on a car that sat with a low battery can hold dozens of
        // faults — a reply too big or too slow for some BLE dongles — and a
        // count-only read (19 01) is the honest last stop.
        session.at("ATSTFF")  // ~1s silence window for big or slow replies
        try {
            var lastError: Exception? = null
            for (mask in listOf(0x0C, 0xAF, 0x08)) {
                try {
                    return parseDtcReport(request(session, 0x19, "02%02X".format(mask)))
                } catch (e: UdsNegativeResponse) {
                    lastError = e
                } catch (e: ObdException) {
                    lastError = e
                }
            }
            val count = try {
                readDtcCount(session)
            } catch (_: Exception) {
                null
            }
            when {
                count == 0 -> return emptyList()
                count != null -> throw ObdException(
                    "$count fault" + (if (count == 1) "" else "s") +
                        " stored, but the full list would not fit through this dongle.",
                    ObdException.Kind.NO_DATA
                )
            }
            throw lastError ?: ObdException("Fault read failed.", ObdException.Kind.NO_DATA)
        } finally {
            try {
                session.at("ATST64")
            } catch (_: Exception) {
            }
        }
    }

    /** 19 01 = reportNumberOfDTCByStatusMask: 59 01 <avail> <fmt> <hi> <lo>. */
    private fun readDtcCount(session: Elm327Session): Int {
        val p = request(session, 0x19, "01AF")
        if (p.size < 6) {
            throw ObdException("Short reply to the fault-count read.", ObdException.Kind.NO_DATA)
        }
        return ((p[4].toInt() and 0xFF) shl 8) or (p[5].toInt() and 0xFF)
    }

    private fun parseDtcReport(data: ByteArray): List<UdsDtc> {
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

    // ------------------------------------------------------------- top level

    fun scan(
        session: Elm327Session,
        simulated: Boolean,
        onProgress: (String) -> Unit = {},
    ): UdsScanReport {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            return UdsScanReport(
                ok = false, simulated = simulated, brand = "bmw", modules = emptyList(),
                message = "This car is talking ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k CAN that BMW F-series module scanning needs.",
            )
        }
        val modules = mutableListOf<UdsModuleResult>()
        begin(session)
        try {
            for ((addr, code, name) in ECUS) {
                onProgress("Probing $code — $name…")
                var viaStandardObd = false
                var present = testerPresent(session, addr)
                if (!present && addr == ENGINE) {
                    // The engine demonstrably talks (generic OBD works), so an
                    // engine that ignores the 6F1 scheme gets one more chance
                    // on the standard OBD channel it must answer.
                    onProgress("Probing $code — $name (standard OBD channel)…")
                    present = try {
                        targetStandard(session)
                        answersHere(session, OBD_PROBES)
                    } catch (_: Exception) {
                        false
                    }
                    viaStandardObd = present
                }
                if (!present) {
                    modules.add(UdsModuleResult(addr, code, name, false, emptyList(), emptyList(), null))
                    continue
                }
                var ident: List<Pair<String, String>> = emptyList()
                var dtcs: List<UdsDtc> = emptyList()
                var error: String? = null
                try {
                    ident = readIdent(session)
                    if (viaStandardObd) {
                        // Engine that ignores UDS here still owns Mode 09.
                        val have = ident.map { it.first }.toSet()
                        ident = ident + obdIdent(session).filter { it.first !in have }
                    }
                    dtcs = readDtcs(session)
                } catch (e: Exception) {
                    if (e !is UdsNegativeResponse && e !is ObdException) throw e
                    if (viaStandardObd) {
                        // Engine on the plain OBD channel may not speak UDS
                        // fault services — generic Mode 03 codes still do.
                        try {
                            dtcs = obdStoredCodes(session)
                        } catch (_: Exception) {
                            error = e.message
                        }
                    } else {
                        error = e.message
                    }
                }
                modules.add(UdsModuleResult(addr, code, name, true, ident, dtcs, error))
            }
        } finally {
            restore(session)
        }

        val present = modules.filter { it.present }
        val faults = present.sumOf { it.dtcs.size }
        val (vin, vinWarning) = VinCheck.consensus(modules)
        val engineHint = if (modules.any { it.address == ENGINE && !it.present }) {
            " The engine ECU only wakes with the ignition ON — turn the key / press " +
                "Start (engine can stay off) and rescan if it's missing."
        } else ""
        val simPrefix = if (simulated) "SIMULATED — no real car connected. " else ""
        return UdsScanReport(
            ok = true,
            simulated = simulated,
            brand = "bmw",
            modules = modules,
            vin = vin,
            message = simPrefix + (vin?.let { "VIN $it. " } ?: "") +
                "${present.size} modules answered, $faults fault" +
                (if (faults == 1) "" else "s") +
                " stored." + vinWarning + engineHint +
                " Read-only scan — nothing was changed on the car.",
        )
    }

    /**
     * Run one service reset's UDS steps against a module (KOMBI for CBS) over
     * the 6F1 gateway addressing — CHANGES THE CAR; the UI confirm-gates it.
     */
    fun performReset(
        session: Elm327Session,
        reset: ServiceReset,
        onProgress: (String) -> Unit,
    ): List<String> {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            throw ObdException("Not on a BMW CAN connection.", ObdException.Kind.PROTOCOL)
        }
        begin(session)
        try {
            target(session, reset.address)
            return ServiceResets.executeSteps(reset.steps, onProgress) { svc, dataHex, timeout ->
                request(session, svc, dataHex, timeout)
            }
        } finally {
            restore(session)
        }
    }

    /** Clear one module's fault memory, then re-read it. Returns fresh DTCs. */
    fun clearDtcs(session: Elm327Session, dst: Int): List<UdsDtc> {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            throw ObdException("Not on a BMW CAN connection.", ObdException.Kind.PROTOCOL)
        }
        begin(session)
        try {
            target(session, dst)
            try {
                request(session, 0x14, "FFFFFF", 8000)
            } catch (e: ObdException) {
                if (dst != ENGINE) throw e
                // Engine that only answers on the standard OBD channel.
                targetStandard(session)
                try {
                    request(session, 0x14, "FFFFFF", 8000)
                } catch (_: Exception) {
                    // OBD-only engine: the generic Mode 04 emissions clear it
                    // must support ("no data" back just means an empty reply).
                    try {
                        session.query(0x04, null, 10000)
                    } catch (e2: ObdException) {
                        if (e2.kind != ObdException.Kind.NO_DATA) throw e2
                    }
                }
            }
            return try {
                readDtcs(session)
            } catch (_: Exception) {
                try {
                    obdStoredCodes(session)
                } catch (_: Exception) {
                    emptyList()
                }
            }
        } finally {
            restore(session)
        }
    }
}
