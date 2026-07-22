package com.carharness.app.obd

/**
 * Mazda broadcast-CAN snapshot over an ELM327 — read-only passive sniffing.
 *
 * The dongle monitors the bus (ATMA) with a receive filter (ATCRA) so the
 * frames the car's modules broadcast anyway can be captured and decoded
 * without asking any ECU anything. On the MX-5 ND the instrument cluster
 * bridges MS-CAN traffic onto HS-CAN, so body data (doors, fuel, ignition)
 * shows up at the OBD port too.
 *
 * Decodes are the ND-community-verified ones; where sources conflicted the
 * ND-verified formula won. Frames this car/year doesn't broadcast are
 * skipped. Mirror of backend/profiles/mazda/snapshot.py — keep in step.
 *
 * Monitor recipe per id: ATH1 once, ATCRA<id> + ATMA, collect for a short
 * window, then stop with a single SPACE and NO trailing CR — a bare CR would
 * re-run ATMA. Restore ATCRA + ATH0 after.
 */
object MazdaCan {

    private const val KM_TO_MI = 0.621371

    // (can_id, window ms) — window sized to the frame's broadcast rate.
    private val SNAPSHOT_IDS = listOf(
        0x202 to 350L,  // PCM: rpm / speed / accelerator
        0x420 to 450L,  // PCM: coolant / ambient
        0x215 to 350L,  // DSC: wheel speeds
        0x078 to 350L,  // DSC: brake pressure
        0x086 to 350L,  // EPS: steering angle
        0x165 to 350L,  // PCM: manual gear position
        0x131 to 450L,  // AT shift position (automatics)
        0x0FD to 450L,  // PCM: manifold pressure
        0x4FA to 450L,  // PCM: intake air temperature
        0x09E to 450L,  // IC: fuel level (litres)
        0x09F to 450L,  // IC: handbrake
        0x050 to 450L,  // SSU: ignition state
        0x43E to 450L,  // BCM: doors / boot
        0x3D1 to 1200L, // CMU: odometer
    )

    private val MT_GEARS = mapOf(19 to "N or 1st", 11 to "2nd", 7 to "3rd", 6 to "4th", 5 to "5th", 3 to "6th")
    private val AT_GEARS = mapOf(1 to "P", 2 to "R", 3 to "N", 4 to "D")
    private val IGNITION = mapOf(0 to "OFF", 1 to "ACC", 2 to "ON", 3 to "START")

    private fun u16(d: ByteArray, i: Int): Int =
        ((d[i].toInt() and 0xFF) shl 8) or (d[i + 1].toInt() and 0xFF)

    private fun b(d: ByteArray, i: Int): Int = d[i].toInt() and 0xFF

    /** One captured frame -> readings. Out-of-range values are dropped, not shown wrong. */
    fun decodeFrame(canId: Int, d: ByteArray): List<EnhancedReading> {
        val out = mutableListOf<EnhancedReading>()
        fun add(name: String, value: String, unit: String = "") {
            out.add(EnhancedReading(name, value, unit))
        }
        when {
            canId == 0x202 && d.size >= 6 -> {
                val rpm = u16(d, 0) / 4.0
                if (rpm in 0.0..9000.0) add("Engine RPM", "%.0f".format(rpm), "rpm")
                val kmh = u16(d, 2) / 100.0
                if (kmh in 0.0..300.0) add("Speed", "%.1f".format(kmh * KM_TO_MI), "mph")
                val accel = u16(d, 4) / 640.0
                if (accel in 0.0..101.0) add("Accelerator pedal", "%.1f".format(accel), "%")
            }
            canId == 0x420 && d.size >= 8 -> {
                add("Coolant temperature", (b(d, 0) - 40).toString(), "°C")
                val ambient = u16(d, 6) * 0.25 - 3200
                if (ambient in -45.0..70.0) add("Outside temperature", "%.1f".format(ambient), "°C")
            }
            canId == 0x215 && d.size >= 8 -> {
                val speeds = listOf(0, 2, 4, 6).map { (u16(d, it) - 10000) / 100.0 }
                if (speeds.all { it in 0.0..300.0 }) {
                    add("Wheel speeds", speeds.joinToString(" / ") { "%.1f".format(it) }, "km/h")
                }
            }
            canId == 0x078 && d.size >= 6 -> {
                val bar = u16(d, 4) * 0.001 - 40
                if (bar in 0.0..250.0) add("Brake pressure", "%.1f".format(bar), "bar")
            }
            canId == 0x086 && d.size >= 2 -> {
                val angle = (16000 - u16(d, 0)) * 0.1
                if (angle in -720.0..720.0) add("Steering angle", "%+.1f".format(angle), "° (+ right)")
            }
            canId == 0x165 && d.size >= 7 -> MT_GEARS[b(d, 6)]?.let { add("Gear (manual)", it) }
            canId == 0x131 && d.size >= 2 -> AT_GEARS[b(d, 1)]?.let { add("Gear selector (auto)", it) }
            canId == 0x0FD && d.size >= 7 -> {
                val kpa = b(d, 6) + 2
                if (kpa in 10..260) add("Manifold pressure", kpa.toString(), "kPa")
            }
            canId == 0x4FA && d.size >= 5 -> {
                val iat = b(d, 4) - 40
                if (iat in -40..150) add("Intake air temperature", iat.toString(), "°C")
            }
            canId == 0x09E && d.size >= 6 -> {
                val litres = b(d, 5) * 0.2
                if (litres in 0.0..60.0) add("Fuel in tank", "%.1f".format(litres), "L")
            }
            canId == 0x09F && d.size >= 1 ->
                add("Handbrake", if (b(d, 0) and 0x10 != 0) "applied" else "released")
            canId == 0x050 && d.size >= 1 ->
                add("Ignition", IGNITION[b(d, 0) and 0x03] ?: "state ${b(d, 0) and 0x03}")
            canId == 0x43E && d.size >= 6 -> {
                val open = mutableListOf<String>()
                if (b(d, 4) and 0x10 != 0) open.add("driver door")
                if (b(d, 4) and 0x20 != 0) open.add("passenger door")
                if (b(d, 5) and 0x80 != 0) open.add("boot")
                add("Doors", if (open.isEmpty()) "all closed" else open.joinToString(", ") + " open")
            }
            canId == 0x3D1 && d.size >= 7 -> {
                val km = ((b(d, 4) shl 14) or (b(d, 5) shl 6) or (b(d, 6) shr 2)).toLong()
                if (km in 1 until 2_000_000) {
                    add("Odometer", "%,d mi (%,d km)".format((km * KM_TO_MI).toLong(), km))
                }
            }
        }
        return out
    }

    /**
     * Raw ATMA capture -> data payloads for one CAN id. With ATH1 + ATS0 a
     * monitored 11-bit frame prints as 3 hex id digits then the data bytes,
     * no DLC. Noise (BUFFER FULL, STOPPED, prompt, clipped lines) is skipped.
     */
    fun parseMonitorText(text: String, canId: Int): List<ByteArray> {
        val want = "%03X".format(canId)
        val frames = mutableListOf<ByteArray>()
        for (tokenRaw in text.uppercase().replace(">", "").split(Regex("\\s+"))) {
            val token = tokenRaw.trim()
            if (token.length < 5 || !token.startsWith(want)) continue
            val payload = token.substring(3)
            if (payload.length % 2 != 0 || payload.length > 16) continue
            if (!payload.all { it.isDigit() || it in 'A'..'F' }) continue
            frames.add(ByteArray(payload.length / 2) { i ->
                payload.substring(i * 2, i * 2 + 2).toInt(16).toByte()
            })
        }
        return frames
    }

    /** Passively capture + decode one round of broadcast frames. Read-only. */
    fun snapshot(
        session: Elm327Session,
        onProgress: (String) -> Unit = {},
    ): List<EnhancedReading> {
        val readings = mutableListOf<EnhancedReading>()
        val seen = mutableSetOf<String>()
        session.at("ATH1")  // monitored frames must show their CAN ids
        try {
            for ((canId, window) in SNAPSHOT_IDS) {
                onProgress("Listening for %03X…".format(canId))
                session.at("ATCRA%03X".format(canId))
                var text = session.monitor("ATMA", window)
                text += session.stopMonitor()
                val frames = parseMonitorText(text, canId)
                if (frames.isNotEmpty()) {
                    for (reading in decodeFrame(canId, frames.last())) {
                        if (seen.add(reading.name)) readings.add(reading)
                    }
                }
            }
        } finally {
            for (cmd in listOf("ATCRA", "ATH0")) {
                try {
                    session.at(cmd)
                } catch (_: Exception) {
                }
            }
        }
        return readings
    }
}
