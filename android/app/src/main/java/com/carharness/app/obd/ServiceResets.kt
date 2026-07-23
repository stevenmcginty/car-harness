package com.carharness.app.obd

/**
 * Service resets — the after-servicing jobs a dealer does daily: oil service
 * reset, brake-pad / brake-fluid reminders, inspection counters.
 *
 * Two kinds:
 *  - UDS resets run over the dongle as a short data-driven script against one
 *    module (mirrors the desktop's backend/obd/uds_reset.py step forms).
 *  - PROCEDURE resets are the official dash-button sequences for cars where
 *    no public diagnostic route exists — the app shows the steps instead of
 *    pretending it can send them.
 *
 * Every entry carries provenance (source) and a confidence grade; nothing is
 * marked "verified" until it has worked on a real car. The tables mirror
 * backend/profiles/<brand>/resets.py — keep both in sync.
 */

sealed class ResetStep {
    data class Session(val level: Int) : ResetStep()
    data class Routine(val rid: Int, val sub: Int = 0x01, val dataHex: String = "") : ResetStep()
    data class WriteDid(val did: Int, val dataHex: String) : ResetStep()
    data class ReadDid(val did: Int) : ResetStep()
    data class Delay(val ms: Long) : ResetStep()

    fun describe(): String = when (this) {
        is Session -> "Extended session (10 %02X)".format(level)
        is Routine -> "Routine 31 %02X %04X".format(sub, rid) +
            (if (dataHex.isEmpty()) "" else " $dataHex")
        is WriteDid -> "Write DID %04X <- %s".format(did, dataHex)
        is ReadDid -> "Read DID %04X".format(did)
        is Delay -> "Wait ${ms}ms"
    }
}

data class ServiceReset(
    val id: String,
    val brand: String,             // "bmw" | "mazda" | "peugeot" | "vw"
    val name: String,
    val description: String,
    val uds: Boolean,              // true = runnable over the dongle
    val confidence: String,        // "verified" | "community" | "experimental"
    val source: String,
    val models: String,
    val cautions: List<String> = emptyList(),
    // UDS resets only:
    val targetCode: String = "",
    val targetName: String = "",
    val address: Int = 0,          // BMW: diagnostic address; Mazda/PSA: request CAN id
    val steps: List<ResetStep> = emptyList(),
    // Dash procedures only:
    val procedure: List<String> = emptyList(),
)

data class ResetOutcome(val ok: Boolean, val message: String, val log: List<String>)

object ServiceResets {

    fun forBrand(brand: String): List<ServiceReset> = when (brand) {
        "bmw" -> BMW
        "mazda" -> MAZDA
        "peugeot" -> PEUGEOT
        "vw" -> VW
        "jlr" -> JLR
        else -> emptyList() // unknown brand must not silently show BMW's resets
    }

    /**
     * Interpret one reset's steps over a brand transport. ``call`` sends one
     * UDS request on the already-aimed module and returns the positive
     * payload (each brand's request() handles 0x78 pending itself).
     */
    fun executeSteps(
        steps: List<ResetStep>,
        onProgress: (String) -> Unit,
        call: (service: Int, dataHex: String, timeoutMs: Long) -> ByteArray,
    ): List<String> {
        val log = mutableListOf<String>()
        for (step in steps) {
            onProgress(step.describe())
            when (step) {
                is ResetStep.Session -> call(0x10, "%02X".format(step.level), 3000)
                is ResetStep.Routine ->
                    call(0x31, "%02X%04X%s".format(step.sub, step.rid, step.dataHex), 8000)
                is ResetStep.WriteDid -> call(0x2E, "%04X%s".format(step.did, step.dataHex), 6000)
                is ResetStep.ReadDid -> call(0x22, "%04X".format(step.did), 3000)
                is ResetStep.Delay -> Thread.sleep(step.ms)
            }
            log.add(step.describe())
        }
        return log
    }

    // ------------------------------------------------------------ registries
    // The actual definitions live in BrandResetData.kt (kept in sync with
    // backend/profiles/<brand>/resets.py). Referenced here so forBrand() has a
    // single dispatch point.

    val BMW: List<ServiceReset> get() = BrandResetData.BMW
    val MAZDA: List<ServiceReset> get() = BrandResetData.MAZDA
    val PEUGEOT: List<ServiceReset> get() = BrandResetData.PEUGEOT
    val VW: List<ServiceReset> get() = BrandResetData.VW
    val JLR: List<ServiceReset> get() = BrandResetData.JLR
}
