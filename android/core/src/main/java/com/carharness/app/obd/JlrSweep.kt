package com.carharness.app.obd

/**
 * JLR engine DID sweep ("mapping") over an ELM327.
 *
 * The diesel aftertreatment values the X260 AdBlue job wants — DPF soot mass,
 * regen status, DEF level/quality, NOx ppm, EGTs — have NO public JLR DID list
 * (docs/private/jlr-x260-adblue-research.md §2 Q3: "treat every specific DID
 * on this car as capture-first"). This is the capture tool: walk a 0x22
 * (ReadDataByIdentifier) range on one module one DID at a time and keep every
 * raw answer.
 *
 * How a signal gets identified (never by guessing): sweep once at idle and
 * once with the engine warm / held at speed, then diff. A DID whose payload
 * moves with rpm and load is a live signal; one that steps between two states
 * is a command or status; a temperature drifts one way only. Only after a DID
 * has been identified on the real car does anything downstream decode it —
 * this tool stores raw hex and invents nothing.
 *
 * Classification per DID:
 *   ANSWERED   a positive 62 <did> — payload kept as raw hex (no scaling),
 *   REFUSED    NRC 0x31 (request out of range) — the module is listening and
 *              the id simply isn't implemented; COUNTED, not listed (on a
 *              real car these are the vast majority),
 *   NRC-OTHER  any other negative response — listed with its named NRC (a
 *              0x7F 22 22 cluster, for instance, means "ask again in the
 *              right session" — try extended=true),
 *   SILENT     no reply at all.
 *
 * Read-only: service 0x22 only, plus an optional best-effort 10 03 extended
 * session before the walk (never fatal if refused). The target must answer a
 * presence probe first — the sweep never blind-sprays a dead bus.
 *
 * Mirror of backend/profiles/jlr/sweep.py — keep the two in step.
 */
object JlrSweep {

    // Keep one call proportionate: 4096 ids at ~0.6 s each is already the
    // better part of an hour over Bluetooth. Split wider ranges; the
    // research's default ranges (0300-05FF, 2100-21FF, 2200-22FF, F400-F4FF
    // on the PCM) all fit.
    private const val MAX_SWEEP = 4096

    // Per-DID read timeout. Most answers land well inside 0.6 s; refusals are
    // immediate; only an unimplemented DID on a slow bus pays the full window.
    private const val DID_TIMEOUT_MS = 600L

    /** One non-0x31 negative response, named. 0x31s are counted, not listed. */
    data class NrcOther(val did: String, val nrc: Int)

    private fun nrcName(nrc: Int): String = UdsConst.NRC_NAMES[nrc] ?: "unknown code"

    private fun hex(bytes: List<Byte>): String =
        bytes.joinToString("") { "%02X".format(it.toInt() and 0xFF) }

    /**
     * Walk a 0x22 DID range on one JLR module and keep everything that answers.
     *
     * first/last: hex DID bounds like "0300" and "05FF" (max 4096 ids per
     *   call). target: the module's 11-bit request id in hex, default the PCM
     *   ("7E0", response = request + 8). extended: open the UDS extended
     *   session (10 03) first — best-effort, never fatal; some DIDs only
     *   answer there. Everything comes back raw. Read-only.
     */
    fun sweep(
        session: Elm327Session,
        simulated: Boolean,
        onProgress: (String) -> Unit = {},
        first: String = "0300",
        last: String = "05FF",
        target: String = "7E0",
        extended: Boolean = false,
    ): List<EnhancedReading> {
        if (simulated) {
            // A plausible PCM scatter over the requested range: one 1-byte, one
            // 2-byte and one 4-byte value, a refused stretch, and one
            // conditions-not-correct NRC. The classification counts always sum
            // to the swept width, as they do on a real sweep.
            onProgress("SIMULATED — building the demo sweep…")
            val (lo, hi) = try {
                Pair(first.toInt(16), last.toInt(16))
            } catch (_: NumberFormatException) {
                Pair(0, 0xFFFF)
            }
            val width = maxOf(0, hi - lo + 1)
            var answered = listOf(
                "033E" to "00C4",        // 2 bytes
                "03F3" to "58",          // 1 byte
                "2104" to "00A100B4",    // 4 bytes, 2 words
                "F405" to "5A",
            )
            var nrcOther = listOf(NrcOther("2201", 0x22))
            answered = answered.filter { (did, _) -> did.toInt(16) in lo..hi }
            nrcOther = nrcOther.filter { it.did.toInt(16) in lo..hi }
            val nSilent = maxOf(0, width - answered.size - nrcOther.size - 1)
            val refused = maxOf(0, width - answered.size - nrcOther.size - nSilent)
            return listOf(
                EnhancedReading(
                    "Note",
                    "SIMULATED — no real car connected. This is the shape a real sweep " +
                        "returns: raw payloads (no scaling invented), the 0x31 refusals " +
                        "counted rather than listed, other NRCs named, the rest silent.",
                    "",
                )
            ) + resultRows(
                "${first.uppercase()}-${last.uppercase()}", target.uppercase(), extended,
                answered, refused, nrcOther, nSilent,
            )
        }

        val firstDid: Int
        val lastDid: Int
        val addr: Int
        try {
            firstDid = first.toInt(16)
            lastDid = last.toInt(16)
            addr = target.toInt(16)
        } catch (_: NumberFormatException) {
            throw IllegalArgumentException(
                "first/last/target must be hex like '0300' / '05FF' / '7E0'."
            )
        }
        if (!(0 <= firstDid && firstDid <= lastDid && lastDid <= 0xFFFF)) {
            throw IllegalArgumentException("Need 0 <= first <= last <= 0xFFFF (hex).")
        }
        if (lastDid - firstDid + 1 > MAX_SWEEP) {
            throw IllegalArgumentException(
                "Range too wide (${lastDid - firstDid + 1} ids); max $MAX_SWEEP per call. " +
                    "Split it."
            )
        }

        session.ensureProtocol()
        if (!JlrUds.protocolSupported(session)) {
            throw ObdException(
                "The car answered ${session.protocol.ifEmpty { "an unknown protocol" }}, " +
                    "not the 11-bit/500k CAN the JLR DID sweep needs.",
                ObdException.Kind.PROTOCOL,
            )
        }

        val trace = JlrUds.Trace()
        val answered = mutableListOf<Pair<String, String>>()
        var refused31 = 0
        val nrcOther = mutableListOf<NrcOther>()
        var silent = 0
        val width = lastDid - firstDid + 1
        JlrUds.begin(session)
        try {
            // Guard: never blind-spray a dead bus — the target must first
            // prove something lives at the address.
            onProgress("Probing 0x%03X…".format(addr))
            if (!JlrUds.testerPresent(session, addr, trace)) {
                throw ObdException(
                    "Nothing answered at 0x%03X (3E 00 / 22 F190 both silent) — refusing " +
                        "to sweep a dead address. Run Discover to find the modules this " +
                        "car actually exposes.".format(addr),
                    ObdException.Kind.NO_DATA,
                )
            }
            if (extended) {
                onProgress("10 03 extended session…")
                JlrUds.openSession(session, addr, trace)   // best-effort only
            }
            var done = 0
            for (did in firstDid..lastDid) {
                if (done > 0 && done % 64 == 0) {
                    onProgress(
                        "Sweeping DIDs $first-$last at 0x%03X — $done/$width done, " +
                            "${answered.size} answered…".format(addr)
                    )
                }
                done++
                val payload = try {
                    JlrUds.request(session, addr, 0x22, "%04X".format(did), DID_TIMEOUT_MS)
                } catch (e: UdsNegativeResponse) {
                    if (e.nrc == 0x31) {
                        refused31++          // counted, not listed
                    } else {
                        nrcOther.add(NrcOther("%04X".format(did), e.nrc))
                    }
                    continue
                } catch (_: Exception) {
                    silent++
                    continue
                }
                answered.add("%04X".format(did) to hex(payload.drop(3)))
            }
        } finally {
            JlrUds.restore(session)
        }
        return resultRows(
            "%04X-%04X".format(firstDid, lastDid), "%03X".format(addr), extended,
            answered, refused31, nrcOther, silent,
        )
    }

    /** One sweep result as card rows: the counts first, then every raw answer,
     *  then the named non-0x31 NRCs, then the raw-payload honesty note. */
    private fun resultRows(
        range: String,
        target: String,
        extended: Boolean,
        answered: List<Pair<String, String>>,
        refused31: Int,
        nrcOther: List<NrcOther>,
        silent: Int,
    ): List<EnhancedReading> {
        val out = mutableListOf(
            EnhancedReading(
                "Summary",
                "Swept ${answered.size + refused31 + nrcOther.size + silent} id(s) at " +
                    "0x$target" + (if (extended) " after a 10 03 extended session" else "") +
                    ": ${answered.size} answered with data, $refused31 refused with NRC 31 " +
                    "(listening, id not implemented), ${nrcOther.size} other NRC(s), " +
                    "$silent silent. Range $range.",
                "",
            )
        )
        for ((did, payload) in answered) {
            out.add(EnhancedReading("DID $did", payload, ""))
        }
        for (n in nrcOther) {
            out.add(EnhancedReading("DID ${n.did}", "NRC %02X (%s)".format(n.nrc, nrcName(n.nrc)), ""))
        }
        out.add(
            EnhancedReading(
                "Note",
                "⚠ Payloads are raw hex on purpose — nothing is decoded. Identify a " +
                    "signal by how it MOVES between an idle sweep and a warm/loaded " +
                    "sweep, then decode it against the car. Read-only.",
                "",
            )
        )
        return out
    }
}
