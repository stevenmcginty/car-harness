package com.carharness.app.obd

import java.io.IOException

/**
 * A byte pipe to an ELM327-style dongle. Implementations are blocking and are
 * always called from Dispatchers.IO. exchange() writes the command and reads
 * until the ELM327 '>' prompt (or the deadline), returning everything received.
 */
interface ObdTransport {
    val label: String
    @Throws(IOException::class)
    fun open()
    @Throws(IOException::class)
    fun exchange(data: ByteArray, timeoutMs: Long): ByteArray
    fun close()
}

/**
 * Simulated dongle + car for demos with no hardware. Speaks just enough
 * ELM327 that the real driver stack runs unchanged; every identity string is
 * clearly labelled SIMULATED.
 */
class DemoTransport : ObdTransport {
    override val label = "Demo (simulated car)"
    private var cleared = false

    // BMW extended-addressing state: set by ATCEA<hh>, cleared by bare ATCEA.
    private var cea: Int? = null
    private val bmwCleared = mutableSetOf<Int>()

    override fun open() {}
    override fun close() {}

    override fun exchange(data: ByteArray, timeoutMs: Long): ByteArray {
        val cmd = data.toString(Charsets.US_ASCII).trim().uppercase().replace(" ", "")
        val reply = when {
            cmd == "ATZ" -> "ELM327 v1.5 (SIMULATED)"
            cmd.startsWith("ATRV") -> "12.4V"
            cmd == "ATDP" -> "SIMULATED CAN 11/500"
            cmd == "ATDPN" -> "A6"
            cmd == "ATCEA" -> { cea = null; "OK" }
            cmd.startsWith("ATCEA") -> { cea = cmd.removePrefix("ATCEA").toIntOrNull(16); "OK" }
            cmd.startsWith("AT") -> "OK"
            cea != null -> bmwReply(cea!!, cmd)
            cmd == "0100" -> "4100BE3FA813"
            cmd == "0120" -> "412080000001"
            cmd == "0140" -> "414040000000"
            cmd == "0101" -> if (cleared) "41010000E000" else "41018200E000"
            cmd == "03" -> if (cleared) "43 00" else "4302030104 20"
            cmd == "07" -> if (cleared) "NO DATA" else "470101 71"
            cmd == "0A" -> "NO DATA"
            cmd == "04" -> { cleared = true; "44" }
            cmd == "0902" ->
                "014\r0:49020153494D55\r1:4C4154454430434152\r2:30303031"
            cmd.startsWith("01") && cmd.length == 4 -> livePid(cmd.substring(2))
            else -> "NO DATA"
        }
        return "$reply\r\r>".toByteArray(Charsets.US_ASCII)
    }

    // ------------------------------------------------- simulated BMW modules

    private val bmwPresent = mapOf(
        0x10 to "8637890", 0x12 to "8631745", 0x18 to "8632267", 0x29 to "6879043",
        0x60 to "9382566", 0x63 to "9387573", 0x78 to "9384046",
    )

    private fun bmwReply(dst: Int, cmd: String): String {
        if (dst !in bmwPresent) return "NO DATA"
        return when {
            cmd == "3E00" -> "7E00"
            cmd.startsWith("22F187") -> isoTp("62F187" + ascii(bmwPresent[dst]!!))
            cmd.startsWith("22F189") -> isoTp("62F189" + ascii("08.11.%02X".format(dst)))
            cmd.startsWith("22F191") -> isoTp("62F191" + ascii("06"))
            cmd.startsWith("22") -> "7F2231"
            cmd.startsWith("1902") -> bmwDtcs(dst)
            cmd == "14FFFFFF" -> { bmwCleared.add(dst); "54" }
            else -> "NO DATA"
        }
    }

    private fun bmwDtcs(dst: Int): String {
        if (dst in bmwCleared) return "5902AF"
        return when (dst) {
            0x63 -> isoTp("5902AF" + "B7F2032F" + "B7F82108") // head unit: 2 faults
            0x18 -> isoTp("5902AF" + "27710108")              // gearbox: 1 fault
            else -> "5902AF"
        }
    }

    private fun ascii(text: String): String =
        text.toByteArray(Charsets.US_ASCII).joinToString("") { "%02X".format(it) }

    /** Render a payload the way an ELM327 prints it (multi-frame if > 7 bytes). */
    private fun isoTp(payloadHex: String): String {
        val nbytes = payloadHex.length / 2
        if (nbytes <= 7) return payloadHex
        val lines = StringBuilder("%03X".format(nbytes))
        var i = 0
        var frame = 0
        while (i < payloadHex.length) {
            val chunk = payloadHex.substring(i, minOf(i + 14, payloadHex.length))
            lines.append("\r").append("%X:".format(frame and 0xF)).append(chunk)
            i += 14
            frame++
        }
        return lines.toString()
    }

    private fun livePid(pid: String): String {
        val t = System.currentTimeMillis() / 1000.0
        fun sine(period: Double) = Math.sin(t / period)
        val value: Int = when (pid) {
            "0C" -> (((820 + 260 * sine(2.1) + 40 * sine(0.6)) * 4).toInt())
            "0D" -> (32 + 30 * sine(9.0)).coerceAtLeast(0.0).toInt()
            "05" -> (55 + (t % 600) / 8.0).coerceAtMost(92.0).toInt() + 40
            "04" -> ((22 + 12 * sine(3.2)) * 255 / 100).toInt()
            "11" -> ((14 + 9 * sine(2.6)) * 255 / 100).toInt()
            "0F" -> 24 + 40
            "10" -> ((6.5 + 3.5 * sine(2.1)) * 100).toInt()
            "2F" -> (62 * 255 / 100)
            "5C" -> (40 + (t % 600) / 7.0).coerceAtMost(95.0).toInt() + 40
            "42" -> ((13.9 + 0.15 * sine(5.0)) * 1000).toInt()
            "0B" -> (33 + 8 * sine(3.0)).toInt()
            "33" -> 101
            "46" -> 19 + 40
            else -> return "NO DATA"
        }
        val pidByte = pid.toInt(16)
        val def = Pids.byPid[pidByte] ?: return "NO DATA"
        return if (def.nbytes == 2) {
            "41%02X%02X%02X".format(pidByte, (value shr 8) and 0xFF, value and 0xFF)
        } else {
            "41%02X%02X".format(pidByte, value.coerceIn(0, 255))
        }
    }
}
