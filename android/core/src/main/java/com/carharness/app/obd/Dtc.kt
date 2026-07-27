package com.carharness.app.obd

import android.content.Context
import org.json.JSONObject

/** Fault-code decoding + descriptions (same database as the desktop app). */
object Dtc {
    private const val LETTERS = "PCBU"
    private var db: Map<String, String> = emptyMap()

    private val categories = listOf(
        "P00" to "Fuel/air metering and auxiliary emission controls",
        "P01" to "Fuel and air metering",
        "P02" to "Fuel and air metering (injector circuit)",
        "P03" to "Ignition system or misfire",
        "P04" to "Auxiliary emissions controls",
        "P05" to "Vehicle speed control and idle control",
        "P06" to "Computer output circuit",
        "P07" to "Transmission",
        "P08" to "Transmission",
        "P09" to "Transmission",
        "P1" to "Manufacturer-specific powertrain code",
        "P2" to "Powertrain (generic)",
        "P3" to "Powertrain (ignition/misfire, manufacturer-specific)",
        "C" to "Chassis (ABS, steering, suspension)",
        "B" to "Body (airbags, locks, lighting, comfort)",
        "U" to "Network / module communication",
    )

    fun load(context: Context) {
        if (db.isNotEmpty()) return
        try {
            val text = context.assets.open("dtc_db.json").bufferedReader().use { it.readText() }
            val json = JSONObject(text)
            val map = mutableMapOf<String, String>()
            for (key in json.keys()) map[key] = json.getString(key)
            db = map
        } catch (_: Exception) {
            db = emptyMap()
        }
    }

    fun decodePair(b1: Int, b2: Int): String {
        val letter = LETTERS[(b1 shr 6) and 0x03]
        return "%c%d%X%X%X".format(letter, (b1 shr 4) and 0x03, b1 and 0x0F, (b2 shr 4) and 0x0F, b2 and 0x0F)
    }

    fun describe(code: String): String {
        db[code]?.let { return it }
        for ((prefix, category) in categories) {
            if (code.startsWith(prefix)) {
                return "$category (no generic description for this exact code)"
            }
        }
        return "Unknown code"
    }

    fun parsePayload(payload: ByteArray, isCan: Boolean): List<String> {
        var body = payload.drop(1)
        if (isCan && body.isNotEmpty()) body = body.drop(1)
        val codes = mutableListOf<String>()
        var i = 0
        while (i + 1 < body.size) {
            val b1 = body[i].toInt() and 0xFF
            val b2 = body[i + 1].toInt() and 0xFF
            if (b1 != 0 || b2 != 0) codes.add(decodePair(b1, b2))
            i += 2
        }
        return codes
    }

    /** Read one DTC service (0x03 stored / 0x07 pending / 0x0A permanent). */
    fun readCodes(session: Elm327Session, service: Int): List<String> {
        val payloads = try {
            session.query(service, null, 8000)
        } catch (e: ObdException) {
            if (e.kind == ObdException.Kind.NO_DATA) return emptyList()
            throw e
        }
        val seen = mutableListOf<String>()
        for (payload in payloads) {
            for (code in parsePayload(payload, session.isCan)) {
                if (code !in seen) seen.add(code)
            }
        }
        return seen
    }
}
