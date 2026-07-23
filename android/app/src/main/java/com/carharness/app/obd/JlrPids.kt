package com.carharness.app.obd

/**
 * Jaguar / Land Rover live "extras" over a plain ELM327
 * (docs/private/jlr-research-modules.md §5, §10b).
 *
 * Two mechanisms, both read-only:
 *  (a) The Old Jaguar Group on-car PID captures (X351 / XK X150 petrols,
 *      "should also work" on the diesels): engine oil temperature, measured oil
 *      LEVEL in mm and calculated oil VOLUME in litres (the dipstick-less cars
 *      carry these), boost, post-intercooler charge temp on the engine 0x7E0;
 *      ZF gearbox oil temperature on 0x7E1; rear e-diff oil temperature on
 *      0x795. UDS service 0x22, one DID at a time. SINGLE-SOURCE capture, so
 *      [OJG_CAVEAT] rides with the readings. -> [JLR_DIDS]
 *  (b) A curated generic Mode-01 set on the engine — the honest fallback a
 *      DoIP-era or gateway-filtered car still answers. -> [OBD_PIDS]
 *
 * Air-suspension corner heights, DPF soot mass and battery SoC are GENUINE GAPS
 * (research §5a): no JLR DID is publicly captured, so nothing is invented here.
 *
 * Mirror of backend/profiles/jlr/pids.py — keep the two in step.
 */

/** One JLR UDS live value: module + DID + decode of the payload after `62 <did>`. */
class JlrDid(
    val module: Int,
    val did: Int,
    val name: String,
    val unit: String,
    val decode: (ByteArray) -> String?,
)

/** A generic Mode-01 value: payload after `41 <pid>` -> display string, or null. */
class JlrObdPid(
    val pid: Int,
    val name: String,
    val unit: String,
    val decode: (ByteArray) -> String?,
)

object JlrPids {

    // Response = request + 8 for all three (incl. 0x795 -> 0x79D), so no id_map.
    const val ENGINE = 0x7E0
    const val TRANS = 0x7E1
    const val RDCM = 0x795

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

    // The Old Jaguar Group captures (research §10b, single on-car source,
    // UNVERIFIED scalings) — formulas exactly as captured, sanity-bounded.
    val JLR_DIDS: List<JlrDid> = listOf(
        JlrDid(ENGINE, 0x03F3, "Engine oil temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 215.0)),
        JlrDid(ENGINE, 0x03E6, "Engine oil level (measured)", "mm", byteVal(1.0, 0.0, lo = 0.0, hi = 120.0)),
        JlrDid(ENGINE, 0x03F2, "Engine oil volume (calculated)", "L", byteVal(0.1, 0.0, dp = 1, lo = 0.0, hi = 12.0)),
        JlrDid(ENGINE, 0x033E, "Boost / manifold pressure", "kPa", byteVal(1.0, 0.0, lo = 0.0, hi = 350.0)),
        JlrDid(ENGINE, 0x0520, "Charge air temp (post-intercooler)", "°C", byteVal(1.0, 0.0, lo = 0.0, hi = 150.0)),
        JlrDid(TRANS, 0x1E69, "Gearbox (ZF) oil temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 215.0)),
        JlrDid(RDCM, 0x1E8A, "Rear e-diff oil temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 215.0)),
    )

    // Curated generic Mode-01 fallback set (standard ISO 15031 PIDs on the
    // engine ECU) — what a DoIP-era / gateway-filtered car still answers.
    val OBD_PIDS: List<JlrObdPid> = listOf(
        JlrObdPid(0x05, "Coolant temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 215.0)),
        JlrObdPid(0x5C, "Engine oil temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 215.0)),
        JlrObdPid(0x42, "Battery voltage (control module)", "V", u16Scaled(1.0 / 1000, dp = 2, lo = 0.0, hi = 32.0)),
        JlrObdPid(0x2F, "Fuel level", "%", byteVal(100.0 / 255, dp = 0, lo = 0.0, hi = 100.0)),
    )

    /** The single-source caveat — shown right after the JLR-specific readings. */
    val OJG_CAVEAT = EnhancedReading(
        "Note",
        "These JLR values are from a single on-car X351/XK petrol PID capture — " +
            "verify the scaling against the car before trusting absolute figures.",
        "",
    )
}
