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

    // 11-bit header state: the Mazda/PSA recipes aim at modules with ATSH<id>.
    private var atsh: Int? = null
    private var cra: Int? = null
    private val mazdaCleared = mutableSetOf<Int>()
    private val psaCleared = mutableSetOf<Int>()
    private val psaExtended = mutableSetOf<Int>()

    // Every id the simulated Mazda's UDS side owns (present or not).
    private val mazdaTargets =
        (0x7E0..0x7E7).toSet() + setOf(0x706, 0x720, 0x726, 0x730, 0x731, 0x736,
            0x737, 0x760, 0x764, 0x772, 0x793)

    // Every id the simulated PSA car's UDS side owns (PeugeotUds.ECUS request
    // ids). Note 0x730/0x760/0x764 are on the Mazda list too — Mazda wins
    // there, which is fine: those PSA modules then read as "not fitted".
    private val psaTargets = setOf(
        0x752, 0x6A8, 0x6A9, 0x6AD, 0x744, 0x75F, 0x75D, 0x6B5, 0x6B3,
        0x76D, 0x6AF, 0x765, 0x6A1, 0x6C1, 0x6B4,
    )

    // The Golf demo shares the ISO powertrain ids (0x7E0..) with the Mazda demo,
    // so answering purely by ATSH id can't tell a VW engine probe from a Mazda
    // one. A VW scan always aims at a VW-only module (the gateway 0x710 leads
    // VwUds.ECUS) before it reaches the shared engine id, so we remember when a
    // VW-only id is addressed and route the shared ids to the Golf demo until the
    // ELM is re-aimed off the VW map (restore()'s ATSH7DF, or a Mazda/PSA
    // module). vwActive is therefore false throughout every non-VW scan.
    private val vwCleared = mutableSetOf<Int>()
    private val vwExtended = mutableSetOf<Int>()
    private var vwActive = false

    // Every id a VW/Audi scan aims at (VwUds.ECUS request ids).
    private val vwTargets = setOf(
        0x7E0, 0x7E1, 0x7E2, 0x710, 0x714, 0x713, 0x715, 0x712, 0x70E, 0x746,
        0x711, 0x70C, 0x70F, 0x732, 0x70B, 0x70A, 0x752, 0x72A, 0x773, 0x7E5, 0x7E6,
    )
    // VW ids that also belong to another demo brand (engine block ↔ Mazda,
    // 0x752 ↔ PSA): addressing one keeps whichever brand is already active.
    private val vwSharedIds = setOf(0x7E0, 0x7E1, 0x7E2, 0x7E5, 0x7E6, 0x752)

    override fun open() {}
    override fun close() {}

    override fun exchange(data: ByteArray, timeoutMs: Long): ByteArray {
        val cmd = data.toString(Charsets.US_ASCII).trim().uppercase().replace(" ", "")
        val reply = when {
            cmd.isEmpty() -> ""  // monitor-stop interrupt char
            cmd == "ATZ" -> "ELM327 v1.5 (SIMULATED)"
            cmd.startsWith("ATRV") -> "12.4V"
            cmd == "ATDP" -> "SIMULATED CAN 11/500"
            cmd == "ATDPN" -> "A6"
            cmd == "ATCEA" -> { cea = null; "OK" }
            cmd.startsWith("ATCEA") -> { cea = cmd.removePrefix("ATCEA").toIntOrNull(16); "OK" }
            cmd == "ATCRA" -> { cra = null; "OK" }
            cmd.startsWith("ATCRA") -> { cra = cmd.removePrefix("ATCRA").toIntOrNull(16); "OK" }
            cmd.startsWith("ATSH") -> {
                val id = cmd.removePrefix("ATSH").toIntOrNull(16)
                atsh = id
                if (id != null) {
                    if (id in vwTargets && id !in vwSharedIds) {
                        vwActive = true         // a VW-only id: a VW scan is running
                    } else if (id !in vwSharedIds) {
                        vwActive = false        // a non-VW id: not a VW scan
                    }
                    // a shared id (0x7E0.. / 0x752): leave vwActive as last set
                }
                "OK"
            }
            cmd == "ATMA" -> mazdaMonitor(cra)
            cmd.startsWith("AT") -> "OK"
            cea != null -> bmwReply(cea!!, cmd)
            vwActive && (atsh ?: 0) in vwTargets -> vwReply(atsh!!, cmd)
            (atsh ?: 0) in mazdaTargets -> mazdaReply(atsh!!, cmd)
            (atsh ?: 0) in psaTargets -> psaReply(atsh!!, cmd)
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
            cmd.startsWith("22F190") -> isoTp("62F190" + ascii("SIMULATEDBMW00CAR"))
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

    // ------------------------------------------------ simulated Mazda modules
    // Mirrors the desktop's backend/profiles/mazda sim: PCM, TCM, cluster,
    // front body module and DSC on HS-CAN; honest "SIMULATED" identity strings.

    private val mazdaPresent = setOf(0x7E0, 0x7E1, 0x720, 0x726, 0x760)

    private fun mazdaReply(reqId: Int, cmd: String): String {
        if (reqId !in mazdaPresent) return "NO DATA"
        return when {
            cmd == "3E00" -> "7E00"
            cmd == "22F190" && reqId == 0x7E0 -> isoTp("62F190" + ascii("SIMULATED000MAZDA"))
            cmd == "22F187" -> isoTp("62F187" + ascii("PY01-%03X-K2".format(reqId)))
            cmd == "22F188" && reqId == 0x7E0 -> isoTp("62F188" + ascii("PY0118881"))
            cmd.startsWith("22") && cmd.length == 6 -> mazdaDid(reqId, cmd.substring(2))
            cmd.startsWith("1902") -> mazdaDtcs(reqId)
            cmd == "14FFFFFF" -> { mazdaCleared.add(reqId); "54" }
            else -> "NO DATA"
        }
    }

    private fun mazdaDtcs(reqId: Int): String {
        if (reqId in mazdaCleared) return "5902AF"
        return when (reqId) {
            0x7E0 -> isoTp("5902AF" + "0302002F" + "04560008") // PCM: P0302 + P0456
            0x760 -> isoTp("5902AF" + "40510008")              // DSC: C0051
            else -> "5902AF"
        }
    }

    // ------------------------------------------------- simulated PSA modules
    // Mirrors the desktop's backend/profiles/peugeot sim: BSI, engine, ABS,
    // airbag, cluster and SCR present; the airbag only reports faults after
    // an extended session (1003), exercising the real escalation path.

    // BSI, engine, ABS, airbag, cluster, SCR and the HV battery module answer.
    private val psaPresent = setOf(0x752, 0x6A8, 0x6AD, 0x744, 0x75F, 0x6A1, 0x6B4)

    private fun psaReply(reqId: Int, cmd: String): String {
        if (reqId !in psaPresent) return "NO DATA"
        return when {
            cmd == "3E00" -> "7E00"
            cmd == "1003" -> { psaExtended.add(reqId); "5003003201F4" }
            cmd == "22F190" && reqId == 0x752 ->
                isoTp("62F190" + ascii("SIMULATED0PEUGEOT"))
            cmd == "22F187" && reqId == 0x752 -> isoTp("62F187" + ascii("9822009980"))
            cmd == "22F188" && reqId == 0x6A8 -> isoTp("62F188" + ascii("1037564321"))
            cmd.startsWith("22") && cmd.length == 6 -> psaDid(reqId, cmd.substring(2))
            cmd.startsWith("21") && reqId == 0x6A8 -> psaBlock(cmd.substring(2))
            cmd.startsWith("1902") -> psaDtcs(reqId)
            cmd == "14FFFFFF" -> { psaCleared.add(reqId); "54" }
            else -> "NO DATA"
        }
    }

    private fun psaDtcs(reqId: Int): String {
        // The airbag refuses in the default session — the client must escalate.
        if (reqId == 0x744 && reqId !in psaExtended) return "7F197F"
        if (reqId in psaCleared) return "5902AF"
        return when (reqId) {
            0x6A8 -> isoTp("5902AF" + "0234002F" + "13380008") // engine: P0234 + P1338
            0x744 -> isoTp("5902AF" + "90040008")              // airbag: B1004
            0x6A1 -> isoTp("5902AF" + "20E80008")              // SCR: P20E8
            else -> "5902AF"
        }
    }

    /** PSA health-check demo answers, matching PeugeotPids' decode formulas. */
    private fun psaDid(reqId: Int, did: String): String {
        val hex = psaDemoDids[reqId]?.get(did) ?: return "7F2231"
        return isoTp("62$did$hex")
    }

    // reqId -> (DID hex -> payload hex), matching PeugeotPids.DIDS/ODO_SOURCES.
    // The demo HV battery (0x6B4) reads as a healthy used EV.
    private val psaDemoDids: Map<Int, Map<String, String>> = mapOf(
        0x6B4 to mapOf(
            "D860" to "0005C0",   // SOH: skip 1 byte, then 0x05C0/16 = 92.0 %
            "D410" to "C501",     // SOC: 0xC501/512 = 98.5 %
            "D815" to "1680",     // voltage: 0x1680/16 = 360.0 V
            "D865" to "0A80",     // energy: 0x0A80/64 = 42.00 kWh
            "D86F" to "0F5A",     // min cell: 3930 mV
            "D870" to "0F6E",     // max cell: 3950 mV
            "D49C" to "01C9C380", // odometer: 0x01C9C380/1000 = 30000 km
        ),
    )

    /** DPF 0x21 block demo (engine 0x6A8) -> a diesel with a half-full filter. */
    private fun psaBlock(localIdAndData: String): String {
        val localId = localIdAndData.take(6)
        if (localId != "CB8001") return "7F2111"  // only the DPF block is demoed
        // 32-byte block; u16 fields at the offsets PeugeotPids reads:
        //  9:soot 0x2EE0/1000=12.0g, 15:diffP 0x001E=30mbar, 19:additive 0x0C80,
        //  25:DPF temp 0x00EB=235C, 28:dist-since-regen 0x00DC=220km, 30:avg 0x01C2=450km.
        val block = "0000000000000000002EE00000000000" +  // bytes 0..15
            "1E00000C800000000000EB0000DC01C2"             // bytes 16..31
        return isoTp("61CB8001$block")
    }

    // ------------------------------------------------ simulated VW/Audi modules
    // Mirrors backend/profiles/vw scan.py _simulated_scan(): a Golf-7-ish car —
    // gateway, engine (2 DTCs), cluster (VIN), ABS and airbag (1 DTC, only after
    // the extended session) present. The VIN agrees across engine and cluster,
    // so there's no false mismatch warning.

    private val vwPresent = setOf(0x710, 0x7E0, 0x714, 0x713, 0x715)

    private fun vwReply(reqId: Int, cmd: String): String {
        if (reqId !in vwPresent) return "NO DATA"
        return when {
            cmd == "3E00" -> "7E00"
            cmd == "1003" -> { vwExtended.add(reqId); "5003003201F4" }
            cmd == "22F190" && (reqId == 0x7E0 || reqId == 0x714) ->
                isoTp("62F190" + ascii("SIMULATEDVWGOLF00"))
            cmd == "22F187" && reqId == 0x7E0 -> isoTp("62F187" + ascii("04E906027AB"))
            cmd == "22F187" && reqId == 0x710 -> isoTp("62F187" + ascii("5Q0907530AB"))
            cmd == "22F189" && reqId == 0x7E0 -> isoTp("62F189" + ascii("0004"))
            cmd.startsWith("22") && cmd.length == 6 -> "7F2231"
            cmd.startsWith("1902") -> vwDtcs(reqId)
            cmd == "14FFFFFF" -> { vwCleared.add(reqId); "54" }
            else -> "NO DATA"
        }
    }

    private fun vwDtcs(reqId: Int): String {
        // The airbag refuses in the default session — the client must escalate.
        if (reqId == 0x715 && reqId !in vwExtended) return "7F197F"
        if (reqId in vwCleared) return "5902FF"
        return when (reqId) {
            0x7E0 -> isoTp("5902FF" + "0300002F" + "01710008") // engine: P0300 + P0171
            0x715 -> isoTp("5902FF" + "90040008")              // airbag: B1004
            else -> "5902FF"
        }
    }

    /** Enhanced (mode-22) demo answers so "Live extras" works hardware-free.
     *  Values chosen to decode plausibly through MazdaPids' formulas. */
    private fun mazdaDid(reqId: Int, did: String): String {
        val hex = when (reqId) {
            0x7E0 -> when (did) {
                "1310" -> "33AE"  // oil temp 92.3°C
                "0415" -> "0140"  // oil pressure 320 kPa
                "0202" -> "02"    // stored faults
                "0301" -> "04D8"  // MAP 1.24V
                "16E9" -> "0073"  // alternator 14.4V
                "16E8" -> "2000"  // field duty 25%
                "03E8" -> "3000"  // knock adjust 0.75
                "0353" -> "0193"  // baro 100.8 kPa
                "057D" -> "76"    // ambient 19°C
                "038F" -> "1619"  // coolant 88.4°C
                "0519" -> "012C"  // 1200 km since oil change
                "0542" -> "2EE0"  // 12000 km interval
                "1172" -> "C6"    // battery 12.4V
                "16C1" -> "50B8"  // fuel 63%
                "096E" -> "0BB8"  // idle target 750rpm
                "0307" -> "30A4"  // pump duty 38%
                else -> null
            }
            0x7E1 -> when (did) {
                "1E1C" -> "1888"  // ATF 78.5°C
                "1E12" -> "04"    // 4th gear
                "1E24" -> "01"    // locked
                "1EE4" -> "1770"  // 120 Nm
                else -> null
            }
            0x720 -> when (did) {
                "61B1" -> "1F"      // 31 L
                "404C" -> "05ED94"  // 38850.0 km
                "2A05", "2A06", "2A07", "2A08" -> "92"  // ~29 psi
                else -> null
            }
            0x726 -> when (did) {
                "4028" -> "54"    // 84%
                "4029" -> "3D"    // 21°C
                "402A" -> "84"    // 12.6V
                "402B" -> "7D"    // -2A
                "4027" -> "01A4"  // 420 days
                else -> null
            }
            else -> null
        } ?: return "7F2231"
        return isoTp("62$did$hex")
    }

    /** ATMA demo: broadcast frames for whichever id ATCRA points at. */
    private fun mazdaMonitor(craId: Int?): String {
        val line = when (craId) {
            0x202 -> "2020D48000000000000"  // 850 rpm, 0 km/h, 0% pedal
            0x420 -> "420" + "80" + "0000000000" + "324C"  // coolant 88°C, ambient 19°C
            0x215 -> "2152710271027102710"  // 4 × 0 km/h
            0x078 -> "078000000009C400000"  // 0 bar
            0x086 -> "0863EA3000000000000"  // -3.5°
            0x165 -> "1650000000000001300"  // N or 1st
            0x0FD -> "0FD0000000000006300"  // 99+2 kPa
            0x4FA -> "4FA000000003E000000"  // IAT 22°C
            0x09E -> "09E00000000009D0000"  // 31.4 L
            0x09F -> "09F1000000000000000"  // handbrake applied
            0x050 -> "0500200000000000000"  // ignition ON
            0x43E -> "43E0000000000000000"  // all closed
            0x3D1 -> "3D100000000025F0800"  // 38850 km
            else -> return ""
        }
        return "$line\r$line"
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
