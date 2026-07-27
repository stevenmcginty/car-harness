package com.carharness.app.obd

/**
 * Mercedes-Benz live "extras" over a plain ELM327 (docs/private/mercedes-research.md §4).
 *
 * Two mechanisms, both read-only:
 *  (a) The one VERIFIED Mercedes-specific value the generic dashboard doesn't
 *      carry: the 722.9 / 9G automatic-transmission (ATF) oil temperature on the
 *      gearbox ECU 0x7E1. Read as KWP `21 30` (722.9 era) or UDS `22 2130` (later
 *      9G); scaling A - 50 = °C. The exact offset (-50 vs -40) needs a real-car
 *      confirm — a warm gearbox idles ~40-60 °C — so [ATF_CAVEAT] rides with it.
 *      The read itself lives in MercedesUds.readEnhanced (it addresses 0x7E1).
 *  (b) A curated generic Mode-01 set (coolant, engine oil temp, control-module
 *      voltage, ambient, fuel level) on the engine ECU 0x7E0 — standard ISO 15031
 *      PIDs (VERIFIED), the honest baseline the research says answers. -> [OBD_PIDS].
 *
 * DPF soot / regen and AdBlue level are GENUINE GAPS (research §4): no header +
 * hex PID + scaling is publicly published for Mercedes, so nothing is invented
 * here — DPF/SCR health shows through the fault codes read by the scan instead.
 *
 * Mirror of backend/profiles/mercedes/pids.py — keep the two in step.
 */

/** A generic Mode-01 value: payload after `41 <pid>` -> display string, or null. */
class MercedesObdPid(
    val pid: Int,
    val name: String,
    val unit: String,
    val decode: (ByteArray) -> String?,
)

object MercedesPids {

    // Response = request + 8, so no id_map is needed for either module.
    const val ENGINE = 0x7E0
    const val TRANS = 0x7E1

    private fun u8(b: ByteArray, i: Int = 0): Int? =
        if (b.size > i) b[i].toInt() and 0xFF else null

    private fun u16(b: ByteArray, i: Int = 0): Int? =
        if (b.size > i + 1) ((b[i].toInt() and 0xFF) shl 8) or (b[i + 1].toInt() and 0xFF) else null

    private fun fmt(v: Double, dp: Int): String = "%.${dp}f".format(v)

    /** First payload byte (A) -> scaled string, or null outside sanity bounds. */
    private fun byteVal(
        scale: Double = 1.0, offset: Double = 0.0, dp: Int = 0,
        lo: Double = Double.NEGATIVE_INFINITY, hi: Double = Double.POSITIVE_INFINITY,
    ): (ByteArray) -> String? = { b ->
        u8(b)?.let { val v = it * scale + offset; if (v in lo..hi) fmt(v, dp) else null }
    }

    /** Big-endian A*256+B -> scaled string, or null outside sanity bounds. */
    private fun u16Scaled(
        scale: Double, offset: Double = 0.0, dp: Int = 1,
        lo: Double = Double.NEGATIVE_INFINITY, hi: Double = Double.POSITIVE_INFINITY,
    ): (ByteArray) -> String? = { b ->
        u16(b)?.let { val v = it * scale + offset; if (v in lo..hi) fmt(v, dp) else null }
    }

    // Curated generic Mode-01 set (research §4): standard ISO 15031 PIDs on the
    // engine ECU, with their own scaling.
    val OBD_PIDS: List<MercedesObdPid> = listOf(
        MercedesObdPid(0x05, "Coolant temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 215.0)),
        MercedesObdPid(0x5C, "Engine oil temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 215.0)),
        MercedesObdPid(0x42, "Battery voltage (control module)", "V", u16Scaled(1.0 / 1000, dp = 2, lo = 0.0, hi = 32.0)),
        MercedesObdPid(0x46, "Ambient air temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 100.0)),
        MercedesObdPid(0x2F, "Fuel level", "%", byteVal(100.0 / 255, dp = 0, lo = 0.0, hi = 100.0)),
    )

    /** The 722.9/9G ATF temp caveat — shown right after the reading so a
     *  warm-gearbox sanity check on a real car settles the -50 vs -40 offset. */
    val ATF_CAVEAT = EnhancedReading(
        "Note",
        "ATF temp is community-verified on the 722.9; scaling is A-50 = °C but the " +
            "offset (-50 vs -40) needs a real-car confirm — a warm gearbox idles " +
            "~40-60 °C, so a reading ~10 °C high means the offset is -40.",
        "",
    )
}
