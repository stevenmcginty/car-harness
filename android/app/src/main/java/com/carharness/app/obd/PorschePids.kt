package com.carharness.app.obd

/**
 * Porsche live "extras" over a plain ELM327
 * (docs/private/porsche-research-modules.md §5, §10b).
 *
 * Two mechanisms, both read-only:
 *  (a) The one Porsche-specific value the OBD port reaches: engine oil
 *      temperature via the DME's KWP2000 `21 32` read (readDataByLocalIdentifier)
 *      on the 997/987 KWP-over-CAN cars. SINGLE-SOURCE capture (one Rennlist
 *      997.2 thread) with a CANDIDATE `A - 60` scaling, so [SINGLE_SOURCE_CAVEAT]
 *      rides with the reading. The absolute oil-pressure (`21 02`) and
 *      pressure-deviation (`21 1A`) reads are deliberately NOT implemented —
 *      their scaling is unknown. -> [KWP_PIDS]
 *  (b) A curated generic Mode-01 set on the engine — the honest fallback a
 *      UDS-era, DoIP-era or K-line car still answers. -> [OBD_PIDS]
 *
 * Camshaft deviation (the 996/997 M96/M97 IMS/chain-wear crown jewel), over-rev
 * history, electronic oil level and PDK clutch temps are GENUINE GAPS (research
 * §5a): every Porsche tool reads them but nobody published the wire-level DID,
 * so nothing is invented here.
 *
 * Mirror of backend/profiles/porsche/pids.py — keep the two in step.
 */

/** One Porsche KWP live value: module + local identifier + decode of the
 *  payload after `61 <lid>`. */
class PorscheKwpPid(
    val module: Int,
    val lid: Int,
    val name: String,
    val unit: String,
    val decode: (ByteArray) -> String?,
)

/** A generic Mode-01 value: payload after `41 <pid>` -> display string, or null. */
class PorscheObdPid(
    val pid: Int,
    val name: String,
    val unit: String,
    val decode: (ByteArray) -> String?,
)

object PorschePids {

    // On Porsche the engine IS 0x7E0 (standard OBD block, response = request + 8).
    const val ENGINE = 0x7E0

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

    // The one reachable Porsche-specific value (research §5a-i / §10b, single
    // Rennlist 997.2 source, candidate A-60 scaling) — sanity-bounded.
    val KWP_PIDS: List<PorscheKwpPid> = listOf(
        PorscheKwpPid(ENGINE, 0x32, "Engine oil temperature", "°C", byteVal(1.0, -60.0, lo = -60.0, hi = 200.0)),
    )

    // Curated generic Mode-01 fallback set (standard ISO 15031 PIDs on the engine
    // ECU) — what a UDS-era / DoIP-era / K-line car still answers (research §5a-ii).
    val OBD_PIDS: List<PorscheObdPid> = listOf(
        PorscheObdPid(0x05, "Coolant temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 215.0)),
        PorscheObdPid(0x5C, "Engine oil temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 215.0)),
        PorscheObdPid(0x0C, "Engine RPM", "rpm", u16Scaled(0.25, dp = 0, lo = 0.0, hi = 9000.0)),
        PorscheObdPid(0x0F, "Intake air temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 215.0)),
        PorscheObdPid(0x42, "Control-module voltage", "V", u16Scaled(1.0 / 1000, dp = 2, lo = 0.0, hi = 32.0)),
    )

    /** The single-source caveat — shown right after the Porsche-specific reading. */
    val SINGLE_SOURCE_CAVEAT = EnhancedReading(
        "Note",
        "This Porsche oil-temperature value is from a single Rennlist 997.2 KWP capture with a " +
            "candidate A−60 scaling — verify it against the car before trusting the absolute figure.",
        "",
    )
}
