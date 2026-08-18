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
        // SDD-169 diesel-PCM DIDs whose factory scaling reproduced a sane
        // number from the live L494 SALWA2EK8HA payload (0396 answered 5AB6 ->
        // 23222 km; F405 answered 6E -> 70 C). Verified, so they show as plain
        // numbers with their units, like the OJG rows.
        JlrDid(ENGINE, 0x0396, "Distance to next service", "km", u16Scaled(1.0, 0.0, dp = 0, lo = 0.0, hi = 65535.0)),
        JlrDid(ENGINE, 0xF405, "Engine coolant temperature (UDS DID)", "°C", byteVal(1.0, -40.0, lo = -40.0, hi = 215.0)),
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

    /**
     * Candidate DIDs for the 306DT diesel, polled RAW alongside the verified
     * set -- the phone's copy of backend/profiles/jlr/data/external_pids.json.
     * NOTHING here is decoded: none of it is verified on this engine, and a
     * raw payload is the honest form.
     *
     * Three sources, all from the 2026-08-18 L494 SALWA2EK8HA sweep or from
     * SDD 169, and all of them ids the car ANSWERED:
     *   * F40C / 0430 / 0434 / F423 / F463 / F485 -- aftertreatment-shaped
     *     candidates, recorded with the raw payload they held at idle so a
     *     warm re-read can be diffed against it;
     *   * 0417 / 0598 -- live, and nothing in SDD or anywhere else names them;
     *   * the SDD-named urea cluster 05FA-05FD, appended below.
     *
     * 042C is deliberately absent. Both the sweep and SDD agree on what it is
     * -- a genuine diesel-PCM identifier for DPF soot in g/l, inferred from
     * differential pressure -- and this PCM answers NRC 31 to it, so polling it
     * would spend a timeout every tick for nothing. Its full definition stays
     * visible in JlrSdd.DIDS and in external_pids.json, where it can be read
     * without being chased, and the backend already refuses to poll it.
     */
    val EXTERNAL_ENGINE_DIDS: List<Triple<Int, String, String>> = listOf(
        Triple(
            0xF40C,
            "Aftertreatment candidate (raw 0A4E at idle)",
            "L494 306DT sweep 2026-08-18 — live, undecoded",
        ),
        Triple(
            0x0430,
            "Aftertreatment candidate (raw 025F at idle)",
            "L494 306DT sweep 2026-08-18 — live, undecoded",
        ),
        Triple(
            0x0434,
            "Aftertreatment candidate (raw 0002FADD at idle)",
            "L494 306DT sweep 2026-08-18 — live, undecoded",
        ),
        Triple(
            0xF423,
            "Aftertreatment candidate (raw 0B4A at idle)",
            "L494 306DT sweep 2026-08-18 — live, undecoded",
        ),
        Triple(
            0xF463,
            "Aftertreatment candidate (raw 02BC at idle)",
            "L494 306DT sweep 2026-08-18 — live, undecoded",
        ),
        Triple(
            0xF485,
            "Aftertreatment candidate (raw 0F0007000742000049A5)",
            "L494 306DT sweep 2026-08-18 — live, undecoded",
        ),
        Triple(
            0x0417,
            "Live DID 0417 (unnamed)",
            "ANSWERED 00 on the L494 306DT 2026-08-18 — live, undecoded, not in SDD",
        ),
        Triple(
            0x0598,
            "Live DID 0598 (unnamed)",
            "ANSWERED 0039 on the L494 306DT 2026-08-18 — live, undecoded, not in SDD",
        ),
    ) + JlrSdd.UREA_DIDS.map { did ->
        Triple(
            did,
            JlrSdd.did(did)?.name ?: "Urea DID %04X".format(did),
            "SDD 169 diesel PCM — name authoritative, SDD publishes no scaling; " +
                "answered live on L494 SALWA2EK8HA 2026-08-18",
        )
    }

    /**
     * The SDD-named rows this car answers but has not proved. The name is the
     * manufacturer's own and is trustworthy; the value is not, either because
     * SDD publishes no scaling or because nobody has checked its arithmetic
     * against a real reading. Read on the live pass as a candidate tier.
     */
    val SDD_CANDIDATE_DIDS: List<Int> = JlrSdd.CANDIDATE_DIDS

    // A 64-byte injector-correction blob is not a phone-card reading. Long raw
    // payloads are shown head-and-tail so the row stays legible; the full
    // capture stays the DID sweep's job.
    private const val RAW_HEAD_BYTES = 8

    private fun rawDisplay(body: ByteArray): String {
        val hex = body.joinToString("") { "%02X".format(it.toInt() and 0xFF) }
        if (body.size <= RAW_HEAD_BYTES * 2) return hex
        return hex.take(RAW_HEAD_BYTES * 2) + "…" + hex.takeLast(RAW_HEAD_BYTES * 2) +
            " (${body.size} bytes)"
    }

    /**
     * One SDD-named-but-unproven DID as a card row.
     *
     * The rule the whole SDD layer turns on: a candidate NEVER appears as a
     * bare number. The raw payload is always in the value; SDD's scaled figure
     * joins it only when SDD publishes scaling. The narrow urea rows also show
     * the payload as an unscaled integer — not a decode, just the same bytes in
     * base 10, which is what makes a monotonic counter visibly count.
     */
    fun sddCandidateReading(did: Int, body: ByteArray): EnhancedReading {
        val entry = JlrSdd.did(did)
        val name = entry?.label() ?: "SDD DID %04X [candidate]".format(did)
        val parts = mutableListOf("raw ${rawDisplay(body)}")
        val decoded = entry?.decode(body)
        if (decoded != null) {
            val unit = entry.units
            parts.add("SDD scaling → $decoded" + if (unit.isNotEmpty()) " $unit" else "")
        } else if (did in JlrSdd.UREA_DIDS && body.size <= 4) {
            // Only for payloads narrow enough to BE one number. 05FA is six
            // bytes and is plainly several fields; reading it as one integer
            // would be a decode, and an invented one.
            JlrSdd.unscaledInt(body)?.let { parts.add("unscaled integer $it") }
        }
        return EnhancedReading("$name — DID %04X".format(did), parts.joinToString(" · "), "")
    }

    /** The caveat that rides with the candidate block, so a raw payload is
     *  never mistaken for a reading the harness stands behind. */
    val SDD_CAVEAT = EnhancedReading(
        "Note",
        "Rows marked [candidate] carry Jaguar Land Rover's own SDD 169 name, which is " +
            "authoritative — but the number beside it is not. Where SDD publishes no " +
            "scaling (the urea cluster 05FA-05FD among them) the payload stays raw. " +
            "Log 05FC warm over a drive to pin its scaling: it is a monotonic total.",
        "",
    )

    /**
     * The DPF + EGR watch preset: the aftertreatment-shaped subset of the
     * candidate list. Logging only these trims the tick so the sample rate
     * rises — the point of a watch is shape-over-time, not breadth. The
     * legislated Mode-01 set is read regardless (see AFTERTREATMENT_PIDS).
     */
    val DPF_EGR_WATCH_DIDS: List<Int> =
        listOf(0xF40C, 0x0430, 0x0434, 0xF423, 0xF463, 0xF485)

    /**
     * The legislated Mode-01 aftertreatment set (SAE J1979DA formulas — a
     * public standard, not JLR-invented; per-car support unknown until
     * asked). The drive log reads these on its every-fifth-tick baseline and
     * records whichever answer, decoded; an unsupported PID answers silence
     * and contributes nothing.
     */
    val AFTERTREATMENT_PIDS: List<Pair<Int, String>> = listOf(
        0x7A to "dpf_delta_pressure_kpa",
        0x7C to "dpf_temp_inlet_c/dpf_temp_outlet_c",
        0x83 to "nox_b1s1_ppm/nox_b1s2_ppm",
        0x85 to "reagent_tank_level_pct",
        0x8B to "dpf_regen_trigger_pct",
        0x78 to "egt_b1s1_c",
        0x77 to "egr_commanded_pct",
    )
}
