package com.carharness.app.obd

/**
 * Fiat / Alfa Romeo (FCA) live "extras" over a plain ELM327, service 0x22
 * (ReadDataByIdentifier) on 29-bit CAN (docs/private/fiat-alfa-research.md §4).
 *
 * Every row is a REAL CarScanner PID captured on an on-car Alfa Giulia 2.2 MJTD
 * MY2016 (danardi78 custompids.csv) -- captured data, but from a SINGLE source,
 * so the scaling must be re-checked on the car before the absolute values are
 * trusted ([DANARDI_CAVEAT] rides with the readings, and the card footer says so
 * -- like VwPids.COMMUNITY_CAVEAT / MercedesPids.ATF_CAVEAT). The diesel DPF rows
 * (soot mass + regeneration) are the highest-value dealer check on a Giulia diesel.
 *
 * Values live on two modules: the engine 0x10 and the transmission (TCT/ZF) 0x18.
 * TPMS (0xC7 / 18DAC7F1) is NEVER read here -- it can engage the immobilizer and
 * stall the car (research §7). There is deliberately no TPMS row.
 *
 * If the 29-bit reads all fail (a CAN-era Fiat), FiatUds.readEnhanced falls back
 * to [OBD_PIDS], a curated generic Mode-01 set on the 11-bit engine.
 *
 * Mirror of backend/profiles/fiat/pids.py -- keep the two in step.
 */

/** UDS service-0x22 FCA value on module [addr]: payload after `62 <did>` -> display. */
class FiatDid(
    val addr: Int,
    val did: Int,
    val name: String,
    val unit: String,
    val decode: (ByteArray) -> String?,
)

/** A generic Mode-01 value: payload after `41 <pid>` -> display string, or null. */
class FiatObdPid(
    val pid: Int,
    val name: String,
    val unit: String,
    val decode: (ByteArray) -> String?,
)

object FiatPids {

    // FCA one-byte addresses for the two live-data modules (research §2a/§4).
    const val ENGINE = 0x10
    const val TRANS = 0x18
    // The engine on the standard OBD block for the CAN-era generic Mode-01 fallback.
    const val ENGINE_OBD = 0x7E0

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

    // ---- the danardi Giulia/Stelvio decoders (research §4, VERIFIED single-source).
    // Each formula is exactly as captured; bytes A,B are the payload after 62 <did>.
    private val soot: (ByteArray) -> String? = { b ->            // 22 18AA = (A*256)+B grams
        u16(b)?.let { if (it in 0..300) fmt(it.toDouble(), 0) else null }
    }
    private val regen: (ByteArray) -> String? = { b ->           // 22 380B = (A*256+B)*100/65535 %
        u16(b)?.let { val v = it * 100.0 / 65535; if (v in 0.0..100.0) fmt(v, 1) else null }
    }
    private val oilTemp: (ByteArray) -> String? = { b ->         // 22 1302 = B degrees C
        u8(b, 1)?.let { if (it in -40..215) fmt(it.toDouble(), 0) else null }
    }
    private val oilLevel: (ByteArray) -> String? = { b ->        // 22 3A41 = (A*256+B)/1000 litres
        u16(b)?.let { val v = it / 1000.0; if (v in 0.0..12.0) fmt(v, 2) else null }
    }
    private val coolant: (ByteArray) -> String? = { b ->         // 22 1003 = ((A*256+B)*0.02)-40 C
        u16(b)?.let { val v = it * 0.02 - 40; if (v in -40.0..215.0) fmt(v, 0) else null }
    }
    private val boost: (ByteArray) -> String? = { b ->           // 22 195A = ((A*256+B)-32768)/1000-1 bar
        u16(b)?.let { val v = (it - 32768) / 1000.0 - 1; if (v in -1.5..4.0) fmt(v, 2) else null }
    }
    private val gear: (ByteArray) -> String? = { b ->            // 22 192D = A
        u8(b, 0)?.let { if (it in 0..12) it.toString() else null }
    }
    private val battery: (ByteArray) -> String? = { b ->         // 22 1955 = (A*256+B)*(0.5/1000) V
        u16(b)?.let { val v = it * 0.5 / 1000; if (v in 0.0..32.0) fmt(v, 2) else null }
    }
    private val transTemp: (ByteArray) -> String? = { b ->       // 22 04FE = A-40 C (TCM 0x18)
        u8(b, 0)?.let { val v = it - 40; if (v in -40..215) fmt(v.toDouble(), 0) else null }
    }

    // (addr, did, name, unit, decode) -- polled ONE at a time (DA10F1 is slow over BLE).
    val GIULIA_DIDS: List<FiatDid> = listOf(
        FiatDid(ENGINE, 0x18AA, "DPF soot mass", "g", soot),
        FiatDid(ENGINE, 0x380B, "DPF regeneration in progress", "%", regen),
        FiatDid(ENGINE, 0x1302, "Engine oil temperature", "°C", oilTemp),
        FiatDid(ENGINE, 0x3A41, "Engine oil level", "L", oilLevel),
        FiatDid(ENGINE, 0x1003, "Coolant temperature", "°C", coolant),
        FiatDid(ENGINE, 0x195A, "Boost / turbo pressure", "bar", boost),
        FiatDid(ENGINE, 0x192D, "Current gear", "", gear),
        FiatDid(ENGINE, 0x1955, "Battery voltage", "V", battery),
        FiatDid(TRANS, 0x04FE, "Transmission oil temperature", "°C", transTemp),
    )

    // Curated generic Mode-01 set for the CAN-era fallback (standard ISO 15031
    // PIDs on the 11-bit engine ECU), mirroring the Mercedes profile's baseline.
    val OBD_PIDS: List<FiatObdPid> = listOf(
        FiatObdPid(0x05, "Coolant temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 215.0)),
        FiatObdPid(0x5C, "Engine oil temperature", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 215.0)),
        FiatObdPid(0x42, "Battery voltage (control module)", "V", u16Scaled(1.0 / 1000, dp = 2, lo = 0.0, hi = 32.0)),
        FiatObdPid(0x2F, "Fuel level", "%", byteVal(100.0 / 255, dp = 0, lo = 0.0, hi = 100.0)),
    )

    /** The danardi values are a single on-car capture -- this caveat rides with
     *  them so the scaling is checked against the car before figures are trusted. */
    val DANARDI_CAVEAT = EnhancedReading(
        "Note",
        "These Giulia/Stelvio values are from a single on-car MY2016 2.2-diesel capture — " +
            "verify the scaling against the car before trusting absolute figures.",
        "",
    )
}
