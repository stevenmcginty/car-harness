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

data class BmwDtc(
    val code: String,        // J2012-style letter code, e.g. B37F2
    val hex: String,         // raw 3-byte BMW code, what forums search by
    val status: String,
    val flags: List<String>,
    val description: String,
)

data class BmwModuleResult(
    val address: Int,
    val code: String,
    val name: String,
    val present: Boolean,
    val ident: List<Pair<String, String>>, // label -> value
    val dtcs: List<BmwDtc>,
    val error: String?,
)

data class BmwScanReport(
    val ok: Boolean,
    val simulated: Boolean,
    val modules: List<BmwModuleResult>,
    val message: String,
)

object BmwUds {

    // Common F-series diagnostic addresses (F25/F26 era) — same table as the
    // desktop's ENET scan. Probing an address that isn't fitted is harmless:
    // the gateway never answers and we mark it "not fitted".
    val ECUS = listOf(
        Triple(0x10, "ZGW", "Central gateway"),
        Triple(0x12, "DME/DDE", "Engine"),
        Triple(0x18, "EGS", "Automatic transmission"),
        Triple(0x01, "ACSM", "Airbag / crash safety"),
        Triple(0x29, "DSC", "ABS / stability control"),
        Triple(0x30, "EPS", "Power steering"),
        Triple(0x40, "FEM", "Front body electronics"),
        Triple(0x72, "REM", "Rear body electronics"),
        Triple(0x60, "KOMBI", "Instrument cluster"),
        Triple(0x63, "HU", "Head unit (nav / radio)"),
        Triple(0x78, "IHKA", "Climate control"),
        Triple(0x36, "TCB", "Telematics (if fitted)"),
        Triple(0x21, "VDM", "Adaptive suspension (if fitted)"),
        Triple(0x64, "PMA", "Parking assistant (if fitted)"),
    )

    private val IDENT_DIDS = listOf(
        0xF187 to "Part number",
        0xF189 to "Software",
        0xF191 to "Hardware",
    )

    private val STATUS_BITS = listOf(
        0x01 to "test failed",
        0x02 to "failed this cycle",
        0x04 to "pending",
        0x08 to "confirmed",
        0x10 to "not completed since clear",
        0x20 to "failed since clear",
        0x40 to "not completed this cycle",
        0x80 to "warning light requested",
    )

    private val NRC_NAMES = mapOf(
        0x10 to "general reject",
        0x11 to "service not supported",
        0x12 to "sub-function not supported",
        0x13 to "incorrect message length",
        0x21 to "busy, repeat request",
        0x22 to "conditions not correct (ignition on?)",
        0x31 to "request out of range",
        0x33 to "security access denied",
        0x78 to "response pending",
        0x7F to "service not supported in active session",
    )

    class NegativeResponse(val service: Int, val nrc: Int) : Exception(
        "Negative response to %02X: %02X (%s)".format(service, nrc, NRC_NAMES[nrc] ?: "unknown")
    )

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

    /** Point the ELM at one module's address. */
    private fun target(session: Elm327Session, dst: Int) {
        session.at("ATSH6F1")
        session.at("ATCEA%02X".format(dst))
        session.at("ATCRA6%02X".format(dst))
        session.at("ATFCSD%02X300008".format(dst))
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
     * Throws NegativeResponse on 7F, ObdException(NO_DATA) when nothing
     * answers. Retries a couple of times on "response pending".
     */
    private fun request(
        session: Elm327Session,
        service: Int,
        dataHex: String,
        timeoutMs: Long = 6000,
    ): ByteArray {
        val cmd = "%02X".format(service) + dataHex
        var pendingRetries = 2
        while (true) {
            val records = session.udsRecords(cmd, timeoutMs)
            var negative: NegativeResponse? = null
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
                    negative = NegativeResponse(r[1].toInt() and 0xFF, r[2].toInt() and 0xFF)
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
    private fun testerPresent(session: Elm327Session, dst: Int): Boolean {
        target(session, dst)
        return try {
            request(session, 0x3E, "00", 4000)
            true
        } catch (_: NegativeResponse) {
            true  // a negative response still proves an ECU is there
        } catch (_: Exception) {
            false
        }
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

    private fun readDtcs(session: Elm327Session): List<BmwDtc> {
        // reportDTCByStatusMask; wide mask first, then a narrow fallback.
        var payload: ByteArray? = null
        var lastNegative: NegativeResponse? = null
        for (mask in listOf(0xAF, 0x0C)) {
            try {
                payload = request(session, 0x19, "02%02X".format(mask))
                break
            } catch (e: NegativeResponse) {
                lastNegative = e
                if (e.nrc == 0x12 || e.nrc == 0x31) continue
                throw e
            }
        }
        val data = payload ?: throw (lastNegative ?: ObdException(
            "Fault read failed.", ObdException.Kind.NO_DATA
        ))

        // Reply: 59 02 <availabilityMask> then 4-byte records [dtc x3][status].
        val dtcs = mutableListOf<BmwDtc>()
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
                BmwDtc(
                    code = code,
                    hex = "%02X%02X%02X".format(b0, b1, b2),
                    status = "%02X".format(status),
                    flags = STATUS_BITS.filter { (bit, _) -> status and bit != 0 }.map { it.second },
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
    ): BmwScanReport {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            return BmwScanReport(
                ok = false, simulated = simulated, modules = emptyList(),
                message = "This car is talking ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k CAN that BMW F-series module scanning needs.",
            )
        }
        val modules = mutableListOf<BmwModuleResult>()
        begin(session)
        try {
            for ((addr, code, name) in ECUS) {
                onProgress("Probing $code — $name…")
                if (!testerPresent(session, addr)) {
                    modules.add(BmwModuleResult(addr, code, name, false, emptyList(), emptyList(), null))
                    continue
                }
                var ident: List<Pair<String, String>> = emptyList()
                var dtcs: List<BmwDtc> = emptyList()
                var error: String? = null
                try {
                    ident = readIdent(session)
                    dtcs = readDtcs(session)
                } catch (e: NegativeResponse) {
                    error = e.message
                } catch (e: ObdException) {
                    error = e.message
                }
                modules.add(BmwModuleResult(addr, code, name, true, ident, dtcs, error))
            }
        } finally {
            restore(session)
        }

        val present = modules.filter { it.present }
        val faults = present.sumOf { it.dtcs.size }
        val simPrefix = if (simulated) "SIMULATED — no real car connected. " else ""
        return BmwScanReport(
            ok = true,
            simulated = simulated,
            modules = modules,
            message = simPrefix + "${present.size} modules answered, $faults fault" +
                (if (faults == 1) "" else "s") +
                " stored. Read-only scan — nothing was changed on the car.",
        )
    }

    /** Clear one module's fault memory, then re-read it. Returns fresh DTCs. */
    fun clearDtcs(session: Elm327Session, dst: Int): List<BmwDtc> {
        session.ensureProtocol()
        if (!protocolSupported(session)) {
            throw ObdException("Not on a BMW CAN connection.", ObdException.Kind.PROTOCOL)
        }
        begin(session)
        try {
            target(session, dst)
            request(session, 0x14, "FFFFFF", 8000)
            return try {
                readDtcs(session)
            } catch (_: Exception) {
                emptyList()
            }
        } finally {
            restore(session)
        }
    }
}
