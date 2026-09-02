package com.carharness.app.obd

/**
 * Ford-specific mode-22 (ReadDataByIdentifier) live values readable over a
 * plain ELM327 on HS-CAN — the "enhanced" data generic OBD-II doesn't carry.
 * Taken from Ford's own IDS MDX diesel-PCM tables, so the names and the
 * resolutions are the manufacturer's, not a guess. Payload bytes are the ones
 * after ``62 <hi> <lo>``, big-endian.
 *
 * An entry this car doesn't support simply never answers and is skipped, so a
 * DID the fitted PCM doesn't implement is harmless.
 */

class FordDid(
    val reqId: Int,          // module request id (PCM 0x7E0, TCM 0x7E1)
    val did: Int,            // 16-bit data identifier
    val name: String,
    val unit: String,
    val decode: (ByteArray) -> String?,  // payload after 62 <did> -> display value
)

object FordPids {

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

    /** Raw u16, shown as a plain integer — no scaling claimed. */
    private val rawU16: (ByteArray) -> String? = { b -> u16(b)?.toString() }

    /** u16 * 8, shown as a plain integer (mg counters, DPF test result). */
    private val eightsU16: (ByteArray) -> String? = { b -> u16(b)?.let { (it * 8).toString() } }

    /** The whole payload as uppercase hex — the adjustment blocks Ford stores
     *  as packed per-cylinder codes, which mean nothing decoded byte-wise. */
    private val hexBlock: (ByteArray) -> String? = { b ->
        if (b.isEmpty()) null else b.joinToString("") { "%02X".format(it.toInt() and 0xFF) }
    }

    val PCM_DIDS: List<FordDid> = listOf(
        // ---- PCM @ 0x7E0 ---------------------------------------------------
        FordDid(
            0x7E0, 0x050D, "Fuel rail pressure control", "MPa",
            scaled(0.0078125, dp = 2, lo = -255.0, hi = 255.0, signed = true),
        ),
        FordDid(0x7E0, 0x0508, "Soot in oil estimate", "mg", eightsU16),
        FordDid(0x7E0, 0x0509, "Sludge in oil estimate", "mg", eightsU16),
        FordDid(0x7E0, 0x0380, "DPF test result", "", eightsU16),
        // DPF-deleted trucks show this frozen — raw, so nothing is implied.
        FordDid(0x7E0, 0x057B, "Soot load", "raw", rawU16),
        FordDid(
            0x7E0, 0x0519, "Distance since oil change", "km",
            scaled(4.0, dp = 0, lo = 0.0, hi = 1_000_000.0),
        ),
        FordDid(
            0x7E0, 0xF405, "Coolant temperature (PCM)", "°C",
            byteVal(1.0, -40.0, dp = 0, lo = -40.0, hi = 215.0),
        ),
        FordDid(0x7E0, 0xF40D, "Vehicle speed DID F40D (raw)", "raw", rawU16),
        FordDid(0x7E0, 0xFD00, "Pilot correction status", "", byteVal()),
        FordDid(0x7E0, 0xDE01, "Injector quantity adjustment (IMA)", "hex", hexBlock),
        FordDid(0x7E0, 0xDE03, "EGR valve adjustment", "hex", hexBlock),
        FordDid(0x7E0, 0xDE04, "Throttle valve actuator adjustment", "hex", hexBlock),
        // ---- TCM @ 0x7E1 ---------------------------------------------------
        // Nothing sourced yet: no invented scaling goes here.
    )
}
