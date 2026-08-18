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
 * THE HONEST GAP, as it now stands. Decrypting SDD 169 closed part of it: the
 * urea/SCR dosing cluster 05FA-05FD is named by the manufacturer and answers
 * live on the PCM (there is no separate SCR ECU — see JlrSdd). SDD publishes
 * no SCALING for any of the four, so they are read raw and marked candidate.
 * What is still genuinely missing: DPF soot mass, regen status, DEF tank
 * level/quality and NOx ppm have no identifier that both exists in SDD and
 * answers on this car — SDD defines 042C as DPF soot in g/l and the L494
 * calibration refuses it outright. So the values that are absent stay absent,
 * and nothing here turns a raw byte into a gram of soot or a litre of AdBlue.
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

    /** OJG engine set + every live external candidate — the drive log's
     *  default poll list when no watch preset is chosen. */
    private fun defaultDriveDids(): List<Int> =
        OJG_ENGINE_DIDS.map { it.first } + JlrPids.EXTERNAL_ENGINE_DIDS.map { it.first }

    // ------------------------------------------------- drive-log watch presets
    /**
     * A watch preset is a named short list of DIDs to poll instead of the
     * default set. Fewer ids per tick means a faster sample rate on each one,
     * which is exactly what calibrating a counter needs: the urea preset exists
     * to catch 05FC ticking over, and it cannot do that while the log is also
     * reading eight other identifiers over a slow Bluetooth link.
     *
     * Mirror of DRIVE_LOG_PRESETS in backend/profiles/jlr/adblue.py — keep the
     * two in step.
     */
    data class WatchPreset(
        val id: String,
        val label: String,
        /** Empty means "the full default set" — the broad log, unchanged. */
        val dids: List<Int>,
        /** The length this watch actually wants, in seconds. Advisory: the
         *  drive-log length picker is what runs the job. */
        val seconds: Long,
        val note: String,
    )

    /** The unfiltered log: everything in [defaultDriveDids]. The zero case of a
     *  preset rather than a special case beside it, so the selector has nothing
     *  to hard-code. */
    val FULL_PRESET = WatchPreset(
        id = "full",
        label = "Full candidate set",
        dids = emptyList(),
        seconds = 180,
        note = "Every known engine DID plus every live candidate — the broad default " +
            "when you do not yet know what you are chasing. Slowest per-DID sample " +
            "rate, widest coverage.",
    )

    /** The aftertreatment-shaped subset of the candidate list (v2.14). Trimming
     *  the tick raises the sample rate — the point of a watch is shape over
     *  time, not breadth. The legislated Mode-01 set is read regardless. */
    val DPF_EGR_PRESET = WatchPreset(
        id = "dpfegr",
        label = "DPF + EGR",
        dids = JlrPids.DPF_EGR_WATCH_DIDS,
        seconds = 1500,
        note = "The six aftertreatment-shaped candidates the 2026-08-18 L494 sweep found " +
            "live (F40C leading), logged on their own for a higher sample rate. None is " +
            "decoded: the job is to see which payload MOVES like soot, EGR or EGT " +
            "between idle and warm/loaded. A regen needs ~20 min at 500 °C+, so give it " +
            "the long end of the length picker to span one.",
    )

    val UREA_PRESET = WatchPreset(
        id = "urea",
        label = "Urea / SCR 05FA-05FD",
        dids = JlrSdd.UREA_DIDS,
        seconds = 900,
        note = "The next real-world step on this car. Four SDD-named, PCM-managed urea " +
            "identifiers, polled on their own so each one gets a fast sample rate. " +
            "Drive it WARM — reagent dosing only starts once the SCR catalyst is up to " +
            "temperature, so a cold log records four constants. " + JlrSdd.UREA_WATCH_NOTE,
    )

    /** Every watch the drive log offers, in the order the chips render. */
    val PRESETS: List<WatchPreset> = listOf(FULL_PRESET, DPF_EGR_PRESET, UREA_PRESET)

    /** Look one up by id; null for an unknown id or for the full set, both of
     *  which mean "no filtering". */
    fun presetById(id: String?): WatchPreset? =
        PRESETS.firstOrNull { it.id == id }?.takeIf { it.dids.isNotEmpty() }

    // ------------------------------------------------ aftertreatment Mode-01 --
    // Legislated SAE J1979DA decoders (public standard formulas — not JLR
    // inventions). Support on this ECU is unknown until asked: an unsupported
    // PID answers silence and contributes nothing to the tick.
    private fun s16(hi: Int, lo: Int): Int {
        val v = (hi shl 8) or lo
        return if (v >= 0x8000) v - 0x10000 else v
    }

    private fun temp10(v: Int?): Double? = v?.let { it / 10.0 - 40 }

    private fun readAftertreatment(session: Elm327Session): Map<String, Double> {
        val out = LinkedHashMap<String, Double>()
        fun u16(b: ByteArray, i: Int): Int? =
            if (b.size > i + 1) ((b[i].toInt() and 0xFF) shl 8) or (b[i + 1].toInt() and 0xFF) else null
        for ((pid, _) in JlrPids.AFTERTREATMENT_PIDS) {
            val b = JlrUds.readPid(session, ENGINE, pid, 800) ?: continue
            when (pid) {
                0x7A -> if (b.size >= 2) out["dpf_delta_pressure_kpa"] =
                    s16(b[0].toInt() and 0xFF, b[1].toInt() and 0xFF) / 100.0
                0x7C -> {
                    temp10(u16(b, 0))?.let { out["dpf_temp_inlet_c"] = it }
                    temp10(u16(b, 2))?.let { out["dpf_temp_outlet_c"] = it }
                }
                0x83 -> {
                    u16(b, 0)?.let { out["nox_b1s1_ppm"] = it.toDouble() }
                    u16(b, 2)?.let { out["nox_b1s2_ppm"] = it.toDouble() }
                }
                0x85 -> if (b.size > 5) out["reagent_tank_level_pct"] =
                    (b[5].toInt() and 0xFF) / 2.55
                0x8B -> if (b.size > 2) out["dpf_regen_trigger_pct"] =
                    (b[2].toInt() and 0xFF) / 2.55
                0x78 -> temp10(u16(b, 0))?.let { out["egt_b1s1_c"] = it }
                0x77 -> if (b.isNotEmpty()) out["egr_commanded_pct"] =
                    (b[0].toInt() and 0xFF) / 2.55
            }
        }
        return out
    }

    // The aftertreatment candidate set: research §7 seeds RANGES (0300-05FF,
    // 2100-21FF, 2200-22FF, F4xx mirrors on the PCM) but no specific ids —
    // those arrive only when the DID sweep has captured them on the real car.
    // Until then this is the honest placeholder the health pass and drive log
    // carry.
    private const val CAPTURE_PENDING =
        "Partly answered. SDD 169 named the urea/SCR dosing cluster 05FA-05FD on the PCM " +
            "— manufacturer names, live on this car — but SDD publishes no scaling for " +
            "any of the four, so they are logged raw as candidates. SDD also defines 042C " +
            "as DPF soot in g/l and this calibration refuses it. ⚠ Still genuinely " +
            "missing: DPF soot mass, regen status, DEF tank level/quality and NOx ppm have " +
            "no identifier that both exists in SDD and answers on this car."
    private const val CAPTURE_PENDING_NEXT =
        "Run the Urea log preset warm — 05FC is 'total mass fluid injected by the urea " +
            "dosing system', a monotonic total, so the rate it climbs is what pins the " +
            "scaling SDD does not publish. For the values still missing entirely, sweep " +
            "0x7E0 over the research §7 ranges (0300-05FF, F400-F4FF) and identify a " +
            "signal by how it moves."

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
            // The SDD-named rows carry the payloads the real L494 answered —
            // this is what the health card looks like once SDD is loaded, and
            // it is the shape the Urea log preset goes on to log over a drive.
            val values = listOf(
                DidValue("033E", "Boost / manifold pressure", "0064"),
                DidValue("03F3", "Engine oil temperature", "58"),
                DidValue("0520", "Charge air temp (post-intercooler)", "2A"),
                DidValue("0396", "Distance to next service", "5AB6"),
                DidValue("F405", "Engine coolant temperature", "6E"),
                DidValue("05FA", JlrSdd.did(0x05FA)?.name ?: "", "048A00000301"),
                DidValue("05FB", JlrSdd.did(0x05FB)?.name ?: "", "0FA0"),
                DidValue("05FC", JlrSdd.did(0x05FC)?.name ?: "", "00039CC5"),
                DidValue("05FD", JlrSdd.did(0x05FD)?.name ?: "", "0651"),
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
            // 3b. The candidates: the two live-but-unnamed ids and the
            //     SDD-named urea cluster. Raw, marked, never decoded here.
            for ((did, name, _) in JlrPids.EXTERNAL_ENGINE_DIDS) {
                onProgress("Reading candidate DID %04X (raw)…".format(did))
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
            val did = v.did.toInt(16)
            val entry = JlrSdd.did(did)
            // SDD's name outranks the harness's own label when SDD has one: it
            // is the manufacturer's, and it carries the confidence grade with
            // it. Raw payload first, always — a candidate must never read as a
            // settled number, and even a verified row is easier to sanity-check
            // with its payload beside it.
            val label = entry?.label() ?: "${v.name} · $OJG_PROVENANCE"
            var display = v.payload
            if (entry != null) {
                val body = ByteArray(v.payload.length / 2) {
                    v.payload.substring(it * 2, it * 2 + 2).toInt(16).toByte()
                }
                val decoded = entry.decode(body)
                display = when {
                    decoded != null ->
                        "${v.payload} → $decoded" +
                            if (entry.units.isNotEmpty()) " ${entry.units}" else ""
                    did in JlrSdd.UREA_DIDS && body.size <= 4 ->
                        JlrSdd.unscaledInt(body)?.let { "${v.payload} (unscaled integer $it)" }
                            ?: v.payload
                    else -> v.payload
                }
            }
            out.add(EnhancedReading("$label — DID ${v.did}", display, ""))
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
     *  coolant/voltage baseline. A null DID payload is a recorded gap. Public
     *  so the app layer can stream each tick to its on-disk CSV as it lands —
     *  the farm-day dataset must survive a crash or a dropped dongle. */
    data class DriveSample(
        val t: Double,
        val rpm: Double?,
        val dids: Map<Int, String?>,
        val coolant: Double?,
        val voltage: Double?,
        val extras: Map<String, Double> = emptyMap(),
    )

    /**
     * Timed log pairing engine rpm with the candidate/known DIDs, raw. Run it
     * through warm-up or a regen (a regen is ~20 min at 500 °C+ per research
     * §5, so the 3-minute phone default only spans warm-up — pass more seconds
     * to cover one). Every tick reads rpm plus each DID's RAW payload with a
     * timestamp; every fifth tick adds coolant and module voltage. Payloads are
     * raw on purpose: a signal is identified by how it moves against rpm, then
     * decoded against the car — never guessed here. Each tick is handed to
     * [onSample] as it lands, so a caller can persist it immediately.
     */
    fun driveLog(
        session: Elm327Session,
        simulated: Boolean,
        onProgress: (String) -> Unit = {},
        seconds: Long = 180,
        onSample: (DriveSample) -> Unit = {},
        preset: WatchPreset? = null,
    ): List<EnhancedReading> {
        if (simulated) {
            // Fake warm-up: rpm with raw DID payloads beside it, coolant +
            // voltage on the every-fifth-tick baseline, a non-answer recorded
            // as a gap.
            onProgress("SIMULATED — building the demo drive log…")
            val didList = preset?.dids ?: defaultDriveDids()
            val samples = listOf(
                DriveSample(
                    0.0, 810.0,
                    mapOf(0x033E to "0064", 0x03E6 to "34", 0x03F2 to "44", 0x03F3 to "3C",
                          0x0520 to "1E", 0x05FC to "00039CC5"),
                    22.0, 12.2,
                    mapOf("dpf_delta_pressure_kpa" to 0.15, "nox_b1s1_ppm" to 85.0,
                          "reagent_tank_level_pct" to 62.4, "egt_b1s1_c" to 118.0),
                ),
                DriveSample(
                    4.2, 1750.0,
                    mapOf(0x033E to "00C4", 0x03E6 to "35", 0x03F2 to "44", 0x03F3 to "42",
                          0x0520 to "28", 0x05FC to "00039CD1"),
                    null, null,
                ),
                DriveSample(
                    8.1, 1980.0,
                    mapOf(0x033E to "00E1", 0x03E6 to "35", 0x03F2 to "45", 0x03F3 to "4B",
                          0x0520 to "2C", 0x05FC to "00039CE6"),
                    null, null,
                ),
                DriveSample(
                    12.4, 690.0,
                    mapOf(0x033E to "005A", 0x03E6 to null, 0x03F2 to "45", 0x03F3 to "50",
                          0x0520 to "30", 0x05FC to "00039CF0"),
                    null, null,
                ),
            )
            // Demo mode fires the callback too — the CSV writer and the share
            // sheet are exactly what must not turn out broken on the day.
            samples.forEach(onSample)
            val summary =
                "SIMULATED — no real car connected. Fake warm-up shape: rpm with raw DID " +
                    "payloads beside it, coolant + voltage on the every-fifth-tick " +
                    "baseline. 4 ticks over 12.4s (~0.30/s) across ${didList.size} DID(s)."
            return logRows(summary, didList, samples)
        }

        val didList = preset?.dids ?: defaultDriveDids()
        session.ensureProtocol()
        if (!JlrUds.protocolSupported(session)) {
            throw ObdException(
                "The car answered ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k CAN the JLR drive log needs.",
                ObdException.Kind.PROTOCOL,
            )
        }

        val samples = mutableListOf<DriveSample>()
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
                var extras: Map<String, Double> = emptyMap()
                if (tick % 5 == 0) {
                    val cBody = JlrUds.readPid(session, ENGINE, 0x05, 800)
                    coolant = decodePid(0x05, cBody)
                    alive = alive || cBody != null
                    val vBody = JlrUds.readPid(session, ENGINE, 0x42, 800)
                    voltage = decodePid(0x42, vBody)
                    alive = alive || vBody != null
                    // Legislated aftertreatment PIDs ride the same baseline.
                    extras = readAftertreatment(session)
                }
                val t = elapsedS()
                val sample = DriveSample(t, rpm, dids, coolant, voltage, extras)
                samples.add(sample)
                onSample(sample)
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
        samples: List<DriveSample>,
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
