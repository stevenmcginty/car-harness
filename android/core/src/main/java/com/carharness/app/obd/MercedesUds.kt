package com.carharness.app.obd

/**
 * Mercedes-Benz module diagnostics over an ELM327: UDS (ISO 14229) *and*
 * Mercedes' KWP2000-on-CAN dialect. Port of backend/obd + profiles/mercedes/scan.py.
 *
 * Scoped honestly to what a plain dongle actually reaches on a Mercedes
 * (docs/private/mercedes-research.md §1-2):
 *  - Engine 0x7E0/0x7E8 and automatic transmission 0x7E1/0x7E9, both on the
 *    standard ISO 15765-4 block (response = request + 8). VERIFIED, §2 Table A.
 *  - EVERYTHING else (ABS/ESP, SRS, SAM, EZS, cluster, Airmatic, Parktronic,
 *    climate) uses Mercedes' proprietary scheme on model-specific *low* CAN ids
 *    (~0x300, NOT 0x7E0..0x7E7) locked in encrypted CBF files behind a filtering
 *    gateway, needing a model-specific wake packet: STAR/XENTRY-only, NOT
 *    reachable over any ELM327. So this is an honest engine+gearbox deep read,
 *    NOT a VCDS/ISTA all-modules walk.
 *
 * Mercedes splits into a KWP2000-over-CAN sub-era (~2004-2014) and a
 * UDS-over-CAN sub-era (~2014-2018); the dongle setup is identical, only the
 * service bytes differ, so each module ladders UDS first then falls back to the
 * KWP dialect (10 92 session, 1A ident, 18 02 FF FF DTC read, 14 FF 00 clear) —
 * UNVERIFIED-plausible for Mercedes (standard KWP2000, not Merc-captured, §3).
 *
 * FORBIDDEN (research §2 Table B): the wake broadcasts 0x01C / 0x692 / 0x4E4
 * (payload 02 10 92 …) that bring body modules up DO exist but are NEVER sent —
 * they are bus-level writes aimed at modules we can't address anyway, outside a
 * read-only tool's remit. Engine + TCM are awake ignition-on and answer
 * point-to-point without any wake packet.
 *
 * SCOPE (research §1): ~2004-2018 for the engine+gearbox read; ~2018+ cars are
 * DoIP (Ethernet) which an ELM327 can't speak — generic OBD-II only, and the
 * scan says so rather than imply a fault.
 *
 * Read-only apart from clearDtcs(), which the UI confirm-gates.
 */
object MercedesUds {

    /** One Mercedes module: request id, its (+8) response id, code, name. */
    data class Ecu(val req: Int, val resp: Int, val code: String, val name: String)

    // The whole publicly-reliable Mercedes table over a plain dongle (research §2
    // Table A). Response = request + 8. No body-module row — those ids are
    // proprietary/STAR-only.
    val ECUS = listOf(
        Ecu(0x7E0, 0x7E8, "ME/CDI", "Engine control (ME / CDI)"),
        Ecu(0x7E1, 0x7E9, "TCM", "Transmission (722.6 / 722.9 / 9G)"),
    )

    private val RESP_FOR: Map<Int, Int> = ECUS.associate { it.req to it.resp }

    // Standard UDS identification DIDs (research §3). Modules support different
    // subsets; read best-effort and keep whatever answers as text.
    private val IDENT_DIDS = listOf(
        0xF190 to "VIN",
        0xF187 to "Part number",
        0xF18C to "ECU serial",
        0xF189 to "Software",
    )

    // KWP2000 identification records (service 0x1A) read when every UDS DID
    // refuses (a ~2004-2014 KWP module). UNVERIFIED-plausible for Mercedes.
    private val KWP_IDENT_RECORDS = listOf(
        0x87 to "Part number (KWP 1A 87)",
        0x88 to "Software (KWP 1A 88)",
        0x86 to "ECU ident (KWP 1A 86)",
        0x9B to "Coding (KWP 1A 9B)",
    )

    // UDS NRCs that mean "I might answer in a different session" — escalate once.
    private val SESSION_NRCS = setOf(0x11, 0x22, 0x7E, 0x7F)

    // reportDTCByStatusMask masks, laddered. No single public "Mercedes mask"
    // (research §3): 0xFF = all, 0x08 = confirmed, 0xAF = broad tool mask. A
    // bad/unsupported mask returns NRC 0x31/0x12 — move to the next mask on those.
    private val DTC_MASKS = listOf(0xFF, 0x08, 0xAF)
    private val MASK_RETRY_NRCS = setOf(0x12, 0x31)

    // On Mercedes the engine IS 0x7E0 — the same id owns the standard OBD slot.
    private const val ENGINE = 0x7E0

    private var target: Int? = null

    /** Mercedes engine/gearbox diagnostics need the 11-bit / 500k CAN protocol. */
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

    /** Point the ELM at one Mercedes module: TX header + RX filter + FC. */
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

    /** Modules whose KWP2000 MB session (10 92) is already open this scan. */
    private val kwpSession = mutableSetOf<Int>()

    /**
     * Open a UDS extended session (10 03) so a module that refused a read in the
     * default session will answer — the UDS-era (2014+) escalation. Read-safe:
     * the session lapses on its own after a few seconds of silence.
     */
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

    /**
     * Bring a KWP2000-over-CAN module (~2004-2014) up: MB session 10 92 first,
     * then plain 10 81 if refused (research §3). UNVERIFIED-plausible: 0x92 is the
     * manufacturer session the wake broadcasts carry, but it is sent point-to-point
     * to an already-awake engine/TCM, NEVER as a bus wake broadcast. Read-safe.
     */
    private fun openKwpSession(session: Elm327Session, reqId: Int, trace: Trace): Boolean {
        if (reqId in kwpSession) return true
        for ((service, data, label) in listOf(
            Triple(0x10, "92", "MB session 10 92"),
            Triple(0x10, "81", "KWP start 10 81"),
        )) {
            try {
                request(session, reqId, service, data, 2000)
                trace.add("$label → OK")
                kwpSession.add(reqId)
                return true
            } catch (e: UdsNegativeResponse) {
                trace.add("$label → ${e.shortText()}")
            } catch (_: Exception) {
                trace.add("$label → no answer")
            }
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
     *  reply of either kind (positive or negative) proves an ECU is there. Engine
     *  and TCM are awake ignition-on, so no wake packet is needed. */
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
     *  an emissions ECU must answer with the ignition on, even on a DoIP-era car
     *  whose manufacturer side stays quiet. On Mercedes the engine IS 0x7E0. */
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
        // Every UDS DID refused: a KWP-flavour module. Open its MB session and
        // read the classic KWP ident records instead (UNVERIFIED-plausible §3).
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
     * if the module doesn't speak UDS DTCs at all, fall back to the KWP fault read
     * (18 02 FF FF) a ~2004-2014 module answers. Mirrors the Peugeot ladder.
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
     *  per DTC (2-byte DTC + status). Adapted from the Peugeot 17 FF 00 decode. */
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
                ok = false, simulated = simulated, brand = "mercedes", modules = emptyList(),
                message = "This car is talking ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k CAN that Mercedes engine/gearbox diagnostics use. If this " +
                    "is the Mercedes, turn the ignition on and retry. A handful of 2001-2003 cars " +
                    "are K-line for emissions engine data only.",
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

        // Standing honesty for Mercedes: the body modules aren't missing — they
        // use proprietary ids behind the gateway and are STAR/XENTRY-only over any
        // dongle, so they are simply not part of this engine+gearbox scan.
        val bodyNote = " Body modules (ABS/ESP, SRS, SAM, EZS, cluster, Airmatic, climate) use " +
            "Mercedes' proprietary addressing behind the gateway and are STAR/XENTRY-only — no " +
            "OBD dongle reaches them, so they are outside this engine+gearbox scan, not faulty."

        // Nothing answered Mercedes diagnostics, but generic OBD did: a DoIP-era
        // (~2018+) car (manufacturer diag on Ethernet) or a pre-2004 car. Not a fault.
        val genericObdOk = vin != null || engineOnObd
        val doipNote = if (mfrPresent == 0 && genericObdOk) {
            " No engine or gearbox module answered Mercedes diagnostics, but generic OBD-II did. " +
                "That points to a DoIP-era (~2018+) Mercedes — its manufacturer diagnostics run over " +
                "Ethernet (STAR/XENTRY), which an OBD dongle can't speak — or a pre-2004 car that " +
                "predates OBD engine diagnostics. Generic-OBD-only territory, not a fault."
        } else ""

        return UdsScanReport(
            ok = true,
            simulated = simulated,
            brand = "mercedes",
            modules = modules,
            vin = vin,
            message = simPrefix + (vin?.let { "VIN $it. " } ?: "") +
                "${present.size} module(s) answered, " +
                "$faults fault" + (if (faults == 1) "" else "s") +
                " stored." + vinWarning + bodyNote + doipNote +
                " Read-only scan — nothing was changed on the car.",
        )
    }

    /** Clear one module's fault memory, then re-read it. Returns fresh DTCs.
     *  Ladder matches the read side: UDS clear (14 FF FF FF), then the KWP clear
     *  a ~2004-2014 module wants (14 FF 00) after the MB session. */
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
     * Dealer live "extras": one read-only pass over the Mercedes-specific values
     * (MercedesPids) — the 722.9/9G ATF oil temp on the gearbox ECU (KWP 21 30
     * then UDS 22 2130), plus a curated generic Mode-01 set on the engine. Safe:
     * reads only. Mirror of backend/profiles/mercedes/pids.py.
     */
    fun readEnhanced(
        session: Elm327Session,
        onProgress: (String) -> Unit = {},
    ): List<EnhancedReading> {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            throw ObdException(
                "Not on the 11-bit/500k CAN a Mercedes car's live values use.",
                ObdException.Kind.PROTOCOL
            )
        }
        val out = mutableListOf<EnhancedReading>()
        begin(session)
        try {
            // (a) The one verified Mercedes-specific extra: 722.9/9G ATF temp.
            onProgress("Reading ATF oil temperature…")
            val atf = readAtfTemp(session)
            if (atf != null) {
                out.add(EnhancedReading("Transmission (722.9/9G) ATF oil temp", "$atf", "°C"))
                out.add(MercedesPids.ATF_CAVEAT)
            }

            // (b) Curated generic Mode-01 set on the engine — decoded honestly.
            for (def in MercedesPids.OBD_PIDS) {
                onProgress("Reading ${def.name}…")
                val body = readPid(session, MercedesPids.ENGINE, def.pid) ?: continue
                val value = try {
                    def.decode(body)
                } catch (_: Exception) {
                    null
                } ?: continue
                out.add(EnhancedReading(def.name, value, def.unit))
            }
        } finally {
            restore(session)
        }
        return out
    }

    /** 722.9/9G ATF oil temp on 0x7E1: KWP 21 30 first, then UDS 22 2130. First
     *  data byte A - 50 = °C (research §4). Returns null if the car doesn't answer. */
    private fun readAtfTemp(session: Elm327Session): Int? {
        for ((service, data, echoLen) in listOf(
            Triple(0x21, "30", 2),
            Triple(0x22, "2130", 3),
        )) {
            try {
                val reply = request(session, MercedesPids.TRANS, service, data, 2500)
                if (reply.size > echoLen && (reply[0].toInt() and 0xFF) == service + 0x40) {
                    val temp = (reply[echoLen].toInt() and 0xFF) - 50
                    if (temp in -50..200) return temp
                }
            } catch (_: Exception) {
            }
        }
        return null
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
