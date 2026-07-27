package com.carharness.app.obd

import java.io.IOException

/** An honest OBD-level failure. */
class ObdException(message: String, val kind: Kind) : IOException(message) {
    enum class Kind { NO_DATA, LINK, PROTOCOL }
}

/**
 * ELM327 driver: init sequence, AT commands, OBD queries and reply parsing.
 * A line-for-line port of the desktop driver (backend/obd/elm327.py).
 */
class Elm327Session(private val transport: ObdTransport) {

    var firmware: String = ""
        private set
    var protocol: String = ""
        private set
    var protocolNum: Int? = null
        private set
    var opened: Boolean = false
        private set

    val isCan: Boolean
        get() = protocolNum == null || protocolNum!! >= 6

    companion object {
        private val NO_DATA_TOKENS = listOf("NO DATA", "NODATA")
        private val LINK_ERROR_TOKENS = listOf(
            "UNABLE TO CONNECT", "BUS INIT: ...ERROR", "BUS INIT:ERROR",
            "CAN ERROR", "BUS ERROR", "FB ERROR", "DATA ERROR", "STOPPED"
        )
        private val HEX_LINE = Regex("^[0-9A-F]+$")
        private val ISO_TP_LEN = Regex("^[0-9A-F]{3}$")
        private val ISO_TP_CONT = Regex("^([0-9A-F]{1,2}):([0-9A-F]+)$")
        private val ELM_VERSION = Regex("ELM327\\s+(v[\\w.]+)", RegexOption.IGNORE_CASE)
    }

    fun open() {
        transport.open()
        // ATZ with retries: 'STOPPED' means we interrupted a running command
        // and an empty reply means it was mid-reset — both settle in a moment.
        var reply = ""
        for (attempt in 0 until 3) {
            reply = raw("ATZ", 7000)
            if (reply.uppercase().contains("ELM327")) break
            Thread.sleep(1000)
        }
        if (!reply.uppercase().contains("ELM327")) {
            val got = reply.trim().ifEmpty { "nothing" }
            throw ObdException(
                "The dongle answered the reset with '$got' instead of an ELM327 banner.",
                ObdException.Kind.LINK
            )
        }
        firmware = ELM_VERSION.find(reply)?.groupValues?.get(1) ?: "ELM327"
        for (cmd in listOf("ATE0", "ATL0", "ATS0", "ATH0", "ATSP0")) raw(cmd, 3000)
        opened = true
    }

    fun close() {
        opened = false
        try {
            transport.close()
        } catch (_: Exception) {
        }
    }

    private fun raw(cmd: String, timeoutMs: Long): String {
        val bytes = transport.exchange((cmd + "\r").toByteArray(Charsets.US_ASCII), timeoutMs)
        return bytes.toString(Charsets.US_ASCII)
    }

    fun at(cmd: String, timeoutMs: Long = 3000): String {
        var text = raw(cmd, timeoutMs).replace(">", " ").replace("\r", " ").replace("\n", " ")
        text = text.replace(cmd, " ")
        return text.split(Regex("\\s+")).filter { it.isNotBlank() }.joinToString(" ").trim()
    }

    fun voltage(): String = at("ATRV")

    fun ensureProtocol() {
        if (protocolNum != null) return
        try {
            query(0x01, 0x00, 15000)
        } catch (e: ObdException) {
            if (e.kind == ObdException.Kind.LINK || e.kind == ObdException.Kind.NO_DATA) {
                throw ObdException(
                    "The dongle is answering but the car is not. Turn the ignition ON " +
                        "(engine can stay off), then try again.",
                    ObdException.Kind.LINK
                )
            }
            throw e
        }
        val dpn = at("ATDPN")
        val m = Regex("([0-9A-F])\\s*$").find(dpn)
        protocolNum = m?.groupValues?.get(1)?.toIntOrNull(16)
        protocol = at("ATDP")
    }

    fun query(service: Int, pid: Int?, timeoutMs: Long = 8000): List<ByteArray> {
        val cmd = if (pid == null) "%02X".format(service) else "%02X%02X".format(service, pid)
        val reply = raw(cmd, timeoutMs)
        return parse(reply, service or 0x40, cmd)
    }

    /**
     * Send a raw hex request (e.g. a UDS command while BMW-addressed) and
     * return every hex record in the reply, unfiltered — module scanning
     * needs to see negative (7F ...) responses, which parse() would discard.
     */
    fun udsRecords(dataHex: String, timeoutMs: Long = 8000): List<ByteArray> =
        collect(raw(dataHex, timeoutMs), dataHex)

    /**
     * Run a bus-monitor command (ATMA) for a fixed window and return the raw
     * capture. There is no '>' while monitoring — the transports return
     * whatever arrived once the deadline lapses.
     */
    fun monitor(cmd: String, windowMs: Long): String = raw(cmd, windowMs)

    /**
     * Stop a running monitor. Any single character interrupts monitoring and
     * is discarded by the ELM — deliberately NOT a CR, which would re-run the
     * last command (the monitor itself). Returns text up to the prompt.
     */
    fun stopMonitor(): String =
        transport.exchange(byteArrayOf(0x20), 4000).toString(Charsets.US_ASCII)

    private fun parse(text: String, expected: Int, echo: String): List<ByteArray> {
        val records = collect(text, echo)
        val matching = records.filter { it.isNotEmpty() && (it[0].toInt() and 0xFF) == expected }
        if (matching.isEmpty()) {
            val got = text.replace("\r", " ").replace("\n", " ").trim()
            throw ObdException("Unexpected reply to $echo: '$got'.", ObdException.Kind.PROTOCOL)
        }
        return matching
    }

    private fun collect(text: String, echo: String): List<ByteArray> {
        // A completely empty reply means the DONGLE never answered (dead link
        // or BLE timeout) -- distinct from NO DATA, where the dongle answered
        // but the car didn't. Monitor/AT paths don't come through here.
        if (text.isBlank()) {
            throw ObdException(
                "The dongle didn't answer — the command timed out.",
                ObdException.Kind.LINK
            )
        }
        val upper = text.uppercase()
        for (token in LINK_ERROR_TOKENS) {
            if (upper.contains(token)) {
                throw ObdException(
                    "The dongle reported '$token' — it could not get an answer from the car.",
                    ObdException.Kind.LINK
                )
            }
        }
        for (token in NO_DATA_TOKENS) {
            if (upper.contains(token)) {
                throw ObdException("The car returned no data for this request.", ObdException.Kind.NO_DATA)
            }
        }

        val records = mutableListOf<ByteArray>()
        val multi = sortedMapOf<Int, String>()
        var multiLen: Int? = null
        for (lineRaw in text.split(Regex("[\\r\\n]+"))) {
            val clean = lineRaw.trim().replace(" ", "").uppercase()
            if (clean.isEmpty() || clean == ">") continue
            if (clean in listOf("SEARCHING...", "SEARCHING", "BUSINIT...", "BUSINIT...OK", "OK")) continue
            if (clean == echo.uppercase()) continue
            val cont = ISO_TP_CONT.find(clean)
            if (cont != null) {
                multi[cont.groupValues[1].toInt(16)] = cont.groupValues[2]
                continue
            }
            if (ISO_TP_LEN.matches(clean)) {
                multiLen = clean.toInt(16)
                continue
            }
            if (HEX_LINE.matches(clean) && clean.length % 2 == 0) {
                records.add(hexToBytes(clean))
            }
        }
        if (multi.isNotEmpty()) {
            var joined = multi.values.joinToString("")
            if (joined.length % 2 == 1) joined = joined.dropLast(1)
            var payload = hexToBytes(joined)
            multiLen?.let { if (payload.size > it) payload = payload.copyOfRange(0, it) }
            records.add(payload)
        }
        return records
    }

    private fun hexToBytes(hex: String): ByteArray =
        ByteArray(hex.length / 2) { i -> hex.substring(i * 2, i * 2 + 2).toInt(16).toByte() }
}
