package com.carharness.app.obd

/**
 * Jaguar X260 XF 3.0d AdBlue/SCR + DPF diagnostics over an ELM327.
 *
 * Target: the 2016 XF (X260) 3.0d V6 diesel (306DT), EU6 with SCR — SDD-era
 * CAN diagnostics, so a plain dongle genuinely reaches the PT-CAN (research:
 * docs/private/jlr-x260-adblue-research.md). The job is read-only depth on the
 * aftertreatment: what fault memory says, what live values can be read, and a
 * sourced assessment of the AdBlue fault space — NOT any invented decoding.
 *
 * THE HONEST GAP: no public JLR DID list exists for DPF soot mass, regen
 * status, DEF level/quality, NOx ppm or the EGTs (research §2 Q3). Every
 * SCR/DPF value is therefore CAPTURE-FIRST: until the DID sweep
 * (JlrSweep) has mapped the DIDs on the real car, this tool reads raw
 * payloads for what IS known (the Old Jaguar Group petrol-capture engine
 * DIDs, provenance-marked) and flags the aftertreatment set as capture
 * pending. Nothing here turns a raw byte into a gram of soot or a litre of
 * AdBlue.
 *
 * health()    one read-only pass: generic Mode-01 baseline FIRST (before any
 *             session games), the known-table modules' fault codes through
 *             the existing JlrUds scan ladder, the known engine DIDs raw, and
 *             a sourced assessment block from research §4 (ranked AdBlue
 *             failure modes + expected codes) and §5 (regen thresholds: auto
 *             ~26 g soot, forced permissible below ~46 g, above that a
 *             physical clean).
 * driveLog()  a timed log pairing rpm with the candidate/known DIDs, raw
 *             payloads with timestamps, through warm-up or a regen — the
 *             timeline that identifies a signal after the sweep has surfaced
 *             its DID. Every-fifth-tick generic baseline, achieved sample rate
 *             reported.
 *
 * Both stay inside the read-only envelope: 01 xx, 3E 00, 22 xx, 10 03 and the
 * JlrUds fault ladder (19 02). Nothing is written, ever.
 *
 * Mirror of backend/profiles/jlr/adblue.py — keep the two in step.
 */
object JlrAdBlue {

    private const val ENGINE = 0x7E0

    // The engine rows of the Old Jaguar Group capture (JlrPids.JLR_DIDS) — the
    // only JLR DIDs with any provenance at all. Provenance rides on every
    // value: they were captured on X351/XK PETROLS, so on the 3.0d diesel they
    // are unverified until re-read. The SCR/DPF set has NO known DIDs.
    private val OJG_ENGINE_DIDS: List<Pair<Int, String>> =
        JlrPids.JLR_DIDS.filter { it.module == ENGINE }.map { it.did to it.name }

    private const val OJG_PROVENANCE = "OJG petrol capture, unverified on diesel"

    // The aftertreatment candidate set: research §7 seeds RANGES (0300-05FF,
    // 2100-21FF, 2200-22FF, F4xx mirrors on the PCM) but no specific ids —
    // those arrive only when the DID sweep has captured them on the real car.
    // Until then this is the honest placeholder the health pass and drive log
    // carry.
    private const val CAPTURE_PENDING =
        "⚠ capture pending — no public JLR DID exists for DPF soot mass, regen status, " +
            "DEF level/quality, NOx ppm or EGTs."
    private const val CAPTURE_PENDING_NEXT =
        "Run the DID sweep on 0x7E0 over the research §7 ranges (0300-05FF, 2100-21FF, " +
            "2200-22FF, F400-F4FF), identify a signal by how it moves, then log it with " +
            "Drive log."

    // Research §4: the AdBlue/SCR fault space on the XF 3.0d, ranked by
    // community frequency. Sourced text, shown as the guide when the dash complains.
    private data class RankedCause(val rank: Int, val cause: String, val source: String)

    private val FAULT_SPACE = listOf(
        RankedCause(
            1,
            "Crystallised DEF injector — urea crystals block the injector; the system " +
                "flags fluid quality.",
            "r/Jaguar 'Incorrect Diesel Exhaust Fluid Quality Detected'; 3RG Stop " +
                "Crystal usage videos",
        ),
        RankedCause(
            2,
            "NOx sensor (upstream or downstream of the SCR) misreads — triggers " +
                "'incorrect DEF quality' with good fluid and can over-dose the SCR cat. " +
                "Swap front/rear sensors to identify the bad one.",
            "jaguarfuelconversions.co.uk; xeforums DEF/NOx thread",
        ),
        RankedCause(
            3,
            "Reagent tank level sensor fault — reads 0/empty with a full tank; distinct " +
                "dash message.",
            "jaguarforum 'AdBlue Level Reset' (2018 X260)",
        ),
        RankedCause(
            4,
            "Contaminated or degraded AdBlue (watered down, screenwash, expired).",
            "community consensus, research §4",
        ),
    )

    // Research §4: the codes to expect in the PCM around this system. Used to
    // match what the fault read actually returned — never claimed unread.
    private val EXPECTED_CODES = linkedMapOf(
        "P204F" to "SCR system efficiency",
        "P249D" to "reductant injection (persistent case documented after pump/NOx/" +
            "temp sensor replacement)",
        "P2463" to "DPF restriction — soot accumulation",
        "P2201" to "NOx sensor circuit (P2201-family)",
    )

    // Research §5: regen knowledge. Thresholds are community-sourced figures,
    // NOT DID-derived — the assessment says so explicitly because no soot-mass
    // DID has been captured yet.
    private const val AUTO_REGEN_SOOT_G = "~26"
    private const val FORCED_REGEN_BELOW_G = "~46"
    private const val REGEN_ABOVE_THAT = "the filter needs physical cleaning, not a forced regen"

    // Consecutive all-silent ticks before the drive log gives up (adapter or
    // ignition gone — a working car never goes quiet this long).
    private const val DEAD_TICKS = 10

    // One known-table module as the health pass read it.
    private data class ModuleRead(
        val code: String,
        val name: String,
        val address: String,
        val present: Boolean,
        val ident: List<Pair<String, String>>,
        val dtcs: List<UdsDtc>,
        val error: String?,
    )

    /** One raw value read: DID + its OJG name + the payload, undecoded. */
    private data class DidValue(val did: String, val name: String, val payload: String)

    /** Generic Mode-01 decoders (ISO 15031 — public formulas, unlike the JLR DIDs). */
    private fun decodePid(pid: Int, body: ByteArray?): Double? {
        if (body == null || body.isEmpty()) return null
        return when {
            pid == 0x0C && body.size >= 2 ->
                (((body[0].toInt() and 0xFF) shl 8) or (body[1].toInt() and 0xFF)) / 4.0
            pid == 0x05 -> (body[0].toInt() and 0xFF) - 40.0
            pid == 0x42 && body.size >= 2 ->
                (((body[0].toInt() and 0xFF) shl 8) or (body[1].toInt() and 0xFF)) / 1000.0
            else -> null
        }
    }

    private fun hex(bytes: ByteArray): String =
        bytes.joinToString("") { "%02X".format(it.toInt() and 0xFF) }

    // ------------------------------------------------------------- assessment

    /**
     * Sourced, conditional text from research §4 + §5 — driven by whatever the
     * fault read actually returned. Never claims a DID-derived soot number
     * (none is readable until the DIDs are captured).
     */
    private fun faultReadNote(modules: List<ModuleRead>): String {
        val codes = modules.filter { it.present }.flatMap { it.dtcs.map { d -> d.code } }
        val matched = codes.filter { it in EXPECTED_CODES }
        val p2201 = codes.filter { it.startsWith("P220") }.sorted()
        val named = (matched + p2201).distinct().sorted()
        return if (named.isNotEmpty()) {
            "The fault read returned aftertreatment-relevant codes (${named.joinToString(", ")}). " +
                "Against research §4: P204F/P249D point at the SCR/dosing side " +
                "(crystallised injector or NOx sensor misread — the ranked list below " +
                "orders them), P2463 at DPF soot restriction, P2201-family at the NOx " +
                "sensor circuits."
        } else {
            "No aftertreatment code (P204F / P249D / P2463 / P2201-family) is stored in " +
                "the modules a dongle reaches. If the dash still shows an AdBlue/DPF " +
                "message, the ranked list below is the guide — and a drive log through " +
                "warm-up is the next read."
        }
    }

    private val regenThresholdsNote =
        "DPF regen thresholds (research §5, community-sourced — NOT read from the car): " +
            "auto regen from roughly $AUTO_REGEN_SOOT_G g soot; a forced regen is only " +
            "permissible below roughly $FORCED_REGEN_BELOW_G g; above that " +
            "$REGEN_ABOVE_THAT. The harness cannot yet measure soot mass on this car — " +
            "no DID captured — so permissibility is judged by the tool that reads it " +
            "(SDD/IIDTool), not invented here. Forced regen stays tool-only: there is no " +
            "public JLR routine-control id, and this harness would not send one."

    // ----------------------------------------------------------------- health

    /**
     * One read-only AdBlue/SCR + DPF health pass on the X260 diesel. Order
     * matters: the generic Mode-01 baseline (rpm, coolant, module voltage) is
     * taken FIRST, before any session games; then the known-table modules get
     * the existing JlrUds fault ladder; then the known engine DIDs are read RAW
     * with their provenance marked, and the SCR/DPF set is flagged capture
     * pending. The assessment block is sourced text from the research doc,
     * conditional on the codes actually read.
     */
    fun health(
        session: Elm327Session,
        simulated: Boolean,
        onProgress: (String) -> Unit = {},
    ): List<EnhancedReading> {
        if (simulated) {
            // A fake X260 diesel: PCM holding the two aftertreatment codes the
            // assessment is written around, TCM clean, e-diff not fitted.
            onProgress("SIMULATED — building the demo health pass…")
            val modules = listOf(
                ModuleRead(
                    "PCM", "Engine control (ECM/PCM)", "0x7E0", true,
                    listOf("VIN" to "SIMULATEDX260DIE"),
                    listOf(
                        UdsDtc(
                            "P249D", "249D00", "2F",
                            listOf("test failed", "confirmed"), Dtc.describe("P249D"),
                        ),
                        UdsDtc(
                            "P2463", "246300", "28",
                            listOf("confirmed"), Dtc.describe("P2463"),
                        ),
                    ),
                    null,
                ),
                ModuleRead(
                    "TCM", "Transmission (ZF 6HP/8HP)", "0x7E1", true,
                    emptyList(), emptyList(), null,
                ),
                ModuleRead(
                    "RDCM", "Rear differential (e-diff)", "0x795", false,
                    emptyList(), emptyList(), null,
                ),
            )
            val values = listOf(
                DidValue("033E", "Boost / manifold pressure", "0064"),
                DidValue("03F3", "Engine oil temperature", "58"),
                DidValue("0520", "Charge air temp (post-intercooler)", "2A"),
            )
            val baseline = Triple(820.0, 74.0, 14.1)
            val summary =
                "SIMULATED — no real car connected. Shape of a real pass: Mode-01 " +
                    "baseline, known-table faults via the scan ladder, known engine DIDs " +
                    "raw with provenance, SCR/DPF set flagged capture pending, sourced " +
                    "assessment below. VIN SIMULATEDX260DIE."
            return healthRows(summary, baseline, modules, values)
        }

        session.ensureProtocol()
        if (!JlrUds.protocolSupported(session)) {
            throw ObdException(
                "The car answered ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k CAN JLR diagnostics use.",
                ObdException.Kind.PROTOCOL,
            )
        }

        // The car's identity first, while the receive filter is still open.
        onProgress("Reading VIN…")
        val obdVin = JlrUds.vinFromObd(session)

        val modules = mutableListOf<ModuleRead>()
        val values = mutableListOf<DidValue>()
        var baseline = Triple<Double?, Double?, Double?>(null, null, null)
        JlrUds.begin(session)
        try {
            // 1. Generic Mode-01 baseline FIRST — before any session games.
            onProgress("Reading the Mode-01 baseline…")
            val rpm = decodePid(0x0C, JlrUds.readPid(session, ENGINE, 0x0C, 1000))
            val coolant = decodePid(0x05, JlrUds.readPid(session, ENGINE, 0x05, 1000))
            val voltage = decodePid(0x42, JlrUds.readPid(session, ENGINE, 0x42, 1000))
            baseline = Triple(rpm, coolant, voltage)

            // 2. Known-table modules through the existing JlrUds ladder.
            for (ecu in JlrUds.ECUS) {
                onProgress("Probing ${ecu.code}…")
                val trace = JlrUds.Trace()
                if (!JlrUds.testerPresent(session, ecu.req, trace)) {
                    modules.add(
                        ModuleRead(
                            ecu.code, ecu.name, "0x%03X".format(ecu.req), false,
                            emptyList(), emptyList(), null,
                        )
                    )
                    continue
                }
                var ident: List<Pair<String, String>> = emptyList()
                var dtcs: List<UdsDtc> = emptyList()
                var error: String? = null
                try {
                    ident = JlrUds.readIdent(session, ecu.req, trace)
                    dtcs = JlrUds.readDtcs(session, ecu.req, trace)
                } catch (e: Exception) {
                    if (e !is UdsNegativeResponse && e !is ObdException) throw e
                    error = e.message
                }
                modules.add(
                    ModuleRead(
                        ecu.code, ecu.name, "0x%03X".format(ecu.req), true,
                        ident, dtcs, error,
                    )
                )
            }

            // 3. Known engine DIDs, RAW, provenance marked. The SCR/DPF set has
            //    nothing to read yet — the capture-pending rows say so below.
            for ((did, name) in OJG_ENGINE_DIDS) {
                onProgress("Reading DID %04X (raw)…".format(did))
                val body = JlrUds.readDid(session, ENGINE, did, 1500) ?: continue
                values.add(DidValue("%04X".format(did), name, hex(body)))
            }
        } finally {
            JlrUds.restore(session)
        }

        val vin = modules
            .flatMap { m -> m.ident.filter { it.first == "VIN" && it.second.length >= 11 } }
            .firstOrNull()?.second?.take(17) ?: obdVin
        val faults = modules.filter { it.present }.sumOf { it.dtcs.size }
        val present = modules.count { it.present }
        val summary =
            (if (vin != null) "VIN $vin. " else "") +
                "$present known-table module(s) answered, $faults fault" +
                (if (faults == 1) "" else "s") + " stored, ${values.size} known engine " +
                "DID(s) answered raw. SCR/DPF values: capture pending (no public DID — " +
                "run the DID sweep). Read-only pass; nothing was changed on the car."
        return healthRows(summary, baseline, modules, values)
    }

    /** The health pass as card rows: summary, baseline, faults, raw DIDs,
     *  capture-pending flags, then the sourced assessment. */
    private fun healthRows(
        summary: String,
        baseline: Triple<Double?, Double?, Double?>,
        modules: List<ModuleRead>,
        values: List<DidValue>,
    ): List<EnhancedReading> {
        val out = mutableListOf(EnhancedReading("Summary", summary, ""))
        baseline.first?.let { out.add(EnhancedReading("Engine rpm (Mode 01)", "%.0f".format(it), "rpm")) }
        baseline.second?.let { out.add(EnhancedReading("Coolant (Mode 01)", "%.0f".format(it), "°C")) }
        baseline.third?.let { out.add(EnhancedReading("Module voltage (Mode 01)", "%.2f".format(it), "V")) }
        for (m in modules) {
            val status = when {
                !m.present -> "no answer"
                m.error != null -> "answered but the fault read failed: ${m.error}"
                m.dtcs.isEmpty() -> "present · no faults stored"
                else -> "present · ${m.dtcs.size} fault" + (if (m.dtcs.size == 1) "" else "s") + " stored"
            }
            out.add(EnhancedReading("${m.code} ${m.address}", status, ""))
            for (dtc in m.dtcs) {
                out.add(EnhancedReading("${dtc.code} (${m.code})", dtc.description, ""))
            }
        }
        for (v in values) {
            out.add(EnhancedReading("${v.name} — DID ${v.did} · $OJG_PROVENANCE", v.payload, ""))
        }
        out.add(EnhancedReading("SCR/DPF values", CAPTURE_PENDING, ""))
        out.add(EnhancedReading("Next step", CAPTURE_PENDING_NEXT, ""))
        out.add(EnhancedReading("Fault read", faultReadNote(modules), ""))
        out.add(EnhancedReading("DPF regen thresholds", regenThresholdsNote, ""))
        out.add(
            EnhancedReading(
                "Codes to expect",
                EXPECTED_CODES.entries.joinToString(" · ") { (code, text) -> "$code $text" },
                "",
            )
        )
        for (c in FAULT_SPACE) {
            out.add(
                EnhancedReading(
                    "Ranked cause ${c.rank} of ${FAULT_SPACE.size}",
                    "${c.cause} Source: ${c.source}",
                    "",
                )
            )
        }
        return out
    }

    // -------------------------------------------------------------- drive log

    /** One tick: timestamp, rpm, each DID's raw payload, plus the every-fifth
     *  coolant/voltage baseline. A null DID payload is a recorded gap. */
    private data class Sample(
        val t: Double,
        val rpm: Double?,
        val dids: Map<Int, String?>,
        val coolant: Double?,
        val voltage: Double?,
    )

    /**
     * Timed log pairing engine rpm with the candidate/known DIDs, raw. Run it
     * through warm-up or a regen (a regen is ~20 min at 500 °C+ per research
     * §5, so the 3-minute phone default only spans warm-up — pass more seconds
     * to cover one). Every tick reads rpm plus each DID's RAW payload with a
     * timestamp; every fifth tick adds coolant and module voltage. Payloads are
     * raw on purpose: a signal is identified by how it moves against rpm, then
     * decoded against the car — never guessed here.
     */
    fun driveLog(
        session: Elm327Session,
        simulated: Boolean,
        onProgress: (String) -> Unit = {},
        seconds: Long = 180,
    ): List<EnhancedReading> {
        if (simulated) {
            // Fake warm-up: rpm with raw DID payloads beside it, coolant +
            // voltage on the every-fifth-tick baseline, a non-answer recorded
            // as a gap.
            onProgress("SIMULATED — building the demo drive log…")
            val didList = OJG_ENGINE_DIDS.map { it.first }
            val samples = listOf(
                Sample(
                    0.0, 810.0,
                    mapOf(0x033E to "0064", 0x03E6 to "34", 0x03F2 to "44", 0x03F3 to "3C", 0x0520 to "1E"),
                    22.0, 12.2,
                ),
                Sample(
                    4.2, 1750.0,
                    mapOf(0x033E to "00C4", 0x03E6 to "35", 0x03F2 to "44", 0x03F3 to "42", 0x0520 to "28"),
                    null, null,
                ),
                Sample(
                    8.1, 1980.0,
                    mapOf(0x033E to "00E1", 0x03E6 to "35", 0x03F2 to "45", 0x03F3 to "4B", 0x0520 to "2C"),
                    null, null,
                ),
                Sample(
                    12.4, 690.0,
                    mapOf(0x033E to "005A", 0x03E6 to null, 0x03F2 to "45", 0x03F3 to "50", 0x0520 to "30"),
                    null, null,
                ),
            )
            val summary =
                "SIMULATED — no real car connected. Fake warm-up shape: rpm with raw DID " +
                    "payloads beside it, coolant + voltage on the every-fifth-tick " +
                    "baseline. 4 ticks over 12.4s (~0.30/s) across ${didList.size} DID(s)."
            return logRows(summary, didList, samples)
        }

        val didList = OJG_ENGINE_DIDS.map { it.first }
        session.ensureProtocol()
        if (!JlrUds.protocolSupported(session)) {
            throw ObdException(
                "The car answered ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k CAN the JLR drive log needs.",
                ObdException.Kind.PROTOCOL,
            )
        }

        val samples = mutableListOf<Sample>()
        val started = System.nanoTime()
        var dead = 0
        var tick = 0
        JlrUds.begin(session)
        try {
            fun elapsedS(): Double = (System.nanoTime() - started) / 1_000_000_000.0
            while (elapsedS() < seconds) {
                var alive = false
                val rpmBody = JlrUds.readPid(session, ENGINE, 0x0C, 800)
                val rpm = decodePid(0x0C, rpmBody)
                alive = alive || rpmBody != null
                val dids = LinkedHashMap<Int, String?>()
                for (did in didList) {
                    val body = JlrUds.readDid(session, ENGINE, did, 1500)
                    dids[did] = body?.let(::hex)
                    alive = alive || body != null
                }
                var coolant: Double? = null
                var voltage: Double? = null
                if (tick % 5 == 0) {
                    val cBody = JlrUds.readPid(session, ENGINE, 0x05, 800)
                    coolant = decodePid(0x05, cBody)
                    alive = alive || cBody != null
                    val vBody = JlrUds.readPid(session, ENGINE, 0x42, 800)
                    voltage = decodePid(0x42, vBody)
                    alive = alive || vBody != null
                }
                val t = elapsedS()
                samples.add(Sample(t, rpm, dids, coolant, voltage))
                val line = StringBuilder("t=%.1fs rpm=%s".format(t, rpm?.let { "%.0f".format(it) } ?: "no answer"))
                for ((did, payload) in dids) {
                    line.append(" · %04X=%s".format(did, payload ?: "—"))
                }
                if (tick % 5 == 0) {
                    coolant?.let { line.append(" · coolant %.0f°C".format(it)) }
                    voltage?.let { line.append(" · %.2fV".format(it)) }
                }
                onProgress(line.toString())
                tick++
                if (alive) {
                    dead = 0
                } else {
                    dead++
                    if (dead >= DEAD_TICKS) break
                    Thread.sleep(200)
                }
            }
        } finally {
            JlrUds.restore(session)
        }

        val elapsed = (System.nanoTime() - started) / 1_000_000_000.0
        val rate = if (elapsed > 0) samples.size / elapsed else 0.0
        val rpms = samples.mapNotNull { it.rpm }.filter { it != 0.0 }
        val died = dead >= DEAD_TICKS
        val summary =
            "Drive log: ${samples.size} tick(s) over %.0fs (~%.2f/s) across ${didList.size} DID(s). ".format(elapsed, rate) +
                (if (rpms.isNotEmpty()) {
                    "Rpm spanned %.0f-%.0f.".format(rpms.min(), rpms.max())
                } else {
                    "No rpm was recorded — check the dongle stayed awake."
                }) +
                (if (died) {
                    " WARNING: the log ended early — the bus went quiet for $DEAD_TICKS " +
                        "consecutive ticks (adapter or ignition dropped)."
                } else "") +
                "Payloads are raw; a regen needs ~20 min at 500 °C+ (research §5) — the " +
                    "3-minute default spans warm-up only. Read-only throughout."
        return logRows(summary, didList, samples)
    }

    /** The log as card rows: the summary (with the achieved sample rate), then
     *  each DID's first → last raw payload so the card stays readable — the
     *  per-tick detail lives in the progress lines. */
    private fun logRows(
        summary: String,
        didList: List<Int>,
        samples: List<Sample>,
    ): List<EnhancedReading> {
        val out = mutableListOf(EnhancedReading("Summary", summary, ""))
        for (did in didList) {
            val payloads = samples.mapNotNull { it.dids[did] }.filter { it.isNotEmpty() }
            val value = if (payloads.isEmpty()) {
                "no answer"
            } else {
                "${payloads.first()} → ${payloads.last()}"
            }
            out.add(EnhancedReading("DID %04X (raw)".format(did), value, ""))
        }
        out.add(
            EnhancedReading(
                "Note",
                "Every tick read rpm plus each DID's raw payload; every fifth tick added " +
                    "coolant and module voltage. Identify a signal by how it moves against " +
                    "rpm, then decode it against the car — never guessed here.",
                "",
            )
        )
        return out
    }
}
