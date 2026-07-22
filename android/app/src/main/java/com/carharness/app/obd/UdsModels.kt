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
)

data class UdsScanReport(
    val ok: Boolean,
    val simulated: Boolean,
    val brand: String,       // "bmw" | "mazda" — which scan produced this
    val modules: List<UdsModuleResult>,
    val message: String,
)

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
