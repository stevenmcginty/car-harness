package com.carharness.app.obd

/**
 * The manufacturer registry — the ONE place a brand is defined.
 *
 * Everything brand-specific the UI and AppState need lives on a [Brand]:
 * the chip label, the Modules-tab hint text, the scan/clear entry points and
 * any extra actions (Mazda live extras + snapshot, Peugeot health check).
 * Screens iterate [Brands.all]; AppState dispatches through the lambdas.
 *
 * Adding a manufacturer = one Brand entry here + its XUds scan object +
 * (optionally) its BrandResetData entries. No when-chains anywhere else, and
 * no silent "unknown brand behaves like BMW" fallthrough.
 */

/** One extra per-brand action on the Modules tab, beyond the module scan. */
data class BrandAction(
    val id: String,          // stable id; doubles as the extras "source"
    val label: String,
    val cardTitle: String,   // title of the readings card it produces
    val emptyHint: String,   // shown when the car answered nothing
    val refreshHint: String, // footer under the readings
    val run: suspend (ObdController, (String) -> Unit) -> List<EnhancedReading>,
)

data class Brand(
    val id: String,
    val label: String,
    val scanHint: String,
    val actions: List<BrandAction> = emptyList(),
    val scan: suspend (ObdController, (String) -> Unit) -> UdsScanReport,
    val clear: suspend (ObdController, Int) -> List<UdsDtc>,
)

object Brands {

    val all: List<Brand> = listOf(
        Brand(
            id = "bmw",
            label = "BMW",
            scanHint = "Walks every control unit on the car — engine, gearbox, ABS, " +
                "airbags, cluster, head unit and more — and reads each one's own fault " +
                "memory. This is the deep scan generic OBD apps can't do.",
            scan = { ctrl, onStep -> ctrl.bmwScan(onStep) },
            clear = { ctrl, addr -> ctrl.bmwClear(addr) },
        ),
        Brand(
            id = "mazda",
            label = "Mazda",
            scanHint = "Walks the Mazda's HS-CAN control units — engine and whatever else " +
                "answers on the diagnostic bus — and reads each one's own fault " +
                "memory. Body/comfort modules sit on MS-CAN and need a switchable " +
                "adapter, so they won't appear here.",
            actions = listOf(
                BrandAction(
                    id = "extras",
                    label = "Live extras",
                    cardTitle = "MAZDA LIVE EXTRAS",
                    emptyHint = "This car didn't answer any of the Mazda-specific values. " +
                        "They vary by model year and engine — the generic live " +
                        "dashboard on the Car tab still works.",
                    refreshHint = "One-shot read — tap Live extras again to refresh.",
                    run = { ctrl, onStep -> ctrl.mazdaEnhanced(onStep) },
                ),
                BrandAction(
                    id = "snapshot",
                    label = "Snapshot",
                    cardTitle = "MAZDA SNAPSHOT (BROADCAST)",
                    emptyHint = "No decodable broadcast frames were captured. Make sure the " +
                        "ignition is ON — with the car fully asleep the bus is quiet.",
                    refreshHint = "Passive listen — nothing was sent to the car. Tap Snapshot to refresh.",
                    run = { ctrl, onStep -> ctrl.mazdaSnapshot(onStep) },
                ),
            ),
            scan = { ctrl, onStep -> ctrl.mazdaScan(onStep) },
            clear = { ctrl, addr -> ctrl.mazdaClear(addr) },
        ),
        Brand(
            id = "peugeot",
            label = "Peugeot",
            scanHint = "Scan all modules walks the Peugeot's (PSA) control units by their own " +
                "CAN ids — BSI, engine, gearbox, ABS, airbags, cluster, AdBlue/DPF " +
                "and more — reads each one's fault memory, and cross-checks every " +
                "module's VIN. Health check adds DPF soot/regen (HDi diesels) and " +
                "battery State-of-Health/charge (e-208 & other e-CMP EVs). " +
                "For ~2010+ cars; pre-2004 (VAN) and many 2004-2010 (KWP) cars " +
                "can't be reached over a plain dongle.",
            actions = listOf(
                BrandAction(
                    id = "extras",
                    label = "Health check",
                    cardTitle = "PEUGEOT HEALTH CHECK",
                    emptyHint = "This car didn't answer any of the PSA-specific values. " +
                        "Make sure the ignition is ON — coverage also varies " +
                        "by model year and engine.",
                    refreshHint = "One-shot read-only pass — tap Health check again to refresh. " +
                        "Values this car doesn't support are skipped.",
                    run = { ctrl, onStep -> ctrl.peugeotEnhanced(onStep) },
                ),
            ),
            scan = { ctrl, onStep -> ctrl.peugeotScan(onStep) },
            clear = { ctrl, addr -> ctrl.peugeotClear(addr) },
        ),
    )

    fun byId(id: String): Brand? = all.firstOrNull { it.id == id }
}
