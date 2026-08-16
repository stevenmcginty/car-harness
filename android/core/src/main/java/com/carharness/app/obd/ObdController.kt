package com.carharness.app.obd

import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

data class DtcRecord(val code: String, val description: String, val status: String)

data class ScanResult(
    val ok: Boolean,
    val simulated: Boolean,
    val vin: String?,
    val protocol: String,
    val voltage: String,
    val firmware: String,
    val milOn: Boolean,
    val dtcCount: Int,
    val stored: List<DtcRecord>,
    val pending: List<DtcRecord>,
    val permanent: List<DtcRecord>,
    val message: String,
)

data class LiveReading(val key: String, val label: String, val value: Double, val unit: String)

/**
 * High-level scan / live / clear over one ELM327 session — the phone-side
 * twin of the desktop's backend/obd/service.py. All calls are suspend and
 * serialised (an ELM327 is strictly one command at a time).
 */
class ObdController(private val session: Elm327Session, val simulated: Boolean) {
    private val mutex = Mutex()
    private var supported: Set<Int>? = null

    private fun readVin(): String? {
        val payloads = try {
            session.query(0x09, 0x02, 10000)
        } catch (_: Exception) {
            return null
        }
        for (payload in payloads) {
            if (payload.size < 4 || (payload[1].toInt() and 0xFF) != 0x02) continue
            val text = payload.drop(3)
                .map { it.toInt() and 0xFF }
                .filter { it in 33..126 }
                .map { it.toChar() }
                .joinToString("")
                .trim()
            if (text.length >= 11) return text.take(17)
        }
        return null
    }

    /**
     * Read the car's identity once, right after connecting: the VIN over the
     * universal OBD Mode 09 read, decoded to make + year where the VIN format
     * really encodes one. When Mode 09 stays silent — normal on KWP-era cars
     * (a 2009-2013 308 CC's MED17 ECU ignores it) — the PSA ladder is tried
     * as a manufacturer fallback before giving up: Mode 09 physical, UDS
     * 22 F1 90, then the KWP 81 + ident zones on the engine's PSA id pair.
     * A null VIN means none of that answered (ignition off?) — the UI still
     * shows the card so the user sees the app tried. Serialised on the same
     * mutex, so it never races the live dashboard or a scan.
     */
    suspend fun identify(): VehicleIdentity = mutex.withLock {
        val vin = try {
            session.ensureProtocol()
            readVin() ?: PeugeotUds.vinFromCar(session)
        } catch (_: Exception) {
            null
        }
        VinDecode.decode(vin)
    }

    suspend fun scan(): ScanResult = mutex.withLock {
        session.ensureProtocol()
        // Generic emissions reads FIRST: the PSA VIN ladder's last rung opens
        // a KWP session on the engine, and while that session is live the
        // ECU stops answering generic Mode 01 (field-verified on the EP6 —
        // the two stacks are mutually exclusive). The session lapses on its
        // own after a few seconds of silence.
        val vin0 = readVin()

        var milOn = false
        var dtcCount = 0
        try {
            for (payload in session.query(0x01, 0x01, 8000)) {
                if (payload.size >= 3 && (payload[1].toInt() and 0xFF) == 0x01) {
                    val a = payload[2].toInt() and 0xFF
                    milOn = milOn || (a and 0x80) != 0
                    dtcCount += a and 0x7F
                }
            }
        } catch (_: Exception) {
        }

        fun read(service: Int): List<String>? = try {
            Dtc.readCodes(session, service)
        } catch (_: Exception) {
            null
        }

        val stored = read(0x03)
        val pending = read(0x07)
        val permanent = read(0x0A)

        fun records(list: List<String>?, status: String): List<DtcRecord> =
            (list ?: emptyList()).map {
                val desc = Dtc.describe(it)
                DtcRecord(it, if (simulated) "SIMULATED — $desc" else desc, status)
            }

        val nStored = stored?.size ?: 0
        // PSA ladder last (see the comment up top): only fires when the
        // generic Mode 09 read came back empty.
        val vin = readVinFallback(vin0)
        val summary = when {
            nStored == 0 && dtcCount > 0 ->
                "The car reports $dtcCount fault(s) but they are in modules this " +
                    "generic scan can't list — a manufacturer-specific scan would be needed."
            nStored == 0 -> "No stored fault codes." + if (milOn) " MIL (engine light) is ON." else ""
            else -> "$nStored stored fault code(s)." + if (milOn) " MIL (engine light) is ON." else ""
        }
        val simPrefix = if (simulated) "SIMULATED — no real car connected. " else ""

        ScanResult(
            ok = true,
            simulated = simulated,
            vin = vin,
            protocol = session.protocol.ifEmpty { "unknown" },
            voltage = session.voltage(),
            firmware = session.firmware,
            milOn = milOn,
            dtcCount = maxOf(dtcCount, nStored),
            stored = records(stored, "stored"),
            pending = records(pending, "pending"),
            permanent = records(permanent, "permanent"),
            message = simPrefix + summary +
                if (vin == null) " (The car did not report a VIN on any read — generic Mode 09 or the PSA ladder.)" else "",
        )
    }

    /** Mode 09 came back empty -> try the PSA ladder (KWP-era cars). */
    private fun readVinFallback(generic: String?): String? =
        if (generic != null) generic
        else try {
            PeugeotUds.vinFromCar(session)
        } catch (_: Exception) {
            null
        }

    suspend fun clear() {
        mutex.withLock {
            session.ensureProtocol()
            try {
                session.query(0x04, null, 10000)
            } catch (e: ObdException) {
                if (e.kind != ObdException.Kind.NO_DATA) throw e
            }
        }
    }

    /** BMW F-series full-module scan. Serialised with everything else. */
    suspend fun bmwScan(onProgress: (String) -> Unit): UdsScanReport = mutex.withLock {
        BmwUds.scan(session, simulated, onProgress)
    }

    /** Clear one BMW module's fault memory; returns that module's fresh DTCs. */
    suspend fun bmwClear(address: Int): List<UdsDtc> = mutex.withLock {
        BmwUds.clearDtcs(session, address)
    }

    /** Mazda HS-CAN module scan (11-bit UDS). Serialised with everything else. */
    suspend fun mazdaScan(onProgress: (String) -> Unit): UdsScanReport = mutex.withLock {
        MazdaUds.scan(session, simulated, onProgress)
    }

    /** Clear one Mazda module's fault memory; returns that module's fresh DTCs. */
    suspend fun mazdaClear(address: Int): List<UdsDtc> = mutex.withLock {
        MazdaUds.clearDtcs(session, address)
    }

    /** One pass over the Mazda enhanced (mode-22) values. */
    suspend fun mazdaEnhanced(onProgress: (String) -> Unit): List<EnhancedReading> =
        mutex.withLock {
            MazdaUds.readEnhanced(session, onProgress)
        }

    /** Passive broadcast-CAN snapshot (ATMA sniff). Read-only. */
    suspend fun mazdaSnapshot(onProgress: (String) -> Unit): List<EnhancedReading> =
        mutex.withLock {
            MazdaCan.snapshot(session, onProgress)
        }

    /** Peugeot / PSA module scan (per-module UDS id pairs). Serialised. */
    suspend fun peugeotScan(onProgress: (String) -> Unit): UdsScanReport = mutex.withLock {
        PeugeotUds.scan(session, simulated, onProgress)
    }

    /** Clear one Peugeot module's fault memory; returns that module's fresh DTCs. */
    suspend fun peugeotClear(address: Int): List<UdsDtc> = mutex.withLock {
        PeugeotUds.clearDtcs(session, address)
    }

    /** One pass over the Peugeot health-check values (PSA mode-22 DIDs). */
    suspend fun peugeotEnhanced(onProgress: (String) -> Unit): List<EnhancedReading> =
        mutex.withLock {
            PeugeotUds.readEnhanced(session, onProgress)
        }

    /** VW / Audi (VAG) module scan (per-module UDS id pairs, +8 / +0x6A). Serialised. */
    suspend fun vwScan(onProgress: (String) -> Unit): UdsScanReport = mutex.withLock {
        VwUds.scan(session, simulated, onProgress)
    }

    /** Clear one VW/Audi module's fault memory; returns that module's fresh DTCs. */
    suspend fun vwClear(address: Int): List<UdsDtc> = mutex.withLock {
        VwUds.clearDtcs(session, address)
    }

    /** One pass over the VW/Audi live extras (VAG mode-22 DIDs). */
    suspend fun vwEnhanced(onProgress: (String) -> Unit): List<EnhancedReading> =
        mutex.withLock {
            VwUds.readEnhanced(session, onProgress)
        }

    /** Mercedes engine + gearbox module scan (UDS then KWP2000-over-CAN). Serialised. */
    suspend fun mercedesScan(onProgress: (String) -> Unit): UdsScanReport = mutex.withLock {
        MercedesUds.scan(session, simulated, onProgress)
    }

    /** Clear one Mercedes module's fault memory; returns that module's fresh DTCs. */
    suspend fun mercedesClear(address: Int): List<UdsDtc> = mutex.withLock {
        MercedesUds.clearDtcs(session, address)
    }

    /** One pass over the Mercedes live extras (722.9/9G ATF temp + generic Mode-01). */
    suspend fun mercedesEnhanced(onProgress: (String) -> Unit): List<EnhancedReading> =
        mutex.withLock {
            MercedesUds.readEnhanced(session, onProgress)
        }

    /** JLR driveline module scan (11-bit UDS then KWP2000-over-CAN). Serialised. */
    suspend fun jlrScan(onProgress: (String) -> Unit): UdsScanReport = mutex.withLock {
        JlrUds.scan(session, simulated, onProgress)
    }

    /** Clear one JLR module's fault memory; returns that module's fresh DTCs. */
    suspend fun jlrClear(address: Int): List<UdsDtc> = mutex.withLock {
        JlrUds.clearDtcs(session, address)
    }

    /** One pass over the JLR live extras (oil temp/level/volume, ZF ATF, e-diff). */
    suspend fun jlrEnhanced(onProgress: (String) -> Unit): List<EnhancedReading> =
        mutex.withLock {
            JlrUds.readEnhanced(session, onProgress)
        }

    /** JLR address discovery: sweep the 0x700-0x7FF request-id block. Read-only. */
    suspend fun jlrDiscover(onProgress: (String) -> Unit): List<EnhancedReading> =
        mutex.withLock {
            JlrDiscover.discover(session, simulated, onProgress)
        }

    /** Walk one 0x22 DID range on a JLR module, keeping every raw answer. */
    suspend fun jlrSweep(
        onProgress: (String) -> Unit,
        first: String = "0300",
        last: String = "05FF",
        target: String = "7E0",
        extended: Boolean = false,
    ): List<EnhancedReading> = mutex.withLock {
        JlrSweep.sweep(session, simulated, onProgress, first, last, target, extended)
    }

    /** One read-only AdBlue/SCR + DPF health pass (X260 3.0d shape). */
    suspend fun jlrAdblueHealth(onProgress: (String) -> Unit): List<EnhancedReading> =
        mutex.withLock {
            JlrAdBlue.health(session, simulated, onProgress)
        }

    /** Timed rpm + raw DID log through warm-up (default 3 minutes). Read-only. */
    suspend fun jlrAdblueDriveLog(
        onProgress: (String) -> Unit,
        seconds: Long = 180,
    ): List<EnhancedReading> = mutex.withLock {
        JlrAdBlue.driveLog(session, simulated, onProgress, seconds)
    }

    /** Porsche module scan (Porsche-own UDS/KWP-over-CAN, VAG-MLB reuse, K-line floor). Serialised. */
    suspend fun porscheScan(onProgress: (String) -> Unit): UdsScanReport = mutex.withLock {
        PorscheUds.scan(session, simulated, onProgress)
    }

    /** Clear one Porsche module's fault memory; returns that module's fresh DTCs. */
    suspend fun porscheClear(address: Int): List<UdsDtc> = mutex.withLock {
        PorscheUds.clearDtcs(session, address)
    }

    /** One pass over the Porsche live extras (997.2 KWP oil temp / generic Mode-01). */
    suspend fun porscheEnhanced(onProgress: (String) -> Unit): List<EnhancedReading> =
        mutex.withLock {
            PorscheUds.readEnhanced(session, onProgress)
        }

    /** Fiat / Alfa (FCA) module scan (29-bit UDS, with an 11-bit CAN-era fallback). Serialised. */
    suspend fun fiatScan(onProgress: (String) -> Unit): UdsScanReport = mutex.withLock {
        FiatUds.scan(session, simulated, onProgress)
    }

    /** Clear one Fiat / Alfa module's fault memory; returns that module's fresh DTCs. */
    suspend fun fiatClear(address: Int): List<UdsDtc> = mutex.withLock {
        FiatUds.clearDtcs(session, address)
    }

    /** One pass over the Fiat / Alfa live extras (danardi Giulia DIDs / generic Mode-01). */
    suspend fun fiatEnhanced(onProgress: (String) -> Unit): List<EnhancedReading> =
        mutex.withLock {
            FiatUds.readEnhanced(session, onProgress)
        }

    /**
     * Run one service reset over the dongle — CHANGES THE CAR (uds resets only).
     * Serialised with everything else. Dispatches on the reset's brand so the
     * right transport addressing (BMW 6F1 gateway vs standard 11-bit) is used.
     */
    suspend fun performReset(
        reset: ServiceReset,
        onProgress: (String) -> Unit,
    ): ResetOutcome = mutex.withLock {
        if (simulated) {
            val log = reset.steps.map { it.describe() }
            return@withLock ResetOutcome(
                ok = true,
                message = "SIMULATED — '${reset.name}' would run these steps; no real car touched.",
                log = log,
            )
        }
        try {
            val log = when (reset.brand) {
                "bmw" -> BmwUds.performReset(session, reset, onProgress)
                "mazda" -> MazdaUds.performReset(session, reset, onProgress)
                "peugeot" -> PeugeotUds.performReset(session, reset, onProgress)
                else -> throw IllegalArgumentException(
                    "No reset engine for brand '${reset.brand}'."
                )
            }
            ResetOutcome(
                ok = true,
                message = "'${reset.name}' sent. Cycle the ignition and confirm on the dash — " +
                    "faults come back if the underlying part is still worn.",
                log = log,
            )
        } catch (e: Exception) {
            ResetOutcome(ok = false, message = "'${reset.name}' failed: ${e.message}", log = emptyList())
        }
    }

    suspend fun live(keys: List<String> = Pids.dashboardKeys): List<LiveReading> = mutex.withLock {
        session.ensureProtocol()
        if (supported == null) supported = Pids.readSupported(session)
        val sup = supported ?: emptySet()
        val readings = mutableListOf<LiveReading>()
        for (key in keys) {
            val def = Pids.byKey[key] ?: continue
            if (sup.isNotEmpty() && def.pid !in sup) continue
            val payloads = try {
                session.query(0x01, def.pid, 4000)
            } catch (_: Exception) {
                continue
            }
            for (payload in payloads) {
                if (payload.size >= 2 + def.nbytes && (payload[1].toInt() and 0xFF) == def.pid) {
                    val value = try {
                        def.decode(payload.drop(2).toByteArray())
                    } catch (_: Exception) {
                        null
                    } ?: continue
                    // Sanity bounds: a clone dongle answering rubbish drops the
                    // value rather than displaying it.
                    if (def.lo != null && value < def.lo) continue
                    if (def.hi != null && value > def.hi) continue
                    readings.add(LiveReading(key, def.label, value, def.unit))
                    break
                }
            }
        }
        readings
    }

    /** Which of the standard catalogue this car reports supporting. An empty
     * bitmap answer returns the whole catalogue — polling then finds out the
     * honest way, one value at a time. */
    suspend fun liveSupportedKeys(): Set<String> = mutex.withLock {
        session.ensureProtocol()
        if (supported == null) supported = Pids.readSupported(session)
        val sup = supported ?: emptySet()
        if (sup.isEmpty()) Pids.all.map { it.key }.toSet()
        else Pids.all.filter { it.pid in sup }.map { it.key }.toSet()
    }
}
