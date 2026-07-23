package com.carharness.app.obd

/**
 * Jaguar / Land Rover module diagnostics over an ELM327: UDS (ISO 14229) with a
 * KWP2000-on-CAN fallback. Port of backend/profiles/jlr/scan.py.
 *
 * Scoped honestly to what a plain dongle actually reaches on a JLR
 * (docs/private/jlr-research-architecture.md + jlr-research-modules.md):
 *  - The 2006-2016 Ford/PLA CAN era (XF X250, XK, XJ X351, F-Type, Disco 3/4,
 *    RR Sport L320, Freelander 2, Ford-era L322) answers 11-bit CAN at 500k,
 *    response = request + 8. The publicly-VERIFIED modules are the DRIVELINE:
 *    engine 0x7E0, ZF gearbox 0x7E1, rear e-diff 0x795 (on-car X351/XK PID pack).
 *  - ABS, air suspension, EPB and the rest exist on the same bus but their
 *    request ids were never published — they are NOT invented here.
 *  - Body/comfort modules (BCM, cluster, HVAC, doors) sit on an MS-CAN that is
 *    NOT wired to OBD pins 6/14 — physically unreachable bare (the Peugeot pin
 *    lesson, JLR edition).
 *  - MY17+ new-architecture cars (Disco 5, Velar, 2018+ XE/XF/F-Pace, Defender
 *    L663, I-Pace, late L405/L494) moved manufacturer diagnostics to
 *    DoIP/Ethernet, which no ELM327 can speak; Evoque L538 / Disco Sport L550
 *    gateway-filter the port to the same effect. Those cars still answer
 *    generic OBD — the scan detects the pattern and says which wall it hit.
 *
 * Dialect: ~2010+ modules speak UDS; the older Ford-era cars may speak
 * KWP2000-over-CAN — each module ladders UDS first then falls back to KWP
 * (10 81 session, 1A ident, 18 02 FF FF read), standard KWP2000 and
 * UNVERIFIED-plausible for JLR, exactly like the Mercedes profile.
 *
 * Nothing security-related is ever addressed (RFA/KVM/immobiliser are
 * HARD-REFUSE in the research — name-only, no ids here at all).
 * Read-only apart from clearDtcs(), which the UI confirm-gates.
 */
object JlrUds {

    /** One JLR module: request id, its (+8) response id, code, name. */
    data class Ecu(val req: Int, val resp: Int, val code: String, val name: String)

    // The publicly-verified JLR driveline table (research §10a). Response =
    // request + 8 for every row, including 0x795 -> 0x79D.
    val ECUS = listOf(
        Ecu(0x7E0, 0x7E8, "PCM", "Engine control (ECM/PCM)"),
        Ecu(0x7E1, 0x7E9, "TCM", "Transmission (ZF 6HP/8HP)"),
        Ecu(0x795, 0x79D, "RDCM", "Rear differential (e-diff)"),
    )

    private val RESP_FOR: Map<Int, Int> = ECUS.associate { it.req to it.resp }

    // Standard UDS identification DIDs (research §4). Modules support different
    // subsets; read best-effort and keep whatever answers as text.
    private val IDENT_DIDS = listOf(
        0xF190 to "VIN",
        0xF187 to "Part number",
        0xF18C to "ECU serial",
        0xF188 to "Software",
        0xF191 to "Hardware",
    )

    // KWP2000 identification records (service 0x1A) read when every UDS DID
    // refuses (an older Ford-era module). UNVERIFIED-plausible for JLR.
    private val KWP_IDENT_RECORDS = listOf(
        0x87 to "Part number (KWP 1A 87)",
        0x88 to "Software (KWP 1A 88)",
        0x86 to "ECU ident (KWP 1A 86)",
    )

    // UDS NRCs that mean "I might answer in a different session" — escalate once.
    private val SESSION_NRCS = setOf(0x11, 0x22, 0x7E, 0x7F)

    // reportDTCByStatusMask masks, laddered. No public "JLR mask" (research §5):
    // 0xFF = all, 0x08 = confirmed, 0xAF = broad tool mask.
    private val DTC_MASKS = listOf(0xFF, 0x08, 0xAF)
    private val MASK_RETRY_NRCS = setOf(0x12, 0x31)

    // On JLR the engine IS 0x7E0 — the same id owns the standard OBD slot.
    private const val ENGINE = 0x7E0

    private var target: Int? = null

    /** JLR module diagnostics need the 11-bit / 500k CAN protocol. */
    fun protocolSupported(session: Elm327Session): Boolean =
        session.protocolNum == 6 || session.protocolNum == null

    // ------------------------------------------------------------- transport

    private fun begin(session: Elm327Session) {
        session.at("ATAT0")   // adaptive timing off: we manage the timeout
        session.at("ATST64")  // ~410ms silence window — plenty for a live module
        target = null
        udsSession.clear()
        kwpSession.clear()
    }

    /** Point the ELM at one JLR module: TX header + RX filter + FC. */
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
        udsSession.clear()
        kwpSession.clear()
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

    /** Modules whose UDS extended session (10 03) is already open this scan. */
    private val udsSession = mutableSetOf<Int>()

    /** Modules whose KWP session (10 81) is already open this scan. */
    private val kwpSession = mutableSetOf<Int>()

    /** Open a UDS extended session (10 03) so a module that refused a read in
     *  the default session will answer. Read-safe: the session lapses on its
     *  own after a few seconds of silence. */
    private fun openSession(session: Elm327Session, reqId: Int, trace: Trace): Boolean {
        if (reqId in udsSession) return true
        try {
            request(session, reqId, 0x10, "03", 2000)
            trace.add("10 03 → extended session")
            udsSession.add(reqId)
            return true
        } catch (e: UdsNegativeResponse) {
            trace.add("10 03 → ${e.shortText()}")
        } catch (_: Exception) {
            trace.add("10 03 → no answer")
        }
        return false
    }

    /** Bring an older KWP2000-over-CAN module up with the plain KWP start
     *  (10 81). UNVERIFIED-plausible for JLR — standard KWP2000. Read-safe. */
    private fun openKwpSession(session: Elm327Session, reqId: Int, trace: Trace): Boolean {
        if (reqId in kwpSession) return true
        try {
            request(session, reqId, 0x10, "81", 2000)
            trace.add("10 81 → KWP session")
            kwpSession.add(reqId)
            return true
        } catch (e: UdsNegativeResponse) {
            trace.add("10 81 → ${e.shortText()}")
        } catch (_: Exception) {
            trace.add("10 81 → no answer")
        }
        return false
    }

    // ------------------------------------------------------------- services

    /** Per-module debug trail — what we sent and what came back, so a failed
     *  field scan tells us exactly which rung of the ladder broke. */
    private class Trace {
        val lines = mutableListOf<String>()
        fun add(line: String) {
            if (lines.size < 14) lines.add(line)
        }
    }

    private fun UdsNegativeResponse.shortText(): String =
        "NRC %02X (%s)".format(nrc, UdsConst.NRC_NAMES[nrc] ?: "unknown")

    /** True if anything diagnostic-shaped lives at this id pair. Some ECUs never
     *  answer a naked TesterPresent but do answer an ident read — try both. A
     *  reply of either kind (positive or negative) proves an ECU is there. */
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
     *  an emissions ECU must answer with the ignition on, even on a DoIP-era or
     *  gateway-filtered car whose manufacturer side stays quiet. */
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
     * request that works on any ~2001+ car regardless of what UDS/KWP does next.
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
        var refused = 0
        for ((did, label) in IDENT_DIDS) {
            try {
                val payload = request(session, reqId, 0x22, "%04X".format(did), 2500)
                if (payload.size < 4) continue
                val value = printable(payload.drop(3))
                if (value.isNotEmpty()) out.add(label to value)
            } catch (e: UdsNegativeResponse) {
                refused++
                if (did == 0xF190) trace.add("22 F190 → ${e.shortText()}")
            } catch (_: Exception) {
                refused++
                if (did == 0xF190) trace.add("22 F190 → no answer")
            }
        }
        // Every UDS DID refused: a KWP-flavour module. Open its session and
        // read the classic KWP ident records instead (UNVERIFIED-plausible).
        if (out.isEmpty() && refused == IDENT_DIDS.size && openKwpSession(session, reqId, trace)) {
            for ((rec, label) in KWP_IDENT_RECORDS) {
                try {
                    val payload = request(session, reqId, 0x1A, "%02X".format(rec), 2500)
                    if (payload.size < 3 || (payload[0].toInt() and 0xFF) != 0x5A) continue
                    val value = printable(payload.drop(2))
                    if (value.isNotEmpty()) {
                        out.add(label to value)
                        trace.add("1A %02X → OK".format(rec))
                        // A 17-char VIN hiding in the record text still counts.
                        Regex("[A-HJ-NPR-Z0-9]{17}").find(value.uppercase())?.let {
                            out.add("VIN" to it.value)
                        }
                    }
                } catch (_: Exception) {
                }
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
     * Fault memory, laddered: UDS 19 02 first; on a session-conditions NRC
     * (0x11/0x22/0x7E/0x7F) open the UDS extended session (10 03) once and retry;
     * if the module doesn't speak UDS DTCs at all, fall back to the KWP fault
     * read (18 02 FF FF) an older Ford-era module answers.
     */
    private fun readDtcs(session: Elm327Session, reqId: Int, trace: Trace): List<UdsDtc> {
        try {
            return readDtcsUds(session, reqId, trace)
        } catch (e: UdsNegativeResponse) {
            if (e.nrc in SESSION_NRCS && openSession(session, reqId, trace)) {
                try {
                    return readDtcsUds(session, reqId, trace)
                } catch (e2: UdsNegativeResponse) {
                    trace.add("19 02 after session → ${e2.shortText()}")
                }
            }
            return readDtcsKwp(session, reqId, trace)
        } catch (_: ObdException) {
            return readDtcsKwp(session, reqId, trace)
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

    /** KWP2000 readDTCByStatus: 18 02 FF FF → 58 <count> then (hi, lo, status)
     *  per DTC. Adapted from the Mercedes/Peugeot decode. */
    private fun readDtcsKwp(session: Elm327Session, reqId: Int, trace: Trace): List<UdsDtc> {
        openKwpSession(session, reqId, trace)
        val data = try {
            request(session, reqId, 0x18, "02FFFF", 6000)
        } catch (e: UdsNegativeResponse) {
            trace.add("18 02 FF FF → ${e.shortText()}")
            throw e
        } catch (e: ObdException) {
            trace.add("18 02 FF FF → no answer")
            throw e
        }
        trace.add("18 02 FF FF → OK (KWP)")
        // 0x58 <count> <triples> is the book format; some ECUs skip the count.
        val offset = when {
            data.size >= 2 && (data.size - 2) % 3 == 0 -> 2
            (data.size - 1) % 3 == 0 -> 1
            else -> 2
        }
        val dtcs = mutableListOf<UdsDtc>()
        var i = offset
        while (i + 2 < data.size) {
            val hi = data[i].toInt() and 0xFF
            val lo = data[i + 1].toInt() and 0xFF
            val status = data[i + 2].toInt() and 0xFF
            i += 3
            if (hi == 0 && lo == 0) continue
            val code = Dtc.decodePair(hi, lo)
            dtcs.add(
                UdsDtc(
                    code = code,
                    hex = "%02X%02X".format(hi, lo),
                    status = "%02X".format(status),
                    flags = listOf("stored (KWP)"),
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
                ok = false, simulated = simulated, brand = "jlr", modules = emptyList(),
                message = "This car is talking ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k CAN that JLR module diagnostics use. If this is the " +
                    "Jaguar / Land Rover, turn the ignition on and retry. Early S-Type/X-Type " +
                    "are J1850 and the 2002-05 BMW-era L322 often won't talk to a CAN dongle.",
            )
        }

        // The car's identity first, while the receive filter is still open:
        // Mode 09 VIN works on any ~2001+ car no matter what UDS/KWP does next.
        onProgress("Reading VIN…")
        val obdVin = if (simulated) null else vinFromObd(session)

        val modules = mutableListOf<UdsModuleResult>()
        var mfrPresent = 0        // modules that answered a manufacturer probe
        var engineOnObd = false
        begin(session)
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
                        engineOnObd = true
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
                if (!viaStandardObd) mfrPresent++
                var ident: List<Pair<String, String>> = emptyList()
                var dtcs: List<UdsDtc> = emptyList()
                var error: String? = null
                try {
                    ident = readIdent(session, ecu.req, trace)
                    dtcs = readDtcs(session, ecu.req, trace)
                } catch (e: Exception) {
                    if (e !is UdsNegativeResponse && e !is ObdException) throw e
                    if (viaStandardObd) {
                        // Engine on the plain OBD channel may not speak UDS/KWP
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

        val eraNote = if (mfrPresent > 0) {
            " CAN-era JLR (2006-2016 Ford/PLA family). This driveline read is the honest dongle " +
                "ceiling: ABS, air suspension and EPB exist on the same bus but their module " +
                "addresses were never published, and body modules (BCM, cluster, HVAC, doors) " +
                "sit on an MS-CAN that isn't wired to the OBD pins the dongle uses — not " +
                "faults, just out of reach. Full-depth work needs a GAP IIDTool or SDD."
        } else if (engineOnObd || vin != null) {
            " The car answers generic OBD but no module answered JLR diagnostics. That is the " +
                "signature of a MY17+ DoIP car (Disco 5, Velar, 2018+ XE/XF/F-Pace, Defender " +
                "L663, I-Pace, late L405/L494 — manufacturer diagnostics moved to Ethernet, " +
                "which no OBD dongle can speak) or of the gateway-filtered Evoque L538 / " +
                "Discovery Sport L550. Generic-OBD-only territory, not a fault. The Service " +
                "tab's dash-procedure resets still work on these cars."
        } else {
            " No JLR module and no engine answered. If this is the Jaguar / Land Rover, turn " +
                "the ignition ON and retry. Early S-Type/X-Type (J1850) and the 2002-05 " +
                "BMW-era L322 can't be diagnosed on this dongle."
        }

        return UdsScanReport(
            ok = true,
            simulated = simulated,
            brand = "jlr",
            modules = modules,
            vin = vin,
            message = simPrefix + (vin?.let { "VIN $it. " } ?: "") +
                "${present.size} module(s) answered, " +
                "$faults fault" + (if (faults == 1) "" else "s") +
                " stored." + vinWarning + eraNote +
                " Read-only scan — nothing was changed on the car.",
        )
    }

    /** Clear one module's fault memory, then re-read it. Returns fresh DTCs.
     *  Ladder matches the read side: UDS clear (14 FF FF FF), then the KWP
     *  clear an older Ford-era module wants (14 FF 00) after 10 81. */
    fun clearDtcs(session: Elm327Session, reqId: Int): List<UdsDtc> {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            throw ObdException("Not on a CAN connection.", ObdException.Kind.PROTOCOL)
        }
        begin(session)
        try {
            val trace = Trace()
            try {
                request(session, reqId, 0x14, "FFFFFF", 8000)
            } catch (_: Exception) {
                openKwpSession(session, reqId, trace)
                request(session, reqId, 0x14, "FF00", 8000)
            }
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
     * Dealer live "extras": one read-only pass over the JLR-specific values
     * (JlrPids) — the Old Jaguar Group oil temp/level/volume, boost, charge-air
     * temp, ZF gearbox temp and e-diff temp — with a curated generic Mode-01
     * fallback for the DoIP-era / gateway-filtered cars. Safe: reads only.
     * Mirror of backend/profiles/jlr/pids.py.
     */
    fun readEnhanced(
        session: Elm327Session,
        onProgress: (String) -> Unit = {},
    ): List<EnhancedReading> {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            throw ObdException(
                "Not on the 11-bit/500k CAN a JLR car's live values use.",
                ObdException.Kind.PROTOCOL
            )
        }
        val out = mutableListOf<EnhancedReading>()
        begin(session)
        try {
            // (a) The Old Jaguar Group DIDs on the driveline modules.
            for (def in JlrPids.JLR_DIDS) {
                onProgress("Reading ${def.name}…")
                val body = readDid(session, def.module, def.did) ?: continue
                val value = try {
                    def.decode(body)
                } catch (_: Exception) {
                    null
                } ?: continue
                out.add(EnhancedReading(def.name, value, def.unit))
            }
            if (out.isNotEmpty()) out.add(JlrPids.OJG_CAVEAT)

            // (b) Nothing JLR-specific answered (DoIP-era / gateway-filtered):
            // the curated generic Mode-01 set keeps the card honest.
            if (out.isEmpty()) {
                for (def in JlrPids.OBD_PIDS) {
                    onProgress("Reading ${def.name}…")
                    val body = readPid(session, JlrPids.ENGINE, def.pid) ?: continue
                    val value = try {
                        def.decode(body)
                    } catch (_: Exception) {
                        null
                    } ?: continue
                    out.add(EnhancedReading(def.name, value, def.unit))
                }
            }
        } finally {
            restore(session)
        }
        return out
    }

    /** One UDS RDBI read -> payload after `62 <did>`, or null. */
    private fun readDid(session: Elm327Session, reqId: Int, did: Int): ByteArray? = try {
        val payload = request(session, reqId, 0x22, "%04X".format(did), 2500)
        if (payload.size < 4 || (payload[0].toInt() and 0xFF) != 0x62) {
            null
        } else {
            payload.drop(3).toByteArray()
        }
    } catch (_: Exception) {
        null
    }

    /** One generic Mode-01 read on the engine -> payload after `41 <pid>`, or null. */
    private fun readPid(session: Elm327Session, reqId: Int, pid: Int): ByteArray? = try {
        val payload = request(session, reqId, 0x01, "%02X".format(pid), 2500)
        if (payload.size < 3 || (payload[0].toInt() and 0xFF) != 0x41 ||
            (payload[1].toInt() and 0xFF) != pid
        ) {
            null
        } else {
            payload.drop(2).toByteArray()
        }
    } catch (_: Exception) {
        null
    }
}
