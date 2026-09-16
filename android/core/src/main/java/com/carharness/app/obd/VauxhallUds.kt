package com.carharness.app.obd

/**
 * Vauxhall / Opel (GM Global A) module diagnostics over an ELM327, on the
 * 11-bit / 500k High Speed GMLAN bus. Same walker shape as the Nissan one,
 * with the two things that are GM's own:
 *
 *  - TWO addressing conventions share the one bus. Powertrain sits in the
 *    legislated ISO 15765-4 block (ECM 0x7E0/0x7E8, TCM 0x7E1/0x7E9 —
 *    reply = request + 8). Body and chassis use GM's own block at
 *    0x24x-0x25x and answer on **request + 0x400** (BCM 0x241/0x641,
 *    ABS 0x243/0x643, cluster 0x24C/0x64C). Getting that filter wrong is the
 *    single biggest way this scan comes back empty on a healthy car, so each
 *    module carries its own response id here.
 *  - TWO DIALECTS. Global A speaks GMW3110, not UDS: fault memory is service
 *    **0xA9**, identification is **0x1A** (one-byte ids), clear is **0x04**.
 *    Some late Global A nodes also answer plain UDS. Neither is assumed —
 *    every module is read GMLAN-first then UDS, and the report records which
 *    dialect actually answered, so the result is evidence and not a guess.
 *
 * SCOPE, and two honest limits that ride on every result:
 *  - Low Speed GMLAN (single-wire 33.3 kbit/s, OBD pin 1) carries much of the
 *    comfort/body traffic and NO standard dongle touches it — the same wall
 *    the Mazda profile hits with MS-CAN.
 *  - 2019+ Vauxhalls (Corsa F, Mokka B, Astra L, Crossland, Grandland) are
 *    PSA/Stellantis, not GM. This profile finds little on them; the Peugeot
 *    one is right for those.
 *
 * Mirror of backend/profiles/vauxhall/scan.py + gmlan.py — KEEP IN SYNC.
 * Read-only apart from clearDtcs(), which the UI confirm-gates.
 */
object VauxhallUds {

    /** One Vauxhall module: request id, its response id, code, name. */
    data class Ecu(val req: Int, val resp: Int, val code: String, val name: String)

    // GM-Global-A-generic, not model-verified: no public source maps the
    // Astra K's own module list. A row that does not answer simply is not
    // fitted at that address on this model — trust the address, not the label.
    val ECUS = listOf(
        // ---- ISO 15765-4 powertrain block (response = request + 8) ---------
        Ecu(0x7E0, 0x7E8, "ECM", "Engine control module"),
        Ecu(0x7E1, 0x7E9, "TCM", "Transmission control (classic Global A address)"),
        Ecu(0x7E2, 0x7EA, "TCM2", "Transmission control / second powertrain node"),
        Ecu(0x7E3, 0x7EB, "PT3", "Third powertrain node (if fitted)"),
        // ---- GM body / chassis block (response = request + 0x400) ----------
        Ecu(0x241, 0x641, "BCM", "Body control module"),
        Ecu(0x242, 0x642, "TDM", "Theft deterrent / immobiliser"),
        Ecu(0x243, 0x643, "EBCM", "ABS / stability control"),
        Ecu(0x244, 0x644, "EHU", "Infotainment / head unit (if fitted)"),
        Ecu(0x246, 0x646, "SIC", "Serial data interface (if fitted)"),
        Ecu(0x247, 0x647, "SDM", "Airbag / restraints control"),
        Ecu(0x24C, 0x64C, "IPC", "Instrument cluster"),
        Ecu(0x251, 0x651, "HVAC", "Air conditioning / heating (if fitted)"),
        Ecu(0x258, 0x658, "RFA", "Remote locking / keyless entry (if fitted)"),
    )

    private val RESP_FOR: Map<Int, Int> = ECUS.associate { it.req to it.resp }

    /** The engine also owns the standard OBD slot. */
    private const val ENGINE = 0x7E0

    // ---------------------------------------------------- GMW3110 services
    private const val SVC_GMLAN_CLEAR = 0x04    // ClearDiagnosticInformation, no args
    private const val SVC_GMLAN_IDENT = 0x1A    // ReadDataByIdentifier, ONE-byte id
    private const val SVC_GMLAN_DTCS = 0xA9     // ReadDiagnosticInformation

    // GMW3110 negative-response codes UDS either doesn't define or names
    // differently. Looked up over UdsConst.NRC_NAMES so one call covers both.
    private val GMLAN_NRC_NAMES = mapOf(
        0x22 to "conditions not correct or request sequence error",
        0x81 to "scheduler full",
        0x83 to "voltage out of range",
        0x85 to "general programming failure",
        0x89 to "device type error",
        0x99 to "ready for download, DTC stored",
        0xE3 to "device control limits exceeded",
    )

    // GMW3110 DTC status bits — NOT ISO 14229. 0x02 is currentDTC, not
    // "failed this cycle", so UdsConst.STATUS_BITS must not be used here.
    private val GMLAN_STATUS_BITS = listOf(
        0x01 to "supported by calibration",
        0x02 to "current",
        0x04 to "not passed since cleared",
        0x08 to "failed since cleared",
        0x10 to "history",
        0x20 to "not passed this power-up",
        0x40 to "current since power-up",
        0x80 to "warning indicator",
    )

    // 0x1A identifiers asked on a scan. Only the ones whose meaning is
    // certain: guessing at the rest of GMW3110's ~95-entry table would put
    // confident wrong labels next to real bytes.
    private val GMLAN_IDENT_IDS = listOf(
        0x90 to "VIN",
        0x92 to "System supplier id",
        0x97 to "System name / engine type",
        0xB5 to "GM broadcast code",
        0xC0 to "Boot software part number",
        0xCB to "End model part number",
    )

    /** Standard UDS identification DIDs, tried on the UDS-dialect path only. */
    private val UDS_IDENT_DIDS = listOf(
        0xF190 to "VIN",
        0xF188 to "Software calibration",
        0xF187 to "Part number",
        0xF191 to "Hardware",
        0xF194 to "Software",
        0xF18C to "ECU serial",
    )

    // Status masks, laddered widest-first in each dialect. There is no single
    // published "GM mask": a module that dislikes one answers NRC 0x31 or
    // 0x12, which means "try the next one", not "this module is broken".
    private val GMLAN_DTC_MASKS = listOf(0xFF, 0x08)
    private val UDS_DTC_MASKS = listOf(0xFF, 0x08, 0xAF)
    private val MASK_RETRY_NRCS = listOf(0x12, 0x31)

    // Where gocan / GDS2 listen after A9 81 — UUDT frames, not ISO-TP.
    private const val UUDT_DTC_CAN_ID = 0x5E8

    // The ELM header/filter the driver is currently aimed at.
    private var target: Int? = null

    /** Vauxhall module diagnostics need the 11-bit / 500k CAN protocol. */
    fun protocolSupported(session: Elm327Session): Boolean =
        session.protocolNum == 6 || session.protocolNum == null

    /** Per-module debug trail — which rung of the dialect ladder broke. */
    private class Trace {
        val lines = mutableListOf<String>()
        fun add(line: String) {
            if (lines.size < 14) lines.add(line)
        }
    }

    private fun nrcName(nrc: Int): String =
        GMLAN_NRC_NAMES[nrc] ?: UdsConst.NRC_NAMES[nrc] ?: "unknown code"

    private fun UdsNegativeResponse.shortText(): String =
        "NRC %02X (%s)".format(nrc, nrcName(nrc))

    // ------------------------------------------------------------- transport

    private fun begin(session: Elm327Session) {
        session.at("ATAT0")   // adaptive timing off: we manage the timeout
        session.at("ATST64")  // ~410ms silence window — plenty for a live module
        target = null
    }

    /** Point the ELM at one module: TX header + RX filter + flow control. The
     *  filter comes from the table, because the 0x24x block replies on
     *  request + 0x400 and request + 8 would never be heard. */
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
     * Send one request to the module at [reqId] and return the positive-response
     * payload. Throws UdsNegativeResponse on 7F, ObdException(NO_DATA) when
     * nothing answers. Retries on "pending". The NRC numbering is compatible
     * between the two dialects, so one helper serves both.
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

    // ------------------------------------------------------------- presence

    /** What a probe found: the request that proved it, and the dialect that
     *  request proves — usually none. */
    private data class Presence(val provedBy: String, val dialect: String?)

    // Probes in cost order, as (service, data, label, dialect a POSITIVE reply
    // proves). Most diagnostic services exist in both dialects and so prove
    // only that something is alive: 0x3E is TesterPresent in UDS and GMW3110
    // alike, and 0x22 is ReadDataBy(Parameter)Identifier in both. Only 0x1A
    // and 0xA9 are GMW3110-only. Nothing here proves UDS — the one UDS-only
    // read service is 0x19, and spending a fault read on every address to find
    // out would be expensive. So probing establishes presence and readModule()
    // settles the dialect by which reader actually worked.
    private val PRESENCE_PROBES: List<Triple<Pair<Int, String>, String, String?>> = listOf(
        Triple(0x3E to "00", "3E 00", null),
        Triple(SVC_GMLAN_IDENT to "90", "1A 90", "gmlan"),
        Triple(0x22 to "F190", "22 F190", null),
        Triple(SVC_GMLAN_DTCS to "82FF", "A9 82 FF", "gmlan"),
    )

    /**
     * Is anything alive here? A negative response counts as presence —
     * something decoded the frame and chose to refuse it — but NOT as dialect
     * proof, since a module refuses an unknown service in whatever dialect it
     * does speak.
     */
    private fun probe(session: Elm327Session, reqId: Int, trace: Trace): Presence? {
        var presence: String? = null
        for ((call, label, proves) in PRESENCE_PROBES) {
            val (service, data) = call
            try {
                request(session, reqId, service, data, 3500)
            } catch (_: UdsNegativeResponse) {
                if (presence == null) presence = label
                continue
            } catch (_: Exception) {
                continue
            }
            if (presence == null) presence = label
            if (proves != null) {
                trace.add("$label → positive, ${proves.uppercase()} dialect proven")
                return Presence(label, proves)
            }
        }
        if (presence != null) {
            trace.add("$presence → answered (dialect not proven by probing)")
            return Presence(presence, null)
        }
        trace.add("3E 00 / 1A 90 / 22 F190 / A9 82 → silence")
        return null
    }

    // ------------------------------------------------------------ fault rows

    /** Text if it reads as text, otherwise hex. Same rule as the VAG scan. */
    private fun printable(body: List<Byte>): String {
        val text = body.map { it.toInt() and 0xFF }.filter { it in 32..126 }
            .map { it.toChar() }.joinToString("").trim()
        if (text.isNotEmpty() && text.length >= (body.size + 1) / 2) return text
        return body.joinToString("") { "%02X".format(it.toInt() and 0xFF) }
    }

    /**
     * A GM fault as a GM tester shows it: code AND failure type together.
     * P0716-71 and P0716-00 are different entries in GM service information,
     * so the displayed code carries both. The three-byte-to-J2012 conversion
     * is identical to UDS, and unlike VAG, GM powertrain codes do use the
     * generic SAE meanings — so the description lookup uses the bare code.
     */
    private fun dtcOf(
        b0: Int,
        b1: Int,
        failureType: Int,
        status: Int,
        dialect: String,
    ): UdsDtc {
        val base = Dtc.decodePair(b0, b1)
        val bits = if (dialect == "gmlan") GMLAN_STATUS_BITS else UdsConst.STATUS_BITS
        return UdsDtc(
            code = "%s-%02X".format(base, failureType),
            hex = "%02X%02X%02X".format(b0, b1, failureType),
            status = "%02X".format(status),
            flags = bits.filter { (bit, _) -> status and bit != 0 }.map { it.second } +
                listOf(if (dialect == "gmlan") "read over GMW3110" else "read over UDS"),
            description = Dtc.describe(base),
        )
    }

    /** Split a GMLAN DTC payload into 4-byte [hi][lo][failureType][status]. */
    private fun recordsFrom(data: ByteArray, offset: Int): List<IntArray> {
        val out = mutableListOf<IntArray>()
        var i = offset
        while (i + 3 < data.size) {
            val b0 = data[i].toInt() and 0xFF
            val b1 = data[i + 1].toInt() and 0xFF
            val ft = data[i + 2].toInt() and 0xFF
            val st = data[i + 3].toInt() and 0xFF
            i += 4
            if (b0 == 0 && b1 == 0 && ft == 0) continue
            out.add(intArrayOf(b0, b1, ft, st))
        }
        return out
    }

    /** GMW3110 fault memory: A9 81 <mask> → E9 81 ... */
    private fun readDtcsGmlan(session: Elm327Session, reqId: Int, trace: Trace): List<UdsDtc> {
        var payload: ByteArray? = null
        var lastNegative: UdsNegativeResponse? = null
        for (mask in GMLAN_DTC_MASKS) {
            try {
                payload = request(session, reqId, SVC_GMLAN_DTCS, "81%02X".format(mask))
                trace.add("A9 81 %02X → OK (GMLAN)".format(mask))
                break
            } catch (e: UdsNegativeResponse) {
                lastNegative = e
                if (e.nrc in MASK_RETRY_NRCS) continue  // dislikes the mask, not the service
                trace.add("A9 81 %02X → ${e.shortText()}".format(mask))
                throw e
            }
        }
        val data = payload ?: run {
            lastNegative?.let { trace.add("A9 81 → ${it.shortText()}") }
            throw (lastNegative ?: ObdException(
                "GMLAN fault read returned nothing usable.", ObdException.Kind.NO_DATA
            ))
        }

        // Reply is E9 81 then the DTC records, but some modules echo the mask
        // back before them and some do not — so both offsets are tried and the
        // one producing a plausible set (every status byte non-zero) wins.
        var records = recordsFrom(data, 3)
        if (records.isEmpty() || records.any { it[3] == 0 }) {
            val alt = recordsFrom(data, 2)
            if (alt.isNotEmpty() && alt.all { it[3] != 0 }) records = alt
        }
        if (records.isNotEmpty()) {
            trace.add("A9 81 ISO-TP → %d fault record(s)".format(records.size))
            return records.map { dtcOf(it[0], it[1], it[2], it[3], "gmlan") }
        }
        // The ISO-TP payload carried no records. That is NOT proof of a clean
        // module: GDS2, gocan and ScanMyOpel all listen for the answer as UUDT
        // frames on 0x5E8 instead. Ask again that way before reporting no
        // faults — a false clean bill of health is the worst answer this scan
        // can give. Mirrors gmlan.read_dtcs()'s tail.
        return readDtcsUudt(session, reqId, trace)
    }

    /**
     * A9 81 FF with the ELM receive filter moved to 0x5E8 — the gocan / GDS2
     * path. The module answers as a run of loose 8-byte UUDT frames rather
     * than one ISO-TP reply, so the frames are read as raw text and parsed
     * here. Port of gmlan._read_dtcs_uudt.
     */
    private fun readDtcsUudt(session: Elm327Session, reqId: Int, trace: Trace): List<UdsDtc> {
        val text = try {
            aim(session, reqId)
            session.at("ATCRA%03X".format(UUDT_DTC_CAN_ID))
            // The filter no longer matches the module, so the next request()
            // must re-aim rather than trust the cached target.
            target = null
            session.rawText("A981FF", 3000)
        } catch (_: Exception) {
            trace.add("A9 81 on 5E8 → no answer")
            return emptyList()
        } finally {
            // Restore this module's own filter on every way out. A leaked
            // 0x5E8 filter silently deafens the next module's read.
            target = null
            try {
                aim(session, reqId)
            } catch (_: Exception) {
            }
        }
        val records = parseUudtDtcFrames(text)
        if (records.isEmpty()) {
            trace.add("A9 81 on 5E8 → no UUDT frames")
            return emptyList()
        }
        trace.add("A9 81 on 5E8 → %d UUDT frame(s)".format(records.size))
        return records.map {
            val dtc = dtcOf(it[0], it[1], it[2], it[3], "gmlan")
            dtc.copy(flags = dtc.flags + "UUDT frames on 5E8")
        }
    }

    /**
     * Turn an ELM hex dump of 0x5E8 UUDT frames into [hi][lo][failureType]
     * [status] records. gocan (pkg/gmlan ReadDiagnosticInformation): each
     * 8-byte payload is [src][dtcHi][dtcLo][failureType][status]...; the list
     * ends at the frame whose first three DTC bytes are all zero.
     */
    private fun parseUudtDtcFrames(text: String): List<IntArray> {
        val out = mutableListOf<IntArray>()
        for (line in text.uppercase().replace("\r", "\n").split("\n")) {
            val hex = line.filter { it in "0123456789ABCDEF" }
            if (hex.length < 10 || hex.length % 2 != 0) continue
            var body = List(hex.length / 2) { i -> hex.substring(i * 2, i * 2 + 2).toInt(16) }
            // ATH0: payload only. ATH1: 5E8 then the payload — strip the id.
            if (body.size >= 5 && body[0] == 0x05 && body[1] == 0xE8) body = body.drop(2)
            if (body.size < 5) continue
            // body[0] is the source byte; the record starts after it.
            val hi = body[1]
            val lo = body[2]
            val ftype = body[3]
            val status = body[4]
            if (hi == 0 && lo == 0 && ftype == 0) break
            out.add(intArrayOf(hi, lo, ftype, status))
        }
        return out
    }

    /** Plain UDS fault memory: 19 02 <mask> → 59 02 <availability> ... */
    private fun readDtcsUds(session: Elm327Session, reqId: Int, trace: Trace): List<UdsDtc> {
        var payload: ByteArray? = null
        var lastNegative: UdsNegativeResponse? = null
        for (mask in UDS_DTC_MASKS) {
            try {
                payload = request(session, reqId, 0x19, "02%02X".format(mask))
                trace.add("19 02 %02X → OK (UDS)".format(mask))
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
            throw (lastNegative ?: ObdException(
                "UDS fault read returned nothing usable.", ObdException.Kind.NO_DATA
            ))
        }
        return recordsFrom(data, 3).map { dtcOf(it[0], it[1], it[2], it[3], "uds") }
    }

    // ---------------------------------------------------------- identification

    /** GMW3110 0x1A: ONE-byte identifier, reply 5A <id> <body>. */
    private fun readIdentGmlan(session: Elm327Session, reqId: Int, trace: Trace): List<Pair<String, String>> {
        val out = mutableListOf<Pair<String, String>>()
        for ((ident, label) in GMLAN_IDENT_IDS) {
            try {
                val payload = request(session, reqId, SVC_GMLAN_IDENT, "%02X".format(ident), 2500)
                if (payload.size < 3 || (payload[1].toInt() and 0xFF) != ident) continue
                val value = printable(payload.drop(2))
                if (value.isNotEmpty()) out.add(label to value)
            } catch (e: UdsNegativeResponse) {
                if (ident == 0x90) trace.add("1A 90 → ${e.shortText()}")
            } catch (_: Exception) {
                // identifier not supported on this module — normal, skip it.
            }
        }
        return out
    }

    /** Plain UDS 0x22: two-byte DID, reply 62 <did hi> <did lo> <body>. */
    private fun readIdentUds(session: Elm327Session, reqId: Int, trace: Trace): List<Pair<String, String>> {
        val out = mutableListOf<Pair<String, String>>()
        for ((did, label) in UDS_IDENT_DIDS) {
            try {
                val payload = request(session, reqId, 0x22, "%04X".format(did), 2500)
                if (payload.size < 4) continue
                val value = printable(payload.drop(3))
                if (value.isNotEmpty()) out.add(label to value)
            } catch (e: UdsNegativeResponse) {
                if (did == 0xF190) trace.add("22 F190 → ${e.shortText()}")
            } catch (_: Exception) {
                // DID not supported on this module — normal, skip it.
            }
        }
        return out
    }

    // -------------------------------------------------------- dialect ladder

    /** GMW3110 first — the expected Global A dialect — unless probing already
     *  proved otherwise. */
    private fun dialectOrder(hint: String?): List<String> =
        if (hint == "uds") listOf("uds", "gmlan") else listOf("gmlan", "uds")

    /** (ident, the dialect that produced it); empty and null if neither did. */
    private fun readIdent(
        session: Elm327Session,
        reqId: Int,
        hint: String?,
        trace: Trace,
    ): Pair<List<Pair<String, String>>, String?> {
        for (which in dialectOrder(hint)) {
            val ident = try {
                if (which == "uds") readIdentUds(session, reqId, trace)
                else readIdentGmlan(session, reqId, trace)
            } catch (_: Exception) {
                continue
            }
            if (ident.isNotEmpty()) return ident to which
        }
        return emptyList<Pair<String, String>>() to null
    }

    /** What one present module gave up: ident, faults, the dialect that read
     *  them, and the error if neither dialect could.
     *
     *  [hint] is whatever probing managed to prove, which is usually nothing.
     *  So the reported dialect is not a guess carried forward from probing: it
     *  is the dialect whose FAULT read actually succeeded, falling back to
     *  whichever produced identification if neither fault service worked. A
     *  module whose dialect stays unproven says so. */
    private fun readModule(
        session: Elm327Session,
        reqId: Int,
        hint: String?,
        trace: Trace,
    ): ModuleRead {
        val (ident, identDialect) = readIdent(session, reqId, hint, trace)
        var error: String? = null
        for (which in dialectOrder(hint)) {
            try {
                val dtcs = if (which == "uds") readDtcsUds(session, reqId, trace)
                else readDtcsGmlan(session, reqId, trace)
                return ModuleRead(ident, dtcs, which, null)
            } catch (e: UdsNegativeResponse) {
                error = "${which.uppercase()} fault read refused: ${e.shortText()}"
            } catch (e: ObdException) {
                error = "${which.uppercase()} fault read failed: ${e.message}"
            }
        }
        return ModuleRead(ident, emptyList(), identDialect, error)
    }

    private data class ModuleRead(
        val ident: List<Pair<String, String>>,
        val dtcs: List<UdsDtc>,
        val dialect: String?,
        val error: String?,
    )

    /**
     * The car's VIN over plain OBD Mode 09 PID 02, broadcast — the one VIN
     * request that works on any ~2001+ car whatever dialect the modules speak,
     * and the backup for the module ident consensus. Must run with the ELM
     * aimed at broadcast (7DF), i.e. after restore().
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

    // ------------------------------------------------------------- top level

    private const val ABSENT_HINT =
        "Silent addresses are not fitted there on this model — but two whole " +
            "groups can never answer a dongle at all. GM's Low Speed GMLAN " +
            "(single-wire, OBD pin 1) carries much of the comfort/body traffic and " +
            "no standard adapter reaches it. And a 2019+ Corsa, Mokka, Astra L, " +
            "Crossland or Grandland is a PSA/Stellantis car, not a GM one — use " +
            "the Peugeot profile for those."

    /** The demo car: an Astra-VXR-shaped Global A bus, so the Modules tab can
     *  be shown with nothing plugged in. */
    private fun simulatedScan(): UdsScanReport {
        val present = setOf(0x7E0, 0x7E2, 0x241, 0x243, 0x24C)
        val idents = mapOf(
            0x7E0 to listOf("VIN" to "SIMULATEDASTRAVXR", "End model part number" to "12657228"),
            0x7E2 to listOf("End model part number" to "24275318"),
            0x241 to listOf("VIN" to "SIMULATEDASTRAVXR"),
            0x243 to listOf("System supplier id" to "BOSCH"),
            0x24C to listOf("GM broadcast code" to "NPVL"),
        )
        val modules = ECUS.map { ecu ->
            if (ecu.req !in present) {
                UdsModuleResult(ecu.req, ecu.code, ecu.name, false, emptyList(), emptyList(), null)
            } else {
                val dtcs = if (ecu.req == 0x7E0) {
                    // P0171-00 system too lean, current + lamp on.
                    listOf(dtcOf(0x01, 0x71, 0x00, 0x82, "gmlan"))
                } else {
                    emptyList()
                }
                UdsModuleResult(
                    ecu.req, ecu.code, ecu.name, true,
                    idents[ecu.req] ?: emptyList(), dtcs, null,
                    listOf("1A 90 → positive, GMLAN dialect proven"),
                )
            }
        }
        return UdsScanReport(
            ok = true,
            simulated = true,
            brand = "vauxhall",
            modules = modules,
            vin = "SIMULATEDASTRAVXR",
            message = "SIMULATED — no real car connected. VIN SIMULATEDASTRAVXR. " +
                "5 module(s) answered, 1 fault stored. Reads were GMW3110 (GMLAN) — " +
                "the expected Global A dialect.",
            absentHint = ABSENT_HINT,
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
                ok = false, simulated = simulated, brand = "vauxhall", modules = emptyList(),
                message = "This car is talking ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k High Speed GMLAN that Vauxhall module scanning " +
                    "uses. Turn the ignition on and retry. If this is a 2019+ " +
                    "Corsa/Mokka/Astra L it is a PSA/Stellantis car, not a GM one — use " +
                    "the Peugeot profile.",
            )
        }

        val modules = mutableListOf<UdsModuleResult>()
        val dialects = mutableSetOf<String>()
        begin(session)
        try {
            for (ecu in ECUS) {
                onProgress("Probing ${ecu.code}…")
                val trace = Trace()
                var hit = probe(session, ecu.req, trace)
                if (hit == null && ecu.req == ENGINE) {
                    // Mode 01 PID 00 on the standard OBD slot: the one request
                    // an emissions ECU must answer with the ignition on.
                    onProgress("Probing ${ecu.code} (standard OBD channel)…")
                    hit = try {
                        request(session, ENGINE, 0x01, "00", 4000)
                        trace.add("answered on standard OBD 7E0 (01 00)")
                        Presence("01 00", null)
                    } catch (_: Exception) {
                        null
                    }
                }
                if (hit == null) {
                    modules.add(
                        UdsModuleResult(
                            ecu.req, ecu.code, ecu.name, false,
                            emptyList(), emptyList(), null, trace.lines.toList(),
                        )
                    )
                    continue
                }
                val read = readModule(session, ecu.req, hit.dialect, trace)
                read.dialect?.let { dialects.add(it) }
                trace.add(
                    "present via ${hit.provedBy}; dialect ${read.dialect?.uppercase() ?: "UNPROVEN"}"
                )
                modules.add(
                    UdsModuleResult(
                        ecu.req, ecu.code, ecu.name, true,
                        read.ident, read.dtcs, read.error, trace.lines.toList(),
                    )
                )
            }
        } finally {
            restore(session)
        }

        val present = modules.filter { it.present }
        val faults = present.sumOf { it.dtcs.size }
        val (consensusVin, vinWarning) = VinCheck.consensus(modules)
        val vin = consensusVin ?: vinFromObd(session)

        // A module whose fault read failed in BOTH dialects has an UNREAD fault
        // memory, not an empty one. Counting it as "0 faults" would hand back a
        // clean bill of health for a car nobody actually asked, so the two
        // cases are separated and the message refuses to summarise a total when
        // any module went unread.
        val unread = present.filter { it.error != null && it.dtcs.isEmpty() }

        val spoken = when {
            present.isEmpty() -> "Nothing answered on either dialect."
            dialects.isEmpty() ->
                "Modules answered but none completed a read in either dialect — check each trace."
            dialects == setOf("gmlan") ->
                "Reads were GMW3110 (GMLAN) — the expected Global A dialect."
            dialects == setOf("uds") -> "Reads were plain UDS."
            else -> "Mixed dialects: some modules read over UDS, some over GMW3110."
        }
        val faultText = if (unread.isNotEmpty()) {
            "FAULT MEMORY NOT READ on ${unread.size} of ${present.size} module(s) " +
                "(${unread.joinToString(", ") { it.code }}) — both the GMLAN (A9) and UDS " +
                "(19) fault services failed there, so those modules are UNREAD, not clean. " +
                "$faults fault(s) were read from the rest. Do not treat this as a clean car. "
        } else {
            "$faults fault${if (faults == 1) "" else "s"} stored. "
        }

        return UdsScanReport(
            ok = true,
            simulated = simulated,
            brand = "vauxhall",
            modules = modules,
            vin = vin,
            message = (vin?.let { "VIN $it. " } ?: "") +
                "${present.size} module(s) answered. " + faultText + spoken + vinWarning +
                " Faults are shown GM-style as code + failure type. Read-only scan — " +
                "nothing was changed on the car.",
            absentHint = ABSENT_HINT,
        )
    }

    /**
     * Clear one module's fault memory, then re-read it. Returns fresh DTCs.
     * Ladder as backend/profiles/vauxhall/scan.py clear() runs it — UDS
     * 14 FF FF FF first, then GMLAN 04. That is deliberately the opposite
     * order to the READ side (GMLAN first): the narrower UDS clear is tried
     * before the one that wipes everything. Note the
     * difference — GMLAN's clear takes no group argument, so on a GMW3110
     * module this wipes that module's ENTIRE fault memory. There is no
     * "emissions only" option. The UI confirm-gates this.
     */
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
                request(session, reqId, SVC_GMLAN_CLEAR, "", 8000)
            }
            return try {
                readModule(session, reqId, null, trace).dtcs
            } catch (_: Exception) {
                emptyList()
            }
        } finally {
            restore(session)
        }
    }
}
