package com.carharness.app.obd

/**
 * Fiat / Alfa Romeo (FCA / Stellantis) module diagnostics over an ELM327 — UDS
 * (ISO 14229) on 29-bit extended CAN. Port of backend/obd + profiles/fiat/scan.py.
 *
 * FCA UDS addresses ECUs with 29-bit CAN at 500k (docs/private/fiat-alfa-research.md
 * §2a/§3a): request 18DAxxF1 (xx = destination ECU, F1 = tester), response
 * 18DAF1xx, functional broadcast 18DB33F1. The 29-bit era needs the ELM on ATSP7,
 * so — unlike the 11-bit brands — this object switches the protocol itself and
 * restores it afterwards. The AT recipe (proven by BmwUds driving ATCEA from
 * Kotlin): ATCP 18 sets the priority byte so ATSH need only carry the low three
 * bytes (DAxxF1); a clone that rejects ATCP ("?") falls back to the full 8-digit
 * ATSH 18DAxxF1.
 *
 * Two tiers, scoped honestly to what a plain dongle reaches (research §1):
 *  - TIER 1 (the target): UDS-era FCA (Alfa Giulia/Stelvio, Fiat 500X/Tipo/124,
 *    Jeep Renegade, ~2014+). Walks [ECUS] on 29-bit. The 2018+ Security Gateway
 *    blocks CLEARING/actuations, NOT reads (§1c), so a read-only scan works fully
 *    even on an SGW car.
 *  - TIER 2 (fallback): CAN-era Fiat (~2005-2016). If nothing answers on 29-bit,
 *    drop to 11-bit (ATSP6) and read the engine on 0x7E0; the message says the
 *    body computer/cluster/airbag need a MultiECUScan B-CAN pin adapter (§1b). If
 *    even the engine is silent, the message names the K-line era (§1a) as
 *    unsupported on this dongle.
 *
 * HARD GUARD (research §7): TPMS at 0xC7 (18DAC7F1) is NEVER addressed — polling
 * it can engage the immobilizer and stall the car. There is no 0xC7 row, and
 * clearDtcs() refuses it.
 *
 * Read-only apart from clearDtcs(), which the UI confirm-gates.
 */
object FiatUds {

    /** One FCA module: one-byte address, hex code (doubles as display), name. */
    data class Ecu(val addr: Int, val code: String, val name: String)

    // The five VERIFIED FCA modules (research §2a). Address is the destination
    // byte (request 18DA<addr>F1). The unverified FCA modules (IPC/ORC/EPS/HVAC/
    // radio) are deliberately NOT hard-coded — research §2 says probe-sweep them
    // on a real car first. TPMS 0xC7 is deliberately absent (immobilizer trap).
    val ECUS = listOf(
        Ecu(0x10, "10", "Engine control (ECM/PCM)"),
        Ecu(0x18, "18", "Transmission (TCM)"),
        Ecu(0x28, "28", "ABS / ESC (+ EPB)"),      // Continental MK C1; hosts the EPB
        Ecu(0x30, "30", "Steering column (SCCM)"),
        Ecu(0x40, "40", "Body Computer (BCM)"),
    )

    // TPMS immobilizer address — referenced only to refuse it (research §7).
    const val TPMS_ADDR = 0xC7

    // Standard UDS identification DIDs (research §3a). Modules support different
    // subsets; read best-effort and keep whatever answers as text.
    private val IDENT_DIDS = listOf(
        0xF190 to "VIN",
        0xF18C to "ECU serial",
        0xF191 to "Hardware",
        0xF195 to "Software",
        0xF1A0 to "System name",
    )

    // UDS NRCs that mean "I might answer in a different session" — escalate once.
    private val SESSION_NRCS = setOf(0x11, 0x22, 0x7E, 0x7F)

    // reportDTCByStatusMask masks, laddered. No single public "FCA mask" (research
    // §3a): 0xFF = all, 0x08 = confirmed, 0xAF = broad tool mask. A bad/unsupported
    // mask returns NRC 0x31/0x12 — move to the next mask on those, like VW/Mercedes.
    private val DTC_MASKS = listOf(0xFF, 0x08, 0xAF)
    private val MASK_RETRY_NRCS = setOf(0x12, 0x31)

    // The engine on the standard OBD block for the CAN-era (tier-2) fallback.
    private const val ENGINE_OBD = 0x7E0

    private var target: Int? = null
    private var priority = true   // ATCP accepted -> 3-byte ATSH; else full 8-byte
    private val sessionOpen = mutableSetOf<Int>()

    /** FCA generic OBD is 11-bit/500k; the module scan then switches to 29-bit. */
    fun protocolSupported(session: Elm327Session): Boolean =
        session.protocolNum == 6 || session.protocolNum == null

    // ------------------------------------------------------------- transport

    private fun begin(session: Elm327Session) {
        session.at("ATAT0")   // adaptive timing off: we manage the timeout
        session.at("ATST64")  // ~410ms silence window; DA10F1 reads are slow
        val reply = session.at("ATCP18")   // 29-bit CAN priority byte (research §3a)
        priority = !reply.contains("?")
        target = null
        sessionOpen.clear()
    }

    /**
     * Point the ELM at one module. For an FCA one-byte address (<= 0xFF) this is
     * the 29-bit 18DAxxF1 / 18DAF1xx pair (research §3a); for the tier-2 engine
     * (0x7E0) it is the standard 11-bit pair. Flow control 30 00 00 =
     * clear-to-send, no block-size limit, no gap.
     */
    private fun aim(session: Elm327Session, dst: Int) {
        if (target == dst) return
        if (dst <= 0xFF) {
            val txLow = "DA%02XF1".format(dst)
            val txFull = "18DA%02XF1".format(dst)
            session.at("ATSH" + if (priority) txLow else txFull)
            session.at("ATCRA18DAF1%02X".format(dst))
            session.at("ATFCSH$txFull")
        } else {
            session.at("ATSH%03X".format(dst))
            session.at("ATCRA%03X".format(dst + 8))
            session.at("ATFCSH%03X".format(dst))
        }
        session.at("ATFCSD300000")
        session.at("ATFCSM1")
        target = dst
    }

    /** Undo the per-module receive filter/flow control/timing and set the 29-bit
     *  functional header. ATSP is restored separately by restoreProtocol(). */
    private fun restore(session: Elm327Session) {
        for (cmd in listOf("ATCRA", "ATSH18DB33F1", "ATFCSM0", "ATAT1", "ATST32")) {
            try {
                session.at(cmd)
            } catch (_: Exception) {
            }
        }
        target = null
        sessionOpen.clear()
    }

    /** Hand the ELM back on the discovered protocol + 11-bit functional header,
     *  so the generic OBD stack (which shares this session) still works. */
    private fun restoreProtocol(session: Elm327Session, discovered: Int?) {
        try {
            session.at("ATSP" + (discovered?.let { "%X".format(it) } ?: "0"))
            session.at("ATSH7DF")
        } catch (_: Exception) {
        }
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
     * default session will answer. Read-safe: the session lapses on its own.
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

    /** True if anything UDS-shaped lives at this address. Some ECUs never answer a
     *  naked TesterPresent but do answer an ident read — try both. A reply of
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

    /** Tier-2 engine probe on the standard OBD slot: Mode 01 PID 00 is the one
     *  request an emissions ECU must answer with the ignition on (CAN-era Fiat). */
    private fun engineAnswersStandard(session: Elm327Session): Boolean = try {
        request(session, ENGINE_OBD, 0x01, "00", 4000)
        true
    } catch (_: UdsNegativeResponse) {
        true
    } catch (_: Exception) {
        false
    }

    /** Generic OBD Mode 03 stored codes as UdsDtc rows (tier-2 engine fallback). */
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
     * The car's VIN over plain OBD Mode 09 PID 02, broadcast — the one VIN request
     * that works on any ~2001+ FCA car. Must run BEFORE aim() narrows the filter.
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
                if (payload.size < 4 || (payload[0].toInt() and 0xFF) != 0x62) continue
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
     * open the UDS extended session (10 03) once and retry — some FCA modules only
     * answer 19 02 in an active session. Response-pending 7F..78 is handled by
     * request().
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
                ok = false, simulated = simulated, brand = "fiat", modules = emptyList(),
                message = "This car is talking ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k CAN that Fiat / Alfa diagnostics use. If this is the FCA " +
                    "car, turn the ignition on and retry. Older Alfa 147/156/GT and Punto mk2 are " +
                    "K-line and aren't supported on this dongle.",
            )
        }
        val discovered = session.protocolNum

        // The car's identity first, while the receive filter is still open and
        // before any ATSP switch: Mode 09 VIN works on any ~2001+ FCA car.
        onProgress("Reading VIN…")
        val obdVin = if (simulated) null else vinFromObd(session)

        val modules = mutableListOf<UdsModuleResult>()
        var mfrPresent = 0        // FCA addresses that answered 29-bit UDS
        var engineOnObd = false

        // TIER 1: 29-bit UDS-era walk on ATSP7.
        session.at("ATSP7")
        session.at("ATCAF1")
        try {
            begin(session)
            try {
                for (ecu in ECUS) {
                    onProgress("Probing ${ecu.code} — ${ecu.name}…")
                    val trace = Trace()
                    if (!testerPresent(session, ecu.addr, trace)) {
                        modules.add(
                            UdsModuleResult(ecu.addr, ecu.code, ecu.name, false,
                                emptyList(), emptyList(), null, trace.lines.toList())
                        )
                        continue
                    }
                    mfrPresent++
                    var ident: List<Pair<String, String>> = emptyList()
                    var dtcs: List<UdsDtc> = emptyList()
                    var error: String? = null
                    try {
                        ident = readIdent(session, ecu.addr, trace)
                        dtcs = readDtcs(session, ecu.addr, trace)
                    } catch (e: Exception) {
                        if (e !is UdsNegativeResponse && e !is ObdException) throw e
                        error = e.message
                    }
                    modules.add(
                        UdsModuleResult(ecu.addr, ecu.code, ecu.name, true,
                            ident, dtcs, error, trace.lines.toList())
                    )
                }
            } finally {
                restore(session)
            }

            // TIER 2: nothing on 29-bit -> the 11-bit engine (CAN-era Fiat).
            if (mfrPresent == 0) {
                session.at("ATSP6")
                onProgress("Probing engine on the standard OBD channel…")
                val trace = Trace()
                begin(session)
                try {
                    if (engineAnswersStandard(session)) {
                        engineOnObd = true
                        trace.add("answered on standard OBD 7E0 (01 00)")
                        var dtcs: List<UdsDtc> = emptyList()
                        var error: String? = null
                        try {
                            dtcs = readDtcs(session, ENGINE_OBD, trace)
                        } catch (e: Exception) {
                            if (e !is UdsNegativeResponse && e !is ObdException) throw e
                            try {
                                dtcs = obdStoredCodes(session)
                                trace.add("fell back to OBD Mode 03")
                            } catch (_: Exception) {
                                error = e.message
                            }
                        }
                        modules.add(
                            UdsModuleResult(ENGINE_OBD, "10", "Engine control (generic OBD)",
                                true, emptyList(), dtcs, error, trace.lines.toList())
                        )
                    } else {
                        trace.add("01 00 → silence")
                        modules.add(
                            UdsModuleResult(ENGINE_OBD, "10", "Engine control (generic OBD)",
                                false, emptyList(), emptyList(), null, trace.lines.toList())
                        )
                    }
                } finally {
                    restore(session)
                }
            }
        } finally {
            restoreProtocol(session, discovered)
        }

        val present = modules.filter { it.present }
        val faults = present.sumOf { it.dtcs.size }
        val simPrefix = if (simulated) "SIMULATED — no real car connected. " else ""

        val (moduleVin, vinWarning) = VinCheck.consensus(modules)
        val vin = moduleVin ?: obdVin

        val eraNote = when {
            mfrPresent > 0 ->
                " UDS-era FCA (Giulia/Stelvio/500X/Tipo/124/Renegade, ~2014+). Reads work even on a " +
                    "2018+ Security-Gateway car — the SGW blocks only clearing and actuations, not reads."
            engineOnObd ->
                " The engine answered generic OBD but no module answered FCA 29-bit diagnostics: this " +
                    "looks like a CAN-era Fiat (~2005-2016: Grande Punto, 500, Panda, MiTo, Giulietta). " +
                    "Engine + ABS are reachable on the bare dongle, but the body computer, cluster and " +
                    "airbag sit on a low-speed B-CAN not wired to the OBD port — they need a " +
                    "MultiECUScan-style B-CAN pin adapter (UK: Gendan)."
            else ->
                " No FCA module and no engine answered. If this is the Fiat / Alfa, turn the ignition " +
                    "ON and retry. Older Alfa 147/156/GT and Punto mk2 are K-line and can't be " +
                    "diagnosed on this dongle."
        }

        return UdsScanReport(
            ok = true,
            simulated = simulated,
            brand = "fiat",
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
     *  UDS 14 FF FF FF over 29-bit. TPMS (0xC7) is refused (immobilizer trap). */
    fun clearDtcs(session: Elm327Session, reqId: Int): List<UdsDtc> {
        if (reqId == TPMS_ADDR) {
            throw ObdException(
                "TPMS (0xC7) is never addressed — polling it can engage the immobilizer " +
                    "and stall the car.",
                ObdException.Kind.PROTOCOL
            )
        }
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            throw ObdException("Not on a CAN connection.", ObdException.Kind.PROTOCOL)
        }
        val discovered = session.protocolNum
        session.at("ATSP7")
        session.at("ATCAF1")
        begin(session)
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
            restoreProtocol(session, discovered)
        }
    }

    /**
     * Dealer live "extras": one read-only pass over the FCA-specific values
     * (FiatPids) on 29-bit CAN — the danardi Giulia/Stelvio DPF/engine/gearbox
     * DIDs, one at a time (DA10F1 is slow), with a caveat that the scaling is a
     * single on-car capture. A CAN-era Fiat falls back to a curated generic
     * Mode-01 set. TPMS is never polled. Mirror of profiles/fiat/pids.py.
     */
    fun readEnhanced(
        session: Elm327Session,
        onProgress: (String) -> Unit = {},
    ): List<EnhancedReading> {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            throw ObdException(
                "Not on the 11-bit/500k CAN a Fiat / Alfa car's live values use.",
                ObdException.Kind.PROTOCOL
            )
        }
        val discovered = session.protocolNum
        val out = mutableListOf<EnhancedReading>()

        // TIER 1: the UDS-era danardi DIDs on 29-bit CAN.
        session.at("ATSP7")
        session.at("ATCAF1")
        try {
            begin(session)
            try {
                for (def in FiatPids.GIULIA_DIDS) {
                    onProgress("Reading ${def.name}…")
                    val body = readDid(session, def.addr, def.did) ?: continue
                    val value = try {
                        def.decode(body)
                    } catch (_: Exception) {
                        null
                    } ?: continue
                    out.add(EnhancedReading(def.name, value, def.unit))
                }
                if (out.isNotEmpty()) out.add(FiatPids.DANARDI_CAVEAT)
            } finally {
                restore(session)
            }

            // TIER 2: nothing on 29-bit -> curated generic Mode-01 on the engine.
            if (out.isEmpty()) {
                session.at("ATSP6")
                begin(session)
                try {
                    for (def in FiatPids.OBD_PIDS) {
                        onProgress("Reading ${def.name}…")
                        val body = readPid(session, FiatPids.ENGINE_OBD, def.pid) ?: continue
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
            }
        } finally {
            restoreProtocol(session, discovered)
        }
        return out
    }

    /** One 29-bit RDBI read -> payload after `62 <did>`, or null. */
    private fun readDid(session: Elm327Session, addr: Int, did: Int): ByteArray? = try {
        val payload = request(session, addr, 0x22, "%04X".format(did), 3000)
        if (payload.size < 4 || (payload[0].toInt() and 0xFF) != 0x62) null
        else payload.drop(3).toByteArray()
    } catch (_: Exception) {
        null
    }

    /** One generic Mode-01 read -> payload after `41 <pid>`, or null. */
    private fun readPid(session: Elm327Session, header: Int, pid: Int): ByteArray? = try {
        val payload = request(session, header, 0x01, "%02X".format(pid), 2500)
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
