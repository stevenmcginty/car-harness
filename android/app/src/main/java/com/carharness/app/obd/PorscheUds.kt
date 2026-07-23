package com.carharness.app.obd

/**
 * Porsche module diagnostics over an ELM327: UDS (ISO 14229) with a
 * KWP2000-on-CAN fallback, plus a generic-EOBD-only K-line path for the
 * earliest cars. Port target of backend/profiles/porsche/scan.py.
 *
 * Scoped honestly to what a plain VeePeak BLE dongle actually reaches on a
 * Porsche (docs/private/porsche-research-architecture.md §7 decision table +
 * porsche-research-modules.md §2-3). Porsche is three cars in one badge, so
 * there are three addressing stories and walls at both ends:
 *  - The Porsche-own CAN cars — 997/987 (2005-12, KWP service 0x21) and
 *    991.1/981/Panamera 970 (2010-16, UDS service 0x22/0x19) — answer 11-bit
 *    CAN at 500k. The engine sits on the standard OBD block, response =
 *    request + 8 (0x7E0/0x7E8); a Tiptronic/PDK gearbox is probed at
 *    0x7E1/0x7E9. Porsche never published its per-module request/response IDs,
 *    so ABS/PSM, airbag, cluster, PASM/air-suspension and body modules can NOT
 *    be addressed by a dongle — that is engine + basics, not a fault.
 *  - Cayenne 958 / Macan 95B are VAG-MLB underneath (VIN WMI WP1 = Porsche
 *    SUV) and inherit VW's addressing best-effort: powertrain request + 8,
 *    body/chassis request + 0x6A (gateway 0x710/0x77A, cluster 0x714/0x77E,
 *    AWD 0x70F/0x779, …). Porsche STGs differ, so some VAG addresses answer and
 *    much is Porsche-specific — the report treats a non-answer as an honest
 *    "Porsche-specific address" caveat, not proof the car is broken.
 *  - The 996/986 (1997-2005) have NO diagnostic CAN — KWP2000 over two K-lines.
 *    K1 (OBD pin 7) carries the engine (DME), Tiptronic and alarm and is
 *    reachable for generic EOBD; K2 (OBD pin 3) carries ABS/PSM, airbag,
 *    cluster, HVAC, seat memory — physically unreachable on the dongle (the
 *    Peugeot pins-3/8 / JLR MS-CAN wall). Enhanced Porsche K-line addresses
 *    were never published, so the K-line read is generic-EOBD-only.
 *  - Cayenne 955/957 speak VW TP2.0 (unreachable over BLE), and MY17+ cars
 *    (Panamera 971, Cayenne 9YA, 911 992, Taycan) moved manufacturer
 *    diagnostics to DoIP/Ethernet, which no ELM327 can speak. Both give generic
 *    OBD only — the scan detects the pattern and says which wall it hit. No
 *    29-bit pass in v1 (none confirmed on Porsche).
 *
 * Dialect: 981/991/970 (and the VAG-MLB SUVs) speak UDS; the older 997/987 may
 * speak KWP2000-over-CAN — each module ladders UDS first then falls back to KWP
 * (1A 90/1A 80 ident, 18 02 FF FF fault read, 14 FF 00 clear), auto-detected
 * exactly like the JLR / Mercedes profiles.
 *
 * Nothing security-related is ever addressed (immobiliser / Kessy / component
 * protection are HARD-REFUSE in the research §7 — name-only, no ids here).
 * Read-only apart from clearDtcs(), which the UI confirm-gates.
 */
object PorscheUds {

    /** One Porsche module: request id, its response id, code, name. */
    data class Ecu(val req: Int, val resp: Int, val code: String, val name: String)

    // Porsche-own CAN cars: the engine is the one VERIFIED row (standard OBD
    // block, response = request + 8). A Tiptronic/PDK gearbox is a standard-block
    // guess — probed, quiet timeout if absent (research §2b/§10a).
    private val BASE_ECUS = listOf(
        Ecu(0x7E0, 0x7E8, "DME", "Engine control (DME)"),
        Ecu(0x7E1, 0x7E9, "TCM", "Transmission (Tiptronic / PDK)"),
    )

    // VAG-MLB inherited addresses, added best-effort ONLY on Cayenne 958 / Macan
    // (VIN WMI WP1). Body/chassis modules answer on request + 0x6A. Every row is
    // a candidate to verify — Porsche STGs differ (research §3c). Security
    // modules (immobiliser/Kessy) are deliberately NOT listed (§7).
    private val VAG_ECUS = listOf(
        Ecu(0x710, 0x77A, "GW", "Gateway (VAG-shared)"),
        Ecu(0x714, 0x77E, "KOMBI", "Instrument cluster (VAG-shared)"),
        Ecu(0x713, 0x77D, "PSM", "ABS / PSM (VAG-shared)"),
        Ecu(0x715, 0x77F, "SRS", "Airbag / SRS (VAG-shared)"),
        Ecu(0x712, 0x77C, "EPS", "Steering (VAG-shared)"),
        Ecu(0x70F, 0x779, "AWD", "AWD coupling (VAG-shared)"),
        Ecu(0x752, 0x7BC, "EPB", "Parking brake (VAG-shared)"),
    )

    // Every response id we might aim at, for aim()'s explicit +8 / +0x6A filter.
    private val RESP_FOR: Map<Int, Int> = (BASE_ECUS + VAG_ECUS).associate { it.req to it.resp }

    // Standard UDS identification DIDs (research §4). Modules support different
    // subsets; read best-effort and keep whatever answers as text.
    private val IDENT_DIDS = listOf(
        0xF190 to "VIN",
        0xF187 to "Porsche part number",
        0xF18C to "ECU serial",
        0xF189 to "Software",
        0xF191 to "Hardware",
        0xF197 to "System name",
    )

    // KWP2000 identification records (service 0x1A) read when every UDS DID
    // refuses (an older 997/987 KWP-over-CAN module). 1A 90 = VIN/ECU ident,
    // 1A 80 = ECU ident (research §4).
    private val KWP_IDENT_RECORDS = listOf(
        0x90 to "VIN / ECU ident (KWP 1A 90)",
        0x80 to "ECU ident (KWP 1A 80)",
    )

    // UDS NRCs that mean "I might answer in a different session" — escalate once.
    private val SESSION_NRCS = setOf(0x11, 0x22, 0x7E, 0x7F)

    // reportDTCByStatusMask masks, laddered. No public "Porsche mask" (research
    // §6): 0xFF = all, 0x08 = confirmed, 0xAF = broad tool mask.
    private val DTC_MASKS = listOf(0xFF, 0x08, 0xAF)
    private val MASK_RETRY_NRCS = setOf(0x12, 0x31)

    // On Porsche the engine IS 0x7E0 — the same id owns the standard OBD slot.
    private const val ENGINE = 0x7E0

    private var target: Int? = null

    /** WMI WP1 = Porsche SUV (Cayenne / Macan) — the MLB cars that inherit the
     *  VAG addressing. WP0 = Porsche sports cars (Porsche-own, no VAG reuse). */
    private fun isVagMlbVin(vin: String?): Boolean =
        vin != null && vin.length >= 3 && vin.uppercase().startsWith("WP1")

    /** Porsche module diagnostics need the 11-bit / 500k CAN protocol. */
    fun protocolSupported(session: Elm327Session): Boolean = session.isCan

    // ------------------------------------------------------------- transport

    private fun begin(session: Elm327Session) {
        session.at("ATAT0")   // adaptive timing off: we manage the timeout
        session.at("ATST64")  // ~410ms silence window — plenty for a live module
        target = null
        udsSession.clear()
        kwpSession.clear()
    }

    /** Point the ELM at one Porsche module: TX header + RX filter + FC. */
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

    /** Bring an older KWP2000-over-CAN module (997/987) up with the plain KWP
     *  start (10 81). Standard KWP2000 — read-safe. */
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
     *  TP2.0 car whose manufacturer side stays quiet. */
    private fun engineAnswersStandard(session: Elm327Session): Boolean = try {
        request(session, ENGINE, 0x01, "00", 4000)
        true
    } catch (_: UdsNegativeResponse) {
        true
    } catch (_: Exception) {
        false
    }

    /** Generic engine presence without CAN addressing (K-line path): Mode 01
     *  PID 00 sent as a plain OBD query the ELM transports on any protocol. */
    private fun engineAnswersGeneric(session: Elm327Session): Boolean = try {
        session.query(0x01, 0x00, 4000)
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
     * request that works on any ~2001+ car regardless of what UDS/KWP does next
     * (and on a 996/986 K1 if the car supports EOBD). Must run BEFORE aim() has
     * narrowed the receive filter to one module.
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
        // Every UDS DID refused: a KWP-flavour 997/987 module. Open its session
        // and read the classic KWP ident records instead.
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
     * read (18 02 FF FF) an older 997/987 module answers.
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
     *  per DTC. Adapted from the JLR / Mercedes / Peugeot decode. */
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

        // The car's identity first, while the receive filter is still open:
        // Mode 09 VIN works on any ~2001+ car (CAN or a 996/986 K1 with EOBD).
        onProgress("Reading VIN…")
        val obdVin = if (simulated) null else vinFromObd(session)

        // 996/986 have no diagnostic CAN — the ELM auto-negotiated K-line. There
        // is no module walk to do; the engine on K1 (pin 7) gives generic EOBD
        // only, and the K2 body bus on pin 3 is physically out of reach.
        if (!protocolSupported(session)) {
            return scanKline(session, simulated, obdVin)
        }

        val scanEcus = BASE_ECUS + (if (isVagMlbVin(obdVin)) VAG_ECUS else emptyList())
        val modules = mutableListOf<UdsModuleResult>()
        var mfrPresent = 0        // modules that answered a manufacturer probe
        var engineOnObd = false
        begin(session)
        try {
            for (ecu in scanEcus) {
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
        val vagMlb = isVagMlbVin(vin)

        val eraNote = if (mfrPresent > 0 && vagMlb) {
            " Cayenne / Macan (VAG-MLB). This car shares Audi machinery, so some VAG-standard " +
                "modules answer on the shared addresses and much else is Porsche-specific — a " +
                "non-answer above is a 'Porsche-specific address,' not proof the module is " +
                "absent. Full-depth work needs PIWIS or an address-aware handheld."
        } else if (mfrPresent > 0) {
            " Porsche-own CAN era (997/987, or 991.1/981/Panamera 970). This engine + basics " +
                "read is the honest dongle ceiling: Porsche never published its per-module CAN " +
                "IDs, so ABS/PSM, airbag, cluster, PASM/air-suspension and body modules can't be " +
                "addressed by a dongle — not faults, just out of reach. Camshaft deviation, " +
                "over-rev history and electronic oil level are Porsche measuring values with no " +
                "public DID. Full-depth work needs PIWIS or Durametric."
        } else if (engineOnObd || vin != null) {
            " The car answers generic OBD but no Porsche module answered diagnostics. That is the " +
                "signature of an MY17+ DoIP car (Panamera 971, Cayenne 9YA, 911 992, Taycan — " +
                "manufacturer diagnostics moved to Ethernet, which no OBD dongle can speak) or of " +
                "the Cayenne 955/957 (a VW Touareg underneath, VW TP2.0 that can't be held over " +
                "BLE). Generic-OBD-only territory, not a fault. The Service tab's dash-procedure " +
                "resets still work on these cars."
        } else {
            " No Porsche module and no engine answered. If this is the Porsche, turn the ignition " +
                "ON and retry. The oldest 996/986 talk K-line (try again — the dongle " +
                "auto-negotiates it), and MY17+/Taycan give generic OBD only."
        }

        return UdsScanReport(
            ok = true,
            simulated = simulated,
            brand = "porsche",
            modules = modules,
            vin = vin,
            message = simPrefix + (vin?.let { "VIN $it. " } ?: "") +
                "${present.size} module(s) answered, " +
                "$faults fault" + (if (faults == 1) "" else "s") +
                " stored." + vinWarning + eraNote +
                " Read-only scan — nothing was changed on the car.",
        )
    }

    /**
     * 996/986 K-line path: generic EOBD on the engine (DME) at K1 (pin 7) only.
     * No CAN addressing, no module walk — the K2 body bus on pin 3 is physically
     * unreachable on the dongle, and enhanced Porsche K-line addresses were never
     * published. Honest single-module report.
     */
    private fun scanKline(
        session: Elm327Session,
        simulated: Boolean,
        obdVin: String?,
    ): UdsScanReport {
        val trace = Trace()
        trace.add("K-line: generic EOBD on K1 (pin 7)")
        val enginePresent = engineAnswersGeneric(session) || obdVin != null
        val dtcs = if (enginePresent) {
            try {
                obdStoredCodes(session)
            } catch (_: Exception) {
                emptyList()
            }
        } else {
            emptyList()
        }
        val ident = obdVin?.let { listOf("VIN" to it) } ?: emptyList()
        val modules = listOf(
            UdsModuleResult(
                ENGINE, "DME", "Engine (DME) — K1 pin 7, generic EOBD",
                enginePresent, ident, dtcs, null, trace.lines.toList()
            )
        )
        val faults = dtcs.size
        val simPrefix = if (simulated) "SIMULATED — no real car connected. " else ""
        val note = if (enginePresent) {
            "This 996/986-era Porsche talks K-line, not CAN. Reachable here is the engine (DME) " +
                "on the first K-line (K1, OBD pin 7) — generic EOBD only; enhanced Porsche K-line " +
                "addresses (Tiptronic, alarm) were never published. Every other system — ABS/PSM, " +
                "airbag, instrument cluster, HVAC, seat memory — sits on the SECOND K-line (K2) at " +
                "OBD pin 3, which this dongle can't reach: it needs a pin-swap adapter. The Service " +
                "tab's dash-procedure resets still work."
        } else {
            "This car answered on K-line but the engine gave no generic EOBD data — the earliest " +
                "996/986 can pre-date full EOBD, or the ignition is off. Turn the ignition ON and " +
                "retry. K2 body modules on OBD pin 3 are unreachable on this dongle regardless."
        }
        return UdsScanReport(
            ok = true,
            simulated = simulated,
            brand = "porsche",
            modules = modules,
            vin = obdVin,
            message = simPrefix + (obdVin?.let { "VIN $it. " } ?: "") +
                "${if (enginePresent) 1 else 0} module answered, " +
                "$faults fault" + (if (faults == 1) "" else "s") + " stored. " + note +
                " Read-only scan — nothing was changed on the car.",
        )
    }

    /** Clear one module's fault memory, then re-read it. Returns fresh DTCs.
     *  CAN: UDS clear (14 FF FF FF) then the KWP clear (14 FF 00) after 10 81.
     *  K-line: generic OBD Mode 04. */
    fun clearDtcs(session: Elm327Session, reqId: Int): List<UdsDtc> {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            // 996/986 K-line: only the generic Mode 04 clear is available.
            try {
                session.query(0x04, null, 10000)
            } catch (e: ObdException) {
                if (e.kind != ObdException.Kind.NO_DATA) throw e
            }
            return try {
                obdStoredCodes(session)
            } catch (_: Exception) {
                emptyList()
            }
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
     * Dealer live "extras": one read-only pass over the Porsche-specific values
     * (PorschePids) — the 997.2 KWP `21 32` engine oil temperature — with a
     * curated generic Mode-01 fallback for the DoIP-era / K-line cars. Safe:
     * reads only. Mirror of backend/profiles/porsche/pids.py.
     */
    fun readEnhanced(
        session: Elm327Session,
        onProgress: (String) -> Unit = {},
    ): List<EnhancedReading> {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            throw ObdException(
                "Not on the 11-bit/500k CAN a Porsche's live values use — a 996/986 on K-line has " +
                    "no Porsche-specific extras; the generic dashboard on the Car tab still works.",
                ObdException.Kind.PROTOCOL
            )
        }
        val out = mutableListOf<EnhancedReading>()
        begin(session)
        try {
            // (a) The Porsche KWP 0x21 local-identifier reads on the DME.
            for (def in PorschePids.KWP_PIDS) {
                onProgress("Reading ${def.name}…")
                val body = readKwpLocal(session, def.module, def.lid) ?: continue
                val value = try {
                    def.decode(body)
                } catch (_: Exception) {
                    null
                } ?: continue
                out.add(EnhancedReading(def.name, value, def.unit))
            }
            if (out.isNotEmpty()) out.add(PorschePids.SINGLE_SOURCE_CAVEAT)

            // (b) Nothing Porsche-specific answered (UDS-era / DoIP-era / K-line):
            // the curated generic Mode-01 set keeps the card honest.
            if (out.isEmpty()) {
                for (def in PorschePids.OBD_PIDS) {
                    onProgress("Reading ${def.name}…")
                    val body = readPid(session, PorschePids.ENGINE, def.pid) ?: continue
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

    /** One Porsche KWP `21 <lid>` read -> payload after `61 <lid>`, or null. */
    private fun readKwpLocal(session: Elm327Session, reqId: Int, lid: Int): ByteArray? = try {
        val payload = request(session, reqId, 0x21, "%02X".format(lid), 2500)
        if (payload.size < 3 || (payload[0].toInt() and 0xFF) != 0x61 ||
            (payload[1].toInt() and 0xFF) != lid
        ) {
            null
        } else {
            payload.drop(2).toByteArray()
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
