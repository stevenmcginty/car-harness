package com.carharness.app.obd

/**
 * Standard OBD-II mode-01 PIDs: SAE J1979 formulas, the same maths Torque uses.
 * Ported from backend/obd/pids.py so phone and desktop read the same catalogue —
 * keep the two in step when adding rows.
 *
 * Numeric readings only: the enumerated PIDs (fuel system status 0x03, fuel
 * type 0x51, OBD standard 0x1C) answer with a code rather than a measurement
 * and LiveReading carries a Double, so they are deliberately absent here.
 *
 * Also deliberately absent, same as the backend: 0x66/0x6B/0x73/0x74 and
 * 0x7A/0x7B/0x7C (DPF pressure/temperature) — composite rows whose byte layout
 * is not settled across public sources. A guessed DPF number on a dealer's
 * screen is worse than a missing one.
 *
 * lo/hi are sanity bounds: a value outside them is treated as "this car does
 * not really support that PID" rather than displayed. Clone dongles answer
 * plenty of PIDs with rubbish.
 */
data class PidDef(
    val pid: Int,
    val key: String,
    val label: String,
    val unit: String,
    val nbytes: Int,
    val group: String = Pids.ENGINE,
    val lo: Double? = null,
    val hi: Double? = null,
    val decode: (ByteArray) -> Double?
)

object Pids {
    const val ENGINE = "Engine"
    const val FUEL_AIR = "Fuel & air"
    const val TURBO = "Turbo & exhaust"
    const val TEMPS = "Temperatures"
    const val EMISSIONS = "Emissions"
    const val ELECTRICAL = "Electrical"
    const val TRIP = "Trip & diagnostics"

    /** Display order — one section per group, same order as the desktop page. */
    val groupOrder = listOf(ENGINE, FUEL_AIR, TURBO, TEMPS, EMISSIONS, ELECTRICAL, TRIP)

    private fun u(b: Byte) = b.toInt() and 0xFF
    private fun u16(d: ByteArray, i: Int = 0) = u(d[i]) * 256 + u(d[i + 1])
    private fun pct255(d: ByteArray, i: Int = 0) = u(d[i]) * 100.0 / 255.0
    private fun fuelTrim(d: ByteArray, i: Int = 0) = (u(d[i]) - 128) * 100.0 / 128.0

    val all: List<PidDef> = buildList {
        // ---- Engine --------------------------------------------------------
        add(PidDef(0x04, "load", "Engine load", "%", 1, ENGINE, 0.0, 100.0) { d -> pct255(d) })
        add(PidDef(0x0C, "rpm", "Engine RPM", "rpm", 2, ENGINE, 0.0, 16000.0) { d -> u16(d) / 4.0 })
        add(PidDef(0x0D, "speed", "Vehicle speed", "km/h", 1, ENGINE, 0.0, 255.0) { d -> u(d[0]).toDouble() })
        add(PidDef(0x0E, "timing", "Timing advance", "°", 1, ENGINE, -64.0, 64.0) { d -> u(d[0]) / 2.0 - 64 })
        add(PidDef(0x11, "throttle", "Throttle", "%", 1, ENGINE, 0.0, 100.0) { d -> pct255(d) })
        add(PidDef(0x1F, "runtime", "Run time", "s", 2, ENGINE, 0.0, 65535.0) { d -> u16(d).toDouble() })
        add(PidDef(0x43, "abs_load", "Absolute load", "%", 2, ENGINE, 0.0, 25700.0) { d -> u16(d) * 100.0 / 255.0 })
        add(PidDef(0x45, "throttle_rel", "Relative throttle", "%", 1, ENGINE, 0.0, 100.0) { d -> pct255(d) })
        add(PidDef(0x47, "throttle_b", "Throttle position B", "%", 1, ENGINE, 0.0, 100.0) { d -> pct255(d) })
        add(PidDef(0x48, "throttle_c", "Throttle position C", "%", 1, ENGINE, 0.0, 100.0) { d -> pct255(d) })
        add(PidDef(0x49, "pedal_d", "Accelerator pedal D", "%", 1, ENGINE, 0.0, 100.0) { d -> pct255(d) })
        add(PidDef(0x4A, "pedal_e", "Accelerator pedal E", "%", 1, ENGINE, 0.0, 100.0) { d -> pct255(d) })
        add(PidDef(0x4B, "pedal_f", "Accelerator pedal F", "%", 1, ENGINE, 0.0, 100.0) { d -> pct255(d) })
        add(PidDef(0x5A, "pedal_rel", "Relative pedal position", "%", 1, ENGINE, 0.0, 100.0) { d -> pct255(d) })
        add(PidDef(0x4C, "throttle_cmd", "Commanded throttle actuator", "%", 1, ENGINE, 0.0, 100.0) { d -> pct255(d) })
        add(PidDef(0x61, "torque_demand", "Driver's demanded torque", "%", 1, ENGINE, -125.0, 130.0) { d -> (u(d[0]) - 125).toDouble() })
        add(PidDef(0x62, "torque_actual", "Actual engine torque", "%", 1, ENGINE, -125.0, 130.0) { d -> (u(d[0]) - 125).toDouble() })
        add(PidDef(0x63, "torque_ref", "Engine reference torque", "Nm", 2, ENGINE, 0.0, 65535.0) { d -> u16(d).toDouble() })

        // ---- Fuel & air ----------------------------------------------------
        add(PidDef(0x06, "stft1", "Short fuel trim B1", "%", 1, FUEL_AIR, -100.0, 100.0) { d -> fuelTrim(d) })
        add(PidDef(0x07, "ltft1", "Long fuel trim B1", "%", 1, FUEL_AIR, -100.0, 100.0) { d -> fuelTrim(d) })
        add(PidDef(0x08, "stft2", "Short fuel trim B2", "%", 1, FUEL_AIR, -100.0, 100.0) { d -> fuelTrim(d) })
        add(PidDef(0x09, "ltft2", "Long fuel trim B2", "%", 1, FUEL_AIR, -100.0, 100.0) { d -> fuelTrim(d) })
        add(PidDef(0x0A, "fuel_pressure", "Fuel pressure (gauge)", "kPa", 1, FUEL_AIR, 0.0, 765.0) { d -> u(d[0]) * 3.0 })
        add(PidDef(0x0B, "map", "Intake manifold", "kPa", 1, FUEL_AIR, 0.0, 255.0) { d -> u(d[0]).toDouble() })
        add(PidDef(0x10, "maf", "MAF air flow", "g/s", 2, FUEL_AIR, 0.0, 655.0) { d -> u16(d) / 100.0 })
        add(PidDef(0x50, "maf_max", "Maximum MAF", "g/s", 1, FUEL_AIR, 0.0, 2550.0) { d -> u(d[0]) * 10.0 })
        add(PidDef(0x22, "rail_pressure_rel", "Fuel rail pressure (vs manifold)", "kPa", 2, FUEL_AIR, 0.0, 5178.0) { d -> u16(d) * 0.079 })
        add(PidDef(0x23, "rail_pressure", "Fuel rail gauge pressure", "kPa", 2, FUEL_AIR, 0.0, 655350.0) { d -> u16(d) * 10.0 })
        add(PidDef(0x59, "rail_pressure_abs", "Fuel rail absolute pressure", "kPa", 2, FUEL_AIR, 0.0, 655350.0) { d -> u16(d) * 10.0 })
        add(PidDef(0x2F, "fuel_level", "Fuel level", "%", 1, FUEL_AIR, 0.0, 100.0) { d -> pct255(d) })
        add(PidDef(0x5E, "fuel_rate", "Fuel rate", "L/h", 2, FUEL_AIR, 0.0, 3276.0) { d -> u16(d) / 20.0 })
        add(PidDef(0x52, "ethanol", "Ethanol content", "%", 1, FUEL_AIR, 0.0, 100.0) { d -> pct255(d) })
        add(PidDef(0x44, "lambda_cmd", "Commanded lambda", "", 2, FUEL_AIR, 0.0, 2.0) { d -> u16(d) * 2.0 / 65536 })
        add(PidDef(0x5D, "injection_timing", "Fuel injection timing", "°", 2, FUEL_AIR, -210.0, 302.0) { d -> (u16(d) - 26880) / 128.0 })

        // ---- Turbo & exhaust -----------------------------------------------
        // 0x78/0x79: byte A is a presence bitmap, then four 16-bit temperatures.
        // Absent sensors read 0xFFFF and fall outside the bounds on their own.
        for (n in 1..4) {
            add(PidDef(0x78, "egt_b1_s$n", "Exhaust gas temp B1 S$n", "°C", 1 + 2 * n, TURBO, -40.0, 6513.0) { d -> u16(d, 1 + 2 * (n - 1)) / 10.0 - 40 })
            add(PidDef(0x79, "egt_b2_s$n", "Exhaust gas temp B2 S$n", "°C", 1 + 2 * n, TURBO, -40.0, 6513.0) { d -> u16(d, 1 + 2 * (n - 1)) / 10.0 - 40 })
        }

        // ---- Temperatures --------------------------------------------------
        add(PidDef(0x05, "coolant", "Coolant temp", "°C", 1, TEMPS, -40.0, 215.0) { d -> (u(d[0]) - 40).toDouble() })
        add(PidDef(0x0F, "intake_temp", "Intake air temp", "°C", 1, TEMPS, -40.0, 215.0) { d -> (u(d[0]) - 40).toDouble() })
        add(PidDef(0x46, "ambient", "Ambient temp", "°C", 1, TEMPS, -40.0, 215.0) { d -> (u(d[0]) - 40).toDouble() })
        add(PidDef(0x5C, "oil_temp", "Oil temp", "°C", 1, TEMPS, -40.0, 215.0) { d -> (u(d[0]) - 40).toDouble() })
        add(PidDef(0x3C, "cat_temp_b1s1", "Catalyst temp B1 S1", "°C", 2, TEMPS, -40.0, 6513.0) { d -> u16(d) / 10.0 - 40 })
        add(PidDef(0x3D, "cat_temp_b2s1", "Catalyst temp B2 S1", "°C", 2, TEMPS, -40.0, 6513.0) { d -> u16(d) / 10.0 - 40 })
        add(PidDef(0x3E, "cat_temp_b1s2", "Catalyst temp B1 S2", "°C", 2, TEMPS, -40.0, 6513.0) { d -> u16(d) / 10.0 - 40 })
        add(PidDef(0x3F, "cat_temp_b2s2", "Catalyst temp B2 S2", "°C", 2, TEMPS, -40.0, 6513.0) { d -> u16(d) / 10.0 - 40 })

        // ---- Emissions -----------------------------------------------------
        add(PidDef(0x2C, "egr_cmd", "Commanded EGR", "%", 1, EMISSIONS, 0.0, 100.0) { d -> pct255(d) })
        add(PidDef(0x2D, "egr_error", "EGR error", "%", 1, EMISSIONS, -100.0, 100.0) { d -> fuelTrim(d) })
        add(PidDef(0x2E, "evap_purge", "Commanded evap purge", "%", 1, EMISSIONS, 0.0, 100.0) { d -> pct255(d) })
        add(PidDef(0x32, "evap_pressure", "Evap vapour pressure", "Pa", 2, EMISSIONS, -8192.0, 8192.0) { d ->
            val raw = u16(d)
            (if (raw > 32767) raw - 65536 else raw) / 4.0
        })
        add(PidDef(0x53, "evap_pressure_abs", "Evap vapour pressure (absolute)", "kPa", 2, EMISSIONS, 0.0, 328.0) { d -> u16(d) / 200.0 })
        add(PidDef(0x55, "o2_trim_st_b1", "Secondary O2 short trim B1/B3", "%", 1, EMISSIONS, -100.0, 100.0) { d -> fuelTrim(d) })
        add(PidDef(0x56, "o2_trim_lt_b1", "Secondary O2 long trim B1/B3", "%", 1, EMISSIONS, -100.0, 100.0) { d -> fuelTrim(d) })
        add(PidDef(0x57, "o2_trim_st_b2", "Secondary O2 short trim B2/B4", "%", 1, EMISSIONS, -100.0, 100.0) { d -> fuelTrim(d) })
        add(PidDef(0x58, "o2_trim_lt_b2", "Secondary O2 long trim B2/B4", "%", 1, EMISSIONS, -100.0, 100.0) { d -> fuelTrim(d) })

        // O2 sensors: 0x14-0x1B narrowband, 0x24-0x2B wideband-voltage,
        // 0x34-0x3B wideband-current. Bank 1 sensors 1-4 then bank 2 sensors
        // 1-4. Generated, not hand-typed — 24 PIDs x 2 readings is 48 rows
        // where one wrong copy-paste would be invisible.
        for (i in 0..7) {
            val bank = i / 4 + 1
            val sensor = i % 4 + 1
            val tag = "b${bank}s$sensor"
            val name = "B$bank S$sensor"
            add(PidDef(0x14 + i, "o2_${tag}_v", "O2 $name voltage", "V", 2, EMISSIONS, 0.0, 1.275) { d -> u(d[0]) / 200.0 })
            add(PidDef(0x14 + i, "o2_${tag}_trim", "O2 $name fuel trim", "%", 2, EMISSIONS, -100.0, 100.0) { d ->
                if (u(d[1]) == 0xFF) null else fuelTrim(d, 1)
            })
            add(PidDef(0x24 + i, "o2_${tag}_lambda", "O2 $name lambda", "", 4, EMISSIONS, 0.0, 2.0) { d -> u16(d) * 2.0 / 65536 })
            add(PidDef(0x24 + i, "o2_${tag}_wb_v", "O2 $name voltage (wideband)", "V", 4, EMISSIONS, 0.0, 8.0) { d -> u16(d, 2) * 8.0 / 65536 })
            add(PidDef(0x34 + i, "o2_${tag}_lambda_i", "O2 $name lambda (current)", "", 4, EMISSIONS, 0.0, 2.0) { d -> u16(d) * 2.0 / 65536 })
            add(PidDef(0x34 + i, "o2_${tag}_current", "O2 $name current", "mA", 4, EMISSIONS, -128.0, 128.0) { d -> u16(d, 2) / 256.0 - 128 })
        }

        // ---- Electrical ----------------------------------------------------
        add(PidDef(0x42, "module_voltage", "Module voltage", "V", 2, ELECTRICAL, 0.0, 65.5) { d -> u16(d) / 1000.0 })
        add(PidDef(0x5B, "hybrid_battery", "Hybrid battery remaining", "%", 1, ELECTRICAL, 0.0, 100.0) { d -> pct255(d) })

        // ---- Trip & diagnostics --------------------------------------------
        add(PidDef(0x33, "baro", "Barometric", "kPa", 1, TRIP, 0.0, 255.0) { d -> u(d[0]).toDouble() })
        add(PidDef(0x21, "mil_distance", "Distance with MIL", "km", 2, TRIP, 0.0, 65535.0) { d -> u16(d).toDouble() })
        add(PidDef(0x4D, "mil_minutes", "Time run with MIL on", "min", 2, TRIP, 0.0, 65535.0) { d -> u16(d).toDouble() })
        // "How recently was this car's fault memory wiped?" — worth more to a
        // buyer than most live data.
        add(PidDef(0x30, "warmups_since_clear", "Warm-ups since codes cleared", "count", 1, TRIP, 0.0, 255.0) { d -> u(d[0]).toDouble() })
        add(PidDef(0x31, "distance_since_clear", "Distance since codes cleared", "km", 2, TRIP, 0.0, 65535.0) { d -> u16(d).toDouble() })
        add(PidDef(0x4E, "minutes_since_clear", "Time since codes cleared", "min", 2, TRIP, 0.0, 65535.0) { d -> u16(d).toDouble() })
    }

    val byKey: Map<String, PidDef> = all.associateBy { it.key }

    /** First row wins, matching the backend — several readings can share a PID. */
    val byPid: Map<Int, PidDef> = buildMap {
        all.forEach { if (it.pid !in this) put(it.pid, it) }
    }

    val dashboardKeys = listOf(
        "rpm", "speed", "coolant", "load", "throttle", "intake_temp",
        "maf", "fuel_level", "oil_temp", "module_voltage"
    )

    /** Query the 01 00/20/…/C0 support bitmaps. Returns the set of supported
     * PIDs — only what the car actually reports. Each bitmap's top bit says
     * whether the next block exists. */
    fun readSupported(session: Elm327Session): Set<Int> {
        val supported = mutableSetOf<Int>()
        var base = 0x00
        while (base <= 0xC0) {
            val payloads = try {
                session.query(0x01, base, 6000)
            } catch (_: Exception) {
                break
            }
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
            val next = base + 0x20
            if (next > 0xC0 || !supported.contains(next)) break
            base = next
        }
        return supported
    }
}
