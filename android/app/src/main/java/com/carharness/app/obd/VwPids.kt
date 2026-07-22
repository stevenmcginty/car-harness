package com.carharness.app.obd

/**
 * VW / Audi (VAG) live "extras" over a plain ELM327, service 0x22
 * (ReadDataByIdentifier) addressed to the engine ECU (0x7E0, standard +8).
 *
 * Two mechanisms (docs/private/vag-research.md §4), both read-only:
 *
 *  (a) The standard OBD PIDs remapped as DIDs at 0xF400 + PID (mechanism
 *      CONFIRMED on a VW: 22 F4 0D and 22 F4 42 captured). These carry the same
 *      scaling as the ISO 15031 PID, so coolant / oil / battery / ambient / load
 *      are decoded honestly. -> [OBD_REMAP_DIDS].
 *  (b) The diesel DPF pack and the EV State-of-Charge DID. These are real
 *      (captured / used by forum tuners) but SINGLE-SOURCE and, crucially, their
 *      SCALING IS NOT PUBLICLY CONFIRMED (research §4b). So we NEVER fabricate an
 *      engineering figure: we show the raw 16-bit value + the exact hex and
 *      append a caveat reading. -> [DPF_DIDS], [EV_DIDS], [rawReading].
 *
 * Research marks a confirmed DSG/ATF-temperature DID and an odometer DID as
 * genuine gaps — they are deliberately NOT invented here.
 *
 * Mirror of backend/profiles/vw/pids.py — keep the two in step.
 */

/** UDS service-0x22 remapped-OBD value: payload after `62 <did>` -> display. */
class VagDid(
    val did: Int,
    val name: String,
    val unit: String,
    val decode: (ByteArray) -> String?,
)

/** A community-sourced DID whose scaling is unconfirmed — shown raw + hex only. */
class VagRawDid(val did: Int, val name: String)

object VwPids {

    // All live reads are addressed to the engine ECU on the standard block
    // (0x7E0, response 0x7E8 = request + 8).
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

    // (a) CONFIRMED remapped OBD set: DID = 0xF400 + standard PID (research §4a).
    // Scaling is the ISO 15031 PID's own scaling.
    val OBD_REMAP_DIDS: List<VagDid> = listOf(
        VagDid(0xF405, "Coolant temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 215.0)),
        VagDid(0xF45C, "Engine oil temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 215.0)),
        VagDid(0xF442, "Battery voltage (control module)", "V", u16Scaled(1.0 / 1000, dp = 2, lo = 0.0, hi = 32.0)),
        VagDid(0xF42F, "Fuel level", "%", byteVal(100.0 / 255, dp = 0, lo = 0.0, hi = 100.0)),
        VagDid(0xF446, "Ambient air temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 100.0)),
        VagDid(0xF404, "Calculated engine load", "%", byteVal(100.0 / 255, dp = 0, lo = 0.0, hi = 100.0)),
    )

    // (b) COMMUNITY-SOURCED, UNVERIFIED diesel DPF DIDs (research §4b). Real but
    // single-source, and their scaling is NOT publicly confirmed — shown raw only.
    val DPF_DIDS: List<VagRawDid> = listOf(
        VagRawDid(0x114F, "DPF soot mass, calculated"),
        VagRawDid(0x114E, "DPF soot mass, measured"),
        VagRawDid(0x1156, "DPF distance since last regen"),
        VagRawDid(0x115E, "DPF time since last regen"),
        VagRawDid(0x178C, "DPF oil-ash residue"),
        VagRawDid(0x11B2, "DPF inlet temperature"),
        VagRawDid(0x10F9, "DPF outlet temperature"),
    )

    // COMMUNITY-SOURCED, UNVERIFIED single-source EV DID (research §4b). Raw only.
    val EV_DIDS: List<VagRawDid> = listOf(
        VagRawDid(0x1DD0, "HV battery State-of-Charge"),
    )

    val COMMUNITY_CAVEAT = EnhancedReading(
        "Note",
        "DPF / HV readings above are community-sourced DIDs with unconfirmed " +
            "scaling — raw values shown; verify against the car before trusting.",
        "",
    )

    /** Community DID: raw 16-bit value + exact bytes, never a fabricated figure. */
    fun rawReading(b: ByteArray): String? {
        if (b.isEmpty()) return null
        val hexed = b.joinToString("") { "%02X".format(it.toInt() and 0xFF) }
        val v = u16(b, 0) ?: return "0x$hexed"
        return "$v (raw 0x$hexed)"
    }
}
