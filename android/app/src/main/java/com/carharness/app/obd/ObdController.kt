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

    suspend fun scan(): ScanResult = mutex.withLock {
        session.ensureProtocol()
        val vin = readVin()

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
                if (vin == null) " (The car did not report a VIN over OBD.)" else "",
        )
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
                        continue
                    }
                    readings.add(LiveReading(key, def.label, value, def.unit))
                    break
                }
            }
        }
        readings
    }
}
