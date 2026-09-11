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
    /**
     * A reference view that touches no car. When set, [run] is never called
     * and the button works with nothing plugged in -- which is the whole point
     * of a lookup you want while standing next to the car deciding what to
     * ask it. Actions without this need a live connection, as before.
     */
    val dataOnly: (() -> List<EnhancedReading>)? = null,
)

data class Brand(
    val id: String,
    val label: String,
    val scanHint: String,
    /** Sub-marques and group names the picker's search should match — "Audi"
     *  must find VW/Audi, "Land Rover" must find Jaguar/LR. Search aid only,
     *  not a coverage claim. */
    val aliases: List<String> = emptyList(),
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
                        "dashboard on the Live tab still works.",
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
            id = "ford",
            label = "Ford",
            aliases = listOf("Ranger", "Transit", "Focus", "Fiesta"),
            scanHint = "Walks the Ford HS-CAN modules (PCM, TCM, ABS, BCM, cluster, airbag, " +
                "steering, SYNC...) reading each one's own fault memory; MS-CAN body " +
                "modules need a switchable adapter.",
            actions = listOf(
                BrandAction(
                    id = "extras",
                    label = "Live extras",
                    cardTitle = "FORD LIVE EXTRAS",
                    emptyHint = "This car didn't answer any of the Ford-specific values. " +
                        "Make sure the ignition is ON — the values come from Ford's own " +
                        "IDS diesel-PCM tables, so coverage varies by model year and engine.",
                    refreshHint = "One-shot read-only pass — tap Live extras again to refresh. " +
                        "Values this car doesn't support are skipped.",
                    run = { ctrl, onStep -> ctrl.fordEnhanced(onStep) },
                ),
            ),
            scan = { ctrl, onStep -> ctrl.fordScan(onStep) },
            clear = { ctrl, addr -> ctrl.fordClear(addr) },
        ),
        Brand(
            id = "nissan",
            label = "Nissan",
            aliases = listOf("370Z", "Qashqai", "Juke", "Leaf", "Navara", "Infiniti"),
            scanHint = "Walks both halves of the Nissan HS-CAN bus — the powertrain block " +
                "(engine, gearbox) that answers on request + 8, and Nissan's own body / " +
                "comfort / AV block that answers on request + 0x20, where the BCM and the " +
                "AV head unit live. The Bose amp has no diagnostic address of its own: its " +
                "faults are stored by the AV unit, so they show up there.",
            scan = { ctrl, onStep -> ctrl.nissanScan(onStep) },
            clear = { ctrl, addr -> ctrl.nissanClear(addr) },
        ),
        Brand(
            id = "peugeot",
            label = "Peugeot",
            aliases = listOf("Citroen", "Citroën", "DS", "PSA"),
            scanHint = "Reads the VIN first (so you always know the car), then walks the " +
                "PSA control units the dongle can reach — engine, gearbox, ABS/ESP, " +
                "steering, AdBlue — speaking both UDS and Peugeot's own KWP dialect. " +
                "Body modules (BSI, airbag, cluster, radio) sit on a separate diag " +
                "CAN at OBD pins 3 & 8: a cheap PSA pin-swap adapter brings them in. " +
                "Health check adds DPF soot/regen (HDi) and EV battery health (e-208).",
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
        Brand(
            id = "vw",
            label = "VW/Audi",
            aliases = listOf("Volkswagen", "Audi", "Skoda", "Škoda", "SEAT", "Cupra", "VAG"),
            scanHint = "Reads the VIN first (so you always know the car), then walks the VAG " +
                "control units — engine, gearbox/DSG, gateway, cluster, ABS, airbag, " +
                "steering and more — as pure UDS, reading each one's own fault memory. " +
                "The gateway routes every module to the OBD port, so no pin-swap adapter " +
                "is needed. UDS-era only: VW/Škoda/SEAT ~2012+ (MQB) and Audi ~2008+ (B8); " +
                "pre-2012 (TP2.0/K-line) cars can't be module-scanned over a plain dongle. " +
                "SFD on 2020+ cars blocks writes only — these read-only checks are unaffected. " +
                "Live extras adds coolant/oil/battery plus raw diesel-DPF and EV values.",
            actions = listOf(
                BrandAction(
                    id = "extras",
                    label = "Live extras",
                    cardTitle = "VW LIVE EXTRAS",
                    emptyHint = "This car didn't answer any of the VAG-specific values. " +
                        "Make sure the ignition is ON — coverage also varies by model " +
                        "year and engine.",
                    refreshHint = "One-shot read-only pass — tap Live extras again to refresh. " +
                        "Values this car doesn't support are skipped.",
                    run = { ctrl, onStep -> ctrl.vwEnhanced(onStep) },
                ),
            ),
            scan = { ctrl, onStep -> ctrl.vwScan(onStep) },
            clear = { ctrl, addr -> ctrl.vwClear(addr) },
        ),
        Brand(
            id = "mercedes",
            label = "Mercedes",
            aliases = listOf("Benz", "AMG"),
            scanHint = "Reads the VIN first (so you always know the car), then the two modules a " +
                "dongle reaches at manufacturer depth on a Mercedes — the engine and the automatic " +
                "gearbox — reading each one's own fault memory, laddering UDS then Mercedes' " +
                "KWP2000-over-CAN dialect. This is an honest engine + gearbox deep read, NOT a " +
                "VCDS/ISTA all-modules walk: body modules (ABS/ESP, SRS, SAM, EZS, cluster, " +
                "Airmatic, climate) use Mercedes' proprietary addressing behind the gateway and are " +
                "STAR-only — no dongle reaches them. Works ~2004-2018; ~2018+ cars are DoIP/Ethernet " +
                "and give generic OBD-II only. Live extras adds the 722.9/9G ATF gearbox temperature.",
            actions = listOf(
                BrandAction(
                    id = "extras",
                    label = "Live extras",
                    cardTitle = "MERCEDES LIVE EXTRAS",
                    emptyHint = "This car didn't answer any of the Mercedes-specific values. " +
                        "Make sure the ignition is ON — the 722.9/9G ATF temp is the one " +
                        "Mercedes-specific extra, and the generic live dashboard on the Live tab " +
                        "still works.",
                    refreshHint = "One-shot read-only pass — tap Live extras again to refresh. " +
                        "Values this car doesn't support are skipped.",
                    run = { ctrl, onStep -> ctrl.mercedesEnhanced(onStep) },
                ),
            ),
            scan = { ctrl, onStep -> ctrl.mercedesScan(onStep) },
            clear = { ctrl, addr -> ctrl.mercedesClear(addr) },
        ),
        Brand(
            id = "fiat",
            label = "Fiat/Alfa",
            aliases = listOf("Alfa Romeo", "Abarth", "Jeep", "FCA"),
            scanHint = "Reads the VIN first (so you always know the car), then walks the Fiat / Alfa " +
                "modules. On a UDS-era FCA (Alfa Giulia/Stelvio, Fiat 500X/Tipo/124, Jeep Renegade, " +
                "≈2014+) it reads engine, gearbox, ABS/ESC, steering and the body computer over 29-bit " +
                "CAN — each one's own fault memory. Reads work even on a 2018+ Security-Gateway car: " +
                "the SGW blocks only clearing and actuations, not reads. On a CAN-era Fiat (≈2005-2016) " +
                "you get engine + ABS on the bare dongle; the body computer, cluster and airbag sit on " +
                "a low-speed B-CAN and need a MultiECUScan-style B-CAN pin adapter (UK: Gendan). Older " +
                "K-line cars (Alfa 147/156/GT, older Punto) aren't supported on this dongle. TPMS is " +
                "never touched — polling it can engage the immobilizer. Live extras adds the Giulia " +
                "diesel DPF soot/regen, oil/coolant/boost and gearbox temperature.",
            actions = listOf(
                BrandAction(
                    id = "extras",
                    label = "Live extras",
                    cardTitle = "FIAT / ALFA LIVE EXTRAS",
                    emptyHint = "This car didn't answer any of the FCA-specific values. " +
                        "Make sure the ignition is ON — the danardi Giulia values are captured " +
                        "on a diesel MY2016, so coverage varies by model year and engine.",
                    refreshHint = "One-shot read-only pass — tap Live extras again to refresh. " +
                        "Single-source scaling; verify against the car. Values this car doesn't " +
                        "support are skipped.",
                    run = { ctrl, onStep -> ctrl.fiatEnhanced(onStep) },
                ),
            ),
            scan = { ctrl, onStep -> ctrl.fiatScan(onStep) },
            clear = { ctrl, addr -> ctrl.fiatClear(addr) },
        ),
        Brand(
            id = "jlr",
            label = "Jaguar/LR",
            aliases = listOf("Jaguar", "Land Rover", "Range Rover"),
            scanHint = "Reads the VIN first (so you always know the car), then the modules a " +
                "dongle genuinely reaches on a 2006-2016 CAN-era Jaguar / Land Rover — engine, " +
                "ZF automatic gearbox and (where fitted) the rear e-diff — laddering UDS then " +
                "KWP2000-over-CAN for the older cars, each module's own fault memory. Honest " +
                "scope: ABS, air suspension and EPB module addresses were never published, and " +
                "body modules sit on an MS-CAN that isn't wired to the OBD pins — a GAP IIDTool " +
                "or SDD reaches those. MY17+ cars (Disco 5, Velar, 2018+ Jaguars, Defender " +
                "L663, I-Pace) do manufacturer diagnostics over DoIP/Ethernet, and the Evoque " +
                "L538 / Disco Sport L550 gateway-filter the port — those give generic OBD only, " +
                "and the scan says which wall it hit. The Service tab's dash-procedure resets " +
                "(oil, EPB pad mode, DPF drive cycle, TPMS) work on EVERY era, dongle or not. " +
                "Live extras adds oil temp/level/volume (dipstick-less cars), boost, ZF gearbox " +
                "and e-diff temperatures.",
            actions = listOf(
                BrandAction(
                    id = "extras",
                    label = "Live extras",
                    cardTitle = "JLR LIVE EXTRAS",
                    emptyHint = "This car didn't answer any of the JLR-specific values. " +
                        "Make sure the ignition is ON — the values are captured on " +
                        "X351/XK petrols, so coverage varies by model year and engine; " +
                        "DoIP-era cars fall back to the generic set.",
                    refreshHint = "One-shot read-only pass — tap Live extras again to refresh. " +
                        "Single-source scaling; verify against the car. Values this car " +
                        "doesn't support are skipped.",
                    run = { ctrl, onStep -> ctrl.jlrEnhanced(onStep) },
                ),
                BrandAction(
                    id = "discover",
                    label = "Discover",
                    cardTitle = "JLR ADDRESS DISCOVERY",
                    emptyHint = "Nothing answered anywhere in the 0x700-0x7FF request-id " +
                        "block. Make sure the ignition is ON — and remember MY17+ cars " +
                        "do manufacturer diagnostics over DoIP, which no dongle speaks.",
                    refreshHint = "Read-only sweep — presence probes and identification " +
                        "reads only. Roughly 2-5 minutes over Bluetooth (each silent " +
                        "address costs one short timeout); tap Discover to re-run.",
                    run = { ctrl, onStep -> ctrl.jlrDiscover(onStep) },
                ),
                BrandAction(
                    id = "sweep",
                    label = "DID sweep",
                    cardTitle = "JLR DID SWEEP",
                    emptyHint = "No DID in the swept range answered with data. The module " +
                        "is listening but implements none of these ids — pick another " +
                        "sweep range above. Do NOT bother with 2100-21FF or 2200-22FF: " +
                        "all 512 refused on the L494 and SDD puts nothing there either.",
                    refreshHint = "One DID at a time over 0x22 on the PCM, for the sweep " +
                        "range picked above — raw hex payloads, nothing decoded. The two " +
                        "Dxxx ranges are where SDD 169 names the EGR/turbo offsets and " +
                        "the particulate-filter regen statistics, which nothing has yet " +
                        "asked this car for. Roughly 0.6 s per id, so 256 ids is about " +
                        "two and a half minutes; tap DID sweep to re-run.",
                    run = { ctrl, onStep -> ctrl.jlrSweep(onStep) },
                ),
                BrandAction(
                    id = "adblue",
                    label = "AdBlue health",
                    cardTitle = "JLR ADBLUE / DPF HEALTH",
                    emptyHint = "Nothing was read — make sure the ignition is ON. The " +
                        "X260-specific aftertreatment values also need the DID capture " +
                        "the sweep tool exists for; the fault read and the sourced " +
                        "assessment stand on their own.",
                    refreshHint = "One read-only pass — Mode-01 baseline, known-table " +
                        "faults, known engine DIDs raw. SCR/DPF values stay capture " +
                        "pending until the DIDs are mapped; nothing is invented.",
                    run = { ctrl, onStep -> ctrl.jlrAdblueHealth(onStep) },
                ),
                BrandAction(
                    id = "drivelog",
                    label = "Drive log",
                    cardTitle = "JLR ADBLUE DRIVE LOG",
                    emptyHint = "No ticks were recorded — the log needs the dongle awake " +
                        "and the ignition ON for the whole run.",
                    refreshHint = "Rpm plus raw DID payloads, for the length and the " +
                        "watch set picked above — drive through warm-up while it runs. " +
                        "The watch set decides which DIDs each tick reads: the full " +
                        "candidate set for breadth, DPF + EGR or Urea for a higher " +
                        "per-DID sample rate. A regen needs ~20 min at 500 °C+, so a " +
                        "longer window is needed to span one. Every tick lands in the " +
                        "CSV as it happens; share it from the button below.",
                    run = { ctrl, onStep -> ctrl.jlrAdblueDriveLog(onStep) },
                ),
                // A pure data view: no car contact at all, so it works with the
                // dongle unplugged — which is when you most want to look up
                // what a module address or an identifier actually is.
                BrandAction(
                    id = "sdd",
                    label = "SDD data",
                    cardTitle = "JLR SDD 169 DEALER DATA",
                    emptyHint = "The SDD tables did not load — this is compiled-in data, " +
                        "so an empty card means a build problem, not a car problem.",
                    refreshHint = "Jaguar Land Rover's own dealer data, decrypted from SDD " +
                        "169 (L494 MY16): 57 module addresses, 37 named diesel-PCM " +
                        "identifiers with factory scaling, 24 routine identifiers. The " +
                        "routines are REFERENCE ONLY — this app sends none of them. Rows " +
                        "marked [candidate] have an authoritative name against a value " +
                        "this car has not proved. No car contact: this reads the same " +
                        "with the dongle unplugged.",
                    run = { ctrl, _ -> ctrl.jlrDealerData() },
                    dataOnly = { JlrSdd.reference() },
                ),
            ),
            scan = { ctrl, onStep -> ctrl.jlrScan(onStep) },
            clear = { ctrl, addr -> ctrl.jlrClear(addr) },
        ),
        Brand(
            id = "porsche",
            label = "Porsche",
            scanHint = "Reads the VIN first (so you always know the car), then the modules a " +
                "dongle genuinely reaches on a Porsche. On the Porsche-own CAN cars — 997/987 " +
                "(2005-12) and 991.1 / 981 / Panamera 970 (2010-16) — it reads the engine and " +
                "probes the gearbox, laddering UDS then Porsche's KWP-over-CAN dialect, each " +
                "module's own fault memory. Honest scope: Porsche never published its per-module " +
                "CAN IDs, so ABS/PSM, airbag, cluster, PASM/air-suspension and body modules can't " +
                "be addressed by a dongle — that's engine + basics, not a fault. On a Cayenne 958 " +
                "/ Macan (VAG-MLB underneath) some VAG-standard modules answer on the shared " +
                "addresses; others are Porsche-specific. Walls the scan names when it hits them: " +
                "996/986 route their body modules onto a second K-line at OBD pin 3 (K1/pin-7 " +
                "gives generic engine EOBD only — pin 3 needs a pin-swap adapter), the Cayenne " +
                "955/957 speak VW TP2.0 and MY17+ cars (Panamera 971, Cayenne 9YA, 992, Taycan) " +
                "use DoIP/Ethernet — those give generic OBD only. Camshaft deviation, over-rev " +
                "history and electronic oil level are Porsche measuring values with no public DID. " +
                "The Service tab's dash resets (997/991 trip-stalk, Macan/Cayenne wheel menu) work " +
                "on every era. Live extras adds the 997.2 KWP engine oil temperature.",
            actions = listOf(
                BrandAction(
                    id = "extras",
                    label = "Live extras",
                    cardTitle = "PORSCHE LIVE EXTRAS",
                    emptyHint = "This car didn't answer any of the Porsche-specific values. " +
                        "Make sure the ignition is ON — the KWP oil-temperature read is a " +
                        "997/987-era value; UDS-era, DoIP-era and K-line cars fall back to the " +
                        "generic set, and the live dashboard on the Live tab still works.",
                    refreshHint = "One-shot read-only pass — tap Live extras again to refresh. " +
                        "Single-source scaling; verify against the car. Values this car doesn't " +
                        "support are skipped.",
                    run = { ctrl, onStep -> ctrl.porscheEnhanced(onStep) },
                ),
            ),
            scan = { ctrl, onStep -> ctrl.porscheScan(onStep) },
            clear = { ctrl, addr -> ctrl.porscheClear(addr) },
        ),
    )

    fun byId(id: String): Brand? = all.firstOrNull { it.id == id }

    /**
     * Which brand profile handles a make decoded from the VIN — so the one-tap
     * scan can pick the manufacturer itself instead of making the user do it.
     *
     * Keyed on the exact make strings VinDecode produces. A make we have no
     * profile for returns null: the caller leaves the user's own choice alone
     * rather than guessing, because scanning a Renault with the BMW walker would
     * just waste time on a bus that will never answer.
     */
    fun forMake(make: String?): Brand? {
        val id = when (make?.trim()?.lowercase()) {
            "bmw", "mini" -> "bmw"
            "mazda" -> "mazda"
            "ford" -> "ford"
            "nissan", "infiniti" -> "nissan"
            "peugeot", "citroen", "citroën", "ds automobiles" -> "peugeot"
            "volkswagen", "audi", "skoda", "škoda", "seat", "cupra" -> "vw"
            "mercedes-benz", "mercedes", "smart" -> "mercedes"
            "fiat", "alfa romeo", "lancia", "abarth", "jeep" -> "fiat"
            "jaguar", "land rover" -> "jlr"
            "porsche" -> "porsche"
            else -> return null
        }
        return byId(id)
    }
}
