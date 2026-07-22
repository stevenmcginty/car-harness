package com.carharness.app.obd

/**
 * Shared result model + UDS constants for manufacturer module scans
 * (BmwUds and MazdaUds both produce these; ModulesScreen renders them).
 */

data class UdsDtc(
    val code: String,        // J2012-style letter code, e.g. B37F2 / P0301
    val hex: String,         // raw 3-byte code, what forums search by
    val status: String,
    val flags: List<String>,
    val description: String,
)

data class UdsModuleResult(
    val address: Int,
    val code: String,
    val name: String,
    val present: Boolean,
    val ident: List<Pair<String, String>>, // label -> value
    val dtcs: List<UdsDtc>,
    val error: String?,
    val trace: List<String> = emptyList(), // request→reply trail for field debugging
)

data class UdsScanReport(
    val ok: Boolean,
    val simulated: Boolean,
    val brand: String,       // Brand.id from Brands.all — which scan produced this
    val modules: List<UdsModuleResult>,
    val message: String,
    val vin: String? = null, // consensus VIN across modules — identifies the car
    val absentHint: String? = null, // why the silent modules are silent, if known
)

/**
 * Scan-level VIN: every module that reports DID F190 should agree. The
 * consensus identifies the car at the top of the report; a disagreement is a
 * dealer red flag — a control unit swapped in from another car (crash repair,
 * or the cluster/gateway dance used to disguise mileage).
 */
object VinCheck {
    /** Returns (consensus VIN or null, mismatch warning or ""). */
    fun consensus(modules: List<UdsModuleResult>): Pair<String?, String> {
        val vins = modules.flatMap { m ->
            m.ident.filter { it.first == "VIN" && it.second.length >= 11 }
                .map { m.code to it.second.take(17) }
        }
        val distinct = vins.map { it.second }.distinct()
        val warning = if (distinct.size > 1) {
            " ⚠ VIN MISMATCH: " + vins.joinToString(", ") { "${it.first}=${it.second}" } +
                " — a control unit from another car is fitted (crash repair or clocking risk)."
        } else ""
        val vin = vins.groupingBy { it.second }.eachCount().maxByOrNull { it.value }?.key
        return vin to warning
    }

    /** Manufacturer from the VIN's WMI — enough to headline "what car is this". */
    fun makeFromVin(vin: String?): String? {
        val wmi = vin?.take(3)?.uppercase() ?: return null
        return when (wmi) {
            "VF3", "VR3" -> "Peugeot"
            "VF7", "VR7" -> "Citroën"
            "VR1" -> "DS Automobiles"
            "W0L", "W0V" -> "Opel / Vauxhall"
            "VXK" -> "Vauxhall"
            "WBA", "WBS", "WBY", "WBW" -> "BMW"
            "WMW" -> "MINI"
            "JMZ", "JM1", "JM0", "3MD", "JM7" -> "Mazda"
            "WVW", "WV1", "WV2", "WVG", "1VW", "3VW", "9BW" -> "Volkswagen"
            "WAU", "WA1", "WUA", "TRU" -> "Audi"
            "TMB" -> "Škoda"
            "VSS" -> "SEAT"
            else -> null
        }
    }
}

/** One live value read from a manufacturer-specific DID (mode 22). */
data class EnhancedReading(
    val name: String,
    val value: String,
    val unit: String,
)

object UdsConst {
    val STATUS_BITS = listOf(
        0x01 to "test failed",
        0x02 to "failed this cycle",
        0x04 to "pending",
        0x08 to "confirmed",
        0x10 to "not completed since clear",
        0x20 to "failed since clear",
        0x40 to "not completed this cycle",
        0x80 to "warning light requested",
    )

    val NRC_NAMES = mapOf(
        0x10 to "general reject",
        0x11 to "service not supported",
        0x12 to "sub-function not supported",
        0x13 to "incorrect message length",
        0x21 to "busy, repeat request",
        0x22 to "conditions not correct (ignition on?)",
        0x31 to "request out of range",
        0x33 to "security access denied",
        0x78 to "response pending",
        0x7F to "service not supported in active session",
    )
}

class UdsNegativeResponse(val service: Int, val nrc: Int) : Exception(
    "Negative response to %02X: %02X (%s)".format(
        service, nrc, UdsConst.NRC_NAMES[nrc] ?: "unknown"
    )
)
