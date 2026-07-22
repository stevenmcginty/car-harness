package com.carharness.app.obd

/** Standard OBD-II mode-01 PIDs (SAE J1979 formulas — the same maths Torque uses). */
data class PidDef(
    val pid: Int,
    val key: String,
    val label: String,
    val unit: String,
    val nbytes: Int,
    val decode: (ByteArray) -> Double
)

object Pids {
    private fun u(b: Byte) = b.toInt() and 0xFF

    val all: List<PidDef> = listOf(
        PidDef(0x04, "load", "Engine load", "%", 1) { d -> u(d[0]) * 100.0 / 255.0 },
        PidDef(0x05, "coolant", "Coolant temp", "°C", 1) { d -> u(d[0]) - 40.0 },
        PidDef(0x06, "stft1", "Short fuel trim B1", "%", 1) { d -> (u(d[0]) - 128) * 100.0 / 128.0 },
        PidDef(0x07, "ltft1", "Long fuel trim B1", "%", 1) { d -> (u(d[0]) - 128) * 100.0 / 128.0 },
        PidDef(0x0B, "map", "Intake manifold", "kPa", 1) { d -> u(d[0]).toDouble() },
        PidDef(0x0C, "rpm", "Engine RPM", "rpm", 2) { d -> (u(d[0]) * 256 + u(d[1])) / 4.0 },
        PidDef(0x0D, "speed", "Vehicle speed", "km/h", 1) { d -> u(d[0]).toDouble() },
        PidDef(0x0E, "timing", "Timing advance", "°", 1) { d -> u(d[0]) / 2.0 - 64 },
        PidDef(0x0F, "intake_temp", "Intake air temp", "°C", 1) { d -> u(d[0]) - 40.0 },
        PidDef(0x10, "maf", "MAF air flow", "g/s", 2) { d -> (u(d[0]) * 256 + u(d[1])) / 100.0 },
        PidDef(0x11, "throttle", "Throttle", "%", 1) { d -> u(d[0]) * 100.0 / 255.0 },
        PidDef(0x1F, "runtime", "Run time", "s", 2) { d -> (u(d[0]) * 256 + u(d[1])).toDouble() },
        PidDef(0x21, "mil_distance", "Distance with MIL", "km", 2) { d -> (u(d[0]) * 256 + u(d[1])).toDouble() },
        PidDef(0x2F, "fuel_level", "Fuel level", "%", 1) { d -> u(d[0]) * 100.0 / 255.0 },
        PidDef(0x33, "baro", "Barometric", "kPa", 1) { d -> u(d[0]).toDouble() },
        PidDef(0x42, "module_voltage", "Module voltage", "V", 2) { d -> (u(d[0]) * 256 + u(d[1])) / 1000.0 },
        PidDef(0x46, "ambient", "Ambient temp", "°C", 1) { d -> u(d[0]) - 40.0 },
        PidDef(0x5C, "oil_temp", "Oil temp", "°C", 1) { d -> u(d[0]) - 40.0 },
        PidDef(0x5E, "fuel_rate", "Fuel rate", "L/h", 2) { d -> (u(d[0]) * 256 + u(d[1])) / 20.0 },
    )

    val byKey: Map<String, PidDef> = all.associateBy { it.key }
    val byPid: Map<Int, PidDef> = all.associateBy { it.pid }

    val dashboardKeys = listOf(
        "rpm", "speed", "coolant", "load", "throttle", "intake_temp",
        "maf", "fuel_level", "oil_temp", "module_voltage"
    )

    /** Query the 01 00/20/40 support bitmaps. Returns the set of supported PIDs. */
    fun readSupported(session: Elm327Session): Set<Int> {
        val supported = mutableSetOf<Int>()
        var base = 0x00
        while (true) {
            val payloads = try {
                session.query(0x01, base, 6000)
            } catch (_: Exception) {
                break
            }
            val merged = mutableSetOf<Int>()
            for (payload in payloads) {
                if (payload.size >= 6 && (payload[1].toInt() and 0xFF) == base) {
                    for (i in 0 until 4) {
                        val byte = payload[2 + i].toInt() and 0xFF
                        for (bit in 0 until 8) {
                            if (byte and (0x80 shr bit) != 0) supported.add(base + i * 8 + bit + 1)
                        }
                    }
                }
            }
            supported.addAll(merged)
            val next = base + 0x20
            if (next > 0x40 || !supported.contains(next)) break
            base = next
        }
        return supported
    }
}
