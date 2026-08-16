package com.carharness.app.obd

/**
 * Peugeot / PSA (Citroen, DS) module diagnostics over an ELM327: UDS
 * (ISO 14229) *and* PSA's KWP2000-on-CAN variant, on whatever bus the dongle
 * reaches. Port of backend/obd/psa_uds_elm327.py + profiles/peugeot/scan.py.
 *
 * WHAT THE FIRST FIELD TEST TAUGHT US (208-era car, VeePeak dongle):
 *  - A standard dongle sits on OBD pins 6/14 — on PSA that is the ENGINE bus.
 *    Only the powertrain modules (engine, gearbox, ABS, sometimes steering)
 *    are wired to it. The BSI, airbag, cluster, radio etc. live on the
 *    diagnostic CAN at OBD pins 3/8 and need a pin-swap adapter — no software
 *    can reach them through a plain dongle. So "no answer" from body modules
 *    is wiring, not a fault, and the report must say so.
 *  - The modules that DO answer are often PSA's KWP flavour, not pure UDS:
 *    session open is 10 C0 (not 10 03), fault read is 17 FF 00 (not 19 02),
 *    ident is 21 <zone> (not 22 <did>). The scan now ladders UDS first and
 *    falls back to KWP per module, and 19 02 leads with PSA's mask 0x09.
 *  - The car's identity must never depend on any of that: the VIN is read
 *    first over plain OBD Mode 09 PID 02 (mandatory on ~2001+ petrol /
 *    2004+ diesel), so the report can always say what car it is.
 *
 * Read-only apart from clearDtcs(), which the UI confirm-gates.
 */
object PeugeotUds {

    /** One PSA module: its request id, its (non-standard) response id, code, name. */
    data class Ecu(val req: Int, val resp: Int, val code: String, val name: String)

    // Curated PSA diagnostic table for the modules a dealer checks pre-sale.
    // Ids from the community PSA databases (ludwig-v/arduino-psa-diag
    // ECU_LIST.md, prototux/PSA-RE nodes/AEE2004.full.yml, PyPSADiag). The
    // T7-era ids (308 / 308 CC / RCZ, 2007-2013) follow PSA's node arithmetic:
    // request 0x6A0+node / response 0x680+node on the 500k IS bus (OBD pins
    // 6/14), request 0x740+node / response 0x640+node on the 125k body buses
    // behind the BSI (OBD pins 3/8). Mirror of the backend's
    // PEUGEOT_ECU_TABLE — KEEP BOTH IN SYNC.
    val ECUS = listOf(
        Ecu(0x752, 0x652, "BSI", "Body Systems Interface (gateway)"),
        Ecu(0x6A8, 0x688, "CMM", "Engine / injection"),
        Ecu(0x6A9, 0x689, "BVA", "Automatic gearbox"),
        Ecu(0x6AD, 0x68D, "ABS/ESP", "Braking / stability"),
        Ecu(0x744, 0x644, "AIRBAG", "Airbags / SRS"),
        Ecu(0x75F, 0x65F, "COMBINE", "Instrument cluster"),
        Ecu(0x767, 0x667, "BTE", "Folding roof (308 CC) — also runs the windows"),
        Ecu(0x75D, 0x65D, "AAS", "Parking aid"),
        Ecu(0x6B5, 0x695, "DIRECTION", "Power steering"),
        Ecu(0x6A5, 0x685, "CAV", "Steering angle sensor (id derived from PSA-RE node map)"),
        Ecu(0x6B7, 0x697, "CORPRO", "Headlamp levelling / directional beams"),
        Ecu(0x6B3, 0x693, "FSE", "Electric parking brake"),
        Ecu(0x76D, 0x66D, "CLIM", "Climate control"),
        Ecu(0x778, 0x678, "TDCAR", "Rear climate control"),
        Ecu(0x6AF, 0x68F, "DSG", "Tyre pressure (TPMS)"),
        Ecu(0x760, 0x660, "AUTORADIO", "Radio / audio"),
        Ecu(0x771, 0x671, "CDCHG", "CD changer"),
        Ecu(0x765, 0x665, "AFFICHEUR", "Central display"),
        Ecu(0x764, 0x664, "TELEMAT", "Telematics"),
        Ecu(0x773, 0x673, "KML", "Bluetooth hands-free kit"),
        Ecu(0x6A1, 0x681, "SCR", "AdBlue / SCR (diesel)"),
        Ecu(0x6C1, 0x601, "ADDITIF", "DPF additive (diesel, newer)"),
        Ecu(0x775, 0x675, "ADDGO3", "DPF additive (diesel, older)"),
        Ecu(0x730, 0x710, "CPL", "Rain / light sensor (newer Hella UDS variant)"),
        Ecu(0x74A, 0x64A, "CDPL", "Rain / light sensor (T7-era id)"),
        Ecu(0x742, 0x642, "COM2000", "Steering-wheel controls unit"),
        Ecu(0x746, 0x646, "RHF", "RF receiver (remote key)"),
        Ecu(0x741, 0x641, "ADML", "Keyless entry / start"),
        Ecu(0x75C, 0x65C, "ALARME", "Factory alarm"),
        Ecu(0x74B, 0x64B, "PDPC", "Driver door electronics / window module"),
        Ecu(0x76E, 0x66E, "BDMC", "Driver seat memory"),
        Ecu(0x76F, 0x66F, "BDMP", "Passenger seat memory"),
        Ecu(0x768, 0x668, "TOUVRANT", "Opening sunroof"),
        Ecu(0x748, 0x648, "BSC", "Boot lid servo"),
        Ecu(0x76C, 0x66C, "BSR", "Towbar / trailer module"),
        Ecu(0x731, 0x711, "BECH", "Additional heater (diesel)"),
        Ecu(0x6B4, 0x694, "BMS", "HV battery (EV / PHEV)"),
    )

    private val RESP_FOR: Map<Int, Int> = ECUS.associate { it.req to it.resp }

    // Powertrain-bus modules: the set a standard dongle CAN reach on pins
    // 6/14 (the 500k IS bus). If nothing outside this set answers (and the
    // always-fitted BSI is silent), the car keeps its body modules on the
    // pins-3/8 diag CAN. Mirror of the backend's ENGINE_BUS — KEEP IN SYNC.
    private val ENGINE_BUS = setOf(0x6A8, 0x6A9, 0x6AD, 0x6B5, 0x6A1, 0x6A5, 0x6B7)

    private val IDENT_DIDS = listOf(
        0xF190 to "VIN",
        0xF187 to "Part number",
        0xF188 to "Software",
        0xF18C to "ECU serial",
    )

    // KWP ident zones read with service 0x21 when the 0x22 DIDs all refuse.
    // Zone 0x80 = the classic PSA "ZA" block (part number + serial — that is
    // what the RCZ field car returned); 0xB0 = the PSA-dialect VIN zone
    // (ludwig-v's KWP zone table, the counterpart of UDS F190).
    private val KWP_ZONES = listOf(
        0x80 to "Ident (zone 80)",
        0xB0 to "VIN (zone B0)",
    )

    // Where a KWP-flavour engine might hide its VIN, tried in order by
    // vinFromCar(). Nothing here is MED17-verified on-car yet (nobody
    // publishes it — DDT2000, ludwig-v's zone tables and the KWP2000 spec
    // all disagree), so we ladder. Zone 0x90 is deliberately NOT probed:
    // DDT2000 maps it to generic live data, not a VIN.
    private val KWP_VIN_ZONES = listOf(0xB0, 0x81, 0x80)

    // Standard KWP2000 ReadECUIdentification (0x1A) VIN option ids, tried
    // after the PSA zones: 0x90 = VIN (current), 0x88 = VIN (original) —
    // DaimlerChrysler KWP2000 Requirements Definition v2.2.
    private val KWP_VIN_IDENTS = listOf(0x90, 0x88)

    // UDS NRCs that mean "I might answer in a different session" — escalate once.
    private val SESSION_NRCS = setOf(0x11, 0x22, 0x7E, 0x7F)

    // reportDTCByStatusMask masks, most-PSA-likely first: 0x09 is what PSA's
    // own tooling asks for (arduino-psa-diag), then the OEM-style masks.
    private val DTC_MASKS = listOf(0x09, 0xAF, 0x0C, 0xFF)

    private const val ENGINE = 0x6A8      // CMM's PSA request id
    private const val OBD_ENGINE = 0x7E0  // standard OBD slot the engine also owns

    private var target: Int? = null

    /** Modules whose PSA/KWP diagnostic session is already open this scan. */
    private val sessionOpen = mutableSetOf<Int>()

    /** PSA module diagnostics need the 11-bit / 500k CAN protocol. */
    fun protocolSupported(session: Elm327Session): Boolean =
        session.protocolNum == 6 || session.protocolNum == null

    // ------------------------------------------------------------- transport

    private fun begin(session: Elm327Session) {
        session.at("ATAT0")   // adaptive timing off: we manage the timeout
        session.at("ATST64")  // ~410ms silence window — plenty for a live module
        target = null
        sessionOpen.clear()
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
        sessionOpen.clear()
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
     * Open a diagnostic session, PSA style. Ladder: UDS extended (10 03), then
     * PSA's KWP session (10 C0 — what Diagbox-era ECUs actually want), then the
     * bare KWP StartCommunication (81). Read-safe: session state lapses on its
     * own after a few seconds of silence and writes nothing to the car.
     */
    private fun openSession(session: Elm327Session, reqId: Int, trace: Trace): Boolean {
        if (reqId in sessionOpen) return true
        for ((service, data, label) in listOf(
            Triple(0x10, "03", "session 10 03"),
            Triple(0x10, "C0", "PSA session 10 C0"),
            Triple(0x81, "", "KWP start 81"),
        )) {
            try {
                request(session, reqId, service, data, 2000)
                trace.add("$label → OK")
                sessionOpen.add(reqId)
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

    /** True if anything diagnostic-shaped lives at this id pair. Ladder: some
     *  ECUs never answer a naked TesterPresent, some only wake for their PSA
     *  session — so probe 3E 00, then an ident read, then 10 C0. */
    private fun testerPresent(session: Elm327Session, reqId: Int, trace: Trace): Boolean {
        for ((service, data) in listOf(0x3E to "00", 0x22 to "F190", 0x10 to "C0")) {
            try {
                request(session, reqId, service, data, 3500)
                if (service == 0x10) sessionOpen.add(reqId)
                return true
            } catch (_: UdsNegativeResponse) {
                return true  // a negative response still proves an ECU is there
            } catch (_: Exception) {
            }
        }
        trace.add("3E 00 / 22 F190 / 10 C0 → silence")
        return false
    }

    /** Engine probe on the standard OBD slot (0x7E0 -> 0x7E8): Mode 01 PID 00
     *  is the one request an emissions ECU must answer with the ignition on,
     *  even on a car whose PSA-specific pair doesn't match our table. */
    private fun engineAnswersStandard(session: Elm327Session): Boolean {
        for ((service, data) in listOf(0x01 to "00", 0x22 to "F190", 0x3E to "00")) {
            try {
                request(session, OBD_ENGINE, service, data, 4000)
                return true
            } catch (_: UdsNegativeResponse) {
                return true
            } catch (_: Exception) {
            }
        }
        return false
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
     * request that works on any ~2001+ car regardless of PSA's diag quirks.
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
        vinOf(text)
    } catch (_: Exception) {
        null
    }

    /**
     * A 17-char VIN hiding in a positive reply after [skip] echo bytes.
     * ASCII filter only, never the hex fallback of printable(): a hex dump of
     * a binary payload is itself 0-9A-F characters and would false-match.
     * And never all digits: a KWP ident zone carries "part number + serial"
     * (the RCZ field car: 9663711580 003966 = 17 digits), which the bare
     * pattern happily matched. A real WMI always carries letters.
     */
    private val VIN_RE = Regex("[A-HJ-NPR-Z0-9]{17}")

    private fun vinOf(text: String): String? =
        VIN_RE.findAll(text.uppercase())
            .firstOrNull { m -> m.value.count { it.isLetter() } >= 2 }?.value

    private fun vinIn(reply: ByteArray, skip: Int): String? {
        if (reply.size <= skip) return null
        val text = reply.drop(skip)
            .map { it.toInt() and 0xFF }
            .filter { it in 32..126 }
            .map { it.toChar() }
            .joinToString("")
        return vinOf(text)
    }

    /**
     * The car's VIN, laddered across every PSA generation a dongle can meet —
     * the phone-side twin of the backend's read_vin(). Generic Mode 09 is
     * answered by ~2010+ UDS cars; the KWP-era engines (MED17-class petrol,
     * DCM diesels — the 2009-2013 308 CC among them) ignore it and only wake
     * for the KWP StartCommunication 81. Rungs, in order, all read-only and
     * failure-tolerant:
     *
     *  1. Mode 09 PID 02 broadcast (0x7DF) — any ~2010+ car.
     *  2. Mode 09 PID 02 physical (0x7E0) — some ECUs ignore functional ids.
     *  3. UDS 22 F1 90 on the engine's PSA pair (0x6A8 -> 0x688).
     *  4. PSA KWP session (10 03 / 10 C0 / 81), the VIN zones (21 B0/81/80),
     *     then the KWP2000 1A ident ids (90 current / 88 original).
     *
     * Must run with the ELM receive filter still open for rung 1 (aim()
     * narrows it for the rungs after). Returns null when every rung is silent.
     */
    fun vinFromCar(session: Elm327Session): String? {
        vinFromObd(session)?.let { return it }
        begin(session)
        try {
            try {
                val reply = request(session, OBD_ENGINE, 0x09, "02", 3000)
                vinIn(reply, 3)?.let { return it }      // 49 02 <count> …
            } catch (_: Exception) {
            }
            try {
                val reply = request(session, ENGINE, 0x22, "F190", 2500)
                vinIn(reply, 3)?.let { return it }      // 62 F1 90 …
            } catch (_: Exception) {
            }
            if (openSession(session, ENGINE, Trace())) {
                for (zone in KWP_VIN_ZONES) {
                    try {
                        val reply = request(session, ENGINE, 0x21, "%02X".format(zone), 2500)
                        vinIn(reply, 2)?.let { return it }  // 61 <zone> …
                    } catch (_: Exception) {
                    }
                }
                for (ident in KWP_VIN_IDENTS) {
                    try {
                        val reply = request(session, ENGINE, 0x1A, "%02X".format(ident), 2500)
                        vinIn(reply, 2)?.let { return it }  // 5A <ident> …
                    } catch (_: Exception) {
                    }
                }
            }
            return null
        } finally {
            restore(session)
        }
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
        // Every UDS DID refused: this is a KWP-flavour module. Open its PSA
        // session and read the classic ident zone instead.
        if (out.isEmpty() && refused == IDENT_DIDS.size && openSession(session, reqId, trace)) {
            for ((zone, label) in KWP_ZONES) {
                try {
                    val payload = request(session, reqId, 0x21, "%02X".format(zone), 2500)
                    if (payload.size < 2) continue
                    val value = printable(payload.drop(2))
                    if (value.isNotEmpty()) {
                        out.add(label to value)
                        trace.add("21 %02X → OK".format(zone))
                        // A 17-char VIN hiding in the zone text still counts
                        // (never all digits: that's a part number + serial).
                        vinOf(value)?.let { out.add("VIN" to it) }
                    }
                } catch (e: UdsNegativeResponse) {
                    trace.add("21 %02X → ${e.shortText()}".format(zone))
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
     * Fault memory, laddered: UDS reportDTCByStatusMask with PSA's preferred
     * mask first; on a session refusal open the PSA session and retry; if the
     * module doesn't speak 19 02 at all, fall back to the KWP fault read
     * (17 FF 00) every Diagbox-era PSA module answers.
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
                if (e.nrc == 0x12 || e.nrc == 0x31) continue
                trace.add("19 02 %02X → ${e.shortText()}".format(mask))
                throw e
            }
        }
        val data = payload ?: run {
            lastNegative?.let { trace.add("19 02 → ${it.shortText()}") }
            throw (lastNegative ?: ObdException("Fault read failed.", ObdException.Kind.NO_DATA))
        }

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

    /** KWP ReadStatusOfDTC: 17 FF 00 → 57 <n> then (hi, lo, status) per DTC. */
    private fun readDtcsKwp(session: Elm327Session, reqId: Int, trace: Trace): List<UdsDtc> {
        openSession(session, reqId, trace)
        val data = try {
            request(session, reqId, 0x17, "FF00", 6000)
        } catch (e: UdsNegativeResponse) {
            trace.add("17 FF 00 → ${e.shortText()}")
            throw e
        } catch (e: ObdException) {
            trace.add("17 FF 00 → no answer")
            throw e
        }
        trace.add("17 FF 00 → OK (KWP)")
        // 0x57 <count> <triples> is the book format; some ECUs skip the count.
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
                ok = false, simulated = simulated, brand = "peugeot", modules = emptyList(),
                message = "This car is talking ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k CAN that PSA module scanning uses. If this is the " +
                    "Peugeot, turn the ignition on and retry. Pre-2004 (VAN) and many 2004-2010 " +
                    "(KWP) cars can't be module-scanned through a plain OBD dongle.",
            )
        }

        // The car's identity first, while the receive filter is still open:
        // the vinFromCar ladder covers UDS-era cars AND the KWP-era engines
        // (the 308 CC MED17 among them) that never answer plain Mode 09.
        onProgress("Reading VIN…")
        val obdVin = if (simulated) null else vinFromCar(session)

        val modules = mutableListOf<UdsModuleResult>()
        begin(session)
        try {
            for (ecu in ECUS) {
                onProgress("Probing ${ecu.code}…")
                val trace = Trace()
                var reqId = ecu.req
                var viaStandardObd = false
                var present = testerPresent(session, reqId, trace)
                if (!present && ecu.req == ENGINE) {
                    onProgress("Probing ${ecu.code} (standard OBD channel)…")
                    present = try {
                        engineAnswersStandard(session)
                    } catch (_: Exception) {
                        false
                    }
                    if (present) {
                        reqId = OBD_ENGINE
                        viaStandardObd = true
                        trace.add("answered on standard OBD 7E0")
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
                    ident = readIdent(session, reqId, trace)
                    dtcs = readDtcs(session, reqId, trace)
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

        // Body modules silent while only powertrain-bus modules answered =
        // the dongle is on the engine bus (OBD pins 6/14) and this car keeps
        // its body diagnostics on pins 3/8. That's wiring, not car trouble.
        val bodySilent = present.isNotEmpty() &&
            present.all { it.address in ENGINE_BUS || it.address == OBD_ENGINE } &&
            modules.any { !it.present && it.address == 0x752 }
        val busNote = if (bodySilent) {
            " The body modules (BSI, airbag, cluster, radio…) aren't missing — on " +
                "Peugeot/Citroën they sit on a separate diagnostic CAN wired to OBD " +
                "pins 3 & 8, which a standard dongle can't reach. A £5-10 'PSA pin " +
                "3/8' adapter between dongle and socket brings them all in."
        } else ""

        return UdsScanReport(
            ok = true,
            simulated = simulated,
            brand = "peugeot",
            modules = modules,
            vin = vin,
            absentHint = if (bodySilent) {
                "On the pins-3/8 diagnostic CAN — reachable with a PSA pin-swap " +
                    "adapter, not over a standard dongle. Not a fault."
            } else null,
            message = simPrefix + (vin?.let { "VIN $it. " } ?: "") +
                "${present.size} module(s) answered, " +
                "$faults fault" + (if (faults == 1) "" else "s") +
                " stored." + vinWarning + busNote +
                " Read-only scan — nothing was changed on the car.",
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
                if (!openSession(session, reqId, Trace())) return null
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

    /** Clear one module's fault memory, then re-read it. Returns fresh DTCs.
     *  Ladder matches the read side: UDS clear, then the KWP clear PSA's own
     *  tooling sends (14 FF 00) after opening the PSA session. */
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
                openSession(session, reqId, trace)
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
}
