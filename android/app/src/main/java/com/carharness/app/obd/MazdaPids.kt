package com.carharness.app.obd

/**
 * Mazda-specific mode-22 (ReadDataByIdentifier) live values readable over a
 * plain ELM327 on HS-CAN — the "enhanced" data generic OBD-II doesn't carry.
 * Compiled from community sources (OBDb signal sets, the SkyActiv Torque CSV,
 * Mazdas247 live-verified threads) for SkyActiv-era cars; grouped by module so
 * the driver re-aims once per module. An entry this car doesn't support simply
 * never answers and is skipped, so unverified entries are harmless.
 *
 * Mirror of backend/profiles/mazda/pids.py — keep the two in step.
 */

class MazdaDid(
    val reqId: Int,          // module request id (PCM 0x7E0, TCM 0x7E1, IC 0x720, FBCM 0x726)
    val did: Int,            // 16-bit data identifier
    val name: String,
    val unit: String,
    val decode: (ByteArray) -> String?,  // payload after 62 <did> -> display value
)

object MazdaPids {

    private const val KM_TO_MI = 0.621371

    private fun u8(b: ByteArray, i: Int = 0): Int? =
        if (b.size > i) b[i].toInt() and 0xFF else null

    private fun u16(b: ByteArray, i: Int = 0): Int? =
        if (b.size > i + 1) ((b[i].toInt() and 0xFF) shl 8) or (b[i + 1].toInt() and 0xFF) else null

    private fun s16(b: ByteArray, i: Int = 0): Int? =
        u16(b, i)?.let { if (it > 0x7FFF) it - 0x10000 else it }

    private fun fmt(v: Double, dp: Int): String = "%.${dp}f".format(v)

    /** u16/s16 big-endian -> scaled value string, or null outside sanity bounds. */
    private fun scaled(
        scale: Double, offset: Double = 0.0, dp: Int = 1,
        lo: Double = Double.NEGATIVE_INFINITY, hi: Double = Double.POSITIVE_INFINITY,
        signed: Boolean = false,
    ): (ByteArray) -> String? = { b ->
        val raw = if (signed) s16(b) else u16(b)
        raw?.let {
            val v = it * scale + offset
            if (v in lo..hi) fmt(v, dp) else null
        }
    }

    private fun byteVal(
        scale: Double = 1.0, offset: Double = 0.0, dp: Int = 0,
        lo: Double = Double.NEGATIVE_INFINITY, hi: Double = Double.POSITIVE_INFINITY,
    ): (ByteArray) -> String? = { b ->
        u8(b)?.let {
            val v = it * scale + offset
            if (v in lo..hi) fmt(v, dp) else null
        }
    }

    private val gearAuto: (ByteArray) -> String? = { b ->
        when (val v = u8(b)) {
            null -> null
            0x50 -> "N"; 0x60 -> "R"; 0x70 -> "P"
            in 1..8 -> v.toString()
            else -> null
        }
    }

    private val lockup: (ByteArray) -> String? = { b ->
        u8(b)?.let { if (it != 0) "locked" else "open" }
    }

    private val odoCluster: (ByteArray) -> String? = { b ->
        if (b.size < 3) null else {
            val km = (((b[0].toInt() and 0xFF) shl 16) or
                ((b[1].toInt() and 0xFF) shl 8) or (b[2].toInt() and 0xFF)) / 10.0
            if (km > 0 && km < 2_000_000) {
                "%,.0f mi (%,.0f km)".format(km * KM_TO_MI, km)
            } else null
        }
    }

    private fun milesFromKm(scaleKm: Double): (ByteArray) -> String? = { b ->
        u16(b)?.let { fmt(it * scaleKm * KM_TO_MI, 0) }
    }

    val PCM_DIDS: List<MazdaDid> = listOf(
        // ---- PCM @ 0x7E0 ---------------------------------------------------
        MazdaDid(0x7E0, 0x1310, "Engine oil temperature", "°C", scaled(0.01, -40.0, lo = -40.0, hi = 160.0)),
        MazdaDid(0x7E0, 0x0415, "Engine oil pressure", "kPa", scaled(1.0, dp = 1, lo = 0.0, hi = 1500.0, signed = true)),
        MazdaDid(0x7E0, 0x0202, "Stored faults (PCM)", "", byteVal(lo = 0.0, hi = 200.0)),
        MazdaDid(0x7E0, 0x0301, "MAP sensor voltage", "V", scaled(0.001, dp = 2, lo = 0.0, hi = 5.1)),
        MazdaDid(0x7E0, 0x16E9, "Alternator output", "V", scaled(0.125, lo = 0.0, hi = 20.0)),
        MazdaDid(0x7E0, 0x16E8, "Alternator field duty", "%", scaled(200.0 / 65535, lo = 0.0, hi = 101.0)),
        MazdaDid(0x7E0, 0x03E8, "Knock / octane adjust ratio", "", scaled(1.0 / 16384, dp = 3, lo = -1.1, hi = 1.1, signed = true)),
        MazdaDid(0x7E0, 0x0353, "Barometric pressure", "kPa", scaled(0.25, lo = 60.0, hi = 110.0)),
        MazdaDid(0x7E0, 0x057D, "Ambient air temp (PCM)", "°C", byteVal(0.5, -40.0, dp = 1, lo = -40.0, hi = 70.0)),
        MazdaDid(0x7E0, 0x038F, "Coolant temp (hi-res)", "°C", scaled(1.0 / 64, signed = true, lo = -40.0, hi = 150.0)),
        MazdaDid(0x7E0, 0x0519, "Distance since oil change", "mi", milesFromKm(4.0)),
        MazdaDid(0x7E0, 0x0542, "Oil change interval", "mi", milesFromKm(1.0)),
        MazdaDid(0x7E0, 0x1172, "Battery voltage (PCM)", "V", byteVal(1.0 / 16, dp = 1, lo = 5.0, hi = 16.0)),
        MazdaDid(0x7E0, 0x16C1, "Fuel level (PCM)", "%", scaled(1.0 / 328, lo = 0.0, hi = 102.0)),
        MazdaDid(0x7E0, 0x096E, "Idle speed target", "rpm", scaled(0.25, dp = 0, lo = 0.0, hi = 3000.0)),
        MazdaDid(0x7E0, 0x0307, "Fuel pump commanded duty", "%", scaled(1.0 / 327.68, lo = 0.0, hi = 101.0)),
        // ---- TCM @ 0x7E1 (automatics; a manual just never answers) ---------
        MazdaDid(0x7E1, 0x1E1C, "Gearbox (ATF) temperature", "°C", scaled(1.0 / 80, lo = -40.0, hi = 160.0)),
        MazdaDid(0x7E1, 0x1E12, "Gear engaged (auto)", "", gearAuto),
        MazdaDid(0x7E1, 0x1E24, "Torque converter", "", lockup),
        MazdaDid(0x7E1, 0x1EE4, "Engine torque (TCM)", "Nm", scaled(0.02, dp = 0, signed = true, lo = -200.0, hi = 500.0)),
        // ---- Instrument cluster @ 0x720 ------------------------------------
        MazdaDid(0x720, 0x61B1, "Fuel in tank (cluster)", "L", byteVal(dp = 1, lo = 0.0, hi = 80.0)),
        MazdaDid(0x720, 0x404C, "Odometer (cluster)", "", odoCluster),
        MazdaDid(0x720, 0x2A05, "Tyre pressure 1", "psi", byteVal(0.199, dp = 1, lo = 2.0, hi = 60.0)),
        MazdaDid(0x720, 0x2A06, "Tyre pressure 2", "psi", byteVal(0.199, dp = 1, lo = 2.0, hi = 60.0)),
        MazdaDid(0x720, 0x2A07, "Tyre pressure 3", "psi", byteVal(0.199, dp = 1, lo = 2.0, hi = 60.0)),
        MazdaDid(0x720, 0x2A08, "Tyre pressure 4", "psi", byteVal(0.199, dp = 1, lo = 2.0, hi = 60.0)),
        // ---- Front body control module @ 0x726 -----------------------------
        MazdaDid(0x726, 0x4028, "12V battery charge", "%", byteVal(lo = 0.0, hi = 100.0)),
        MazdaDid(0x726, 0x4029, "12V battery temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 100.0)),
        MazdaDid(0x726, 0x402A, "12V battery voltage", "V", byteVal(0.05, 6.0, dp = 1, lo = 5.0, hi = 17.0)),
        MazdaDid(0x726, 0x402B, "12V battery current", "A", byteVal(1.0, -127.0, lo = -130.0, hi = 130.0)),
        MazdaDid(0x726, 0x4027, "12V battery age", "days", scaled(1.0, dp = 0, lo = 0.0, hi = 8000.0)),
    )
}
