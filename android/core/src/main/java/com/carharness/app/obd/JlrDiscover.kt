package com.carharness.app.obd

/**
 * JLR diagnostic-address discovery over an ELM327 — the probe-sweep that finds
 * what the shipped module table doesn't know.
 *
 * The verified table (JlrUds.ECUS) is the three publicly-VERIFIED driveline
 * rows — engine 0x7E0, ZF gearbox 0x7E1, rear e-diff 0x795 — captured on
 * X351/XK petrols. Everything else on the reachable HS-CAN (ABS, EPB, and the
 * question that started the X260 AdBlue job: is there a separate reagent/SCR
 * dosing controller?) has an UNKNOWN request id. This tool IS that probe-sweep.
 *
 * Method: walk the 0x700-0x7FF request-id block one id at a time with a quick
 * presence probe (3E 00, then 22 F190 — the same ladder style as JlrUds), and
 * for anything that answers, read identification over UDS 0x22 only. The KWP
 * 1A ident rung JlrUds falls back to is deliberately NOT walked here — this
 * sweep sends only 3E 00 and 22 xx — so an older Ford-era module that refuses
 * every UDS read is reported honestly as "unproven" rather than probed in its
 * own dialect. JLR's verified convention is response = request + 8 for every
 * module, so no id map is needed. Eight ids at the top of the block
 * (0x7F8-0x7FF) cannot be JLR request ids at all — their +8 response would
 * fall outside the 11-bit id space — and are skipped with a note rather than
 * probed with an impossible receive filter.
 *
 * COST, stated honestly: ~248 short probes. A silent address costs one short
 * timeout, so expect roughly 2-5 minutes over a Bluetooth dongle. Silent ids
 * get the cheap probe only — never the full identification ladder — so the
 * walk stays proportionate.
 *
 * Read-only: presence probes and identification reads only. No fault services,
 * no sessions, no writes. Body modules on MS-CAN and MY17+ DoIP cars are as
 * unreachable here as they are to JlrUds.scan — those walls are reported, not
 * presented as faults.
 *
 * Mirror of backend/profiles/jlr/discover.py — keep the two in step.
 */
object JlrDiscover {

    // The swept request-id block. 0x7E0/0x7E1/0x795 — the known table — sit
    // inside it, so the walk re-probes them anyway and they get their table
    // name in the result (that is the "verify the X260 PCM/TCM addresses" half
    // of the job). Response = request + 8 throughout.
    private const val SWEEP_FIRST = 0x700
    private const val SWEEP_LAST = 0x7FF

    // 0x700-0x7FF minus the eight ids a +8 response makes impossible.
    private const val SWEEPABLE_COUNT = SWEEP_LAST - SWEEP_FIRST + 1 - 8

    /** A request id whose +8 response would leave the 11-bit id space cannot
     *  be a JLR module address under the verified convention — skip, don't
     *  mis-probe. */
    private fun sweepable(reqId: Int): Boolean = reqId + 8 <= 0x7FF

    /** One answering address: what proved it lives there, and what it said. */
    data class Found(
        val address: String,                    // "0x730"
        val response: String,                   // "0x738" (request + 8)
        val dialect: String,                    // "uds" or "unproven"
        val provedBy: String,                   // proving rung: "3E 00" / "22 F190"
        val ident: List<Pair<String, String>>,  // UDS ident reads that answered
        val module: String,                     // module name, "" when unnamed
        val source: String,                     // where the name came from, "" if none
    )

    /**
     * (display name, where it came from) for a swept address.
     *
     * Two sources, in order of standing: the shipped table (three rows publicly
     * verified on a real car), then the decrypted SDD 169 L494 MY16 address map
     * (57 rows, the manufacturer's own). Before the SDD map was loaded, 54 of
     * 57 modules could only be reported as "not in the shipped table" beside a
     * bare CAN id; now the sweep prints ABS at 0x7E6 and DSM at 0x744 the way
     * the dealer tool does. Anything neither source knows still says so.
     */
    private fun identify(reqId: Int): Pair<String, String> {
        JlrUds.ECUS.firstOrNull { it.req == reqId }?.let {
            return "${it.code} (${it.name})" to "shipped table"
        }
        val code = JlrSdd.moduleCode(reqId)
        if (code.isNotEmpty()) return code to "SDD 169 ${JlrSdd.PLATFORM} map"
        return "" to ""
    }

    /**
     * Cheap presence ladder for the walk: 3E 00, then 22 F190. Same two-rung
     * style as JlrUds.testerPresent but with sweep timeouts — a silent address
     * must cost one short timeout, not a full ident ladder. Any reply, positive
     * or negative, proves an ECU lives at the address; the label of the proving
     * request is returned, or null for silence.
     */
    private fun quickProbe(session: Elm327Session, reqId: Int): String? {
        for ((service, data, label) in listOf(
            Triple(0x3E, "00", "3E 00"),
            Triple(0x22, "F190", "22 F190"),
        )) {
            try {
                JlrUds.request(session, reqId, service, data, 400)
                return label
            } catch (_: UdsNegativeResponse) {
                return label
            } catch (_: Exception) {
            }
        }
        return null
    }

    /**
     * Best-effort identification over UDS 0x22 only (JlrUds's IDENT_DIDS). The
     * KWP 1A fallback is JlrUds's, not this sweep's — see the object header —
     * so a module that refuses every UDS read simply returns empty and is
     * reported "unproven", never guessed.
     */
    private fun readIdentUds(session: Elm327Session, reqId: Int): List<Pair<String, String>> {
        val out = mutableListOf<Pair<String, String>>()
        for ((did, label) in JlrUds.IDENT_DIDS) {
            try {
                val payload = JlrUds.request(session, reqId, 0x22, "%04X".format(did), 1500)
                if (payload.size < 4) continue
                val value = JlrUds.printable(payload.drop(3))
                if (value.isNotEmpty()) out.add(label to value)
            } catch (_: Exception) {
            }
        }
        return out
    }

    /** The findings as generic name/value rows — the shape the readings card
     *  renders for any brand action, so discovery needs no UI code of its own. */
    private fun foundRows(found: List<Found>): List<EnhancedReading> = found.map { f ->
        val vin = f.ident.firstOrNull { it.first == "VIN" }?.second ?: ""
        val parts = mutableListOf(f.dialect.uppercase())
        parts.add(f.module.ifEmpty { "unnamed by any source" })
        if (vin.isNotEmpty()) parts.add("VIN $vin")
        parts.add("proved by ${f.provedBy}")
        if (f.source.isNotEmpty()) parts.add("named from the ${f.source}")
        EnhancedReading("${f.address} → ${f.response}", parts.joinToString(" · "), "")
    }

    /** The swept/skipped/answered counts, the not-in-table caution and the
     *  honest cost note — the desktop's result "message", flattened. */
    private fun summaryRows(swept: Int, skipped: Int, found: List<Found>): List<EnhancedReading> {
        val out = mutableListOf(
            EnhancedReading(
                "Summary",
                "Swept $swept addresses ($skipped skipped — a +8 response would leave " +
                    "the 11-bit id space), ${found.size} answered.",
                "",
            )
        )
        val fromSdd = found.filter { it.source.startsWith("SDD") }.map { it.address }
        if (fromSdd.isNotEmpty()) {
            out.add(
                EnhancedReading(
                    "Named from the SDD map",
                    "${fromSdd.joinToString(", ")} — manufacturer data, but the map is " +
                        "the L494's, so on another JLR treat a name as a strong hint " +
                        "rather than a fact.",
                    "",
                )
            )
        }
        val unnamed = found.filter { it.module.isEmpty() }.map { it.address }
        if (unnamed.isNotEmpty()) {
            out.add(
                EnhancedReading(
                    "Unnamed by any source",
                    "⚠ ${unnamed.joinToString(", ")} — neither the shipped table nor the " +
                        "SDD map knows these addresses; identify before trusting.",
                    "",
                )
            )
        }
        out.add(
            EnhancedReading(
                "Note",
                "Expect this sweep to take roughly 2-5 minutes over a Bluetooth dongle " +
                    "(each silent address costs one short timeout). Read-only sweep; " +
                    "nothing was changed on the car.",
                "",
            )
        )
        return out
    }

    /**
     * Sweep the JLR PT-CAN request-id block and report what answers. Purpose:
     * find the SCR/reagent controller if the car has a separate one, and verify
     * the PCM/TCM addresses the shipped table claims. Read-only — presence
     * probes and identification reads only, no fault services, no writes.
     */
    fun discover(
        session: Elm327Session,
        simulated: Boolean,
        onProgress: (String) -> Unit = {},
    ): List<EnhancedReading> {
        if (simulated) {
            // A fake X260-shaped bus: PCM + TCM on their table addresses, plus
            // one unexpected UDS answerer at 0x730 to show what discovery is FOR.
            onProgress("SIMULATED — building the demo discovery…")
            val found = listOf(
                Triple(0x7E6, "3E 00", listOf("Part number" to "GX63-2C405-AC")),
                Triple(0x744, "3E 00", emptyList()),
                Triple(
                    0x7E0, "3E 00",
                    listOf(
                        "VIN" to "SIMULATEDX260DIE",
                        "Part number" to "AA52-12A650-EC",
                    ),
                ),
                Triple(0x7E1, "22 F190", listOf("VIN" to "SIMULATEDX260DIE")),
            ).map { (addr, proved, ident) ->
                val (module, source) = identify(addr)
                Found(
                    "0x%03X".format(addr), "0x%03X".format(addr + 8),
                    if (ident.isEmpty()) "unproven" else "uds", proved, ident,
                    module, source,
                )
            }
            return listOf(
                EnhancedReading(
                    "Note",
                    "SIMULATED — swept $SWEEPABLE_COUNT addresses, 4 answered. The PCM and " +
                        "TCM come from the shipped table; 0x7E6 (ABS) and 0x744 (DSM) are " +
                        "named from the decrypted SDD 169 L494 map, which is what that map " +
                        "is for — 0x7E6 was long suspected of being an SCR controller and " +
                        "is not. No real car connected.",
                    "",
                )
            ) + foundRows(found) + summaryRows(SWEEPABLE_COUNT, 8, found)
        }

        session.ensureProtocol()
        if (!JlrUds.protocolSupported(session)) {
            throw ObdException(
                "The car answered ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k CAN the JLR address sweep needs.",
                ObdException.Kind.PROTOCOL,
            )
        }

        val found = mutableListOf<Found>()
        var swept = 0
        var skipped = 0
        onProgress("Sweeping the JLR request-id block (0x700-0x7F7)…")
        JlrUds.begin(session)
        try {
            for (reqId in SWEEP_FIRST..SWEEP_LAST) {
                if (!sweepable(reqId)) {
                    skipped++
                    continue
                }
                swept++
                if (swept % 32 == 0) {
                    onProgress(
                        "Swept $swept of $SWEEPABLE_COUNT addresses — ${found.size} answered so far…"
                    )
                }
                val provedBy = quickProbe(session, reqId) ?: continue
                // Only an answering address pays for identification.
                onProgress("Identifying 0x%03X…".format(reqId))
                val ident = readIdentUds(session, reqId)
                val (module, source) = identify(reqId)
                found.add(
                    Found(
                        address = "0x%03X".format(reqId),
                        response = "0x%03X".format(reqId + 8),
                        dialect = if (ident.isEmpty()) "unproven" else "uds",
                        provedBy = provedBy,
                        ident = ident,
                        module = module,
                        source = source,
                    )
                )
            }
        } finally {
            JlrUds.restore(session)
        }
        return foundRows(found) + summaryRows(swept, skipped, found)
    }
}
